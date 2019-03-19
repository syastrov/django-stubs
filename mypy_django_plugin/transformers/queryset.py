import dataclasses
from typing import Union, Dict, Optional, NamedTuple

from mypy.nodes import StrExpr, TypeInfo, Context
from mypy.plugin import MethodContext, CheckerPluginInterface
from mypy.types import Type, Instance, AnyType, TypeOfAny

from mypy_django_plugin import helpers
from mypy_django_plugin.transformers.fields import get_private_descriptor_type


def extract_proper_type_for_values_list(ctx: MethodContext) -> Type:
    # TODO: Support .values also

    object_type = ctx.type
    if not isinstance(object_type, Instance):
        return ctx.default_return_type

    flat = helpers.parse_bool(helpers.get_argument_by_name(ctx, 'flat'))
    named = helpers.parse_bool(helpers.get_argument_by_name(ctx, 'named'))

    ret = ctx.default_return_type

    any_type = AnyType(TypeOfAny.implementation_artifact)
    fields_arg_expr = ctx.args[ctx.callee_arg_names.index('fields')]

    model_arg: Union[AnyType, Type] = ret.args[0] if len(ret.args) > 0 else any_type

    # TODO: Base on config setting
    use_strict_types = True

    fill_column_types = True

    field_names = []
    field_types = {}

    # Figure out each field name passed to fields
    for field_expr in fields_arg_expr:
        if not isinstance(field_expr, StrExpr):
            # Dynamic field names are not supported (partial support is possible for values_list, but not values)
            fill_column_types = False
            break
        field_name = field_expr.value
        field_names.append(field_name)

        # Default to any type
        field_types[field_name] = any_type

    if use_strict_types and fill_column_types:
        if isinstance(model_arg, Instance):
            model_type_info = model_arg.type
            field_types = refine_lookup_types(ctx, field_types, model_type_info)

    if named and flat:
        ctx.api.fail("'flat' and 'named' can't be used together.", ctx.context)
        return ret
    elif named:
        # TODO: Fill in namedtuple fields/types
        row_arg = ctx.api.named_generic_type('typing.NamedTuple', [])
    elif flat:
        if len(ctx.args[0]) > 1:
            ctx.api.fail("'flat' is not valid when values_list is called with more than one field.", ctx.context)
            return ret
        if fill_column_types:
            row_arg = field_types[field_names[0]]
        else:
            row_arg = any_type
    else:
        if fill_column_types:
            args = [
                field_types[field_name]
                for field_name in field_names
            ]
        else:
            args = [any_type]
        row_arg = ctx.api.named_generic_type('builtins.tuple', args)

    new_type_args = [model_arg, row_arg]
    return helpers.reparametrize_instance(ret, new_type_args)


@dataclasses.dataclass
class RelatedModelNode:
    typ: Instance
    is_nullable: bool


@dataclasses.dataclass
class FieldNode:
    typ: Type


@dataclasses.dataclass
class PrimitiveNode:
    typ: Type


LookupNode = Union[RelatedModelNode, FieldNode, PrimitiveNode]


def resolve_lookup(api: CheckerPluginInterface, context: Context, model_type_info: TypeInfo, lookup: str,
                   current_node: LookupNode = None) -> Optional[LookupNode]:
    if lookup == '':
        return current_node

    lookup_parts = lookup.split("__")
    lookup_part = lookup_parts.pop(0)

    new_node = None
    if current_node is None:
        new_node = resolve_model_lookup(api, context, model_type_info, lookup_part)
    elif isinstance(current_node, RelatedModelNode):
        new_node = resolve_model_lookup(api, context, current_node.typ.type, lookup_part)
    elif isinstance(current_node, FieldNode):
        # new_node = resolve_field_lookup(api, context, , lookup_part)
        raise Exception(f"Field lookups not yet supported for lookup {lookup}")
    remaining_lookup = "__".join(lookup_parts)
    return resolve_lookup(api, context, model_type_info, remaining_lookup, new_node)


def resolve_model_lookup(api: CheckerPluginInterface, context: Context, model_type_info: TypeInfo, lookup: str) -> Optional[LookupNode]:
    lookups_metadata = helpers.get_lookups_metadata(model_type_info)
    lookup_metadata = lookups_metadata.get(lookup)
    if lookup_metadata is None:
        api.fail(
            f'"{lookup}" is not a valid lookup on model {model_type_info.name()}',
            context)
        return None

    related_name = lookup_metadata.get('related_name', None)
    if related_name:
        # If the lookup is a related lookup, then look at the field specified by related_name.
        # This is to support if related_query_name is set and differs from.
        field_name = related_name
    else:
        field_name = lookup

    field_node = model_type_info.get(field_name)
    if not field_node:
        api.fail(
            f'When resolving lookup "{lookup}", field "{field_name}" was not found in model {model_type_info.name()}',
            context)
        return None

    field_node_type = field_node.type
    if field_node_type is None or not isinstance(field_node_type, Instance):
        api.fail(
            f'When resolving lookup "{lookup}", could not determine field type for {model_type_info.name()}.{field_name}',
            context)
        return None

    if helpers.has_any_of_bases(field_node_type.type, (helpers.FOREIGN_KEY_FULLNAME,
                                                       helpers.ONETOONE_FIELD_FULLNAME)):
        field_type = helpers.extract_field_getter_type(field_node_type)
        is_nullable = helpers.is_field_nullable(model_type_info, field_name)

        if isinstance(field_type, Instance):
            return RelatedModelNode(typ=field_type, is_nullable=is_nullable)
    field_type = helpers.extract_field_getter_type(field_node_type)
    if field_type:
        # If it's a field, return the type of the Field's getter
        return FieldNode(typ=field_type)
    else:
        # Not a Field, just a normal type
        related_manager_arg = None
        if field_node_type.type.has_base(helpers.RELATED_MANAGER_CLASS_FULLNAME):
            related_manager_arg = field_node_type.args[0]

        if related_manager_arg is not None:
            # Reverse relation
            return RelatedModelNode(typ=related_manager_arg, is_nullable=True)
        else:
            return PrimitiveNode(typ=field_node_type)


def resolve_values_lookup(api: CheckerPluginInterface, context: Context, model_type_info: TypeInfo, lookup: str):
    node = resolve_lookup(api, context, model_type_info, lookup)
    if isinstance(node, RelatedModelNode):
        # Related models used in values/values_list get resolved to the primary key of the related model.
        related_model_type = node.typ
        pk_type = helpers.extract_primary_key_type_for_get(related_model_type.type)
        if not pk_type:
            # extract get type of AutoField
            autofield_info = api.lookup_typeinfo('django.db.models.fields.AutoField')
            pk_type = get_private_descriptor_type(autofield_info, '_pyi_private_get_type',
                                                  is_nullable=False)
        related_primary_key_type = pk_type
        if node.is_nullable:
            related_primary_key_type = helpers.make_optional(related_primary_key_type)
        return related_primary_key_type
    if node is not None:
        return node.typ
    else:
        return None


def refine_lookup_types(ctx, lookup_types: Dict[str, Type], model_type_info: TypeInfo):
    return {
        lookup: resolve_values_lookup(ctx.api, ctx.context, model_type_info, lookup) or lookup_types[lookup]
        for lookup in lookup_types.keys()
    }

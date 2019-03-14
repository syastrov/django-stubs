from typing import Union, Dict

from mypy.nodes import StrExpr, TypeInfo
from mypy.plugin import MethodContext
from mypy.types import Type, Instance, AnyType, TypeOfAny

from mypy_django_plugin import helpers


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
            lookup_and_refine_field_types(ctx, field_types, model_type_info)

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


def lookup_and_refine_field_types(ctx, field_types: Dict[str, Type], model_type_info: TypeInfo):
    lookups_metadata = helpers.get_lookups_metadata(model_type_info)

    for field_name in field_types.keys():
        lookup_metadata = lookups_metadata.get(field_name)
        if lookup_metadata is None:
            ctx.api.fail(
                f'"{field_name}" is not a valid lookup on model {model_type_info.name()}',
                ctx.context)
            continue

        if lookup_metadata.get('is_field', False):
            field_node = model_type_info.get(field_name)
            if not field_node:
                ctx.api.fail(
                    f'Field "{field_name}" was not found in model {model_type_info.name()}',
                    ctx.context)
                continue
            field_node_type = field_node.type

            if field_node_type is not None:
                field_getter_type = helpers.extract_field_getter_type(field_node_type)
            else:
                field_getter_type = None
            if not field_getter_type:
                ctx.api.fail(
                    f'Could not determine field type for {model_type_info.name()}.{field_name} in call to values_list.',
                    ctx.context)
                continue
            field_types[field_name] = field_getter_type
        else:
            # Not a field, just use the type on the model
            # TODO: This is really a special case for id/related_id fields
            field_node = model_type_info.get(field_name)
            if not field_node:
                ctx.api.fail(
                    f'Field "{field_name}" was not found in model {model_type_info.name()}',
                    ctx.context)
                continue

            field_node_type = field_node.type
            is_related_manager = False

            # if hasattr(field_node_type, "bases"):
            #     # TODO: Make this a function
            #     for i, base in enumerate(field_node_type.bases):
            #         if base.type.fullname() == helpers.RELATED_MANAGER_CLASS_FULLNAME:
            #             is_related_manager = True
            #             field_node_type = base.type
            #             break
            #
            # field_types[field_name] = field_node_type
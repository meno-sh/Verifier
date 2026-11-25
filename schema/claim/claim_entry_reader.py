"""Reading the keys of one object of the claim register, with the type of each checked.

ClaimRegisterParser (schema/claim/claim_register_parser.py) makes one reader for each claim,
and one for each source.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from enum import Enum

from schema import primitives
from schema.inputs import NUMBER, SCALAR_TYPES, ClaimRegisterError, FieldType, Scalar
from schema.primitives import MethodName, read_only


def frozen_json_value(value: object) -> object:
    """A JSON value with every object made a read-only mapping and every list a tuple."""
    if isinstance(value, dict):
        return read_only({k: frozen_json_value(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(frozen_json_value(v) for v in value)
    return value


class ClaimEntryReader:
    """Reads the keys of one object of the claim register, and checks the type of each.

    Each reader method reads one key, checks the type of its value, and gives it in the
    type the records of schema/inputs.py declare, or raises ClaimRegisterError. A key
    that is absent or null reads as None.
    """

    def __init__(self, raw: dict[str, object], label: str, base_folder: str) -> None:
        """Read `raw`, which error messages name as `label`, with paths from `base_folder`."""
        self.raw = raw
        self.label = label
        self.base_folder = base_folder

    def refuse_unknown_keys(self, allowed: Collection[str]) -> None:
        """Raise if the object has a key outside `allowed`, so a misspelt key is never ignored."""
        unknown = sorted(set(self.raw) - set(allowed))
        if unknown:
            raise ClaimRegisterError(f"{self.label}: unknown key(s) {', '.join(unknown)}; "
                                     f"allowed are {', '.join(sorted(allowed))}")

    def optional_value[T](self, key: str, kind: type[T] | tuple[type[T], ...]) -> T | None:
        """A key whose value must have the given type, or None when it is absent.

        A JSON true or false only passes where bool is the type asked for, although
        Python counts it as the number 1 or 0.
        """
        value = self.raw.get(key)
        if value is None:
            return None
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if isinstance(value, float) and not math.isfinite(value):
            raise ClaimRegisterError(f"{self.label}: `{key}` must be a finite number, "
                                     f"got {value}")
        if isinstance(value, kinds) and (bool in kinds or not isinstance(value, bool)):
            return value
        raise self._wrong_type_error(key, " or ".join(k.__name__ for k in kinds), value)

    def required_value[T](self, key: str, kind: type[T] | tuple[type[T], ...]) -> T:
        """A key whose value must have the given type, and which must be present."""
        value = self.optional_value(key, kind)
        if value is None:
            raise ClaimRegisterError(f"{self.label}: `{key}` is required")
        return value

    def tolerance(self) -> float | None:
        """The optional `tol`, which must be above zero: zero or less fails every recompute."""
        tol = self.optional_value("tol", NUMBER)
        if tol is not None and tol <= 0:
            raise ClaimRegisterError(f"{self.label}: `tol` must be above zero, got {tol}")
        return tol

    def text_list(self, key: str) -> tuple[str, ...] | None:
        """A list of strings, as a tuple, or None when absent."""
        value = self.raw.get(key)
        if value is None:
            return None
        if not isinstance(value, list):
            raise self._wrong_type_error(key, "list", value)
        if not all(isinstance(v, str) for v in value):
            raise ClaimRegisterError(f"{self.label}: every entry of `{key}` must be a string")
        return tuple(value)

    def scalar_mapping(self, key: str) -> Mapping[str, Scalar] | None:
        """An optional object whose values are all plain values, as a read-only mapping."""
        value = self.raw.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise self._wrong_type_error(key, "dict", value)
        if not all(isinstance(v, SCALAR_TYPES) for v in value.values()):
            raise ClaimRegisterError(
                f"{self.label}: every value in `{key}` must be a string, number or boolean")
        return read_only(value)

    def path_relative_to_register(self, key: str) -> str:
        """The required relative path at `key`, resolved against the base folder."""
        return self._resolve_relative_path(key, self.required_value(key, str))

    def optional_path_relative_to_register(self, key: str) -> str | None:
        """The relative path at `key`, resolved against the base folder, or None when absent."""
        value = self.optional_value(key, str)
        return None if value is None else self._resolve_relative_path(key, value)

    def _resolve_relative_path(self, key: str, value: str) -> str:
        """`value` resolved against the base folder.

        An absolute path is an error, because it names a file on one machine only.
        """
        resolved = primitives.path_in_folder(self.base_folder, value)
        if resolved is None:
            raise ClaimRegisterError(f"{self.label}: `{key}` {value!r} is an absolute path; "
                                     "write it relative to the claim register's folder")
        return resolved

    def field_declared_by_a_check(self, key: str, field_type: FieldType) -> object:
        """One key a claim check declared, read as its declared type, or None when absent.

        A path is resolved against the base folder, like every other path in the register.
        """
        match field_type:
            case FieldType.INTEGER:
                return self.optional_value(key, int)
            case FieldType.NUMBER:
                return self.optional_value(key, NUMBER)
            case FieldType.BOOLEAN:
                return self.optional_value(key, bool)
            case FieldType.TEXT:
                return self.optional_value(key, str)
            case FieldType.TEXT_LIST:
                return self.text_list(key)
            case FieldType.OBJECT:
                return self.inline_object(key)
            case FieldType.PATH:
                return self.optional_path_relative_to_register(key)
        raise TypeError(f"no reader for {field_type!r}")

    def inline_object(self, key: str) -> Mapping[str, object] | None:
        """An inline object, made read-only all the way down."""
        value = self.raw.get(key)
        if value is None:
            return None
        if isinstance(value, dict):
            return read_only({k: frozen_json_value(v) for k, v in value.items()})
        raise self._wrong_type_error(key, "dict", value)

    def method_name(self, method_names: Collection[MethodName]) -> MethodName:
        """The required `method`, which must be one that a method file defines."""
        name = self.required_value("method", str)
        if name not in method_names:
            known = (", ".join(sorted(method_names))
                     or "none: give a folder of method files")
            raise ClaimRegisterError(f"{self.label}: `method` {name!r} is not a known "
                                     f"computational method; known are {known}")
        return name

    def checksums(self, key: str) -> str | Mapping[str, str] | None:
        """One checksum, or an object mapping each pin key (a file path) to its checksum."""
        value = self.raw.get(key)
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
            return read_only(value)
        raise ClaimRegisterError(f"{self.label}: `{key}` must be a string, or an object of "
                                 "strings")

    def proposed_checksums(self) -> Mapping[str, str] | None:
        """The optional `proposed_sha256`: an object mapping each pin key to a checksum."""
        value = self.checksums("proposed_sha256")
        if isinstance(value, str):
            raise ClaimRegisterError(f"{self.label}: `proposed_sha256` must map each pin "
                                     "key to a checksum")
        return value

    def enum_value[E: Enum](self, key: str, kind: type[E]) -> E:
        """A required key whose value must be one of the values of an enum."""
        value = self.required_value(key, str)
        try:
            return kind(value)
        except ValueError:
            accepted = ", ".join(sorted(m.value for m in kind))
            raise ClaimRegisterError(f"{self.label}: `{key}` {value!r} is not one of "
                                     f"{accepted}") from None

    def _wrong_type_error(self, key: str, expected: str, value: object) -> ClaimRegisterError:
        """The error for a key whose value has the wrong type."""
        return ClaimRegisterError(f"{self.label}: `{key}` must be {expected}, "
                                  f"got {type(value).__name__}")

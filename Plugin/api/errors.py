"""Structured command errors and JSON-safe API response values."""

from __future__ import annotations

import math
import numbers
import os
from collections.abc import Mapping
from enum import Enum
from pathlib import PurePath
from typing import Any


_CYCLE_MARKER = "<cycle>"
_MAX_DEPTH_MARKER = "<max-depth>"
_OBJECT_IDENTITY_ATTRIBUTES = (
    "name",
    "name_full",
    "type",
    "bl_idname",
    "identifier",
    "idname",
    "label",
    "filepath",
)


def json_safe(value: Any, *, max_depth: int = 12) -> Any:
    """Project arbitrary command data onto deterministic JSON value types.

    Blender RNA objects frequently appear in validation details.  Their default
    representations are neither JSON serializable nor stable across processes,
    so opaque objects are represented by a small identity record instead.  The
    active-object stack handles cycles without collapsing repeated, non-cyclic
    values elsewhere in the payload.
    """
    if max_depth < 0:
        raise ValueError("max_depth must be zero or greater")
    return _json_safe(value, depth=0, max_depth=max_depth, active=set())


def _json_safe(value: Any, *, depth: int, max_depth: int, active: set[int]) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value

    if isinstance(value, Enum):
        return _json_safe(value.value, depth=depth, max_depth=max_depth, active=active)

    if isinstance(value, numbers.Integral):
        return int(value)

    if isinstance(value, numbers.Real):
        numeric = float(value)
        if math.isfinite(numeric):
            return value if isinstance(value, float) else numeric
        if math.isnan(numeric):
            return "NaN"
        return "Infinity" if numeric > 0 else "-Infinity"

    if isinstance(value, numbers.Number):
        # Decimal and complex values are not accepted by ``json.dumps``.  A
        # string preserves their exact diagnostic value without guessing at a
        # lossy numeric conversion.
        return str(value)

    if isinstance(value, PurePath):
        return str(value)

    if isinstance(value, os.PathLike):
        path = os.fspath(value)
        return path.decode("utf-8", errors="replace") if isinstance(path, bytes) else str(path)

    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")

    if depth >= max_depth:
        return _MAX_DEPTH_MARKER

    object_id = id(value)
    if object_id in active:
        return _CYCLE_MARKER

    if isinstance(value, Mapping):
        active.add(object_id)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                base_key = _json_key(key)
                json_key = base_key
                suffix = 2
                while json_key in result:
                    json_key = f"{base_key}#{suffix}"
                    suffix += 1
                result[json_key] = _json_safe(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    active=active,
                )
            return result
        finally:
            active.remove(object_id)

    if isinstance(value, (list, tuple)):
        active.add(object_id)
        try:
            return [
                _json_safe(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    active=active,
                )
                for item in value
            ]
        finally:
            active.remove(object_id)

    if isinstance(value, (set, frozenset)):
        active.add(object_id)
        try:
            projected = [
                _json_safe(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    active=active,
                )
                for item in value
            ]
            return sorted(projected, key=_json_sort_token)
        finally:
            active.remove(object_id)

    return _json_object_identity(
        value,
        depth=depth,
        max_depth=max_depth,
        active=active,
    )


def _json_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, numbers.Real):
        projected = _json_safe(value, depth=0, max_depth=1, active=set())
        return str(projected)
    if isinstance(value, numbers.Number):
        return str(value)
    if isinstance(value, (PurePath, os.PathLike)):
        return str(os.fspath(value))

    type_name = type(value).__name__
    for attribute in _OBJECT_IDENTITY_ATTRIBUTES:
        candidate = _safe_attribute(value, attribute)
        if candidate is not None and isinstance(candidate, (str, bool, numbers.Number)):
            return f"{type_name}:{attribute}={candidate}"
    return type_name


def _json_sort_token(value: Any) -> str:
    """Return a stable ordering token without calling an object's repr."""
    if isinstance(value, dict):
        items = sorted((key, _json_sort_token(item)) for key, item in value.items())
        return "{" + ",".join(f"{key}:{item}" for key, item in items) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_json_sort_token(item) for item in value) + "]"
    return f"{type(value).__name__}:{value}"


def _json_object_identity(
    value: Any,
    *,
    depth: int,
    max_depth: int,
    active: set[int],
) -> dict[str, Any]:
    object_id = id(value)
    if object_id in active:
        return {"__type__": type(value).__name__, "__value__": _CYCLE_MARKER}

    active.add(object_id)
    try:
        result: dict[str, Any] = {"__type__": type(value).__name__}
        for attribute in _OBJECT_IDENTITY_ATTRIBUTES:
            candidate = _safe_attribute(value, attribute)
            if candidate is None or callable(candidate):
                continue
            result[attribute] = _json_safe(
                candidate,
                depth=depth + 1,
                max_depth=max_depth,
                active=active,
            )

        rna = _safe_attribute(value, "bl_rna")
        rna_identifier = _safe_attribute(rna, "identifier") if rna is not None else None
        if isinstance(rna_identifier, str) and rna_identifier:
            result["rna_identifier"] = rna_identifier

        if isinstance(value, BaseException):
            result["message"] = str(value)
        return result
    finally:
        active.remove(object_id)


def _safe_attribute(value: Any, attribute: str) -> Any:
    try:
        return getattr(value, attribute)
    except Exception:
        return None


class CommandError(RuntimeError):
    """Error carrying support-reporting metadata for CLI/API callers."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "COMMAND_FAILED",
        stage: str | None = None,
        details: list | dict | None = None,
        artifacts: dict | None = None,
        context: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = json_safe(details) if details is not None else None
        self.artifacts = json_safe(artifacts or {})
        self.context = json_safe(context or {})

    def to_response_error(self) -> dict:
        payload = {
            "code": self.code,
            "type": self.__class__.__name__,
            "message": str(self),
        }
        if self.stage:
            payload["stage"] = self.stage
        if self.details is not None:
            payload["details"] = self.details
        return json_safe(payload)

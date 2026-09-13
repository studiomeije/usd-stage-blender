"""The validator is the single capability authority.

`validate` and the export-time warning pass each kept their own list of which
Blender node types are supported. The extractor's copy had drifted 14 entries
behind, so TEX_NOISE, CLAMP, MAP_RANGE, REROUTE and others exported correctly
while being reported as "unrecognized; export may differ" - and INVERT as
"requires baking" - directly contradicting what `validate` had just said about
the same material.

Measured before the fix: a plain Value -> Clamp -> Roughness graph exported
successfully and emitted
"Material 'M': Node 'Clamp' (CLAMP) is unrecognized; export may differ."
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.extract import core  # noqa: E402
from Plugin.nodes import validate  # noqa: E402


def _warning_pass_source() -> str:
    return inspect.getsource(core.collect_material_warnings)


def test_the_warning_pass_does_not_hardcode_a_supported_list():
    """A second literal set is how the two drifted apart in the first place."""
    source = _warning_pass_source()
    assert "supported_types = set(" in source, (
        "the supported-type set must be derived, not re-declared"
    )
    assert "'BSDF_PRINCIPLED'," not in source, (
        "a literal node-type list has reappeared in the warning pass"
    )


def test_the_warning_pass_does_not_hardcode_a_bake_list():
    source = _warning_pass_source()
    assert "bake_types = set(" in source
    assert "'TEX_MAGIC'," not in source


def test_validator_supported_and_bake_sets_stay_disjoint():
    """A type in both would make the warning phrasing arbitrary."""
    overlap = validate.SUPPORTED_TYPES & validate.BAKE_TYPES
    assert overlap == set(), f"types declared both supported and bake-only: {overlap}"


def test_types_the_validator_accepts_are_not_called_unrecognized():
    """The property that failed: CLAMP was supported and still warned."""
    source = _warning_pass_source()
    # The warning pass must consult the validator's set for this decision.
    assert "_VALIDATOR_SUPPORTED_TYPES" in source
    assert "CLAMP" in validate.SUPPORTED_TYPES


def test_math_operation_tables_stay_in_sync_with_the_validator():
    """The resolver's op -> nodedef tables and the validator's supported-op
    set must describe the same capability, or validate says yes while the
    export ships an unresolved warning (or vice versa)."""
    resolver_ops = (
        set(core._MATH_SINGLE_INPUT_OPS)
        | set(core._MATH_TWO_INPUT_OPS)
        | set(core._MATH_NAMED_INPUT_OPS)
        | set(core._MATH_COMPOSED_OPS)
    )
    assert resolver_ops == set(validate.SUPPORTED_MATH_OPERATIONS)


def test_the_warning_pass_derives_math_support_from_the_validator():
    source = _warning_pass_source()
    assert "_VALIDATOR_SUPPORTED_MATH_OPS" in source
    assert "math_refusal_message" in source


def test_semantic_mismatch_operations_stay_refused():
    """MODULO (truncated fmod vs MaterialX floored modulo) and the ops with
    no exact nodedef must never drift into the supported set silently."""
    refused = {
        'MODULO', 'SMOOTH_MIN', 'SMOOTH_MAX', 'PINGPONG', 'WRAP', 'SNAP',
        'COMPARE', 'INVERSE_SQRT', 'TRUNC', 'LESS_THAN', 'GREATER_THAN',
    }
    overlap = refused & set(validate.SUPPORTED_MATH_OPERATIONS)
    assert overlap == set(), f"refused operations leaked into support: {overlap}"

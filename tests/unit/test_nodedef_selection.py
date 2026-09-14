"""A supplied type is a constraint on nodedef selection, not a hint.

`select_nodedef_name_for_node` used to fall through to progressively looser
indexes when an exact match was missing: signature, then input/output, then
output-only, then *any* nodedef of that name. The looser steps ignore the
constraints the caller supplied, and callers treat any non-None result as
success, so the "no mapping" diagnostic never fired.

Measured before the guard, against the shipped manifest:

- `convert` has no `color3->float` entry in `by_node_io`, and the output-only
  fallback returned `ND_convert_boolean_float` — whose input type is `boolean`.
  It was authored with a `color3f` `in` and wired to `inputs:roughness`.
- `luminance` has no `float` output at all, and the by-node fallback returned
  `ND_luminance_color3` for an `output_type` of `float`.

Both shipped from a plain `Image Texture -> RGB to BW -> Roughness` graph with
`ok: true` and no diagnostics. RealityKit cannot bind either.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.manifest.materialx_nodes import (  # noqa: E402
    load_manifest,
    select_nodedef_name_for_node,
)


@pytest.fixture(scope="module")
def manifest():
    return load_manifest()


def _nodedef(manifest, name):
    return manifest["nodes"][name]


def _output_types(node_def):
    return {entry.get("type") for entry in node_def.get("outputs", [])}


def test_impossible_conversion_returns_none(manifest):
    """color3 -> float has no convert nodedef; do not invent one."""
    assert select_nodedef_name_for_node(
        manifest, "convert", input_type="color3", output_type="float"
    ) is None


def test_unavailable_output_type_returns_none(manifest):
    """luminance has no float output; do not return the color3 one."""
    assert select_nodedef_name_for_node(
        manifest, "luminance", output_type="float"
    ) is None


@pytest.mark.parametrize(
    "node_name,input_type,output_type,expected",
    [
        ("convert", "float", "color3", "ND_convert_float_color3"),
        ("convert", "color3", "vector3", "ND_convert_color3_vector3"),
    ],
)
def test_valid_conversions_still_resolve(
    manifest, node_name, input_type, output_type, expected
):
    assert select_nodedef_name_for_node(
        manifest, node_name, input_type=input_type, output_type=output_type
    ) == expected


def test_output_only_lookup_still_works_when_no_input_type_is_given(manifest):
    """The looser index is legitimate when the caller constrains less."""
    selected = select_nodedef_name_for_node(
        manifest, "luminance", output_type="color3"
    )
    assert selected == "ND_luminance_color3"


def test_unconstrained_lookup_still_works(manifest):
    assert select_nodedef_name_for_node(manifest, "normalmap") == "ND_normalmap"


def test_a_returned_nodedef_always_honours_the_requested_output_type(manifest):
    """The property the fallbacks used to violate."""
    for node_name in ("convert", "luminance", "image", "normalmap", "mix"):
        for output_type in ("float", "color3", "vector3"):
            selected = select_nodedef_name_for_node(
                manifest, node_name, output_type=output_type
            )
            if selected is None:
                continue
            declared = _output_types(_nodedef(manifest, selected))
            assert output_type in declared, (
                f"{node_name} -> {output_type} selected {selected}, which "
                f"declares outputs {sorted(declared)}"
            )


# ---------------------------------------------------------------------------
# Regression: the constraint must filter candidates, not disable the fallback.
#
# The first attempt at the guard above skipped the looser indexes entirely
# whenever the caller supplied a type. That broke every node whose manifest
# entry has only a by_node list of type-suffixed variants and no io/output
# index - separate4 among them - so exporting any transparent material failed
# with "No separate4 nodedef found for color4 inputs". The fallback is
# legitimate; returning a candidate that violates the constraint is not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_type,expected",
    [("color4", "ND_separate4_color4"), ("vector4", "ND_separate4_vector4")],
)
def test_by_node_fallback_still_resolves_type_suffixed_variants(
    manifest, input_type, expected
):
    """separate4 has no by_node_io and no by_node_output entry at all."""
    assert select_nodedef_name_for_node(
        manifest, "separate4", input_type=input_type
    ) == expected


def test_a_returned_nodedef_always_accepts_the_requested_input_type(manifest):
    """The mirror of the output-type property, on the input axis."""
    for node_name in ("convert", "separate4", "separate3", "image", "mix"):
        for input_type in ("float", "color3", "color4", "vector3", "vector4"):
            selected = select_nodedef_name_for_node(
                manifest, node_name, input_type=input_type
            )
            if selected is None:
                continue
            declared = {
                entry.get("type")
                for entry in manifest["nodes"][selected].get("inputs", [])
                if entry.get("type")
            }
            assert not declared or input_type in declared, (
                f"{node_name} with input {input_type} selected {selected}, "
                f"which declares inputs {sorted(declared)}"
            )


# ---------------------------------------------------------------------------
# Every literal (node, type) request the resolver makes must be satisfiable.
#
# _nodedef_for now raises on an impossible ask instead of leaking a bare node
# name into the expression tree. Two literal call sites were unsatisfiable
# when this guard landed - luminance/float (RGB-to-BW) and separate3/color3
# (RGB Curves) - and both shipped fabricated info:id values. This test walks
# the resolver's source so a new call site with an impossible pair fails here,
# in milliseconds, instead of at an artist's export.
# ---------------------------------------------------------------------------


def test_every_literal_nodedef_request_in_the_resolver_is_satisfiable(manifest):
    import ast
    from pathlib import Path

    extract_dir = (
        Path(__file__).resolve().parents[2] / "Plugin" / "export" / "materials" / "extract"
    )
    calls = [
        node
        for module in sorted(extract_dir.glob("*.py"))
        for node in ast.walk(ast.parse(module.read_text()))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "_nodedef_for")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "_nodedef_for")
        )
    ]
    assert len(calls) > 100

    unsatisfiable = []
    for node in calls:
        args = [
            arg.value if isinstance(arg, ast.Constant) else None
            for arg in node.args
        ]
        keywords = {
            kw.arg: kw.value.value
            for kw in node.keywords
            if isinstance(kw.value, ast.Constant)
        }
        name = args[0] if args else None
        output_type = args[1] if len(args) > 1 else keywords.get("output_type")
        input_type = keywords.get("input_type")
        if name is None or (len(node.args) > 1 and output_type is None):
            continue  # dynamic arguments; covered by the runtime raise
        if select_nodedef_name_for_node(
            manifest, name, input_type=input_type, output_type=output_type
        ) is None:
            unsatisfiable.append((node.lineno, name, input_type, output_type))

    assert unsatisfiable == [], (
        "these _nodedef_for call sites can never resolve and would fail every "
        f"material that reaches them: {unsatisfiable}"
    )


def test_editor_unresolvable_nodedefs_are_never_selected(manifest):
    """The pbrlib closure defs exist in the manifest but only in RCP's USD
    parsing libraries, so the ShaderGraph editor cannot resolve them. Selection must skip them entirely; without
    the filter the by_node fallback happily returned ND_dielectric_bsdf."""
    for node_name in ("dielectric_bsdf", "mix_bsdf", "displacement", "surface"):
        selected = select_nodedef_name_for_node(manifest, node_name)
        node = manifest["nodes"].get(selected) if selected else None
        assert not (
            node and node.get("policy", {}).get("editor_unresolvable")
        ), (node_name, selected)

"""The shader-implementation checker must trust evidence, not names.

``scripts/check_shader_implementations.py`` exists, as
docs/APPLE_PLATFORM_CONTRACT.md explains, for two traps: a nodedef can resolve
in RealityKit's store and still
have no shader, and a Metal symbol is not always spelled like its nodedef. The
second trap caught the checker itself. ``ND_realitykit_pbr_surfaceshader_2_0``
is implemented by ``ND_realitykit_pbr_surfaceshader_v2``, declared in an
``<implementation function="...">`` element the checker deliberately ignored,
so it reported PBR Surface 2 as unbuildable - while Reality Composer Pro built
an export of it, rendered it, and recorded the surface in its own
``.tm_material``.

The logic is tested against synthetic tables, where it is the subject. The
facts are tested against the installed platform, where they live.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts import check_shader_implementations as checker  # noqa: E402

PBR2 = "ND_realitykit_pbr_surfaceshader_2_0"
PBR2_FUNCTION = "ND_realitykit_pbr_surfaceshader_v2"

needs_rcp = pytest.mark.skipif(
    not Path(checker.METAL_LIBRARIES["1.38"]).exists(),
    reason="Reality Composer Pro is not installed here",
)


def _tables(*, symbols=(), nodegraphs=(), functions=None):
    """The (symbols, nodegraphs, functions) triple ``_is_implemented`` reads."""
    return (tuple(sorted(symbols)), frozenset(nodegraphs), dict(functions or {}))


# ---------------------------------------------------------------------------
# The verdict, on synthetic tables
# ---------------------------------------------------------------------------


def test_a_declared_function_that_is_shipped_counts():
    """The measured case: the nodedef name matches nothing, the function does."""
    tables = _tables(
        symbols=[PBR2_FUNCTION + "_c0ffee"],
        functions={PBR2: PBR2_FUNCTION},
    )
    assert checker._is_implemented(PBR2, tables)


def test_a_declared_function_that_is_not_shipped_does_not_count():
    """The caveat the old code guarded, kept.

    An implementation element is a claim. Reading it must not turn a missing
    function into a pass - that is the exact failure the script exists to find.
    """
    tables = _tables(symbols=[], functions={PBR2: PBR2_FUNCTION})
    assert not checker._is_implemented(PBR2, tables)


def test_a_bare_function_name_is_matched():
    """Implementation functions need not carry the ND_ prefix.

    The InternalRealityKit texture families are bound as bare symbols. A
    harvest that kept only ND_* could never match them, so every one read as
    unimplemented whatever the XML declared.
    """
    nodedef = "ND_InternalRealityKitTexture2DArrayRead_vector4"
    function = "InternalRealityKitTexture2DArrayRead_vector4"
    tables = _tables(symbols=[function + "_1a2b3c"], functions={nodedef: function})
    assert checker._is_implemented(nodedef, tables)


def test_nodegraph_expansion_and_direct_symbol_still_win():
    """The two pre-existing routes are untouched."""
    assert checker._is_implemented("ND_x", _tables(nodegraphs=["ND_x"]))
    assert checker._is_implemented("ND_y", _tables(symbols=["ND_y_deadbeef"]))


def test_nothing_declared_anywhere_still_fails():
    assert not checker._is_implemented("ND_nowhere_float", _tables())


# ---------------------------------------------------------------------------
# Reading the element
# ---------------------------------------------------------------------------


def test_other_backends_are_not_claims_about_realitykit():
    """A genosl or genglsl function is for another renderer entirely.

    Honouring one would pass a node RealityKit cannot build - a false pass,
    which is worse than the false fail this change removes.
    """
    for target in ("genosl", "genglsl"):
        element = f'<implementation name="i" nodedef="ND_d" function="f" target="{target}"/>'
        assert checker._implementation_function(element) is None, target


@pytest.mark.parametrize(
    "target_attribute",
    [
        "",
        ' target="realitykit"',
        ' target="realitykit_surface_shader"',
        ' target="realitykit_geometry_modifier"',
        ' target="realitykit_post_lighting_shader"',
        ' target="genmsl"',
    ],
)
def test_realitykit_and_targetless_claims_are_read(target_attribute):
    """No target means every backend; PBR Surface 2's own element has none."""
    element = f'<implementation name="i" nodedef="ND_d" function="f"{target_attribute}/>'
    assert checker._implementation_function(element) == ("ND_d", "f")


def test_an_element_without_a_function_makes_no_claim():
    """sourcecode= and nodegraph= implementations are not verified here yet."""
    element = '<implementation name="i" nodedef="ND_d" sourcecode="x.metal" target="realitykit"/>'
    assert checker._implementation_function(element) is None


# ---------------------------------------------------------------------------
# The installed platform
# ---------------------------------------------------------------------------


@needs_rcp
def test_pbr_surface_2_is_implemented_on_this_machine():
    """Assert against the shipped XML and library, not against our own table.

    Three facts, each measured from Apple's files: the XML maps PBR2 to the
    renamed function; that function is in the Metal library; the nodedef name
    on its own is not - which is the defect, and why matching names failed.
    """
    functions = checker.xml_implementation_functions("1.38")
    symbols = checker.implemented_symbols("1.38")

    assert functions.get(PBR2) == PBR2_FUNCTION
    assert checker._has_symbol(symbols, PBR2_FUNCTION)
    assert not checker._has_symbol(symbols, PBR2)

    tables = (symbols, checker.xml_implemented("1.38"), functions)
    assert checker._is_implemented(PBR2, tables)


@needs_rcp
def test_the_widened_harvest_sees_bare_symbols_on_this_machine():
    """The InternalRealityKit families really are shipped without ND_."""
    symbols = checker.implemented_symbols("1.38")
    assert checker._has_symbol(symbols, "InternalRealityKitTexture2DArrayRead_vector4")


def _layer(nodedef: str) -> str:
    return f'''#usda 1.0

def Material "Probe"
{{
    string config:mtlx:version = "1.38"

    def Shader "Surface"
    {{
        uniform token info:id = "{nodedef}"
    }}
}}
'''


@needs_rcp
def test_scan_passes_pbr_surface_2_and_still_fails_a_made_up_nodedef(tmp_path):
    """End to end: the fix rescues PBR2 and softens nothing else."""
    tables = {
        version: (
            checker.implemented_symbols(version),
            checker.xml_implemented(version),
            checker.xml_implementation_functions(version),
        )
        for version in checker.METAL_LIBRARIES
    }
    good = tmp_path / "pbr2.usda"
    good.write_text(_layer(PBR2))
    bad = tmp_path / "bad.usda"
    bad.write_text(_layer("ND_realitykit_no_such_surfaceshader"))

    assert checker.scan(good, tables) == []
    assert [(m, n) for m, n, _ in checker.scan(bad, tables)] == [
        ("Probe", "ND_realitykit_no_such_surfaceshader")
    ]

"""Unit tests for the texture-bake reuse key (``_make_cache_key``).

The bake loop assigns one shared baked material per ``_make_cache_key`` result,
so objects that share a source material + mesh under identical bake parameters
reuse a single baked material instead of each getting a private copy+bake. That
shared binding is what lets the USD exporter emit instanceable references (e.g.
all 8 pawns of a chess set collapse to ~2 marble texture sets, not 16).

These tests pin the key's discrimination contract directly - they need no
Blender and no full export. ``bake_textures`` imports ``bpy`` at module load, so
a minimal stub is injected before import; ``_make_cache_key`` itself is pure and
takes only primitives.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ``bake_textures`` does ``import bpy`` at module scope. Inject a stub so the
# module imports under plain pytest; the key function never touches bpy.
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.export.bake_textures import _make_cache_key, _strip_baked_suffix  # noqa: E402


def _key(**overrides):
    base = dict(
        source_material_name="Marble",
        mesh_id=1001,
        resolution=2048,
        uv_layer="UVMap",
        bake_mode="LIT_IBL",
        bake_base=True,
        use_opacity=False,
        bake_roughness_map=False,
        roughness_single=False,
        is_flat=False,
    )
    base.update(overrides)
    return _make_cache_key(**base)


def test_key_is_hashable_and_usable_as_dict_key():
    # The chess-set case: two pawns, same marble material + same mesh + same
    # params -> same key -> one shared baked material -> instanceable. A dict
    # round-trip needs both __eq__ and __hash__ to agree, so it covers plain
    # equality too.
    cache = {_key(): "baked_marble"}
    assert cache[_key()] == "baked_marble"


def test_different_source_material_does_not_collide():
    assert _key(source_material_name="Marble") != _key(source_material_name="Gold")


def test_different_mesh_does_not_collide():
    # A baked texture is tied to a UV layout; objects sharing a material but not
    # a mesh must not share a bake even if every other parameter matches.
    # Keyed on datablock identity (id(obj.data)), not name.
    assert _key(mesh_id=1001) != _key(mesh_id=2002)


def test_different_resolution_does_not_collide():
    assert _key(resolution=2048) != _key(resolution=1024)


def test_different_uv_layer_does_not_collide():
    assert _key(uv_layer="UVMap") != _key(uv_layer="UVMap.001")


def test_different_bake_mode_does_not_collide():
    assert _key(bake_mode="LIT_IBL") != _key(bake_mode="LIT_ALBEDO")


def test_each_flag_discriminates():
    for flag in ("bake_base", "use_opacity", "bake_roughness_map", "roughness_single", "is_flat"):
        assert _key(**{flag: False}) != _key(**{flag: True}), flag


def test_resolution_compared_as_int():
    # Sources resolving to the same size via different types still collide.
    assert _key(resolution=2048) == _key(resolution=2048.0)


def test_uv_layer_none_and_empty_collapse():
    # ``_get_active_uv`` can hand back None vs ""; both mean "no named layer".
    assert _key(uv_layer=None) == _key(uv_layer="")


# --- _strip_baked_suffix: keep re-bakes from compounding names ---------------

def test_strip_no_suffix_is_unchanged():
    assert _strip_baked_suffix("_1___Default") == "_1___Default"


def test_strip_compounded_baked_chain():
    name = "_1___Default_Baked_3_Baked_Baked_Baked_Baked_Baked_Baked_Baked"
    assert _strip_baked_suffix(name) == "_1___Default"


def test_strip_preserves_non_baked_numeric_suffix():
    # A real name component ending in digits is not a _Baked run.
    assert _strip_baked_suffix("_2___Default_001") == "_2___Default_001"
    assert _strip_baked_suffix("_2___Default_001_Baked") == "_2___Default_001"


def test_strip_idempotent_basename():
    # Re-baking any of these yields the same stable base -> "<base>_Baked".
    base = "Marble"
    for n in ("Marble", "Marble_Baked", "Marble_Baked_Baked", "Marble_Baked_2_Baked"):
        assert _strip_baked_suffix(n) == base


def test_strip_never_returns_empty():
    assert _strip_baked_suffix("_Baked") == "_Baked"


def test_strip_does_not_eat_real_names():
    # The fragile direction: only a trailing "_Baked"/"_Baked_<n>" run is a bake
    # product. Names that merely contain "baked" must survive untouched, so a
    # future regex tweak can't silently start eating real material names.
    for name in (
        "Baked",          # no leading underscore
        "Rebaked",        # "baked" mid-word
        "Baked_Clay",     # "_Baked" not at the end
        "Wood_Baked_v2",  # trailing _v2 breaks the anchor
        "Marble_Baked01",  # no underscore before digits
        "Clay_baked",     # case-sensitive: lowercase is not our suffix
    ):
        assert _strip_baked_suffix(name) == name, name


def test_cache_key_does_not_strip_baked():
    # Intentional asymmetry: the reuse key keys on the raw source name, NOT the
    # stripped one. Two genuinely different sources "Foo" and "Foo_Baked" must
    # stay distinct so they never wrongly share a bake.
    assert _key(source_material_name="Foo") != _key(source_material_name="Foo_Baked")

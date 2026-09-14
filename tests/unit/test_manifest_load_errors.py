"""A damaged manifest tells the user to reinstall and is not swallowed.

The extension ships the manifest; users have no repository scripts to rebuild
it. Extraction used to turn a load failure into an empty manifest, which
surfaced later as a misleading "no nodedef satisfies" error.
"""

from __future__ import annotations

import pytest

from Plugin.export.materials.extract import core
from Plugin.manifest import materialx_nodes


def test_a_missing_manifest_says_to_reinstall(tmp_path, monkeypatch):
    monkeypatch.setattr(materialx_nodes, "_manifest_path", lambda: tmp_path / "missing.json")
    with pytest.raises(materialx_nodes.ManifestError, match="Reinstall the USD Stage for Blender extension"):
        materialx_nodes.load_manifest()


def test_a_corrupt_manifest_is_not_swallowed_by_extraction(tmp_path, monkeypatch):
    corrupt = tmp_path / "rk_nodes_manifest.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(materialx_nodes, "_manifest_path", lambda: corrupt)
    monkeypatch.setattr(core, "_MANIFEST_CACHE", None)
    with pytest.raises(materialx_nodes.ManifestError, match="Reinstall"):
        core._get_manifest()

"""Which file an authored asset path names - one answer for every stager."""

from __future__ import annotations

from pathlib import Path

import pytest

from Plugin.export import asset_paths, usd_assets, usd_textures


@pytest.mark.parametrize("url, path", [
    ("file:///Users/me/My%20Tex/a.png", "/Users/me/My Tex/a.png"),
    ("FILE:///Users/me/a.png", "/Users/me/a.png"),
    ("file://localhost/tmp/a%23b.png", "/tmp/a#b.png"),
    ("/plain/a.png", "/plain/a.png"),
])
def test_a_file_url_becomes_its_path(url, path):
    assert asset_paths.normalize_file_url(url) == path


def test_a_network_file_url_keeps_its_server():
    """Dropping the host would name a different, local file."""
    assert asset_paths.normalize_file_url("file://server/share/a.png") == "//server/share/a.png"


@pytest.mark.parametrize("path", [
    "http://example.com/a.png", "HTTPS://example.com/a.png", "anon:0x1:root.usda",
    "data:image/png;base64,AA", "s3://bucket/a.png", "omniverse://host/a.usd",
])
def test_a_path_with_any_other_scheme_names_no_local_file(path):
    assert asset_paths.local_asset_path(path) is None
    assert asset_paths.is_non_file_asset(path)


def test_a_windows_drive_is_a_path_not_a_scheme():
    assert asset_paths.asset_scheme("C:\\tex\\a.png") == ""
    assert asset_paths.local_asset_path("C:\\tex\\a.png") == Path("C:\\tex\\a.png")


def test_both_stagers_use_the_one_implementation():
    """Two copies of these drifted apart once; there is now only one."""
    assert usd_assets._normalize_file_url is asset_paths.normalize_file_url
    assert usd_textures._local_asset_path is asset_paths.local_asset_path
    assert usd_assets._unique_destination_name is asset_paths.unique_destination_name
    assert usd_textures._unique_destination_name is asset_paths.unique_destination_name

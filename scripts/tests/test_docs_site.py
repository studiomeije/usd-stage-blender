"""The documentation site's link rewriting, and that every current link resolves.

``scripts/docs_site.py`` maps a link written for GitHub to where its target is
on the site. MkDocs checks Markdown links itself, but not raw HTML or links
that leave the site, so the hook checks those, and these tests check the hook.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "docs_site", REPOSITORY_ROOT / "scripts" / "docs_site.py")
docs_site = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(docs_site)

REPO_BLOB = docs_site.REPO_BLOB


def rewrite(markdown, src_uri="USER_GUIDE.md", dest_uri="USER_GUIDE/index.html", exists=None):
    return docs_site.rewrite_links(
        markdown, src_uri, dest_uri, exists=exists or (lambda _path: True))


@pytest.mark.parametrize(("repo_path", "site"), [
    ("README.md", "index.md"),
    ("CONTRIBUTING.md", "contributing.md"),
    ("CHANGELOG.md", "changelog.md"),
    ("docs/README.md", "documentation.md"),
    ("docs/SETTINGS.md", "SETTINGS.md"),
    ("docs/images/user-guide/export.png", "images/user-guide/export.png"),
    ("LICENSE", REPO_BLOB + "LICENSE"),
    ("Plugin/export/variants.py", REPO_BLOB + "Plugin/export/variants.py"),
])
def test_each_repository_path_has_one_place_on_the_site(repo_path, site):
    assert docs_site.site_target(repo_path) == site


def test_a_docs_page_links_back_to_the_root_pages():
    text, missing = rewrite(
        "[install](../README.md#install), [tests](../CONTRIBUTING.md), "
        "[index](README.md), [settings](SETTINGS.md#panel-map)")

    assert text == (
        "[install](index.md#install), [tests](contributing.md), "
        "[index](documentation.md), [settings](SETTINGS.md#panel-map)")
    assert missing == []


def test_a_root_page_links_into_docs_and_out_to_repository_files():
    text, _ = rewrite(
        "[guide](docs/USER_GUIDE.md) [docs](docs/README.md) [licence](LICENSE)",
        src_uri="index.md", dest_uri="index.html")

    assert text == (
        f"[guide](USER_GUIDE.md) [docs](documentation.md) [licence]({REPO_BLOB}LICENSE)")


def test_raw_html_is_made_relative_to_the_page_url():
    """USER_GUIDE.md is served from USER_GUIDE/, so its images are one up."""
    text, _ = rewrite('<img src="images/user-guide/export.png" alt="">')

    assert text == '<img src="../images/user-guide/export.png" alt="">'


def test_raw_html_on_the_home_page_stays_at_the_root():
    text, _ = rewrite('<img src="docs/images/usd-stage-for-blender.png">',
                      src_uri="index.md", dest_uri="index.html")

    assert text == '<img src="images/usd-stage-for-blender.png">'


def test_a_raw_html_link_to_a_page_goes_to_its_directory_url():
    text, _ = rewrite('<a href="SETTINGS.md#panel-map">settings</a>')

    assert text == '<a href="../SETTINGS/#panel-map">settings</a>'


def test_absolute_links_and_anchors_are_left_alone():
    source = "[web](https://example.com/a.md) [here](#contents) [root](/x.md) <a href=\"mailto:a@b\">"
    text, missing = rewrite(source)

    assert text == source
    assert missing == []


def test_code_is_never_rewritten():
    source = (
        "Call `handlers[kind](node)` here.\n\n"
        "```markdown\n[the guide](docs/USER_GUIDE.md)\n```\n\n"
        "  ~~~\n  [a](../README.md)\n  ~~~\n\n"
        "[after](../README.md)\n"
    )
    text, missing = rewrite(source)

    assert text == source.replace("[after](../README.md)", "[after](index.md)")
    assert missing == []


def test_a_link_to_a_missing_file_is_reported():
    text, missing = rewrite(
        '<img src="images/user-guide/typo.png"> [gone](../NOPE.md) [ok](SETTINGS.md)',
        exists=lambda path: path != "docs/images/user-guide/typo.png" and path != "NOPE.md")

    assert missing == ["docs/images/user-guide/typo.png", "NOPE.md"]
    assert "../images/user-guide/typo.png" in text


@pytest.mark.parametrize("source", [
    "README.md", "CONTRIBUTING.md", "CHANGELOG.md",
    *sorted(str(p.relative_to(REPOSITORY_ROOT)) for p in (REPOSITORY_ROOT / "docs").glob("*.md")),
])
def test_every_link_in_the_published_pages_resolves(source):
    """The hook's own check, over the real pages, without needing MkDocs."""
    path = REPOSITORY_ROOT / source
    if source.startswith("docs/"):
        name = Path(source).name
        src_uri = docs_site.DOCS_INDEX if name == "README.md" else name
    else:
        src_uri = docs_site.ROOT_PAGES[source]
    stem = src_uri[:-len(".md")]
    dest_uri = "index.html" if stem == "index" else f"{stem}/index.html"

    _, missing = docs_site.rewrite_links(
        path.read_text(encoding="utf-8"), src_uri, dest_uri,
        exists=lambda repo_path: (REPOSITORY_ROOT / repo_path).exists())

    assert missing == []

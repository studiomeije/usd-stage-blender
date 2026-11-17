"""MkDocs hooks that publish the repository's root pages alongside docs/.

The Markdown is written for GitHub: README.md sits at the repository root and
links into docs/, and the pages in docs/ link back with ``../``. The site
build serves one flat set of pages, so these hooks bring README.md,
CONTRIBUTING.md and CHANGELOG.md in as pages and rewrite every relative link
from where it points in the repository to where that file is on the site.
The sources are not changed, so they keep working on GitHub.

MkDocs validates Markdown links itself, but not raw HTML or links that leave
the site, so the hook checks each path it rewrites and warns on one that does
not exist; ``mkdocs build --strict`` turns that warning into a failure.
"""

from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path

#: Where a link to a repository file that is not part of the site goes.
REPO_BLOB = "https://github.com/studiomeije/usd-stage-blender/blob/main/"

#: Root files published as pages: {repository path: site page}.
ROOT_PAGES = {
    "README.md": "index.md",
    "CONTRIBUTING.md": "contributing.md",
    "CHANGELOG.md": "changelog.md",
}

#: docs/README.md is the documentation index. The repository README takes
#: index.md, so the docs index is published under its own name.
DOCS_INDEX = "documentation.md"

#: A Markdown link target, or an HTML ``src`` or ``href``, without its anchor.
_LINK = re.compile(r'(\]\(|src="|href=")([^)"\s#]+)(#[^)"\s]*)?')

#: Fenced code blocks, then inline code spans: never rewritten.
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$", re.M | re.S)
_CODE_SPAN = re.compile(r"(`+)(?!`).*?(?<!`)\1", re.S)

#: A logger under ``mkdocs.`` so --strict counts its warnings.
log = logging.getLogger("mkdocs.hooks.docs_site")


def on_files(files, config):
    """Add the root pages, and rename the docs index out of index.md's way."""
    from mkdocs.structure.files import File

    root = Path(config["docs_dir"]).parent
    docs_index = files.get_file_from_path("README.md")
    if docs_index is not None:
        files.remove(docs_index)
        files.append(File.generated(config, DOCS_INDEX, abs_src_path=docs_index.abs_src_path))
    for source, page in ROOT_PAGES.items():
        files.append(File.generated(config, page, abs_src_path=str(root / source)))
    return files


def on_page_markdown(markdown, page, config, files):
    """Point each relative link at the site page or file it names."""
    root = Path(config["docs_dir"]).parent
    rewritten, missing = rewrite_links(
        markdown, page.file.src_uri, page.file.dest_uri,
        exists=lambda repo_path: (root / repo_path).exists(),
    )
    for repo_path in missing:
        log.warning("%s links to '%s', which does not exist in the repository",
                    page.file.src_uri, repo_path)
    return rewritten


def rewrite_links(markdown, src_uri, dest_uri, exists=lambda _path: True):
    """Rewrite a page's relative links for the site.

    Returns the rewritten Markdown and the repository paths it links to that
    ``exists`` says are missing. Code blocks and code spans are left as they
    are, since a link-shaped sample there is text, not a link.
    """
    origin = {page: source for source, page in ROOT_PAGES.items()}.get(src_uri)
    base = "" if origin is not None else "docs/"
    # MkDocs resolves a Markdown link against the source file, but passes raw
    # HTML through: a src or href has to be relative to the page's URL, which
    # for USER_GUIDE.md is USER_GUIDE/, one level below the docs root.
    page_dir = posixpath.dirname(dest_uri) or "."
    missing: list[str] = []

    def rewrite(match):
        prefix, target = match.group(1), match.group(2)
        if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("/"):
            return match.group(0)
        repo_path = posixpath.normpath(base + target)
        if not exists(repo_path) and repo_path not in missing:
            missing.append(repo_path)
        resolved = site_target(repo_path)
        if prefix != "](" and not resolved.startswith("https:"):
            html = _html_target(resolved)
            resolved = posixpath.relpath(html, page_dir)
            if html.endswith("/") and not resolved.endswith("/"):
                resolved += "/"
        return f"{prefix}{resolved}{match.group(3) or ''}"

    def outside_code(text):
        pieces, last = [], 0
        for span in _CODE_SPAN.finditer(text):
            pieces.append(_LINK.sub(rewrite, text[last:span.start()]))
            pieces.append(span.group(0))
            last = span.end()
        pieces.append(_LINK.sub(rewrite, text[last:]))
        return "".join(pieces)

    pieces, last = [], 0
    for fence in _FENCE.finditer(markdown):
        pieces.append(outside_code(markdown[last:fence.start()]))
        pieces.append(fence.group(0))
        last = fence.end()
    pieces.append(outside_code(markdown[last:]))
    return "".join(pieces), missing


def site_target(repo_path: str) -> str:
    """Where a repository path is on the site, relative to the docs root."""
    if repo_path in ROOT_PAGES:
        return ROOT_PAGES[repo_path]
    if repo_path == "docs/README.md":
        return DOCS_INDEX
    if repo_path.startswith("docs/"):
        return repo_path[len("docs/"):]
    return REPO_BLOB + repo_path


def _html_target(site_path: str) -> str:
    """An HTML link to a page goes to its directory URL; a file stays a file."""
    if site_path.endswith(".md"):
        stem = site_path[:-len(".md")]
        return "./" if stem == "index" else f"{stem}/"
    return site_path

"""MkDocs hooks that publish the repository's root pages alongside docs/.

The Markdown is written for GitHub: README.md sits at the repository root and
links into docs/, and the pages in docs/ link back with ``../``. The site
build serves one flat set of pages, so these hooks bring README.md,
CONTRIBUTING.md and CHANGELOG.md in as pages and rewrite every relative link
from where it points in the repository to where that file is on the site.
The sources are not changed, so they keep working on GitHub.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from mkdocs.structure.files import File

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

#: A Markdown link target or an HTML ``src``, without its anchor.
_LINK = re.compile(r'(\]\(|src=")([^)"\s#]+)(#[^)"\s]*)?')


def on_files(files, config):
    """Add the root pages, and rename the docs index out of index.md's way."""
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
    source = page.file.src_uri
    origin = {page: source for source, page in ROOT_PAGES.items()}.get(source)
    base = "" if origin is not None else "docs/"

    # MkDocs resolves a Markdown link against the source file, but passes raw
    # HTML through: an <img src> has to be relative to the page's URL, which
    # for USER_GUIDE.md is USER_GUIDE/, one level below the docs root.
    page_dir = posixpath.dirname(page.file.dest_uri) or "."

    def rewrite(match):
        target = match.group(2)
        if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("/"):
            return match.group(0)
        anchor = match.group(3) or ""
        resolved = site_target(posixpath.normpath(base + target))
        if match.group(1) == 'src="' and not resolved.startswith("https:"):
            resolved = posixpath.relpath(resolved, page_dir)
        return f"{match.group(1)}{resolved}{anchor}"

    return _LINK.sub(rewrite, markdown)


def site_target(repo_path: str) -> str:
    """Where a repository path is on the site, relative to a top-level page."""
    if repo_path in ROOT_PAGES:
        return ROOT_PAGES[repo_path]
    if repo_path == "docs/README.md":
        return DOCS_INDEX
    if repo_path.startswith("docs/"):
        return repo_path[len("docs/"):]
    return REPO_BLOB + repo_path

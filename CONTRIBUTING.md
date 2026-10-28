# Contributing to USD Stage for Blender

Thank you for helping. This page covers setting up a checkout, running the
tests, and where a change usually goes. How the code is organised, and the
step-by-step for the common extensions, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Reporting a problem

Open an issue on [GitHub](https://github.com/studiomeije/usd-stage-blender/issues)
with a support bundle attached; [Reporting a problem](docs/CLI.md#reporting-a-problem)
says how to create one. Search the open issues first.

## Setting up a checkout

You need Blender 5.2 LTS, Python 3.13, and [Git LFS](https://git-lfs.com): the
bundled node-group library and the test scenes are LFS files, and without LFS
they check out as pointer files that Blender cannot open.

```bash
git lfs install
git clone https://github.com/studiomeije/usd-stage-blender.git
```

Create a virtual environment with the versions CI pins
(`.github/workflows/ci.yml`):

```bash
python3.13 -m venv .venv
```

```bash
.venv/bin/pip install pytest==9.1.1 usd-core==26.8 numpy==2.3.4 PyYAML==6.0.3 pillow==12.3.0 ruff==0.16.7
```

To try the add-on from the checkout, build the extension archive and install
it in Blender with **Edit ▸ Preferences ▸ Extensions ▸ Add-ons ▸ Install from
Disk…**:

```bash
python3 scripts/release_archive.py
```

The archive is written to `dist/`. The command line runs straight from the
checkout:

```bash
alias usdstage="python3 $PWD/Plugin"
```

## Running the tests

The unit tests run without Blender, in a few seconds:

```bash
.venv/bin/python -m pytest tests/unit -q
```

The integration tests drive a real Blender in the background. They are skipped
unless `USDSTAGE_BLENDER` names a Blender 5.2 executable:

```bash
USDSTAGE_BLENDER=/Applications/Blender.app/Contents/MacOS/Blender .venv/bin/python -m pytest tests/integration -q
```

Lint with the rule set in `ruff.toml`, which CI runs on every revision:

```bash
.venv/bin/ruff check .
```

A change that alters what an export writes needs a test that exports it. The
integration tests build the scenes they need in Blender at test time, so a test
does not need a committed `.blend`. Material fidelity is checked with
`scripts/check_shader_implementations.py`; `usdchecker` and `realitytool`
accept a file whose materials RealityKit will drop, so neither proves a
material survived.

## Making a change

- Branch from `dev` and open the pull request against `dev`.
- Blender 5.2 LTS is the only supported version. Do not add compatibility code
  for older releases.
- An export that cannot be translated faithfully stops with an error that names
  the node or setting and the fix. Do not add silent fallbacks.
- Keep each commit to one change, with a message that says what changed for the
  user and why.

## Documentation

The pages under `docs/` describe how the current version behaves, for people
using the add-on and people changing it. Write what is true now: not the
history of a change, not who found a bug, and no line numbers, which go stale.
A change in behaviour updates the page that describes it and adds an entry to
[CHANGELOG.md](CHANGELOG.md) written for users: what now works, what now stops
the export, and what they have to do differently.

## License

By contributing you agree that your contribution is licensed under the
[GNU General Public License, version 3 or later](LICENSE), the project's
license.

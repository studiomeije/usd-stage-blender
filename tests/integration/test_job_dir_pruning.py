"""Finished job directories are pruned; live ones never are.

Every background bake left a `.usdstage_jobs/<id>/` directory next to the
export and nothing removed it, so the folder grew without bound beside the asset
the user ships. Each holds a status file, a log and any diagnostics, worth
keeping for the last few runs - so prune rather than delete on completion.

The safety property is that a queued or running job, or one whose status cannot
be read yet, is never eligible: pruning must not be able to delete the tree a
live runner is writing into.

Runs in Blender because bake_export_operator imports bpy.props at module load.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

_DRIVER = r'''
import json, os, sys, time, tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[sys.argv.index("--") + 1])
import Plugin
Plugin.register()
from Plugin.ops import bake_export_operator as beo

root = Path(tempfile.mkdtemp()) / ".usdstage_jobs"
root.mkdir(parents=True)

def job(name, state, age):
    d = root / name
    d.mkdir()
    if state is not None:
        (d / "status.json").write_text(json.dumps({"state": state, "pid": 1}))
    stamp = time.time() - age
    os.utime(d, (stamp, stamp))

for i in range(8):
    job(f"done_{i}", "done", 1000 - i)
job("live", "running", 99999)
job("queued", "queued", 99999)
job("setup", None, 99999)

beo._prune_finished_job_dirs(root)

kept = sorted(p.name for p in root.iterdir())
print("PRUNE_RESULT " + json.dumps({
    "kept": kept,
    "limit": beo._KEPT_FINISHED_JOB_DIRS,
}))
'''


@pytest.fixture(scope="module")
def prune_result(tmp_path_factory) -> dict:
    workdir = tmp_path_factory.mktemp("job_prune")
    script = workdir / "driver.py"
    script.write_text(_DRIVER)
    repo_root = str(Path(__file__).resolve().parents[2])

    proc = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", repo_root],
        capture_output=True, text=True, timeout=300,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("PRUNE_RESULT "):
            return json.loads(line[len("PRUNE_RESULT "):])
    raise AssertionError(proc.stdout + proc.stderr)


def test_only_the_newest_finished_jobs_survive(prune_result):
    finished = [name for name in prune_result["kept"] if name.startswith("done_")]

    assert len(finished) == prune_result["limit"]
    assert "done_7" in finished, "the newest finished job must survive"


def test_a_running_job_is_never_pruned(prune_result):
    assert "live" in prune_result["kept"], (
        "pruning deleted the directory a live runner is writing into"
    )


def test_a_queued_job_is_never_pruned(prune_result):
    assert "queued" in prune_result["kept"]


def test_a_directory_without_a_status_file_is_never_pruned(prune_result):
    """A job still being set up has no status.json yet."""
    assert "setup" in prune_result["kept"]

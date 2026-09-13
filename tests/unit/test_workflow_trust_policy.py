"""Regression coverage for the protected Apple runner trust boundary.

Every assertion here runs against the real ``.github/workflows`` artifacts:
structural policy is checked on the PyYAML-parsed workflow documents, and the
authorization semantics are checked by executing the actual bash policy script
extracted from the parsed workflow against fixture git repositories. Editing a
workflow in a way that violates the trust policy therefore fails these tests.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = ROOT / ".github/workflows"
RELEASE_WORKFLOW = ROOT / ".github/workflows/build-archive.yml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
FULL_COMMIT_ACTION_REF = re.compile(r"[^\s@]+@[0-9a-f]{40}")
ZERO_SHA = "0" * 40

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "trust-policy-test",
    "GIT_AUTHOR_EMAIL": "trust-policy-test@example.invalid",
    "GIT_COMMITTER_NAME": "trust-policy-test",
    "GIT_COMMITTER_EMAIL": "trust-policy-test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))




def _steps_by_name(job: dict) -> dict[str, dict]:
    return {step["name"]: step for step in job["steps"] if "name" in step}


def _run_scripts(workflow: dict) -> list[tuple[str, str]]:
    """Return every (job_id, run-script) pair defined by a workflow."""

    scripts = []
    for job_id, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []) or []:
            if isinstance(step.get("run"), str):
                scripts.append((job_id, step["run"]))
    return scripts


# ---------------------------------------------------------------------------
# Static policy checkers. Each returns human-readable violations so the same
# checker can be proven to catch tampered workflow fixtures.
# ---------------------------------------------------------------------------




def _privileged_action_pins(workflow: dict) -> tuple[list[str], list[str]]:
    """Return (privileged job ids, unpinned third-party refs they use)."""

    privileged_jobs = []
    violations = []
    for job_id, job in workflow.get("jobs", {}).items():
        permissions = job.get("permissions") or {}
        if permissions.get("contents") != "write":
            continue
        privileged_jobs.append(job_id)
        for step in job.get("steps", []) or []:
            action_ref = step.get("uses")
            if not action_ref or action_ref.startswith(("actions/", "./")):
                continue
            if not FULL_COMMIT_ACTION_REF.fullmatch(action_ref):
                violations.append(f"{job_id}: {action_ref}")
    return privileged_jobs, violations


def _inert_diff_check_lines(workflow: dict) -> list[str]:
    """Find bare `git diff --check` invocations, which can never fail in CI."""

    violations = []
    for job_id, script in _run_scripts(workflow):
        for line in script.splitlines():
            if re.fullmatch(r"\s*git diff --check\s*", line):
                violations.append(f"{job_id}: {line.strip()}")
    return violations


# ---------------------------------------------------------------------------
# Behavioral harness: execute the real authorization script from the workflow.
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Structural policy on the real workflow documents.
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Behavioral policy: the real bash script, executed against fixture repos.
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Release-write hygiene across every workflow document.
# ---------------------------------------------------------------------------


def test_privileged_jobs_pin_third_party_actions_to_full_commit_shas() -> None:
    """A mutable action tag must never receive release-write authority."""

    privileged_jobs: list[str] = []
    violations: list[str] = []
    for workflow_path in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
        workflow = _load_workflow(workflow_path)
        jobs, unpinned = _privileged_action_pins(workflow)
        privileged_jobs.extend(f"{workflow_path.name}:{job_id}" for job_id in jobs)
        violations.extend(f"{workflow_path.name}:{ref}" for ref in unpinned)

    assert privileged_jobs
    assert violations == []

    release_workflow = _load_workflow(RELEASE_WORKFLOW)
    publish_steps = _steps_by_name(release_workflow["jobs"]["publish-release"])
    assert publish_steps["Publish release and assets"]["uses"] == (
        "softprops/action-gh-release@efb35369e0ad2afab669f228072c1b0d510eae64"
    )
    # Keep the human-readable version breadcrumb next to the pin.
    assert (
        "softprops/action-gh-release@"
        "efb35369e0ad2afab669f228072c1b0d510eae64 # v3.0.3"
    ) in RELEASE_WORKFLOW.read_text(encoding="utf-8")


def test_policy_checker_catches_unpinned_privileged_action() -> None:
    """Prove the pin checker fails when a workflow copy drops the SHA pin."""

    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    _, violations = _privileged_action_pins(yaml.safe_load(source))
    assert violations == []

    tampered = source.replace(
        "softprops/action-gh-release@"
        "efb35369e0ad2afab669f228072c1b0d510eae64 # v3.0.3",
        "softprops/action-gh-release@v2",
    )
    assert tampered != source
    _, violations = _privileged_action_pins(yaml.safe_load(tampered))
    assert violations == ["publish-release: softprops/action-gh-release@v2"]


def test_release_tag_is_revalidated_immediately_before_publication() -> None:
    workflow = _load_workflow(RELEASE_WORKFLOW)
    publish_job = workflow["jobs"]["publish-release"]
    step_names = [step["name"] for step in publish_job["steps"] if "name" in step]

    revalidate_name = "Revalidate release tag immediately before publication"
    publish_name = "Publish release and assets"
    assert step_names.index(revalidate_name) < step_names.index(publish_name)

    steps = _steps_by_name(publish_job)
    revalidate_script = steps[revalidate_name]["run"]
    assert '"+refs/tags/${release_tag}:refs/tags/${release_tag}"' in revalidate_script
    assert 'git rev-parse "refs/tags/${release_tag}^{commit}"' in revalidate_script
    assert '[[ "$tag_commit" != "$GITHUB_SHA" ]]' in revalidate_script
    assert steps[publish_name]["with"]["target_commitish"] == "${{ github.sha }}"


# ---------------------------------------------------------------------------
# CI whitespace gate: `git diff --check` must diff the change under test.
# ---------------------------------------------------------------------------


def test_no_workflow_runs_an_inert_bare_git_diff_check() -> None:
    for workflow_path in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
        assert _inert_diff_check_lines(_load_workflow(workflow_path)) == [], (
            f"{workflow_path.name} runs a bare `git diff --check`, which "
            "compares a pristine checkout to its index and can never fail"
        )


def test_policy_checker_catches_inert_bare_git_diff_check() -> None:
    tampered = yaml.safe_load(
        "jobs:\n"
        "  unit-tests:\n"
        "    steps:\n"
        "      - name: Check repository\n"
        "        run: |\n"
        "          git diff --check\n"
        "          echo done\n"
    )
    assert _inert_diff_check_lines(tampered) == ["unit-tests: git diff --check"]


def test_ci_whitespace_gate_diffs_the_revision_under_test() -> None:
    workflow = _load_workflow(CI_WORKFLOW)
    job = workflow["jobs"]["unit-tests"]

    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    # The gate needs history to resolve the PR base, the push predecessor,
    # and origin/dev; a depth-1 clone would make every base unreachable.
    assert checkout["with"]["fetch-depth"] == 0

    gate = _steps_by_name(job)["Check whitespace introduced by this revision"]
    assert gate["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert gate["env"]["PR_BASE_REF"] == "${{ github.base_ref }}"
    assert gate["env"]["PUSH_BEFORE"] == "${{ github.event.before }}"

    script = gate["run"]
    assert 'git merge-base "refs/remotes/origin/${PR_BASE_REF}" HEAD' in script
    assert f'"$PUSH_BEFORE" != {ZERO_SHA}' in script
    assert "git merge-base refs/remotes/origin/dev HEAD" in script
    assert 'git diff --check "$base" HEAD' in script


# ---------------------------------------------------------------------------
# The portable Specular Tint rejection stays wired into the Apple lane.
# ---------------------------------------------------------------------------

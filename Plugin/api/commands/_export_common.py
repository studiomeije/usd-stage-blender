"""What the ``export`` and ``bake-export`` commands share.

Both write the same diagnostics sidecar on failure, point at the same support
bundle command, and turn the export's own refusals into the same public error
codes.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import CommandError


def save_diagnostics(diag, diagnostics_path: str | None) -> None:
    """Write the diagnostics report, recording where it went."""
    if not diagnostics_path:
        return
    diag.set_artifact("diagnostics_path", diagnostics_path)
    diag.save(Path(diagnostics_path))


def support_hint(blend_file: str | None, output_path: str | None,
                 diagnostics_path: str | None = None) -> str | None:
    """The ``usdstage support-bundle`` command that would capture this export."""
    if not blend_file:
        return None
    parts = ["usdstage", "support-bundle", blend_file]
    if output_path:
        parts.extend(["--export-path", output_path])
    if diagnostics_path:
        parts.extend(["--diagnostics-path", diagnostics_path])
    return " ".join(parts)


def artifacts(diagnostics_path: str | None, output_path: str, blend_file: str | None) -> dict:
    """The files a failed export leaves for the user, for an error payload."""
    return {
        "diagnostics_path": diagnostics_path,
        "support_bundle_hint": support_hint(blend_file, output_path, diagnostics_path),
    }


def command_error(refusal, **extra) -> CommandError:
    """A public :class:`CommandError` for an :class:`ExportRefused`.

    A material refusal lists its issues as ``details`` - without the live
    Blender node, which is not JSON - and names the material in ``context``.
    """
    details = refusal.details
    context = None
    if refusal.issues:
        details = [
            {key: value for key, value in issue.items() if key != "node"}
            for issue in refusal.issues
        ]
        context = {
            "material": refusal.material,
            "errors": [
                f"{issue.get('node_name', '?')} ({issue.get('node_type', '?')}): "
                f"{issue.get('message', '')}"
                for issue in refusal.issues[:10]
            ],
        }
    return CommandError(
        str(refusal), code=refusal.code, stage=refusal.stage,
        details=details, context=context, **extra,
    )

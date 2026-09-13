"""
Export diagnostics and reporting
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Dict, Any
from datetime import datetime


class ExportDiagnostics:
    """Collects and reports export diagnostics"""

    def __init__(self):
        """Initialize diagnostics"""
        self.data = {
            'timestamp': datetime.now().isoformat(),
            'materials': {
                'converted': 0,
                'failed': 0,
                'warnings': [],
            },
            'textures': {
                'copied': 0,
                'converted': 0,
                'failed': [],
            },
            'animations': {
                'enabled': False,
                'fps': None,
                'total_frames': None,
                'segments': [],
                'targets': [],
            },
            'export_context': {},
            'environment': {},
            'phases': [],
            'validation': {
                'unsupported_nodes': [],
            },
            'material_issues': {
                'unsupported_nodes': [],
                'bad_graphs': [],
                'missing_mappings': [],
                'materialx_failures': [],
            },
            'generated_files': [],
            'asset_dependencies': {
                'missing_image_count': 0,
                'missing_images': [],
            },
            'exceptions': [],
            'artifacts': {},
            'errors': [],
            'warnings': [],
        }
        self._active_phases: Dict[str, Dict[str, Any]] = {}

    def set_export_context(self, **context):
        """Record stable context for the export request."""
        self.data['export_context'].update(_json_safe(context))

    def set_environment(self, **environment):
        """Record runtime environment metadata."""
        self.data['environment'].update(_json_safe(environment))

    def set_artifact(self, name: str, path: str | None):
        """Record a generated support artifact path."""
        if path:
            self.data['artifacts'][name] = str(path)

    def begin_phase(self, name: str, context: dict | None = None):
        """Start timing a named phase."""
        phase = {
            'name': name,
            'started_at': datetime.now().isoformat(),
            '_started_monotonic': time.monotonic(),
            'status': 'running',
            'context': _json_safe(context or {}),
        }
        self._active_phases[name] = phase

    def end_phase(self, name: str, status: str = "ok", context: dict | None = None):
        """Finish timing a named phase."""
        phase = self._active_phases.pop(name, None)
        if phase is None:
            phase = {
                'name': name,
                'started_at': None,
                '_started_monotonic': time.monotonic(),
                'context': {},
            }
        started = phase.pop('_started_monotonic', None)
        phase['ended_at'] = datetime.now().isoformat()
        phase['status'] = status
        if started is not None:
            phase['duration_seconds'] = round(max(0.0, time.monotonic() - started), 3)
        if context:
            phase.setdefault('context', {}).update(_json_safe(context))
        self.data['phases'].append(phase)

    def record_phase_error(self, name: str, error: Exception | str, context: dict | None = None):
        """Finish a phase as failed and attach error context."""
        error_message = str(error)
        payload = dict(context or {})
        payload['error'] = error_message
        self.end_phase(name, status="error", context=payload)
        self.add_error(f"{name} failed: {error_message}")

    def add_material_converted(self, material_name: str):
        """Record a successfully converted material"""
        self.data['materials']['converted'] += 1

    def add_material_failed(self, material_name: str, reason: str):
        """Record a failed material conversion"""
        self.data['materials']['failed'] += 1
        self.data['materials']['warnings'].append({
            'material': material_name,
            'reason': reason,
        })
        self.add_material_issue(
            'materialx_failures',
            material=material_name,
            message=reason,
        )
        self.add_error(f"Material conversion failed: {material_name} ({reason})")

    def add_texture_copied(self, texture_path: str, destination_path: str | None = None, **context):
        """Record a copied texture"""
        self.data['textures']['copied'] += 1
        if destination_path or context:
            self.data['textures'].setdefault('copied_files', []).append(_json_safe({
                'source': texture_path,
                'destination': destination_path,
                **context,
            }))

    def add_texture_converted(self, texture_path: str):
        """Record a converted texture"""
        self.data['textures']['converted'] += 1

    def add_texture_failed(self, texture_path: str, reason: str):
        """Record a failed texture operation"""
        self.data['textures']['failed'].append({
            'texture': texture_path,
            'reason': reason,
        })
        self.add_error(f"Texture operation failed: {texture_path} ({reason})")

    def add_error(self, error: str):
        """Add an error message"""
        self.data['errors'].append(error)

    def add_warning(self, warning: str):
        """Add a warning message"""
        self.data['warnings'].append(warning)

    def add_info(self, message: str):
        """Record routine pipeline notes that need no artist action.

        Warnings are the channel for "your asset may not be what you expect";
        the UI shows only the first few and the CLI surfaces them on success.
        Boilerplate that fires on every export - the doubleSided
        normalization note, the post-processing completion line - was filed
        as warnings, so real ones (a black bake, a dropped take) could never
        win those slots.
        """
        self.data.setdefault('info', []).append(message)

    def add_validation_issue(self, material_name: str, issue: dict, severity: str = "error"):
        """Record a structured validation issue."""
        entry = {
            'material': material_name,
            'severity': severity,
            'node_name': issue.get('node_name', ''),
            'node_type': issue.get('node_type', ''),
            'socket': issue.get('socket', ''),
            'message': issue.get('message', ''),
        }
        self.data['validation']['unsupported_nodes'].append(_json_safe(entry))
        self.add_material_issue('unsupported_nodes', **entry)

    def add_material_issue(self, category: str, **issue):
        """Record a structured material graph issue."""
        bucket = self.data['material_issues'].setdefault(category, [])
        bucket.append(_json_safe(issue))

    def add_generated_file(self, role: str, path: str, **context):
        """Record a generated file that may help support triage."""
        self.data['generated_files'].append(_json_safe({
            'role': role,
            'path': path,
            **context,
        }))

    def add_exception(self, exc: Exception, stage: str | None = None, include_traceback: bool = True):
        """Record an exception and optional traceback."""
        entry = {
            'type': exc.__class__.__name__,
            'message': str(exc),
        }
        if stage:
            entry['stage'] = stage
        if include_traceback:
            entry['traceback'] = traceback.format_exc()
        self.data['exceptions'].append(entry)
        self.add_error(f"{stage + ': ' if stage else ''}{exc}")

    def set_animation_schedule(self, fps: int, total_frames: int, segments: list, targets: list):
        """Record animation schedule and targets."""
        self.data['animations']['enabled'] = True
        self.data['animations']['fps'] = fps
        self.data['animations']['total_frames'] = total_frames
        self.data['animations']['segments'] = segments
        self.data['animations']['targets'] = targets

    def to_dict(self) -> Dict[str, Any]:
        """Get diagnostics as dictionary"""
        return self.data.copy()

    def to_json(self, indent: int = 2) -> str:
        """Get diagnostics as JSON string"""
        return json.dumps(self.data, indent=indent)

    def save(self, filepath: Path):
        """Save diagnostics to JSON file"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2)
def _json_safe(value):
    """Convert common non-JSON values to serializable forms."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

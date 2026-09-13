"""
Main UI panel for USD Stage for Blender export
"""

import bpy
import json
from pathlib import Path

from .. import prefs as addon_prefs
from . import brand
from ..api.commands._settings_common import (
    REALITYKIT_OS27_DEFAULTS,
)
from ..ops.background_jobs import (
    ACTIVE_JOB_STATES,
    pid_is_running,
    safe_int,
    status_pid,
)
from ..export_profile import (
    MATERIAL_TYPE_PBR,
    MATERIAL_TYPE_UNLIT,
    PBR_PROCESSING_BAKE,
    PBR_PROCESSING_TRANSLATE,
    PIPELINE_DIRECT,
    UNLIT_APPEARANCE_COLOR,
    UNLIT_APPEARANCE_LIGHTING,
    resolve_ui_export_route,
)
from bpy.app.handlers import persistent
from bpy.props import StringProperty, BoolProperty, EnumProperty, FloatProperty, IntProperty
from bpy.types import Panel, PropertyGroup

_PERSIST_APPLY_SCHEDULED = set()
_MISSING_BACKGROUND_STATUS_MESSAGE = (
    "Background job status file is missing. Clear stale job state, then export again."
)
_STALE_BACKGROUND_STATUS_MESSAGE = (
    "Background job is no longer attached to this Blender session. Clear stale job state."
)
_EXPORT_FORMAT_EXTENSIONS = {
    "USDA": ".usda",
    "USDC": ".usdc",
    "USDZ": ".usdz",
}


def _persist_settings(context, settings) -> None:
    """Persist settings using the shared versioned profile."""
    addon_prefs.persist_export_settings(context, settings)


def _output_path_with_format_extension(filepath: str, export_format: str) -> str:
    filepath = str(filepath or "").strip()
    if not filepath:
        return ""

    extension = _EXPORT_FORMAT_EXTENSIONS.get(export_format, ".usdz")
    try:
        return str(Path(filepath).with_suffix(extension))
    except ValueError:
        return filepath


def _sync_output_path_extension(settings) -> bool:
    filepath = getattr(settings, "filepath", "")
    if not filepath:
        return False

    updated = _output_path_with_format_extension(
        filepath,
        getattr(settings, "export_format", "USDZ"),
    )
    if not updated or updated == filepath:
        return False

    settings.persist_suspended = True
    try:
        settings.filepath = updated
    finally:
        settings.persist_suspended = False
    return True


def _on_settings_changed(self, context) -> None:
    """Update callback for export settings."""
    if getattr(self, "persist_suspended", False):
        return
    _sync_output_path_extension(self)
    _persist_settings(context, self)



_TEXTURE_RESOLUTION_ITEMS = [
    (
        'ORIGINAL',
        "Keep Original",
        "Size each material from its own source textures; do not resize "
        "existing exported textures",
        0,
    ),
    ('512', "512", "512 px", 1),
    ('1024', "1024", "1024 px", 2),
    ('2048', "2048", "2048 px", 3),
    ('4096', "4096", "4096 px", 4),
    ('CUSTOM', "Custom", "Use a custom resolution", 5),
]

_TEXTURE_FORMAT_ITEMS = [
    ('ORIGINAL', "Original", "Keep PNG, JPEG, and OpenEXR encodings; convert AVIF and other LDR inputs to PNG", 0),
    ('PNG', "PNG", "Bake textures as PNG with alpha", 1),
    ('AVIF', "AVIF", "Bake textures as AVIF with alpha. Reality Composer Pro 3 cannot import AVIF; only for packages loaded by RealityKit at runtime", 2),
]


def _enum_index(items, identifier) -> int:
    return next((item[3] for item in items if item[0] == identifier), 0)


def _source_texture_values(settings):
    """Resolution and format a direct export applies: Original while the gate is off."""
    if not settings.export_texture_settings_enabled:
        return 'ORIGINAL', 'ORIGINAL'
    return settings.bake_resolution, settings.bake_image_format


def _set_source_textures(settings, resolution, image_format) -> None:
    """Store a direct-route choice; anything but Original on both turns the gate on."""
    settings.bake_resolution = resolution
    settings.bake_image_format = image_format
    settings.export_texture_settings_enabled = not (
        resolution == 'ORIGINAL' and image_format == 'ORIGINAL'
    )


def _get_ui_source_texture_resolution(self):
    return _enum_index(_TEXTURE_RESOLUTION_ITEMS, _source_texture_values(self)[0])


def _set_ui_source_texture_resolution(self, value):
    resolution = next(item[0] for item in _TEXTURE_RESOLUTION_ITEMS if item[3] == value)
    _set_source_textures(self, resolution, _source_texture_values(self)[1])


def _get_ui_source_texture_format(self):
    return _enum_index(_TEXTURE_FORMAT_ITEMS, _source_texture_values(self)[1])


def _set_ui_source_texture_format(self, value):
    image_format = next(item[0] for item in _TEXTURE_FORMAT_ITEMS if item[3] == value)
    _set_source_textures(self, _source_texture_values(self)[0], image_format)


class USDStageExportSettings(PropertyGroup):
    """Export settings stored in scene"""

    ui_material_type: EnumProperty(
        name="Profile",
        description="Choose the RealityKit material profile authored by the export",
        items=[
            (
                MATERIAL_TYPE_PBR,
                "RealityKit PBR",
                "Export materials that respond to RealityKit lighting",
            ),
            (
                MATERIAL_TYPE_UNLIT,
                "RealityKit Unlit",
                "Export materials shown without RealityKit lighting",
            ),
        ],
        default=MATERIAL_TYPE_PBR,
        update=_on_settings_changed,
    )

    ui_pbr_processing: EnumProperty(
        name="Material Processing",
        description="Translate compatible materials directly or bake them to textures",
        items=[
            (
                PBR_PROCESSING_TRANSLATE,
                "Translate Materials",
                "Translate compatible Blender materials directly to MaterialX",
            ),
            (
                PBR_PROCESSING_BAKE,
                "Bake Materials",
                "Bake material color and roughness before authoring RealityKit PBR",
            ),
        ],
        default=PBR_PROCESSING_TRANSLATE,
        update=_on_settings_changed,
    )

    ui_unlit_appearance: EnumProperty(
        name="Appearance",
        description="Choose what the exported unlit textures contain",
        items=[
            (
                UNLIT_APPEARANCE_COLOR,
                "Material Color Only",
                "Bake material color without Blender lighting or shadows",
            ),
            (
                UNLIT_APPEARANCE_LIGHTING,
                "Lighting & Shadows",
                "Bake Blender lighting and shadows into the final appearance",
            ),
        ],
        default=UNLIT_APPEARANCE_COLOR,
        update=_on_settings_changed,
    )

    filepath: StringProperty(
        name="Output Path",
        description="Path where the USD/USDZ file will be exported",
        default="",
        maxlen=1024,
        subtype='FILE_PATH',
        update=_on_settings_changed,
    )

    export_format: EnumProperty(
        name="Format",
        description="Export format and file extension",
        items=[
            ('USDA', "USD ASCII (.usda)", "Export as USD ASCII (.usda)"),
            ('USDC', "USD Binary (.usdc)", "Export as USD binary (.usdc)"),
            ('USDZ', "USDZ Package (.usdz)", "Export as USDZ package (.usdz)"),
        ],
        default='USDZ',
        update=_on_settings_changed,
    )

    root_prim_name: StringProperty(
        name="Root Prim",
        description="Root prim path or name (e.g. /root or Scene)",
        default="/root",
        update=_on_settings_changed,
    )


    export_animation: BoolProperty(
        name="Export Animation",
        description="Include animation data in the USD export",
        default=False,
        update=_on_settings_changed,
    )

    author_animation_library: BoolProperty(
        name="RCP Clip Library",
        description=(
            "Author Reality Composer Pro AnimationLibrary clip metadata. "
            "Leave off for RealityKit runtime exports; split imported animations in app code."
        ),
        default=False,
        update=_on_settings_changed,
    )

    selected_objects_only: BoolProperty(
        name="Selection Only",
        description="Only export selected objects",
        default=False,
        update=_on_settings_changed,
    )

    export_custom_properties: BoolProperty(
        name="Custom Properties",
        description="Export custom properties as USD attributes",
        default=True,
        update=_on_settings_changed,
    )

    custom_properties_namespace: StringProperty(
        name="Namespace",
        description="Namespace prefix for custom property names",
        default="userProperties",
        update=_on_settings_changed,
    )

    author_blender_name: BoolProperty(
        name="Blender Names",
        description="Author USD attributes with Blender object/data names. Requires Custom Properties export to be enabled",
        default=True,
        update=_on_settings_changed,
    )

    allow_unicode: BoolProperty(
        name="Allow Unicode",
        description="Preserve UTF-8 characters in USD names (USD 24.03+)",
        default=True,
        update=_on_settings_changed,
    )

    clamp_specular_tint: BoolProperty(
        name="Clamp Overbright Specular Tint",
        description=(
            "Clamp a constant, uncoloured Principled Specular Tint above 1 to 1 for this "
            "export only; the .blend is not changed, and coloured or linked tints still stop the export"
        ),
        default=REALITYKIT_OS27_DEFAULTS["clamp_specular_tint"],
        update=_on_settings_changed,
    )

    evaluation_mode: EnumProperty(
        name="Use Settings for",
        description="Choose viewport or render evaluation settings",
        items=[
            ('RENDER', "Render", "Use render settings"),
            ('VIEWPORT', "Viewport", "Use viewport settings"),
        ],
        default='RENDER',
        update=_on_settings_changed,
    )

    triangulate_meshes: BoolProperty(
        name="Triangulate Meshes",
        description="Triangulate meshes during export",
        default=False,
        update=_on_settings_changed,
    )

    quad_method: EnumProperty(
        name="Quad Method",
        description="Method for splitting quads into triangles",
        items=[
            ('SHORTEST_DIAGONAL', "Shortest Diagonal", "Split along the shortest diagonal"),
            ('BEAUTY', "Beauty", "Split for best-looking results"),
            ('FIXED', "Fixed", "Split quads on the first diagonal"),
            ('FIXED_ALTERNATE', "Fixed Alternate", "Split quads on the opposite diagonal"),
        ],
        default='SHORTEST_DIAGONAL',
        update=_on_settings_changed,
    )

    ngon_method: EnumProperty(
        name="N-gon Method",
        description="Method for splitting n-gons into triangles",
        items=[
            ('BEAUTY', "Beauty", "Split for best-looking results"),
            ('EAR_CLIP', "Ear Clip", "Clip ears to split n-gons"),
        ],
        default='BEAUTY',
        update=_on_settings_changed,
    )

    export_subdivision: EnumProperty(
        name="Subdivision",
        description="How subdivision modifiers are exported",
        items=[
            ('IGNORE', "Ignore", "Export base mesh without subdivision"),
            ('TESSELLATE', "Tessellate", "Export subdivided mesh without subdivision scheme"),
            ('BEST_MATCH', "Best Match", "Export subdivision scheme when possible"),
        ],
        default='BEST_MATCH',
        update=_on_settings_changed,
    )

    export_armatures: BoolProperty(
        name="Armatures",
        description="Export armatures as USD skeletons",
        default=True,
        update=_on_settings_changed,
    )

    only_deform_bones: BoolProperty(
        name="Only Deform Bones",
        description="Export only deform bones and parents",
        default=False,
        update=_on_settings_changed,
    )

    export_shapekeys: BoolProperty(
        name="Shape Keys",
        description="Export shape keys as USD blend shapes",
        default=True,
        update=_on_settings_changed,
    )

    use_instancing: BoolProperty(
        name="Instancing",
        description="Export instanced objects as USD references",
        default=True,
        update=_on_settings_changed,
    )

    bake_mode: EnumProperty(
        name="Bake Profile",
        description="The baking Profile: what the baked textures contain and which RealityKit surface they drive",
        items=[
            ('UNLIT_ALBEDO', "RealityKit Unlit ▸ Material Color Only", "Bake light-independent material color and author RealityKit Unlit materials — shown as-is, ignoring scene lighting. Blender shadows are not baked."),
            ('LIT_ALBEDO', "RealityKit PBR ▸ Bake Materials", "Bake light-independent material color and author Lit PBR materials so Reality Composer Pro or RealityKit lights the baked color. Blender shadows are not baked."),
            ('LIT_IBL', "RealityKit Unlit ▸ Lighting & Shadows", "Use when the export should match the Blender preview. Blender lighting and shadows are baked into textures (authored Unlit)."),
        ],
        default='LIT_IBL',
        update=_on_settings_changed,
    )

    bake_ibl_source: EnumProperty(
        name="Lighting Source",
        description="Select which scene world or HDRI lighting source is baked into textures",
        items=[
            ('SCENE_WORLD', "Scene World", "Use the current scene World for the lighting bake"),
            ('HDRI_FILE', "HDRI File", "Use a specific HDRI file for the lighting bake"),
        ],
        default='SCENE_WORLD',
        update=_on_settings_changed,
    )

    bake_ibl_filepath: StringProperty(
        name="HDRI File",
        description="HDRI file used when baking lighting and shadows",
        default="",
        maxlen=1024,
        subtype='FILE_PATH',
        update=_on_settings_changed,
    )

    bake_ibl_strength: FloatProperty(
        name="Lighting Strength",
        description="Lighting strength multiplier for the HDRI bake",
        default=1.0,
        min=0.0,
        update=_on_settings_changed,
    )

    bake_ibl_rotation: FloatProperty(
        name="Lighting Rotation",
        description="Z rotation for the HDRI lighting source",
        default=0.0,
        subtype='ANGLE',
        update=_on_settings_changed,
    )

    bake_isolate_meshes_lit: BoolProperty(
        name="Isolate Meshes for Shadows",
        description="When baking lighting and shadows, hide other meshes while baking each mesh to avoid cross-mesh shadows",
        default=False,
        update=_on_settings_changed,
    )

    bake_step_timeout_seconds: IntProperty(
        name="Step Timeout (sec)",
        description="Maximum duration of one background bake/export step; use 0 for no timeout",
        default=0,
        min=0,
        update=_on_settings_changed,
    )

    ui_source_texture_resolution: EnumProperty(
        name="Maximum Resolution",
        description=(
            "Largest size for source textures in a direct export. Keep Original "
            "copies them at their own size"
        ),
        items=_TEXTURE_RESOLUTION_ITEMS,
        get=_get_ui_source_texture_resolution,
        set=_set_ui_source_texture_resolution,
    )

    ui_source_texture_format: EnumProperty(
        name="Image Format",
        description=(
            "Encoding for source textures in a direct export. Original keeps PNG, "
            "JPEG and OpenEXR and converts other formats to PNG"
        ),
        items=_TEXTURE_FORMAT_ITEMS,
        get=_get_ui_source_texture_format,
        set=_set_ui_source_texture_format,
    )

    export_texture_settings_enabled: BoolProperty(
        name="Optimize Source Textures",
        description="Resize or transcode source textures during direct export",
        default=False,
        update=_on_settings_changed,
    )

    bake_resolution: EnumProperty(
        name="Texture Resolution",
        description=(
            "Resolution for baked textures and opt-in exported texture "
            "overrides. Keep Original sizes each material from its own source "
            "textures instead of forcing one resolution"
        ),
        items=_TEXTURE_RESOLUTION_ITEMS,
        # Source-keyed by default so the sidebar and the CLI agree. The Blender
        # Export button forces export_texture_settings_enabled on, which
        # activates this setting, while the CLI leaves it off unless
        # --resolution/--image-format/--margin is passed. With a fixed default
        # the same scene baked 2048x2048 from the sidebar and 512x512 from the
        # CLI - an 8x upscale of a 256px source in one front end and not the
        # other. _resolve_bake_resolution already treats ORIGINAL and the
        # disabled gate identically, so this makes the two paths converge on
        # the behaviour the code comment there argues for: a 1K material stays
        # 1K rather than being upscaled.
        default='ORIGINAL',
        update=_on_settings_changed,
    )

    bake_image_format: EnumProperty(
        name="Image Format",
        description="File format for texture overrides (Original keeps PNG, JPEG and OpenEXR encodings and converts AVIF and other LDR inputs to PNG)",
        items=_TEXTURE_FORMAT_ITEMS,
        default='PNG',
        update=_on_settings_changed,
    )

    bake_resolution_custom: IntProperty(
        name="Custom Resolution",
        description="Custom bake resolution (pixels)",
        default=2048,
        min=32,
        update=_on_settings_changed,
    )

    bake_margin: IntProperty(
        name="Bake Margin",
        description="Bake padding in pixels",
        default=8,
        min=0,
        update=_on_settings_changed,
    )

    bake_base_color: BoolProperty(
        name="Bake Base Color",
        description="Bake base color textures",
        default=True,
        update=_on_settings_changed,
    )

    bake_opacity: BoolProperty(
        name="Bake Opacity",
        description="Bake opacity textures",
        default=True,
        update=_on_settings_changed,
    )

    bake_roughness_mode: EnumProperty(
        name="Roughness",
        description="How Lit PBR roughness is exported ('RealityKit PBR ▸ Bake Materials' only)",
        items=[
            ('TEXTURE', "Bake Roughness Maps", "Bake a per-texel roughness texture (accurate, larger file)."),
            ('AVERAGE', "Average to Single Value", "Use one averaged roughness constant — no roughness texture exported (smaller file)."),
        ],
        default='TEXTURE',
        update=_on_settings_changed,
    )

    force_unlit_materials: BoolProperty(
        name="Force Unlit Materials",
        description="Force rewrite to RealityKit Unlit materials",
        default=False,
        options={'HIDDEN'},
        update=_on_settings_changed,
    )

    diagnostics_enabled: BoolProperty(
        name="Keep Success Diagnostics",
        description=(
            "Write a diagnostics JSON sidecar for successful exports. "
            "Failed exports always write diagnostics"
        ),
        default=False,
        update=_on_settings_changed,
    )


    last_diagnostics_path: StringProperty(
        name="Last Diagnostics Path",
        description="Last diagnostics JSON file path",
        default="",
        options={'HIDDEN'}
    )

    background_job_dir: StringProperty(
        name="Background Job Dir",
        description="Path to the active background bake/export job",
        default="",
        options={'HIDDEN', 'SKIP_SAVE'}
    )

    background_job_pid: IntProperty(
        name="Background Job PID",
        description="PID for the active background job",
        default=0,
        options={'HIDDEN', 'SKIP_SAVE'}
    )

    history_applied: BoolProperty(
        name="History Applied",
        description="Whether persisted settings were applied",
        default=False,
        options={'HIDDEN', 'SKIP_SAVE'}
    )

    persist_suspended: BoolProperty(
        name="Persist Suspended",
        description="Suspend settings persistence while loading",
        default=False,
        options={'HIDDEN', 'SKIP_SAVE'}
    )



class USDSTAGE_PT_export_panel(Panel):
    """Main export panel"""
    bl_label = "USD Stage Export"
    bl_idname = "USDSTAGE_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "USD Stage"

    def draw_header(self, context):
        brand.draw_header_icon(self.layout, 'EXPORT')

    def draw(self, context):
        """Draw panel UI"""
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = getattr(context.scene, "usd_stage_export_settings", None)
        if settings is None:
            layout.label(text="Export settings unavailable. Reload the add-on.")
            layout.operator("usdstage.export", icon='EXPORT', text="Export Scene")
            return

        try:
            _apply_persisted_settings(context)
            status = _read_background_job_status(settings)
            job_state = status.get("state") if status else None
            job_running = job_state in ACTIVE_JOB_STATES

            # Job monitor first: while a job runs (and everything below is
            # locked), state, progress and the cancel affordance must be
            # visible without scrolling.
            if status:
                _draw_job_monitor(layout, status, job_state, job_running)

            export_box = layout.box()
            export_box.enabled = not job_running
            export_box.prop(settings, "filepath", placeholder="Choose where to save the export")
            export_box.prop(settings, "export_format")
            include = export_box.column(heading="Include", align=True)
            include.prop(settings, "selected_objects_only", text="Selection Only")
            include.prop(settings, "export_animation", text="Animation")
            if settings.export_animation:
                include.prop(settings, "author_animation_library")
            profile_row = export_box.row(align=True)
            profile_row.prop(settings, "ui_material_type", expand=True)

            route = resolve_ui_export_route(settings)
            export_row = export_box.row()
            export_row.enabled = not job_running
            export_row.scale_y = 1.4
            operator_id = (
                "usdstage.export"
                if route.pipeline == PIPELINE_DIRECT
                else "usdstage.bake_export_background"
            )
            operator = export_row.operator(
                operator_id,
                icon='EXPORT',
                text="Export",
            )
            if route.pipeline != PIPELINE_DIRECT:
                operator.apply_ui_profile = True

        except Exception as exc:
            layout.label(text=f"UI error: {exc}", icon='STATUS_ERROR')
            layout.operator("usdstage.export", icon='EXPORT', text="Export Scene")


def _draw_job_monitor(layout, status, job_state, job_running):
    """Background-job status card.

    Header row carries the state plus a compact cancel/clear button, running
    jobs get a real progress bar (the runner's step message doubles as the bar
    label) and a note explaining why the rest of the panel is greyed out, and
    failures render in Blender's alert styling instead of a plain label.
    """
    monitor = layout.box()
    header = monitor.row(align=True)
    if job_running:
        header.label(text="Background Job - Running", icon='TIME')
        header.operator("usdstage.cancel_bake_export", text="", icon='CANCEL')
    else:
        state_row = header.row(align=True)
        if job_state == "error":
            state_row.alert = True
            state_row.label(text="Background Job - Failed", icon='STATUS_ERROR')
        elif job_state == "done":
            state_row.label(text="Background Job - Done", icon='CHECKMARK')
            # A completed bake can still need attention, for example black
            # textures from a scene with no lights; show the runner's warnings.
            for warning_text in (status.get("warnings") or [])[:3]:
                warning_row = monitor.row()
                warning_row.label(text=str(warning_text)[:120], icon='ERROR')
        elif job_state == "canceled":
            state_row.label(text="Background Job - Canceled", icon='CANCEL')
        else:
            state_row.label(
                text=f"Background Job - {job_state or 'Unknown'}",
                icon='STATUS_WARNING',
            )
        header.operator("usdstage.clear_bake_job", text="", icon='TRASH')

    message = str(status.get("message") or "")
    if job_running:
        factor = 0.0
        try:
            factor = max(0.0, min(1.0, float(status.get("progress"))))
        except (TypeError, ValueError):
            pass
        monitor.progress(text=message or f"{int(factor * 100)}%", factor=factor, type='BAR')
        monitor.label(
            text="Settings are locked until the job finishes or is canceled.",
            icon='LOCKED',
        )
    elif message:
        message_column = monitor.column()
        message_column.alert = job_state == "error"
        message_column.label(
            text=message,
            icon='STATUS_ERROR' if job_state == "error" else 'STATUS_INFO',
        )

    if status.get("export_path"):
        monitor.label(text=f"Output: {status.get('export_path')}", icon='FILE')

    file_row = monitor.row(align=True)
    if status.get("log_path"):
        op = file_row.operator("usdstage.open_support_text", icon='TEXT', text="Open Log")
        op.filepath = status.get("log_path")
        op.text_name = "USD Stage Background Log"
    if status.get("diagnostics_path"):
        op = file_row.operator(
            "usdstage.open_diagnostics_text", icon='INFO', text="Open Diagnostics"
        )
        op.filepath = status.get("diagnostics_path")


class USDSTAGE_PT_export_material_settings(Panel):
    """Contextual options for the selected output material type."""

    bl_label = "Material Settings"
    bl_idname = "USDSTAGE_PT_export_material_settings"
    bl_parent_id = "USDSTAGE_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "USD Stage"
    bl_order = 1

    def draw_header(self, context):
        self.layout.label(text="", icon='MATERIAL')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.usd_stage_export_settings
        layout.enabled = not _is_job_running(settings)

        if settings.ui_material_type == MATERIAL_TYPE_PBR:
            processing_row = layout.row(align=True)
            processing_row.prop(settings, "ui_pbr_processing", expand=True)
            if settings.ui_pbr_processing == PBR_PROCESSING_TRANSLATE:
                layout.prop(settings, "clamp_specular_tint")
                if settings.clamp_specular_tint:
                    policy = layout.box()
                    policy.label(
                        text="Export only; the .blend is not changed.",
                        icon='STATUS_WARNING',
                    )
                    policy.label(text="Coloured or linked tints still stop the export.")
            else:
                layout.prop(settings, "bake_roughness_mode", text="Roughness")
                _draw_bake_channel_options(layout, settings)
            return

        appearance_row = layout.row(align=True)
        appearance_row.prop(settings, "ui_unlit_appearance", expand=True)
        if settings.ui_unlit_appearance == UNLIT_APPEARANCE_LIGHTING:
            layout.prop(settings, "bake_ibl_source")
            if settings.bake_ibl_source == 'HDRI_FILE':
                layout.prop(
                    settings,
                    "bake_ibl_filepath",
                    placeholder="//studio.hdr",
                )
                layout.prop(settings, "bake_ibl_strength")
                layout.prop(settings, "bake_ibl_rotation")
            layout.prop(settings, "bake_isolate_meshes_lit")
        _draw_bake_channel_options(layout, settings)


def _draw_bake_channel_options(layout, settings):
    advanced_header, advanced_body = layout.panel(
        "usdstage_profile_bake_advanced",
        default_closed=True,
    )
    advanced_header.label(text="Advanced")
    if advanced_body is not None:
        advanced_body.prop(settings, "bake_base_color")
        advanced_body.prop(settings, "bake_opacity")
        advanced_body.prop(settings, "bake_step_timeout_seconds")


class USDSTAGE_PT_export_optimization(Panel):
    """Source texture optimization and bake-output settings."""

    bl_label = "Textures"
    bl_idname = "USDSTAGE_PT_export_optimization"
    bl_parent_id = "USDSTAGE_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "USD Stage"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2

    def draw_header(self, context):
        self.layout.label(text="", icon='MODIFIER')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.usd_stage_export_settings
        layout.enabled = not _is_job_running(settings)

        route = resolve_ui_export_route(settings)
        if route.pipeline == PIPELINE_DIRECT:
            # Keep Original and Original leave source textures untouched;
            # choosing anything else turns optimization on.
            layout.prop(settings, "ui_source_texture_resolution", text="Maximum Resolution")
            layout.prop(settings, "ui_source_texture_format", text="Image Format")
            if settings.ui_source_texture_resolution == 'CUSTOM':
                layout.prop(settings, "bake_resolution_custom")
            return

        layout.prop(settings, "bake_resolution", text="Bake Resolution")
        layout.prop(settings, "bake_image_format")
        if settings.bake_resolution == 'CUSTOM':
            layout.prop(settings, "bake_resolution_custom")
        layout.prop(settings, "bake_margin")
        if settings.bake_image_format == 'ORIGINAL':
            layout.label(
                text="Newly baked textures use PNG when Original is selected.",
                icon='STATUS_INFO',
            )


class USDSTAGE_PT_export_usd_root(Panel):
    """USD export settings root panel.

    All setting groups are drawn as inline collapsible sections
    (``layout.panel``) instead of separately registered sub-panels: one less
    nesting level to click through, section open/closed state is remembered
    per region, and the sections stay expandable while a background job runs
    (only their contents grey out, with a note explaining why).
    """
    bl_label = "Advanced USD"
    bl_idname = "USDSTAGE_PT_export_usd_root"
    bl_parent_id = "USDSTAGE_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "USD Stage"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3

    def draw_header(self, context):
        self.layout.label(text="", icon='SETTINGS')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.usd_stage_export_settings
        job_running = _is_job_running(settings)
        if job_running:
            layout.label(text="Locked while a background job runs.", icon='LOCKED')

        for idname, title, draw_section in _USD_EXPORT_SECTIONS:
            header, body = layout.panel(idname, default_closed=True)
            header.label(text=title)
            if body is not None:
                body.enabled = not job_running
                draw_section(body, settings)


def _draw_usd_general_section(layout, settings):
    layout.prop(settings, "root_prim_name", placeholder="Scene")
    layout.prop(settings, "export_custom_properties")
    if settings.export_custom_properties:
        layout.prop(settings, "custom_properties_namespace")
        layout.prop(settings, "author_blender_name")
    else:
        row = layout.row()
        row.enabled = False
        row.prop(settings, "author_blender_name")
    layout.prop(settings, "allow_unicode")
    layout.prop(settings, "evaluation_mode")
    layout.prop(settings, "use_instancing")


def _draw_usd_geometry_section(layout, settings):
    layout.prop(settings, "triangulate_meshes")
    if settings.triangulate_meshes:
        layout.prop(settings, "quad_method")
        layout.prop(settings, "ngon_method")
    layout.prop(settings, "export_subdivision")


def _draw_usd_rigging_section(layout, settings):
    layout.prop(settings, "export_shapekeys")
    layout.prop(settings, "export_armatures")
    layout.prop(settings, "only_deform_bones")


_USD_EXPORT_SECTIONS = (
    ("usdstage_usd_general", "General", _draw_usd_general_section),
    ("usdstage_usd_geometry", "Geometry", _draw_usd_geometry_section),
    ("usdstage_usd_rigging", "Rigging", _draw_usd_rigging_section),
)


class USDSTAGE_PT_export_diagnostics(Panel):
    """Diagnostics and support actions."""
    bl_label = "Diagnostics"
    bl_idname = "USDSTAGE_PT_export_diagnostics"
    bl_parent_id = "USDSTAGE_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "USD Stage"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 4

    def draw_header(self, context):
        self.layout.label(text="", icon='STATUS_INFO')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        settings = context.scene.usd_stage_export_settings
        job_running = _is_job_running(settings)
        toggle_row = layout.row()
        toggle_row.enabled = not job_running
        toggle_row.prop(settings, "diagnostics_enabled")

        # A failed export always writes diagnostics, so the actions are
        # offered whenever a report exists, whatever the toggle says.
        diag_path = _existing_diagnostics_path(settings)
        if diag_path:
            layout.label(text=f"Path: {diag_path}", icon='FILE')
        else:
            layout.label(text="Failures always write diagnostics.", icon='STATUS_INFO')
        actions = layout.row(align=True)
        actions.enabled = bool(diag_path)
        actions.operator(
            "usdstage.show_diagnostics",
            icon='STATUS_INFO',
            text="Show Diagnostics",
        )
        actions.operator(
            "usdstage.create_support_bundle",
            icon='FILE_FOLDER',
            text="Create Support Bundle",
        )

        layout.separator()
        layout.link(
            url="https://github.com/studiomeije/usd-stage-blender#readme",
            text="Documentation",
            icon='HELP',
        )


def _existing_diagnostics_path(settings) -> str:
    """The newest diagnostics report for this scene's exports, if one exists."""
    status = _read_background_job_status(settings)
    candidates = [
        (status or {}).get("diagnostics_path"),
        getattr(settings, "last_diagnostics_path", ""),
    ]
    filepath = str(getattr(settings, "filepath", "") or "").strip()
    if filepath:
        candidates.append(str(Path(filepath).with_suffix(".diagnostics.json")))
    for candidate in candidates:
        if candidate and Path(str(candidate)).is_file():
            return str(candidate)
    return ""


def register():
    """Register UI classes"""
    bpy.utils.register_class(USDStageExportSettings)
    bpy.utils.register_class(USDSTAGE_PT_export_panel)
    bpy.utils.register_class(USDSTAGE_PT_export_material_settings)
    bpy.utils.register_class(USDSTAGE_PT_export_optimization)
    bpy.utils.register_class(USDSTAGE_PT_export_usd_root)
    bpy.utils.register_class(USDSTAGE_PT_export_diagnostics)

    # Register property on Scene
    bpy.types.Scene.usd_stage_export_settings = bpy.props.PointerProperty(
        type=USDStageExportSettings
    )
    _remove_background_job_load_handlers()
    bpy.app.handlers.load_post.append(_clear_background_job_state_on_load)
    _clear_missing_background_job_status()


def unregister():
    """Unregister UI classes"""
    _remove_background_job_load_handlers()
    del bpy.types.Scene.usd_stage_export_settings
    bpy.utils.unregister_class(USDSTAGE_PT_export_diagnostics)
    bpy.utils.unregister_class(USDSTAGE_PT_export_usd_root)
    bpy.utils.unregister_class(USDSTAGE_PT_export_optimization)
    bpy.utils.unregister_class(USDSTAGE_PT_export_material_settings)
    bpy.utils.unregister_class(USDSTAGE_PT_export_panel)
    bpy.utils.unregister_class(USDStageExportSettings)


def _apply_persisted_settings_now(context, settings) -> None:
    """Apply persisted export settings immediately (safe outside draw)."""
    result = addon_prefs.apply_persisted_export_settings(context, settings)

    if _sync_output_path_extension(settings):
        _persist_settings(context, settings)
    return result


def _apply_persisted_settings(context) -> None:
    """Schedule applying persisted export settings once per scene."""
    scene = context.scene
    settings = scene.usd_stage_export_settings
    if settings.history_applied:
        return

    key = scene.as_pointer()
    if key in _PERSIST_APPLY_SCHEDULED:
        return

    _PERSIST_APPLY_SCHEDULED.add(key)

    def _apply():
        try:
            _apply_persisted_settings_now(bpy.context, settings)
        finally:
            _PERSIST_APPLY_SCHEDULED.discard(key)
        return None

    bpy.app.timers.register(_apply, first_interval=0.0)


def _read_background_job_status(settings):
    job_dir = getattr(settings, "background_job_dir", "")
    if not job_dir:
        return None
    status_path = Path(job_dir) / "status.json"
    if not status_path.exists():
        if int(getattr(settings, "background_job_pid", 0)) > 0:
            return {"state": "error", "message": _MISSING_BACKGROUND_STATUS_MESSAGE}
        return None
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        if int(getattr(settings, "background_job_pid", 0)) > 0:
            return {"state": "error", "message": "Background job status file could not be read. Clear stale job state."}
        return None
    if data.get("state") in ACTIVE_JOB_STATES:
        pid = safe_int(getattr(settings, "background_job_pid", 0)) or 0
        recorded_pid = status_pid(data)
        if pid <= 0 or recorded_pid != pid or not pid_is_running(pid):
            stale = dict(data)
            stale["state"] = "error"
            stale["message"] = _STALE_BACKGROUND_STATUS_MESSAGE
            return stale
    return data


def _is_job_running(settings) -> bool:
    status = _read_background_job_status(settings)
    if not status:
        return False
    return status.get("state") in ACTIVE_JOB_STATES


def _clear_background_job_state(settings) -> None:
    try:
        settings.background_job_dir = ""
    except Exception:
        pass
    try:
        settings.background_job_pid = 0
    except Exception:
        pass


def _clear_missing_background_job_status() -> None:
    for scene in getattr(bpy.data, "scenes", []):
        settings = getattr(scene, "usd_stage_export_settings", None)
        if settings is None:
            continue
        job_dir = getattr(settings, "background_job_dir", "")
        if not job_dir:
            continue
        if not (Path(job_dir) / "status.json").exists():
            _clear_background_job_state(settings)


@persistent
def _clear_background_job_state_on_load(_dummy) -> None:
    """Reset per-scene job state when a new file is opened.

    A background bake belongs to the Blender session, not to the .blend, so a
    runner launched before the load keeps going while the UI forgets its job
    directory and pid. Name it on the console before dropping the handle, so
    the user can find the process and its log.
    """
    for scene in getattr(bpy.data, "scenes", []):
        settings = getattr(scene, "usd_stage_export_settings", None)
        if settings is None:
            continue
        _warn_about_orphaned_background_job(settings)
        _clear_background_job_state(settings)


def _warn_about_orphaned_background_job(settings) -> None:
    job_dir = str(getattr(settings, "background_job_dir", "") or "")
    pid = safe_int(getattr(settings, "background_job_pid", 0)) or 0
    if not job_dir or pid <= 0 or not pid_is_running(pid):
        return
    print(
        "USDStage: a background bake job (pid "
        f"{pid}) is still running after loading a new file. The UI can no "
        "longer watch or cancel it. Its log and status are in: "
        f"{job_dir}"
    )


def _remove_background_job_load_handlers() -> None:
    for handler in list(bpy.app.handlers.load_post):
        if getattr(handler, "__name__", "") == "_clear_background_job_state_on_load":
            try:
                bpy.app.handlers.load_post.remove(handler)
            except ValueError:
                pass

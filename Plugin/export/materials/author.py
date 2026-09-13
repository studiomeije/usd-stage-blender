"""
MaterialX material authoring for USD stages.
"""

from typing import Any, Dict

from ..usd_utils import UsdShade, Sdf
from .conversions import (
    get_usd_type,
    _map_mtlx_type_to_sdf,
    _normalize_mtlx_type,
    _sdf_type_to_mtlx,
    _set_shader_input_value,
    _default_value_from_input_def,
    _coerce_value_to_input_type,
    _create_convert_output,
)
from .helpers import (
    _assign_graph_node_names,
    _collect_connected_inputs,
    _get_node_def,
    _get_nodedef_name,
    _get_input_def,
    _get_output_def,
)
from .textures import (
    _coerce_texture_spec_for_input,
    _create_texture_connection,
    _materialx_file_colorspace,
    _texture_cache_key,
    texture_source_lacks_alpha,
)
from .mapping import require_realitykit_mapping_contract


def _opaque_alpha_value(input_def):
    """1.0 in the shape the input takes, for the alpha of a file with none."""
    width = {
        'vector2': 2, 'half2': 2,
        'color3': 3, 'vector3': 3, 'half3': 3,
        'color4': 4, 'vector4': 4, 'half4': 4,
    }.get(((input_def or {}).get('type') or '').lower())
    return [1.0] * width if width else 1.0


def create_materialx_material(
    stage,
    material_path: str,
    material_name: str,
    graph: Dict[str, Any],
    manifest: Dict[str, Any],
    diagnostics=None,
):
    """Create a MaterialX material in USD stage."""
    # RealityKit evaluates only the first 2D texture transform in a material.
    # Validate the complete graph before even defining the Material prim so a
    # direct authoring caller cannot leave a partially mutated USD layer.
    require_realitykit_mapping_contract(graph, material_name)

    material_prim = stage.DefinePrim(material_path, "Material")
    _author_materialx_profile_metadata(stage, material_prim, graph)
    material = UsdShade.Material(material_prim)
    nodes = graph.get('nodes', [])
    if not nodes:
        raise ValueError("MaterialX graph has no nodes")

    connections = graph.get('connections', [])
    name_map = _assign_graph_node_names(stage, material_path, nodes)
    connected_inputs = _collect_connected_inputs(connections)

    texture_prefs: Dict[Any, str] = {}
    texture_alpha_usage: Dict[Any, bool] = {}
    for node in nodes:
        for input_value in node.get('inputs', {}).values():
            if not isinstance(input_value, dict):
                continue
            if input_value.get("type") != "texture":
                continue
            key = _texture_cache_key(input_value)
            channel = (input_value.get("channel") or "").lower()
            output_type = (input_value.get("output_type") or "").lower()
            if channel == "a":
                # A file with no alpha channel never gets a four-channel
                # reader; the input is authored as 1.0 below, Blender's
                # alpha for such a file.
                if texture_source_lacks_alpha(input_value):
                    continue
                texture_prefs[key] = "color4"
                texture_alpha_usage[key] = True
            elif output_type in {"color4", "vector4"}:
                texture_prefs[key] = "color4"

    texture_cache: Dict[Any, Dict[str, Any]] = {}
    mapping_cache: Dict[Any, Any] = {}

    node_shaders: Dict[str, Any] = {}
    node_defs: Dict[str, Any] = {}

    for node in nodes:
        node_name = node.get('name')
        node_id = node.get('node_id')
        if not node_name or not node_id:
            continue
        node_def = _get_node_def(manifest, node_id)
        shader_nodedef = _get_nodedef_name(node_id, node_def)
        if diagnostics and not node_def:
            diagnostics.add_warning(
                f"Missing nodedef for node '{node_id}' in material '{material_name}'."
            )
            diagnostics.add_error(
                f"Missing nodedef for node '{node_id}' in material '{material_name}'."
            )

        shader_path = f"{material_path}/{name_map.get(node_name, node_name)}"
        shader_prim = stage.DefinePrim(shader_path, "Shader")
        shader = UsdShade.Shader(shader_prim)

        shader.CreateIdAttr(shader_nodedef)
        if hasattr(shader, "SetSourceType"):
            shader.SetSourceType("mtlx")

        node_shaders[node_name] = shader
        node_defs[node_name] = node_def

        for input_name, input_value in node.get('inputs', {}).items():
            if input_name in connected_inputs.get(node_name, set()):
                continue
            input_def = _get_input_def(node_def, input_name)
            input_type = _map_mtlx_type_to_sdf(input_def.get('type') if input_def else None)
            if diagnostics and input_def is None:
                diagnostics.add_warning(
                    f"Missing input definition '{input_name}' for node '{node_id}' in material '{material_name}'."
                )
                diagnostics.add_error(
                    f"Missing input definition '{input_name}' for node '{node_id}' in material '{material_name}'."
                )

            if isinstance(input_value, dict) and input_value.get("type") == "file_asset":
                # A file authored directly on a filename input (triplanar
                # projection). The path becomes an Asset the texture staging
                # pass relocates like any other, and the color-space token is
                # verified by the same fail-closed policy as ND_image files.
                shader_input = shader.CreateInput(
                    input_name, Sdf.ValueTypeNames.Asset
                )
                shader_input.Set(input_value.get("path"))
                file_colorspace = _materialx_file_colorspace(
                    input_value, input_name, diagnostics
                )
                if file_colorspace:
                    shader_input.GetAttr().SetColorSpace(file_colorspace)
                continue

            if isinstance(input_value, dict):
                if input_def:
                    input_value = _coerce_texture_spec_for_input(input_value, input_def, diagnostics)
                cache_key = _texture_cache_key(input_value)
                pref_type = texture_prefs.get(cache_key)
                if pref_type:
                    input_value = dict(input_value)
                    input_value["image_type_override"] = pref_type
                if texture_alpha_usage.get(cache_key):
                    input_value = dict(input_value)
                    input_value["force_separate4"] = True
                if (
                    (input_value.get("channel") or "").lower() == "a"
                    and texture_source_lacks_alpha(input_value)
                ):
                    # Blender's Image Texture Alpha output is 1 for a file
                    # with no alpha channel. The nodedef default is not: a
                    # roughness or metallic read of that alpha would take 0.5
                    # or 0 instead.
                    if diagnostics:
                        diagnostics.add_warning(
                            f"Texture '{input_value.get('path')}' has no alpha channel; "
                            f"input '{input_name}' reads 1.0 for its alpha, as Blender does."
                        )
                    shader_input = shader.CreateInput(
                        input_name, input_type or Sdf.ValueTypeNames.Float
                    )
                    _set_shader_input_value(
                        shader_input, _opaque_alpha_value(input_def)
                    )
                    continue
                texture_output = _create_texture_connection(
                    stage,
                    material_path,
                    input_name,
                    input_value,
                    manifest,
                    material_name,
                    texture_cache,
                    diagnostics,
                    mapping_cache=mapping_cache,
                )
                if texture_output:
                    shader_input = shader.CreateInput(
                        input_name,
                        input_type or texture_output.GetTypeName()
                    )
                    source_type = _normalize_mtlx_type(
                        _sdf_type_to_mtlx(texture_output.GetTypeName())
                    )
                    target_type = _normalize_mtlx_type(
                        input_def.get('type') if input_def else None
                    )
                    if source_type and target_type and source_type != target_type:
                        texture_output = _create_convert_output(
                            manifest,
                            stage,
                            material_path,
                            input_name,
                            texture_output,
                            source_type,
                            target_type,
                            diagnostics,
                        )
                    shader_input.ConnectToSource(texture_output)
                else:
                    default_value = _default_value_from_input_def(input_def)
                    if default_value is not None:
                        shader_input = shader.CreateInput(
                            input_name,
                            input_type or get_usd_type(default_value)
                        )
                        coerced_value = _coerce_value_to_input_type(default_value, input_def)
                        _set_shader_input_value(shader_input, coerced_value)
            else:
                shader_input = shader.CreateInput(input_name, input_type or get_usd_type(input_value))
                coerced_value = _coerce_value_to_input_type(input_value, input_def)
                _set_shader_input_value(shader_input, coerced_value)

    for connection in connections:
        from_node = connection.get("from_node")
        to_node = connection.get("to_node")
        if not from_node or not to_node:
            continue
        from_shader = node_shaders.get(from_node)
        to_shader = node_shaders.get(to_node)
        if not from_shader or not to_shader:
            continue

        output_name = connection.get("from_output") or "out"
        input_name = connection.get("to_input")
        if not input_name:
            continue

        from_def = node_defs.get(from_node)
        to_def = node_defs.get(to_node)

        output_def = _get_output_def(from_def, output_name)
        output_type = _map_mtlx_type_to_sdf(output_def.get('type') if output_def else None)
        if not output_type:
            output_type = Sdf.ValueTypeNames.Token
            if diagnostics:
                diagnostics.add_warning(
                    f"Unknown output type for '{from_node}.{output_name}' in material '{material_name}'."
                )
                diagnostics.add_error(
                    f"Unknown output type for '{from_node}.{output_name}' in material '{material_name}'."
                )

        shader_output = from_shader.GetOutput(output_name)
        if not shader_output:
            shader_output = from_shader.CreateOutput(output_name, output_type)

        input_def = _get_input_def(to_def, input_name)
        input_type = _map_mtlx_type_to_sdf(input_def.get('type') if input_def else None)
        if not input_type:
            input_type = shader_output.GetTypeName()
            if diagnostics:
                diagnostics.add_warning(
                    f"Unknown input type for '{to_node}.{input_name}' in material '{material_name}'."
                )
                diagnostics.add_error(
                    f"Unknown input type for '{to_node}.{input_name}' in material '{material_name}'."
                )

        shader_input = to_shader.GetInput(input_name)
        if not shader_input:
            shader_input = to_shader.CreateInput(input_name, input_type)

        from_type = _normalize_mtlx_type(output_def.get('type') if output_def else None)
        if not from_type:
            from_type = _normalize_mtlx_type(_sdf_type_to_mtlx(shader_output.GetTypeName()))
        to_type = _normalize_mtlx_type(input_def.get('type') if input_def else None)
        if not to_type:
            to_type = _normalize_mtlx_type(_sdf_type_to_mtlx(input_type))

        if from_type and to_type and from_type != to_type:
            converted_output = _create_convert_output(
                manifest,
                stage,
                material_path,
                input_name,
                shader_output,
                from_type,
                to_type,
                diagnostics,
            )
            shader_input.ConnectToSource(converted_output)
        else:
            shader_input.ConnectToSource(shader_output)

    output_target = graph.get('output')
    if isinstance(output_target, dict):
        output_node_name = output_target.get('node')
        output_name = output_target.get('output') or "out"
    else:
        output_node_name = output_target
        output_name = "out"

    output_node = node_shaders.get(output_node_name)
    if not output_node:
        raise ValueError(f"MaterialX graph output node not found: {output_node_name}")

    output_def = _get_output_def(node_defs.get(output_node_name), output_name)
    output_type = _map_mtlx_type_to_sdf(output_def.get('type') if output_def else None)
    if not output_type:
        output_type = Sdf.ValueTypeNames.Token

    material_output = material.CreateSurfaceOutput("mtlx")
    shader_output = output_node.GetOutput(output_name)
    if not shader_output:
        shader_output = output_node.CreateOutput(output_name, output_type)
    material_output.ConnectToSource(shader_output)

    vertex_node_name = graph.get('vertex_output')
    if vertex_node_name:
        vertex_shader = node_shaders.get(vertex_node_name)
        if not vertex_shader:
            raise ValueError(f"MaterialX graph vertex node not found: {vertex_node_name}")
        vertex_output = vertex_shader.GetOutput("out") or vertex_shader.CreateOutput(
            "out", Sdf.ValueTypeNames.Token
        )
        # RealityKit reads the geometry modifier from the material's
        # ``realitykit:vertex`` output, beside ``mtlx:surface``.
        material.CreateOutput("realitykit:vertex", Sdf.ValueTypeNames.Token).ConnectToSource(
            vertex_output
        )

    return material


def _author_materialx_profile_metadata(stage, material_prim, graph: Dict[str, Any]) -> None:
    """Author the explicit MaterialX/color contract used by Blender 5.2/RCP3."""
    materialx_version = graph.get("materialx_version")
    if not materialx_version:
        return

    # MaterialXConfigAPI is understood by Blender 5.2 and RCP3, but older
    # OpenUSD Python builds may not have a registered schema class for it.
    # AddAppliedSchema still authors the correct apiSchemas list-op.
    material_prim.AddAppliedSchema("MaterialXConfigAPI")
    material_prim.AddAppliedSchema("ColorSpaceAPI")
    material_prim.CreateAttribute(
        "config:mtlx:version",
        Sdf.ValueTypeNames.String,
        custom=False,
    ).Set(str(materialx_version))
    material_prim.CreateAttribute(
        "colorSpace:name",
        Sdf.ValueTypeNames.Token,
        custom=False,
        variability=Sdf.VariabilityUniform,
    ).Set("lin_rec709_scene")

    surface_profile = graph.get("surface_profile")
    if surface_profile:
        material_prim.SetCustomDataByKey("USDStage:surfaceProfile", surface_profile)

    root_prim = stage.GetDefaultPrim()
    if not root_prim:
        prefixes = material_prim.GetPath().GetPrefixes()
        root_prim = stage.GetPrimAtPath(prefixes[0]) if prefixes else None
    if root_prim:
        root_prim.AddAppliedSchema("ColorSpaceAPI")
        root_prim.CreateAttribute(
            "colorSpace:name",
            Sdf.ValueTypeNames.Token,
            custom=False,
            variability=Sdf.VariabilityUniform,
        ).Set("lin_rec709_scene")

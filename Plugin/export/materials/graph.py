"""
MaterialX graph construction for RealityKit shaders.

Builds node graphs that reference RealityKit nodedefs.
"""

import json
from typing import Any, Dict, List, Optional

from ...manifest.materialx_nodes import select_node_def_for_node


RCP3_PBR2_NODEDEF = "ND_realitykit_pbr_surfaceshader_2_0"

#: RealityKit's hair surface, the terminal for a Principled Hair BSDF.
RCP3_HAIR_NODEDEF = "ND_realitykit_hair_surfaceshader"
#: The one MaterialX surface a translated material terminates in. Reality
#: Composer Pro builds it natively, it is implemented in Metal, and it carries
#: every Principled control this exporter maps.
_SURFACE_PROFILE = "realitykit_pbr2"

#: RealityKit's vertex-stage terminal, attached when a material displaces.
_GEOMETRY_MODIFIER_NODE = 'realitykit_geometrymodifier_vertexshader'
#: The MaterialX version every material declares. RealityKit keeps one nodedef
#: store per declared version, so this decides which nodes resolve.
_MATERIALX_VERSION = "1.39"

_COLOR_TEXTURE_INPUTS = {
    "color",
    "baseColor",
    "emissiveColor",
    "subsurfaceColor",
    "sheenColor",
    "specularColor",
    "base_color",
    "emission_color",
    "subsurface_color",
    "fuzz_color",
    "specular_color",
    "coat_color",
}


#: RealityKit PBR Surface 2's dielectric reflectance at normal incidence per
#: unit of ``specular``, while ``baseDiffuseRoughness`` is unauthored, as the
#: export always leaves it: F0 is 0.08 * saturate(specular) and
#: ``specularIOR`` is ignored. ``specularWeight`` scales the whole lobe, metals and grazing
#: angles included, so it is left at its default 1. Measured with
#: RealityRenderer on macOS 27 by matching renders: specular 0.5 without the
#: input against specular 1 and IOR 1.5 with it, specular 1 against IOR 1.7923,
#: and specular 0.5 at IOR 3 against specular 1 at IOR 2.0938.
PBR2_F0_PER_SPECULAR = 0.08

#: Cycles' ``CLOSURE_WEIGHT_CUTOFF``: a lobe weight at or below it is skipped.
CYCLES_CLOSURE_WEIGHT_CUTOFF = 1e-5


def cycles_f0_from_ior(ior: float) -> float:
    """Cycles' ``F0_from_ior`` (kernel/closure/bsdf_util.h)."""
    return ((ior - 1.0) / (ior + 1.0)) ** 2


def cycles_ior_from_f0(f0: float) -> float:
    """Cycles' ``ior_from_F0`` (kernel/closure/bsdf_util.h)."""
    root = min(max(f0, 0.0), 0.99) ** 0.5
    return (1.0 + root) / (1.0 - root)


def cycles_specular_eta(ior: float, level: float) -> float:
    """The IOR of the Principled BSDF's dielectric specular lobe.

    ``svm_node_closure_bsdf``'s 'Apply IOR adjustment': Specular IOR Level
    scales F0 by twice itself and the lobe takes the IOR of that F0. Metals
    ignore it.
    """
    ior = max(float(ior), 1e-5)
    level = max(float(level), 0.0)
    if level == 0.5:
        return ior
    eta = cycles_ior_from_f0(cycles_f0_from_ior(ior) * 2.0 * level)
    return 1.0 / eta if ior < 1.0 else eta


def diffuse_roughness_is_lambert(value: float) -> bool:
    """Cycles' ``diffuse_roughness_is_almost_zero`` after its saturate."""
    return min(max(float(value), 0.0), 1.0) < 1e-5


def pbr2_specular_inputs(ior: float, level: float) -> Dict[str, float]:
    """PBR Surface 2 ``specular`` for a constant IOR and level.

    F0 caps at 0.08 (see ``PBR2_F0_PER_SPECULAR``);
    ``pbr2_specular_cap_exceeded`` says when.
    """
    eta = cycles_specular_eta(ior, level)
    return {'specular': min(1.0, cycles_f0_from_ior(eta) / PBR2_F0_PER_SPECULAR)}


def pbr2_specular_cap_exceeded(ior: float, level: float) -> Optional[float]:
    """Cycles' F0 when it exceeds what ``specular`` can carry, else None."""
    f0 = cycles_f0_from_ior(cycles_specular_eta(ior, level))
    return f0 if f0 > PBR2_F0_PER_SPECULAR + 1e-6 else None


def texture_colorspace_role(input_name: str) -> str:
    """Return the color-space role a texture feeding ``input_name`` must use.

    Shared so every extraction path tags its textures the same way. A spec with
    no role skips ``textures._materialx_file_colorspace``'s data branch, which
    warns when a normal or roughness image left at Blender's default sRGB is
    decoded, as Cycles decodes it.
    """
    return "color" if input_name in _COLOR_TEXTURE_INPUTS else "data"


class MaterialXGraphBuilder:
    """Build MaterialX graphs for RealityKit-compatible materials."""

    def __init__(
        self,
        manifest: Dict[str, Any],
        diagnostics=None,
    ):
        """Initialize the graph builder.

        Args:
            manifest: MaterialX node manifest.
            diagnostics: Optional ExportDiagnostics instance.
        """
        self.manifest = manifest
        self.diagnostics = diagnostics
        self.node_counter = 0

    def build_pbr_material(self, material_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build a PBR MaterialX graph.

        Args:
            material_data: Material data extracted from Blender.

        Returns:
            MaterialX graph structure.
        """
        graph = {
            'nodes': [],
            'connections': [],
            'output': None,
        }

        pbr_node_id = self._require_pbr_surface_2()
        pbr_node_def = self._find_node_def(pbr_node_id)

        if not pbr_node_def:
            raise ValueError(f"PBR node definition not found: {pbr_node_id}")

        pbr_inputs = self._map_realitykit_pbr2_inputs(material_data)
        pbr_node = self._create_node(
            node_id=pbr_node_id,
            node_name='pbr_surfaceshader',
            inputs=pbr_inputs,
        )
        graph['nodes'].append(pbr_node)
        profile_graphs = self._pbr2_input_graphs(
            material_data.get('input_graphs', {}),
            material_data,
        )
        weight_name, color_name, base_name = (
            'subsurfaceWeight',
            'subsurfaceColor',
            'baseColor',
        )
        has_subsurface = weight_name in pbr_inputs or weight_name in profile_graphs
        if has_subsurface and color_name not in pbr_inputs and color_name not in profile_graphs:
            if base_name in profile_graphs:
                profile_graphs = dict(profile_graphs)
                profile_graphs[color_name] = profile_graphs[base_name]
            elif base_name in pbr_inputs:
                pbr_node['inputs'][color_name] = pbr_inputs[base_name]
        self._apply_graph_inputs(
            graph,
            pbr_node['name'],
            profile_graphs,
        )
        graph['output'] = pbr_node['name']
        graph['surface_profile'] = _SURFACE_PROFILE
        graph['materialx_version'] = _MATERIALX_VERSION
        self._apply_vertex_modifier(graph, material_data.get('vertex_offset'))

        return graph

    def build_unlit_material(self, material_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build an Unlit MaterialX graph.

        Args:
            material_data: Material data extracted from Blender.

        Returns:
            MaterialX graph structure.
        """
        graph = {
            'nodes': [],
            'connections': [],
            'output': None,
        }

        unlit_node_id = 'realitykit_unlit_surfaceshader'
        unlit_node_def = self._find_node_def(unlit_node_id)

        if not unlit_node_def:
            raise ValueError(f"Unlit node definition not found: {unlit_node_id}")

        unlit_node = self._create_node(
            node_id=unlit_node_id,
            node_name='unlit_surfaceshader',
            inputs=self._map_unlit_inputs(material_data),
        )
        graph['nodes'].append(unlit_node)
        # Filter, don't pass through. input_graphs is keyed for the PBR surface
        # (roughness, metallic, _emissionColor, ...); the unlit surface exposes
        # none of those. Authoring them anyway produced a shader prim carrying
        # inputs the nodedef does not declare - author.py records an error for
        # each but does not raise, so the rewrite never rolled back and the
        # export died later with an opaque diagnostics-gate message.
        self._apply_graph_inputs(
            graph,
            unlit_node['name'],
            self._unlit_input_graphs(
                material_data.get('input_graphs', {}), unlit_node_def
            ),
        )
        graph['output'] = unlit_node['name']
        graph['surface_profile'] = "realitykit_unlit"
        graph['materialx_version'] = _MATERIALX_VERSION
        self._apply_vertex_modifier(graph, material_data.get('vertex_offset'))

        return graph

    def build_hair_material(self, material_data: Dict[str, Any]) -> Dict[str, Any]:
        """Build a graph terminating in RealityKit's hair surface.

        Only the inputs the extractor mapped are authored. ``tangent`` is the
        constant strand direction in the tangent frame; ``normal`` is left
        unconnected so the platform uses the geometry's own, as it does for
        PBR Surface 2's ``normal``.
        """
        graph: Dict[str, Any] = {'nodes': [], 'connections': [], 'output': None}

        node_def = self._find_node_def(RCP3_HAIR_NODEDEF)
        if not node_def:
            raise ValueError(
                f"{RCP3_HAIR_NODEDEF} is not in the MaterialX manifest; "
                "reinstall the USD Stage for Blender extension"
            )

        hair_node = self._create_node(
            node_id=RCP3_HAIR_NODEDEF,
            node_name='hair_surfaceshader',
            inputs=self._map_hair_inputs(material_data),
        )
        graph['nodes'].append(hair_node)
        self._apply_graph_inputs(
            graph,
            hair_node['name'],
            self._hair_input_graphs(material_data.get('input_graphs', {}), node_def),
        )
        graph['output'] = hair_node['name']
        graph['surface_profile'] = "realitykit_hair"
        graph['materialx_version'] = _MATERIALX_VERSION
        self._apply_vertex_modifier(graph, material_data.get('vertex_offset'))

        return graph

    def _map_hair_inputs(self, material_data: Dict[str, Any]) -> Dict[str, Any]:
        """The constant hair-surface inputs the extractor resolved."""
        inputs: Dict[str, Any] = {}
        for name, value in (material_data.get('hair_inputs') or {}).items():
            if name in ('baseColor', 'secondarySpecularColor'):
                inputs[name] = self._convert_color(value)
            else:
                inputs[name] = value
        if 'alpha_threshold' in material_data:
            inputs['opacityThreshold'] = material_data['alpha_threshold']
        return inputs

    def _hair_input_graphs(
        self,
        input_graphs: Dict[str, Any],
        hair_node_def: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Keep only graph inputs the hair surface declares."""
        if not input_graphs:
            return {}

        supported = self._declared_input_names(hair_node_def)
        kept: Dict[str, Any] = {}
        omitted = []
        for name, expression in input_graphs.items():
            if name in supported:
                kept[name] = expression
            else:
                omitted.append(name)

        if omitted and self.diagnostics:
            self.diagnostics.add_warning(
                "Hair material profile omitted inputs the hair surface does not "
                "expose: " + ", ".join(sorted(omitted))
            )
        return kept

    def build_rk_material(self, node_id: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Build a MaterialX graph from a RealityKit node id and inputs."""
        graph = {
            'nodes': [],
            'connections': [],
            'output': None,
        }

        node_def = self._find_node_def(node_id)
        if not node_def:
            raise ValueError(f"RealityKit node definition not found: {node_id}")

        rk_node = self._create_node(
            node_id=node_id,
            node_name=f"rk_{node_id}",
            inputs=inputs,
        )
        graph['nodes'].append(rk_node)
        graph['output'] = rk_node['name']
        graph['surface_profile'] = "realitykit_custom"
        graph['materialx_version'] = self.manifest.get('metadata', {}).get(
            'materialx_version',
            '1.39',
        )

        return graph

    def build_rk_graph(self, graph: Dict[str, Any]) -> Dict[str, Any]:
        """Pass through a pre-built RealityKit node graph."""
        if not graph or not graph.get('nodes'):
            raise ValueError("RealityKit graph is empty")
        for node in graph.get('nodes', []):
            node_id = node.get('node_id')
            if not node_id:
                raise ValueError("RealityKit graph node missing node_id")
            if not self._find_node_def(node_id):
                raise ValueError(f"RealityKit node definition not found: {node_id}")
        graph = dict(graph)
        graph.setdefault('surface_profile', "realitykit_custom")
        graph.setdefault(
            'materialx_version',
            self.manifest.get('metadata', {}).get('materialx_version', '1.39'),
        )
        return graph

    def _apply_vertex_modifier(self, graph: Dict[str, Any], offset: Any) -> None:
        """Attach RealityKit's geometry modifier carrying a model-space vertex offset.

        The modifier is a second terminal beside the surface: the material's
        ``realitykit:vertex`` output. Its normal, bitangent, colour and UV
        inputs keep their geometry-property defaults, so only the position
        moves.
        """
        if not isinstance(offset, dict):
            return
        node_def = self._find_node_def(_GEOMETRY_MODIFIER_NODE)
        if not node_def:
            raise ValueError(
                f"{_GEOMETRY_MODIFIER_NODE} is not in the MaterialX manifest; "
                "reinstall the USD Stage for Blender extension"
            )
        node = self._create_node(
            node_id=_GEOMETRY_MODIFIER_NODE, node_name='geometry_modifier', inputs={}
        )
        graph['nodes'].append(node)
        if offset.get("kind") == "node":
            # A height or vector map is data, never colour: the role keeps the
            # data-texture guard on and authors no colour-space token.
            connection = self._inject_expression(graph, offset, 'vertex_offset', texture_role='data')
            if connection:
                graph['connections'].append(
                    {
                        "from_node": connection["node"],
                        "from_output": connection.get("output") or "out",
                        "to_node": node['name'],
                        "to_input": "modelPositionOffset",
                    }
                )
        else:
            value = self._expression_to_value(offset, texture_role='data')
            if value is not None:
                node['inputs']['modelPositionOffset'] = value
        graph['vertex_output'] = node['name']

    def _find_node_def(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Find a node definition in the manifest."""
        node_def = select_node_def_for_node(self.manifest, node_id)
        if not node_def and isinstance(node_id, str) and node_id.startswith("ND_"):
            node_def = self.manifest.get("nodes", {}).get(node_id)
        return node_def

    def _declared_input_names(self, node_def: Optional[Dict[str, Any]]) -> set:
        """Return the input names a nodedef actually declares."""
        return {
            entry.get('name')
            for entry in ((node_def or {}).get('inputs') or [])
            if entry.get('name')
        }

    def _require_pbr_surface_2(self) -> str:
        """The nodedef every PBR material terminates in, or a loud failure.

        A manifest without it means the platform contract moved; refuse rather
        than fall back to a different shading model in silence.
        """
        if not self.manifest.get("nodes", {}).get(RCP3_PBR2_NODEDEF):
            raise ValueError(
                f"{RCP3_PBR2_NODEDEF} is not in the MaterialX manifest; "
                "reinstall the USD Stage for Blender extension"
            )
        return RCP3_PBR2_NODEDEF

    def _create_node(self, node_id: str, node_name: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new node payload with a unique name."""
        self.node_counter += 1
        unique_name = f"{node_name}_{self.node_counter}"

        return {
            'name': unique_name,
            'node_id': node_id,
            'type': 'nodedef',
            'inputs': inputs,
        }

    def _apply_graph_inputs(
        self,
        graph: Dict[str, Any],
        target_node: str,
        graph_inputs: Dict[str, Any],
    ) -> None:
        """Attach expression graphs to inputs on a target node."""
        if not graph_inputs:
            return
        target = next(
            (node for node in graph.get('nodes', []) if node.get('name') == target_node),
            None,
        )
        for input_name, expr in graph_inputs.items():
            texture_role = texture_colorspace_role(input_name)
            if isinstance(expr, dict) and expr.get("kind") in {"constant", "texture"}:
                value = self._expression_to_value(expr, texture_role=texture_role)
                if target is not None and value is not None:
                    target.setdefault('inputs', {})[input_name] = value
                continue
            connection = self._inject_expression(
                graph,
                expr,
                f"{target_node}_{input_name}",
                texture_role=texture_role,
            )
            if not connection:
                continue
            graph['connections'].append(
                {
                    "from_node": connection["node"],
                    "from_output": connection.get("output") or "out",
                    "to_node": target_node,
                    "to_input": input_name,
                }
            )

    def _inject_expression(
        self,
        graph: Dict[str, Any],
        expr: Any,
        name_hint: str,
        texture_role: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Convert an expression spec into graph nodes/connections."""
        if not isinstance(expr, dict):
            return None
        kind = expr.get("kind")
        if kind == "constant" or kind == "texture":
            return None
        if kind != "node":
            return None

        node_id = expr.get("node_id")
        if not node_id:
            return None

        # One authored node per distinct expression within a graph. The
        # resolver hands over trees, so a term used in several places (the
        # cosine and the root inside a Fresnel, a reader feeding two inputs)
        # arrives once per use; without this memo each use authored its own
        # copy, and a transcribed Fresnel came out as three hundred nodes.
        memo = graph.setdefault('_expression_memo', {})
        memo_key = (json.dumps(expr, sort_keys=True, default=str), texture_role)
        cached = memo.get(memo_key)
        if cached is not None:
            return dict(cached)

        inputs: Dict[str, Any] = {}
        node = self._create_node(node_id=node_id, node_name=name_hint, inputs=inputs)
        graph['nodes'].append(node)
        node_name = node["name"]

        for input_name, input_expr in (expr.get("inputs") or {}).items():
            if isinstance(input_expr, dict) and input_expr.get("kind") == "node":
                child = self._inject_expression(
                    graph,
                    input_expr,
                    f"{name_hint}_{input_name}",
                    texture_role=texture_role,
                )
                if child:
                    graph['connections'].append(
                        {
                            "from_node": child["node"],
                            "from_output": child.get("output") or "out",
                            "to_node": node_name,
                            "to_input": input_name,
                        }
                    )
                continue

            value = self._expression_to_value(input_expr, texture_role=texture_role)
            if value is not None:
                node["inputs"][input_name] = value

        result = {"node": node_name, "output": expr.get("output") or "out"}
        memo[memo_key] = dict(result)
        return result

    def _expression_to_value(
        self,
        expr: Any,
        texture_role: Optional[str] = None,
    ) -> Optional[Any]:
        if not isinstance(expr, dict):
            return expr
        kind = expr.get("kind")
        if kind == "constant":
            return expr.get("value")
        if kind == "texture":
            return self._texture_spec_from_expr(expr, texture_role=texture_role)
        if kind == "file_asset":
            # A raw image file authored directly on a filename input (e.g.
            # triplanarprojection's filex/filey/filez); the authoring stage
            # sets the Asset path and its color-space token on the input
            # itself instead of spawning an ND_image reader.
            spec = {key: value for key, value in expr.items() if key != "kind"}
            if texture_role and not spec.get("colorspace_role"):
                spec["colorspace_role"] = texture_role
            return spec
        if kind == "node":
            return None
        return None

    def _texture_spec_from_expr(
        self,
        expr: Dict[str, Any],
        texture_role: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._create_texture_input(
            expr.get("path"),
            expr.get("output_type") or "color3",
            channel=expr.get("channel", "rgb"),
            texcoord=expr.get("texcoord") or expr.get("uv_map"),
            mapping=expr.get("mapping"),
            colorspace=expr.get("colorspace"),
            alpha_mode=expr.get("alpha_mode"),
            sampling=expr.get("sampling"),
            scale=expr.get("scale"),
            texture_role=expr.get("colorspace_role") or texture_role,
            normal_decode=expr.get("normal_decode"),
            source_channels=expr.get("source_channels"),
            source_has_alpha=expr.get("source_has_alpha"),
        )


    def _map_realitykit_pbr2_inputs(self, material_data: Dict[str, Any]) -> Dict[str, Any]:
        """Map Blender Principled BSDF inputs to RealityKit PBR Surface 2."""
        inputs: Dict[str, Any] = {}

        if 'base_color_texture' in material_data:
            inputs['baseColor'] = self._create_texture_input(
                material_data['base_color_texture'],
                'color3',
                texcoord=material_data.get('base_color_texture_texcoord'),
                mapping=material_data.get('base_color_texture_mapping'),
                colorspace=material_data.get('base_color_texture_colorspace'),
                alpha_mode=material_data.get('base_color_texture_alpha_mode'),
                sampling=material_data.get('base_color_texture_sampling'),
                texture_role='color',
            )
        elif 'base_color' in material_data:
            inputs['baseColor'] = self._convert_color(material_data['base_color'])

        if 'metallic_texture' in material_data:
            inputs['metallic'] = self._create_texture_input(
                material_data['metallic_texture'],
                'float',
                channel=material_data.get('metallic_texture_channel', ''),
                texcoord=material_data.get('metallic_texture_texcoord'),
                mapping=material_data.get('metallic_texture_mapping'),
                colorspace=material_data.get('metallic_texture_colorspace'),
                alpha_mode=material_data.get('metallic_texture_alpha_mode'),
                sampling=material_data.get('metallic_texture_sampling'),
                texture_role='data',
            )
        elif 'metallic' in material_data:
            inputs['metallic'] = material_data['metallic']

        if 'roughness_texture' in material_data:
            inputs['roughness'] = self._create_texture_input(
                material_data['roughness_texture'],
                'float',
                # Only a Separate Color or Separate XYZ node sets a channel. A
                # plain Image -> Roughness link reads none, so the colour
                # reaches the float input through Blender's linear RGB to grey.
                channel=material_data.get('roughness_texture_channel', ''),
                texcoord=material_data.get('roughness_texture_texcoord'),
                mapping=material_data.get('roughness_texture_mapping'),
                colorspace=material_data.get('roughness_texture_colorspace'),
                alpha_mode=material_data.get('roughness_texture_alpha_mode'),
                sampling=material_data.get('roughness_texture_sampling'),
                texture_role='data',
            )
        elif 'roughness' in material_data:
            inputs['roughness'] = material_data['roughness']

        if 'normal_texture' in material_data:
            inputs['normal'] = self._create_normal_input(
                material_data['normal_texture'],
                texcoord=material_data.get('normal_texture_texcoord'),
                mapping=material_data.get('normal_texture_mapping'),
                colorspace=material_data.get('normal_texture_colorspace'),
                alpha_mode=material_data.get('normal_texture_alpha_mode'),
                sampling=material_data.get('normal_texture_sampling'),
                scale=material_data.get('normal_texture_scale'),
                space=material_data.get('normal_texture_space'),
            )

        if 'emission_texture' in material_data:
            emission_strength = material_data.get('emission_strength')
            if emission_strength is not None and abs(emission_strength - 1.0) < 1e-4:
                emission_strength = None
            inputs['emissiveColor'] = self._create_texture_input(
                material_data['emission_texture'],
                'color3',
                texcoord=material_data.get('emission_texture_texcoord'),
                mapping=material_data.get('emission_texture_mapping'),
                colorspace=material_data.get('emission_texture_colorspace'),
                alpha_mode=material_data.get('emission_texture_alpha_mode'),
                sampling=material_data.get('emission_texture_sampling'),
                scale=emission_strength,
                texture_role='color',
            )
        elif 'emission_color' in material_data:
            strength = float(material_data.get('emission_strength', 1.0))
            inputs['emissiveColor'] = [
                component * strength
                for component in self._convert_color(material_data['emission_color'])
            ]

        is_transparent = material_data.get('is_transparent', False)

        if is_transparent and 'alpha_texture' in material_data:
            inputs['opacity'] = self._create_texture_input(
                material_data['alpha_texture'],
                'float',
                channel=material_data.get('alpha_texture_channel', ''),
                texcoord=material_data.get('alpha_texture_texcoord'),
                mapping=material_data.get('alpha_texture_mapping'),
                colorspace=material_data.get('alpha_texture_colorspace'),
                alpha_mode=material_data.get('alpha_texture_alpha_mode'),
                sampling=material_data.get('alpha_texture_sampling'),
                texture_role='data',
                source_channels=material_data.get('alpha_texture_source_channels'),
                source_has_alpha=material_data.get(
                    'alpha_texture_source_has_alpha'
                ),
            )
        elif is_transparent and 'alpha' in material_data:
            inputs['opacity'] = material_data['alpha']

        if 'alpha_threshold' in material_data:
            inputs['opacityThreshold'] = material_data['alpha_threshold']

        if 'ao_texture' in material_data:
            inputs['ambientOcclusion'] = self._create_texture_input(
                material_data['ao_texture'],
                'float',
                channel=material_data.get('ao_texture_channel', ''),
                texcoord=material_data.get('ao_texture_texcoord'),
                mapping=material_data.get('ao_texture_mapping'),
                colorspace=material_data.get('ao_texture_colorspace'),
                alpha_mode=material_data.get('ao_texture_alpha_mode'),
                sampling=material_data.get('ao_texture_sampling'),
                texture_role='data',
            )

        if 'clearcoat' in material_data:
            inputs['clearcoat'] = material_data['clearcoat']
            if 'clearcoat_roughness' in material_data:
                inputs['clearcoatRoughness'] = material_data['clearcoat_roughness']
            if 'clearcoat_normal_texture' in material_data:
                inputs['clearcoatNormal'] = self._create_normal_input(
                    material_data['clearcoat_normal_texture'],
                    texcoord=material_data.get('clearcoat_normal_texture_texcoord'),
                    mapping=material_data.get('clearcoat_normal_texture_mapping'),
                    colorspace=material_data.get('clearcoat_normal_texture_colorspace'),
                    alpha_mode=material_data.get('clearcoat_normal_texture_alpha_mode'),
                    sampling=material_data.get('clearcoat_normal_texture_sampling'),
                    scale=material_data.get('clearcoat_normal_texture_scale'),
                    space=material_data.get('clearcoat_normal_texture_space'),
                )

        # Diffuse Roughness is never authored. Any baseDiffuseRoughness, 0
        # included, switches RealityKit to its rough-diffuse shading, which
        # lights the diffuse from the environment about a third as brightly as
        # its Lambert shading (measured with RealityRenderer: a 0.5 grey renders
        # like a 0.17 grey), where Cycles' rough diffuse stays close to Lambert.
        # Lambert is the nearer match, and principled_notices says so.

        # Specular IOR Level and IOR, folded into the reflectance Cycles gives
        # the dielectric lobe. A linked one is built in _pbr2_input_graphs.
        graphs = material_data.get('input_graphs') or {}
        if '_specularLevel' not in graphs and 'specularIOR' not in graphs:
            inputs.update(
                pbr2_specular_inputs(
                    material_data.get('ior', 1.5),
                    material_data.get('specular', 0.5),
                )
            )

        # Sheen: any authored sheenColor, black included, switches RealityKit
        # to a sheen shading that replaces the specular lobe. Author it only
        # where Cycles builds a sheen layer. A linked weight or tint is built
        # in _pbr2_input_graphs.
        if '_sheenWeight' not in graphs and '_sheenTint' not in graphs:
            sheen_weight = max(float(material_data.get('sheen_weight', 0.0)), 0.0)
            if sheen_weight > CYCLES_CLOSURE_WEIGHT_CUTOFF:
                tint = self._convert_color(material_data.get('sheen_tint', [1.0, 1.0, 1.0]))
                inputs['sheenColor'] = [max(component, 0.0) * sheen_weight for component in tint]

        # RealityKit PBR Surface 2 / Blender 5.2 Principled additions.
        pbr2_fields = {
            'subsurface_weight': 'subsurfaceWeight',
            'subsurface_radius': 'subsurfaceRadius',
            'subsurface_radius_scale': 'subsurfaceRadiusScale',
            'subsurface_anisotropy': 'subsurfaceScatterAnisotropy',
            'clearcoat_ior': 'clearcoatIOR',
            'clearcoat_anisotropy': 'clearcoatAnisotropyLevel',
            'clearcoat_anisotropy_rotation': 'clearcoatAnisotropyAngle',
            'anisotropic': 'specularAnisotropyLevel',
            'anisotropic_rotation': 'specularAnisotropyAngle',
            'specular_tint': 'specularColor',
        }
        for source_name, target_name in pbr2_fields.items():
            if source_name in material_data:
                value = material_data[source_name]
                if target_name in {'subsurfaceRadiusScale', 'specularColor'}:
                    value = self._convert_color(value)
                inputs[target_name] = value

        if 'subsurfaceWeight' in inputs:
            if 'baseColor' in inputs and 'subsurfaceColor' not in inputs:
                inputs['subsurfaceColor'] = inputs['baseColor']
            elif 'base_color' in material_data and 'subsurfaceColor' not in inputs:
                inputs['subsurfaceColor'] = self._convert_color(material_data['base_color'])

        if material_data.get('has_premultiplied_alpha'):
            inputs['hasPremultipliedAlpha'] = True

        return inputs




    def _pbr2_input_graphs(
        self,
        graph_inputs: Dict[str, Any],
        material_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        material_data = material_data or {}
        graph_inputs = dict(graph_inputs or {})
        emission_color = graph_inputs.pop('_emissionColor', None)
        emission_strength = graph_inputs.pop('_emissionStrength', None)
        sheen_weight = graph_inputs.pop('_sheenWeight', None)
        sheen_tint = graph_inputs.pop('_sheenTint', None)
        sheen_roughness = graph_inputs.pop('_sheenRoughness', None)
        specular_level = graph_inputs.pop('_specularLevel', None)

        if emission_color is not None or emission_strength is not None:
            color_expr = emission_color or self._emission_expr_from_material_data(material_data)
            strength_expr = emission_strength or {
                'kind': 'constant',
                'value': float(material_data.get('emission_strength', 1.0)),
            }
            graph_inputs['emissiveColor'] = self._scaled_color_expr(
                color_expr,
                strength_expr,
            )

        constant_weight = max(float(material_data.get('sheen_weight', 0.0)), 0.0)
        if sheen_weight is not None or (
            sheen_tint is not None and constant_weight > CYCLES_CLOSURE_WEIGHT_CUTOFF
        ):
            weight_expr = sheen_weight or {
                'kind': 'constant',
                'value': constant_weight,
            }
            tint_expr = sheen_tint or {
                'kind': 'constant',
                'value': material_data.get('sheen_tint', [1.0, 1.0, 1.0]),
            }
            weight_color = {
                'kind': 'node',
                'node_id': self._nodedef_for_graph('combine3', 'color3'),
                'inputs': {'in1': weight_expr, 'in2': weight_expr, 'in3': weight_expr},
            }
            graph_inputs['sheenColor'] = {
                'kind': 'node',
                'node_id': self._nodedef_for_graph('multiply', 'color3'),
                'inputs': {'in1': tint_expr, 'in2': weight_color},
            }
        if sheen_roughness is not None and self.diagnostics:
            self.diagnostics.add_warning(
                "RealityKit PBR Surface 2 has no sheen roughness input; bake this control."
            )
        specular_ior = graph_inputs.pop('specularIOR', None)
        if specular_level is not None or specular_ior is not None:
            graph_inputs.update(
                self._specular_graphs(
                    specular_level or {'kind': 'constant', 'value': float(material_data.get('specular', 0.5))},
                    specular_ior or {'kind': 'constant', 'value': float(material_data.get('ior', 1.5))},
                )
            )
        return graph_inputs

    def _specular_graphs(
        self,
        level: Dict[str, Any],
        ior: Dict[str, Any],
    ) -> Dict[str, Any]:
        """``pbr2_specular_inputs`` for a linked Specular IOR Level or IOR.

        F0 = ((ior - 1) / (ior + 1))^2 * 2 * level, with Cycles' clamps of
        the IOR to 1e-5 and the level to 0. Cycles skips the rescale at level
        0.5 exactly, which changes nothing but float rounding.
        """
        def node(name, **node_inputs):
            return {
                'kind': 'node',
                'node_id': self._nodedef_for_graph(name, 'float'),
                'inputs': node_inputs,
            }

        def const(value):
            return {'kind': 'constant', 'value': float(value)}

        safe_ior = node('max', in1=ior, in2=const(1e-5))
        ratio = node(
            'divide',
            in1=node('subtract', in1=safe_ior, in2=const(1.0)),
            in2=node('add', in1=safe_ior, in2=const(1.0)),
        )
        scaled_level = node('multiply', in1=node('max', in1=level, in2=const(0.0)), in2=const(2.0))
        f0 = node('multiply', in1=node('multiply', in1=ratio, in2=ratio), in2=scaled_level)
        return {
            'specular': node(
                'clamp',
                **{'in': node('divide', in1=f0, in2=const(PBR2_F0_PER_SPECULAR)), 'low': const(0.0), 'high': const(1.0)},
            )
        }

    def _nodedef_for_graph(self, node_name: str, output_type: str) -> str:
        from ...manifest.materialx_nodes import select_nodedef_name_for_node

        return select_nodedef_name_for_node(
            self.manifest,
            node_name,
            output_type=output_type,
        ) or f"ND_{node_name}_{output_type}"

    def _scaled_color_expr(
        self,
        color_expr: Dict[str, Any],
        strength_expr: Dict[str, Any],
    ) -> Dict[str, Any]:
        if strength_expr.get('kind') == 'constant':
            strength = float(strength_expr.get('value', 1.0))
            if abs(strength - 1.0) <= 1e-6:
                return color_expr
            if color_expr.get('kind') == 'texture':
                result = dict(color_expr)
                result['scale'] = float(result.get('scale', 1.0)) * strength
                return result
        strength_color = {
            'kind': 'node',
            'node_id': self._nodedef_for_graph('combine3', 'color3'),
            'inputs': {
                'in1': strength_expr,
                'in2': strength_expr,
                'in3': strength_expr,
            },
        }
        return {
            'kind': 'node',
            'node_id': self._nodedef_for_graph('multiply', 'color3'),
            'inputs': {'in1': color_expr, 'in2': strength_color},
        }

    def _emission_expr_from_material_data(
        self,
        material_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        path = material_data.get('emission_texture')
        if path:
            result = {
                'kind': 'texture',
                'path': path,
                'output_type': 'color3',
                'channel': material_data.get('emission_texture_channel', 'rgb'),
                'colorspace_role': 'color',
            }
            for source_suffix, target_name in (
                ('texcoord', 'uv_map'),
                ('mapping', 'mapping'),
                ('colorspace', 'colorspace'),
                ('alpha_mode', 'alpha_mode'),
                ('sampling', 'sampling'),
            ):
                value = material_data.get(f'emission_texture_{source_suffix}')
                if value is not None:
                    result[target_name] = value
            return result
        return {
            'kind': 'constant',
            'value': self._convert_color(material_data.get('emission_color', [0.0, 0.0, 0.0])),
        }

    def _with_normal_decode(self, expr: Any, decode: str) -> Any:
        if not isinstance(expr, dict):
            return expr
        result = dict(expr)
        if result.get('kind') == 'texture':
            if (result.get('space') or 'tangent').strip().lower() not in {'', 'tangent'}:
                raise ValueError(
                    "Geometry normals require a tangent-space normal map; "
                    "bake object-space normals before export"
                )
            result['normal_decode'] = decode
            return result
        if result.get('kind') == 'node':
            result['inputs'] = {
                name: self._with_normal_decode(value, decode)
                for name, value in (result.get('inputs') or {}).items()
            }
        return result

    def _unlit_input_graphs(
        self,
        input_graphs: Dict[str, Any],
        unlit_node_def: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Keep only graph inputs the unlit surface actually declares.

        The supported set is read from the nodedef rather than hard-coded so it
        cannot drift from the manifest. ``baseColor`` is the one name that
        differs between the PBR and unlit surfaces.
        """
        if not input_graphs:
            return {}

        supported = self._declared_input_names(unlit_node_def)
        rename = {'baseColor': 'color'}

        kept: Dict[str, Any] = {}
        omitted = []
        for name, expression in input_graphs.items():
            target = rename.get(name, name)
            if target in supported:
                kept[target] = expression
            else:
                omitted.append(name)

        if omitted and self.diagnostics:
            self.diagnostics.add_warning(
                "Unlit material profile omitted inputs the unlit surface does "
                "not expose: " + ", ".join(sorted(omitted))
            )
        return kept

    def _map_unlit_inputs(self, material_data: Dict[str, Any]) -> Dict[str, Any]:
        """Map Blender material inputs to RealityKit Unlit inputs."""
        inputs: Dict[str, Any] = {}

        if 'base_color_texture' in material_data:
            # Only a scale the resolver folded into the Base Color link. A
            # Principled BSDF's Emission Strength scales its Emission Color
            # and never its Base Color, textured or constant.
            color_scale = material_data.get('base_color_texture_scale')
            inputs['color'] = self._create_texture_input(
                material_data['base_color_texture'],
                'color3',
                texcoord=material_data.get('base_color_texture_texcoord'),
                mapping=material_data.get('base_color_texture_mapping'),
                colorspace=material_data.get('base_color_texture_colorspace'),
                alpha_mode=material_data.get('base_color_texture_alpha_mode'),
                sampling=material_data.get('base_color_texture_sampling'),
                scale=color_scale,
                texture_role='color',
            )
        elif 'base_color' in material_data:
            inputs['color'] = self._convert_color(material_data['base_color'])

        is_transparent = material_data.get('is_transparent', False)

        if is_transparent and 'alpha_texture' in material_data:
            inputs['opacity'] = self._create_texture_input(
                material_data['alpha_texture'],
                'float',
                channel=material_data.get('alpha_texture_channel', ''),
                texcoord=material_data.get('alpha_texture_texcoord'),
                mapping=material_data.get('alpha_texture_mapping'),
                colorspace=material_data.get('alpha_texture_colorspace'),
                alpha_mode=material_data.get('alpha_texture_alpha_mode'),
                sampling=material_data.get('alpha_texture_sampling'),
                texture_role='data',
                source_channels=material_data.get('alpha_texture_source_channels'),
                source_has_alpha=material_data.get(
                    'alpha_texture_source_has_alpha'
                ),
            )
        elif is_transparent and 'alpha' in material_data:
            inputs['opacity'] = material_data['alpha']

        if 'alpha_threshold' in material_data:
            inputs['opacityThreshold'] = material_data['alpha_threshold']

        if material_data.get('has_premultiplied_alpha'):
            inputs['hasPremultipliedAlpha'] = True

        return inputs

    def _convert_color(self, color: Any) -> List[float]:
        """Convert a color value to a 3-float list."""
        if isinstance(color, (list, tuple)) and len(color) >= 3:
            return [float(color[0]), float(color[1]), float(color[2])]
        return [1.0, 1.0, 1.0]

    def _create_texture_input(
        self,
        texture_path: str,
        output_type: str,
        channel: str = 'rgb',
        texcoord: Optional[str] = None,
        mapping: Optional[Dict[str, Any]] = None,
        colorspace: Optional[str] = None,
        alpha_mode: Optional[str] = None,
        scale: Optional[float] = None,
        texture_role: Optional[str] = None,
        normal_decode: Optional[str] = None,
        sampling: Optional[Dict[str, str]] = None,
        source_channels: Optional[int] = None,
        source_has_alpha: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Create a texture reference for the USD post-process stage."""
        spec = {
            'type': 'texture',
            'path': texture_path,
            'output_type': output_type,
            'channel': channel,
        }
        if source_channels is not None:
            spec['source_channels'] = source_channels
        if source_has_alpha is not None:
            spec['source_has_alpha'] = source_has_alpha
        if texcoord:
            spec['texcoord'] = texcoord
        if mapping:
            spec['mapping'] = mapping
        if colorspace:
            spec['colorspace'] = colorspace
        if alpha_mode:
            spec['alpha_mode'] = alpha_mode
        if scale is not None:
            spec['scale'] = scale
        if texture_role:
            spec['colorspace_role'] = texture_role
        if normal_decode:
            spec['normal_decode'] = normal_decode
        if sampling:
            spec['sampling'] = dict(sampling)
        return spec

    def _create_normal_input(
        self,
        texture_path: str,
        texcoord: Optional[str] = None,
        mapping: Optional[Dict[str, Any]] = None,
        colorspace: Optional[str] = None,
        alpha_mode: Optional[str] = None,
        scale: Optional[float] = None,
        space: Optional[str] = None,
        sampling: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Create a normal map reference for the USD post-process stage."""
        spec = {
            'type': 'normal_texture',
            'path': texture_path,
            'output_type': 'vector3',
            'colorspace_role': 'data',
        }
        if texcoord:
            spec['texcoord'] = texcoord
        if mapping:
            spec['mapping'] = mapping
        if colorspace:
            spec['colorspace'] = colorspace
        if alpha_mode:
            spec['alpha_mode'] = alpha_mode
        if scale is not None:
            spec['scale'] = scale
        if space:
            spec['space'] = space
        if sampling:
            spec['sampling'] = dict(sampling)
        return spec

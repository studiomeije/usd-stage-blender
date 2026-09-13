"""
Preview builder for RealityKit PBR surface.
"""


def build_preview(group, _entry, helpers):
    """Build a Principled preview for the PBR surface node."""
    input_node = group.nodes.new("NodeGroupInput")
    output_node = group.nodes.new("NodeGroupOutput")
    principled = group.nodes.new("ShaderNodeBsdfPrincipled")

    input_node.location = (-300, 0)
    principled.location = (0, 0)
    output_node.location = (300, 0)

    mapping = (
        ("baseColor", "Base Color"),
        ("metallic", "Metallic"),
        ("roughness", "Roughness"),
        ("emissiveColor", "Emission Color"),
        ("opacity", "Alpha"),
        ("specular", "Specular IOR Level"),
        ("clearcoat", "Coat Weight"),
        ("clearcoatRoughness", "Coat Roughness"),
        ("clearcoatNormal", "Coat Normal"),
    )
    for socket_name, target_name in mapping:
        if socket_name in input_node.outputs and target_name in principled.inputs:
            group.links.new(input_node.outputs[socket_name], principled.inputs[target_name])

    helpers.connect_output(group, output_node, "out", principled.outputs.get("BSDF"))

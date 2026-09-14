"""
Preview builder for RealityKit Unlit surface.
"""


def build_preview(group, _entry, helpers):
    """Build an Emission preview for the Unlit surface node."""
    input_node = group.nodes.new("NodeGroupInput")
    output_node = group.nodes.new("NodeGroupOutput")
    emission = group.nodes.new("ShaderNodeEmission")

    input_node.location = (-300, 0)
    emission.location = (0, 0)
    output_node.location = (300, 0)

    if "color" in input_node.outputs:
        group.links.new(input_node.outputs["color"], emission.inputs["Color"])

    helpers.connect_output(group, output_node, "out", emission.outputs.get("Emission"))

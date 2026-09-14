"""
Preview builder for RealityKit texture nodes.
"""


def supports(node_id: str) -> bool:
    """Return True if the node id is texture-like."""
    if not node_id:
        return False
    if node_id.startswith("RealityKitTexture"):
        return True
    return node_id in {"image", "tiledimage", "RealityKitTextureRead"}


def build_preview(group, entry, helpers):
    """Build a checker texture preview."""
    input_node, output_node = helpers.basic_io(group)
    checker = group.nodes.new("ShaderNodeTexChecker")
    checker.location = (0, 0)

    texcoord = helpers.get_input_socket(input_node, "texcoord")
    if texcoord:
        group.links.new(texcoord, checker.inputs.get("Vector"))

    output_kind = helpers.output_kind(entry)
    if output_kind == "float":
        helpers.connect_output(group, output_node, "out", checker.outputs.get("Fac"))
    else:
        helpers.connect_output(group, output_node, "out", checker.outputs.get("Color"))

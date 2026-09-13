"""
Preview builder for RealityKit Mix.
"""


def build_preview(group, entry, helpers):
    """Build a mix preview using Blender mix nodes."""
    kind = helpers.output_kind(entry)
    input_node, output_node = helpers.basic_io(group)

    if kind == "color":
        mix_node = group.nodes.new("ShaderNodeMixRGB")
        mix_node.location = (0, 0)
        helpers.link_mix_inputs(group, input_node, mix_node, "mix", "fg", "bg")
        helpers.connect_output(group, output_node, "out", mix_node.outputs.get("Color"))
        return

    mix_node = helpers.new_mix_node(group, kind)
    if mix_node:
        mix_node.location = (0, 0)
        helpers.link_mix_inputs(group, input_node, mix_node, "mix", "fg", "bg")
        helpers.connect_output(group, output_node, "out", mix_node.outputs[0])
        return

    helpers.build_float_mix_preview(group, input_node, output_node, "mix", "bg", "fg")

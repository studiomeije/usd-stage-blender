# MaterialX Definitions for RealityKit ShaderGraph nodes

These MaterialX files are used to allow access to commonly used node definitions from within ShaderGraph in RealityKit.

You can learn more about the ShaderGraph nodes on the [ShaderGraph Developer page](https://developer.apple.com/documentation/ShaderGraph).

## Notes about provided definitions and implementations

The definitions provided here are intended to allow for authoring and interchange of assets with DCC's that support
MaterialX. In many cases, they can allow a compatible DCC to provide visualization of the look of the asset as you author it.

There are some caveats that you must be aware of before using these nodes.

1. There are several features provided by RealityKit that are not available within MaterialX.
   
    In those instances, nodes are either omitted or provide fallback behaviour.
2. RealityKit provides a bespoke lighting model that may not be representable in your DCC.
3. RealityKit may change the node definitions and implementations within ShaderGraph in the future.

    In the event of a discrepancy between the behaviour or parameters of these node definitions, you should always
    defer to the implementation within ShaderGraph as the correct implementation.


As such you should always verify what your content look like within RealityKit.
As nodes may change in the future, you should periodically check for updates to this collection of definitions.

It is also recommended that you verify your content with the lighting files provided on the
[Specifying a lighting environment in AR Quick Look Developer page](https://developer.apple.com/documentation/arkit/arkit_in_ios/specifying_a_lighting_environment_in_ar_quick_look)


Some important notes about node types to be aware of:

* **GeometryModifiers** : MaterialX does not have a related paradigm to map to, so these nodes are omitted.
* **Half Types** : MaterialX does not currently have a Half type by default.
                   The nodes are provided as they can be used within a material graph, but will not function unless your
                   version of MaterialX supports Half
* **Fallback Values** : Several nodes provide fallback behaviour rather than implementations when the implementation is
                        specific to the internal implementation of RealityKit.
                        This allows use in a material graph but all materials should be verified against RealityKit.
* **KTX Texture Use** : Some texture nodes require use of KTX textures, and are not supported outside of RealityKit.
                        The `RealityKitTextureRead`, `RealityKitTextureCube`, `RealityKitTextureCubeLOD` and `RealityKitTextureCubeGradient`
                        nodes will implement a constant value placeholder to avoid confusion.

## Integrating Node Definitions

To make these node definitions available in your install of MaterialX, please consult the MaterialX documentation
for your DCC.

In many cases, you can simply add the `realitykit` folder under your MaterialX `libraries` directory.

In some cases, you may need to run a script specific to your DCC to support them.

Some DCC's may not like the availability of the Half type nodes.
In that case, you can remove `realitykit_half_defs.mtlx` and `realitykit_half_ng_mtlx`.

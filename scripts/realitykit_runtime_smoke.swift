// RealityKit 27 runtime acceptance smoke for USD Stage for Blender exports.
//
// Compile this file with scripts/run_realitykit_runtime_smoke.sh. Each --asset
// starts a new expectation group; expectation flags that follow apply only to
// that asset. The executable deliberately uses public RealityKit APIs only.

import Darwin
import Foundation
import Metal
import RealityKit

private let schemaVersion = 3

private struct AssetExpectation {
    let path: String
    var requiresModel = false
    var requiresShaderGraph = false
    var requiresAnimation = false
    var requiredAnimationKeys: [String] = []
    var requiredAnimationNames: [String] = []
    var requiredComponentTypes: [String] = []

    var hasExpectation: Bool {
        requiresModel
            || requiresShaderGraph
            || requiresAnimation
            || !requiredAnimationKeys.isEmpty
            || !requiredAnimationNames.isEmpty
            || !requiredComponentTypes.isEmpty
    }
}

private struct Configuration {
    var assets: [AssetExpectation]
    var outputPath: String?
}

private struct ExpectationReport: Encodable {
    let model: Bool
    let shaderGraph: Bool
    let animation: Bool
    let animationKeys: [String]
    let animationNames: [String]
    let componentTypes: [String]
}

private struct MaterialReport: Encodable {
    let entityPath: String
    let slot: Int
    let type: String
    let name: String?
    let shaderGraphParameterNames: [String]?
}

private struct AnimationLibraryReport: Encodable {
    let entityPath: String
    let defaultKey: String?
    let keys: [String]
    let resources: [String]
    let unkeyedResources: [String]
}

private struct AnimationGraphReport: Encodable {
    let entityPath: String
    let activeNodeNames: [String]
    let activeClipNames: [String]
    let activeStateMachineNames: [String]
}

private struct AvailableAnimationReport: Encodable {
    let entityPath: String
    let resources: [String]
}

private struct BoundsReport: Encodable {
    let min: [Float]
    let max: [Float]
    let center: [Float]
    let extents: [Float]
    let boundingRadius: Float
    let isEmpty: Bool
}

private struct AssetReport: Encodable {
    let path: String
    let kind: String
    let expectations: ExpectationReport
    let status: String
    let loadError: String?
    let failures: [String]
    let entityCount: Int
    let modelEntityCount: Int
    let shaderGraphMaterialCount: Int
    let entityPaths: [String]
    let componentTypes: [String]
    let materials: [MaterialReport]
    let availableAnimations: [AvailableAnimationReport]
    let animationLibraries: [AnimationLibraryReport]
    let animationGraphs: [AnimationGraphReport]
    let discoveredAnimationKeys: [String]
    let discoveredAnimationNames: [String]
    let visualBounds: BoundsReport?
}

private struct RunReport: Encodable {
    let schemaVersion: Int
    let status: String
    let operatingSystem: String
    let metalDevice: String?
    let assets: [AssetReport]
}

private enum CommandLineError: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case .message(let message): message
        }
    }
}

private struct Inspection {
    var entityPaths: [String] = []
    var componentTypes: Set<String> = []
    var materials: [MaterialReport] = []
    var availableAnimations: [AvailableAnimationReport] = []
    var animationLibraries: [AnimationLibraryReport] = []
    var animationGraphs: [AnimationGraphReport] = []
    var modelEntityCount = 0
    var shaderGraphMaterialCount = 0

    var animationKeys: Set<String> {
        Set(animationLibraries.flatMap(\.keys))
    }

    var animationNames: Set<String> {
        var names = Set(availableAnimations.flatMap(\.resources))
        names.formUnion(animationLibraries.flatMap(\.resources))
        names.formUnion(animationLibraries.flatMap(\.unkeyedResources))
        names.formUnion(animationGraphs.flatMap(\.activeClipNames))
        return names
    }

    var hasAnimationEvidence: Bool {
        availableAnimations.contains { !$0.resources.isEmpty }
            || animationLibraries.contains {
                !$0.keys.isEmpty || !$0.resources.isEmpty || !$0.unkeyedResources.isEmpty
            }
            || animationGraphs.contains {
                !$0.activeClipNames.isEmpty || !$0.activeNodeNames.isEmpty
            }
    }
}

private func usage() -> String {
    """
    Usage:
      realitykit_runtime_smoke \\
        --asset PATH [EXPECTATIONS ...] \\
        [--asset PATH [EXPECTATIONS ...]] \\
        [--output REPORT.json]

    Expectations are scoped to the most recent --asset:
      --expect-model                  Require at least one recursive ModelComponent.
      --expect-shader-graph           Require at least one ShaderGraphMaterial.
      --expect-animation              Require public animation evidence.
      --expect-animation-key KEY      Require an AnimationLibraryComponent key.
      --expect-animation-name NAME    Require an AnimationResource or active clip name.
      --expect-component TYPE         Require a recursive component type. A short
                                      name such as MeshDeformerComponent matches
                                      its framework-qualified runtime type.

    Every asset must have at least one expectation. Supported input extensions are
    .usdc, .usdz, and .reality. Exit status is 0 on success, 2 for invalid usage,
    and 3 when loading or any expectation fails.

    Example:
      realitykit_runtime_smoke \\
        --asset /tmp/RedCube.usdc --expect-model --expect-shader-graph \\
        --asset /tmp/Character.reality --expect-model --expect-animation \\
        --expect-animation-key Walk \\
        --output /tmp/realitykit-runtime.json
    """
}

private func parseCommandLine(_ arguments: [String]) throws -> Configuration {
    var completedAssets: [AssetExpectation] = []
    var currentAsset: AssetExpectation?
    var outputPath: String?
    var index = 0

    func value(after option: String, at index: inout Int) throws -> String {
        let valueIndex = index + 1
        guard valueIndex < arguments.count else {
            throw CommandLineError.message("Missing value after \(option)")
        }
        index = valueIndex
        return arguments[valueIndex]
    }

    func finishCurrentAsset() throws {
        guard let asset = currentAsset else { return }
        guard asset.hasExpectation else {
            throw CommandLineError.message(
                "Asset has no explicit expectations: \(asset.path)"
            )
        }
        completedAssets.append(asset)
        currentAsset = nil
    }

    while index < arguments.count {
        let argument = arguments[index]
        switch argument {
        case "--asset":
            try finishCurrentAsset()
            let path = try value(after: argument, at: &index)
            currentAsset = AssetExpectation(path: path)
        case "--expect-model":
            guard currentAsset != nil else {
                throw CommandLineError.message("\(argument) must follow --asset")
            }
            currentAsset?.requiresModel = true
        case "--expect-shader-graph":
            guard currentAsset != nil else {
                throw CommandLineError.message("\(argument) must follow --asset")
            }
            currentAsset?.requiresShaderGraph = true
        case "--expect-animation":
            guard currentAsset != nil else {
                throw CommandLineError.message("\(argument) must follow --asset")
            }
            currentAsset?.requiresAnimation = true
        case "--expect-animation-key":
            guard currentAsset != nil else {
                throw CommandLineError.message("\(argument) must follow --asset")
            }
            let key = try value(after: argument, at: &index)
            currentAsset?.requiredAnimationKeys.append(key)
        case "--expect-animation-name":
            guard currentAsset != nil else {
                throw CommandLineError.message("\(argument) must follow --asset")
            }
            let name = try value(after: argument, at: &index)
            currentAsset?.requiredAnimationNames.append(name)
        case "--expect-component":
            guard currentAsset != nil else {
                throw CommandLineError.message("\(argument) must follow --asset")
            }
            let componentType = try value(after: argument, at: &index)
            currentAsset?.requiredComponentTypes.append(componentType)
        case "--output":
            if outputPath != nil {
                throw CommandLineError.message("--output may be provided only once")
            }
            outputPath = try value(after: argument, at: &index)
        case "--help", "-h":
            print(usage())
            Darwin.exit(0)
        default:
            throw CommandLineError.message("Unknown argument: \(argument)")
        }
        index += 1
    }

    try finishCurrentAsset()
    guard !completedAssets.isEmpty else {
        throw CommandLineError.message("At least one --asset is required")
    }
    return Configuration(assets: completedAssets, outputPath: outputPath)
}

@MainActor
private func resourceName(_ resource: AnimationResource) -> String {
    let trimmed = resource.name?.trimmingCharacters(in: .whitespacesAndNewlines)
    if let trimmed, !trimmed.isEmpty {
        return trimmed
    }
    return "<unnamed>"
}

@MainActor
private func inspect(_ entity: Entity, path: String, into result: inout Inspection) {
    let displayName = entity.name.isEmpty ? "<unnamed>" : entity.name
    let entityPath = path.isEmpty ? "/\(displayName)" : "\(path)/\(displayName)"
    result.entityPaths.append(entityPath)

    for component in entity.components {
        result.componentTypes.insert(String(reflecting: type(of: component)))
    }

    if let model = entity.components[ModelComponent.self] {
        result.modelEntityCount += 1
        for (slot, material) in model.materials.enumerated() {
            let shaderGraph = material as? ShaderGraphMaterial
            if shaderGraph != nil {
                result.shaderGraphMaterialCount += 1
            }
            result.materials.append(
                MaterialReport(
                    entityPath: entityPath,
                    slot: slot,
                    type: String(reflecting: type(of: material)),
                    name: material.name,
                    shaderGraphParameterNames: shaderGraph?.parameterNames.sorted()
                )
            )
        }
    }

    let available = entity.availableAnimations.map(resourceName)
    if !available.isEmpty {
        result.availableAnimations.append(
            AvailableAnimationReport(entityPath: entityPath, resources: available.sorted())
        )
    }

    if let library = entity.components[AnimationLibraryComponent.self] {
        let keyedResources = Array(library.animations)
        let unkeyed = (library.unkeyedResources ?? []).map(resourceName).sorted()
        result.animationLibraries.append(
            AnimationLibraryReport(
                entityPath: entityPath,
                defaultKey: library.defaultKey,
                keys: keyedResources.map(\.key).sorted(),
                resources: keyedResources.map { resourceName($0.value) }.sorted(),
                unkeyedResources: unkeyed
            )
        )
    }

    if let graph = entity.components[AnimationGraphComponent.self] {
        result.animationGraphs.append(
            AnimationGraphReport(
                entityPath: entityPath,
                activeNodeNames: graph.activeNodes.map(\.name).sorted(),
                activeClipNames: graph.activeClipNodes.map(\.name).sorted(),
                activeStateMachineNames: graph.activeStateMachineNodes.map(\.name).sorted()
            )
        )
    }

    for child in entity.children {
        inspect(child, path: entityPath, into: &result)
    }
}

private func expectationReport(_ expectation: AssetExpectation) -> ExpectationReport {
    ExpectationReport(
        model: expectation.requiresModel,
        shaderGraph: expectation.requiresShaderGraph,
        animation: expectation.requiresAnimation,
        animationKeys: expectation.requiredAnimationKeys.sorted(),
        animationNames: expectation.requiredAnimationNames.sorted(),
        componentTypes: expectation.requiredComponentTypes.sorted()
    )
}

private func kind(for url: URL) -> String {
    url.pathExtension.lowercased()
}

private func unloadedReport(
    expectation: AssetExpectation,
    kind: String,
    error: String
) -> AssetReport {
    AssetReport(
        path: expectation.path,
        kind: kind,
        expectations: expectationReport(expectation),
        status: "fail",
        loadError: error,
        failures: [error],
        entityCount: 0,
        modelEntityCount: 0,
        shaderGraphMaterialCount: 0,
        entityPaths: [],
        componentTypes: [],
        materials: [],
        availableAnimations: [],
        animationLibraries: [],
        animationGraphs: [],
        discoveredAnimationKeys: [],
        discoveredAnimationNames: [],
        visualBounds: nil
    )
}

@MainActor
private func validate(_ expectation: AssetExpectation) async -> AssetReport {
    let url = URL(fileURLWithPath: expectation.path).standardizedFileURL
    let assetKind = kind(for: url)
    let supportedKinds: Set<String> = ["usdc", "usdz", "reality"]

    guard supportedKinds.contains(assetKind) else {
        return unloadedReport(
            expectation: expectation,
            kind: assetKind,
            error: "Unsupported asset extension .\(assetKind); expected .usdc, .usdz, or .reality"
        )
    }
    guard FileManager.default.isReadableFile(atPath: url.path) else {
        return unloadedReport(
            expectation: expectation,
            kind: assetKind,
            error: "Asset is missing or unreadable: \(url.path)"
        )
    }

    let entity: Entity
    do {
        // The registry name must be unique per file. Using the basename made
        // distinct assets sharing a stem collide in RealityKit's named-entity
        // cache: with validate_exports.py's {stem}-{platform}-{target}.reality
        // outputs, loading 7 same-named files in one process yielded 1 pass
        // and 6 RERealityFileErrorDomain error 20 - indistinguishable from a
        // genuinely broken asset. The full path is unique and deterministic;
        // the name is only ever displayed, never asserted.
        entity = try await Entity(contentsOf: url, withName: url.path)
    } catch {
        return unloadedReport(
            expectation: expectation,
            kind: assetKind,
            error: "RealityKit failed to load the asset: \(error.localizedDescription)"
        )
    }

    var inspection = Inspection()
    inspect(entity, path: "", into: &inspection)
    let bounds = entity.visualBounds(relativeTo: nil)
    let boundsValues = [
        bounds.min.x, bounds.min.y, bounds.min.z,
        bounds.max.x, bounds.max.y, bounds.max.z,
        bounds.extents.x, bounds.extents.y, bounds.extents.z,
        bounds.boundingRadius,
    ]
    let boundsReport = BoundsReport(
        min: [bounds.min.x, bounds.min.y, bounds.min.z],
        max: [bounds.max.x, bounds.max.y, bounds.max.z],
        center: [bounds.center.x, bounds.center.y, bounds.center.z],
        extents: [bounds.extents.x, bounds.extents.y, bounds.extents.z],
        boundingRadius: bounds.boundingRadius,
        isEmpty: bounds.isEmpty
    )
    var failures: [String] = []

    if expectation.requiresModel && inspection.modelEntityCount == 0 {
        failures.append("Expected at least one ModelComponent in the entity hierarchy")
    }
    if expectation.requiresModel
        && (bounds.isEmpty || boundsValues.contains(where: { !$0.isFinite }))
    {
        failures.append("Expected finite, non-empty recursive visual bounds")
    }
    if expectation.requiresShaderGraph && inspection.shaderGraphMaterialCount == 0 {
        let actualTypes = Set(inspection.materials.map(\.type)).sorted()
        failures.append(
            "Expected at least one ShaderGraphMaterial; actual material types: "
                + (actualTypes.isEmpty ? "<none>" : actualTypes.joined(separator: ", "))
        )
    }
    if expectation.requiresAnimation && !inspection.hasAnimationEvidence {
        failures.append(
            "Expected animation evidence in availableAnimations, "
                + "AnimationLibraryComponent, or AnimationGraphComponent"
        )
    }
    for requiredKey in expectation.requiredAnimationKeys
        where !inspection.animationKeys.contains(requiredKey)
    {
        failures.append("Required animation-library key was not found: \(requiredKey)")
    }
    for requiredName in expectation.requiredAnimationNames
        where !inspection.animationNames.contains(requiredName)
    {
        failures.append("Required animation resource/clip name was not found: \(requiredName)")
    }
    for requiredType in expectation.requiredComponentTypes {
        let matches = inspection.componentTypes.contains { actualType in
            actualType == requiredType || actualType.hasSuffix(".\(requiredType)")
        }
        if !matches {
            failures.append(
                "Required component type was not found: \(requiredType); actual types: "
                    + (inspection.componentTypes.isEmpty
                        ? "<none>"
                        : inspection.componentTypes.sorted().joined(separator: ", "))
            )
        }
    }

    return AssetReport(
        path: url.path,
        kind: assetKind,
        expectations: expectationReport(expectation),
        status: failures.isEmpty ? "pass" : "fail",
        loadError: nil,
        failures: failures,
        entityCount: inspection.entityPaths.count,
        modelEntityCount: inspection.modelEntityCount,
        shaderGraphMaterialCount: inspection.shaderGraphMaterialCount,
        entityPaths: inspection.entityPaths,
        componentTypes: inspection.componentTypes.sorted(),
        materials: inspection.materials,
        availableAnimations: inspection.availableAnimations,
        animationLibraries: inspection.animationLibraries,
        animationGraphs: inspection.animationGraphs,
        discoveredAnimationKeys: inspection.animationKeys.sorted(),
        discoveredAnimationNames: inspection.animationNames.sorted(),
        visualBounds: boundsReport
    )
}

private func writeReport(_ report: RunReport, to outputPath: String?) throws {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    let data = try encoder.encode(report)

    if let outputPath {
        let outputURL = URL(fileURLWithPath: outputPath).standardizedFileURL
        try FileManager.default.createDirectory(
            at: outputURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try data.write(to: outputURL, options: .atomic)
        print("RealityKit runtime report: \(outputURL.path)")
    } else if let json = String(data: data, encoding: .utf8) {
        print(json)
    }
}

@main
private struct RealityKitRuntimeSmoke {
    @MainActor
    static func main() async {
        let configuration: Configuration
        do {
            configuration = try parseCommandLine(Array(CommandLine.arguments.dropFirst()))
        } catch {
            fputs("error: \(error.localizedDescription)\n\n\(usage())\n", stderr)
            Darwin.exit(2)
        }

        guard let metalDevice = MTLCreateSystemDefaultDevice() else {
            let message = "No default Metal device is available; RealityKit runtime loading cannot run"
            let assetReports = configuration.assets.map {
                unloadedReport(
                    expectation: $0,
                    kind: kind(for: URL(fileURLWithPath: $0.path)),
                    error: message
                )
            }
            let runReport = RunReport(
                schemaVersion: schemaVersion,
                status: "fail",
                operatingSystem: ProcessInfo.processInfo.operatingSystemVersionString,
                metalDevice: nil,
                assets: assetReports
            )
            do {
                try writeReport(runReport, to: configuration.outputPath)
            } catch {
                fputs("error: Could not write runtime report: \(error.localizedDescription)\n", stderr)
            }
            fputs("error: \(message)\n", stderr)
            Darwin.exit(3)
        }

        var assetReports: [AssetReport] = []
        for asset in configuration.assets {
            let report = await validate(asset)
            assetReports.append(report)

            let materialTypes = Set(report.materials.map(\.type)).sorted()
            let summary = "models=\(report.modelEntityCount) "
                + "shaderGraphs=\(report.shaderGraphMaterialCount) "
                + "animationKeys=\(report.discoveredAnimationKeys.count) "
                + "animationNames=\(report.discoveredAnimationNames.count)"
            print("[\(report.status.uppercased())] \(report.path): \(summary)")
            if !materialTypes.isEmpty {
                print("  material types: \(materialTypes.joined(separator: ", "))")
            }
            for failure in report.failures {
                fputs("  failure: \(failure)\n", stderr)
            }
        }

        let didPass = assetReports.allSatisfy { $0.status == "pass" }
        let runReport = RunReport(
            schemaVersion: schemaVersion,
            status: didPass ? "pass" : "fail",
            operatingSystem: ProcessInfo.processInfo.operatingSystemVersionString,
            metalDevice: metalDevice.name,
            assets: assetReports
        )
        do {
            try writeReport(runReport, to: configuration.outputPath)
        } catch {
            fputs("error: Could not write runtime report: \(error.localizedDescription)\n", stderr)
            Darwin.exit(3)
        }

        Darwin.exit(didPass ? 0 : 3)
    }
}

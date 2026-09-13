#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/realitykit_runtime_smoke.swift"
TEMP_ROOT="${TMPDIR:-/tmp}"
BUILD_DIR="${APPLE_VALIDATION_DIR:-${TEMP_ROOT%/}/usdstage-realitykit-runtime-smoke}"
BINARY="$BUILD_DIR/realitykit_runtime_smoke"
DEVELOPER_PATH="${DEVELOPER_DIR:-$(xcode-select -p)}"

export DEVELOPER_DIR="$DEVELOPER_PATH"

SDK_VERSION="$(xcrun --sdk macosx --show-sdk-version)"
case "$SDK_VERSION" in
    27.*) ;;
    *)
        echo "error: RealityKit runtime smoke requires the macOS 27 SDK; found $SDK_VERSION" >&2
        exit 2
        ;;
esac

HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    arm64|x86_64) ;;
    *)
        echo "error: Unsupported macOS host architecture: $HOST_ARCH" >&2
        exit 2
        ;;
esac

mkdir -p "$BUILD_DIR"
mkdir -p "$BUILD_DIR/module-cache"

echo "Building RealityKit runtime smoke with $(xcrun swiftc --version | head -n 1)"
echo "macOS SDK: $SDK_VERSION"

xcrun swiftc \
    -parse-as-library \
    -swift-version 6 \
    -strict-concurrency=complete \
    -warnings-as-errors \
    -module-cache-path "$BUILD_DIR/module-cache" \
    -sdk "$(xcrun --sdk macosx --show-sdk-path)" \
    -target "${HOST_ARCH}-apple-macosx27.0" \
    -framework Metal \
    -framework RealityKit \
    "$SOURCE" \
    -o "$BINARY"

exec "$BINARY" "$@"

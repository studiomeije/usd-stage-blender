#!/usr/bin/env bash

# Hermetic tests for scripts/check_apple27_toolchain.sh. No installed Apple
# software is used; every inspected command and bundle is a temporary fixture.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="${REPO_ROOT}/scripts/check_apple27_toolchain.sh"
FIXTURE_ROOT="$(mktemp -d)"
FIXTURE_BIN="${FIXTURE_ROOT}/bin"
FIXTURE_DEVELOPER_DIR="${FIXTURE_ROOT}/Xcode.app/Contents/Developer"
FIXTURE_SDK_ROOT="${FIXTURE_ROOT}/sdks"
FIXTURE_RCP_APP="${FIXTURE_ROOT}/RealityComposerPro.app"

cleanup() {
    rm -rf "$FIXTURE_ROOT"
}
trap cleanup EXIT

mkdir -p \
    "$FIXTURE_BIN" \
    "$FIXTURE_DEVELOPER_DIR" \
    "$FIXTURE_SDK_ROOT/macosx" \
    "$FIXTURE_SDK_ROOT/xros" \
    "$FIXTURE_SDK_ROOT/iphoneos" \
    "$FIXTURE_SDK_ROOT/appletvos" \
    "$FIXTURE_RCP_APP/Contents/MacOS"

: >"$FIXTURE_RCP_APP/Contents/Info.plist"

cat >"$FIXTURE_BIN/uname" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    -s) printf '%s\n' "${FAKE_OS_NAME:-Darwin}" ;;
    -m) printf '%s\n' "${FAKE_ARCH:-arm64}" ;;
    *) exit 2 ;;
esac
EOF

cat >"$FIXTURE_BIN/sw_vers" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-productVersion" ]] || exit 2
printf '%s\n' "${FAKE_MACOS_VERSION:-27.0}"
EOF

cat >"$FIXTURE_BIN/xcode-select" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-p" ]] || exit 2
printf '%s\n' "${FAKE_DEVELOPER_DIR:?}"
EOF

cat >"$FIXTURE_BIN/xcodebuild" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-version" ]] || exit 2
printf 'Xcode %s\nBuild version TEST\n' "${FAKE_XCODE_VERSION:-27.0}"
EOF

cat >"$FIXTURE_BIN/xcrun" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "--sdk" && "${3:-}" == "--show-sdk-version" ]]; then
    printf '%s\n' "${FAKE_SDK_VERSION:-27.0}"
elif [[ "${1:-}" == "--sdk" && "${3:-}" == "--show-sdk-path" ]]; then
    printf '%s/%s\n' "${FAKE_SDK_ROOT:?}" "$2"
elif [[ "${1:-}" == "--find" ]]; then
    printf '%s/%s\n' "${FAKE_BIN:?}" "$2"
else
    exit 2
fi
EOF

cat >"$FIXTURE_BIN/blender" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "--version" ]] || exit 2
printf 'Blender %s LTS\n' "${FAKE_BLENDER_VERSION:-5.2.0}"
EOF

cat >"$FIXTURE_BIN/realitytool" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "--help" ]] || exit 2
printf 'OVERVIEW: Reality Composer Pro Assets Compiler.\n'
EOF

cat >"$FIXTURE_BIN/usdchecker" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "--help" ]] || exit 2
printf 'Utility for checking the compliance of a given USD stage.\n'
EOF

cat >"$FIXTURE_BIN/usdcat" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "--help" ]] || exit 2
printf 'Write usd file(s) either as text or to an output file.\n'
EOF

cat >"$FIXTURE_BIN/plutil" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-extract" ]] || exit 2
case "$2" in
    CFBundleShortVersionString) printf '%s\n' "${FAKE_RCP_VERSION:-3.0}" ;;
    CFBundleExecutable) printf 'FakeRealityComposerPro\n' ;;
    *) exit 2 ;;
esac
EOF

cat >"$FIXTURE_RCP_APP/Contents/MacOS/FakeRealityComposerPro" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

chmod +x "$FIXTURE_BIN"/* "$FIXTURE_RCP_APP/Contents/MacOS/FakeRealityComposerPro"

run_preflight() {
    env \
        UNAME_BIN="$FIXTURE_BIN/uname" \
        SW_VERS_BIN="$FIXTURE_BIN/sw_vers" \
        XCODE_SELECT_BIN="$FIXTURE_BIN/xcode-select" \
        XCODEBUILD_BIN="$FIXTURE_BIN/xcodebuild" \
        XCRUN_BIN="$FIXTURE_BIN/xcrun" \
        PLUTIL_BIN="$FIXTURE_BIN/plutil" \
        REALITYTOOL_BIN="$FIXTURE_BIN/realitytool" \
        USDCHECKER_BIN="$FIXTURE_BIN/usdchecker" \
        USDCAT_BIN="$FIXTURE_BIN/usdcat" \
        DEVELOPER_DIR="$FIXTURE_DEVELOPER_DIR" \
        USDSTAGE_BLENDER="$FIXTURE_BIN/blender" \
        RCP_APP="$FIXTURE_RCP_APP" \
        BLENDER_VERSION_MODE="${BLENDER_VERSION_MODE:-exact}" \
        FAKE_DEVELOPER_DIR="$FIXTURE_DEVELOPER_DIR" \
        FAKE_SDK_ROOT="$FIXTURE_SDK_ROOT" \
        FAKE_BIN="$FIXTURE_BIN" \
        FAKE_OS_NAME="${FAKE_OS_NAME:-Darwin}" \
        FAKE_ARCH="${FAKE_ARCH:-arm64}" \
        FAKE_MACOS_VERSION="${FAKE_MACOS_VERSION:-27.0}" \
        FAKE_BLENDER_VERSION="${FAKE_BLENDER_VERSION:-5.2.0}" \
        FAKE_XCODE_VERSION="${FAKE_XCODE_VERSION:-27.0}" \
        FAKE_SDK_VERSION="${FAKE_SDK_VERSION:-27.0}" \
        FAKE_RCP_VERSION="${FAKE_RCP_VERSION:-3.0}" \
        "$CHECK_SCRIPT" "$@"
}

assert_status() {
    local expected="$1"
    local actual="$2"
    local label="$3"
    local output_file="$4"
    if [[ "$actual" -ne "$expected" ]]; then
        printf 'FAIL: %s: expected exit %s, got %s\n' "$label" "$expected" "$actual" >&2
        cat "$output_file" >&2
        exit 1
    fi
}

assert_contains() {
    local expected="$1"
    local output_file="$2"
    if ! grep -Fq "$expected" "$output_file"; then
        printf 'FAIL: missing expected output: %s\n' "$expected" >&2
        cat "$output_file" >&2
        exit 1
    fi
}

run_case() {
    local label="$1"
    local expected_status="$2"
    shift 2
    local output_file="${FIXTURE_ROOT}/${label}.out"
    local actual_status

    set +e
    "$@" >"$output_file" 2>&1
    actual_status=$?
    set -e
    assert_status "$expected_status" "$actual_status" "$label" "$output_file"
    printf 'PASS: %s\n' "$label"
}

run_case ready 0 run_preflight
assert_contains "RESULT: READY (14 checks passed)" "$FIXTURE_ROOT/ready.out"

run_case invalid-argument 2 "$CHECK_SCRIPT" --invalid
assert_contains "ERROR: Unknown argument: --invalid" "$FIXTURE_ROOT/invalid-argument.out"

run_case invalid-config 2 env BLENDER_VERSION_MODE=unsupported "$CHECK_SCRIPT"
assert_contains 'BLENDER_VERSION_MODE must be "exact" or "minimum"' "$FIXTURE_ROOT/invalid-config.out"

BLENDER_VERSION_MODE=minimum FAKE_BLENDER_VERSION=5.3.0 \
    run_case minimum-mode 0 run_preflight
assert_contains "Blender 5.3.0 satisfies >=5.2" "$FIXTURE_ROOT/minimum-mode.out"

FAKE_BLENDER_VERSION=5.3.0 run_case exact-mode-rejects-newer 1 run_preflight
assert_contains "does not match required 5.2.x" "$FIXTURE_ROOT/exact-mode-rejects-newer.out"

FAKE_OS_NAME=Linux \
FAKE_ARCH=x86_64 \
FAKE_MACOS_VERSION=26.0 \
FAKE_BLENDER_VERSION=5.1.0 \
FAKE_XCODE_VERSION=26.0 \
FAKE_SDK_VERSION=26.0 \
FAKE_RCP_VERSION=2.0 \
    run_case aggregate-failure 1 run_preflight
assert_contains "requires macOS (Darwin)" "$FIXTURE_ROOT/aggregate-failure.out"
assert_contains "requires native arm64" "$FIXTURE_ROOT/aggregate-failure.out"
assert_contains "macOS 26.0 is too old" "$FIXTURE_ROOT/aggregate-failure.out"
assert_contains "Xcode 26.0 is too old" "$FIXTURE_ROOT/aggregate-failure.out"
assert_contains "visionOS SDK 26.0 is too old" "$FIXTURE_ROOT/aggregate-failure.out"
assert_contains "Reality Composer Pro 2.0 does not match" "$FIXTURE_ROOT/aggregate-failure.out"
assert_contains "RESULT: FAILED (4 passed, 10 failed)" "$FIXTURE_ROOT/aggregate-failure.out"

printf 'All Apple 27 toolchain preflight tests passed.\n'

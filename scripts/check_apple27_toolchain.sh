#!/usr/bin/env bash

# Fail-closed preflight for Apple 27 validation on a Mac.
#
# This script only inspects the selected toolchain. It does not install software
# or change the active Xcode selection.

set -uo pipefail

SCRIPT_NAME="$(basename "$0")"
FAILURES=0
PASSES=0

usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [--help]

Validate that the current machine can run USD Stage for Blender's Apple 27 release
checks. The command exits 0 only when every required component is present and
meets the configured version requirement.

Environment:
  DEVELOPER_DIR           Xcode Developer directory. When unset, use the
                          selection reported by xcode-select.
  USDSTAGE_BLENDER    Blender executable. Default:
                          /Applications/Blender.app/Contents/MacOS/Blender
  RCP_APP                 Reality Composer Pro application bundle. Default:
                          /Applications/RealityComposerPro.app
  APPLE_MIN_VERSION       Minimum macOS, Xcode, and SDK version. Default: 27.0
  BLENDER_TARGET_VERSION  Blender target major/minor. Default: 5.2
  BLENDER_VERSION_MODE    "exact" requires the target major/minor (5.2.x).
                          "minimum" accepts the target or newer. Default: exact
  RCP_REQUIRED_MAJOR      Required Reality Composer Pro major. Default: 3

Advanced command overrides (primarily for hermetic tests):
  UNAME_BIN, SW_VERS_BIN, XCODE_SELECT_BIN, XCODEBUILD_BIN, XCRUN_BIN,
  PLUTIL_BIN, REALITYTOOL_BIN, USDCHECKER_BIN, USDCAT_BIN

Examples:
  ${SCRIPT_NAME}
  DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer ${SCRIPT_NAME}
  USDSTAGE_BLENDER=/Applications/Blender.app/Contents/MacOS/Blender ${SCRIPT_NAME}
  RCP_APP=/Applications/RealityComposerPro.app ${SCRIPT_NAME}
  BLENDER_VERSION_MODE=minimum ${SCRIPT_NAME}
EOF
}

case "${1:-}" in
    "")
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        printf 'ERROR: Unknown argument: %s\n\n' "$1" >&2
        usage >&2
        exit 2
        ;;
esac

if [[ "$#" -gt 1 ]]; then
    printf 'ERROR: This command does not accept positional arguments.\n\n' >&2
    usage >&2
    exit 2
fi

pass() {
    PASSES=$((PASSES + 1))
    printf 'PASS: %s\n' "$1"
}

fail() {
    FAILURES=$((FAILURES + 1))
    printf 'FAIL: %s\n' "$1" >&2
    if [[ -n "${2:-}" ]]; then
        printf '      %s\n' "$2" >&2
    fi
}

info() {
    printf 'INFO: %s\n' "$1"
}

resolve_command() {
    local candidate="$1"
    if [[ "$candidate" == */* ]]; then
        if [[ -x "$candidate" && ! -d "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
        return 1
    fi
    command -v "$candidate" 2>/dev/null
}

extract_version() {
    # Extract the first dotted numeric version from arbitrary tool output.
    awk 'match($0, /[0-9]+([.][0-9]+)+/) {
        print substr($0, RSTART, RLENGTH)
        exit
    }'
}

is_version() {
    [[ "$1" =~ ^[0-9]+([.][0-9]+)*$ ]]
}

version_at_least() {
    local actual="$1"
    local required="$2"
    awk -v actual="$actual" -v required="$required" 'BEGIN {
        actual_count = split(actual, a, ".")
        required_count = split(required, r, ".")
        count = actual_count > required_count ? actual_count : required_count
        for (i = 1; i <= count; i++) {
            av = (i <= actual_count ? a[i] : 0) + 0
            rv = (i <= required_count ? r[i] : 0) + 0
            if (av > rv) exit 0
            if (av < rv) exit 1
        }
        exit 0
    }'
}

version_major() {
    printf '%s\n' "${1%%.*}"
}

version_major_minor() {
    local value="$1"
    local major="${value%%.*}"
    local remainder="${value#*.}"
    local minor="${remainder%%.*}"
    printf '%s.%s\n' "$major" "$minor"
}

APPLE_MIN_VERSION="${APPLE_MIN_VERSION:-27.0}"
BLENDER_TARGET_VERSION="${BLENDER_TARGET_VERSION:-5.2}"
BLENDER_VERSION_MODE="${BLENDER_VERSION_MODE:-exact}"
RCP_REQUIRED_MAJOR="${RCP_REQUIRED_MAJOR:-3}"

if ! is_version "$APPLE_MIN_VERSION"; then
    printf 'ERROR: APPLE_MIN_VERSION must be numeric (for example, 27.0); got %s.\n' \
        "$APPLE_MIN_VERSION" >&2
    exit 2
fi
if ! is_version "$BLENDER_TARGET_VERSION" || [[ "$BLENDER_TARGET_VERSION" != *.* ]]; then
    printf 'ERROR: BLENDER_TARGET_VERSION must include major and minor numbers; got %s.\n' \
        "$BLENDER_TARGET_VERSION" >&2
    exit 2
fi
case "$BLENDER_VERSION_MODE" in
    exact|minimum)
        ;;
    *)
        printf 'ERROR: BLENDER_VERSION_MODE must be "exact" or "minimum"; got %s.\n' \
            "$BLENDER_VERSION_MODE" >&2
        exit 2
        ;;
esac
if [[ ! "$RCP_REQUIRED_MAJOR" =~ ^[0-9]+$ ]]; then
    printf 'ERROR: RCP_REQUIRED_MAJOR must be an integer; got %s.\n' \
        "$RCP_REQUIRED_MAJOR" >&2
    exit 2
fi

UNAME_BIN="${UNAME_BIN:-uname}"
SW_VERS_BIN="${SW_VERS_BIN:-sw_vers}"
XCODE_SELECT_BIN="${XCODE_SELECT_BIN:-xcode-select}"
XCODEBUILD_BIN="${XCODEBUILD_BIN:-xcodebuild}"
XCRUN_BIN="${XCRUN_BIN:-xcrun}"
PLUTIL_BIN="${PLUTIL_BIN:-plutil}"

info "USD Stage for Blender Apple 27 toolchain preflight"
info "Required Apple version: >=${APPLE_MIN_VERSION}; Blender target: ${BLENDER_TARGET_VERSION} (${BLENDER_VERSION_MODE}); Reality Composer Pro: ${RCP_REQUIRED_MAJOR}.x"

uname_path="$(resolve_command "$UNAME_BIN" || true)"
if [[ -z "$uname_path" ]]; then
    fail "uname is unavailable." "Set UNAME_BIN to an executable uname implementation."
else
    os_name="$("$uname_path" -s 2>&1)"
    os_status=$?
    if [[ "$os_status" -ne 0 ]]; then
        fail "Could not determine the operating system with '$uname_path -s'." "$os_name"
    elif [[ "$os_name" != "Darwin" ]]; then
        fail "Apple validation requires macOS (Darwin); found '$os_name'." "Run Apple validation on a Mac running macOS 27."
    else
        pass "Operating system is macOS (Darwin)."
    fi

    machine_arch="$("$uname_path" -m 2>&1)"
    arch_status=$?
    if [[ "$arch_status" -ne 0 ]]; then
        fail "Could not determine the machine architecture with '$uname_path -m'." "$machine_arch"
    elif [[ "$machine_arch" != "arm64" ]]; then
        fail "Apple validation requires native arm64; found '$machine_arch'." "Use an Apple Silicon runner and do not launch the job under Rosetta."
    else
        pass "Machine architecture is native arm64."
    fi
fi

sw_vers_path="$(resolve_command "$SW_VERS_BIN" || true)"
if [[ -z "$sw_vers_path" ]]; then
    fail "sw_vers is unavailable." "Set SW_VERS_BIN to the macOS sw_vers executable."
else
    macos_output="$("$sw_vers_path" -productVersion 2>&1)"
    macos_status=$?
    macos_version="$(printf '%s\n' "$macos_output" | extract_version)"
    if [[ "$macos_status" -ne 0 ]]; then
        fail "Could not read the macOS version." "$macos_output"
    elif [[ -z "$macos_version" ]]; then
        fail "Could not parse the macOS version from '$macos_output'." "Expected a dotted version such as 27.0."
    elif ! version_at_least "$macos_version" "$APPLE_MIN_VERSION"; then
        fail "macOS ${macos_version} is too old; ${APPLE_MIN_VERSION} or newer is required." "Update macOS before validating this release."
    else
        pass "macOS ${macos_version} satisfies >=${APPLE_MIN_VERSION}."
    fi
fi

blender_bin="${USDSTAGE_BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
blender_path="$(resolve_command "$blender_bin" || true)"
if [[ -z "$blender_path" ]]; then
    fail "Blender executable not found at '$blender_bin'." "Set USDSTAGE_BLENDER to the Blender executable inside the application bundle."
else
    blender_output="$("$blender_path" --version 2>&1)"
    blender_status=$?
    blender_version="$(printf '%s\n' "$blender_output" | extract_version)"
    if [[ "$blender_status" -ne 0 ]]; then
        fail "Blender could not start to report its version (exit ${blender_status})." "$blender_output"
    elif [[ -z "$blender_version" ]]; then
        fail "Could not parse Blender's version." "$blender_output"
    elif [[ "$BLENDER_VERSION_MODE" == "exact" ]]; then
        if [[ "$(version_major_minor "$blender_version")" != "$(version_major_minor "$BLENDER_TARGET_VERSION")" ]]; then
            fail "Blender ${blender_version} does not match required ${BLENDER_TARGET_VERSION}.x." "Install Blender ${BLENDER_TARGET_VERSION}.x or explicitly set BLENDER_VERSION_MODE=minimum for forward-compatibility validation."
        else
            pass "Blender ${blender_version} matches required ${BLENDER_TARGET_VERSION}.x."
        fi
    elif ! version_at_least "$blender_version" "$BLENDER_TARGET_VERSION"; then
        fail "Blender ${blender_version} is too old; ${BLENDER_TARGET_VERSION} or newer is required." "Install a supported Blender build."
    else
        pass "Blender ${blender_version} satisfies >=${BLENDER_TARGET_VERSION}."
    fi
fi

xcode_select_path="$(resolve_command "$XCODE_SELECT_BIN" || true)"
selected_developer_dir="${DEVELOPER_DIR:-}"
if [[ -z "$selected_developer_dir" ]]; then
    if [[ -z "$xcode_select_path" ]]; then
        fail "xcode-select is unavailable and DEVELOPER_DIR is unset." "Set DEVELOPER_DIR to Xcode 27's Contents/Developer directory."
    else
        developer_output="$("$xcode_select_path" -p 2>&1)"
        developer_status=$?
        if [[ "$developer_status" -ne 0 ]]; then
            fail "xcode-select could not report a Developer directory." "$developer_output"
        else
            selected_developer_dir="$developer_output"
        fi
    fi
fi

if [[ -z "$selected_developer_dir" ]]; then
    fail "No Xcode Developer directory is selected." "Set DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer."
elif [[ ! -d "$selected_developer_dir" ]]; then
    fail "Xcode Developer directory does not exist: '$selected_developer_dir'." "Point DEVELOPER_DIR at Xcode 27's Contents/Developer directory."
else
    export DEVELOPER_DIR="$selected_developer_dir"
    pass "Using Xcode Developer directory '$selected_developer_dir'."
fi

xcodebuild_path="$(resolve_command "$XCODEBUILD_BIN" || true)"
if [[ -z "$xcodebuild_path" ]]; then
    fail "xcodebuild is unavailable." "Install Xcode 27 and ensure xcodebuild is on PATH."
else
    xcode_output="$("$xcodebuild_path" -version 2>&1)"
    xcode_status=$?
    xcode_version="$(printf '%s\n' "$xcode_output" | extract_version)"
    if [[ "$xcode_status" -ne 0 ]]; then
        fail "xcodebuild could not report the selected Xcode version." "$xcode_output"
    elif [[ -z "$xcode_version" ]]; then
        fail "Could not parse the selected Xcode version." "$xcode_output"
    elif ! version_at_least "$xcode_version" "$APPLE_MIN_VERSION"; then
        fail "Xcode ${xcode_version} is too old; ${APPLE_MIN_VERSION} or newer is required." "Select Xcode 27 with DEVELOPER_DIR."
    else
        pass "Xcode ${xcode_version} satisfies >=${APPLE_MIN_VERSION}."
    fi
fi

xcrun_path="$(resolve_command "$XCRUN_BIN" || true)"
check_sdk() {
    local sdk="$1"
    local display_name="$2"
    local sdk_output
    local sdk_status
    local sdk_version
    local path_output
    local path_status

    if [[ -z "$xcrun_path" ]]; then
        fail "${display_name} SDK could not be checked because xcrun is unavailable." "Install/select Xcode 27."
        return
    fi

    sdk_output="$("$xcrun_path" --sdk "$sdk" --show-sdk-version 2>&1)"
    sdk_status=$?
    sdk_version="$(printf '%s\n' "$sdk_output" | extract_version)"
    path_output="$("$xcrun_path" --sdk "$sdk" --show-sdk-path 2>&1)"
    path_status=$?

    if [[ "$sdk_status" -ne 0 ]]; then
        fail "${display_name} SDK is unavailable in the selected Xcode." "$sdk_output"
    elif [[ -z "$sdk_version" ]]; then
        fail "Could not parse the ${display_name} SDK version." "$sdk_output"
    elif ! version_at_least "$sdk_version" "$APPLE_MIN_VERSION"; then
        fail "${display_name} SDK ${sdk_version} is too old; ${APPLE_MIN_VERSION} or newer is required." "Install/select Xcode 27 with the ${display_name} SDK."
    elif [[ "$path_status" -ne 0 || ! -d "$path_output" ]]; then
        fail "${display_name} SDK ${sdk_version} reported an invalid path '$path_output'." "Repair or reinstall the selected Xcode."
    else
        pass "${display_name} SDK ${sdk_version} is available at '$path_output'."
    fi
}

if [[ -z "$xcrun_path" ]]; then
    fail "xcrun is unavailable." "Install Xcode 27 and ensure xcrun is on PATH."
fi
check_sdk "macosx" "macOS"
check_sdk "xros" "visionOS"
check_sdk "iphoneos" "iPhoneOS"
check_sdk "appletvos" "AppleTVOS"

find_xcode_tool() {
    local override="$1"
    local tool_name="$2"
    local found

    if [[ -n "$override" ]]; then
        resolve_command "$override"
        return
    fi
    if [[ -z "$xcrun_path" ]]; then
        return 1
    fi
    found="$("$xcrun_path" --find "$tool_name" 2>/dev/null)" || return 1
    resolve_command "$found"
}

realitytool_path="$(find_xcode_tool "${REALITYTOOL_BIN:-}" "realitytool" || true)"
if [[ -z "$realitytool_path" ]]; then
    fail "realitytool is unavailable in the selected Xcode." "Select a complete Xcode 27 installation; command-line tools alone are insufficient."
else
    realitytool_output="$("$realitytool_path" --help 2>&1)"
    realitytool_status=$?
    if [[ "$realitytool_status" -ne 0 ]]; then
        fail "realitytool exists but is not runnable (exit ${realitytool_status})." "$realitytool_output"
    elif [[ "$realitytool_output" != *"Reality Composer Pro Assets Compiler"* ]]; then
        fail "The executable at '$realitytool_path' does not identify itself as the Reality Composer Pro Assets Compiler." "Check REALITYTOOL_BIN and the selected Xcode installation."
    else
        pass "realitytool is runnable at '$realitytool_path'."
    fi
fi

usdchecker_path="$(find_xcode_tool "${USDCHECKER_BIN:-}" "usdchecker" || true)"
if [[ -z "$usdchecker_path" ]]; then
    fail "usdchecker is unavailable." "Install/select Xcode 27 or set USDCHECKER_BIN to an executable USD checker."
else
    usdchecker_output="$("$usdchecker_path" --help 2>&1)"
    usdchecker_status=$?
    if [[ "$usdchecker_status" -ne 0 ]]; then
        fail "usdchecker exists but is not runnable (exit ${usdchecker_status})." "$usdchecker_output"
    elif [[ "$usdchecker_output" != *"Utility for checking the compliance"* ]]; then
        fail "The executable at '$usdchecker_path' does not identify itself as usdchecker." "Check USDCHECKER_BIN and the selected Xcode installation."
    else
        pass "usdchecker is runnable at '$usdchecker_path'."
    fi
fi

usdcat_path="$(find_xcode_tool "${USDCAT_BIN:-}" "usdcat" || true)"
if [[ -z "$usdcat_path" ]]; then
    fail "usdcat is unavailable." "Install/select Xcode 27 or set USDCAT_BIN to an executable USD converter."
else
    usdcat_output="$("$usdcat_path" --help 2>&1)"
    usdcat_status=$?
    if [[ "$usdcat_status" -ne 0 ]]; then
        fail "usdcat exists but is not runnable (exit ${usdcat_status})." "$usdcat_output"
    elif [[ "$usdcat_output" != *"Write usd file"* ]]; then
        fail "The executable at '$usdcat_path' does not identify itself as usdcat." "Check USDCAT_BIN and the selected Xcode installation."
    else
        pass "usdcat is runnable at '$usdcat_path'."
    fi
fi

rcp_app="${RCP_APP:-/Applications/RealityComposerPro.app}"
rcp_info_plist="${rcp_app}/Contents/Info.plist"
plutil_path="$(resolve_command "$PLUTIL_BIN" || true)"
if [[ ! -d "$rcp_app" ]]; then
    fail "Reality Composer Pro application not found at '$rcp_app'." "Install Reality Composer Pro 3 and set RCP_APP to its application bundle."
elif [[ ! -f "$rcp_info_plist" ]]; then
    fail "Reality Composer Pro has no readable Info.plist at '$rcp_info_plist'." "Reinstall Reality Composer Pro 3."
elif [[ -z "$plutil_path" ]]; then
    fail "plutil is unavailable, so Reality Composer Pro cannot be verified." "Use the macOS plutil executable or set PLUTIL_BIN."
else
    rcp_version_output="$("$plutil_path" -extract CFBundleShortVersionString raw -o - "$rcp_info_plist" 2>&1)"
    rcp_version_status=$?
    rcp_version="$(printf '%s\n' "$rcp_version_output" | extract_version)"
    rcp_executable_output="$("$plutil_path" -extract CFBundleExecutable raw -o - "$rcp_info_plist" 2>&1)"
    rcp_executable_status=$?
    rcp_executable_path="${rcp_app}/Contents/MacOS/${rcp_executable_output}"

    if [[ "$rcp_version_status" -ne 0 || -z "$rcp_version" ]]; then
        fail "Could not read Reality Composer Pro's application version." "$rcp_version_output"
    elif [[ "$(version_major "$rcp_version")" != "$RCP_REQUIRED_MAJOR" ]]; then
        fail "Reality Composer Pro ${rcp_version} does not match required major ${RCP_REQUIRED_MAJOR}." "Install Reality Composer Pro ${RCP_REQUIRED_MAJOR}.x or explicitly update RCP_REQUIRED_MAJOR for a future validated release."
    elif [[ "$rcp_executable_status" -ne 0 || ! -x "$rcp_executable_path" ]]; then
        fail "Reality Composer Pro ${rcp_version} has no runnable application executable." "Expected '$rcp_executable_path'; reinstall the application."
    else
        pass "Reality Composer Pro ${rcp_version} is installed at '$rcp_app'."
    fi
fi

printf '\n'
if [[ "$FAILURES" -ne 0 ]]; then
    printf 'RESULT: FAILED (%d passed, %d failed)\n' "$PASSES" "$FAILURES" >&2
    exit 1
fi

printf 'RESULT: READY (%d checks passed)\n' "$PASSES"
exit 0

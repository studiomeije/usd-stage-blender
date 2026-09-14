#!/usr/bin/env bash
# Apple 27 platform verification, run by hand on a Mac.
#
#   scripts/verify_apple_platform.sh [OUTPUT_DIR]
#
# Needs: macOS 27, Xcode 27 (xcrun usdchecker, realitytool, swiftc),
# Reality Composer Pro 3, Blender 5.2, and a Python with pytest, usd-core and
# numpy: USDSTAGE_PYTHON, else the repository's .venv, else python3.
#
# What it does NOT do: prove anything renders. Every check here is a syntax,
# packaging, shader-implementation or load check. Only importing the
# evaluation scenes into Reality Composer Pro and looking at them does that -
# see References/Blender/TEST_SCENES.md.

set -uo pipefail

cd "$(dirname "$0")/.."
OUT="${1:-/tmp/usdstage-apple-verify}"

: "${USDSTAGE_BLENDER:=/Applications/Blender.app/Contents/MacOS/Blender}"
export USDSTAGE_BLENDER
if [ -z "${USDSTAGE_PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then USDSTAGE_PYTHON=.venv/bin/python; else USDSTAGE_PYTHON=python3; fi
fi
PYTHON="$USDSTAGE_PYTHON"

failures=0
step() { printf '\n=== %s\n' "$1"; }
check() {
  if "$@"; then
    printf '  ok    %s\n' "$*"
  else
    printf '  FAIL  %s\n' "$*"
    failures=$((failures + 1))
  fi
}

# ---------------------------------------------------------------------------
step "Apple 27 toolchain"
# ---------------------------------------------------------------------------
if ! scripts/check_apple27_toolchain.sh; then
  echo "The toolchain does not meet the Apple 27 requirements; nothing else was run." >&2
  exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT/exports" "$OUT/compiled"
echo "workspace: $OUT"

# ---------------------------------------------------------------------------
step "Integration tests on Apple Silicon"
# ---------------------------------------------------------------------------
check "$PYTHON" -m pytest -q -p no:cacheprovider tests/integration

# ---------------------------------------------------------------------------
step "Tests that need Reality Composer Pro and Xcode"
# ---------------------------------------------------------------------------
# These skip everywhere else, so a skip here means the check did not run.
mac_tests() {
  local log="$OUT/mac-only-tests.log"
  "$PYTHON" -m pytest -q -p no:cacheprovider -rs \
    tests/unit/test_manifest_runtime_overlay.py \
    tests/unit/test_manifest_matches_editor_libraries.py \
    tests/unit/test_check_shader_implementations.py > "$log" 2>&1
  local status=$?
  cat "$log"
  [ "$status" -eq 0 ] && ! grep -qi "skipped" "$log"
}
check mac_tests

# ---------------------------------------------------------------------------
step "Export the fixtures"
# ---------------------------------------------------------------------------
export_fixture() {  # name blend [overrides...]
  local name="$1" blend="$2"; shift 2
  mkdir -p "$OUT/exports/$name"
  "$PYTHON" Plugin --blender "$USDSTAGE_BLENDER" \
    export "$blend" "$@" \
    -o "$OUT/exports/$name/$name.usdc" --format USDC --diagnostics \
    > "$OUT/exports/$name/export-usdc.json"
}

check export_fixture t22_red_cube References/Blender/t22_red_cube.blend
check export_fixture t23_cube_with_4_animations References/Blender/t23_cube_with_4_animations.blend \
  export-animation=true author-animation-library=true
check export_fixture t12_skinned_limb References/Blender/t12_skinned_limb.blend \
  export-animation=true author-animation-library=true

mkdir -p "$OUT/exports/t22_red_cube"
check "$PYTHON" Plugin --blender "$USDSTAGE_BLENDER" \
  export References/Blender/t22_red_cube.blend \
  -o "$OUT/exports/t22_red_cube/t22_red_cube.usdz" --format USDZ --diagnostics

# ---------------------------------------------------------------------------
step "The Specular Tint refusal"
# ---------------------------------------------------------------------------
# The refusal comes from the material, so the whole scene must be exported. A
# --selected-only run yields NO_EXPORTABLE_OBJECTS instead, which is not what
# this proves.
mkdir -p "$OUT/exports/t21_specular_tint_refusal"
tint_report="$OUT/exports/t21_specular_tint_refusal/expected-refusal.json"
"$PYTHON" Plugin --blender "$USDSTAGE_BLENDER" --json \
  export References/Blender/t21_specular_tint_refusal.blend \
  -o "$OUT/exports/t21_specular_tint_refusal/t21_specular_tint_refusal.usdc" --format USDC --diagnostics \
  > "$tint_report"
if [[ $? -eq 0 ]]; then
  echo "  FAIL  Specular Tint export unexpectedly succeeded"
  failures=$((failures + 1))
else
  if TINT_REPORT="$tint_report" "$PYTHON" - <<'PY'
import json, os
report = json.load(open(os.environ["TINT_REPORT"]))
error = report.get("error") or {}
assert error.get("code") == "UNSUPPORTED_MATERIAL_NODES", error.get("code")
details = error.get("details") or []
assert any("Specular Tint" in d.get("message", "") for d in details), details
assert any("color semantics" in d.get("message", "") for d in details), details
print("  ok    refused with UNSUPPORTED_MATERIAL_NODES: coloured overbright Specular Tint")
PY
  then :; else failures=$((failures + 1)); fi
  check test ! -e "$OUT/exports/t21_specular_tint_refusal/t21_specular_tint_refusal.usdc"
fi

# ---------------------------------------------------------------------------
step "usdchecker, strict and ARKit-strict"
# ---------------------------------------------------------------------------
for asset in \
  "$OUT/exports/t22_red_cube/t22_red_cube.usdc" \
  "$OUT/exports/t23_cube_with_4_animations/t23_cube_with_4_animations.usdc" \
  "$OUT/exports/t12_skinned_limb/t12_skinned_limb.usdc" \
  "$OUT/exports/t22_red_cube/t22_red_cube.usdz"
do
  [ -e "$asset" ] || continue
  check xcrun usdchecker --strict "$asset"
  check xcrun usdchecker --arkit --strict "$asset"
done

# ---------------------------------------------------------------------------
step "Every material terminates in PBR Surface 2"
# ---------------------------------------------------------------------------
if [ -e "$OUT/exports/t22_red_cube/t22_red_cube.usdc" ]; then
  profile="$OUT/t22_red_cube-material-profile.usda"
  if xcrun usdcat "$OUT/exports/t22_red_cube/t22_red_cube.usdc" --out "$profile"; then
    check grep -Fq 'realitykit_pbr2' "$profile"
    check grep -Fq 'ND_realitykit_pbr_surfaceshader_2_0"' "$profile"
  else
    echo "  FAIL  usdcat could not read t22_red_cube.usdc"
    failures=$((failures + 1))
  fi
fi

# ---------------------------------------------------------------------------
step "Every authored nodedef has a shader implementation"
# ---------------------------------------------------------------------------
check "$PYTHON" scripts/check_shader_implementations.py "$OUT/exports"

# ---------------------------------------------------------------------------
step "Compile for every Apple 27 platform"
# ---------------------------------------------------------------------------
for platform in xros xrsimulator macosx iphoneos iphonesimulator appletvos appletvsimulator; do
  for name in t22_red_cube t23_cube_with_4_animations t12_skinned_limb; do
    asset="$OUT/exports/$name/$name.usdc"
    [ -e "$asset" ] || continue
    check "$PYTHON" scripts/validate_exports.py \
      --input "$asset" \
      --output "$OUT/compiled/$platform/$name" \
      --platform "$platform" \
      --deployment-target 27.0
  done
done

# ---------------------------------------------------------------------------
step "RealityKit 27 loads the exports"
# ---------------------------------------------------------------------------
check scripts/run_realitykit_runtime_smoke.sh \
  --asset "$OUT/exports/t22_red_cube/t22_red_cube.usdc" --expect-model --expect-shader-graph \
  --asset "$OUT/exports/t22_red_cube/t22_red_cube.usdz" --expect-model --expect-shader-graph \
  --asset "$OUT/exports/t23_cube_with_4_animations/t23_cube_with_4_animations.usdc" --expect-model --expect-animation \
  --asset "$OUT/exports/t12_skinned_limb/t12_skinned_limb.usdc" --expect-model --expect-animation \
  --output "$OUT/realitykit-runtime.json"

# ---------------------------------------------------------------------------
printf '\n'
if [ "$failures" -eq 0 ]; then
  echo "All Apple 27 checks passed. Evidence in $OUT"
  echo
  echo "This proves the files parse, package, compile and load. It does not prove"
  echo "they render. Run scripts/export_test_scenes.sh, import the scenes from"
  echo "References/RealityComposerProProject/Export into Reality Composer Pro, and"
  echo "check them against References/Blender/TEST_SCENES.md before releasing."
  exit 0
fi
echo "$failures check(s) failed. Evidence in $OUT"
exit 1

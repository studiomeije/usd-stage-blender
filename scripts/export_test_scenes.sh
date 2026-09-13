#!/usr/bin/env bash
# Export every evaluation scene to the Reality Composer Pro project's Export
# folder, one directory per scene, and nothing else. Import them yourself.
#
#   scripts/export_test_scenes.sh [FORMAT]      # FORMAT defaults to USDZ
#
# Each scene's directory is emptied first, so the folder holds exactly one
# export per scene rather than accumulating formats, textures and diagnostics
# from earlier runs. The whole folder is gitignored and reproducible: delete it
# whenever you like and run this again.
set -uo pipefail

cd "$(dirname "$0")/.."
FORMAT="${1:-USDZ}"
EXT=$(echo "$FORMAT" | tr '[:upper:]' '[:lower:]')
SCENES="References/Blender"
OUT="References/RealityComposerProProject/Export"

: "${USDSTAGE_BLENDER:=/Applications/Blender.app/Contents/MacOS/Blender}"
export USDSTAGE_BLENDER

# Animation is off by default, so the scenes judged on their motion turn it on,
# with the clip library that splits their takes in the editor.
animated() {
  case "$1" in
    t11_transform_animation|t12_skinned_limb|t23_cube_with_4_animations) return 0 ;;
    *) return 1 ;;
  esac
}

failures=0
for blend in "$SCENES"/t*.blend; do
  name=$(basename "$blend" .blend)
  rm -rf "$OUT/$name"
  mkdir -p "$OUT/$name"
  overrides=()
  if animated "$name"; then
    overrides=(export_animation=true author_animation_library=true)
  fi
  if python3 Plugin export "$blend" ${overrides[@]+"${overrides[@]}"} -o "$OUT/$name/$name.$EXT" --format "$FORMAT" >/dev/null 2>&1; then
    size=$(wc -c < "$OUT/$name/$name.$EXT" | tr -d ' ')
    printf "  %-30s ok    %6s bytes\n" "$name" "$size"
  else
    # A refusal is the expected result for some scenes; see TEST_SCENES.md.
    printf "  %-30s refused\n" "$name"
    failures=$((failures + 1))
  fi
done

echo
echo "$failures scene(s) refused or failed — check TEST_SCENES.md for which are meant to."

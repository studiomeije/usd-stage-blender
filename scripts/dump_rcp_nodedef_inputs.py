#!/usr/bin/env python3
"""Regenerate Plugin/manifest/rcp_nodedef_input_gaps.json from a real RCP install.

RealityKit keeps one MaterialX nodedef store per declared version, and the
stores disagree about which inputs a nodedef has. Authoring an input the bound
nodedef does not declare makes RealityKit's shader compiler discard the whole
material's graph and substitute default PBR - silently, with `realitytool`
exiting 0 and `usdchecker --arkit --strict` passing.

This script asks the resolver Reality Composer Pro itself uses
(`SGNodeDefStore` in ShaderGraph.framework) for the real input list of every
nodedef our manifest knows, then records the difference.

Run it on a Mac with Reality Composer Pro installed, after an OS or RCP update:

    python3 scripts/dump_rcp_nodedef_inputs.py

Requires the Command Line Tools (`clang`); it compiles a small helper into a
temporary directory and deletes it afterwards.
"""

from __future__ import annotations

import argparse
import collections
import json
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "Plugin" / "manifest" / "rk_nodes_manifest.json"
OUTPUT = REPO_ROOT / "Plugin" / "manifest" / "rcp_nodedef_input_gaps.json"
RCP_APP = Path("/Applications/RealityComposerPro.app")
SHADERGRAPH = "/System/Library/SubFrameworks/ShaderGraph.framework/ShaderGraph"

#: Versions RealityKit can be asked for. Every export declares 1.39; 1.38 is
#: measured too so the gap table records what the older store lacks.
VERSIONS = ("1.38", "1.39")

PROBE_SOURCE = r"""
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <dlfcn.h>

// Ask ShaderGraph for each nodedef and read back the inputs it really declares.
// Emits {"ND_name": ["input", ...]} or {"ND_name": "__UNRESOLVED__"} as JSON.
int main(int argc, char **argv) { @autoreleasepool {
  if (argc < 3) { fprintf(stderr, "usage: probe <manifest.json> <version>\n"); return 2; }
  if (!dlopen("SHADERGRAPH_PATH", RTLD_NOW)) { fprintf(stderr, "cannot load ShaderGraph\n"); return 1; }
  Class Store = objc_getClass("SGNodeDefStore");
  Class Node = objc_getClass("SGNode");
  if (!Store || !Node) { fprintf(stderr, "SGNodeDefStore/SGNode missing\n"); return 1; }

  NSData *data = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:argv[1]]];
  NSDictionary *manifest = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
  NSString *version = [NSString stringWithUTF8String:argv[2]];

  NSError *error = nil;
  id store = ((id(*)(id, SEL, id, NSError **))objc_msgSend)(
      Store, sel_registerName("storeWithMaterialXVersion:error:"), version, &error);
  if (!store) { fprintf(stderr, "no store for %s\n", argv[2]); return 1; }

  NSMutableDictionary *result = [NSMutableDictionary new];
  for (NSString *nodedef in manifest) {
    NSError *nodeError = nil;
    id node = ((id(*)(id, SEL, id, id, id, NSError **))objc_msgSend)(
        Node, sel_registerName("nodeWithNodeDefName:name:nodeDefStore:error:"),
        nodedef, @"probe", store, &nodeError);
    if (!node) { result[nodedef] = @"__UNRESOLVED__"; continue; }
    NSArray *inputs = ((id(*)(id, SEL))objc_msgSend)(node, sel_registerName("inputs"));
    NSMutableArray *names = [NSMutableArray new];
    for (id port in inputs) {
      // -description is SGInput("name", type, ...); take the first quoted run.
      NSString *text = [port description];
      NSRange open = [text rangeOfString:@"\""];
      if (open.location == NSNotFound) continue;
      NSString *rest = [text substringFromIndex:open.location + 1];
      NSRange close = [rest rangeOfString:@"\""];
      if (close.location != NSNotFound) [names addObject:[rest substringToIndex:close.location]];
    }
    result[nodedef] = names;
  }
  NSData *out = [NSJSONSerialization dataWithJSONObject:result options:0 error:nil];
  fwrite(out.bytes, 1, out.length, stdout);
} return 0; }
"""


def rcp_build() -> str:
    """The Reality Composer Pro build this table was measured against."""
    try:
        with (RCP_APP / "Contents" / "Info.plist").open("rb") as handle:
            return str(plistlib.load(handle).get("CFBundleVersion") or "unknown")
    except OSError:
        return "unknown"


def manifest_inputs() -> dict[str, list[str]]:
    nodes = json.loads(MANIFEST.read_text(encoding="utf-8"))["nodes"]
    return {
        name: [i["name"] for i in node.get("inputs", []) if i.get("name")]
        for name, node in nodes.items()
        if node.get("inputs")
    }


def probe(work: Path, declared: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    source = work / "probe.m"
    source.write_text(PROBE_SOURCE.replace("SHADERGRAPH_PATH", SHADERGRAPH), encoding="utf-8")
    binary = work / "probe"
    subprocess.run(
        ["clang", "-fobjc-arc", "-framework", "Foundation", "-o", str(binary), str(source)],
        check=True,
    )
    names = work / "names.json"
    names.write_text(json.dumps(declared), encoding="utf-8")

    gaps: dict[str, dict[str, list[str]]] = {}
    for version in VERSIONS:
        finished = subprocess.run(
            [str(binary), str(names), version], check=True, capture_output=True, text=True
        )
        shipped = json.loads(finished.stdout)
        found = collections.OrderedDict()
        missing = []
        for nodedef in sorted(declared):
            real = shipped.get(nodedef)
            if not isinstance(real, list):
                missing.append(nodedef)
                continue
            absent = [name for name in declared[nodedef] if name not in real]
            if absent:
                found[nodedef] = absent
        gaps[version] = {"inputs": found, "unresolved": missing}
        print(
            f"  MaterialX {version}: {len(found)} nodedefs declare inputs "
            f"RealityKit lacks, {len(missing)} do not resolve at all"
        )
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the checked-in table is stale, without rewriting it.",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("This needs macOS with Reality Composer Pro installed.", file=sys.stderr)
        return 2
    if not RCP_APP.exists():
        print(f"Reality Composer Pro not found at {RCP_APP}.", file=sys.stderr)
        return 2

    declared = manifest_inputs()
    print(f"Probing {len(declared)} manifest nodedefs against ShaderGraph...")
    with tempfile.TemporaryDirectory() as tmp:
        gaps = probe(Path(tmp), declared)

    document = collections.OrderedDict()
    document["_doc"] = (
        "Where this repo's MaterialX manifest disagrees with RealityKit's own "
        "nodedef store, per declared MaterialX version. `inputs` lists inputs we "
        "declare that the shipped nodedef does not; `unresolved` lists nodedefs "
        "that do not exist at that version at all. Authoring either makes "
        "RealityKit's shader compiler discard the whole material's graph and "
        "substitute default PBR, with no diagnostic from realitytool or "
        "usdchecker. The manifest is version-agnostic and cannot express this "
        "split, which is why the table exists. Regenerate with "
        "scripts/dump_rcp_nodedef_inputs.py on a machine with Reality Composer Pro."
    )
    document["_source"] = (
        "ShaderGraph.framework SGNodeDefStore (the resolver RCP itself uses)"
    )
    document["_rcp_build"] = rcp_build()
    document["by_version"] = collections.OrderedDict(
        (
            version,
            collections.OrderedDict(
                (
                    ("inputs", collections.OrderedDict(sorted(entries["inputs"].items()))),
                    ("unresolved", sorted(entries["unresolved"])),
                )
            ),
        )
        for version, entries in sorted(gaps.items())
    )
    rendered = json.dumps(document, indent=2) + "\n"

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current == rendered:
            print(f"{OUTPUT.relative_to(REPO_ROOT)} is current.")
            return 0
        print(
            f"{OUTPUT.relative_to(REPO_ROOT)} is stale for this RCP build; "
            "rerun without --check.",
            file=sys.stderr,
        )
        return 1

    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)} (RCP {document['_rcp_build']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

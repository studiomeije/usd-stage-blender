---
name: Bug report
about: Report a problem with USD Stage for Blender export, materials, animation, or baking
title: "[Bug] "
labels: bug
assignees: ""
---

## Summary
<!-- What happened? One sentence. -->

## Environment
- Blender version:
- USD Stage for Blender version:
- OS:
- Export format: `.usda` / `.usdc` / `.usdz`
- Profile: RealityKit PBR, Translate Materials / RealityKit PBR, Bake Materials / RealityKit Unlit, Material Color Only / RealityKit Unlit, Lighting & Shadows
- Route: sidebar / `usdstage` command line

## Steps to reproduce
1.
2.
3.

## Expected result
<!-- What you expected to happen. -->

## Actual result
<!-- What actually happened (include exact error messages). -->

## Attachments
- A support bundle: in the sidebar, **USD Stage ▸ Diagnostics ▸ Create Support Bundle**, or `usdstage support-bundle`. It collects the diagnostics, logs and environment details, with your home folder redacted.
- The minimal `.blend` file that reproduces the problem.
- The exported `.usda`, `.usdc` or `.usdz`.
- Screenshots of the Blender shader graph and of the result in Reality Composer Pro.

A failed export always writes `<export>.diagnostics.json` next to the output; a successful one keeps it only with **Diagnostics ▸ Keep Success Diagnostics** on.

## Notes
<!-- Anything else that might help: which material or mesh, which node types, whether the file was imported, etc. -->

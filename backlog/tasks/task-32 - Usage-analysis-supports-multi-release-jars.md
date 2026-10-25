---
id: TASK-32
title: Usage analysis supports multi-release jars
status: Done
assignee: []
created_date: '2026-10-25 12:37'
updated_date: '2026-10-25 14:12'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
jackson-databind and many modern jars are multi-release (META-INF/versions/N overrides); the helper currently refuses them, which blocked the flagship demo. Analyze baseline classes plus versioned overrides with honest naming: versioned classes reported with their version marker, never as misleadingly prefixed baseline names. Java helper change, rebuilt vendored jar with updated hashes, usage pipeline and tests.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A multi-release dependency jar produces usage references for baseline and versioned classes with explicit version labeling.
- [x] #2 Vendored helper rebuilt offline, SHA256SUMS and BUILD-INFO updated, verification passes.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation: UsageHelper now analyzes multi-release jars through derived views (baseline jar without META-INF/versions plus one stripped jar per declared version, written as sibling files of the staged input, best-effort deleted after analysis); class names stay real, versioned variants enumerated per version in the input report (multi_release, multi_release_versions, versioned_classes); manifest-only MR declarations analyze normally; directory inputs, malformed version directories and over-bound derived views are still refused with the unsupported issue. Python validation extended (_MR_INPUT_KEYS with strict version/class checks); pipeline fixture mr.jar fixed to actually include its versioned entry (was manifest-only by construction). Vendored helper rebuilt offline: new helper.jar SHA256SUMS BUILD-INFO installed. Tests: helper 16 green, usage-related modules 125 green. Remaining: docs sweep done (no stale claims found), simplify+review gates for both TASK-31 and TASK-32 together, full suite.

Review APPROVED with one LOW applied post-review: versioned_classes subset check added to usage.py validation (versioned names must be within the input class union); reviewer also fixed the stale usage_pipeline limitation text in-flight. Vendored artifacts verified (helper.jar matches SHA256SUMS; BUILD-INFO source hash matches UsageHelper.java).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Multi-release jars analyzed through derived views: baseline jar plus one stripped jar per declared version (sibling files, best-effort cleanup, 2x size bound, numeric version >= 6), real class names preserved, versioned variants enumerated per version; manifest-only declarations analyze normally; directories, malformed versions and over-bound views refused. Python validation extended with versioned-classes subset check (LOW applied). Vendored helper rebuilt offline and verified. Review APPROVED; usage-related modules and full suite green.
<!-- SECTION:FINAL_SUMMARY:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

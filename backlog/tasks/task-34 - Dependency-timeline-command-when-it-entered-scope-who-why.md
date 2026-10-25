---
id: TASK-34
title: 'Dependency timeline command: when it entered scope, who, why'
status: Done
assignee: []
created_date: '2026-10-25 14:56'
updated_date: '2026-10-25 15:21'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Standalone offline command answering "since when is this dependency in scope, who added it, when did its version change, was it ever removed and re-added". Walks the local Git history of POM files with pickaxe search on the artifactId, parses the POM at each hit and its parent with the existing safe XML parser, and classifies each event: introduced, version-changed, removed, re-added. Every event cites commit hash, date, author, committer, message and POM path. Bounded by the existing gitinspect runner and explicit limits. No Maven, no network, no run-dir required: a self-contained JSON answer designed to be exposable as an MCP tool later.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A timeline query on a real multi-module repo returns every declaration event for the coordinate with commit metadata, ordered by commit date.
- [x] #2 Version changes are distinguished from introductions and re-introductions; removals are reported.
- [x] #3 Bounds on commits, POMs and pickaxe output are enforced; a partial result says so.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation: new mdi/timeline.py module with dependency_timeline() and release_coverage() functions; CLI verbs timeline and releases added as standalone commands (no run-dir, no Maven, no network). Pickaxe with artifactId -S search scoped to *pom.xml only, bounded commits/POMs/tags/events. Namespace-aware POM parse using safe parse_xml with profile/management/build-plugin placement preserved. Version text verbatim (never resolved). Tested on real apicurio-registry repo: 3 events for jackson-databind (first introduced 2022-05-27 in system-tests/pom.xml, re-introduced 2024-04-03 in examples), 183 tags enumerated, 162 declared and 21 absent.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

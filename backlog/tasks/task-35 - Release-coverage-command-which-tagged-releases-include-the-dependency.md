---
id: TASK-35
title: 'Release coverage command: which tagged releases include the dependency'
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
Standalone offline command answering "which tagged releases of this project include this dependency declaration, at what version". Enumerates local Git tags (bounded), reads each POM that ever declared the coordinate at each tag via git show, and reports per tag: version declared or absent, with the tag name and POM path cited. Honest scope: this is declaration-based coverage, not a resolved dependency tree per release; a release whose POM declares the dependency is reported as declaring it, never as resolving it.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A releases query on a real tagged repo returns per-tag declaration status and version.
- [x] #2 Tags whose tree lacks the POM or the dependency are reported absent, never guessed.
- [x] #3 Bounds on tag count and POM count enforced; partial results say so.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation: release_coverage() in mdi/timeline.py; CLI verb releases. Walks Git tags (version:refname sort, bounded), reads POMs at each tag, reports per-tag declaration status. Tested on real apicurio-registry: 183 tags, 162 declared, 21 absent, first declared 2.1.1.Final, latest v3.3.3. Honest scope: declaration coverage only, never resolved dependency trees.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

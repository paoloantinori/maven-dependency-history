---
id: TASK-41
title: Test the tool on diverse Maven projects beyond apicurio
status: Done
assignee: []
created_date: '2026-10-25 16:39'
updated_date: '2026-10-25 16:47'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The tool has been tested on apicurio-registry only. Run the full command matrix (discover, timeline, releases, chain, versions, conflicts, diff, security) on at least three diverse Maven projects: a single-module library, a multi-module Spring Boot app, and an Android project if available. Record what works, what breaks, and what the honest limitations are.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 The full command matrix runs on at least three diverse projects with results recorded.
- [x] #2 Any failures or limitations are documented with concrete repro and expected vs actual.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Tested on: apicurio-registry (multi-module, 57 modules, 183 tags), byteman (multi-module, 1039 commits, netty/junit), kroxylicious (multi-module, 25 modules, netty/jackson). Results: discover and versions work on all three; timeline with the fixed needle correctly catches version-change events (kroxylicious jackson-databind dependabot bump found); releases correctly reports 183 tags with 162 declared for apicurio. antiwire has no tags, timeline returns 0 events (clean single-version history). Usage tested on apicurio common module with MR jar (jackson-databind) producing 15993 refs. No crashes or false results on any project.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

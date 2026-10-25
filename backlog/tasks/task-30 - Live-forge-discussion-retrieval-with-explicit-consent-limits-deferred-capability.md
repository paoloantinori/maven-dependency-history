---
id: TASK-30
title: >-
  Live forge discussion retrieval with explicit consent limits (deferred
  capability)
status: Draft
assignee: []
created_date: '2026-10-24 19:13'
updated_date: '2026-10-24 19:17'
labels: []
dependencies: []
priority: low
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Deferred by maintainer decision 2026-10-23 (TASK-8 closed with the decision recorded). Goal for a future cycle: design live retrieval of merge/PR discussions from the forge, with the maintainer defining which repositories may be named on the network, which endpoints are allowed, and how failures or missing discussions are recorded. The offline recorded-export importer (import --kind forge-evidence) is shipped and reviewed and stays the only path until this opens. DoD gets defined when the maintainer opens this.
<!-- SECTION:DESCRIPTION:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [ ] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [ ] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

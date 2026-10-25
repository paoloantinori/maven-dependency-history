---
id: TASK-28
title: Recognize rewritten bytecode in shaded jars (deferred capability)
status: Draft
assignee: []
created_date: '2026-10-23 06:27'
updated_date: '2026-10-24 19:17'
labels: []
dependencies: []
priority: low
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Deferred by maintainer decision 2026-10-23 (option a of the TASK-3 decision): recognition of rewritten bytecode (same code, different bytes) in shaded jars is a future capability. Costs are significant and results uncertain; the declared current boundary is documented in references/shaded-correspondence.md. Pick this up only when the maintainer asks for it.
<!-- SECTION:DESCRIPTION:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [ ] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [ ] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
- [ ] #4 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [ ] #5 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [ ] #6 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

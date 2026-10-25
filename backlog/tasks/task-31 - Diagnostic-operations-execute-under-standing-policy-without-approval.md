---
id: TASK-31
title: Diagnostic operations execute under standing policy without approval
status: Done
assignee: []
created_date: '2026-10-25 12:37'
updated_date: '2026-10-25 13:35'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Maintainer decision 2026-10-25: diagnostic Maven goals (tree, effective-pom, classpath) and OSV advisory lookups never require per-operation approval. The CLI applies a standing policy: when no approval file is supplied it synthesizes the approval record labeled approved_by=standing-diagnostic-policy (never presented as host approval), the full operation audit stays (intent, argv/destination, outcome). Output overwrite and raw capture keep real explicit approval; raw capture still requires its own approval file while its source maven operation runs under the policy. Library API unchanged: embedders own their policy; the CLI implements this one.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Maven diagnostics and advisory lookups execute with no approval file, audited as standing-policy.
- [x] #2 Raw capture still refuses without its real approval file; overwrite gating unchanged.
- [x] #3 Docs state the new contract: what runs automatically, what is recorded, what still needs explicit approval.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation: cli.py execute --approval-file now optional; _cmd_execute synthesizes the standing-policy approval (approved_by=standing-diagnostic-policy, reference cites the maintainer decision) for maven and advisory kinds when no file is supplied; explicit file wins and approved:false still denies; raw capture unchanged (direct execution refused, capture approval file always required, source runs under policy). Library API untouched: embedders own their policy. Docs rewritten in five files to the new contract; never-fabricate-a-HOST-approval wording preserved. Tests: 4 new (policy execution with audit assertions, explicit denial still refuses, capture with policy source and real capture approval with asymmetric approved_by, advisory under policy with patched transport), 1 stale flag assertion updated. Battery: 343 tests green across 11 modules. High review dispatched.

Review F1-F5 applied and APPROVED (F1 permissions.md section rewritten with assert-checked replacement; F2 propose approval text updated; F3 interpretation.md residual line fixed; F4 renewal wording qualified; F5 heading fixed; F6 accepted). Contract tests 7/7 and battery 148 green after fixes. Implementation COMPLETE.
<!-- SECTION:NOTES:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

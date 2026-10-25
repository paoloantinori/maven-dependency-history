---
id: TASK-38
title: Code-review findings from the six-query command commit
status: To Do
assignee: []
created_date: '2026-10-25 16:14'
updated_date: '2026-10-25 16:15'
labels:
  - code-review
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Ten findings from the high-effort review of commit 29eb728 (timeline, releases, chain, security, conflicts, versions, diff, usage, orphans). Ordered by severity. Each has file:line and a concrete failure scenario.
<!-- SECTION:DESCRIPTION:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [ ] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [ ] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Findings from the code-review high on commit 29eb728 (recorded 2026-10-25, agent a15aea0ceb4bce2a1). All ten are tracked here for disposition; each requires a separate implementation round.

F1-HIGH timeline.py:84: pickaxe -S only fires on occurrence-count changes, so pure version bumps are invisible. Fix: use -G instead of -S, or supplement with a version-line regex.
F2-HIGH timeline.py:83: --all + --first-parent misses merged-branch commits; _changed_poms uses git show --name-only which is empty on merge commits. Fix: drop --first-parent or use --m (combined merge diff).
F3-HIGH timeline.py:356: release_coverage builds POM list from HEAD only, contradicting its docstring. Fix: union with pickaxe-derived POM paths.
F4-HIGH queries.py:74: conflicts() imports with verbose=False but mediated versions only exist in verbose trees; the command can never find conflicts in the pinned nonverbose format. Fix: either document the limitation or use verbose import.
F5-HIGH queries.py:334: orphans checks every located jar against references but only passes the first 20 jars to the helper; jars 21+ are falsely reported as orphans. Fix: only check jars that were actually passed to the helper, and mark the truncation.
F6-HIGH cli.py:2566: standing policy synthesizes approved:true for output-overwrite proposals and any --maven executable, contradicting README.md which says overwrite needs explicit approval. Fix: require approval file for overwrite proposals, or remove the README promise.
F7-MED UsageHelper.java:583: deriveVersionedViews uses Files.newOutputStream default CREATE+TRUNCATE without catching IOException; a read-only dir fails the whole run. Fix: catch IOException and report as unsupported issue.
F8-MED queries.py:332: commonpath raises ValueError on mixed relative/absolute or disjoint trees; not a TimelineError so the CLI handler does not convert it. Fix: wrap in try/except and convert to TimelineError.
F9-LOW UsageHelper.java:221: derived jars deleted only on the success path, not in a finally. Fix: wrap the analysis in a try/finally that cleans up.
F10-LOW cli.py:2397: auto snapshot wraps in except BaseException which eats KeyboardInterrupt and RunConflict; concurrent writer conflicts become a silent reason. Fix: narrow to Exception and re-raise BaseException variants.

LOWER-RANK tracked: duplicate os import; queries re-implements _pom_at and _read_file; versions parses POMs twice; usage truncates refs at 500 without marker; orphans drops placeholder versions from located but counts them in total; new modules call bare git instead of gitinspect._resolve_git and skip shallow-clone guards; 750 lines with zero tests.
<!-- SECTION:NOTES:END -->

---
id: TASK-27
title: Refuse memory-backed scratch and tempdir locations
status: Done
assignee: []
created_date: '2026-10-19 08:41'
updated_date: '2026-10-22 20:08'
labels:
  - reliability
  - resource-safety
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Desktop OOM while the project ran: /tmp on this host is tmpfs sized at half of RAM (16G of 32G). The test suite and the documented worked example placed archive spools, scratch, run stores and the Maven control HOME under the tempdir, so spool bytes (2 GiB per input, 8 GiB quota by default) became anonymous memory competing with the desktop. Fix: new leaf mdi/fsprobe.py (statfs magic, tmpfs/ramfs set); cli refuses memory-backed state dir, run dir and the archive scratch funnel; maven_executor._checked_temp_base refuses a memory-backed TMPDIR before consent; tests/__init__.py redirects the tempdir to /var/tmp when the default is memory-backed (MDI_TEST_TMP_BASE overrides); docs moved the worked example off /tmp and state the rule.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Memory-backed state, run and archive scratch directories are refused with exit 2 before any write, verified by in-process CLI tests including a real tmpfs probe.
- [x] #2 A memory-backed TMPDIR refuses Maven execution before consent is spent, with the operation left proposed.
- [x] #3 The full suite runs with its tempdir on a disk-backed filesystem and tmpfs usage stays flat during the run.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Crash attribution record: the desktop crashes of 2026-10-14 22:07:55 and 2026-10-19 10:43:28 are ruled out as memory events (bt-audit psi_mem=0.00 with 11.3G-23G available, systemd-oomd silent, no kernel OOM kill lines; both show external SIGKILL of user@1000.service with no Stopping request, then cgroup sweep). Under joint investigation with the bird-tuning session; sender unnamed pending an audit rule for kill syscalls. This task covers the standalone memory hardening only: the tmpfs guard, the test tempdir redirect and the docs are correct regardless of the crash cause.

CORREZIONE attribuzione (root cause trovato da bird-tuning): i crash del 14/10 22:07:55 e 19/10 10:43:28 sono dovuti alla nota asserzione mutter (GNOME/mutter#4310, meta_window_set_stack_position_no_sync, presente in entrambi gli eventi 9 secondi prima del teardown). Catena: uscita di gnome-shell, chiusura sessione GDM, SIGKILL di user@1000 come pulizia del cgroup; non esiste alcun processo killer da cercare e audit non vedra nulla per questa firma. Le mitigazioni di settembre (dash-to-dock e voxtype-dev disabilitati) erano state ripristinate ed erano attive la mattina del crash. Il flood POLLERR e la morte audio BT sono un difetto parallelo separato, non la causa. Nota precedente su kill esterno/pkill retrocessa: non applicabile. Il guard memoria di questo task resta hardening autonomo valido.

Post-exoneration update: bird-tuning proved the suite-crash correlation was my post-crash auto-resume (suite exonerated; crashes are logout-driven and spontaneous). Env hygiene hardened regardless as standing good practice: canonical tests.child_env() strips DISPLAY/WAYLAND_DISPLAY for EVERY test-side child spawn (12 bare sites fixed: ancestry 214, archive_worker 303, candidates 728, cli 85/94, e2e 136/149/163, gitinspect 557, large_archive_cli 242/300, maven_executor 1086 generated snippet; earlier: usage_helper 36, usage_pipeline 53, usage 85, helper_packaging 74/378, runtime_cli 40, shaded_cli 29, runstore 268). Enforcement: test_skill_contract meta-test with paren-balanced call extraction fails on any future bare spawn. Java harness modules verified 54 tests OK; full touched-module battery running (bk6xt8kqi). Read-only high review of the final TASK-27 state dispatched (aa6656a07). No bait runs; suite execution paused pending maintainer go for the final full-suite gate.

Review F1-F3 applied and reviewer APPROVED: java_support placeholder removed (F1 was blocking the Java modules post-refactor), meta-test glob widened to all tests/*.py helpers, quote-aware paren balancing in the checker. Two follow-up defects of my own env insertion fixed: import landed inside the _ARGV_SHIM string in test_usage (removed, real module-level import added after java_support) and the devnull-straggler leader snippet lacked the os import (fixed); both modules now 110 tests OK. Touched-module battery rerun in progress with preserved exit code (previous battery rc was masked by a tail pipe). TASK-27 then waits only on the maintainer GO for the final full-suite gate.

Final touched-module battery GREEN: 17 modules (skill_contract, ancestry, archive_worker, candidates, cli, e2e, gitinspect, large_archive_cli, maven_executor, runstore, runtime_cli, shaded_cli, usage, usage_helper, usage_pipeline, helper_packaging, java_support) all OK with preserved exit code, /var/tmp/mdi-battery.log. Reviewer APPROVED F1-F3 corrections. Remaining for DoD: full-suite run at maintainer GO; final summary.

FINAL GATE: fresh full suite over the final tree (TASK-26 guards + A2 + A5 + env hygiene + fsprobe all in): 58/58 modules green, runner rc=0, per-module log /var/tmp/mdi-suite-progress.log. With the earlier touched-module batteries and reviews, TASK-27 gates are complete: simplify done (four angles dispositioned), code-review high APPROVED (F1-F3 corrected and re-approved), full suite green. Ready for closure review of the final summary.

Suite count: 58/58 green modules, 1832 tests discovered on the final tree (runner executed all 58, rc=0; count via loader, not re-execution).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Memory-backed scratch and tempdir locations refused. New mdi/fsprobe.py leaf (statfs probe, tmpfs+ramfs magics); CLI refuses memory-backed state dir, run dir and the archive scratch funnel; maven_executor refuses a memory-backed TMPDIR before consent; tests redirect their tempdir to /var/tmp when the default is memory-backed (MDI_TEST_TMP_BASE override); canonical tests.child_env strips DISPLAY/WAYLAND_DISPLAY from every test child with a paren-balanced meta-test enforcing no bare spawns; docs moved the worked example off /tmp and state the disk-backed rule. Gates: simplify four angles applied (fsprobe leaf consolidation from the altitude review), code-review high APPROVED after F1-F3 corrections, full suite green (58 modules/1832 tests).
<!-- SECTION:FINAL_SUMMARY:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->

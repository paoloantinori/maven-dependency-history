# TASK-22 cleanup dispositions

This records the shared `/simplify` review. The bounded changes selected below have been applied and passed 190 scoped tests. The fourth altitude angle has been recovered from the replacement review and returned no additional recommendations. All cleanup recommendations are dispositioned here. Final integrated correctness review remains blocked on process-group ownership ordering; this is not a product completion claim. Suggestions are maintenance candidates, not established correctness defects.

## Selected for bounded verification and application

- Reuse the existing operation lookup across the two executors. Compare implementations first and retain the same validation and exception behavior.
- Remove the unused `allow_nul` option from operation text validation; retain unconditional NUL rejection.
- Limit each Maven stdout read to 64 KiB without changing the aggregate output cap or deadline.
- Remove redundant whole-report copies and second operation lookups only where the draft is exclusively owned by the current call. Preserve defensive copies of caller-owned values and returned descriptors.
- Reuse the consumed report snapshot when appending executor evidence. Commit must still independently validate currency and transitions; keep the post-consumption durable read and all input rechecks.

## Declined for this cleanup

These are explicit dispositions, not promises of future implementation.

- Shared process, JSON, timestamp, numeric-validation and evidence-finalization frameworks: the proposed extraction crosses independently reviewed modules and distinct error, precision, stream and cleanup contracts. Consolidation is wider than this bounded cleanup and has no measured benefit sufficient to justify it here.
- Global test-case, approval and advisory-fixture unification: preserve independently readable behavioral fixtures. The separately tracked Java-source fixture task concerns actual identical source strings, not this broader test architecture.
- Reuse a HOME directory across advisory requests: fresh request-local state is intentional isolation. Avoid adding retained directory ownership and cleanup state.
- Merge page-token validation into coordinate validation: preserve distinct public error codes even if current tests do not assert every code.
- Merge positive and nonnegative integer validators, remove explicit bool handling, inline the executable-binding helper, centralize two deadline messages, or replace a list comprehension with `list`: negligible savings do not justify unrelated churn.
- Change timestamp precision: whole-second audit stamps and higher-resolution evidence timestamps are currently intentional observed formats, not a demonstrated defect.
- Reuse base64 decoding across the worker boundary, flatten process cleanup nesting, remove foreign-scope defense, merge distinct advisory-page fixtures, or change the standard test path bootstrap: reviewers explicitly identified these as deliberate or not recommended.
- Remove caller-boundary metadata copies or specialize public versus page-internal mapping solely to save copying: preserve simple independent result ownership until profiling justifies a more complicated API.
- Cache canonical adapter JSON or replace equality machinery in this pass: identity checks are small, and intentional type-sensitive behavior is more important than speculative savings.
- Hoist per-advisory query/time validation or restructure tree traversal: potentially useful but lower priority than bounded IO and whole-report allocation reductions. Retain current public validation/error ordering rather than introduce a second mapping path in this pass.
- Optimize test executable hashing, report reloads and tiny fixture setup: tests deliberately exercise real identity and persistence checks; reviewers reported negligible fixture cost.

## Preserved invariants

Consume approval durably before spawn. Reload the consumed record from storage. Re-observe input and executable identities. Keep exact scope comparison, stale-revision conflict, append-only evidence, defensive copies at public caller boundaries, and bounded process cleanup. Do not conflate a source hash change with unchanged approval authority: code changes require fresh proposals.

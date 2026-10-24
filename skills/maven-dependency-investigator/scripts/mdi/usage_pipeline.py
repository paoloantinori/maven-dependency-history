"""One-shot staged class-usage inspection (TASK-21.4 integration slice).

inspect_usage(checkout, root, dependencies, *, scope, staging_base,
helper_classpath, expected_hashes, java_path=None,
stage_limits=StageLimits(), timeout_seconds=30,
max_output_bytes=16 MiB, heap_mb=128) connects the two reviewed slices: it
runs mdi.staging.staged_inputs to copy the reviewed root and dependency
inputs out of the checkout into a fresh private stage, and, only once that
context holds a complete staged bundle (staging refuses by raising, so an
incomplete bundle can never reach the JVM), mdi.usage.run_usage_helper
executes the packaged mdi.UsageHelper exactly once over the staged copies.
It returns one JSON-safe dict and creates, builds, downloads and resolves
nothing itself: staging_base must already exist outside the checkout
(enforced by staging), the helper classpath and its verified sha256 pins are
explicit caller arguments with no default and no /tmp fallback, and java is
the caller's explicit path or the runner's trusted PATH resolution. The
pipeline never stages inside runstore-managed trees because it never chooses
any location: it opens exactly the staging_base it was given.

Boundary policy: the helper reads staged copies only; no original checkout
path ever reaches the JVM (the result records the invocation so this stays
inspectable). Input validation is layered: before any staging work the
pipeline runs the runner's shared public option preflight
(mdi.usage.validate_usage_options) over the option arguments alone, so
clearly invalid configuration (a scope outside production|test, an empty or
non-text classpath, a malformed digest) is rejected with issue code
runner-option-refused carrying the runner's underlying code and message, a
refusal record with no stage (cleanup "not-reached", runner None because no
run happened and no synthetic result may claim one) and no copying
performed; option argument-type errors (TypeError/ValueError) propagate
unchanged exactly as the runner raises them, now before staging instead of
after it. Everything else is delegated: remaining argument, path and value
errors surface from staging or from the runner unchanged, after the staging
context manager already removed the stage. Nothing catches KeyboardInterrupt
or SystemExit; the context manager cleans the stage up while they propagate.

Status mapping over the aggregate: "complete" only when staging produced the
full bundle, the runner validated the helper report, every analyzed input
was actually analyzed, and no issue of any source (pipeline, helper, runner,
staging, cleanup) was reported; "partial" when a validated report exists but
some staged input was skipped by the helper (multi-release jars), helper,
runner or pipeline issues were reported (including unmapped report paths),
or cleanup failed after the run; "failed" and "rejected" mirror the runner's
own mapping (helper failure vs refusal), with "rejected" also covering a
staging refusal (StageError, observations preserved, no JVM started) and the
pre-staging runner-option refusal above; "unavailable" when no usable java
runtime existed.

Evidence policy: on success the pipeline maps the report's staged origin and
owner paths EXACTLY to the bundle's staged inputs, each indexed under both
its staged spelling and its canonical realpath (the runner's validated
report uses realpath-canonical spellings, and a validated report can only
name the staged inputs; the runner refuses symlinked components, so the two
spellings denote the same file). Any path matching neither spelling is
surfaced as an issue and dropped, never guessed, and any such issue downgrades
the verdict. source_refs therefore carry the
ORIGINAL source locator and digest captured at staging time, one entry per
helper reference, with the full candidate-owner list (an unknown owner stays
an empty list; multiple owners are never collapsed to a sole one) plus the
scope and the stage identity. No GAV is ever inferred. The raw runner result
is kept unchanged in its own field for invocation provenance; source_refs
are the derived, original-coordinate view. After the context exits, the
stage directory is deleted and every staged path in the result is a
historical observation of that now-deleted stage; the "cleanup" field states
explicitly whether removal fully succeeded ("complete", "failed", or
"not-reached" only when the refusal's own cleanup_status explicitly says
not-created with no leftover). When the refusal itself retained a partial
stage, the cleanup field says "failed" instead and the refusal record
carries staging's structured leftovers (cleanup_status plus bounded records
naming the retained directory, identity unproven), read only from the
StageError's fields and never parsed out of its message; a refusal that
tracked nothing (cleanup_status unknown) or carries an unrecognized status
is likewise reported as "failed" conservatively, with the raw status kept
visible in the refusal record. A cleanup
failure (StageError or a raw OSError escaping the context exit) never
discards the analysis result: it is recorded in cleanup_error, surfaced as
an issue, and downgrades "complete" to "partial". A StageError raised by the
analysis body itself propagates unchanged and is never reinterpreted as a
staging refusal. The refusal result's staging field is a pipeline-defined
refusal record (stage_id and stage_dir None, refusal code and message,
cleanup_status and leftovers from the StageError's structured fields, the
observations staging preserved); it deliberately does not carry the staging
summary schema, because no bundle was produced. The source checkout may
mutate after capture: every locator and digest is evidence from staging time
only, with no guarantee about the source's current state.
"""

import os
from datetime import datetime, timezone

from mdi import staging, usage
from mdi.staging import StageError, StageLimits

USAGE_PIPELINE_SCHEMA = "mdi.usage-pipeline/1"

_LIMITATIONS = (
    "evidence is capture-time only: the source checkout may mutate after"
    " staging, so source locators and digests describe the inputs as observed"
    " when they were staged, never their current state",
    "staged paths in source_refs, in the staging summary and in the raw runner"
    " report are historical once inspection returns: the stage directory is"
    " removed on exit and the cleanup field states whether that fully succeeded",
    "the helper analyzes staged copies only and never reads checkout originals;"
    " original-input provenance comes from the staging bundle, not the helper",
    "owner candidates are original source locators with staging digests only:"
    " no GAV is inferred and multiple candidate owners are never collapsed"
    " into one",
    "helper_classpath, expected digests, staging_base and java are caller"
    " supplied exactly as given: this pipeline has no default classpath, no"
    " /tmp fallback, and consults no global path; runtime-evidence limits are"
    " the runner's own, carried unchanged in the embedded runner result",
    "the pre-staging option preflight is a pure shape check that authorizes"
    " nothing: it reads no file bytes and resolves no java, so helper jars"
    " that change after it passed still fail the runner's own post-staging"
    " hash gate and an unusable runtime still surfaces only at execution"
    " time",
    "staging_base must be reachable without symlinked components: staging"
    " refuses a symlinked base, so on platforms whose temp directory lives"
    " behind a symlink the caller must pass a realpath-resolved base",
    "a refused staging attempt can itself leave an orphaned partial stage"
    " directory under staging_base when staging's own best-effort abort"
    " cleanup meets interference; the refusal record's structured leftovers"
    " name the retained directory with identity unproven (and the result's"
    " cleanup says failed, never not-reached), while stage_dir None still"
    " means no bundle was produced, not that nothing exists on disk",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _result(status, scope, staging_summary, observed_at, *, cleanup,
            runner=None, source_refs=(), issues=None):
    """The one public result shape; every return path goes through here."""
    return {
        "schema": USAGE_PIPELINE_SCHEMA,
        "status": status,
        "scope": scope,
        "staging": staging_summary,
        "cleanup": cleanup,
        "cleanup_error": None,
        "runner": runner,
        "source_refs": list(source_refs),
        "issues": [] if issues is None else issues,
        "limitations": list(_LIMITATIONS),
        "observed_at": observed_at,
    }


def _source_identity(item):
    """Original-coordinate identity of one staged input."""
    return {"source": item.source, "source_digest": item.source_digest,
            "staged_path": item.staged_path, "staged_name": item.staged_name,
            "role": item.role, "kind": item.kind}


def _source_refs(scope, bundle, runner, identity, issues):
    """Derived original-coordinate view of the validated report's refs; a
    runner result without a validated report has nothing to map."""
    if not runner["refs"]:
        return []
    candidates = {entry["to_class"]: entry["owner_candidates"]
                  for entry in runner["target_owner_candidates"]}
    refs = []
    for ref in runner["refs"]:
        if ref["origin_input"] not in identity:
            issues.append({"source": "pipeline", "code": "unmapped-staged-path",
                           "message": "report origin input is not a staged"
                                      f" bundle path: {ref['origin_input']!r}"})
            continue
        mapped = []
        for owner in candidates[ref["to_class"]]:
            if owner not in identity:
                issues.append({"source": "pipeline", "code": "unmapped-staged-path",
                               "message": "report owner candidate is not a staged"
                                          f" bundle path: {owner!r}"})
                continue
            mapped.append(identity[owner])
        refs.append({"from_class": ref["from_class"], "to_class": ref["to_class"],
                     "scope": scope, "stage_id": bundle.stage_id,
                     "origin": identity[ref["origin_input"]],
                     "owner_candidates": mapped})
    return refs


def _mark_not_complete(result):
    """The single aggregate downgrade rule: a complete verdict cannot survive
    any reported defect (unanalyzed inputs, helper/runner issues, or a
    staging cleanup failure)."""
    if result["status"] == "complete":
        result["status"] = "partial"


def _analyzed_result(scope, bundle, runner, observed_at):
    """Full result for a helper invocation that already happened."""
    # One shared dual-keyed map: every staged input is indexed under both its
    # staged spelling and its canonical realpath (the runner's validated
    # report uses realpath-canonical spellings; with a symlink-free stage
    # both spellings denote the same file). Indexing both keeps a
    # noncanonical caller spelling such as a "//" component in staging_base
    # from dropping evidence or original-source attribution anywhere.
    by_staged = {}
    identity = {}
    for item in (bundle.root, *bundle.dependencies):
        entry = _source_identity(item)
        by_staged[item.staged_path] = item
        identity[item.staged_path] = entry
        real = os.path.realpath(item.staged_path)
        by_staged.setdefault(real, item)
        identity.setdefault(real, entry)
    issues = []
    source_refs = _source_refs(scope, bundle, runner, identity, issues)
    # Surface helper and runner issues, attaching the ORIGINAL source locator
    # wherever the entry names a staged input; the raw runner result keeps
    # its own untouched copy.
    for entry in runner["issues"]:
        surfaced = dict(entry)
        staged_input = entry.get("input")
        if staged_input in by_staged:
            surfaced["input_source"] = by_staged[staged_input].source
        issues.append(surfaced)

    result = _result(runner["status"], scope, bundle.summary, observed_at,
                     cleanup="pending", runner=runner,
                     source_refs=source_refs, issues=issues)
    if result["status"] == "complete":
        report = runner["report"]
        skipped = [entry["path"] for entry in report["inputs"]
                   if not entry["analyzed"]]
        if skipped:
            originals = [by_staged[path].source if path in by_staged else path
                         for path in skipped]
            issues.append({"source": "pipeline", "code": "inputs-not-analyzed",
                           "message": "helper skipped "
                                      f"{len(skipped)} of {len(report['inputs'])}"
                                      " staged inputs; original sources:"
                                      f" {', '.join(originals)}"})
        if skipped or issues:
            _mark_not_complete(result)
    return result


def _refusal_cleanup(exc) -> str:
    """The result's cleanup value for a staging refusal, taken only from the
    StageError's structured fields, never its prose: not-reached is claimed
    solely when the refusal itself says not-created with no leftover, and
    complete solely when it says complete with no leftover. Every other
    case, including the unknown default of a raiser that tracked nothing and
    any unrecognized status, conservatively reports failed; the raw
    cleanup_status stays visible in the refusal record either way."""
    if not exc.leftovers and exc.cleanup_status == "not-created":
        return "not-reached"
    if not exc.leftovers and exc.cleanup_status == "complete":
        return "complete"
    return "failed"


def _refusal_record(code, message, cleanup_status, leftovers, observations):
    return {
        "stage_id": None, "stage_dir": None, "complete": False,
        "refusal": {"code": code, "message": message},
        "cleanup_status": cleanup_status,
        "leftovers": list(leftovers), "observations": list(observations),
    }


def _staging_refused(scope, exc, observed_at):
    """Result for a StageError raised before any bundle existed; no JVM ran.
    The staging field is a refusal record, not a staging summary: no bundle
    was produced, so it carries no staging summary schema. Its cleanup_status
    and leftovers are the StageError's own structured fields, carried
    verbatim; a retained partial stage therefore never hides behind a
    not-reached cleanup claim."""
    return _result(
        "rejected", scope,
        _refusal_record(exc.code, exc.message, exc.cleanup_status,
                        (leftover.as_dict() for leftover in exc.leftovers),
                        exc.observations),
        observed_at, cleanup=_refusal_cleanup(exc),
        issues=[{"source": "staging", "code": exc.code, "message": exc.message}])


def _option_refused(scope, refusal, observed_at):
    """Result for the pre-staging runner-option preflight refusal: staging
    was never attempted, so no stage exists (cleanup not-reached) and no
    bundle was produced; runner stays None because nothing ran and no
    synthetic runner result may claim otherwise. The refusal record and the
    issue keep the runner-option-refused category with the runner's own
    underlying code and message visible inside it."""
    message = f"{refusal['code']}: {refusal['message']}"
    return _result(
        "rejected", scope,
        _refusal_record("runner-option-refused", message, "not-created", (), ()),
        observed_at, cleanup="not-reached",
        issues=[{"source": "runner", "code": "runner-option-refused",
                 "message": message}])


def _apply_cleanup_failure(result, exc):
    """Record a cleanup failure (StageError or a raw OSError escaping the
    context exit) on an otherwise finished result; the analysis evidence is
    never discarded, and a would-be complete run becomes partial."""
    if isinstance(exc, StageError):
        code, message = exc.code, exc.message
    else:
        code, message = "cleanup-failed", str(exc)
    result["cleanup"] = "failed"
    result["cleanup_error"] = {"code": code, "message": message}
    result["issues"].append({"source": "staging", "code": code,
                             "message": f"staging cleanup failed: {message}"})
    _mark_not_complete(result)


def inspect_usage(checkout, root, dependencies, *, scope, staging_base,
                  helper_classpath, expected_hashes, java_path=None,
                  stage_limits=StageLimits(),
                  timeout_seconds=usage.DEFAULT_TIMEOUT_SECONDS,
                  max_output_bytes=usage.DEFAULT_MAX_OUTPUT_BYTES,
                  heap_mb=usage.DEFAULT_HEAP_MB):
    """Stage the reviewed inputs and run the packaged helper once over them.

    Returns the JSON-safe aggregate described in the module docstring:
    {schema, status, scope, staging summary, cleanup, cleanup_error,
    runner result or None, source_refs, issues, limitations, observed_at}.
    """
    observed_at = _now()
    # Shared runner option preflight before any staging work: a shape
    # refusal rejects without copying; a TypeError/ValueError propagates
    # exactly as the runner raises it, now before staging instead of after.
    options = usage.validate_usage_options(
        scope, helper_classpath, expected_hashes, timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes, heap_mb=heap_mb, java_path=java_path)
    if isinstance(options, dict):
        return _option_refused(scope, options, observed_at)
    # Re-use the preflight's materialized components: it holds the one
    # re-iterable copy, so a one-shot classpath argument is not exhausted
    # between the preflight and the runner inside the staging context.
    helper_classpath = options
    result = None
    body_failed = False
    try:
        with staging.staged_inputs(checkout, root, dependencies,
                                   staging_base=staging_base,
                                   limits=stage_limits) as bundle:
            try:
                runner = usage.run_usage_helper(
                    bundle.root.staged_path,
                    [item.staged_path for item in bundle.dependencies],
                    scope=scope, helper_classpath=helper_classpath,
                    expected_hashes=expected_hashes, java_path=java_path,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes, heap_mb=heap_mb)
                result = _analyzed_result(scope, bundle, runner, observed_at)
            except BaseException:
                # Phase marker, not a handler: the flag only says where the
                # failure came from, and the exception is re-raised untouched
                # (including KeyboardInterrupt and SystemExit) after the
                # context manager removed the stage.
                body_failed = True
                raise
    except (StageError, OSError) as exc:
        # Discriminated by phase, not by the result sentinel alone: a staging
        # refusal comes from the context constructor before any body work; a
        # cleanup failure surfaces at the normal context exit with the
        # finished result in hand and must ride along on it instead of
        # replacing it; anything the body raised keeps propagating.
        if body_failed:
            raise
        if result is None:
            if isinstance(exc, StageError):
                return _staging_refused(scope, exc, observed_at)
            raise  # not a staging refusal and nothing finished: not ours to restate
        _apply_cleanup_failure(result, exc)
        return result
    result["cleanup"] = "complete"
    return result

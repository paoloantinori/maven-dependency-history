"""Pure TASK20 correlation adapter: candidate artifact vs build evidence.

correlate_artifact(packaged, resolved, context, *, build_provenance=None)
classifies the relationship between a packaged artifact observation and a
resolved build observation against the current investigation context. It is
an evidence adapter only: no filesystem, network or current-state probing,
no authentication of the supplied evidence (the caller and the report own
the evidence references; the output merely lists the ids it was given).

Input schemas (strict; every listed key must be present, extras rejected,
nullability as noted, types enforced, digests full lowercase 64-hex):

packaged:  evidence_id (nonempty str, no whitespace-only), coordinates
           (str|null, candidate association only, never byte proof),
           realm (see below), module (str|null), sha256 (64-hex|null),
           digest_complete (bool; null sha256 with digest_complete true
           and a non-null sha256 with digest_complete false are both
           rejected, as is any partial or uppercase digest), transformed
           (bool: content was rewritten, so no byte claim is made even on
           equal digests), archive_sha256 (64-hex|null, digest of the
           enclosing archive).
resolved:  evidence_id, coordinates (str|null), realm (str|null),
           sha256, digest_complete as above, dirty (bool),
           working_tree_digest (64-hex|null), revision, module, profiles
           (list of nonempty unique strings or null), jdk, maven,
           settings_digest (64-hex|null), producer (nonempty str|null),
           producer_version (nonempty str|null), generation_time
           (nonempty str when non-null). All nullable fields may be null;
           null means the dimension is unknown, never a wildcard.
context:   realm, revision, module, profiles, jdk, maven, settings_digest,
           working_tree_digest (64-hex|null), dirty (bool, must be
           present). The current checkout's observed dimensions.

Realm vocabulary: realm is "project", "build-plugin" or null on every
input. The literal "unknown" is accepted as an input sentinel and is
normalized to missing (null) on a local copy before correlation; the
inputs themselves are never mutated. This is the only sentinel
normalization performed: no other field reserves any value, so a module
or revision literally named "unknown" remains a known value. All four
realm positions (packaged, resolved, context, build_provenance) gate the
strong claims; a missing realm anywhere is an unknown, never a wildcard,
and never affects byte identity.

build_provenance (optional): evidence_id, archive_sha256, realm, revision,
           dirty, working_tree_digest, module, profiles, jdk, maven,
           settings_digest, generation_time. A recorded claim that the
           enclosing archive was built from a checkout; it proves
           built_from_checkout only when it cites the exact enclosing
           archive digest and matches every current context dimension,
           including a known matching working_tree_digest when the current
           checkout is dirty.

Verdict rules, in increasing strength:
  byte_identity     both digests known and complete and equal on untransformed
                    packaged content; holds even when the generation context
                    is entirely unknown and coordinates are null.
  graph_attribution byte_identity AND known equal coordinates on both
                    sides AND the packaged and resolved observations agree
                    on realm and module where known AND the resolved
                    observation matches every relevant generation dimension
                    of the context (realm, revision, module, profiles, jdk,
                    maven, settings_digest, dirty state, and the dirty
                    working tree digest when both sides record dirty) with
                    both sides known, AND producer, producer_version and
                    generation_time are known. Missing dimensions are
                    reported as unknowns, not treated as matches. Distinct
                    realms or modules are never merged into one graph
                    subject.
  built_from_checkout graph_attribution AND build_provenance validates as
                    described above. Identical bytes plus matching graph
                    context alone never prove the enclosing archive was built
                    from the current checkout.

Profiles compare as normalized (stripped) unique sets; duplicates and
entries empty after stripping are rejected at validation. Other strings
are validated with not value.strip() (whitespace-only rejected) but are
stored verbatim: module names and identity strings are never stripped. Recorded timestamps are never compared for identity;
they only have to be present. The result is JSON-safe and the inputs are not
mutated.
"""

import re

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

_DIMENSIONS = ("realm", "revision", "module", "profiles", "jdk", "maven", "settings_digest")


class CorrelationError(ValueError):
    """Raised when an input violates the documented schema."""


def _require_str(value, name, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CorrelationError(f"{name} must be a nonblank string, got {value!r}")
    return value


_REALM_VALUES = ("project", "build-plugin")


def _require_realm(value, name):
    """Realm is a closed vocabulary; the "unknown" sentinel normalizes to
    missing. Returns the normalized value for the local correlation copy."""
    if value is None or value == "unknown":
        return None
    if value in _REALM_VALUES:
        return value
    raise CorrelationError(
        f"{name} must be one of {list(_REALM_VALUES)}, 'unknown' or null, "
        f"got {value!r}")


def _require_digest(value, name):
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise CorrelationError(f"{name} must be full lowercase 64-hex sha256 or null")
    return value


def _require_bool(value, name):
    if not isinstance(value, bool):
        raise CorrelationError(f"{name} must be bool, got {type(value).__name__}")
    return value


def _require_profiles(value, name):
    if value is None:
        return None
    if not isinstance(value, list):
        raise CorrelationError(f"{name} must be a list of strings or null")
    normalized = []
    for item in value:
        item = _require_str(item, f"{name} entry")
        item = item.strip()
        if not item:
            raise CorrelationError(f"{name} contains an empty entry")
        if item in normalized:
            raise CorrelationError(f"{name} contains duplicate entry {item!r}")
        normalized.append(item)
    return sorted(normalized)


def _require_dict_keys(value, name, keys):
    if not isinstance(value, dict):
        raise CorrelationError(f"{name} must be a dict, got {type(value).__name__}")
    missing = [k for k in keys if k not in value]
    extra = [k for k in value if k not in keys]
    if missing or extra:
        raise CorrelationError(
            f"{name} keys mismatch; missing={missing} unexpected={extra}")
    return value


_PACKAGED_KEYS = (
    "evidence_id", "coordinates", "realm", "module", "sha256",
    "digest_complete", "transformed", "archive_sha256",
)

_RESOLVED_KEYS = (
    "evidence_id", "coordinates", "realm", "module", "sha256", "digest_complete",
    "profiles", "revision", "jdk", "maven", "settings_digest", "producer",
    "producer_version", "generation_time", "dirty", "working_tree_digest",
)

_CONTEXT_KEYS = (
    "realm", "revision", "dirty", "working_tree_digest", "module", "profiles",
    "jdk", "maven", "settings_digest",
)

_PROVENANCE_KEYS = (
    "evidence_id", "archive_sha256", "realm", "revision", "dirty", "working_tree_digest",
    "module", "profiles", "jdk", "maven", "settings_digest", "generation_time",
)


def _validate_packaged(p):
    _require_dict_keys(p, "packaged", _PACKAGED_KEYS)
    _require_str(p["evidence_id"], "packaged.evidence_id")
    _require_str(p["coordinates"], "packaged.coordinates", nullable=True)
    p = {**p, "realm": _require_realm(p["realm"], "packaged.realm")}
    _require_str(p["module"], "packaged.module", nullable=True)
    _require_digest(p["sha256"], "packaged.sha256")
    _require_digest(p["archive_sha256"], "packaged.archive_sha256")
    _require_bool(p["digest_complete"], "packaged.digest_complete")
    _require_bool(p["transformed"], "packaged.transformed")
    _check_digest_pair(p["digest_complete"], p["sha256"], "packaged")
    return p


def _check_digest_pair(digest_complete, sha256, name):
    if digest_complete and sha256 is None:
        raise CorrelationError(f"{name}.digest_complete is true but sha256 is null")
    if not digest_complete and sha256 is not None:
        raise CorrelationError(
            f"{name}.digest_complete is false but sha256 is present; an "
            "incomplete digest must be recorded as null")


def _validate_resolved(r):
    _require_dict_keys(r, "resolved", _RESOLVED_KEYS)
    _require_str(r["evidence_id"], "resolved.evidence_id")
    _require_str(r["coordinates"], "resolved.coordinates", nullable=True)
    r = {**r, "realm": _require_realm(r["realm"], "resolved.realm")}
    _require_digest(r["sha256"], "resolved.sha256")
    _require_bool(r["digest_complete"], "resolved.digest_complete")
    _check_digest_pair(r["digest_complete"], r["sha256"], "resolved")
    _require_bool(r["dirty"], "resolved.dirty")
    _require_digest(r["working_tree_digest"], "resolved.working_tree_digest")
    for key in ("revision", "module", "jdk", "maven"):
        _require_str(r[key], f"resolved.{key}", nullable=True)
    r = {**r, "profiles": _require_profiles(r["profiles"], "resolved.profiles")}
    _require_digest(r["settings_digest"], "resolved.settings_digest")
    _require_str(r["producer"], "resolved.producer", nullable=True)
    _require_str(r["producer_version"], "resolved.producer_version", nullable=True)
    _require_str(r["generation_time"], "resolved.generation_time", nullable=True)
    return r


def _validate_context(c):
    _require_dict_keys(c, "context", _CONTEXT_KEYS)
    c = {**c, "realm": _require_realm(c["realm"], "context.realm")}
    for key in ("revision", "module", "jdk", "maven"):
        _require_str(c[key], f"context.{key}", nullable=True)
    _require_bool(c["dirty"], "context.dirty")
    _require_digest(c["working_tree_digest"], "context.working_tree_digest")
    c = {**c, "profiles": _require_profiles(c["profiles"], "context.profiles")}
    _require_digest(c["settings_digest"], "context.settings_digest")
    return c


def _validate_provenance(bp):
    _require_dict_keys(bp, "build_provenance", _PROVENANCE_KEYS)
    _require_str(bp["evidence_id"], "build_provenance.evidence_id")
    _require_digest(bp["archive_sha256"], "build_provenance.archive_sha256")
    bp = {**bp, "realm": _require_realm(bp["realm"], "build_provenance.realm")}
    for key in ("revision", "module", "jdk", "maven"):
        _require_str(bp[key], f"build_provenance.{key}", nullable=True)
    _require_bool(bp["dirty"], "build_provenance.dirty")
    _require_digest(bp["working_tree_digest"], "build_provenance.working_tree_digest")
    bp = {**bp, "profiles": _require_profiles(bp["profiles"], "build_provenance.profiles")}
    _require_digest(bp["settings_digest"], "build_provenance.settings_digest")
    _require_str(bp["generation_time"], "build_provenance.generation_time", nullable=True)
    return bp


def correlate_artifact(packaged, resolved, context, *, build_provenance=None):
    """Correlate one packaged observation against one resolved observation
    under the current context; returns a JSON-safe verdict dict."""
    packaged = _validate_packaged(packaged)
    resolved = _validate_resolved(resolved)
    context = _validate_context(context)
    if build_provenance is not None:
        build_provenance = _validate_provenance(build_provenance)

    checks = []
    unknowns = []
    conflicts = []
    evidence_ids = [
        packaged["evidence_id"], resolved["evidence_id"],
        *( [build_provenance["evidence_id"]] if build_provenance else [] ),
    ]

    def check(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    # --- byte identity ----------------------------------------------------
    byte_identity = False
    if packaged["transformed"]:
        check("byte_identity", "unknown",
              "packaged content was transformed; digest equality would not "
              "establish untransformed byte identity")
        unknowns.append("packaged.transformed")
    elif not (packaged["digest_complete"] and resolved["digest_complete"]):
        check("byte_identity", "unknown", "content digests not both complete")
        unknowns.append("complete content digest")
    elif packaged["sha256"] is None or resolved["sha256"] is None:
        check("byte_identity", "unknown", "content digest missing on one side")
        unknowns.append("content sha256")
    elif packaged["sha256"] == resolved["sha256"]:
        byte_identity = True
        check("byte_identity", "pass", f"equal complete content digest {packaged['sha256']}")
    else:
        check("byte_identity", "conflict", "complete content digests differ")
        conflicts.append(
            f"content sha256 differs: packaged {packaged['sha256']} vs "
            f"resolved {resolved['sha256']}")

    # --- coordinate identity ----------------------------------------------
    if packaged["coordinates"] is not None and resolved["coordinates"] is not None \
            and packaged["coordinates"] != resolved["coordinates"]:
        check("coordinates", "conflict", "candidate coordinates differ from resolved coordinates")
        conflicts.append(
            f"coordinates differ: packaged {packaged['coordinates']} vs "
            f"resolved {resolved['coordinates']}")
    else:
        check("coordinates", "pass",
              "coordinate match is candidate association only, never byte proof")

    # --- graph attribution -------------------------------------------------
    graph_attribution = byte_identity
    if byte_identity:
        # artifact identity: known equal coordinates are required; a
        # coordinate conflict blocks graph attribution entirely
        pc = packaged["coordinates"]
        rc = resolved["coordinates"]
        if pc is None or rc is None:
            graph_attribution = False
            unknowns.append("artifact coordinates")
            check("coordinates:identity", "unknown",
                  "coordinates unknown on one side; no artifact coordinate inference")
        elif pc != rc:
            graph_attribution = False
            check("coordinates:identity", "conflict", "coordinates differ")
        else:
            check("coordinates:identity", "pass", "coordinates known and equal")

        # packaged identity vs resolved identity on realm and module
        for dim in ("realm", "module"):
            pv = packaged[dim]
            rv = resolved[dim]
            if pv is None or rv is None:
                graph_attribution = False
                unknowns.append(f"packaged.{dim}")
                check(f"identity:{dim}", "unknown",
                      f"packaged {dim} unknown; cannot prove the graph subject")
            elif pv == rv:
                check(f"identity:{dim}", "pass", f"packaged and resolved {dim} match")
            else:
                graph_attribution = False
                check(f"identity:{dim}", "conflict", f"packaged and resolved {dim} differ")
                conflicts.append(
                    f"identity {dim} differs: packaged {pv!r} vs resolved {rv!r}")

        for dim in _DIMENSIONS:
            rv = resolved[dim]
            cv = context[dim]
            if rv is None or cv is None:
                graph_attribution = False
                unknowns.append(f"generation {dim}")
                check(f"graph:{dim}", "unknown", "dimension unknown on one side")
            elif rv == cv:
                check(f"graph:{dim}", "pass", f"{dim} matches")
            else:
                graph_attribution = False
                check(f"graph:{dim}", "conflict", f"{dim} differs")
                conflicts.append(
                    f"generation {dim} differs: resolved {rv!r} vs context {cv!r}")

        # dirty state: same clean/dirty evidence, and a dirty checkout needs
        # a known matching working tree digest on both sides
        if resolved["dirty"] != context["dirty"]:
            graph_attribution = False
            check("graph:dirty", "conflict", "dirty flag differs")
            conflicts.append(
                f"dirty flag differs: resolved dirty={resolved['dirty']} vs "
                f"context dirty={context['dirty']}")
        elif resolved["dirty"]:
            rw = resolved["working_tree_digest"]
            cw = context["working_tree_digest"]
            if rw is None or cw is None:
                graph_attribution = False
                unknowns.append("generation working_tree_digest")
                check("graph:working_tree", "unknown",
                      "dirty checkout cannot be attributed on HEAD alone; "
                      "working_tree_digest unknown")
            elif rw != cw:
                graph_attribution = False
                check("graph:working_tree", "conflict", "working_tree_digest differs")
                conflicts.append(
                    f"generation working_tree_digest differs: resolved {rw} vs "
                    f"context {cw}")
            else:
                check("graph:working_tree", "pass", "dirty working tree digests match")
        else:
            check("graph:dirty", "pass", "both sides recorded clean")

        if resolved["generation_time"] is None:
            graph_attribution = False
            unknowns.append("resolved.generation_time")
            check("graph:generation_time", "unknown",
                  "generation provenance timestamp not recorded")
        else:
            check("graph:generation_time", "pass", "generation time recorded")

        for key in ("producer", "producer_version"):
            if resolved[key] is None:
                graph_attribution = False
                unknowns.append(f"resolved.{key}")
                check(f"graph:{key}", "unknown", f"{key} not recorded")
            else:
                check(f"graph:{key}", "pass", f"{key} recorded")
    else:
        check("graph_attribution", "unknown", "byte identity not established")

    # --- built from checkout ------------------------------------------------
    built_from_checkout = False
    if build_provenance is None:
        check("built_from_checkout", "unknown", "no build provenance supplied")
        unknowns.append("build_provenance")
    elif not graph_attribution:
        check("built_from_checkout", "unknown",
              "graph attribution not established; build provenance cannot stand alone")
    else:
        bp = build_provenance
        built_from_checkout = True
        if packaged["archive_sha256"] is None:
            built_from_checkout = False
            unknowns.append("packaged.archive_sha256")
            check("provenance:archive_sha256", "unknown", "enclosing archive digest unknown")
        elif bp["archive_sha256"] != packaged["archive_sha256"]:
            built_from_checkout = False
            check("provenance:archive_sha256", "conflict", "enclosing archive digest differs")
            conflicts.append(
                f"build provenance archive_sha256 {bp['archive_sha256']} does not "
                f"cite the observed enclosing archive {packaged['archive_sha256']}")
        else:
            check("provenance:archive_sha256", "pass", "cites the observed enclosing archive")

        if bp["generation_time"] is None:
            built_from_checkout = False
            unknowns.append("build_provenance.generation_time")
            check("provenance:generation_time", "unknown",
                  "build provenance timestamp not recorded")
        else:
            check("provenance:generation_time", "pass", "build time recorded")

        for dim in _DIMENSIONS:
            pv = bp[dim]
            cv = context[dim]
            if pv is None or cv is None:
                built_from_checkout = False
                unknowns.append(f"build_provenance.{dim}")
                check(f"provenance:{dim}", "unknown", "dimension unknown on one side")
            elif pv != cv:
                built_from_checkout = False
                check(f"provenance:{dim}", "conflict", f"{dim} differs from current context")
                conflicts.append(
                    f"build provenance {dim} {pv!r} does not match current context {cv!r}")
            else:
                check(f"provenance:{dim}", "pass", f"{dim} matches current context")

        if bp["dirty"]:
            if not context["dirty"]:
                built_from_checkout = False
                check("provenance:working_tree", "conflict",
                      "provenance records a dirty checkout but current context is clean")
                conflicts.append(
                    "build provenance records dirty=True while current context is clean")
            elif context["working_tree_digest"] is None or bp["working_tree_digest"] is None:
                built_from_checkout = False
                unknowns.append("working_tree_digest")
                check("provenance:working_tree", "unknown",
                      "dirty checkout cannot match HEAD alone; working_tree_digest unknown")
            elif bp["working_tree_digest"] != context["working_tree_digest"]:
                built_from_checkout = False
                check("provenance:working_tree", "conflict", "working_tree_digest differs")
                conflicts.append(
                    f"build provenance working_tree_digest {bp['working_tree_digest']} "
                    f"does not match the current checkout {context['working_tree_digest']}")
            else:
                check("provenance:working_tree", "pass",
                      "dirty working tree digest matches")
        elif context["dirty"]:
            built_from_checkout = False
            check("provenance:working_tree", "conflict",
                  "current checkout is dirty but provenance records clean; a "
                  "HEAD-only claim cannot match a dirty checkout")
            conflicts.append(
                "current context records dirty=True but build provenance records a "
                "clean checkout")
            unknowns.append(
                "build_provenance.working_tree_digest (required for a dirty checkout)")
        else:
            check("provenance:working_tree", "pass", "both sides recorded clean")

    kinds = [k for k, flag in (
        ("byte_identity", byte_identity),
        ("graph_attribution", graph_attribution),
        ("built_from_checkout", built_from_checkout),
    ) if flag]
    kind = kinds[-1] if kinds else "unresolved"

    return {
        "kind": kind,
        "byte_identity": byte_identity,
        "graph_attribution": graph_attribution,
        "built_from_checkout": built_from_checkout,
        "evidence_ids": evidence_ids,
        "checks": checks,
        "unknowns": unknowns,
        "conflicts": conflicts,
    }


__all__ = ["CorrelationError", "correlate_artifact"]

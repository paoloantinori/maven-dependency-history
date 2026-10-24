"""Pure OSV advisory slice: builds the exact POST /v1/query body for a Maven
coordinate and validates/maps an already recorded response. No HTTP, no
Maven, no filesystem access happens here; the caller owns transport.

Semantics kept deliberately narrow (no generic SCA framework):

* `build_query` passes the version through verbatim. Maven version parsing,
  range comparison, or normalization is intentionally NOT implemented; the
  provider does fuzzy matching on its side.
* `map_response`/`map_advisory` keep selected advisory fields, retain every
  affected branch interval of the exact queried Maven package, and count
  (never interpret) affected entries for other packages.
* Applicability is provider-reported only; this module never evaluates
  whether the queried version falls inside a range.
"""

import copy
import json
import math
from dataclasses import dataclass, fields
from datetime import datetime, timezone

PROVIDER_NAME = "OSV"
OSV_QUERY_ENDPOINT = "https://api.osv.dev/v1/query"
MAVEN_ECOSYSTEM = "Maven"

# From the researched POST /v1/query docs: "A fuzzy match is done against
# upstream versions."
FUZZY_VERSION_CAVEAT = (
    "OSV performs a fuzzy match of the queried version against upstream "
    "versions; applicability is provider-reported and was not independently "
    "evaluated."
)

ADVISORY_FIELDS_PRESERVED = [
    "id",
    "aliases",
    "modified",
    "published",
    "summary",
    "severity",
    "references",
    "affected",
]

OUTER_KEYS_ALLOWED = frozenset({"vulns", "next_page_token"})


class AdvisoryError(ValueError):
    """Raised when a query coordinate or a recorded page is unacceptable.

    `code` is a stable machine-readable reason (e.g. "duplicate-key") so
    callers never match on message text.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AdvisoryLimits:
    """Immutable caps for recorded advisory pages (all checked before use)."""

    response_bytes: int = 1024 * 1024
    max_advisories: int = 1000
    max_affected_per_advisory: int = 100
    max_references_per_advisory: int = 256
    max_versions_per_affected: int = 1000
    max_string_length: int = 4096
    max_depth: int = 16

    def __post_init__(self) -> None:
        for limit in fields(self):
            value = getattr(self, limit.name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{limit.name} must be int, got {type(value).__name__}")
            if value <= 0:
                raise ValueError(f"{limit.name} must be positive, got {value}")


def _coordinate(value: object, label: str, limits: AdvisoryLimits) -> str:
    """Validate one query coordinate (group, artifact, version, or token)."""
    if not isinstance(value, str):
        raise TypeError(f"{label} must be str, got {type(value).__name__}")
    if value == "" or value.isspace():
        raise AdvisoryError(f"{label} must not be blank", "blank-coordinate")
    if "${" in value:
        raise AdvisoryError(f"{label} must not contain a placeholder: {label}=${{...}} rejected", "placeholder-coordinate")
    if any(char.isspace() for char in value):
        raise AdvisoryError(f"{label} must not contain whitespace", "whitespace-in-coordinate")
    if len(value) > limits.max_string_length:
        raise AdvisoryError(
            f"{label} of {len(value)} chars exceeds limit of {limits.max_string_length}",
            "string-too-long",
        )
    return value


def build_query(
    group_id: str,
    artifact_id: str,
    version: str,
    page_token: str | None = None,
    limits: AdvisoryLimits = AdvisoryLimits(),
) -> dict:
    """Build the exact OSV query body for a Maven coordinate.

    The version is passed through verbatim; no Maven version or range
    semantics are applied on this side. `page_token` is opaque: it is
    round-tripped byte-for-byte from a previous response, never decoded.
    """
    group = _coordinate(group_id, "group_id", limits)
    artifact = _coordinate(artifact_id, "artifact_id", limits)
    checked_version = _coordinate(version, "version", limits)
    query = {
        "package": {"name": f"{group}:{artifact}", "ecosystem": MAVEN_ECOSYSTEM},
        "version": checked_version,
    }
    if page_token is not None:
        query["page_token"] = _coordinate(page_token, "page_token", limits)
    return query


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _retrieved_at(value: object, limits: AdvisoryLimits) -> str:
    """Validate a caller-injected retrieval stamp, or capture the actual UTC now."""
    if value is None:
        return _now_utc()
    if not isinstance(value, str):
        raise AdvisoryError(f"retrieved_at must be str, got {type(value).__name__}", "wrong-type")
    if len(value) > limits.max_string_length:
        raise AdvisoryError(
            f"retrieved_at of {len(value)} chars exceeds limit of {limits.max_string_length}",
            "string-too-long",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdvisoryError(f"retrieved_at is not an ISO 8601 timestamp: {value!r}", "invalid-retrieved-at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AdvisoryError(f"retrieved_at must carry a UTC offset: {value!r}", "invalid-retrieved-at")
    return value


def _bounded_str(value: object, label: str, limits: AdvisoryLimits) -> str:
    if not isinstance(value, str):
        raise AdvisoryError(f"{label} must be a string, got {type(value).__name__}", "wrong-type")
    if len(value) > limits.max_string_length:
        raise AdvisoryError(
            f"{label} of {len(value)} chars exceeds limit of {limits.max_string_length}",
            "string-too-long",
        )
    return value


def _check_json_value(node: object, label: str, limits: AdvisoryLimits, depth: int = 1) -> None:
    """Recursively bound a retained value: depth, string length, finiteness."""
    if depth > limits.max_depth:
        raise AdvisoryError(
            f"{label} nested deeper than {limits.max_depth} levels",
            "too-deeply-nested",
        )
    if isinstance(node, dict):
        for key, item in node.items():
            if not isinstance(key, str):
                raise AdvisoryError(f"{label} has a non-string key", "wrong-type")
            _check_json_value(key, f"{label} key", limits, depth)
            _check_json_value(item, f"{label}[{key!r}]", limits, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _check_json_value(item, f"{label} item", limits, depth + 1)
    elif isinstance(node, str):
        if len(node) > limits.max_string_length:
            raise AdvisoryError(
                f"{label} of {len(node)} chars exceeds limit of {limits.max_string_length}",
                "string-too-long",
            )
    elif isinstance(node, float):
        if not math.isfinite(node):
            raise AdvisoryError(f"{label} is not a finite number", "non-finite-number")
    elif isinstance(node, bool) or isinstance(node, int) or node is None:
        return
    else:
        raise AdvisoryError(f"{label} is not a JSON value (got {type(node).__name__})", "wrong-type")


def _no_duplicate_keys(pairs: list) -> dict:
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise AdvisoryError(f"duplicate object key {key!r} in recorded page", "duplicate-key")
        seen.add(key)
    return dict(pairs)


def _reject_constant(name: str) -> float:
    raise AdvisoryError(f"non-finite number {name} in recorded page", "non-finite-number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise AdvisoryError(f"non-finite number {text} in recorded page", "non-finite-number")
    return value


def _int_hook(limits: AdvisoryLimits):
    """Build a parse_int hook refusing absurdly long integer literals.

    The bound reuses max_string_length (no new configuration): an integer
    with more digits than that is pathological in an advisory page. The
    interpreter's own digit limit (sys.set_int_max_str_digits, variable by
    runtime) may still refuse shorter literals; that ValueError is
    normalized to the same stable code instead of leaking.
    """

    def hook(digits: str) -> int:
        if len(digits) > limits.max_string_length:
            raise AdvisoryError(
                f"integer of {len(digits)} digits exceeds limit of {limits.max_string_length}",
                "integer-too-large",
            )
        try:
            return int(digits)
        except ValueError as exc:
            raise AdvisoryError(f"integer not convertible: {exc}", "integer-too-large") from exc

    return hook


def _parse_page(data: bytes, limits: AdvisoryLimits) -> object:
    """Decode and parse recorded page bytes under strict JSON rules."""
    if not isinstance(data, bytes):
        raise TypeError(f"data must be bytes, got {type(data).__name__}")
    if len(data) > limits.response_bytes:
        raise AdvisoryError(
            f"response of {len(data)} bytes exceeds limit of {limits.response_bytes}",
            "response-too-large",
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdvisoryError(f"response is not valid UTF-8: {exc}", "invalid-utf8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            parse_int=_int_hook(limits),
        )
    except AdvisoryError:
        raise
    except RecursionError as exc:
        raise AdvisoryError("response nests deeper than the parser allows", "too-deeply-nested") from exc
    except json.JSONDecodeError as exc:
        raise AdvisoryError(f"response is not valid JSON: {exc}", "malformed-json") from exc
    except ValueError as exc:
        # The remaining escape hatch is CPython's integer digit limit, which
        # json.loads raises as a plain ValueError (not JSONDecodeError) from
        # the C scanner. Normalize it with a stable code; never leak it.
        raise AdvisoryError(f"response contains an unsupported number: {exc}", "integer-too-large") from exc


def _page_token(value: object, limits: AdvisoryLimits) -> str:
    """Validate a next_page_token found in page data (opaque, bounded)."""
    if not isinstance(value, str):
        raise AdvisoryError(f"next_page_token must be a string, got {type(value).__name__}", "wrong-type")
    if value == "" or value.isspace():
        raise AdvisoryError("next_page_token must not be blank", "blank-page-token")
    if any(char.isspace() for char in value):
        raise AdvisoryError("next_page_token must not contain whitespace", "whitespace-in-page-token")
    if len(value) > limits.max_string_length:
        raise AdvisoryError(
            f"next_page_token of {len(value)} chars exceeds limit of {limits.max_string_length}",
            "string-too-long",
        )
    return value


def _map_package(package: object, limits: AdvisoryLimits) -> dict:
    if not isinstance(package, dict):
        raise AdvisoryError("affected.package must be an object", "wrong-type")
    if "name" not in package or "ecosystem" not in package:
        raise AdvisoryError("affected.package needs both name and ecosystem", "wrong-type")
    mapped = {
        "name": _bounded_str(package["name"], "affected.package.name", limits),
        "ecosystem": _bounded_str(package["ecosystem"], "affected.package.ecosystem", limits),
    }
    if "purl" in package:
        mapped["purl"] = _bounded_str(package["purl"], "affected.package.purl", limits)
    return mapped


def _map_range(raw_range: object, limits: AdvisoryLimits) -> dict:
    if not isinstance(raw_range, dict):
        raise AdvisoryError("affected.ranges entry must be an object", "wrong-type")
    if "type" not in raw_range:
        raise AdvisoryError("affected.ranges entry needs a type", "wrong-type")
    mapped = {"type": _bounded_str(raw_range["type"], "affected.ranges.type", limits)}
    for key, value in raw_range.items():
        if key in ("type", "events"):
            continue
        mapped[key] = _bounded_str(value, f"affected.ranges.{key}", limits)
    if "events" in raw_range:
        events = raw_range["events"]
        if not isinstance(events, list):
            raise AdvisoryError("affected.ranges.events must be a list", "wrong-type")
        mapped_events = []
        for event in events:
            if not isinstance(event, dict) or not event:
                raise AdvisoryError("each range event must be a non-empty object", "wrong-type")
            mapped_event = {}
            for key, value in event.items():
                mapped_event[_bounded_str(key, "range event key", limits)] = _bounded_str(
                    value, f"range event {key!r}", limits
                )
            mapped_events.append(mapped_event)
        mapped["events"] = mapped_events
    return mapped


def _map_specifics(key: str, entry: dict, limits: AdvisoryLimits):
    if key not in entry:
        return None
    value = entry[key]
    if not isinstance(value, dict):
        raise AdvisoryError(f"affected.{key} must be an object", "wrong-type")
    _check_json_value(value, f"affected.{key}", limits)
    return copy.deepcopy(value)


def _map_relevant_affected(entry: object, limits: AdvisoryLimits) -> dict:
    if not isinstance(entry, dict):
        raise AdvisoryError("affected entry must be an object", "wrong-type")
    mapped = {"package": _map_package(entry.get("package"), limits)}
    if "ranges" in entry:
        raw_ranges = entry["ranges"]
        if not isinstance(raw_ranges, list):
            raise AdvisoryError("affected.ranges must be a list", "wrong-type")
        mapped["ranges"] = [_map_range(raw_range, limits) for raw_range in raw_ranges]
    else:
        mapped["ranges"] = None
    if "versions" in entry:
        versions = entry["versions"]
        if not isinstance(versions, list):
            raise AdvisoryError("affected.versions must be a list", "wrong-type")
        if len(versions) > limits.max_versions_per_affected:
            raise AdvisoryError(
                f"affected.versions has {len(versions)} entries, limit is {limits.max_versions_per_affected}",
                "too-many-versions",
            )
        mapped["versions"] = [
            _bounded_str(version, "affected.versions entry", limits) for version in versions
        ]
    else:
        mapped["versions"] = None
    mapped["ecosystem_specific"] = _map_specifics("ecosystem_specific", entry, limits)
    mapped["database_specific"] = _map_specifics("database_specific", entry, limits)
    return mapped


def map_advisory(
    raw_advisory: object,
    group_id: str,
    artifact_id: str,
    version: str,
    retrieved_at: str | None = None,
    limits: AdvisoryLimits = AdvisoryLimits(),
    *,
    request_page_token: str | None = None,
) -> dict:
    """Map one raw OSV advisory object for the queried Maven coordinate.

    The input is never mutated. Affected entries are filtered to the exact
    package `group:artifact` in the `Maven` ecosystem; every other entry is
    counted in `excluded_affected_count` and dropped without any claim about
    its applicability. An advisory that does not mention the queried package
    at all is refused rather than recorded as a non-match. When the advisory
    arrived on a continuation page, pass the token that was actually sent as
    `request_page_token` (keyword-only, so the original retrieved_at/limits
    positional slots keep their meaning): it is validated exactly like
    build_query's token and lands verbatim in the evidence query.
    """
    query = build_query(group_id, artifact_id, version, page_token=request_page_token, limits=limits)
    retrieved = _retrieved_at(retrieved_at, limits)
    if not isinstance(raw_advisory, dict):
        raise AdvisoryError("advisory must be an object", "advisory-not-object")

    advisory_id = _bounded_str(raw_advisory.get("id"), "advisory id", limits)
    if advisory_id == "" or advisory_id.isspace():
        raise AdvisoryError("advisory id must not be blank", "invalid-advisory-id")

    def optional_str(key: str):
        if key not in raw_advisory:
            return None
        return _bounded_str(raw_advisory[key], f"advisory {key}", limits)

    aliases = None
    if "aliases" in raw_advisory:
        raw_aliases = raw_advisory["aliases"]
        if not isinstance(raw_aliases, list):
            raise AdvisoryError("advisory aliases must be a list", "wrong-type")
        aliases = [_bounded_str(alias, "advisory alias", limits) for alias in raw_aliases]

    severity = None
    if "severity" in raw_advisory:
        raw_severity = raw_advisory["severity"]
        if not isinstance(raw_severity, list):
            raise AdvisoryError("advisory severity must be a list", "wrong-type")
        mapped_severity = []
        for entry in raw_severity:
            if not isinstance(entry, dict) or "type" not in entry or "score" not in entry:
                raise AdvisoryError("each severity entry needs type and score", "wrong-type")
            score = entry["score"]
            if not isinstance(score, str):
                _check_json_value(score, "severity score", limits)
            mapped_severity.append(
                {
                    "type": _bounded_str(entry["type"], "severity type", limits),
                    "score": score if isinstance(score, str) else copy.deepcopy(score),
                }
            )
        severity = mapped_severity

    references = None
    if "references" in raw_advisory:
        raw_references = raw_advisory["references"]
        if not isinstance(raw_references, list):
            raise AdvisoryError("advisory references must be a list", "wrong-type")
        if len(raw_references) > limits.max_references_per_advisory:
            raise AdvisoryError(
                f"advisory has {len(raw_references)} references, limit is {limits.max_references_per_advisory}",
                "too-many-references",
            )
        references = []
        for reference in raw_references:
            if not isinstance(reference, dict) or "type" not in reference or "url" not in reference:
                raise AdvisoryError("each reference needs type and url", "wrong-type")
            references.append(
                {
                    "type": _bounded_str(reference["type"], "reference type", limits),
                    "url": _bounded_str(reference["url"], "reference url", limits),
                }
            )

    affected = raw_advisory.get("affected")
    if affected is None:
        affected = []
    if not isinstance(affected, list):
        raise AdvisoryError("advisory affected must be a list", "wrong-type")

    wanted_name = f"{group_id}:{artifact_id}"
    relevant = []
    excluded = 0
    for entry in affected:
        if not isinstance(entry, dict):
            raise AdvisoryError("affected entry must be an object", "wrong-type")
        package = entry.get("package")
        if not isinstance(package, dict):
            raise AdvisoryError("affected.package must be an object", "wrong-type")
        # Only the exact queried package and ecosystem counts as relevant;
        # matching is exact and case-sensitive, as the OSV API is.
        if (
            isinstance(package.get("name"), str)
            and package.get("name") == wanted_name
            and isinstance(package.get("ecosystem"), str)
            and package.get("ecosystem") == MAVEN_ECOSYSTEM
        ):
            relevant.append(_map_relevant_affected(entry, limits))
        else:
            excluded += 1

    if not relevant:
        raise AdvisoryError(
            f"advisory {advisory_id} does not list the queried package "
            f"{wanted_name!r} in ecosystem {MAVEN_ECOSYSTEM}; refusing to "
            "record a non-match as absence of vulnerability",
            "identity-not-in-advisory",
        )
    if len(relevant) > limits.max_affected_per_advisory:
        raise AdvisoryError(
            f"advisory {advisory_id} has {len(relevant)} affected entries for "
            f"{wanted_name}, limit is {limits.max_affected_per_advisory}",
            "too-many-affected-entries",
        )

    modified = optional_str("modified")
    return {
        "id": advisory_id,
        "aliases": aliases,
        "modified": modified,
        "published": optional_str("published"),
        "summary": optional_str("summary"),
        "severity": severity,
        "references": references,
        "affected": relevant,
        "excluded_affected_count": excluded,
        "provider_reported": True,
        "evidence": {
            "id": advisory_id,
            "source_url": OSV_QUERY_ENDPOINT,
            "provider": PROVIDER_NAME,
            "query": query,
            "modified": modified,
            "retrieved_at": retrieved,
        },
    }


def map_response(
    data: bytes,
    group_id: str,
    artifact_id: str,
    version: str,
    retrieved_at: str | None = None,
    limits: AdvisoryLimits = AdvisoryLimits(),
    *,
    request_page_token: str | None = None,
) -> dict:
    """Validate a recorded POST /v1/query response and map it to a page result.

    Status is "matches" (advisories found, no continuation), "partial"
    (a next_page_token is present: the query is incomplete, even when this
    page carries no advisories), or "no-known-matches" (complete empty page).
    A page whose advisories never mention the queried package is rejected,
    never reported as a clean non-match.

    The result describes exactly ONE response page, never an aggregate:
    `page_scope` is always "single-response-page", `is_continuation` reports
    whether this page answered a token-carrying followup query
    (`request_page_token`, keyword-only so the original retrieved_at/limits
    positional slots keep their meaning), and `more_pages` reports whether
    the provider signalled further pages. An empty final continuation page
    therefore carries status "no-known-matches" about that page alone; only
    the caller, after aggregating every page, may state anything about the
    whole query.
    """
    query = build_query(group_id, artifact_id, version, page_token=request_page_token, limits=limits)
    retrieved = _retrieved_at(retrieved_at, limits)
    page = _parse_page(data, limits)

    if not isinstance(page, dict):
        raise AdvisoryError("response top level must be a JSON object", "wrong-type")
    unexpected = sorted(set(page) - OUTER_KEYS_ALLOWED)
    if unexpected:
        raise AdvisoryError(
            f"response has keys outside vulns/next_page_token: {', '.join(unexpected)}",
            "unexpected-key",
        )

    token = None
    if "next_page_token" in page:
        token = _page_token(page["next_page_token"], limits)

    vulns = page.get("vulns")
    if vulns is None:
        vulns = []
    if not isinstance(vulns, list):
        raise AdvisoryError("vulns must be a list", "wrong-type")
    if len(vulns) > limits.max_advisories:
        raise AdvisoryError(
            f"page has {len(vulns)} advisories, limit is {limits.max_advisories}",
            "too-many-advisories",
        )

    advisories = [
        map_advisory(
            raw,
            group_id,
            artifact_id,
            version,
            request_page_token=request_page_token,
            retrieved_at=retrieved,
            limits=limits,
        )
        for raw in vulns
    ]
    excluded_total = sum(advisory["excluded_affected_count"] for advisory in advisories)

    if token is not None:
        status = "partial"
    elif advisories:
        status = "matches"
    else:
        status = "no-known-matches"

    return {
        "provider": PROVIDER_NAME,
        "status": status,
        "query": query,
        "source_url": OSV_QUERY_ENDPOINT,
        "retrieved_at": retrieved,
        "provider_reported": True,
        "version_applicability": "provider-reported; not independently evaluated",
        "caveat": FUZZY_VERSION_CAVEAT,
        "advisory_fields_preserved": list(ADVISORY_FIELDS_PRESERVED),
        "page_scope": "single-response-page",
        "is_continuation": request_page_token is not None,
        "more_pages": token is not None,
        "advisories": advisories,
        "counts": {
            "returned_advisories": len(vulns),
            "mapped_advisories": len(advisories),
            "excluded_affected_entries": excluded_total,
        },
        "next_page_token": token,
    }


def transport_error_result(
    group_id: str,
    artifact_id: str,
    version: str,
    exc: BaseException,
    retrieved_at: str | None = None,
    limits: AdvisoryLimits = AdvisoryLimits(),
    *,
    request_page_token: str | None = None,
) -> dict:
    """Build the canonical result for a transport failure (raised elsewhere).

    This helper never catches or swallows exceptions; it only shapes one a
    caller already has. Its status is deliberately distinct from the three
    page statuses so a failed fetch can never be confused with an answered
    query. No response arrived, so `page_scope` is
    "attempted-single-response-page" and `more_pages` is None: pagination
    state is unknown, never reported as False. `request_page_token`
    (keyword-only) echoes the token of the followup query that failed and
    sets `is_continuation`.
    """
    return {
        "provider": PROVIDER_NAME,
        "status": "transport-error",
        "query": build_query(group_id, artifact_id, version, page_token=request_page_token, limits=limits),
        "retrieved_at": _retrieved_at(retrieved_at, limits),
        "provider_reported": False,
        "caveat": FUZZY_VERSION_CAVEAT,
        "page_scope": "attempted-single-response-page",
        "is_continuation": request_page_token is not None,
        "more_pages": None,
        "error_type": type(exc).__name__,
        "error": str(exc)[: limits.max_string_length],
        "advisories": [],
    }

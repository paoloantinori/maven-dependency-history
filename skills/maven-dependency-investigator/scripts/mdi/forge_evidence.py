"""Pure import of caller-exported forge discussions; no retrieval or execution.

The internal schema is ``mdi.forge-evidence/1``. All keys are required; unknown
keys are refused. This is not a parser for a provider's native API response.

* schema, origin, repository, retrieval, coverage, records
* origin: HTTPS origin only (no path except '/', credentials, query or fragment)
* repository: nonempty opaque identifier, or null when unknown
* retrieval: tool, version, retrieved_at (nullable text/offset-aware timestamp)
* coverage: records, review_bodies, inline_comments; each exactly complete,
  partial, unknown or not-applicable. Completeness is caller-attested only.
* records: list of id, kind, provider_id, author, created_at, updated_at, body,
  parent_id, thread_id, source_refs, source_locator
* kind: pull, issue, commit, review, inline-comment
* author: login (nullable text), role (author, committer, reviewer, commenter,
  bot or unknown). Roles are recorded labels, never authority or inferred intent.
* timestamps: nullable, timezone-aware ISO, maximum six fractional digits
* body: text including empty text, or null; preserved verbatim as untrusted quote
* parent_id: null or another record id; parent cycles are refused
* thread_id: nullable opaque provider thread identifier, not a record reference
* source_refs: unique ids of other records in this export, not URLs to fetch
* source_locator: same-origin HTTPS URL, optional fragment, no query/credentials

A source locator is caller-supplied attribution, not authenticated evidence that
text appeared at that URL. Unknown parent/source references are rejected: omit
unexported links as null/[] and mark coverage partial rather than invent records.
URL validation excludes credentials, query strings and control characters; it
is not a secret detector for caller-supplied paths, fragments or quoted bodies.
The origin check establishes host/port consistency, not repository membership.
No author or bot filtering occurs. Conflicting bodies remain separate records;
this module does not infer which claim is correct or why a dependency exists.
Explicit complete export coverage does not prove retrieval was comprehensive;
empty bodies and exports remain evidence gaps. No run-global IDs are assigned.

The exact input bytes are capped before parsing and hashed. A narrow bridge to
the existing runtime parser reuses its strict JSON, lexical/decoded size guards
and timestamp rules; errors are translated to this module's stable error type.
"""

import hashlib
from dataclasses import dataclass, fields
from urllib.parse import urlsplit

from mdi.runtime_evidence import RuntimeEvidenceError, _parse, _timestamp


SCHEMA = 'mdi.forge-evidence/1'
_KINDS = frozenset(('pull', 'issue', 'commit', 'review', 'inline-comment'))
_ROLES = frozenset(('author', 'committer', 'reviewer', 'commenter', 'bot', 'unknown'))
_COVERAGE = frozenset(('complete', 'partial', 'unknown', 'not-applicable'))
_LIMITATIONS = (
    'exported records, source URLs, roles and retrieval coverage are caller-attested, not independently authenticated',
    'record bodies are untrusted quotations, never instructions or inferred technical or business intent',
    'conflicting recorded rationales remain separate claims; none is selected as authoritative',
    'missing records or rationale do not prove there was no discussion or reason',
    'complete coverage means explicit exporter attestation within this export, not verified provider completeness',
    'record identifiers are export-local; the coordinator must assign separate run-local evidence identifiers',
)


class ForgeEvidenceError(ValueError):
    """Rejected export with a stable code; messages never quote body content."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ForgeLimits:
    max_input_bytes: int = 8 * 1024 * 1024
    max_records: int = 10000
    max_string_length: int = 4096
    max_depth: int = 16
    max_nodes: int = 500000

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int:
                raise TypeError(f'{field.name} must be an integer')
            if value <= 0:
                raise ValueError(f'{field.name} must be positive')


def _fail(code, message):
    raise ForgeEvidenceError(message, code)


def _object(value, keys, where):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        _fail('schema', f'{where} has invalid fields')


def _text(value, where, unknown, *, nullable=False):
    if nullable and value is None:
        unknown.append(where)
        return
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        _fail('schema', f'{where} must be nonempty text' + (' or null' if nullable else ''))


def _choice(value, choices, where):
    if not isinstance(value, str) or value not in choices:
        _fail('schema', f'{where} has an unsupported value')


def _url(value, *, origin=False):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail('invalid-url', 'source URLs must be credential-free HTTPS URLs')
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme != 'https' or not parts.hostname
                or parts.username is not None or parts.password is not None
                or '?' in value or origin and ('#' in value or parts.path not in ('', '/'))):
            raise ValueError()
    except (ValueError, UnicodeError):
        _fail('invalid-url', 'source URLs must be credential-free HTTPS URLs')
    return parts.hostname.lower(), port if port is not None else 443


def _validate_parents(records, by_id):
    # Iterative coloring keeps a long exported thread bounded without recursion.
    finished = set()
    for record in records:
        seen = set()
        current = record['id']
        while current is not None and current not in finished:
            if current in seen:
                _fail('parent-cycle', 'record parent references form a cycle')
            seen.add(current)
            current = by_id[current]['parent_id']
        finished.update(seen)


def _validate(doc, limits):
    _object(doc, 'schema origin repository retrieval coverage records', 'export')
    if doc['schema'] != SCHEMA:
        _fail('schema', 'unsupported forge export schema')
    origin = _url(doc['origin'], origin=True)
    unknown, issues = [], []
    _text(doc['repository'], 'repository', unknown, nullable=True)
    _object(doc['retrieval'], 'tool version retrieved_at', 'retrieval')
    for name in ('tool', 'version'):
        _text(doc['retrieval'][name], f'retrieval.{name}', unknown, nullable=True)
    retrieved = _timestamp(doc['retrieval']['retrieved_at'], 'retrieval.retrieved_at', unknown)
    _object(doc['coverage'], 'records review_bodies inline_comments', 'coverage')
    for name, state in doc['coverage'].items():
        _choice(state, _COVERAGE, f'coverage.{name}')
        if state != 'complete':
            unknown.append(f'coverage.{name}')
    records = doc['records']
    if not isinstance(records, list):
        _fail('schema', 'records must be a list')
    if len(records) > limits.max_records:
        _fail('record-limit', 'export exceeds the record count bound')
    if not records:
        issues.append({'code': 'no-records'})
    by_id = {}
    for index, record in enumerate(records):
        where = f'records[{index}]'
        _object(record, 'id kind provider_id author created_at updated_at body parent_id thread_id source_refs source_locator', where)
        for name in ('id', 'provider_id', 'source_locator'):
            _text(record[name], f'{where}.{name}', unknown)
        if record['id'] in by_id:
            _fail('duplicate-id', 'export record identifiers must be unique')
        by_id[record['id']] = record
        _choice(record['kind'], _KINDS, f'{where}.kind')
        if _url(record['source_locator']) != origin:
            _fail('source-origin-mismatch', 'record URL differs from the export origin')
        _object(record['author'], 'login role', f'{where}.author')
        _text(record['author']['login'], f'{where}.author.login', unknown, nullable=True)
        _choice(record['author']['role'], _ROLES, f'{where}.author.role')
        if record['author']['role'] == 'unknown':
            unknown.append(f'{where}.author.role')
        created = _timestamp(record['created_at'], f'{where}.created_at', unknown)
        updated = _timestamp(record['updated_at'], f'{where}.updated_at', unknown)
        if created is not None and updated is not None and updated < created:
            _fail('timestamp-order', 'record update precedes creation')
        if retrieved is not None and any(at is not None and at > retrieved for at in (created, updated)):
            _fail('timestamp-order', 'record timestamp is after the stated retrieval')
        body = record['body']
        if body is not None and not isinstance(body, str):
            _fail('schema', 'record body must be text or null')
        if body is None or not body.strip():
            issues.append({'code': 'rationale-unavailable', 'record_id': record['id']})
        for name in ('parent_id', 'thread_id'):
            if record[name] is not None:
                _text(record[name], f'{where}.{name}', unknown)
        refs = record['source_refs']
        if not isinstance(refs, list):
            _fail('invalid-reference', 'source_refs must be a list of local record ids')
        seen = set()
        for ref in refs:
            _text(ref, f'{where}.source_refs', unknown)
            if ref in seen:
                _fail('invalid-reference', 'source_refs must not repeat')
            seen.add(ref)
    for record in records:
        refs = record['source_refs']
        parent = record['parent_id']
        if parent is not None and (parent not in by_id or parent == record['id']):
            _fail('invalid-reference', 'parent_id must name another exported record')
        if any(ref not in by_id or ref == record['id'] for ref in refs):
            _fail('invalid-reference', 'source_refs must name other exported records')
    _validate_parents(records, by_id)
    return unknown, issues


def import_forge_evidence(data, *, limits=ForgeLimits()):
    """Import exact captured UTF-8 bytes as quoted, caller-attested records.

    This function never reads URLs or local paths. All output objects originate
    from parsing these bytes; there is no alias to a caller-owned input object.
    """
    if not isinstance(limits, ForgeLimits):
        raise ForgeEvidenceError('limits must be ForgeLimits', 'invalid-limits')
    try:
        doc = _parse(data, limits)
        unknown, issues = _validate(doc, limits)
    except RuntimeEvidenceError as exc:
        raise ForgeEvidenceError(f'forge export rejected: {exc.code}', exc.code) from exc
    if unknown:
        issues.append({'code': 'provenance-or-coverage-unknown'})
    return {
        'schema': SCHEMA,
        'input': {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
        'origin': doc['origin'], 'repository': doc['repository'],
        'retrieval': doc['retrieval'], 'records': doc['records'],
        'coverage': {
            'status': 'partial' if issues else 'complete-within-export',
            'record_count': len(doc['records']), 'unknown_fields': unknown,
            'exporter_attestation': doc['coverage'],
        },
        'issues': issues,
        'provenance_attestation': 'caller-attested',
        'statement_kind': 'recorded-quotation', 'intent_inferred': False,
        'limitations': list(_LIMITATIONS),
    }

"""Pure import of caller-supplied runtime observations; no instrumentation or IO.

The internal input schema is ``mdi.runtime-observations/1``. All object keys
below are required (nullable provenance is explicit); unknown keys are refused:

* schema, observer, workload, environment, window, scope, observations
* observer: tool, version, format, capture_method (nullable strings)
* workload: id, description (nullable strings)
* environment: summary, fingerprint (nullable strings), unknowns (string list)
* window: start, end (nullable timezone-aware ISO timestamps, at most six
  fractional-second digits; offset hours 00..23 and minutes 00..59)
* scope: production or test
* observations: objects with id, kind, mechanism, observed_at, source_locator,
  subject. The timestamp is nullable; id and locator are nonempty strings.
* subject: module (nullable string), realm (project/build-plugin/unknown),
  coordinates (null or exactly groupId/artifactId/version, each nullable),
  class_name and method_name (nullable strings).

Kinds describe claims: used, loaded, invoked, not-observed-in-workload.
Mechanisms describe the observer's capture category: class-loading,
service-loading, reflection, native, other. Neither vocabulary independently
verifies execution. Provenance, timestamps, fingerprints and coordinates are
caller-attested; a fingerprint is opaque, not verified against any environment.
Missing provenance and subject identity produce partial coverage. Even complete
coverage means only well-described recorded claims within that workload.

Source locators and text are retained as untrusted data, never fetched, parsed
as code/XML or used as commands. The returned input digest identifies exactly
the imported bytes, not their authenticity. No process, file or network APIs
are called. The summary API only returns workload-local observation summaries;
it has no absence, removal, reachability or exploitability inference mode.
"""

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, fields
from datetime import datetime


SCHEMA = 'mdi.runtime-observations/1'
_KINDS = frozenset(('used', 'loaded', 'invoked', 'not-observed-in-workload'))
_MECHANISMS = frozenset(('class-loading', 'service-loading', 'reflection', 'native', 'other'))
_REALMS = frozenset(('project', 'build-plugin', 'unknown'))
_LIMITATIONS = (
    'observations and provenance are caller-attested, not independently authenticated',
    'claims apply only to the recorded workload and observation window',
    'missing observations do not prove global non-use or safe removal',
    'runtime claims do not prove vulnerable-function reachability or exploitability',
    'static framework configuration must not be promoted to runtime execution evidence',
)
_NOT_OBSERVED = 'not observed in this workload does not prove absence or safe removal'
_TIMESTAMP = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
    r'(?:\.(?P<fraction>[0-9]+))?'
    r'(?:Z|[+-](?P<offset_hour>[0-9]{2}):(?P<offset_minute>[0-9]{2}))'
)


class RuntimeEvidenceError(ValueError):
    """Rejected recorded evidence, with a stable machine-readable code."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RuntimeLimits:
    max_input_bytes: int = 8 * 1024 * 1024
    max_observations: int = 10000
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
    raise RuntimeEvidenceError(message, code)


def _lexical_bounds(data, limits):
    # JSON structural delimiters inside strings must not contribute to nesting.
    depth, in_string, escaped, string_bytes = 0, False, False, 0
    for byte in data:
        if in_string:
            string_bytes += 1
            # A supplementary Unicode scalar can be two six-byte JSON escapes.
            # The closing quote contributes one byte; decoded length is checked later.
            if string_bytes > limits.max_string_length * 12 + 1:
                _fail('string-limit', 'JSON string exceeds the encoded string bound')
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
        elif byte == 34:
            in_string, string_bytes = True, 0
        elif byte in (91, 123):
            depth += 1
            if depth > limits.max_depth:
                _fail('depth-limit', 'JSON nesting exceeds the depth bound')
        elif byte in (93, 125):
            depth -= 1
    # Grammar, balance and encoding errors are handled by the strict parser.


def _json_bounds(value, limits):
    stack = [(iter((value,)), 0)]
    count = 0
    while stack:
        iterator, depth = stack[-1]
        try:
            node = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        count += 1
        if count > limits.max_nodes:
            _fail('node-limit', 'JSON exceeds the node bound')
        if depth > limits.max_depth:
            _fail('depth-limit', 'JSON nesting exceeds the depth bound')
        if isinstance(node, str):
            if len(node) > limits.max_string_length:
                _fail('string-limit', 'JSON string exceeds the character bound')
            if any(0xD800 <= ord(char) <= 0xDFFF for char in node):
                _fail('invalid-unicode', 'JSON contains an unpaired Unicode surrogate')
        elif isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                _fail('schema', 'JSON object keys must be strings')
            stack.append((iter(node.values()), depth + 1))
            stack.append((iter(node.keys()), depth + 1))
        elif isinstance(node, list):
            stack.append((iter(node), depth + 1))
        elif node is None or type(node) in (bool, int):
            continue
        elif type(node) is float:
            if not math.isfinite(node):
                _fail('non-finite', 'JSON contains a non-finite number')
        else:
            _fail('schema', 'value is not JSON-safe')


def _parse(data, limits):
    if not isinstance(data, bytes):
        _fail('input-type', 'runtime input must be captured bytes')
    if len(data) > limits.max_input_bytes:
        _fail('input-too-large', 'runtime input exceeds the byte bound')
    _lexical_bounds(data, limits)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail('duplicate-key', 'duplicate JSON object key')
            result[key] = value
        return result

    def integer(text):
        if len(text.lstrip('-')) > limits.max_string_length:
            _fail('integer-limit', 'JSON integer exceeds the digit bound')
        try:
            return int(text)
        except ValueError:
            _fail('integer-limit', 'JSON integer exceeds the interpreter bound')

    def floating(text):
        value = float(text)
        if not math.isfinite(value):
            _fail('non-finite', 'non-finite JSON number')
        return value

    def constant(_text):
        _fail('non-finite', 'non-finite JSON constant')

    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        _fail('invalid-utf8', 'runtime input is not UTF-8')
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant,
                           parse_int=integer, parse_float=floating)
    except RuntimeEvidenceError:
        raise
    except RecursionError:
        _fail('depth-limit', 'JSON exceeds parser nesting support')
    except (ValueError, OverflowError):
        _fail('malformed-json', 'runtime input is not valid JSON')
    _json_bounds(value, limits)
    return value


def _object(value, keys, where):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        _fail('schema', f'{where} requires exactly: {keys}')


def _text(value, where, unknown, nullable=False):
    if nullable and value is None:
        unknown.append(where)
        return
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        _fail('schema', f'{where} must be a nonempty string' + (' or null' if nullable else ''))


def _timestamp(value, where, unknown):
    _text(value, where, unknown, nullable=True)
    if value is None:
        return None
    matched = _TIMESTAMP.fullmatch(value)
    if matched is None:
        _fail('invalid-timestamp', f'{where} must be a timezone-aware ISO timestamp')
    if len(matched.group('fraction') or '') > 6:
        _fail('unsupported-timestamp-precision', f'{where} exceeds microsecond precision')
    if matched.group('offset_hour') is not None and (
        int(matched.group('offset_hour')) > 23 or int(matched.group('offset_minute')) > 59
    ):
        _fail('invalid-timestamp', f'{where} has an invalid UTC offset')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError()
        return parsed
    except (ValueError, OverflowError):
        _fail('invalid-timestamp', f'{where} is not a valid timestamp')


def _choice(value, allowed, where):
    if not isinstance(value, str) or value not in allowed:
        _fail('schema', f'{where} has an unsupported value')


def _validate(doc, limits):
    unknown = []
    _object(doc, 'schema observer workload environment window scope observations', 'document')
    if doc['schema'] != SCHEMA:
        _fail('schema', 'unsupported runtime observation schema')
    for name, keys in (('observer', 'tool version format capture_method'),
                       ('workload', 'id description')):
        _object(doc[name], keys, name)
        for key in keys.split():
            _text(doc[name][key], f'{name}.{key}', unknown, nullable=True)
    _object(doc['environment'], 'summary fingerprint unknowns', 'environment')
    for key in ('summary', 'fingerprint'):
        _text(doc['environment'][key], f'environment.{key}', unknown, nullable=True)
    items = doc['environment']['unknowns']
    if not isinstance(items, list):
        _fail('schema', 'environment.unknowns must be a list')
    for value in items:
        _text(value, 'environment.unknowns entry', unknown)
    if items:
        unknown.append('environment.unknowns')
    _object(doc['window'], 'start end', 'window')
    start = _timestamp(doc['window']['start'], 'window.start', unknown)
    end = _timestamp(doc['window']['end'], 'window.end', unknown)
    if start is not None and end is not None and end < start:
        _fail('invalid-window', 'observation window ends before it starts')
    _choice(doc['scope'], ('production', 'test'), 'scope')
    observations = doc['observations']
    if not isinstance(observations, list):
        _fail('schema', 'observations must be a list')
    if len(observations) > limits.max_observations:
        _fail('observation-limit', 'too many runtime observations')
    seen = set()
    for index, observation in enumerate(observations):
        where = f'observations[{index}]'
        _object(observation, 'id kind mechanism observed_at source_locator subject', where)
        for key in ('id', 'source_locator'):
            _text(observation[key], f'{where}.{key}', unknown)
        if observation['id'] in seen:
            _fail('duplicate-observation-id', 'duplicate runtime observation ID')
        seen.add(observation['id'])
        _choice(observation['kind'], _KINDS, f'{where}.kind')
        _choice(observation['mechanism'], _MECHANISMS, f'{where}.mechanism')
        at = _timestamp(observation['observed_at'], f'{where}.observed_at', unknown)
        if at is not None and ((start is not None and at < start) or (end is not None and at > end)):
            _fail('outside-window', f'{where}.observed_at lies outside the recorded window')
        subject = observation['subject']
        _object(subject, 'module realm coordinates class_name method_name', f'{where}.subject')
        _choice(subject['realm'], _REALMS, f'{where}.subject.realm')
        if subject['realm'] == 'unknown':
            unknown.append(f'{where}.subject.realm')
        for key in ('module', 'class_name', 'method_name'):
            # Class-level events need no method identity; invocations do.
            if key == 'method_name' and subject[key] is None and observation['kind'] != 'invoked':
                continue
            _text(subject[key], f'{where}.subject.{key}', unknown, nullable=True)
        coords = subject['coordinates']
        if coords is None:
            unknown.append(f'{where}.subject.coordinates')
        else:
            _object(coords, 'groupId artifactId version', f'{where}.subject.coordinates')
            for key, value in coords.items():
                field = f'{where}.subject.coordinates.{key}'
                _text(value, field, unknown, nullable=True)
                if value is not None and ('${' in value or any(char.isspace() for char in value)):
                    unknown.append(field)
    return unknown


def import_runtime_observations(data, *, limits=RuntimeLimits()):
    """Import exact recorded bytes; return evidence payload and explicit coverage.

    No evidence IDs belonging to a report are assigned here. Observation IDs are
    local to this imported document and never references to another run.
    """
    if not isinstance(limits, RuntimeLimits):
        _fail('limits-type', 'limits must be RuntimeLimits')
    doc = _parse(data, limits)
    unknown = _validate(doc, limits)
    issues = []
    if unknown:
        issues.append({'code': 'unknown-provenance-or-identity', 'fields': unknown})
    if not doc['observations']:
        issues.append({'code': 'no-observations'})
    observations = [
        {'claim': copy.deepcopy(claim),
         'limitations': list(_LIMITATIONS) + ([_NOT_OBSERVED] if claim['kind'] == 'not-observed-in-workload' else [])}
        for claim in doc['observations']
    ]
    return {
        'kind': 'runtime-observation', 'schema': SCHEMA,
        'input': {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
        'provenance_attestation': 'caller-attested', 'independently_authenticated': False,
        'document': doc, 'observations': observations,
        'safe_removal_proven': False, 'global_nonuse_proven': False,
        'issues': issues,
        'coverage': {'status': 'partial' if issues else 'complete-within-recorded-workload',
                     'observation_count': len(observations), 'unknown_fields': list(unknown),
                     'limitations': list(_LIMITATIONS)},
    }


def summarize_observations(imported, observation_ids, *, statement_kind='observation-summary',
                           limits=RuntimeLimits()):
    """Build a fixed workload-local summary; unsupported inference modes refuse.

    This returns observation IDs, not report evidence IDs. The report coordinator
    must bind the summary to the actual imported evidence in the current run.
    """
    if statement_kind != 'observation-summary':
        _fail('unsupported-inference', 'runtime observations cannot establish absence, removal or exploitability')
    if not isinstance(limits, RuntimeLimits):
        _fail('limits-type', 'limits must be RuntimeLimits')
    if not isinstance(imported, dict) or not isinstance(imported.get('document'), dict):
        _fail('schema', 'summary requires an imported runtime document')
    doc = imported['document']
    _json_bounds(doc, limits)
    unknown = _validate(doc, limits)
    if not isinstance(observation_ids, list) or not observation_ids or len(observation_ids) > limits.max_observations:
        _fail('observation-selection', 'select a bounded nonempty list of observation IDs')
    if any(not isinstance(item, str) or len(item) > limits.max_string_length for item in observation_ids):
        _fail('observation-selection', 'selected observation IDs must be bounded strings')
    if len(set(observation_ids)) != len(observation_ids):
        _fail('observation-selection', 'selected observation IDs must be unique')
    by_id = {item['id']: item for item in doc['observations']}
    if any(item not in by_id for item in observation_ids):
        _fail('observation-selection', 'selected observation ID is unknown in this document')
    claims = [copy.deepcopy(by_id[item]) for item in observation_ids]
    limitations = list(_LIMITATIONS)
    if any(item['kind'] == 'not-observed-in-workload' for item in claims):
        limitations.append(_NOT_OBSERVED)
    return {'statement_kind': 'observation-summary', 'observation_ids': list(observation_ids),
            'workload': copy.deepcopy(doc['workload']), 'window': copy.deepcopy(doc['window']),
            'scope': doc['scope'], 'claims': claims, 'limitations': limitations,
            'unknown_fields': unknown}

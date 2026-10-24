"""Pure, bounded comparison of recorded Maven tree evidence within one run.

Input snapshots are report evidence records whose payload.import is the result
of mdi.maven.import_dependency_tree. Node inclusion must be explicitly
nonverbose/included-per-report; diagnostic snapshots remain gaps, not empty
graphs. Producer and build provenance remain caller-attested or unknown.

Ancestry edges name snapshot evidence IDs, carry verified=True and nonempty
proof evidence IDs from the caller's run-local evidence set. This function
checks bindings and graph order, not Git: only a trusted coordinator that has
actually verified ancestry may supply them. Timestamps are an explicit
alternative ordering, always labeled temporal-only. Neither revision hashes,
input list order nor snapshot filenames establish chronology. Timestamp input
uses ASCII date/time fields, UTC offsets with hours at most 23 and minutes at
most 59, and at most six fractional digits; invalid values stay unordered
rather than being normalized or rounded. Unknown module/realm snapshots that
could belong to a group stay in its order as barriers and reset comparison
history; positively different contexts remain separate. Only uniquely
ordered adjacent supplied snapshots are compared; unobserved intervals are
never reconstructed as exact introduction dates or shipped-artifact facts.

Node paths are reconstructed from validated parent links. Path positions and
ancestor versions are excluded from matching identity, so reordering and an
introducing dependency upgrade do not manufacture dependency removals. Every
occurrence remains in a multiset; type, classifier, scope, module and realm
remain distinct. Optional identity uses validated semantic booleans, not raw
producer spelling; emitted events retain both raw and parsed observations.
Unknown optional states make a snapshot a qualified comparison gap, not an
empty graph. A unique residual version pair yields a version change;
multiple possible pairs yield separate additions/removals with ambiguity.
"""

import copy
import json
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, fields
from datetime import datetime


class GraphHistoryError(ValueError):
    def __init__(self, message, code='invalid-input'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HistoryLimits:
    max_snapshots: int = 128
    max_nodes: int = 10000
    max_json_nodes: int = 1000000
    max_depth: int = 128
    max_input_bytes: int = 16 * 1024 * 1024
    max_output_bytes: int = 24 * 1024 * 1024
    max_text: int = 4096
    max_events: int = 20000

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise GraphHistoryError(f'{field.name} must be a positive integer', 'invalid-limit')


_LIMITATIONS = [
    'snapshots describe recorded producer reports, not verified historical builds or shipped artifacts',
    'unobserved intervals between supplied snapshots remain gaps; no exact original introduction date is proved',
    'ancestry proof references are supplied by the trusted caller; this pure comparator performs no Git verification',
    'timestamps provide temporal ordering only, never causal ancestry',
    'provenance and context differences qualify every derived event; no present-checkout resolution is claimed',
]
_CONTEXT = ('profiles', 'jdk', 'maven', 'settings_digest', 'dirty', 'working_tree_digest')


def _text(value, label, limits, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > limits.max_text or '\x00' in value:
        raise GraphHistoryError(f'{label} must be bounded nonempty text')


def _bounded_json(value, limits, byte_limit):
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > limits.max_json_nodes or depth > limits.max_depth:
            raise GraphHistoryError('JSON structure exceeds limits', 'structure-limit')
        if isinstance(item, dict):
            if len(item) > limits.max_json_nodes - count:
                raise GraphHistoryError('JSON structure exceeds limits', 'structure-limit')
            for key, child in item.items():
                _text(key, 'object key', limits)
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > limits.max_json_nodes - count:
                raise GraphHistoryError('JSON structure exceeds limits', 'structure-limit')
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > limits.max_text:
                raise GraphHistoryError('JSON string exceeds limits', 'text-limit')
        elif item is not None and type(item) not in (bool, int, float):
            raise GraphHistoryError('input is not JSON-safe')
        elif isinstance(item, float) and not math.isfinite(item):
            raise GraphHistoryError('nonfinite JSON number')
    size = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(value):
            size += len(chunk)
            if size > byte_limit:
                raise GraphHistoryError('serialized data exceeds byte limit', 'byte-limit')
    except (RecursionError, ValueError) as exc:
        if isinstance(exc, GraphHistoryError):
            raise
        raise GraphHistoryError('data cannot be serialized', 'invalid-json') from exc
    return size


def _identity(node):
    coords = node['coordinates']
    return (coords['groupId'], coords['artifactId'], node['type'], node['classifier'])


def _optional_state(node):
    optional = node.get('optional')
    if not isinstance(optional, dict) or set(optional) != {'raw', 'parsed'}:
        raise GraphHistoryError('optional must contain exactly raw and parsed')
    raw, parsed = optional['raw'], optional['parsed']
    if type(raw) not in (str, bool) or (parsed is not None and type(parsed) is not bool):
        raise GraphHistoryError('optional raw/parsed shape invalid')
    expected = raw if type(raw) is bool else {'true': True, 'false': False}.get(raw)
    if parsed is not expected:
        raise GraphHistoryError('optional parsed value disagrees with raw spelling')
    return parsed


def _graph(imported, limits):
    evidence = imported.get('evidence')
    if not isinstance(evidence, dict) or not isinstance(evidence.get('nodes'), list):
        raise GraphHistoryError('tree evidence needs a node list')
    nodes = evidence['nodes']
    if not nodes or len(nodes) > limits.max_nodes:
        raise GraphHistoryError('tree node count outside limits', 'node-limit')
    indexed = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise GraphHistoryError('tree node must be an object')
        _text(node.get('id'), 'node id', limits)
        if node['id'] in indexed:
            raise GraphHistoryError('duplicate node id')
        coords = node.get('coordinates')
        if not isinstance(coords, dict) or set(coords) != {'groupId', 'artifactId', 'version'}:
            raise GraphHistoryError('node coordinates require exactly groupId, artifactId and version')
        for key in ('groupId', 'artifactId', 'version'):
            _text(coords.get(key), 'coordinate', limits)
        for key in ('type', 'scope', 'classifier'):
            if not isinstance(node.get(key), str):
                raise GraphHistoryError('node type/scope/classifier must be strings')
        if not isinstance(node.get('children'), list) or not isinstance(node.get('inclusion'), dict):
            raise GraphHistoryError('node children/inclusion shape invalid')
        _optional_state(node)
        indexed[node['id']] = node
    root = evidence.get('root')
    if not isinstance(root, str) or root not in indexed or indexed[root].get('parent') is not None:
        raise GraphHistoryError('tree root missing or parented')
    parents = defaultdict(list)
    for nid, node in indexed.items():
        if nid == root:
            continue
        parent = node.get('parent')
        if not isinstance(parent, str) or parent not in indexed or parent == nid:
            raise GraphHistoryError('unknown or invalid node parent')
        parents[parent].append(nid)
    for nid, node in indexed.items():
        children = node['children']
        if any(not isinstance(c, str) for c in children) or len(set(children)) != len(children) \
                or set(children) != set(parents[nid]):
            raise GraphHistoryError('child references disagree with parent links')
    paths = {root: []}
    queue = deque([root])
    while queue:
        nid = queue.popleft()
        for child in parents[nid]:
            path = paths[nid] + [child]
            if len(path) > limits.max_depth:
                raise GraphHistoryError('tree path too deep', 'structure-limit')
            paths[child] = path
            queue.append(child)
    if len(paths) != len(nodes):
        raise GraphHistoryError('tree contains disconnected cycle')
    return indexed, paths, root


def _snapshot(record, limits):
    if not isinstance(record, dict):
        raise GraphHistoryError('snapshot evidence must be an object')
    _text(record.get('id'), 'snapshot id', limits)
    payload = record.get('payload')
    imported = payload.get('import') if isinstance(payload, dict) else None
    if not isinstance(imported, dict) or imported.get('kind') != 'dependency-tree':
        raise GraphHistoryError('snapshot must wrap a dependency-tree import')
    provenance = imported.get('provenance')
    if not isinstance(provenance, dict):
        raise GraphHistoryError('snapshot lacks provenance object')
    for key in ('revision', 'module', 'generated_at'):
        _text(provenance.get(key), key, limits, nullable=True)
    profiles = provenance.get('profiles')
    if profiles is not None and (not isinstance(profiles, list)
                                or any(not isinstance(p, str) or not p for p in profiles)):
        raise GraphHistoryError('profiles must be a string list or unknown')
    if provenance.get('dirty') is not None and type(provenance['dirty']) is not bool:
        raise GraphHistoryError('dirty must be bool or unknown')
    realm = imported.get('subject_realm')
    if realm not in ('project', 'build-plugin', 'unknown'):
        raise GraphHistoryError('invalid snapshot realm')
    nodes, paths, root = _graph(imported, limits)
    usable = imported.get('mode') == 'nonverbose' and imported.get('inclusion_authority') == 'recorded-context'
    usable = usable and all(n['inclusion'].get('status') == 'included-per-report'
                            and all('${' not in value for value in n['coordinates'].values())
                            for n in nodes.values())
    return dict(id=record['id'], provenance=provenance, realm=realm,
                module=provenance.get('module'), nodes=nodes, paths=paths, root=root,
                usable=usable, attestation=payload.get('provenance_attestation', 'unknown'),
                assumed_grammar=payload.get('assumed_grammar'))


def _query(query, limits):
    _text(query, 'query', limits)
    parts = query.split(':')
    if len(parts) not in (1, 2, 3) or any(not part or '${' in part for part in parts):
        raise GraphHistoryError('query must be artifactId or groupId:artifactId[:version]')
    return parts


def _matches(coords, query):
    if len(query) == 1:
        return coords['artifactId'] == query[0]
    return coords['groupId'] == query[0] and coords['artifactId'] == query[1] \
        and (len(query) == 2 or coords['version'] == query[2])


def _inventory(snapshot, query):
    buckets = defaultdict(lambda: defaultdict(list))
    for nid, path in snapshot['paths'].items():
        if nid == snapshot['root']:
            continue
        node = snapshot['nodes'][nid]
        if not _matches(node['coordinates'], query):
            continue
        members = [snapshot['nodes'][p] for p in path]
        key = (tuple(_identity(n) for n in members), node['scope'],
               _optional_state(node))
        buckets[key][node['coordinates']['version']].append(nid)
    return buckets


def _ancestry_order(group, edges, issues):
    ids = {s['id'] for s in group}
    links = {sid: set() for sid in ids}
    indegree = dict.fromkeys(ids, 0)
    proofs = {}
    for e in edges:
        before, after = e['before'], e['after']
        if before in ids and after in ids:
            if after not in links[before]:
                links[before].add(after)
                indegree[after] += 1
            proofs[before, after] = e['evidence_ids']
    order = []
    while len(order) < len(ids):
        ready = [sid for sid, degree in indegree.items() if degree == 0 and sid not in order]
        if not ready:
            raise GraphHistoryError('ancestry graph has a cycle', 'ancestry-cycle')
        if len(ready) != 1:
            issues.append({'code': 'ambiguous-order', 'evidence_ids': sorted(ids)})
            return None, None
        sid = ready[0]
        order.append(sid)
        for following in links[sid]:
            indegree[following] -= 1
    by_id = {s['id']: s for s in group}
    return [by_id[s] for s in order], proofs


_TIMESTAMP = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
    r'(?:\.[0-9]{1,6})?'
    r'(?:Z|[+-](?P<offset_hour>[0-9]{2}):(?P<offset_minute>[0-9]{2}))'
)


def _timestamp_order(group, issues):
    stamped = []
    for snapshot in group:
        value = snapshot['provenance'].get('generated_at')
        try:
            matched = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
            if matched is None:
                raise ValueError('invalid timestamp syntax or precision')
            hour, minute = matched.group('offset_hour', 'offset_minute')
            if hour is not None and (int(hour) > 23 or int(minute) > 59):
                raise ValueError('invalid UTC offset')
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.utcoffset() is None:
                raise ValueError('missing timezone')
        except ValueError:
            issues.append({'code': 'missing-or-invalid-timestamp', 'evidence_ids': [snapshot['id']]})
            return None
        stamped.append((stamp, snapshot))
    if len({stamp for stamp, _ in stamped}) != len(stamped):
        issues.append({'code': 'ambiguous-order', 'evidence_ids': [s['id'] for s in group]})
        return None
    return [s for _, s in sorted(stamped, key=lambda pair: pair[0])]


def _diff(before, after, query, seen, ordering, result, limits):
    old, new = _inventory(before, query), _inventory(after, query)
    unknown = [field for field in _CONTEXT if before['provenance'].get(field) is None
               or after['provenance'].get(field) is None]
    changed = [field for field in _CONTEXT if field not in unknown
               and (type(before['provenance'][field]) is not type(after['provenance'][field])
                    or before['provenance'][field] != after['provenance'][field])]
    attestations = [dict(evidence_id=s['id'], attestation=s['attestation'],
                        assumed_grammar=s['assumed_grammar']) for s in (before, after)]
    if unknown or changed:
        result['issues'].append({'code': 'context-incomplete-or-changed',
                                 'evidence_ids': [before['id'], after['id']],
                                 'unknown_fields': unknown, 'changed_fields': changed})

    def event(kind, key, version, count, old_ids, new_ids, other=None):
        if len(result['events']) >= limits.max_events:
            raise GraphHistoryError('event count exceeds limit', 'event-limit')
        snap, ids = (after, new_ids) if new_ids else (before, old_ids)
        path = [dict(snap['nodes'][nid]['coordinates'], type=snap['nodes'][nid]['type'],
                     classifier=snap['nodes'][nid]['classifier']) for nid in snap['paths'][ids[0]]]
        record = dict(kind=kind, evidence_ids=[before['id'], after['id']],
            statement_kind='inference', ordering=ordering, realm=before['realm'], module=before['module'],
            path=path, scope=key[1], count=count, before_count=sum(map(len, old.get(key, {}).values())),
            after_count=sum(map(len, new.get(key, {}).values())), before_node_ids=old_ids,
            after_node_ids=new_ids, before_version=version if old_ids else None,
            after_version=(other if other is not None else version) if new_ids else None,
            context_unknown_fields=unknown, context_changed_fields=changed,
            provenance_attestations=attestations,
            optional_observations={
                'before': [dict(node_id=nid, **before['nodes'][nid]['optional']) for nid in old_ids],
                'after': [dict(node_id=nid, **after['nodes'][nid]['optional']) for nid in new_ids],
            }, limitations=list(_LIMITATIONS))
        result['_event_bytes'] += _bounded_json(record, limits, limits.max_output_bytes)
        if result['_event_bytes'] > limits.max_output_bytes:
            raise GraphHistoryError('events exceed output byte limit', 'byte-limit')
        result['events'].append(record)

    for key in sorted(set(old) | set(new)):
        ov, nv = old.get(key, {}), new.get(key, {})
        removed, added = {}, {}
        for version in sorted(set(ov) | set(nv)):
            a, b = ov.get(version, []), nv.get(version, [])
            common = min(len(a), len(b))
            if len(a) > common:
                removed[version] = (len(a) - common, a)
            if len(b) > common:
                added[version] = (len(b) - common, b)
        if len(removed) == len(added) == 1:
            old_version, (nold, old_ids) = next(iter(removed.items()))
            new_version, (nnew, new_ids) = next(iter(added.items()))
            count = min(nold, nnew)
            event('graph-version-change', key, old_version, count, old_ids, new_ids, new_version)
            removed[old_version] = (nold-count, old_ids)
            added[new_version] = (nnew-count, new_ids)
        elif removed and added:
            result['issues'].append({'code': 'ambiguous-version-pairing',
                                     'evidence_ids': [before['id'], after['id']]})
        for version, (count, ids) in removed.items():
            if count:
                event('graph-removal', key, version, count, ids, [])
        for version, (count, ids) in added.items():
            if count:
                kind = 'graph-reintroduction' if key in seen and key not in old else 'graph-addition'
                event(kind, key, version, count, [], ids)
    seen.update(old)
    seen.update(new)


def compare_graph_snapshots(snapshots, query, *, ancestry=(), proof_evidence_ids=(),
                            ordering='ancestry', limits=HistoryLimits()):
    """Compare report evidence snapshots without IO; never authenticate supplied proofs.

    ``ancestry`` entries are exactly {before, after, evidence_ids, verified};
    before/after are snapshot IDs. ``proof_evidence_ids`` enumerates known
    run-local ancestry evidence references. The caller owns their verification.
    Timestamp ordering must be requested explicitly and never falls back from
    missing ancestry. Returned records are detached from all caller inputs.
    """
    if not isinstance(limits, HistoryLimits):
        raise GraphHistoryError('limits must be HistoryLimits')
    if not isinstance(snapshots, list) or len(snapshots) > limits.max_snapshots:
        raise GraphHistoryError('snapshots must be a bounded list', 'snapshot-limit')
    if ordering not in ('ancestry', 'timestamps'):
        raise GraphHistoryError('ordering must be ancestry or timestamps')
    if not isinstance(ancestry, (list, tuple)) or len(ancestry) > limits.max_snapshots ** 2:
        raise GraphHistoryError('ancestry must be a bounded sequence')
    if not isinstance(proof_evidence_ids, (list, tuple)) or len(proof_evidence_ids) > limits.max_json_nodes:
        raise GraphHistoryError('proof_evidence_ids must be a bounded sequence')
    _bounded_json([snapshots, list(ancestry), list(proof_evidence_ids)], limits, limits.max_input_bytes)
    selected = _query(query, limits)
    items = [_snapshot(s, limits) for s in snapshots]
    indexed = {s['id']: s for s in items}
    if len(indexed) != len(items):
        raise GraphHistoryError('duplicate snapshot evidence id')
    proofs = set()
    for ref in proof_evidence_ids:
        _text(ref, 'proof evidence id', limits)
        if ref in proofs or ref in indexed:
            raise GraphHistoryError('duplicate or overlapping proof evidence id')
        proofs.add(ref)
    edges = []
    edge_keys = set()
    for e in ancestry:
        if not isinstance(e, dict) or set(e) != {'before', 'after', 'evidence_ids', 'verified'}:
            raise GraphHistoryError('invalid ancestry edge shape')
        if e['verified'] is not True:
            raise GraphHistoryError('ancestry edge must be actually verified')
        for endpoint in ('before', 'after'):
            _text(e[endpoint], 'edge endpoint', limits)
            if e[endpoint] not in indexed:
                raise GraphHistoryError('unknown snapshot endpoint')
        if e['before'] == e['after']:
            raise GraphHistoryError('ancestry edge cannot point to itself')
        key = (e['before'], e['after'])
        if key in edge_keys:
            raise GraphHistoryError('duplicate ancestry edge')
        edge_keys.add(key)
        refs = e['evidence_ids']
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in proofs for r in refs):
            raise GraphHistoryError('unknown or missing ancestry evidence reference')
        edges.append(e)
    result = dict(kind='graph-history', query=query, status='partial', events=[], issues=[], _event_bytes=0,
                  snapshot_ids=[s['id'] for s in items], historical_build_verified=False,
                  limitations=list(_LIMITATIONS))
    if not items:
        result['issues'].append({'code': 'insufficient-snapshots', 'evidence_ids': []})
    groups = defaultdict(list)
    unknown_context = []
    for s in items:
        if not s['usable']:
            result['issues'].append({'code': 'diagnostic-snapshot', 'evidence_ids': [s['id']]})
        unknown_optional = [nid for nid, node in s['nodes'].items()
                            if nid != s['root'] and _optional_state(node) is None]
        if unknown_optional:
            s['usable'] = False
            result['issues'].append({'code': 'unknown-optional-state', 'evidence_ids': [s['id']],
                                     'node_ids': unknown_optional,
                                     'limitation': 'snapshot comparisons skipped rather than treating unknown optional state as absence'})
        if s['realm'] == 'unknown' or s['module'] is None:
            result['issues'].append({'code': 'unknown-subject-context', 'evidence_ids': [s['id']]})
            s['usable'] = False
            unknown_context.append(s)
            continue
        groups[s['realm'], s['module'], _identity(s['nodes'][s['root']])].append(s)
    for (realm, module, root_identity), known in groups.items():
        # Unknown context may hide an observation belonging to this group.
        # Keep it in the ordering as a barrier, not an empty graph or an
        # omitted snapshot that lets the neighboring known records bridge it.
        group = known + [s for s in unknown_context
                         if s['realm'] in ('unknown', realm)
                         and s['module'] in (None, module)
                         and _identity(s['nodes'][s['root']]) == root_identity]
        if len(group) < 2:
            result['issues'].append({'code': 'insufficient-snapshots', 'evidence_ids': [s['id'] for s in group]})
            continue
        if ordering == 'ancestry':
            missing = [s['id'] for s in group if s['provenance'].get('revision') is None]
            if missing:
                result['issues'].append({'code': 'missing-revision', 'evidence_ids': missing})
                continue
            if len({s['provenance']['revision'] for s in group}) != len(group):
                result['issues'].append({'code': 'ambiguous-order', 'evidence_ids': [s['id'] for s in group]})
                continue
            ordered, proof_map = _ancestry_order(group, edges, result['issues'])
        else:
            ordered = _timestamp_order(group, result['issues'])
            proof_map = {}
        if ordered is None:
            continue
        seen = set()
        for before, after in zip(ordered, ordered[1:]):
            if not before['usable'] or not after['usable']:
                seen.clear()
                continue
            basis = dict(basis='verified-ancestry' if ordering == 'ancestry' else 'recorded-timestamp-temporal-only',
                         evidence_ids=proof_map.get((before['id'], after['id']), []))
            _diff(before, after, selected, seen, basis, result, limits)
    del result['_event_bytes']
    result['status'] = 'partial' if result['issues'] else 'complete-within-snapshots'
    _bounded_json(result, limits, limits.max_output_bytes)
    return copy.deepcopy(result)

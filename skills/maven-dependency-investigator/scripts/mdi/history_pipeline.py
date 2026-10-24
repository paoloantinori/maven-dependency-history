"""Assemble run-local graph history from imports and fresh local Git proofs.

This module returns a same-revision draft, never persists it. The coordinator
must commit with the expected revision. Only evidence already in that report
can become a snapshot; caller-supplied ancestry edges or foreign reports are
not accepted. Imported producer context remains caller-attested. Source
qualifications are copied into fresh run-local qualification records affecting
the derived graph evidence and optional declaration leads, so analyses citing
only those derived records must still carry the source limitations. Local Git
ancestry authenticates ordering only, not the historical build or snapshot.

Declaration history is optional and is a separate candidate-lead observation.
It does not date graph introduction precisely or prove shipped contents. Each
underlying collector retains its own documented time/output limits; this is
not a single hard wall-time or checkout-atomic transaction.
"""

import copy
import json
import os
from datetime import datetime, timezone

from mdi.ancestry import AncestryError, _check_child_reaping, collect_ancestry
from mdi.graph_history import GraphHistoryError, HistoryLimits, _bounded_json, compare_graph_snapshots
from mdi.history import _validate_pom_paths, inspect_history
from mdi.report import ReportError, _qualification_text, validate_report, validate_transition


class HistoryPipelineError(ValueError):
    """The supplied run, query, identifiers or collector limits are invalid."""


_LIMITATIONS = (
    'snapshots are run-local imported observations; their producer context is caller-attested, not authenticated by Git',
    'Git ancestry proves ordering of available local commit objects, not historical builds or shipped contents',
    'declaration changes for introducing dependencies are candidate leads, not proof of exact transitive introduction',
    'separate collector deadlines and output caps apply; no whole-call hard deadline or atomic checkout snapshot is claimed',
    'default SIGCHLD and no concurrent external child reaping are required; changing and restoring policy during a collector is unsupported',
)


def _text(value, label, bound=4096):
    if not isinstance(value, str) or not value or len(value) > bound or '\x00' in value:
        raise HistoryPipelineError(f'{label} must be bounded nonempty text')
    return value


def _query_parts(query):
    parts = _text(query, 'query').split(':')
    if len(parts) not in (1, 2, 3) or any(not p or '${' in p for p in parts):
        raise HistoryPipelineError('query must be artifactId or groupId:artifactId[:version]')
    return parts


def _matches(coords, parts):
    if isinstance(coords, str):
        values = coords.split(':')
        coords = ({'artifactId': values[0]} if len(values) == 1 else
                  dict(zip(('groupId', 'artifactId', 'version'), values)) if len(values) in (2, 3) else None)
    if not isinstance(coords, dict):
        return False
    keys = ('artifactId',) if len(parts) == 1 else ('groupId', 'artifactId', 'version')[:len(parts)]
    return all(coords.get(key) == value for key, value in zip(keys, parts))


def _fresh_ids(current, identifiers):
    known = {r['id'] for field in ('evidence', 'subjects', 'analyses', 'operations', 'qualifications')
             for r in current[field]}
    for identifier in identifiers:
        _text(identifier, 'new evidence id', 256)
        if identifier in known:
            raise HistoryPipelineError('new evidence ids must be unique and absent from the run')
        known.add(identifier)


def _snapshots(current, selected):
    snapshots, excluded, uncertain = [], [], []
    for record in current['evidence']:
        if record['category'] != 'imported-tree-json':
            continue
        payload = record.get('payload')
        imported = payload.get('import') if isinstance(payload, dict) else None
        if not isinstance(imported, dict) or imported.get('kind') != 'dependency-tree':
            raise HistoryPipelineError('imported-tree-json evidence lacks its dependency-tree payload')
        provenance = imported.get('provenance')
        if not isinstance(provenance, dict):
            raise HistoryPipelineError('tree import lacks provenance')
        module, realm = provenance.get('module'), imported.get('subject_realm')
        compatible = [s for s in selected
                      if (module is None or s.get('module') is None or s.get('module') == module)
                      and (realm == 'unknown' or s['realm'] == 'unknown' or s['realm'] == realm)]
        if not compatible:
            excluded.append(record['id'])
            continue
        snapshots.append(record)
        if module is None or realm == 'unknown' or any(
                s.get('module') is None or s['realm'] == 'unknown' for s in compatible):
            uncertain.append(record['id'])
    return snapshots, excluded, uncertain


def _attested_candidate_scopes(subject, ga, snapshots):
    attested = {}
    for record in snapshots:
        imported = record['payload']['import']
        provenance = imported['provenance']
        context = record.get('generation_context')
        if not isinstance(context, dict):
            continue
        supplied = context.get('supplied_provenance')
        realm = imported.get('subject_realm')
        if context.get('realm_attestation') != 'caller-attested' or realm not in ('project', 'build-plugin') \
                or context.get('realm') != realm or not isinstance(supplied, dict) \
                or supplied.get('module') != subject['module'] or provenance.get('module') != subject['module']:
            continue
        if not any(node.get('parent') is not None and _matches(node['coordinates'], ga.split(':'))
                   for node in imported['evidence']['nodes']):
            continue
        attested.setdefault(realm, []).append(record['id'])
    return attested


def _selected_scopes(selected, snapshots):
    scopes, issues = {}, []
    for subject in selected:
        coordinates = subject.get('coordinates', '').split(':')
        module, realm = subject.get('module'), subject['realm']
        if len(coordinates) not in (2, 3) or any(not p or '${' in p for p in coordinates[:2]) \
                or module is None or '${' in module:
            issues.append(dict(code='selected-subject-scope-unknown', subject_id=subject['id']))
            continue
        # Versions deliberately do not constrain a historical comparison.
        ga = ':'.join(coordinates[:2])
        candidates = _attested_candidate_scopes(subject, ga, snapshots) if realm == 'unknown' else {realm: []}
        if not candidates:
            issues.append(dict(code='selected-subject-scope-unknown', subject_id=subject['id']))
        for context_realm, evidence_ids in candidates.items():
            candidate = realm == 'unknown'
            key = (ga, module, context_realm)
            scope = scopes.setdefault(key, dict(query=ga, module=module, realm=context_realm,
                basis='caller-attested-import-candidate' if candidate else 'selected-subject',
                subject_ids=[], evidence_ids=[]))
            scope['subject_ids'].append(subject['id'])
            if candidate:
                scope['basis'] = 'caller-attested-import-candidate'
            scope['evidence_ids'] = list(dict.fromkeys(scope['evidence_ids'] + evidence_ids))
            if candidate:
                issues.append(dict(code='candidate-history-scope', subject_id=subject['id'],
                    evidence_ids=evidence_ids, realm=context_realm, module=module))
    return list(scopes.values()), issues


def _scope_snapshots(snapshots, scope):
    return [record for record in snapshots
            if record['payload']['import']['provenance'].get('module') in (None, scope['module'])
            and record['payload']['import'].get('subject_realm') in ('unknown', scope['realm'])]


def _compare_scopes(snapshots, scopes, issues, query, limits, ancestry=(), proof_evidence_ids=()):
    graph = dict(kind='graph-history', query=query, status='partial', events=[], issues=list(issues),
                 snapshot_ids=[s['id'] for s in snapshots], historical_build_verified=False,
                 limitations=list(_LIMITATIONS), selected_scopes=copy.deepcopy(scopes))
    # Validate even when the selected context is unknown, without producing
    # events from an unscoped query. Unknown-context snapshots stay in every
    # compatible scope as barriers for the comparator, never silently removed.
    if not scopes:
        validation = compare_graph_snapshots(snapshots, query, limits=limits)
        graph['issues'].extend(validation['issues'])
    for scope in scopes:
        items = _scope_snapshots(snapshots, scope)
        ids = {s['id'] for s in items}
        edges = [e for e in ancestry if e['before'] in ids and e['after'] in ids]
        comparison = compare_graph_snapshots(items, scope['query'], ancestry=edges,
                    proof_evidence_ids=proof_evidence_ids, limits=limits)
        if len(graph['events']) + len(comparison['events']) > limits.max_events:
            raise GraphHistoryError('combined selected-scope events exceed limit', 'event-limit')
        for event in comparison['events']:
            event['selected_scope'] = copy.deepcopy(scope)
        graph['events'].extend(comparison['events'])
        for issue in comparison['issues']:
            if issue not in graph['issues']:
                graph['issues'].append(issue)
        for limitation in comparison['limitations']:
            if limitation not in graph['limitations']:
                graph['limitations'].append(limitation)
        _bounded_json(graph, limits, limits.max_output_bytes)
    if not snapshots and not any(i['code'] == 'insufficient-snapshots' for i in graph['issues']):
        graph['issues'].append(dict(code='insufficient-snapshots', evidence_ids=[]))
    graph['status'] = 'partial' if graph['issues'] else 'complete-within-snapshots'
    return graph


def _scope_pom_paths(paths, scope):
    # A selected parent module does not select its child modules implicitly.
    return tuple(path for path in paths if (path.rpartition('/')[0] or '.') == scope['module'])


def _json_copy(value):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, RecursionError) as exc:
        raise HistoryPipelineError('collector returned a non-JSON result') from exc


def _inherit_limitations(record, qualifications):
    limitations = record.setdefault('limitations', [])
    for qualification in qualifications:
        text = _qualification_text(qualification)
        if text not in limitations:
            limitations.append(text)


def _derived_qualifications(current, qualifications, targets, new_ids):
    used = {record['id'] for field in ('subjects', 'evidence', 'analyses', 'operations', 'qualifications')
            for record in current[field]} | set(new_ids)
    inherited = []
    index = 1
    for source in qualifications:
        while f'graph-history-qualification-{index}' in used:
            index += 1
        identifier = f'graph-history-qualification-{index}'
        used.add(identifier)
        inherited.append(dict(id=identifier, affected_evidence_ids=list(targets),
            kind=source['kind'], reason=source['reason'], observed_at=source['observed_at']))
    return inherited


def _candidate_scope_qualifications(scopes):
    return [dict(kind='candidate-history-scope', observed_at=datetime.now(timezone.utc).isoformat(),
        reason=f"{scope['query']} in module {scope['module']} and realm {scope['realm']} is associated through "
               f"caller-attested imports {', '.join(scope['evidence_ids'])}; this is candidate graph history, "
               "not packaged realm, component identity, or historical build proof")
        for scope in scopes if scope['basis'] == 'caller-attested-import-candidate']


def _evidence(identifier, category, repo, payload):
    return dict(id=identifier, category=category, source_locator=repo,
                observed_at=datetime.now(timezone.utc).isoformat(), payload=payload)


def collect_graph_history(current, repo, query, *, ancestry_evidence_id,
                          graph_evidence_id, declaration_evidence_id=None,
                          pom_paths=(), limits=HistoryLimits(), timeout_seconds=10,
                          max_output_bytes=1024 * 1024, max_commits=64):
    """Return a validated draft, graph/ancestry results and optional declaration leads.

    A recorded selection is required and the query must match at least one
    selected subject. Each known selected group/artifact/module/realm is compared
    separately with a versionless query so upgrades remain visible. Unknown
    selected scopes produce partial evidence rather than broad matching. For a
    known selected GA/module with unknown realm, an explicit same-run imported
    realm/module attestation containing that GA permits candidate graph-history
    scopes only. The packaged subject stays unknown and inherited qualifications
    prohibit promotion to packaged identity or build proof. Snapshots
    from known different module/realm contexts are excluded explicitly; unknown
    contexts remain qualified barriers. All eligible trees,
    including diagnostic or unknown-mode snapshots, are retained so gaps are
    not silently bridged. ``pom_paths`` explicitly selects local declaration
    history to inspect; an archive-only subject need not have its own declaration.

    Identifiers are chosen by the coordinator, must be fresh, and refer only to
    this run. No new revision is published. ``limits`` bounds graph inputs/events;
    ``timeout_seconds`` and ``max_output_bytes`` bound the ancestry collector and
    separately each selected-scope declaration collector, not their combined
    duration. Multiple declaration scopes return their individual observations
    alongside merged events. Supplied POM paths belong only to the exactly
    selected module directory; selecting a root module does not select its children.
    """
    try:
        validate_report(current)
    except (ReportError, RecursionError) as exc:
        raise HistoryPipelineError('invalid input report') from exc
    if not isinstance(limits, HistoryLimits):
        raise HistoryPipelineError('limits must be HistoryLimits')
    if type(max_commits) is not int or max_commits <= 0:
        raise HistoryPipelineError('max_commits must be a positive integer')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise HistoryPipelineError('timeout_seconds must be an integer from 1 through 3600')
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 8 * 1024 * 1024:
        raise HistoryPipelineError('max_output_bytes must be a positive integer at most 8 MiB')
    parts = _query_parts(query)
    if current['selection'] is None:
        raise HistoryPipelineError('a recorded candidate selection is required')
    ids = set(current['selection']['candidate_ids'])
    selected = [s for s in current['subjects'] if s['id'] in ids and _matches(s.get('coordinates'), parts)]
    if not selected:
        raise HistoryPipelineError('query does not match a selected subject')
    try:
        repo = os.path.abspath(_text(os.fspath(repo), 'repo'))
        if not isinstance(pom_paths, (list, tuple)):
            raise ValueError('pom_paths must be a list or tuple')
        paths = _validate_pom_paths(pom_paths) if pom_paths else ()
    except (TypeError, ValueError) as exc:
        raise HistoryPipelineError('repo and POM paths must be valid local paths') from exc
    for record in current['evidence']:
        if record['category'] == 'discovery-context':
            payload = record['payload']
            if not isinstance(payload, dict) or payload.get('repo') != repo:
                raise HistoryPipelineError('repo differs from the recorded discovery context')
    if bool(paths) != (declaration_evidence_id is not None):
        raise HistoryPipelineError('declaration_evidence_id and nonempty pom_paths must be supplied together')
    identifiers = [ancestry_evidence_id, graph_evidence_id]
    if declaration_evidence_id is not None:
        identifiers.append(declaration_evidence_id)
    _fresh_ids(current, identifiers)
    snapshots, excluded, uncertain = _snapshots(current, selected)
    if len(snapshots) > limits.max_snapshots:
        raise HistoryPipelineError('snapshot count exceeds limits')
    # Validate the entire comparator input before invoking local Git. No ordering
    # evidence is asserted by this validation-only comparison.
    try:
        _bounded_json(snapshots, limits, limits.max_input_bytes)
        compare_graph_snapshots(snapshots, query, limits=limits)
        scopes, scope_issues = _selected_scopes(selected, snapshots)
        if len(scopes) > limits.max_snapshots:
            raise HistoryPipelineError('selected scope count exceeds limits')
        _compare_scopes(snapshots, scopes, scope_issues, query, limits)
    except GraphHistoryError as exc:
        raise HistoryPipelineError(f'invalid snapshot evidence ({exc.code})') from exc

    try:
        ancestry = collect_ancestry(repo, snapshots, evidence_id=ancestry_evidence_id,
                                   max_snapshots=min(limits.max_snapshots, 128),
                                   timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
    except AncestryError as exc:
        raise HistoryPipelineError('invalid snapshot ancestry inputs') from exc
    graph = _compare_scopes(snapshots, scopes, scope_issues, query, limits,
                            ancestry=ancestry['ancestry'], proof_evidence_ids=ancestry['proof_evidence_ids'])
    snapshot_ids = {s['id'] for s in snapshots}
    qualifications = [q for q in current['qualifications']
                      if snapshot_ids.intersection(q['affected_evidence_ids'])]
    scope_qualifications = _candidate_scope_qualifications(scopes)
    inherited = qualifications + scope_qualifications
    _inherit_limitations(graph, inherited)
    for event in graph['events']:
        event_ids = set(event['evidence_ids'])
        affected = [q for q in qualifications if event_ids.intersection(q['affected_evidence_ids'])]
        _inherit_limitations(event, affected + scope_qualifications)
    payload = dict(comparison=graph, source_evidence_ids=[s['id'] for s in snapshots],
                   ancestry_evidence_ids=ancestry['proof_evidence_ids'],
                   source_qualifications=copy.deepcopy(qualifications),
                   limitations=list(_LIMITATIONS))
    _inherit_limitations(payload, inherited)
    new_evidence = ancestry['evidence'] + [_evidence(graph_evidence_id, 'graph-history', repo, payload)]
    declarations = None
    reasons = [i['code'] for i in graph['issues']] + [i['code'] for i in ancestry['issues']]
    if uncertain:
        reasons.append('selected-or-snapshot-context-unknown')
    if qualifications:
        reasons.append('source-evidence-qualified')
    if paths:
        # Only observed introducing paths from eligible comparisons are leads.
        # No POM is asserted to have introduced the target merely by proximity.
        observations = []
        for scope in scopes:
            scoped_paths = _scope_pom_paths(paths, scope)
            if not scoped_paths or scope['realm'] != 'project':
                observations.append(dict(events=[], issues=[{'code': 'declaration-scope-unavailable'}],
                    coverage={'status': 'partial', 'limitations': list(_LIMITATIONS)}, selected_scope=scope))
                continue
            introducing = sorted({p['groupId'] + ':' + p['artifactId']
                for event in graph['events'] if event['module'] == scope['module'] and event['realm'] == scope['realm']
                and _matches(event['path'][-1], scope['query'].split(':'))
                for p in event['path'][:1] if not _matches(p, scope['query'].split(':'))})
            try:
                _check_child_reaping()
                observation = _json_copy(inspect_history(repo, scope['query'], pom_paths=scoped_paths,
                    introducing_dependencies=introducing, max_commits=max_commits,
                    timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes))
                _check_child_reaping()
            except Exception:
                observation = dict(events=[], issues=[{'code': 'declaration-history-failed'}],
                                   coverage={'status': 'failed', 'limitations': list(_LIMITATIONS)})
            observation['selected_scope'] = scope
            for event in observation['events']:
                event['selected_scope'] = scope
            observations.append(observation)
            if sum(len(item['events']) for item in observations) > limits.max_events:
                raise HistoryPipelineError('combined declaration events exceed limits')
            _bounded_json(observations, limits, limits.max_output_bytes)
        if len(observations) == 1:
            declarations = observations[0]
        else:
            declarations = dict(events=[e for o in observations for e in o['events']],
                issues=[i for o in observations for i in o['issues']], selected_scopes=scopes,
                coverage={'status': 'partial' if not observations or any(o['coverage']['status'] != 'complete' for o in observations) else 'complete',
                          'limitations': list(_LIMITATIONS)}, observations=observations)
            if not observations:
                declarations['issues'].append({'code': 'selected-subject-scope-unknown'})
        if len(declarations['events']) > limits.max_events:
            raise HistoryPipelineError('combined declaration events exceed limits')
        _bounded_json(declarations, limits, limits.max_output_bytes)
        _inherit_limitations(declarations['coverage'], inherited)
        for observation in declarations.get('observations', []):
            _inherit_limitations(observation['coverage'], inherited)
        for event in declarations['events']:
            if event['kind'] == 'candidate-introduction':
                _inherit_limitations(event, inherited)
        declaration_payload = dict(observation=declarations, graph_evidence_id=graph_evidence_id,
                                   limitations=list(_LIMITATIONS))
        _inherit_limitations(declaration_payload, inherited)
        new_evidence.append(_evidence(declaration_evidence_id, 'declaration-history-candidates', repo,
                                     declaration_payload))
        if declarations['coverage']['status'] != 'complete':
            reasons.append('declaration-history-incomplete')
    _bounded_json(new_evidence, limits, limits.max_output_bytes)
    stage = dict(name='graph-history', outcome='partial' if reasons else 'complete',
        coverage=dict(snapshot_ids=[s['id'] for s in snapshots], selected_scopes=copy.deepcopy(scopes),
                      excluded_snapshot_ids=excluded,
                      uncertain_context_snapshot_ids=uncertain, ordering='local-git-ancestry',
                      graph_status=graph['status'], ancestry_status=ancestry['coverage']['status'],
                      historical_build_verified=False, limitations=list(_LIMITATIONS)),
        reasons=list(dict.fromkeys(reasons)),
        evidence_ids=[s['id'] for s in snapshots] + [e['id'] for e in new_evidence])
    draft = copy.deepcopy(current)
    draft['evidence'].extend(copy.deepcopy(new_evidence))
    targets = [graph_evidence_id] + ([declaration_evidence_id] if paths else [])
    draft['qualifications'].extend(_derived_qualifications(current, inherited, targets, identifiers))
    for index, previous in enumerate(draft['stages']):
        if previous['name'] == stage['name']:
            draft['stages'][index] = copy.deepcopy(stage)
            break
    else:
        draft['stages'].append(copy.deepcopy(stage))
    validate_report(draft)
    successor = dict(draft, revision=draft['revision'] + 1)
    validate_transition(current, successor)
    return dict(draft=draft, graph=copy.deepcopy(graph), ancestry=copy.deepcopy(ancestry),
                declarations=copy.deepcopy(declarations), stage=copy.deepcopy(stage))

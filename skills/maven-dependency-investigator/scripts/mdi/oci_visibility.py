"""Pure visibility composition of recorded OCI layer inventories.

Accepts the result shape of oci_layout.inspect_oci_layout, never opens paths or
executes layer contents. The supplied manifest order and captured inventories
are caller-provided evidence, not independently authenticated here. Returned
visibility applies to the recorded regular-file/directory subset only, not a
reconstructed runnable filesystem or proof of global absence.

Rules grounded in the locally retained OCI image-spec v1.1.1 layer document
(/tmp/mdi-oci-layer-spec.txt, read 2026-09-12), sections Applying Changesets,
Changeset over existing files and Whiteouts: whiteouts affect LOWER layers
only, including when they appear after same-layer additions; opaque markers
hide lower descendants but not their containing directory; directory over
directory preserves children, other replacements remove the old path. The
spec forbids duplicate paths within a changeset. Duplicate or unsupported
members are therefore uncertainty, never arbitrary first/last-wins choices.

Every supplied layer must be present in increasing contiguous index order.
Unsupported links/extensions, missing or partial layers, rejected names,
conflicting same-layer file parents and malformed whiteouts make the entire
view partial. All recorded visibility then becomes uncertain, including any
apparent removals. This intentionally conservative rule prevents unknown
changes from being reported as definitive absence or retained final content.
Metadata candidates are exported only from members with visible final content
in a complete recorded view, and remain candidates with realm unknown. Whiteout
summary arrays are not trusted as authority: markers are derived from the
bounded, validated member records themselves. Reserved whiteout components in
ancestor paths are unsupported, never ordinary visible directories. Before
exporting Java candidates, complete archive roots must match their enclosing
member digest and size, and every nested chain must link to a complete indexed
archive entry. Inconsistent or incomplete Java inventories make the view partial.
"""

import copy
import json
import math
import re
from dataclasses import dataclass, asdict


_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_DESCRIPTOR = re.compile(r'sha256:[0-9a-f]{64}\Z')
_REGULAR = ('30', '00')
_KNOWN_ISSUES = {'layer-composition-not-performed', 'layer-whiteouts-not-applied'}
_LIMITATIONS = (
    'visibility is derived from caller-supplied recorded layer inventories, not independently authenticated',
    'only complete regular-file and directory changesets are composed; unsupported or missing data makes visibility uncertain',
    'this is not a runnable merged filesystem or a proof of global absence',
    'metadata candidates do not prove component identity, application/container attribution, build provenance or safe removal',
)


class VisibilityError(ValueError):
    """Input or output exceeds the bounded visibility contract."""


@dataclass(frozen=True)
class VisibilityLimits:
    max_layers: int = 64
    max_members: int = 20000
    max_text: int = 4096
    max_nodes: int = 500000
    max_depth: int = 32
    max_input_bytes: int = 24 * 1024**2
    max_output_bytes: int = 24 * 1024**2
    max_comparisons: int = 1000000
    max_path_depth: int = 128

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value <= 0:
                raise VisibilityError(f'{name} must be a positive integer')


def _bounded(value, limits, cap):
    # Visit iteratively without pushing all members of a wide container. Bound
    # strings before the encoder can allocate a complete escaped string token.
    stack = [(iter((value,)), 0)]
    nodes = 0
    while stack:
        iterator, depth = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        nodes += 1
        if nodes > limits.max_nodes or depth > limits.max_depth:
            raise VisibilityError('JSON structure exceeds bounds')
        if isinstance(item, str):
            if len(item) > limits.max_text or any(0xD800 <= ord(c) <= 0xDFFF for c in item):
                raise VisibilityError('JSON string exceeds bounds or contains a surrogate')
        elif isinstance(item, dict):
            if len(item) > limits.max_nodes - nodes:
                raise VisibilityError('JSON object exceeds bounds')
            for key in item:
                if not isinstance(key, str) or len(key) > limits.max_text:
                    raise VisibilityError('invalid JSON object key')
            stack.append((iter(item.values()), depth + 1))
        elif isinstance(item, list):
            if len(item) > limits.max_nodes - nodes:
                raise VisibilityError('JSON list exceeds bounds')
            stack.append((iter(item), depth + 1))
        elif item is None or type(item) is bool:
            pass
        elif type(item) is int:
            if item.bit_length() > 256:
                raise VisibilityError('integer exceeds bounds')
        elif type(item) is float:
            if not math.isfinite(item):
                raise VisibilityError('nonfinite JSON number')
        else:
            raise VisibilityError('value is not JSON-safe')
    size = 0
    for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(value):
        size += len(part)
        if size > cap:
            raise VisibilityError('serialized evidence exceeds byte bound')


def _path(name):
    if (not isinstance(name, str) or not name or name.startswith('/') or '\\' in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or (len(name) > 1 and name[1] == ':')):
        return None
    return name if all(p not in ('', '.', '..') for p in name.split('/')) else None


def _member(row):
    if not isinstance(row, dict) or type(row.get('index')) is not int or row['index'] < 0:
        raise VisibilityError('invalid member index')
    if not isinstance(row.get('name'), str) or not isinstance(row.get('type'), str):
        raise VisibilityError('invalid member locator/type')
    if not isinstance(row.get('status'), str) or type(row.get('name_truncated')) is not bool:
        raise VisibilityError('invalid member status')
    for key in ('size_actual', 'size_declared'):
        if row.get(key) is not None and (type(row[key]) is not int or row[key] < 0):
            raise VisibilityError('invalid member size')
    if row.get('sha256') is not None and (not isinstance(row['sha256'], str) or not _DIGEST.fullmatch(row['sha256'])):
        raise VisibilityError('invalid member digest')


def _java_inventory_complete(inspection, member, limits):
    """Check the nested identity chain, not authenticity of supplied content."""
    if not isinstance(inspection, dict):
        return False
    coverage = inspection.get('coverage')
    containers, entries = inspection.get('archives'), inspection.get('entries')
    if (not isinstance(coverage, dict) or coverage.get('status') != 'inspected'
            or coverage.get('input_truncated') is not False or inspection.get('issues') != []
            or not isinstance(containers, list) or not containers
            or len(containers) > limits.max_members or not isinstance(entries, list)
            or len(entries) > limits.max_members):
        return False
    outer = coverage.get('outer_capture')
    if (not isinstance(outer, dict) or outer.get('complete') is not True
            or outer.get('sha256') != member['sha256']
            or coverage.get('input_bytes') != member['size_actual']):
        return False
    by_id, by_chain, entry_map = {}, {}, {}
    for container in containers:
        if not isinstance(container, dict):
            return False
        identifier, chain = container.get('archive_id'), container.get('entry_chain')
        if (not isinstance(identifier, str) or not identifier or identifier in by_id
                or not isinstance(chain, list) or len(chain) > limits.max_path_depth
                or any(type(index) is not int or index < 0 for index in chain)
                or tuple(chain) in by_chain
                or container.get('inspection_status') != 'complete'
                or not isinstance(container.get('sha256'), str)
                or not _DIGEST.fullmatch(container['sha256'])
                or type(container.get('size')) is not int or container['size'] < 0
                or container.get('path') != outer.get('path')):
            return False
        identity = container.get('identity')
        if (not isinstance(identity, dict) or identity.get('status') not in ('candidate', 'unknown')
                or identity.get('conflict') is not False
                or not isinstance(identity.get('candidates'), list)):
            return False
        for value in identity['candidates']:
            if not isinstance(value, dict) or any(
                    not isinstance(value.get(key), str) or not value[key]
                    for key in ('groupId', 'artifactId', 'version')):
                return False
        by_id[identifier], by_chain[tuple(chain)] = container, container
    root = by_chain.get(())
    if (root is None or root['sha256'] != member['sha256']
            or root['size'] != member['size_actual']):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        owner, index = entry.get('archive_id'), entry.get('index')
        if (not isinstance(owner, str) or owner not in by_id or type(index) is not int or index < 0
                or (owner, index) in entry_map or not isinstance(entry.get('name'), str)
                or type(entry.get('size_actual')) is not int or entry['size_actual'] < 0
                or type(entry.get('size_declared')) is not int
                or entry['size_actual'] != entry['size_declared']
                or type(entry.get('is_archive')) is not bool):
            return False
        if entry.get('kind') == 'directory':
            if (entry.get('status') != 'recorded' or entry['size_actual'] != 0
                    or entry.get('sha256') is not None or entry['is_archive']
                    or not entry['name'].endswith('/')):
                return False
        elif (entry.get('kind') != 'file' or entry.get('status') != 'hashed'
                or not isinstance(entry.get('sha256'), str) or not _DIGEST.fullmatch(entry['sha256'])):
            return False
        entry_map[owner, index] = entry
    for chain, container in by_chain.items():
        if not chain:
            continue
        parent = by_chain.get(chain[:-1])
        entry = entry_map.get((parent['archive_id'], chain[-1])) if parent else None
        if (entry is None or not entry['is_archive']
                or entry['sha256'] != container['sha256'] or entry['size_actual'] != container['size']):
            return False
    for (owner, index), entry in entry_map.items():
        if entry['is_archive'] and tuple(by_id[owner]['entry_chain']) + (index,) not in by_chain:
            return False
    return True


def compose_visibility(inventory, *, limits=VisibilityLimits()):
    """Return visible/removed/uncertain records and qualified Java candidates.

    Does not mutate the input. Structural invalidity raises VisibilityError;
    incomplete inventories and unsupported changes return partial evidence.
    Same-layer whiteouts are applied before all same-layer additions.
    """
    if not isinstance(limits, VisibilityLimits):
        raise VisibilityError('limits must be VisibilityLimits')
    _bounded(inventory, limits, limits.max_input_bytes)
    if not isinstance(inventory, dict) or not isinstance(inventory.get('layers'), list):
        raise VisibilityError('expected OCI inventory object')
    layers = inventory['layers']
    if len(layers) > limits.max_layers:
        raise VisibilityError('too many layers')
    issues, records, active, candidates = [], [], {}, []
    member_count = comparisons = 0
    prior_index = -1
    archive_refs = {}

    def issue(code, layer_index=None):
        if len(issues) >= limits.max_members:
            raise VisibilityError('issue limit exceeded')
        issues.append({'code': code, 'layer_index': layer_index})

    def spend():
        nonlocal comparisons
        comparisons += 1
        if comparisons > limits.max_comparisons:
            raise VisibilityError('visibility comparison budget exceeded')

    def remove(path, cause, reason, descendants=True, exact=True):
        for key in list(active):
            spend()
            if (exact and key == path) or (descendants and (not path or key.startswith(path + '/'))):
                old = active.pop(key)
                old.update(visibility='removed', reason=reason, changed_by=cause)

    selected = inventory.get('selected_manifest')
    if not isinstance(selected, str) or not _DESCRIPTOR.fullmatch(selected):
        issue('manifest-selection-unknown')
    source_issues = inventory.get('issues', [])
    if not isinstance(source_issues, list) or any(not isinstance(i, dict) for i in source_issues):
        raise VisibilityError('invalid source issues')
    if any(i.get('code') not in _KNOWN_ISSUES for i in source_issues):
        issue('source-inventory-incomplete')
    if isinstance(inventory.get('source'), dict) and inventory['source'].get('unchanged') is False:
        issue('source-identity-invalidated')
    for layer in layers:
        if not isinstance(layer, dict) or type(layer.get('index')) is not int or layer['index'] <= prior_index:
            raise VisibilityError('layers must have unique increasing integer indices')
        position = layer['index']
        if position != prior_index + 1:
            issue('missing-layer-inventory', position)
        prior_index = position
        digest = layer.get('digest')
        if not isinstance(digest, str) or not _DESCRIPTOR.fullmatch(digest):
            raise VisibilityError('invalid layer digest')
        distribution = layer.get('distribution')
        if not isinstance(distribution, dict) or not isinstance(distribution.get('members'), list):
            raise VisibilityError('invalid layer distribution')
        members = distribution['members']
        member_count += len(members)
        if member_count > limits.max_members:
            raise VisibilityError('member limit exceeded')
        coverage = distribution.get('coverage')
        if not isinstance(coverage, dict) or coverage.get('status') != 'complete' or distribution.get('issues'):
            issue('layer-inventory-incomplete', position)
        by_path, indices, markers, additions = {}, {}, [], []
        for member in members:
            _member(member)
            index = member['index']
            if index in indices:
                raise VisibilityError('duplicate member index')
            indices[index] = member['name']
            path = _path(member['name'])
            record = dict(layer_index=position, layer_digest=digest, member_index=index,
                          path=member['name'], type=member['type'], sha256=member.get('sha256'),
                          size=member.get('size_actual'), visibility='visible', reason='recorded-member',
                          changed_by=None, realm='unknown')
            records.append(record)
            if path is not None and path.count('/') + 1 > limits.max_path_depth:
                raise VisibilityError('member path depth exceeded')
            if path is None or member['name_truncated']:
                issue('unsupported-member-path', position)
                continue
            if any(component.startswith('.wh.') for component in path.split('/')[:-1]):
                issue('reserved-whiteout-ancestor', position)
                continue
            if path in by_path:
                issue('duplicate-member-path', position)
            by_path[path] = record
            valid = (member['status'] == 'recorded' and member['type'] in (*_REGULAR, '35')
                     and member.get('sha256') is not None
                     and member.get('size_actual') == member.get('size_declared')
                     and (member['type'] != '35' or member.get('size_actual') == 0))
            if not valid:
                issue('unsupported-or-incomplete-member', position)
                continue
            parent, _, basename = path.rpartition('/')
            if basename.startswith('.wh.'):
                record.update(visibility='removed', reason='whiteout-marker')
                if member['type'] not in _REGULAR or member['size_actual'] != 0:
                    issue('invalid-whiteout', position)
                elif basename == '.wh..wh..opq':
                    markers.append((parent, False, record))
                elif basename[4:] not in ('', '.', '..') and not basename.startswith('.wh..wh.'):
                    markers.append(((parent + '/' if parent else '') + basename[4:], True, record))
                else:
                    issue('unsupported-whiteout', position)
            else:
                additions.append(record)
        # No extraction order can make duplicate paths or regular-file parents
        # an unambiguous supported changeset. Preserve all rows as uncertainty.
        for row in additions:
            parts = row['path'].split('/')
            for count in range(1, len(parts)):
                parent = by_path.get('/'.join(parts[:count]))
                if parent is not None and parent['type'] != '35':
                    issue('same-layer-path-conflict', position)
        for path, exact, marker in markers:
            cause = {'layer_index': position, 'member_index': marker['member_index']}
            remove(path, cause, 'opaque-whiteout' if not exact else 'whiteout', exact=exact)
        # Directories first keeps directory metadata updates independent of tar
        # order, without allowing whiteouts to hide this layer's additions.
        for row in sorted(additions, key=lambda r: (r['path'].count('/'), r['member_index'])):
            path = row['path']
            cause = {'layer_index': position, 'member_index': row['member_index']}
            old = active.get(path)
            same_dirs = old is not None and old['type'] == row['type'] == '35'
            if row['type'] != '35' or (old is not None and not same_dirs):
                remove(path, cause, 'replaced')
            elif old is not None:
                remove(path, cause, 'directory-metadata-replaced', descendants=False)
            parts = path.split('/')
            for count in range(1, len(parts)):
                parent = active.get('/'.join(parts[:count]))
                if parent is not None and parent['type'] != '35':
                    issue('unsupported-parent-replacement', position)
            active[path] = row
        archives = distribution.get('archives', [])
        if not isinstance(archives, list):
            raise VisibilityError('invalid Java archive inventory')
        for archive in archives:
            if not isinstance(archive, dict) or type(archive.get('member_index')) is not int:
                raise VisibilityError('invalid Java member reference')
            member_index = archive['member_index']
            if member_index not in indices or archive.get('member_name') != indices[member_index]:
                raise VisibilityError('Java archive references unknown member')
            if (position, member_index) in archive_refs:
                raise VisibilityError('duplicate Java archive reference')
            archive_refs[position, member_index] = archive
    for row in records:
        archive = archive_refs.get((row['layer_index'], row['member_index']))
        if archive is not None and row['visibility'] == 'visible':
            member_identity = {'sha256': row['sha256'], 'size_actual': row['size']}
            if row['type'] not in _REGULAR or not _java_inventory_complete(
                    archive.get('inspection'), member_identity, limits):
                issue('java-inventory-incomplete-or-inconsistent', row['layer_index'])
    if issues:
        for row in records:
            row.update(visibility='uncertain', reason='incomplete-recorded-view', changed_by=None)
    else:
        for row in records:
            if row['visibility'] != 'visible':
                continue
            archive = archive_refs.get((row['layer_index'], row['member_index']))
            if archive is None:
                continue
            inspection = archive.get('inspection')
            if not isinstance(inspection, dict) or not isinstance(inspection.get('archives'), list):
                raise VisibilityError('invalid nested Java inventory')
            for container in inspection['archives']:
                if not isinstance(container, dict) or not isinstance(container.get('identity'), dict):
                    raise VisibilityError('invalid nested Java identity')
                values = container['identity'].get('candidates', [])
                if not isinstance(values, list):
                    raise VisibilityError('invalid Java metadata candidates')
                for value in values:
                    if len(candidates) >= limits.max_members:
                        raise VisibilityError('Java candidate limit exceeded')
                    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) for k in ('groupId', 'artifactId', 'version')):
                        raise VisibilityError('invalid Java metadata coordinate')
                    candidates.append(dict(path=row['path'], layer_index=row['layer_index'],
                        member_index=row['member_index'], archive_id=container.get('archive_id'),
                        entry_chain=copy.deepcopy(container.get('entry_chain')), coordinates=copy.deepcopy(value),
                        realm='unknown', identity_basis='metadata-candidate',
                        limitations=list(_LIMITATIONS)))
    result = dict(schema='mdi.oci-visibility/1', selected_manifest=selected, realm='unknown',
                  entries=records, java_candidates=candidates, issues=issues,
                  coverage=dict(status='partial' if issues else 'complete-within-recorded-layers',
                                merged_filesystem=False, definitive_absence=False,
                                layers=len(layers), members=member_count, comparisons=comparisons,
                                limits=asdict(limits), limitations=list(_LIMITATIONS)))
    _bounded(result, limits, limits.max_output_bytes)
    return result

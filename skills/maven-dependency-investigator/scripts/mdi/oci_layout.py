"""Bounded offline OCI-layout layer inventory, not a merged container filesystem.

Format grounding: OCI image-spec v1.1.1 image-layout.md, image-index.md,
manifest.md, descriptor.md and layer.md (official opencontainers/image-spec
sources read 2026-09-12). Supported layout marker is 1.0.0; index/manifest
schemaVersion is 2. Only local sha256 blobs are opened. Descriptor URLs and
embedded data are never fetched or used as fallbacks. Unknown media types are
reported as unsupported coverage, never interpreted as images or absence.

This first slice inventories one unambiguously selected image manifest, possibly
reached through nested indexes. A digest explicitly selects a reachable image;
multiple distinct manifests without selection yield an ambiguity, not first-wins.
Descriptor platform claims and config platform fields are recorded separately;
conflicts refuse that image. Config command/environment fields are not returned
or executed. The layout may contain unrelated files; they are left untouched.

Each tar/tar.gz layer is inspected independently using distributions.py, in
manifest order, explicitly enabling the reader's OCI-whiteout context. Only
safe zero-length regular markers are recorded, before any JAR-suffix handling;
malformed markers remain partial. Whiteouts are observed but NOT applied. Even without whiteouts,
replacements, links and lower-layer semantics require a separate composition
stage. All findings therefore concern layer blobs, not final packaged presence.
Component realm is unknown. Synthetic fixtures validate this implementation;
no external real-world OCI layout has been verified by this slice, so coverage
is explicitly provisional rather than a blanket format-support claim.

Opened descriptors are anchored to a nofollow layout root; digest-derived names
cannot choose another path. Full blob size/digest and stat identity are checked
before parsing. Named identities are checked again at completion; changes clear
derived observations. The path API also rechecks the named root. The descriptor
API uses a caller-opened read-only root instead and never reopens its logical
locator; root rename/replacement does not redirect captured-file checks. It
preserves the caller's descriptor and directory offset. Containment of that
root descriptor remains the caller's responsibility. These are best-effort
checks, not a same-user snapshot.
The total referenced-input size, metadata, descriptor, layer, expanded-data and
retained-result budgets are bounded across the inventory. Repeated references
consume the input budget again; hashing and layer parsing read blobs separately,
so the input-size budget is not a bound on all kernel IO. The inventory budget
covers manifest choices and layer records, not the final wrapper or temporary
JSON allocations; a supervising worker must also cap memory and serialized
responses. Deadlines are cooperative between IO/decompression calls;
a supervising worker is required for hard cancellation. Scratch must be private,
outside both layout and checkout, and exclusively owned by the caller. There is
no scratch quota registry, pruning, container launch, image pull or network IO.

Cleanup obligations have their own top-level ledger, separate from the bounded
layer inventory and identity evidence. Lower-reader cleanup metadata is copied
immediately on return or exception, before source verification or inventory
retention. Up to 31 bounded path/reason records plus one scratch-root recovery
record are retained independently of max_inventory_bytes. Saturation stops new
layer work and marks individual_paths_complete false; the root is a recovery
boundary, not authorization to delete it. Interruptions carry this ledger in
oci_cleanup, even when all derived image/layer identities are discarded.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from mdi.discover import open_checkout_fd
from mdi.distributions import DistributionLimits, inspect_distribution_fd

INDEX = 'application/vnd.oci.image.index.v1+json'
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
_LAYER_FORMATS = {
    'application/vnd.oci.image.layer.v1.tar': 'tar',
    'application/vnd.oci.image.layer.v1.tar+gzip': 'tar.gz',
    'application/vnd.oci.image.layer.nondistributable.v1.tar': 'tar',
    'application/vnd.oci.image.layer.nondistributable.v1.tar+gzip': 'tar.gz',
}
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
_CHUNK = 65536
_MAX_CLEANUP_PATHS = 32
_MAX_CLEANUP_TEXT = 4096


class _CleanupLedger:
    """Recovery obligations survive inventory rejection and identity invalidation.

    Overflow or an explicitly incomplete lower ledger preserves the scratch root
    as an unverified recovery boundary and stops further layer work. Known paths
    remain recorded. The ledger never grants permission to delete it.
    """
    def __init__(self, scratch):
        self.scratch = scratch
        self.records = []
        self.saturated = False

    def collect(self, metadata):
        if self.saturated:
            return
        rows = metadata.get('leftovers') if isinstance(metadata, dict) else None
        if not isinstance(rows, list):
            self._overflow()
            return
        complete = metadata.get('individual_paths_complete', True) is True
        for row in rows:
            if not isinstance(row, dict):
                self._overflow()
                break
            path, reason = row.get('path'), row.get('reason')
            if (not isinstance(path, str) or not path or len(path) > _MAX_CLEANUP_TEXT
                    or not isinstance(reason, str) or not reason or len(reason) > _MAX_CLEANUP_TEXT):
                self._overflow()
                break
            record = {'path': path, 'reason': reason, 'identity_verified': False}
            if record in self.records:
                continue
            # Reserve one slot for the recovery boundary if details overflow.
            if len(self.records) >= _MAX_CLEANUP_PATHS - 1:
                self._overflow()
                break
            self.records.append(record)
        if not complete:
            self._overflow()

    def _overflow(self):
        if not self.saturated:
            self.records.append({'path': self.scratch, 'reason': 'cleanup-ledger-limit',
                                 'identity_verified': False})
            self.saturated = True

    def result(self):
        return {'status': 'retained' if self.records else 'complete',
                'individual_paths_complete': not self.saturated,
                'leftovers': [dict(record) for record in self.records]}


@dataclass(frozen=True)
class OCILimits:
    max_json_bytes: int = 4 * 1024**2
    max_json_nodes: int = 100000
    max_json_depth: int = 32
    max_text: int = 4096
    max_descriptors: int = 256
    max_index_depth: int = 4
    max_layers: int = 64
    max_blob_bytes: int = 2 * 1024**3
    max_total_blob_bytes: int = 2 * 1024**3
    max_expanded_bytes: int = 2 * 1024**3
    max_members: int = 20000
    max_archives: int = 32
    max_zip_entries: int = 20000
    max_member_bytes: int = 256 * 1024**2
    max_inventory_bytes: int = 24 * 1024**2

    def __post_init__(self):
        for name, value in asdict(self).items():
            minimum = 0 if name == 'max_index_depth' else 1
            if type(value) is not int or value < minimum:
                raise ValueError('OCI limits must be bounded nonnegative depth and positive integers')
        if self.max_json_depth > 64 or self.max_index_depth > 8:
            raise ValueError('OCI JSON/index depth exceeds supported ceiling')


class OCIError(ValueError):
    """An invalid invocation, not evidence of an absent image."""


class _Stop(Exception):
    def __init__(self, code):
        self.code = code


def _close(fd):
    primary = sys.exception()
    try:
        os.close(fd)
    except BaseException:
        if primary is None:
            raise
        primary.add_note('OCI descriptor closure unconfirmed')


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _path(value):
    try:
        value = os.fspath(value)
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ValueError()
        value = os.path.abspath(value)
        value.encode('utf-8')
        if len(value) > 4096:
            raise ValueError()
        return value
    except (TypeError, ValueError, UnicodeError):
        raise OCIError('paths must be bounded UTF-8 text') from None


def _no_overlap(first, second):
    for a, b in ((first, second), (os.path.realpath(first), os.path.realpath(second))):
        # Normalize the POSIX double-slash alias for component comparisons.
        a, b = '/' + a.lstrip('/'), '/' + b.lstrip('/')
        if os.path.commonpath((a, b)) in (a, b):
            raise OCIError('scratch must not overlap layout or checkout')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Stop('invalid-json')
        result[key] = value
    return result


def _constant(_value):
    raise _Stop('invalid-json')


def _json(data, limits):
    # Bytes are already capped. This cheap pass prevents deeply nested input
    # reaching json's recursive decoder; quotes/escapes never contribute depth.
    quoted = escaped = False
    depth = 0
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > limits.max_json_depth:
                raise _Stop('invalid-json')
        elif byte in (93, 125):
            depth -= 1
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise _Stop('invalid-json') from None
    stack = [value]
    count = 0
    while stack:
        item = stack.pop()
        count += 1
        if count > limits.max_json_nodes:
            raise _Stop('json-node-limit')
        if isinstance(item, str):
            if len(item) > limits.max_text:
                raise _Stop('json-text-limit')
            try:
                item.encode('utf-8')
            except UnicodeError:
                raise _Stop('invalid-json') from None
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise _Stop('invalid-json')
    if not isinstance(value, dict):
        raise _Stop('invalid-json')
    return value


def _schema(document, media):
    if type(document.get('schemaVersion')) is not int or document['schemaVersion'] != 2:
        raise _Stop('unsupported-schema-version')
    if document.get('mediaType', media) != media:
        raise _Stop('media-type-conflict')


def _descriptor(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('mediaType'), str) or not raw['mediaType']:
        raise _Stop('invalid-descriptor')
    if not isinstance(raw.get('digest'), str) or not _DIGEST.fullmatch(raw['digest']):
        raise _Stop('unsupported-or-invalid-digest')
    if type(raw.get('size')) is not int or raw['size'] < 0:
        raise _Stop('invalid-descriptor')
    result = {key: raw[key] for key in ('mediaType', 'digest', 'size')}
    if 'platform' in raw:
        p = raw['platform']
        if not isinstance(p, dict) or any(not isinstance(p.get(k), str) or not p[k]
                                          for k in ('architecture', 'os')):
            raise _Stop('invalid-platform')
        result['platform'] = {k: p[k] for k in ('architecture', 'os', 'variant', 'os.version') if k in p}
        if any(not isinstance(v, str) or not v for v in result['platform'].values()):
            raise _Stop('invalid-platform')
    return result


class _Layout:
    def __init__(self, fd, path, limits, deadline):
        self.fd, self.path, self.limits, self.deadline = fd, path, limits, deadline
        self.identities = {}
        self.captures = {}
        self.total_bytes = self.descriptors = 0
        self.expanded = self.members = self.archives = self.zip_entries = 0
        self.retained = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise _Stop('deadline-exceeded')

    def opened(self, parts):
        parent = os.dup(self.fd)
        leaf = None
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=parent)
                old, parent = parent, child
                _close(old)
            leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            old, parent = parent, None
            _close(old)
            result, leaf = leaf, None
            return result
        except OSError:
            raise _Stop('file-unavailable') from None
        finally:
            primary = sys.exception()
            for owned in (leaf, parent):
                if owned is not None:
                    try:
                        os.close(owned)
                    except BaseException as exc:
                        if primary is None:
                            primary = exc
                        else:
                            primary.add_note('OCI path descriptor closure unconfirmed')
            if primary is not None and sys.exception() is None:
                raise primary

    def read(self, parts, descriptor=None, metadata=True):
        self.check()
        fd = self.opened(parts)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise _Stop('not-a-regular-file')
            size = before.st_size
            cap = self.limits.max_json_bytes if metadata else self.limits.max_blob_bytes
            if size > cap:
                raise _Stop('json-byte-limit' if metadata else 'blob-byte-limit')
            if descriptor is not None and size != descriptor['size']:
                raise _Stop('blob-size-mismatch')
            if self.total_bytes + size > self.limits.max_total_blob_bytes:
                raise _Stop('total-input-limit')
            self.total_bytes += size
            digest = hashlib.sha256()
            pieces = [] if metadata else None
            offset = 0
            while offset < size:
                self.check()
                block = os.pread(fd, min(_CHUNK, size-offset), offset)
                if not block:
                    raise _Stop('source-changed')
                offset += len(block)
                digest.update(block)
                if pieces is not None:
                    pieces.append(block)
            if _identity(os.fstat(fd)) != _identity(before):
                raise _Stop('source-changed')
            if descriptor is not None and 'sha256:' + digest.hexdigest() != descriptor['digest']:
                raise _Stop('blob-digest-mismatch')
            key = tuple(parts)
            if key in self.identities and self.identities[key] != _identity(before):
                raise _Stop('source-changed')
            self.identities[key] = _identity(before)
            self.captures[key] = {'path': '/'.join(parts), 'sha256': digest.hexdigest(), 'size': size}
            if metadata:
                return _json(b''.join(pieces), self.limits)
            result, fd = fd, None
            return result
        finally:
            if fd is not None:
                _close(fd)

    def blob(self, descriptor, metadata=True):
        return self.read(('blobs', 'sha256', descriptor['digest'][7:]), descriptor, metadata)

    def counted(self, raw):
        self.check()
        self.descriptors += 1
        if self.descriptors > self.limits.max_descriptors:
            raise _Stop('descriptor-limit')
        return _descriptor(raw)

    def retain(self, value):
        # A bounded distribution already limits each retained string/object.
        # Do not concatenate the whole serialized result to check its size.
        for text in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(value):
            self.retained += len(text)
            if self.retained > self.limits.max_inventory_bytes:
                raise _Stop('inventory-limit')

    def unchanged(self):
        try:
            for parts, identity in self.identities.items():
                self.check()
                fd = self.opened(parts)
                try:
                    if _identity(os.fstat(fd)) != identity:
                        return False
                finally:
                    _close(fd)
        except (OSError, _Stop):
            return False
        return True


def _manifests(state, index, route=(), depth=0):
    state.check()
    if depth > state.limits.max_index_depth:
        raise _Stop('index-depth-limit')
    _schema(index, INDEX)
    items = index.get('manifests')
    if not isinstance(items, list):
        raise _Stop('invalid-index')
    found = []
    for position, raw in enumerate(items):
        descriptor = state.counted(raw)
        path = (*route, position)
        if descriptor['mediaType'] == MANIFEST:
            found.append({'descriptor': descriptor, 'routes': [list(path)]})
        elif descriptor['mediaType'] == INDEX:
            nested = state.blob(descriptor)
            found.extend(_manifests(state, nested, (*path, descriptor['digest']), depth+1))
        else:
            # It is valid for an OCI index to contain auxiliary descriptors;
            # preserve the unsupported media type without opening its content.
            found.append({'unsupported': descriptor['mediaType'], 'route': list(path)})
    return found


def _platform(config):
    result = {}
    for name in ('architecture', 'os', 'variant', 'os.version'):
        if name in config:
            if not isinstance(config[name], str) or not config[name]:
                raise _Stop('invalid-platform')
            result[name] = config[name]
    if not all(name in result for name in ('architecture', 'os')):
        raise _Stop('invalid-platform')
    return result


def _layer(state, descriptor, index, checkout, scratch, cleanup):
    format = _LAYER_FORMATS.get(descriptor['mediaType'])
    if format is None:
        raise _Stop('unsupported-layer-media-type')
    left = {
        'max_expanded_bytes': state.limits.max_expanded_bytes - state.expanded,
        'max_members': state.limits.max_members - state.members,
        'max_archives': state.limits.max_archives - state.archives,
        'max_zip_entries': state.limits.max_zip_entries - state.zip_entries,
    }
    if any(v <= 0 for v in left.values()):
        raise _Stop('expanded-limit' if left['max_expanded_bytes'] <= 0 else 'layer-inventory-limit')
    fd = state.blob(descriptor, metadata=False)
    try:
        before = _identity(os.fstat(fd))
        try:
            distribution = inspect_distribution_fd(
                fd, source_path=os.path.join(state.path, 'blobs', 'sha256', descriptor['digest'][7:]),
                checkout=checkout, scratch_base=scratch, format=format, deadline=state.deadline,
                oci_whiteouts=True,
                limits=DistributionLimits(max_input_bytes=state.limits.max_blob_bytes,
                    max_member_bytes=state.limits.max_member_bytes,
                    max_inventory_bytes=state.limits.max_inventory_bytes,
                    max_text=state.limits.max_text, **left))
        except BaseException as exc:
            metadata = getattr(exc, 'distribution_cleanup', None)
            if metadata is not None:
                cleanup.collect(metadata)
            else:
                # An interruption may precede the reader publishing its ledger.
                cleanup._overflow()
            raise
        cleanup.collect(distribution.get('coverage', {}).get('cleanup'))
        if cleanup.saturated:
            raise _Stop('cleanup-ledger-limit')
        if _identity(os.fstat(fd)) != before or distribution['source']['sha256'] != descriptor['digest'][7:]:
            raise _Stop('source-changed')
    finally:
        _close(fd)
    state.expanded += distribution['coverage']['expanded_bytes']
    state.members += len(distribution['members'])
    state.archives += len(distribution['archives'])
    state.zip_entries += distribution['coverage']['zip_entries']
    whiteouts = []
    for member in distribution['members']:
        name = member['name']
        if 'whiteout' in member:
            whiteouts.append({'member_index': member['index'], 'name': name,
                              **member['whiteout']})
    return {'index': index, **descriptor, 'realm': 'unknown', 'distribution': distribution,
            'whiteouts': whiteouts, 'presence_scope': 'layer-blob-only'}


def inspect_oci_layout(layout, *, checkout, scratch_base, deadline, manifest_digest=None,
                       limits=OCILimits()):
    """Inventory local verified layer blobs; output is always non-merged, provisional.

    A manifest digest selects only a descriptor reachable from index.json. No
    implicit architecture/platform selection occurs. Invalid filesystem or OCI
    data returns partial coverage and stable issue codes; invalid invocation
    raises OCIError. Scratch remains caller-owned and is never globally pruned.
    """
    return _inspect_oci_layout(layout, checkout=checkout, scratch_base=scratch_base,
                               deadline=deadline, manifest_digest=manifest_digest, limits=limits)


def inspect_oci_layout_fd(layout_fd, *, layout_path, checkout, scratch_base, deadline,
                          manifest_digest=None, limits=OCILimits()):
    """Inspect a caller-owned read-only directory descriptor, not its pathname.

    layout_path is a logical evidence locator and is never opened. The caller
    establishes descriptor containment before passing it here. Reads and final
    captured-file checks stay relative to that descriptor even if the locator is
    renamed or replaced. The descriptor's offset and lifetime remain caller-owned.
    Checkout and scratch paths retain their existing path-based contracts; this
    entry point does not make scratch lifetime descriptor-only or race-free.
    """
    if type(layout_fd) is not int or layout_fd < 0:
        raise OCIError('layout_fd must be a read-only directory descriptor')
    try:
        info = os.fstat(layout_fd)
        flags = fcntl.fcntl(layout_fd, fcntl.F_GETFL)
    except (OSError, OverflowError, ValueError):
        raise OCIError('layout_fd is unavailable') from None
    if (not stat.S_ISDIR(info.st_mode) or flags & os.O_ACCMODE != os.O_RDONLY
            or flags & getattr(os, 'O_PATH', 0)):
        raise OCIError('layout_fd must be a read-only directory descriptor')
    return _inspect_oci_layout(layout_path, checkout=checkout, scratch_base=scratch_base,
                               deadline=deadline, manifest_digest=manifest_digest, limits=limits,
                               layout_fd=layout_fd)


def _inspect_oci_layout(layout, *, checkout, scratch_base, deadline, manifest_digest, limits,
                        layout_fd=None):
    if not isinstance(limits, OCILimits):
        raise OCIError('limits must be OCILimits')
    try:
        finite = type(deadline) in (int, float) and math.isfinite(deadline)
    except OverflowError:
        finite = False
    if not finite:
        raise OCIError('deadline must be finite')
    if manifest_digest is not None and (not isinstance(manifest_digest, str) or not _DIGEST.fullmatch(manifest_digest)):
        raise OCIError('selection must be an exact lowercase sha256 digest')
    path, checkout, scratch = map(_path, (layout, checkout, scratch_base))
    _no_overlap(path, scratch)
    _no_overlap(checkout, scratch)
    result = {'source': {'path': path, 'observed_at': datetime.now(timezone.utc).isoformat()},
              'manifests': [], 'selected_manifest': None, 'config': None, 'layers': [], 'issues': [],
              'coverage': {'status': 'partial', 'scope': 'layer-inventory-only',
                  'merged_filesystem': False, 'format_support': 'provisional-synthetic-fixtures',
                  'limits': asdict(limits), 'input_bytes': 0, 'expanded_bytes': 0,
                  'limitations': ['layer changesets are not applied; final filesystem presence is unknown',
                      'component application/container attribution is unknown',
                      'config commands and environment are neither executed nor retained',
                      'local descriptor hashes prove bytes, not source authenticity or build provenance',
                      'deadline checks are cooperative; hard cancellation requires worker isolation',
                      'scratch quota and exclusive lifetime are caller responsibilities',
                      'same-user mutation checks are not an atomic layout snapshot']}}
    cleanup = _CleanupLedger(scratch)
    fd = scratch_fd = None
    state = None
    try:
        if time.monotonic() >= deadline:
            raise _Stop('deadline-exceeded')
        fd = open_checkout_fd(path) if layout_fd is None else os.dup(layout_fd)
        root_info = os.fstat(fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise _Stop('not-a-directory')
        root_identity = _identity(root_info)
        if layout_fd is not None:
            result['source']['binding'] = 'caller-opened-layout-descriptor'
            result['coverage']['limitations'].append(
                'layout_path is a logical locator; containment of the supplied root descriptor '
                'is caller-attested and its pathname is not reopened')
        scratch_fd = open_checkout_fd(scratch)
        info = os.fstat(scratch_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise _Stop('unsafe-scratch')
        state = _Layout(fd, path, limits, deadline)
        marker = state.read(('oci-layout',))
        if marker.get('imageLayoutVersion') != '1.0.0':
            raise _Stop('unsupported-layout-version')
        index = state.read(('index.json',))
        groups = {}
        for row in _manifests(state, index):
            if 'unsupported' in row:
                result['issues'].append({'code': 'unsupported-index-media-type', 'media_type': row['unsupported']})
                continue
            descriptor = row['descriptor']
            digest = descriptor['digest']
            if digest in groups:
                if groups[digest]['descriptor'] != descriptor:
                    raise _Stop('conflicting-descriptors')
                groups[digest]['routes'].extend(row['routes'])
            else:
                groups[digest] = row
        result['manifests'] = list(groups.values())
        state.retain(result['manifests'])
        if manifest_digest is None:
            if len(groups) != 1:
                raise _Stop('ambiguous-manifest' if groups else 'no-image-manifest')
            manifest_digest = next(iter(groups))
        if manifest_digest not in groups:
            raise _Stop('manifest-not-found')
        selected = groups[manifest_digest]['descriptor']
        manifest = state.blob(selected)
        _schema(manifest, MANIFEST)
        raw_layers = manifest.get('layers')
        if not isinstance(raw_layers, list):
            raise _Stop('invalid-manifest')
        if len(raw_layers) > limits.max_layers:
            raise _Stop('layer-limit')
        config_descriptor = state.counted(manifest.get('config'))
        if config_descriptor['mediaType'] != CONFIG:
            raise _Stop('unsupported-config-media-type')
        config = state.blob(config_descriptor)
        platform = _platform(config)
        declared = selected.get('platform', {})
        if any(platform.get(k) != v for k, v in declared.items()):
            raise _Stop('platform-conflict')
        result['selected_manifest'] = manifest_digest
        result['config'] = {**config_descriptor, 'platform': platform}
        for position, raw in enumerate(raw_layers):
            try:
                descriptor = state.counted(raw)
                layer = _layer(state, descriptor, position, checkout, scratch, cleanup)
                state.retain(layer)
                result['layers'].append(layer)
                if layer['whiteouts']:
                    result['issues'].append({'code': 'layer-whiteouts-not-applied', 'layer_index': position})
                if layer['distribution']['coverage']['status'] != 'complete':
                    result['issues'].append({'code': 'layer-incomplete', 'layer_index': position})
            except _Stop as exc:
                result['issues'].append({'code': exc.code, 'layer_index': position})
                break
        result['issues'].append({'code': 'layer-composition-not-performed'})
    except _Stop as exc:
        result['issues'].append({'code': exc.code})
    except (OSError, ValueError):
        result['issues'].append({'code': 'file-unavailable'})
    finally:
        primary = sys.exception()
        if state is not None:
            result['coverage']['input_bytes'] = state.total_bytes
            result['coverage']['expanded_bytes'] = state.expanded
            if primary is None:
                try:
                    unchanged = state.unchanged()
                    if layout_fd is None:
                        fresh = open_checkout_fd(path)
                        try:
                            unchanged = unchanged and _identity(os.fstat(fresh)) == root_identity
                        finally:
                            _close(fresh)
                    else:
                        # Renaming the held directory changes ctime, not the
                        # anchored contents. File identities above still use
                        # ctime; root membership changes are checked by mtime.
                        unchanged = unchanged and _identity(os.fstat(fd))[:-1] == root_identity[:-1]
                except OSError:
                    unchanged = False
                except BaseException as exc:
                    primary, unchanged = exc, False
                result['source']['captures'] = list(state.captures.values())
                result['source']['unchanged'] = unchanged
                if not unchanged:
                    result['selected_manifest'] = None
                    result['config'] = None
                    result['manifests'] = []
                    result['layers'] = []
                    result['issues'].append({'code': 'source-changed'})
        for owned in (scratch_fd, fd):
            if owned is not None:
                try:
                    os.close(owned)
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        primary.add_note('OCI descriptor closure unconfirmed')
        result['cleanup'] = cleanup.result()
        if cleanup.records:
            result['issues'].append({'code': 'scratch-retained'})
        if cleanup.saturated and not any(x['code'] == 'cleanup-ledger-limit' for x in result['issues']):
            result['issues'].append({'code': 'cleanup-ledger-limit'})
        if primary is not None:
            primary.oci_cleanup = cleanup.result()
            if sys.exception() is None:
                raise primary
    return result

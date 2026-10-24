"""Synthetic local OCI layouts; no image pulls or container execution."""
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from mdi.oci_layout import OCIError, OCILimits, inspect_oci_layout

INDEX = 'application/vnd.oci.image.index.v1+json'
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
TAR = 'application/vnd.oci.image.layer.v1.tar'


def tar_bytes(names=('lib/example.jar',), gzip=False):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz' if gzip else 'w', format=tarfile.USTAR_FORMAT) as archive:
        for name in names:
            data = b'' if '/.wh.' in name or name.startswith('.wh.') else b'not-a-zip'
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


class OCILayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.layout = self.root / 'image'
        (self.layout / 'blobs' / 'sha256').mkdir(parents=True)
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir(mode=0o700)
        (self.layout / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
        self.config = self.blob(CONFIG, json.dumps({'architecture': 'amd64', 'os': 'linux',
                                                  'config': {'Env': ['SECRET=never-copy']}}).encode())
        self.layer = self.blob(TAR, tar_bytes(('lib/plain.txt',)))
        self.manifest = self.image([self.layer])
        self.index([self.manifest])

    def blob(self, media, data):
        digest = hashlib.sha256(data).hexdigest()
        (self.layout / 'blobs' / 'sha256' / digest).write_bytes(data)
        return {'mediaType': media, 'digest': 'sha256:' + digest, 'size': len(data)}

    def image(self, layers, config=None):
        return self.blob(MANIFEST, json.dumps({'schemaVersion': 2, 'mediaType': MANIFEST,
                                             'config': config or self.config, 'layers': layers}).encode())

    def index(self, descriptors):
        (self.layout / 'index.json').write_text(json.dumps(
            {'schemaVersion': 2, 'mediaType': INDEX, 'manifests': descriptors}))

    def inspect(self, **kwargs):
        return inspect_oci_layout(self.layout, checkout=self.layout,
                                  scratch_base=self.scratch, deadline=time.monotonic()+10, **kwargs)

    def codes(self, result):
        return {x['code'] for x in result['issues']}

    def inspect_fd(self, fd, **kwargs):
        from mdi.oci_layout import inspect_oci_layout_fd
        return inspect_oci_layout_fd(fd, layout_path=self.layout, checkout=self.layout,
                                     scratch_base=self.scratch, deadline=time.monotonic()+10,
                                     **kwargs)

    def test_descriptor_inspection_preserves_caller_offset_and_ownership(self):
        fd = os.open(self.layout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            result = self.inspect_fd(fd)
            self.assertEqual(result['selected_manifest'], self.manifest['digest'])
            self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
            os.fstat(fd)
            self.assertNotIn('never-copy', json.dumps(result))
            self.assertEqual(result['cleanup']['status'], 'complete')
        finally:
            os.close(fd)

    def test_descriptor_uses_original_layout_after_path_replacement(self):
        from mdi import oci_layout
        fd = os.open(self.layout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        original_read = oci_layout._Layout.read
        moved = self.root / 'original-image'
        changed = False
        def replace_then_read(state, *args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                self.layout.rename(moved)
                self.layout.mkdir()
                (self.layout / 'oci-layout').write_text('{"imageLayoutVersion":"WRONG"}')
            return original_read(state, *args, **kwargs)
        try:
            with mock.patch.object(oci_layout._Layout, 'read', replace_then_read):
                result = self.inspect_fd(fd)
            self.assertEqual(result['selected_manifest'], self.manifest['digest'])
            self.assertEqual(result['layers'][0]['digest'], self.layer['digest'])
            self.assertTrue(result['source']['unchanged'])
            self.assertNotIn('WRONG', json.dumps(result))
            self.assertEqual(os.fstat(fd).st_ino, moved.stat().st_ino)
        finally:
            os.close(fd)

    def test_descriptor_logical_path_is_not_opened(self):
        from mdi.oci_layout import inspect_oci_layout_fd
        fd = os.open(self.layout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        alias = self.root / 'logical-alias'
        alias.symlink_to(self.layout, target_is_directory=True)
        try:
            result = inspect_oci_layout_fd(
                fd, layout_path=alias, checkout=self.layout, scratch_base=self.scratch,
                deadline=time.monotonic()+10)
            self.assertEqual(result['selected_manifest'], self.manifest['digest'])
            self.assertEqual(result['source']['path'], str(alias))
            self.assertIn('file-unavailable', self.codes(inspect_oci_layout(
                alias, checkout=self.layout, scratch_base=self.scratch,
                deadline=time.monotonic()+10)))
        finally:
            os.close(fd)

    def test_descriptor_rejects_non_directory_and_invalid_handles(self):
        fd = os.open(self.layout / 'index.json', os.O_RDONLY)
        try:
            for bad in (fd, -1, True, '1', 2**100):
                with self.subTest(fd=bad), self.assertRaises(OCIError):
                    self.inspect_fd(bad)
            os.fstat(fd)
        finally:
            os.close(fd)

    def test_descriptor_cleanup_ledger_survives_inventory_rejection(self):
        from mdi import distributions
        self.index([self.image([self._jar_layer()])])
        fd = os.open(self.layout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with mock.patch.object(distributions.os, 'unlink', side_effect=OSError('refuse')):
                result = self.inspect_fd(fd, limits=OCILimits(max_inventory_bytes=1000))
            self.assertEqual(result['layers'], [])
            self.assertTrue(result['cleanup']['leftovers'])
            self.assertEqual({Path(row['path']) for row in result['cleanup']['leftovers']},
                             set(self.scratch.iterdir()))
            os.fstat(fd)
        finally:
            os.close(fd)

    def test_descriptor_rejects_path_only_handle(self):
        if not hasattr(os, 'O_PATH'):
            self.skipTest('O_PATH is not provided on this platform')
        fd = os.open(self.layout, os.O_PATH | os.O_DIRECTORY)
        try:
            with self.assertRaises(OCIError):
                self.inspect_fd(fd)
            os.fstat(fd)
        finally:
            os.close(fd)

    def test_descriptor_source_mutation_still_invalidates_identity(self):
        from mdi import oci_layout
        fd = os.open(self.layout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        original = oci_layout.inspect_distribution_fd
        def changed(source, **kwargs):
            result = original(source, **kwargs)
            path = self.layout / 'blobs' / 'sha256' / self.layer['digest'][7:]
            with path.open('ab') as stream:
                stream.write(b'changed')
            return result
        try:
            with mock.patch.object(oci_layout, 'inspect_distribution_fd', side_effect=changed):
                result = self.inspect_fd(fd)
            self.assertIn('source-changed', self.codes(result))
            self.assertEqual(result['layers'], [])
            self.assertFalse(result['source']['unchanged'])
            os.fstat(fd)
        finally:
            os.close(fd)

    def test_descriptor_final_identity_interrupt_preserves_caller_and_cleanup(self):
        from mdi import oci_layout
        fd = os.open(self.layout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = set(os.listdir('/proc/self/fd'))
        try:
            error = KeyboardInterrupt('final check')
            with mock.patch.object(oci_layout._Layout, 'unchanged', side_effect=error):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.inspect_fd(fd)
            self.assertIs(caught.exception, error)
            self.assertIn('leftovers', caught.exception.oci_cleanup)
            os.fstat(fd)
            self.assertEqual(set(os.listdir('/proc/self/fd')), before)
        finally:
            os.close(fd)

    def test_descriptor_interrupt_retains_cleanup_and_closes_only_owned_handles(self):
        from mdi import oci_layout
        fd = os.open(self.layout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = set(os.listdir('/proc/self/fd'))
        try:
            error = KeyboardInterrupt('stop')
            with mock.patch.object(oci_layout, 'inspect_distribution_fd', side_effect=error):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.inspect_fd(fd)
            self.assertIs(caught.exception, error)
            self.assertEqual(caught.exception.oci_cleanup['status'], 'retained')
            os.fstat(fd)
            self.assertEqual(set(os.listdir('/proc/self/fd')), before)
        finally:
            os.close(fd)

    def test_single_manifest_inventory_is_not_merged_image(self):
        r = self.inspect()
        self.assertEqual(r['selected_manifest'], self.manifest['digest'])
        self.assertEqual(r['layers'][0]['distribution']['members'][0]['name'], 'lib/plain.txt')
        self.assertEqual(r['layers'][0]['digest'], self.layer['digest'])
        self.assertEqual(r['config']['platform'], {'architecture': 'amd64', 'os': 'linux'})
        self.assertFalse(r['coverage']['merged_filesystem'])
        self.assertEqual(r['coverage']['status'], 'partial')
        self.assertNotIn('never-copy', json.dumps(r))
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_real_nested_jar_metadata_is_layer_candidate_only(self):
        import zipfile
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('META-INF/maven/example/lib/pom.properties',
                             'groupId=example\nartifactId=lib\nversion=1\n')
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo('app/lib.jar')
            info.size = len(jar.getvalue())
            archive.addfile(info, io.BytesIO(jar.getvalue()))
        self.index([self.image([self.blob(TAR, payload.getvalue())])])
        r = self.inspect()
        self.assertEqual(r['layers'][0]['presence_scope'], 'layer-blob-only')
        self.assertEqual(r['layers'][0]['realm'], 'unknown')
        observed = r['layers'][0]['distribution']['archives']
        self.assertEqual(len(observed), 1)
        self.assertIn('lib', json.dumps(observed))
        self.assertFalse(r['coverage']['merged_filesystem'])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_repeated_invocations_do_not_leak_descriptors(self):
        before = set(os.listdir('/proc/self/fd'))
        for _ in range(5):
            self.inspect()
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_final_identity_interrupt_closes_layout_descriptors(self):
        from mdi import oci_layout
        before = set(os.listdir('/proc/self/fd'))
        with mock.patch.object(oci_layout._Layout, 'unchanged', side_effect=KeyboardInterrupt('stop')):
            with self.assertRaises(KeyboardInterrupt):
                self.inspect()
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_path_parent_close_failure_releases_unreturned_leaf(self):
        from mdi import oci_layout
        before = set(os.listdir('/proc/self/fd'))
        original = oci_layout._close
        def closed_then_failed(fd):
            original(fd)
            raise OSError('close unconfirmed')
        with mock.patch.object(oci_layout, '_close', side_effect=closed_then_failed):
            r = self.inspect()
        self.assertIn('file-unavailable', self.codes(r))
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def _jar_layer(self):
        import zipfile
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('x.txt', b'x')
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo('app/lib.jar')
            info.size = len(jar.getvalue())
            archive.addfile(info, io.BytesIO(jar.getvalue()))
        return self.blob(TAR, payload.getvalue())

    def test_cleanup_survives_inventory_limit_before_layer_retention(self):
        from mdi import distributions
        self.index([self.image([self._jar_layer()])])
        with mock.patch.object(distributions.os, 'unlink', side_effect=OSError('refuse')):
            result = self.inspect(limits=OCILimits(max_inventory_bytes=1000))
        self.assertEqual(result['layers'], [])
        leftovers = result['cleanup']['leftovers']
        self.assertTrue(leftovers)
        self.assertEqual(result['cleanup']['status'], 'retained')
        self.assertEqual({Path(x['path']) for x in leftovers}, set(self.scratch.iterdir()))

    def test_cleanup_survives_source_invalidation(self):
        from mdi import distributions, oci_layout
        layer = self._jar_layer()
        self.index([self.image([layer])])
        original = oci_layout.inspect_distribution_fd
        def changed(fd, **kwargs):
            result = original(fd, **kwargs)
            path = self.layout / 'blobs' / 'sha256' / layer['digest'][7:]
            with path.open('ab') as stream:
                stream.write(b'changed')
            return result
        with mock.patch.object(distributions.os, 'unlink', side_effect=OSError('refuse')):
            with mock.patch.object(oci_layout, 'inspect_distribution_fd', side_effect=changed):
                result = self.inspect()
        self.assertEqual(result['layers'], [])
        self.assertIn('source-changed', self.codes(result))
        self.assertEqual({Path(x['path']) for x in result['cleanup']['leftovers']},
                         set(self.scratch.iterdir()))
        self.assertTrue(result['cleanup']['leftovers'])

    def test_cleanup_exception_metadata_survives_interrupt(self):
        from mdi import oci_layout
        error = KeyboardInterrupt('stop')
        retained = self.scratch / 'retained'
        retained.write_bytes(b'owned-test-file')
        error.distribution_cleanup = {'leftovers': [{'path': str(retained), 'reason': 'interrupted'}]}
        with mock.patch.object(oci_layout, 'inspect_distribution_fd', side_effect=error):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.inspect()
        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.oci_cleanup['leftovers'][0]['path'], str(retained))

    def test_incomplete_lower_cleanup_flag_survives_oci_collection(self):
        from mdi import oci_layout
        self.index([self.image([self.layer, self.layer])])
        original = oci_layout.inspect_distribution_fd
        known = str(self.scratch / 'known-retained')
        calls = []

        def incomplete(fd, **kwargs):
            calls.append(fd)
            result = original(fd, **kwargs)
            result['coverage']['cleanup'] = {
                'leftovers': [{'path': known, 'reason': 'retained'},
                              {'path': str(self.scratch), 'reason': 'nested-cleanup-ledger-unconfirmed'}],
                'individual_paths_complete': False,
            }
            return result

        with mock.patch.object(oci_layout, 'inspect_distribution_fd', side_effect=incomplete):
            result = self.inspect()
        self.assertFalse(result['cleanup']['individual_paths_complete'])
        self.assertEqual(len(calls), 1)
        self.assertIn(known, [x['path'] for x in result['cleanup']['leftovers']])
        self.assertIn(str(self.scratch), [x['path'] for x in result['cleanup']['leftovers']])

    def test_incomplete_lower_exception_cleanup_flag_survives_oci(self):
        from mdi import oci_layout
        error = KeyboardInterrupt('interrupted')
        error.distribution_cleanup = {
            'leftovers': [{'path': str(self.scratch), 'reason': 'nested-cleanup-ledger-unconfirmed'}],
            'individual_paths_complete': False,
        }
        with mock.patch.object(oci_layout, 'inspect_distribution_fd', side_effect=error):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.inspect()
        self.assertIs(caught.exception, error)
        self.assertFalse(error.oci_cleanup['individual_paths_complete'])
        self.assertIn(str(self.scratch), [x['path'] for x in error.oci_cleanup['leftovers']])

    def test_cleanup_saturation_retains_recovery_root_and_stops_layers(self):
        from mdi import oci_layout
        self.index([self.image([self.layer, self.layer])])
        original = oci_layout.inspect_distribution_fd
        calls = []
        def retained(fd, **kwargs):
            calls.append(fd)
            result = original(fd, **kwargs)
            result['coverage']['cleanup']['leftovers'] = [
                {'path': str(self.scratch / ('entry-' + str(i))), 'reason': 'retained'}
                for i in range(100)]
            return result
        with mock.patch.object(oci_layout, 'inspect_distribution_fd', side_effect=retained):
            result = self.inspect()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(result['cleanup']['leftovers']), 32)
        self.assertFalse(result['cleanup']['individual_paths_complete'])
        self.assertIn(str(self.scratch), [x['path'] for x in result['cleanup']['leftovers']])
        self.assertIn('cleanup-ledger-limit', self.codes(result))

    def test_ambiguous_index_never_selects_first(self):
        other = self.image([self.blob(TAR, tar_bytes(('second.txt',)))])
        self.index([self.manifest, other])
        r = self.inspect()
        self.assertIsNone(r['selected_manifest'])
        self.assertEqual(r['layers'], [])
        self.assertIn('ambiguous-manifest', self.codes(r))
        selected = self.inspect(manifest_digest=other['digest'])
        self.assertEqual(selected['selected_manifest'], other['digest'])

    def test_nested_index_and_duplicate_routes(self):
        nested = self.blob(INDEX, json.dumps({'schemaVersion': 2, 'manifests': [self.manifest]}).encode())
        self.index([nested, self.manifest])
        r = self.inspect()
        self.assertEqual(len(r['manifests']), 1)
        self.assertEqual(len(r['manifests'][0]['routes']), 2)
        self.assertEqual(r['selected_manifest'], self.manifest['digest'])

    def test_whiteouts_remain_layer_observations(self):
        first = self.blob(TAR, tar_bytes(('lib/a.txt',)))
        second = self.blob(TAR, tar_bytes(('lib/.wh.a.txt', 'lib/.wh..wh..opq')))
        self.index([self.image([first, second])])
        r = self.inspect()
        self.assertIn('layer-whiteouts-not-applied', self.codes(r))
        self.assertEqual([x['kind'] for x in r['layers'][1]['whiteouts']], ['remove', 'opaque'])
        self.assertEqual(r['layers'][0]['distribution']['members'][0]['name'], 'lib/a.txt')
        self.assertFalse(r['coverage']['merged_filesystem'])

    def test_whiteout_jar_name_does_not_become_empty_nested_jar(self):
        self.index([self.image([self.blob(TAR, tar_bytes(('lib/.wh.b.jar',)))])])
        result = self.inspect()
        layer = result['layers'][0]
        self.assertEqual(layer['distribution']['coverage']['status'], 'complete')
        self.assertEqual(layer['distribution']['archives'], [])
        self.assertEqual(layer['whiteouts'], [{'member_index': 0, 'name': 'lib/.wh.b.jar',
                                              'kind': 'remove', 'target': 'lib/b.jar'}])
        self.assertNotIn('layer-incomplete', self.codes(result))
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertFalse(result['coverage']['merged_filesystem'])

    def test_malformed_whiteout_does_not_gain_deletion_semantics(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo('lib/.wh.b.jar')
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
        self.index([self.image([self.blob(TAR, payload.getvalue())])])
        result = self.inspect()
        self.assertIn('layer-incomplete', self.codes(result))
        self.assertEqual(result['layers'][0]['whiteouts'], [])

    def test_gzip_layer_and_extra_layout_files(self):
        layer = self.blob(TAR+'+gzip', tar_bytes(('plain.txt',), gzip=True))
        self.index([self.image([layer])])
        (self.layout / 'unrelated').write_text('leave me')
        self.assertEqual(len(self.inspect()['layers']), 1)
        self.assertEqual((self.layout / 'unrelated').read_text(), 'leave me')

    def test_bad_blob_digest_never_parsed(self):
        path = self.layout / 'blobs' / 'sha256' / self.layer['digest'][7:]
        path.write_bytes(b'x' * self.layer['size'])
        r = self.inspect()
        self.assertIn('blob-digest-mismatch', self.codes(r))
        self.assertEqual(r['layers'], [])

    def test_missing_blob_never_fetched(self):
        (self.layout / 'blobs' / 'sha256' / self.layer['digest'][7:]).unlink()
        r = self.inspect()
        self.assertIn('file-unavailable', self.codes(r))
        self.assertEqual(r['layers'], [])

    def test_size_mismatch(self):
        self.index([dict(self.manifest, size=self.manifest['size']+1)])
        self.assertIn('blob-size-mismatch', self.codes(self.inspect()))

    def test_symlink_blob_and_ancestor_refused(self):
        path = self.layout / 'blobs' / 'sha256' / self.layer['digest'][7:]
        external = self.root / 'external'
        path.rename(external)
        path.symlink_to(external)
        self.assertIn('file-unavailable', self.codes(self.inspect()))
        path.unlink()
        external.rename(path)
        original = self.layout / 'blobs'
        original.rename(self.layout / 'real-blobs')
        original.symlink_to(self.layout / 'real-blobs', target_is_directory=True)
        self.assertIn('file-unavailable', self.codes(self.inspect()))

    def test_digest_path_injection_rejected(self):
        for digest in ('sha256:../../secret', 'sha512:'+'a'*128, 'sha256:'+'A'*64):
            with self.subTest(digest=digest):
                self.index([dict(self.manifest, digest=digest)])
                r = self.inspect()
                self.assertEqual(r['layers'], [])
                self.assertIn('unsupported-or-invalid-digest', self.codes(r))

    def test_foreign_media_and_urls_not_followed(self):
        foreign = dict(self.layer, mediaType='application/unknown', urls=['https://invalid.example/secret'])
        self.index([self.image([foreign])])
        r = self.inspect()
        self.assertIn('unsupported-layer-media-type', self.codes(r))
        self.assertEqual(r['layers'], [])
        self.assertNotIn('invalid.example', json.dumps(r))

    def test_config_digest_failure_prevents_layer_claims(self):
        path = self.layout / 'blobs' / 'sha256' / self.config['digest'][7:]
        path.write_bytes(b'x'*self.config['size'])
        self.assertEqual(self.inspect()['layers'], [])

    def test_platform_descriptor_conflict_stops_selection(self):
        self.index([dict(self.manifest, platform={'architecture': 'arm64', 'os': 'linux'})])
        r = self.inspect()
        self.assertIn('platform-conflict', self.codes(r))
        self.assertEqual(r['layers'], [])

    def test_unknown_config_not_interpreted_or_executed(self):
        config = self.blob('application/foreign', b'{"architecture":"invented"}')
        self.index([self.image([self.layer], config=config)])
        r = self.inspect()
        self.assertIn('unsupported-config-media-type', self.codes(r))
        self.assertEqual(r['layers'], [])
        self.assertNotIn('invented', json.dumps(r))

    def test_descriptor_count_depth_and_json_caps(self):
        self.index([self.manifest, self.manifest])
        self.assertIn('descriptor-limit', self.codes(self.inspect(limits=OCILimits(max_descriptors=1))))
        self.assertIn('json-byte-limit', self.codes(self.inspect(limits=OCILimits(max_json_bytes=8))))
        nested = self.blob(INDEX, json.dumps({'schemaVersion': 2, 'manifests': [self.manifest]}).encode())
        self.index([nested])
        self.assertIn('index-depth-limit', self.codes(self.inspect(limits=OCILimits(max_index_depth=0))))

    def test_json_duplicate_nonfinite_and_depth_rejected(self):
        for text in ('{"schemaVersion":2,"schemaVersion":2,"manifests":[]}',
                     '{"schemaVersion":2,"manifests":[],"x":NaN}',
                     '{"schemaVersion":2,"manifests":[],"x":'+ '['*40+'0'+']'*40+'}'):
            (self.layout / 'index.json').write_text(text)
            self.assertIn('invalid-json', self.codes(self.inspect()))

    def test_layer_count_and_cumulative_expanded_cap(self):
        self.index([self.image([self.layer, self.layer])])
        self.assertIn('layer-limit', self.codes(self.inspect(limits=OCILimits(max_layers=1))))
        r = self.inspect(limits=OCILimits(max_expanded_bytes=1500))
        self.assertLessEqual(r['coverage']['expanded_bytes'], 1501)
        self.assertEqual(len(r['layers']), 1)
        self.assertIn('expanded-limit', self.codes(r))

    def test_expired_deadline_no_reads(self):
        r = inspect_oci_layout(self.layout, checkout=self.layout, scratch_base=self.scratch,
                               deadline=time.monotonic()-1)
        self.assertIn('deadline-exceeded', self.codes(r))
        self.assertEqual(r['layers'], [])

    def test_selection_must_be_reachable_from_index(self):
        r = self.inspect(manifest_digest='sha256:'+'a'*64)
        self.assertIn('manifest-not-found', self.codes(r))
        self.assertEqual(r['layers'], [])

    def test_layer_mutation_invalidates_inventory(self):
        from mdi import oci_layout
        original = oci_layout.inspect_distribution_fd
        def changed(fd, **kwargs):
            r = original(fd, **kwargs)
            path = self.layout / 'blobs' / 'sha256' / self.layer['digest'][7:]
            path.write_bytes(path.read_bytes()+b'changed')
            return r
        with mock.patch.object(oci_layout, 'inspect_distribution_fd', side_effect=changed):
            r = self.inspect()
        self.assertIn('source-changed', self.codes(r))
        self.assertEqual(r['layers'], [])


if __name__ == '__main__':
    unittest.main()

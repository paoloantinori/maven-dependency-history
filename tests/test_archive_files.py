"""File-backed traversal preserves archive evidence without whole-input buffers."""
import hashlib
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'))
from mdi import archives
from mdi.archive_files import FileArchiveLimits, inspect_archive_fd
from tests.test_zip_preflight import zip64_bytes


def jar(items):
    out = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name, data in items:
                zf.writestr(name, data)
    return out.getvalue()


META = 'META-INF/maven/example/lib/pom.properties'
PROPS = b'groupId=example\nartifactId=lib\nversion=1\n'


class ArchiveFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir(mode=0o700)
        self.path = self.root / 'app.jar'

    def inspect(self, data, **kwargs):
        self.path.write_bytes(data)
        with self.path.open('rb') as source:
            source.seek(3)
            result = inspect_archive_fd(source.fileno(), source_path=str(self.path),
                sha256=hashlib.sha256(data).hexdigest(), scratch_base=self.scratch,
                deadline=time.monotonic() + 20, **kwargs)
            self.assertEqual(source.tell(), 3)
        return result

    def test_nested_boot_parity_with_existing_reader(self):
        child = jar([(META, PROPS), ('example/C.class', b'class-bytes')])
        data = jar([('BOOT-INF/lib/lib.jar', child), ('app/A.class', b'app')])
        result = self.inspect(data)
        prior = archives.inspect_archive(self.path)
        self.assertEqual(result['archives'], prior['archives'])
        self.assertEqual(result['entries'], prior['entries'])
        self.assertEqual(result['coverage']['expanded_bytes_used'], prior['coverage']['expanded_bytes_used'])
        self.assertEqual(result['coverage']['status'], 'inspected')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_zip64_and_sfx(self):
        data = zip64_bytes(jar([(META, PROPS)]))
        for prefix in (b'', b'launcher prefix'):
            with self.subTest(prefix=prefix):
                result = self.inspect(prefix + data)
                self.assertEqual(result['coverage']['status'], 'inspected')
                self.assertEqual(result['archives'][0]['identity']['candidates'][0]['artifactId'], 'lib')

    def test_duplicate_names_read_by_index(self):
        result = self.inspect(jar([('same', b'one'), ('same', b'two')]))
        self.assertEqual([e['sha256'] for e in result['entries']],
                         [hashlib.sha256(x).hexdigest() for x in (b'one', b'two')])

    def test_metadata_cap_hashes_but_never_parses_partial_metadata(self):
        result = self.inspect(jar([(META, PROPS)]), limits=FileArchiveLimits(max_metadata_entry_bytes=8))
        entry = result['entries'][0]
        self.assertEqual(entry['sha256'], hashlib.sha256(PROPS).hexdigest())
        self.assertEqual(entry['status'], 'hashed')
        self.assertEqual(result['archives'][0]['identity']['candidates'], [])
        self.assertEqual(result['coverage']['status'], 'partial')

    def test_entry_cap_keeps_outer_identity(self):
        data = jar([('large', b'x' * 200)])
        result = self.inspect(data, limits=FileArchiveLimits(max_entry_bytes=10))
        self.assertEqual(result['entries'][0]['status'], 'too-large')
        self.assertIsNone(result['entries'][0]['sha256'])
        self.assertEqual(result['archives'][0]['sha256'], hashlib.sha256(data).hexdigest())

    def test_depth_limit_preserves_nested_copy_digest(self):
        deepest = jar([(META, PROPS)])
        child = jar([('inner.jar', deepest)])
        result = self.inspect(jar([('child.jar', child)]), limits=FileArchiveLimits(max_depth=1))
        entry = result['entries'][1]
        self.assertEqual(entry['status'], 'depth-limit')
        self.assertEqual(entry['sha256'], hashlib.sha256(deepest).hexdigest())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_global_entries_rechecked_after_recursion(self):
        child = jar([('a', b'a'), ('b', b'b')])
        result = self.inspect(jar([('child.jar', child), ('later', b'x')]),
                              limits=FileArchiveLimits(max_entries=3))
        self.assertEqual(len(result['entries']), 3)
        self.assertEqual(result['coverage']['status'], 'partial')

    def test_expanded_budget_counts_nested_container_twice(self):
        child = jar([(META, PROPS)])
        result = self.inspect(jar([('child.jar', child)]),
                              limits=FileArchiveLimits(max_expanded_bytes=len(child)))
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(len(result['archives']), 1)
        self.assertLessEqual(result['coverage']['expanded_bytes_used'], len(child) + 1)

    def test_unsafe_and_unsupported_entries_remain_explicit(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as zf:
            zf.writestr('../escape', b'x')
            link = zipfile.ZipInfo('link')
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(link, b'target')
            zf.writestr('compressed', b'x', compress_type=zipfile.ZIP_BZIP2)
        result = self.inspect(out.getvalue())
        self.assertEqual([e['status'] for e in result['entries']],
                         ['name-rejected', 'symlink', 'unsupported-compression'])
        self.assertEqual(result['coverage']['status'], 'partial')

    def test_digest_mismatch_and_input_cap_reject_before_zipfile(self):
        data = jar([('a', b'a')]); self.path.write_bytes(data)
        with self.path.open('rb') as f, mock.patch('mdi.archive_files.zipfile.ZipFile') as ctor:
            result = inspect_archive_fd(f.fileno(), source_path=str(self.path), sha256='0'*64,
                scratch_base=self.scratch, deadline=time.monotonic()+10)
            self.assertEqual(result['coverage']['status'], 'rejected')
            ctor.assert_not_called()
        result = self.inspect(data, limits=FileArchiveLimits(max_input_bytes=1))
        self.assertEqual(result['coverage']['status'], 'rejected')

    def test_preflight_entry_budget_before_zipinfo(self):
        data = jar([('a', b'a'), ('b', b'b')])
        with mock.patch('mdi.archive_files.zipfile.ZipFile') as ctor:
            result = self.inspect(data, limits=FileArchiveLimits(max_entries=1))
            self.assertEqual(result['coverage']['status'], 'partial')
            ctor.assert_not_called()

    def test_expired_deadline(self):
        self.path.write_bytes(jar([]))
        with self.path.open('rb') as f:
            result = inspect_archive_fd(f.fileno(), source_path=str(self.path), sha256='0'*64,
                scratch_base=self.scratch, deadline=time.monotonic()-1)
        self.assertTrue(result['coverage']['deadline_exceeded'])

    def test_scratch_symlink_refused(self):
        alias = self.root / 'alias'; alias.symlink_to(self.scratch)
        self.path.write_bytes(jar([]))
        with self.path.open('rb') as f, self.assertRaises(OSError):
            inspect_archive_fd(f.fileno(), source_path=str(self.path), sha256='0'*64,
                scratch_base=alias, deadline=time.monotonic()+10)

    def test_interrupt_closes_nested_spools_and_preserves_caller(self):
        child = jar([(META, PROPS)])
        data = jar([('child.jar', child)]); self.path.write_bytes(data)
        original = zipfile.ZipFile.open
        def interrupted(zf, name, *args, **kwargs):
            if name.filename == META:
                raise KeyboardInterrupt()
            return original(zf, name, *args, **kwargs)
        with self.path.open('rb') as f, mock.patch.object(zipfile.ZipFile, 'open', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                inspect_archive_fd(f.fileno(), source_path=str(self.path), sha256=hashlib.sha256(data).hexdigest(),
                    scratch_base=self.scratch, deadline=time.monotonic()+10)
            self.assertFalse(f.closed)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_expanded_budget_has_only_one_global_sentinel(self):
        result = self.inspect(jar([(str(i), b'xxx') for i in range(20)]),
                              limits=FileArchiveLimits(max_expanded_bytes=2))
        self.assertEqual(result['coverage']['expanded_bytes_used'], 3)
        self.assertEqual(len(result['entries']), 1)

    def test_mutated_source_drops_identity_claims(self):
        data = jar([(META, PROPS)])
        self.path.write_bytes(data)
        original = zipfile.ZipFile.open
        def changing(zf, info, *args, **kwargs):
            with self.path.open('ab') as writer:
                writer.write(b'changed')
            return original(zf, info, *args, **kwargs)
        with self.path.open('rb') as f, mock.patch.object(zipfile.ZipFile, 'open', changing):
            result = inspect_archive_fd(f.fileno(), source_path=str(self.path),
                sha256=hashlib.sha256(data).hexdigest(), scratch_base=self.scratch,
                deadline=time.monotonic()+10)
        self.assertFalse(result['coverage']['outer_capture']['complete'])
        self.assertIsNone(result['archives'][0]['sha256'])
        self.assertEqual(result['archives'][0]['identity']['candidates'], [])
        self.assertTrue(all(e['sha256'] is None for e in result['entries']))

    def test_replaced_nested_file_is_not_deleted(self):
        child = jar([(META, PROPS)])
        original = zipfile.ZipFile.open
        def replacing(zf, info, *args, **kwargs):
            if info.filename == META:
                path = next(self.scratch.iterdir())
                path.unlink()
                path.write_bytes(b'unrelated replacement')
            return original(zf, info, *args, **kwargs)
        with mock.patch.object(zipfile.ZipFile, 'open', replacing):
            result = self.inspect(jar([('child.jar', child)]))
        self.assertEqual(result['coverage']['cleanup']['status'], 'retained')
        self.assertEqual(next(self.scratch.iterdir()).read_bytes(), b'unrelated replacement')

    def test_readwrite_and_nonregular_descriptors_refused(self):
        self.path.write_bytes(jar([]))
        with self.path.open('r+b') as f, self.assertRaises(ValueError):
            inspect_archive_fd(f.fileno(), source_path=str(self.path), sha256='0'*64,
                scratch_base=self.scratch, deadline=time.monotonic()+10)
        read_fd, write_fd = os.pipe()
        try:
            with self.assertRaises(ValueError):
                inspect_archive_fd(read_fd, source_path='pipe', sha256='0'*64,
                    scratch_base=self.scratch, deadline=time.monotonic()+10)
        finally:
            os.close(read_fd); os.close(write_fd)

    def test_nested_files_are_private_generated_names(self):
        original = zipfile.ZipFile.open
        modes = []
        def inspect_mode(zf, info, *args, **kwargs):
            if info.filename == META:
                for path in self.scratch.iterdir():
                    modes.append(stat.S_IMODE(path.stat().st_mode))
                    self.assertTrue(path.name.startswith('mdi-nested-'))
            return original(zf, info, *args, **kwargs)
        with mock.patch.object(zipfile.ZipFile, 'open', inspect_mode):
            self.inspect(jar([('BOOT-INF/lib/lib.jar', jar([(META, PROPS)]))]))
        self.assertEqual(modes, [0o600])

    def test_fd_inventory_stays_stable(self):
        if not Path('/proc/self/fd').is_dir():
            self.skipTest('descriptor inventory needs procfs')
        before = len(list(Path('/proc/self/fd').iterdir()))
        for _ in range(5):
            self.inspect(jar([('child.jar', jar([(META, PROPS)]))]))
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def test_conflicting_metadata_candidates_preserved(self):
        other = 'META-INF/maven/other/lib/pom.properties'
        result = self.inspect(jar([(META, PROPS), (other, PROPS.replace(b'version=1', b'version=2'))]))
        identity = result['archives'][0]['identity']
        self.assertTrue(identity['conflict'])
        self.assertEqual(len(identity['candidates']), 2)

    def test_unrelated_scratch_file_untouched(self):
        sentinel = self.scratch / 'user-file'
        sentinel.write_bytes(b'keep')
        self.inspect(jar([('child.jar', jar([(META, PROPS)]))]))
        self.assertEqual(list(self.scratch.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_reader_never_issues_whole_source_reads(self):
        import mdi.archive_files as archive_files
        self.path.write_bytes(b'abc')
        with self.path.open('rb') as source, archive_files._Reader(source.fileno(), lambda: None, 64) as reader:
            for count in (-1, 65):
                with self.assertRaises(ValueError):
                    reader.read(count)
            self.assertEqual(reader.read(3), b'abc')
            self.assertEqual(source.tell(), 0)

    def test_nested_mutation_before_recursion_cannot_mix_digest_and_metadata(self):
        from mdi import archive_files
        original = archive_files._container
        first = jar([(META, PROPS)])
        second = jar([(META, PROPS.replace(b'version=1', b'version=2'))])
        self.assertEqual(len(first), len(second))

        def mutate(state, fd, *args, **kwargs):
            if args[3]:  # entry_chain is nonempty for nested containers
                os.pwrite(fd, second, 0)
            return original(state, fd, *args, **kwargs)

        with mock.patch.object(archive_files, '_container', side_effect=mutate):
            result = self.inspect(jar([('child.jar', first)]))
        child = result['archives'][1]
        self.assertEqual(child['inspection_status'], 'partial')
        self.assertIsNone(child['sha256'])
        self.assertEqual(child['identity']['candidates'], [])
        self.assertIsNone(result['entries'][0]['sha256'])
        self.assertEqual(result['coverage']['status'], 'partial')

    def test_write_interrupt_survives_identity_refresh_failure(self):
        from mdi import archive_files
        original = archive_files._identity
        interrupted = [False]

        def write(fd, data):
            interrupted[0] = True
            raise KeyboardInterrupt('primary-write-interrupt')

        def identity(fd):
            if interrupted[0]:
                raise OSError('secondary-fstat-failure')
            return original(fd)

        with mock.patch.object(archive_files.os, 'write', side_effect=write), \
                mock.patch.object(archive_files, '_identity', side_effect=identity):
            with self.assertRaisesRegex(KeyboardInterrupt, 'primary-write-interrupt'):
                self.inspect(jar([('child.jar', jar([(META, PROPS)]))]))
        self.assertTrue(list(self.scratch.iterdir()), 'unconfirmed spool must be retained')

    def test_cleanup_error_does_not_mask_primary_interrupt(self):
        from mdi import archive_files
        original_open = zipfile.ZipFile.open
        original_close = archive_files._Nested.close

        def opening(zf, info, *args, **kwargs):
            if info.filename == META:
                raise KeyboardInterrupt('primary-read-interrupt')
            return original_open(zf, info, *args, **kwargs)

        def closing(nested):
            original_close(nested)
            raise OSError('secondary-cleanup-failure')

        with mock.patch.object(zipfile.ZipFile, 'open', opening), \
                mock.patch.object(archive_files._Nested, 'close', closing):
            with self.assertRaisesRegex(KeyboardInterrupt, 'primary-read-interrupt'):
                self.inspect(jar([('child.jar', jar([(META, PROPS)]))]))

    def test_limits_validation(self):
        with self.assertRaises(ValueError):
            FileArchiveLimits(max_depth=5)
        for value in (0, -1, True, 1.5):
            with self.assertRaises((TypeError, ValueError)):
                FileArchiveLimits(max_entries=value)


if __name__ == '__main__':
    unittest.main()

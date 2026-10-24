"""Local tar inventory tests; no extraction, network or Maven."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'))
from mdi import distributions as d


def jar():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('META-INF/maven/example/lib/pom.properties', 'groupId=example\nartifactId=lib\nversion=1\n')
    return out.getvalue()


def tar(rows):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w', format=tarfile.USTAR_FORMAT) as t:
        for name, data, kind in rows:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data)
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = '/etc/passwd'
            t.addfile(info, io.BytesIO(data))
    return out.getvalue()


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.checkout = self.base / 'checkout'
        self.checkout.mkdir()
        self.scratch = self.base / 'scratch'
        self.scratch.mkdir(mode=0o700)

    def inspect(self, data, format='tar', **kw):
        p = self.checkout / 'input'
        p.write_bytes(data)
        fd = os.open(p, os.O_RDONLY)
        try:
            os.lseek(fd, 7, os.SEEK_SET)
            result = d.inspect_distribution_fd(fd, source_path=str(p), checkout=str(self.checkout),
                scratch_base=str(self.scratch), format=format, deadline=kw.pop('deadline', time.monotonic()+10), **kw)
            self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 7)
            os.fstat(fd)
            json.dumps(result, allow_nan=False)
            return result
        finally:
            os.close(fd)

    def test_tar_and_gzip_nested_jar(self):
        plain = tar([('lib/a.jar', jar(), tarfile.REGTYPE)])
        for fmt, data in [('tar', plain), ('tar.gz', gzip.compress(plain))]:
            with self.subTest(fmt=fmt):
                r = self.inspect(data, fmt)
                self.assertEqual(r['coverage']['status'], 'complete')
                self.assertEqual(r['source']['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(r['members'][0]['role'], 'unknown')
                self.assertEqual(r['archives'][0]['inspection']['archives'][0]['identity']['candidates'][0]['artifactId'], 'lib')
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_oci_whiteout_mode_skips_empty_archive_marker_only(self):
        data = tar([('lib/.wh.b.jar', b'', tarfile.REGTYPE),
                    ('lib/.wh..wh..opq', b'', tarfile.REGTYPE)])
        ordinary = self.inspect(data)
        self.assertEqual(ordinary['coverage']['status'], 'partial')
        self.assertEqual(len(ordinary['archives']), 1)
        self.assertNotIn('whiteout', ordinary['members'][0])
        marked = self.inspect(data, oci_whiteouts=True)
        self.assertEqual(marked['coverage']['status'], 'complete')
        self.assertEqual(marked['archives'], [])
        self.assertEqual([x['whiteout']['kind'] for x in marked['members']], ['remove', 'opaque'])
        self.assertEqual(marked['members'][0]['whiteout']['target'], 'lib/b.jar')
        self.assertEqual(marked['members'][1]['whiteout']['target'], 'lib')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_invalid_oci_markers_remain_partial_without_archive_inspection(self):
        for name, data, kind in [('lib/.wh.b.jar', b'x', tarfile.REGTYPE),
                                 ('lib/.wh.b.jar', b'', tarfile.SYMTYPE),
                                 ('lib/.wh.b.jar', b'', tarfile.DIRTYPE),
                                 ('lib/.wh.', b'', tarfile.REGTYPE),
                                 ('lib/.wh...', b'', tarfile.REGTYPE),
                                 ('../.wh.b.jar', b'', tarfile.REGTYPE)]:
            with self.subTest(name=name, kind=kind):
                result = self.inspect(tar([(name, data, kind)]), oci_whiteouts=True)
                self.assertEqual(result['coverage']['status'], 'partial')
                self.assertEqual(result['archives'], [])
                self.assertNotIn('whiteout', result['members'][0])

    def test_whiteout_option_requires_boolean(self):
        for value in (1, 'true', None):
            with self.assertRaises(d.DistributionError):
                self.inspect(tar([]), oci_whiteouts=value)

    def test_duplicate_names_are_separate_indices(self):
        r = self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)] * 2))
        self.assertEqual([x['member_index'] for x in r['archives']], [0, 1])
        self.assertEqual([x['index'] for x in r['members']], [0, 1])

    def test_links_devices_and_traversal_not_followed(self):
        rows = [(name, b'', kind) for name, kind in [('../a.jar', tarfile.REGTYPE),
            ('/abs.jar', tarfile.REGTYPE), ('link.jar', tarfile.SYMTYPE),
            ('hard.jar', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE)]]
        r = self.inspect(tar(rows))
        self.assertEqual(r['archives'], [])
        self.assertEqual(r['coverage']['status'], 'partial')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_skipped_bytes_count_against_gzip_expansion_limit(self):
        r = self.inspect(gzip.compress(tar([('big.txt', b'x'*20000, tarfile.REGTYPE)])),
                         'tar.gz', limits=d.DistributionLimits(max_expanded_bytes=2048))
        self.assertEqual(r['coverage']['status'], 'partial')
        self.assertIn('expanded-limit', [x['code'] for x in r['issues']])
        self.assertLessEqual(r['coverage']['expanded_bytes'], 2049)
        self.assertEqual(r['archives'], [])

    def test_member_limit(self):
        r = self.inspect(tar([('a', b'x', tarfile.REGTYPE)]*3), limits=d.DistributionLimits(max_members=2))
        self.assertEqual(len(r['members']), 2)
        self.assertIn('member-limit', [x['code'] for x in r['issues']])

    def test_oversized_member_is_drained_without_spooling(self):
        r = self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]), limits=d.DistributionLimits(max_member_bytes=10))
        self.assertEqual(r['archives'], [])
        self.assertEqual(r['members'][0]['status'], 'member-too-large')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_pax_longname_sparse_refused_without_parsing_payload(self):
        for typ in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_SPARSE):
            info = tarfile.TarInfo('extended')
            info.type = typ
            info.size = 0
            r = self.inspect(info.tobuf(format=tarfile.GNU_FORMAT) + b'\0'*1024)
            self.assertIn('unsupported-tar-extension', [x['code'] for x in r['issues']])

    def test_truncated_body(self):
        r = self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)])[:520])
        self.assertIn('truncated-tar', [x['code'] for x in r['issues']])
        self.assertEqual(r['archives'], [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_invalid_gzip_and_tar(self):
        for fmt in ('tar', 'tar.gz'):
            r = self.inspect(b'not an archive', fmt)
            self.assertEqual(r['coverage']['status'], 'partial')

    def test_deadline_and_input_size(self):
        r = self.inspect(tar([]), limits=d.DistributionLimits(max_input_bytes=100))
        self.assertEqual(r['coverage']['status'], 'partial')
        self.assertIn('input-limit', [x['code'] for x in r['issues']])
        r = self.inspect(tar([]), deadline=0)
        self.assertIn('deadline-exceeded', [x['code'] for x in r['issues']])

    def test_growth_between_size_stat_and_identity_check_is_not_complete(self):
        original = os.fstat
        mutated = []
        data = tar([('a.jar', jar(), tarfile.REGTYPE)])

        def stat_then_grow(fd):
            observed = original(fd)
            if not mutated and os.readlink('/proc/self/fd/' + str(fd)) == str(self.checkout / 'input'):
                mutated.append(True)
                with (self.checkout / 'input').open('ab') as stream:
                    stream.write(b'growth-between-stats!')
            return observed

        with mock.patch.object(d.os, 'fstat', side_effect=stat_then_grow):
            r = self.inspect(data)
        self.assertEqual(mutated, [True])
        self.assertEqual(r['coverage']['status'], 'partial')
        self.assertIn('source-changed', [issue['code'] for issue in r['issues']])
        self.assertIsNone(r['source']['sha256'])
        self.assertEqual(r['archives'], [])
        self.assertTrue(all(member['sha256'] is None for member in r['members']))
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_nonzero_directory_size_cannot_hide_following_jar_header(self):
        directory = tarfile.TarInfo('directory')
        directory.type = tarfile.DIRTYPE
        directory.size = 512
        data = directory.tobuf(format=tarfile.USTAR_FORMAT) + tar([
            ('hidden.jar', b'', tarfile.REGTYPE),
        ])
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
            self.assertEqual([entry.name for entry in archive], ['directory', 'hidden.jar'])
        r = self.inspect(data)
        self.assertEqual(r['coverage']['status'], 'partial')
        self.assertIn('nonregular-member-size', [issue['code'] for issue in r['issues']])
        self.assertEqual(r['coverage']['expanded_bytes'], 512)
        self.assertEqual(r['archives'], [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_nonzero_link_device_fifo_and_unknown_sizes_refused_before_drain(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                     tarfile.BLKTYPE, tarfile.FIFOTYPE, b'Z'):
            with self.subTest(kind=kind):
                info = tarfile.TarInfo('nonregular')
                info.type = kind
                info.size = 512
                data = info.tobuf(format=tarfile.USTAR_FORMAT) + tar([
                    ('hidden.jar', b'', tarfile.REGTYPE),
                ])
                r = self.inspect(data)
                self.assertEqual(r['coverage']['status'], 'partial')
                self.assertIn('nonregular-member-size', [issue['code'] for issue in r['issues']])
                self.assertEqual(r['coverage']['expanded_bytes'], 512)
                self.assertEqual(r['archives'], [])

    def test_source_mutation_invalidates_all_claims(self):
        real = d.inspect_archive_fd
        def mutate(*a, **kw):
            result = real(*a, **kw)
            with (self.checkout/'input').open('ab') as f:
                f.write(b'changed')
            return result
        with mock.patch.object(d, 'inspect_archive_fd', side_effect=mutate):
            r = self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]))
        self.assertIsNone(r['source']['sha256'])
        self.assertEqual(r['archives'], [])
        self.assertTrue(all(m['sha256'] is None for m in r['members']))

    def test_interrupt_closes_and_cleans_owned_spool(self):
        before = len(os.listdir('/proc/self/fd'))
        with mock.patch.object(d, 'inspect_archive_fd', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]))
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_scratch_inside_checkout_or_symlink_refused(self):
        p = self.checkout/'input'
        p.write_bytes(tar([]))
        link = self.base/'link'
        link.symlink_to(self.scratch)
        fd = os.open(p, os.O_RDONLY)
        try:
            for scratch in (self.checkout, link):
                with self.assertRaises((ValueError, OSError)):
                    d.inspect_distribution_fd(fd, source_path=str(p), checkout=str(self.checkout),
                        scratch_base=str(scratch), format='tar', deadline=time.monotonic()+10)
        finally:
            os.close(fd)

    def test_nested_zip_expansion_is_global_across_members(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('data', b'x' * 5000)
        r = self.inspect(tar([('a.jar', out.getvalue(), tarfile.REGTYPE),
                              ('b.jar', out.getvalue(), tarfile.REGTYPE)]),
                         limits=d.DistributionLimits(max_expanded_bytes=8500))
        self.assertEqual(r['coverage']['status'], 'partial')
        self.assertLessEqual(r['coverage']['expanded_bytes'], 8501)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_result_budget_refuses_large_inventory(self):
        r = self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]),
                         limits=d.DistributionLimits(max_inventory_bytes=1000))
        self.assertIn('result-limit', [x['code'] for x in r['issues']])
        self.assertEqual(r['archives'], [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_initial_spool_identity_error_reports_retained_path(self):
        original = d._identity
        def fail_spool(fd):
            if 'mdi-distribution-' in os.readlink('/proc/self/fd/' + str(fd)):
                raise OSError('stat failed')
            return original(fd)
        with mock.patch.object(d, '_identity', side_effect=fail_spool):
            r = self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]))
        self.assertEqual(r['coverage']['status'], 'partial')
        self.assertEqual(len(r['coverage']['cleanup']['leftovers']), 1)
        self.assertTrue(Path(r['coverage']['cleanup']['leftovers'][0]['path']).is_file())

    def test_short_writes_complete_member_capture(self):
        original = os.write
        with mock.patch.object(d.os, 'write', side_effect=lambda fd, data: original(fd, data[:7])):
            r = self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]))
        self.assertEqual(r['coverage']['status'], 'complete')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_primary_interrupt_survives_spool_identity_refresh_failure(self):
        original = d._identity
        interrupted = KeyboardInterrupt('original')
        armed = [False]
        def fail_write(fd, data):
            armed[0] = True
            raise interrupted
        def fail_identity(fd):
            if armed[0] and 'mdi-distribution-' in os.readlink('/proc/self/fd/' + str(fd)):
                raise OSError('secondary stat failure')
            return original(fd)
        before = len(os.listdir('/proc/self/fd'))
        with mock.patch.object(d.os, 'write', side_effect=fail_write), mock.patch.object(d, '_identity', side_effect=fail_identity):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]))
        self.assertIs(raised.exception, interrupted)
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        self.assertEqual(len(list(self.scratch.iterdir())), 1)

    def test_nested_cleanup_ledger_survives_reader_close_failure(self):
        for failure in (OSError('close failed'), KeyboardInterrupt('close interrupted')):
            with self.subTest(failure=type(failure).__name__):
                self._nested_cleanup_failure(close_error=failure)

    def test_nested_cleanup_ledger_survives_reader_exception_and_close_failure(self):
        self._nested_cleanup_failure(
            close_error=OSError('secondary close failed'),
            reader_error=KeyboardInterrupt('original reader interruption'),
        )

    def test_nested_cleanup_ledger_survives_inventory_budget_failure(self):
        self._nested_cleanup_failure(inventory_cap=1000)

    def _nested_cleanup_failure(self, close_error=None, reader_error=None, inventory_cap=None):
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.writestr('lib/inner.jar', jar())
        original_inspect = d.inspect_archive_fd
        original_unlink = os.unlink
        original_close = os.close
        retained = []
        close_target = [None]
        closes = []

        def refuse_nested_unlink(path, *args, **kwargs):
            if str(path).startswith('mdi-nested-'):
                raise OSError('retain nested spool')
            return original_unlink(path, *args, **kwargs)

        def inspect_then_fail(fd, **kwargs):
            result = original_inspect(fd, **kwargs)
            retained.extend(result['coverage']['cleanup']['leftovers'])
            self.assertEqual(len(retained), 1)
            self.assertTrue(Path(retained[0]['path']).is_file())
            close_target[0] = fd
            if reader_error is not None:
                reader_error.archive_cleanup = result['coverage']['cleanup']
                raise reader_error
            return result

        def close_then_fail(fd):
            original_close(fd)
            if fd == close_target[0]:
                close_target[0] = None
                closes.append(fd)
                if close_error is not None:
                    raise close_error

        before = len(os.listdir('/proc/self/fd'))
        options = {} if inventory_cap is None else {
            'limits': d.DistributionLimits(max_inventory_bytes=inventory_cap)}
        try:
            with mock.patch.object(d, 'inspect_archive_fd', side_effect=inspect_then_fail), \
                    mock.patch.object(d.os, 'unlink', side_effect=refuse_nested_unlink), \
                    mock.patch.object(d.os, 'close', side_effect=close_then_fail):
                primary = reader_error or (close_error if isinstance(close_error, KeyboardInterrupt) else None)
                if primary is not None:
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        self.inspect(tar([('outer.jar', outer.getvalue(), tarfile.REGTYPE)]), **options)
                    self.assertIs(caught.exception, primary)
                    cleanup = caught.exception.distribution_cleanup
                else:
                    result = self.inspect(tar([('outer.jar', outer.getvalue(), tarfile.REGTYPE)]), **options)
                    self.assertEqual(result['coverage']['status'], 'partial')
                    self.assertEqual(result['archives'], [])
                    cleanup = result['coverage']['cleanup']
                self.assertIn(retained[0], cleanup['leftovers'])
                self.assertEqual(len(closes), 1)
            self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        finally:
            for item in retained:
                Path(item['path']).unlink()

    def test_nested_cleanup_obligations_have_an_independent_bound(self):
        records = [{'path': str(self.scratch / f'child-{i}'), 'reason': 'retained'}
                   for i in range(d._MAX_NESTED_LEFTOVERS + 1)]
        leftovers = []
        complete = d._retain_nested_cleanup({'leftovers': records}, leftovers, str(self.scratch))
        self.assertFalse(complete)
        self.assertEqual(len(leftovers), d._MAX_NESTED_LEFTOVERS)
        self.assertEqual(leftovers[0], records[0])
        self.assertEqual(leftovers[-1], {'path': str(self.scratch),
                                        'reason': 'nested-cleanup-ledger-unconfirmed'})
        self.assertFalse(d._retain_nested_cleanup(None, leftovers, str(self.scratch)))
        self.assertEqual(len(leftovers), d._MAX_NESTED_LEFTOVERS)
        exact = {'path': str(self.scratch / 'child'), 'reason': 'retained'}
        detached = []
        d._retain_nested_cleanup({'leftovers': [exact]}, detached, str(self.scratch))
        exact['reason'] = 'changed'
        self.assertEqual(detached[0]['reason'], 'retained')

    def test_unknown_nested_cleanup_is_incomplete_in_actual_worker(self):
        from mdi import distribution_worker
        source = self.checkout / 'worker-input.tar'
        source.write_bytes(tar([('a.jar', jar(), tarfile.REGTYPE)]))
        unknown = self.scratch / 'unknown-nested.spool'
        code = (
            'import sys\nfrom pathlib import Path\n'
            f'sys.path.insert(0, {str(Path(d.__file__).resolve().parent.parent)!r})\n'
            'from mdi import distributions, distribution_worker\n'
            'def fail(*args, **kwargs):\n'
            f' Path({str(unknown)!r}).write_bytes(b"retained")\n'
            ' raise OSError("no nested cleanup ledger")\n'
            'distributions.inspect_archive_fd = fail\n'
            'raise SystemExit(distribution_worker._worker())\n'
        )
        fd = os.open(source, os.O_RDONLY)
        try:
            result = distribution_worker.inspect_isolated(
                fd, source_path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                checkout=str(self.checkout), scratch_base=str(self.scratch),
                format='tar', deadline=time.monotonic() + 10,
                worker_command=[sys.executable, '-B', '-I', '-S', '-c', code],
            )
        finally:
            os.close(fd)
        self.assertTrue(unknown.is_file())
        self.assertEqual(result['result']['coverage']['status'], 'partial')
        self.assertFalse(result['cleanup']['individual_paths_complete'])
        self.assertFalse(result['result']['coverage']['cleanup']['individual_paths_complete'])
        self.assertIn(str(self.scratch), [x['path'] for x in result['cleanup']['leftovers']])

    def test_saturated_reader_ledger_retains_known_paths_and_reports_incomplete(self):
        original = d.inspect_archive_fd
        first = str(self.scratch / 'known-first')

        def saturated(*args, **kwargs):
            result = original(*args, **kwargs)
            result['coverage']['cleanup'] = {'leftovers': [
                {'path': first if i == 0 else str(self.scratch / f'entry-{i}'), 'reason': 'retained'}
                for i in range(d._MAX_NESTED_LEFTOVERS + 1)]}
            return result

        with mock.patch.object(d, 'inspect_archive_fd', side_effect=saturated):
            r = self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]))
        cleanup = r['coverage']['cleanup']
        self.assertFalse(cleanup['individual_paths_complete'])
        self.assertEqual(len(cleanup['leftovers']), d._MAX_NESTED_LEFTOVERS)
        self.assertEqual(cleanup['leftovers'][0]['path'], first)
        self.assertEqual(cleanup['leftovers'][-1]['path'], str(self.scratch))
        self.assertEqual(r['coverage']['status'], 'partial')

    def test_unknown_exception_cleanup_explicitly_marks_incomplete_paths(self):
        interrupted = KeyboardInterrupt('lower interruption without ledger')
        with mock.patch.object(d, 'inspect_archive_fd', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as raised:
                self.inspect(tar([('a.jar', jar(), tarfile.REGTYPE)]))
        self.assertIs(raised.exception, interrupted)
        self.assertFalse(interrupted.distribution_cleanup['individual_paths_complete'])
        self.assertIn(str(self.scratch), [x['path'] for x in interrupted.distribution_cleanup['leftovers']])

    def test_invalid_options(self):
        for value in (0, -1, True):
            with self.assertRaises(ValueError): d.DistributionLimits(max_members=value)
        with self.assertRaises(ValueError): self.inspect(tar([]), 'tar.xz')


if __name__ == '__main__': unittest.main()

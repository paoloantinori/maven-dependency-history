"""Synthetic local OCI process-boundary tests. No pulls or container execution."""
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from mdi import oci_worker as worker
from mdi.oci_layout import CONFIG, INDEX, MANIFEST, OCILimits

TAR = 'application/vnd.oci.image.layer.v1.tar'


class OCIWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.layout = self.base / 'image'
        (self.layout / 'blobs/sha256').mkdir(parents=True)
        self.checkout = self.base / 'checkout'
        self.checkout.mkdir()
        self.scratch = self.base / 'scratch'
        self.scratch.mkdir(mode=0o700)
        (self.layout / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
        config = self.blob(CONFIG, json.dumps({'os': 'linux', 'architecture': 'amd64',
            'config': {'Env': ['SECRET=must-not-appear'], 'Cmd': ['never-run']}}).encode())
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('META-INF/maven/example/lib/pom.properties',
                            'groupId=example\nartifactId=lib\nversion=1\n')
        self.layer = self.blob(TAR, self.tar({'lib/library.jar': jar.getvalue()}))
        self.manifest = self.blob(MANIFEST, json.dumps({'schemaVersion': 2,
            'mediaType': MANIFEST, 'config': config, 'layers': [self.layer]}).encode())
        self.index([self.manifest])
        self.fd = os.open(self.layout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.addCleanup(os.close, self.fd)

    def blob(self, media, data):
        digest = hashlib.sha256(data).hexdigest()
        (self.layout / 'blobs/sha256' / digest).write_bytes(data)
        return dict(mediaType=media, digest='sha256:' + digest, size=len(data))

    def index(self, descriptors):
        (self.layout / 'index.json').write_text(json.dumps(
            dict(schemaVersion=2, mediaType=INDEX, manifests=descriptors)))

    def tar(self, entries):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return out.getvalue()

    def run_worker(self, **over):
        args = dict(layout_path=str(self.layout), checkout=str(self.checkout),
                    scratch_base=str(self.scratch), deadline=time.monotonic() + 10)
        args.update(over)
        return worker.inspect_isolated(self.fd, **args)

    def fake(self, code):
        path = self.base / ('fake-' + str(len(list(self.base.glob('fake-*')))) + '.py')
        path.write_text(code)
        return [sys.executable, '-B', '-I', '-S', str(path)]

    def assert_reaped(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def forged(self, change):
        good = self.run_worker()
        self.assertEqual(good['outcome'], 'complete', good)
        envelope = dict(schema=worker.SCHEMA, result=good['result'], error=None,
                        cleanup=good['cleanup'])
        change(envelope)
        return self.fake('print(' + repr(json.dumps(envelope)) + ')')

    def test_actual_layout_nested_jar_and_caller_ownership(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        inventory = result['result']
        self.assertEqual(inventory['selected_manifest'], self.manifest['digest'])
        self.assertFalse(inventory['coverage']['merged_filesystem'])
        self.assertEqual(inventory['coverage']['status'], 'partial')
        nested = inventory['layers'][0]['distribution']['archives'][0]['inspection']
        self.assertEqual(nested['archives'][0]['identity']['candidates'][0]['artifactId'], 'lib')
        self.assertNotIn('must-not-appear', json.dumps(result))
        self.assertNotIn('never-run', json.dumps(result))
        self.assertEqual(result['cleanup']['leftovers'], [])
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertTrue(result['cleanup_result']['worker_stopped'])
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), 0)
        os.fstat(self.fd)
        self.assert_reaped(result['worker_pid'])

    def test_actual_gzip_layer_and_nested_index_routes(self):
        import gzip
        document = json.loads((self.layout / 'blobs/sha256' / self.manifest['digest'][7:]).read_bytes())
        raw = (self.layout / 'blobs/sha256' / self.layer['digest'][7:]).read_bytes()
        document['layers'] = [self.blob(TAR + '+gzip', gzip.compress(raw))]
        manifest = self.blob(MANIFEST, json.dumps(document).encode())
        index = self.blob(INDEX, json.dumps(dict(schemaVersion=2, mediaType=INDEX,
                                               manifests=[manifest])).encode())
        self.index([index, manifest])
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['result']['selected_manifest'], manifest['digest'])
        self.assertEqual(result['result']['layers'][0]['distribution']['source']['format'], 'tar.gz')
        self.assertEqual(len(result['result']['manifests'][0]['routes']), 2)

    def test_nonzero_directory_offset_preserved(self):
        os.lseek(self.fd, 9, os.SEEK_SET)
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(os.lseek(self.fd, 0, os.SEEK_CUR), 9)

    def test_root_path_replacement_does_not_redirect_layout(self):
        old = self.base / 'original'
        self.layout.rename(old)
        self.layout.mkdir()
        (self.layout / 'oci-layout').write_text('not a layout')
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['result']['selected_manifest'], self.manifest['digest'])
        self.assertTrue(result['result']['source']['unchanged'])

    def test_manifest_ambiguity_and_explicit_selection(self):
        other = self.blob(MANIFEST, json.dumps(dict(schemaVersion=2, mediaType=MANIFEST,
            config=self.blob(CONFIG, b'{"os":"linux","architecture":"arm64"}'), layers=[])).encode())
        self.index([self.manifest, other])
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertIsNone(result['result']['selected_manifest'])
        self.assertIn('ambiguous-manifest', [r['code'] for r in result['result']['issues']])
        result = self.run_worker(manifest_digest=other['digest'])
        self.assertEqual(result['result']['selected_manifest'], other['digest'])
        self.assertEqual(result['result']['config']['platform']['architecture'], 'arm64')

    def test_implicit_request_rejects_selected_response_from_ambiguous_layout(self):
        other = self.blob(MANIFEST, json.dumps(dict(schemaVersion=2, mediaType=MANIFEST,
            config=self.blob(CONFIG, b'{"os":"linux","architecture":"arm64"}'), layers=[])).encode())
        self.index([self.manifest, other])
        explicit = self.run_worker(manifest_digest=other['digest'])
        self.assertEqual(explicit['outcome'], 'complete', explicit)
        envelope = dict(schema=worker.SCHEMA, result=explicit['result'], error=None,
                        cleanup=explicit['cleanup'])
        result = self.run_worker(worker_command=self.fake('print(' + repr(json.dumps(envelope)) + ')'))
        self.assertEqual(result['error'], 'invalid-response')
        self.assertIsNone(result['result'])

    def test_whiteouts_are_observed_never_applied(self):
        doc = json.loads((self.layout / 'blobs/sha256' / self.manifest['digest'][7:]).read_bytes())
        doc['layers'].append(self.blob(TAR, self.tar({'lib/.wh.library.jar': b''})))
        manifest = self.blob(MANIFEST, json.dumps(doc).encode())
        self.index([manifest])
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertFalse(result['result']['coverage']['merged_filesystem'])
        self.assertEqual(result['result']['layers'][1]['whiteouts'][0]['kind'], 'remove')
        self.assertEqual(len(result['result']['layers'][0]['distribution']['archives']), 1)

    def test_valid_whiteout_targets_and_opaque_markers_survive_boundary(self):
        document = json.loads((self.layout / 'blobs/sha256' / self.manifest['digest'][7:]).read_bytes())
        document['layers'] = [self.blob(TAR, self.tar({
            'lib/.wh.library.jar': b'', 'lib/.wh..wh..opq': b'', '.wh.root.jar': b'',
        }))]
        self.index([self.blob(MANIFEST, json.dumps(document).encode())])
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        layer = result['result']['layers'][0]
        self.assertEqual(layer['distribution']['archives'], [])
        self.assertEqual(layer['whiteouts'], [
            dict(member_index=0, name='lib/.wh.library.jar', kind='remove', target='lib/library.jar'),
            dict(member_index=1, name='lib/.wh..wh..opq', kind='opaque', target='lib'),
            dict(member_index=2, name='.wh.root.jar', kind='remove', target='root.jar'),
        ])
        self.assertFalse(layer['distribution']['coverage']['layer_changes_applied'])

    def test_malformed_marker_remains_partial_without_deletion_semantics(self):
        document = json.loads((self.layout / 'blobs/sha256' / self.manifest['digest'][7:]).read_bytes())
        document['layers'] = [self.blob(TAR, self.tar({'lib/.wh.library.jar': b'not empty'}))]
        self.index([self.blob(MANIFEST, json.dumps(document).encode())])
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        layer = result['result']['layers'][0]
        self.assertEqual(layer['whiteouts'], [])
        self.assertEqual(layer['distribution']['members'][0]['status'], 'invalid-oci-whiteout')
        self.assertNotIn('whiteout', layer['distribution']['members'][0])
        self.assertEqual(layer['distribution']['archives'], [])

    def test_forged_whiteout_metadata_is_not_trusted(self):
        document = json.loads((self.layout / 'blobs/sha256' / self.manifest['digest'][7:]).read_bytes())
        document['layers'] = [self.blob(TAR, self.tar({'lib/.wh.library.jar': b''}))]
        self.index([self.blob(MANIFEST, json.dumps(document).encode())])
        def member(e): return e['result']['layers'][0]['distribution']['members'][0]
        def wrong_target(e): member(e)['whiteout']['target'] = '../elsewhere'
        def wrong_kind(e): member(e)['whiteout']['kind'] = 'opaque'
        def nonempty(e): member(e)['size_declared'] = member(e)['size_actual'] = 1
        def wrong_type(e): member(e)['type'] = '35'
        def wrong_digest(e): member(e)['sha256'] = '0' * 64
        def wrong_name(e): member(e)['name'] = 'lib/ordinary.jar'
        def omitted(e): del member(e)['whiteout']
        def context(e): e['result']['layers'][0]['distribution']['coverage']['oci_whiteouts'] = False
        def projection(e): e['result']['layers'][0]['whiteouts'][0]['target'] = 'another'
        for change in (wrong_target, wrong_kind, nonempty, wrong_type, wrong_digest,
                       wrong_name, omitted, context, projection):
            with self.subTest(change=change.__name__):
                result = self.run_worker(worker_command=self.forged(change))
                self.assertEqual(result['error'], 'invalid-response', result)

    def test_ordinary_tar_validator_rejects_injected_whiteout_metadata(self):
        from mdi import distribution_worker
        path = self.checkout / 'ordinary.tar'
        path.write_bytes(self.tar({'lib/.wh.placeholder': b''}))
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        result = distribution_worker.inspect_isolated(fd, source_path=str(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), checkout=str(self.checkout),
            scratch_base=str(self.scratch), format='tar', deadline=time.monotonic()+10)
        self.assertEqual(result['outcome'], 'complete', result)
        inventory = result['result']
        self.assertFalse(inventory['coverage']['oci_whiteouts'])
        self.assertNotIn('whiteout', inventory['members'][0])
        inventory['members'][0]['whiteout'] = dict(kind='remove', target='lib/placeholder')
        with self.assertRaises(ValueError):
            distribution_worker._validate_result(inventory,
                distribution_worker.DistributionLimits(), str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size,
                'tar', result['cleanup'], str(self.scratch))

    def test_missing_and_corrupt_blobs_are_partial_not_no_dependencies(self):
        path = self.layout / 'blobs/sha256' / self.layer['digest'][7:]
        path.write_bytes(b'x' * self.layer['size'])
        result = self.run_worker()
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['result']['layers'], [])
        self.assertIn('blob-digest-mismatch', [i['code'] for i in result['result']['issues']])
        path.unlink()
        self.assertEqual(self.run_worker()['result']['coverage']['status'], 'partial')

    def test_deadline_stall_and_flood_preserve_recovery(self):
        cases = [('import time;time.sleep(30)', 'deadline-exceeded', 4096),
                 ('import os\nwhile True: os.write(1,b"x"*65536)', 'response-too-large', 1024)]
        for code, expected, cap in cases:
            with self.subTest(expected=expected):
                start = time.monotonic()
                result = self.run_worker(worker_command=self.fake(code), deadline=start + .3,
                                         max_response_bytes=cap)
                self.assertEqual(result['error'], expected, result)
                self.assertFalse(result['cleanup']['individual_paths_complete'])
                self.assertEqual(result['cleanup']['leftovers'][-1]['path'], str(self.scratch))
                self.assertTrue(result['cleanup_result']['worker_stopped'])
                self.assert_reaped(result['worker_pid'])
                self.assertLess(time.monotonic() - start, 4)

    def test_drip_does_not_reset_shared_deadline(self):
        result = self.run_worker(worker_command=self.fake(
            'import os,time\nwhile True: os.write(1,b" ");time.sleep(.02)'), deadline=time.monotonic()+.25)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assert_reaped(result['worker_pid'])

    def test_bad_protocol_and_forged_bindings_rejected(self):
        mutations = [lambda e: e['result']['source'].update(path='/foreign'),
                     lambda e: e['result'].update(selected_manifest='sha256:' + '0'*64),
                     lambda e: e['result']['layers'][0].update(digest='sha256:' + '0'*64),
                     lambda e: e['result']['layers'][0]['distribution']['members'][0].update(size_actual=10**12),
                     lambda e: e['result']['config']['platform'].update(Env=['TOPSECRET']),
                     lambda e: e['result']['coverage'].update(merged_filesystem=True),
                     lambda e: e['result']['layers'][0].update(whiteouts=[dict(member_index=99, name='.wh.bad', kind='remove')])]
        for mutate in mutations:
            with self.subTest(mutation=repr(mutate)):
                result = self.run_worker(worker_command=self.forged(mutate))
                self.assertEqual(result['error'], 'invalid-response', result)
                self.assertIsNone(result['result'])
                self.assertEqual(result['cleanup']['leftovers'][-1]['path'], str(self.scratch))
        result = self.run_worker(worker_command=self.fake('print("TOPSECRET")'))
        self.assertEqual(result['error'], 'invalid-response')
        self.assertNotIn('TOPSECRET', json.dumps(result))

    def test_explicit_manifest_selection_cannot_be_changed_by_worker(self):
        result = self.run_worker(manifest_digest='sha256:' + '0'*64,
                                 worker_command=self.forged(lambda e: None))
        self.assertEqual(result['error'], 'invalid-response')

    def test_cleanup_ledger_survives_partial_and_rejects_outside_paths(self):
        def retained(e):
            e['cleanup'] = dict(leftovers=[dict(path=str(self.scratch / 'owned'), reason='retained')],
                                individual_paths_complete=True)
            e['result']['cleanup'] = dict(status='retained', individual_paths_complete=True,
                leftovers=[dict(path=str(self.scratch / 'owned'), reason='retained', identity_verified=False)])
        result = self.run_worker(worker_command=self.forged(retained))
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(result['cleanup']['leftovers'][0]['path'], str(self.scratch / 'owned'))
        def outside(e):
            e['cleanup']['leftovers'] = [dict(path='/outside', reason='bad')]
        self.assertEqual(self.run_worker(worker_command=self.forged(outside))['error'], 'invalid-response')

    def test_limits_and_invalid_handles_fail_before_spawn(self):
        with mock.patch.object(worker.subprocess, 'Popen') as spawn:
            for over in (dict(memory_bytes=worker.MAX_MEMORY_BYTES+1), dict(deadline=float('nan')),
                         dict(manifest_digest='sha256:../bad'), dict(max_response_bytes=0)):
                with self.subTest(over=over), self.assertRaises(worker.OCIWorkerError):
                    self.run_worker(**over)
            with mock.patch.object(worker.signal, 'getsignal', return_value=signal.SIG_IGN):
                with self.assertRaises(worker.OCIWorkerError):
                    self.run_worker()
            result = self.run_worker(deadline=time.monotonic()-1)
            self.assertEqual(result['error'], 'deadline-exceeded')
            spawn.assert_not_called()
        for fd in (-1, True):
            with self.assertRaises(worker.OCIWorkerError):
                worker.inspect_isolated(fd, layout_path=str(self.layout), checkout=str(self.checkout),
                    scratch_base=str(self.scratch), deadline=time.monotonic()+10)

    def test_interrupt_and_secondary_close_preserve_primary_and_owned_fds(self):
        primary = KeyboardInterrupt('primary')
        original_close = os.close
        closed = []
        def close_then_fail(fd):
            original_close(fd)
            closed.append(fd)
            if len(closed) == 1:
                raise OSError('secondary private detail')
        count = len(os.listdir('/proc/self/fd'))
        with mock.patch.object(worker, '_exchange', side_effect=primary), \
             mock.patch.object(worker, '_close_all', wraps=worker._close_all):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_worker(worker_command=self.fake('import time;time.sleep(30)'))
        self.assertIs(caught.exception, primary)
        self.assertTrue(primary.oci_cleanup['worker']['leader_reaped'])
        self.assertEqual(primary.oci_cleanup['leftovers'][-1]['path'], str(self.scratch))
        self.assertEqual(len(os.listdir('/proc/self/fd')), count)
        # Inject only final parent-owned fd close failures, not worker/pipe closes.
        original_all = worker._close_all
        primary = KeyboardInterrupt('second')
        def closing(handles, active):
            if handles and all(type(h) is int for h in handles):
                with mock.patch.object(os, 'close', side_effect=close_then_fail):
                    return original_all(handles, active)
            return original_all(handles, active)
        with mock.patch.object(worker, '_exchange', side_effect=primary), \
             mock.patch.object(worker, '_close_all', side_effect=closing):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_worker(worker_command=self.fake('import time;time.sleep(30)'))
        self.assertIs(caught.exception, primary)
        self.assertEqual(len(closed), 3)
        self.assertEqual(len(os.listdir('/proc/self/fd')), count)
        os.fstat(self.fd)

    def test_actual_resource_limit_is_applied_before_inspection(self):
        marker = self.base / 'limit.json'
        script = self.fake(
            'import json,sys,resource\n'
            'r=json.loads(sys.stdin.readline())\n'
            'sys.path.insert(0,' + repr(str(SCRIPTS)) + ')\n'
            'from mdi.oci_worker import _set_memory_limit\n'
            '_set_memory_limit(r["memory_bytes"])\n'
            'open(' + repr(str(marker)) + ',"w").write(json.dumps(resource.getrlimit(resource.RLIMIT_AS)))\n'
            'print("invalid response")\n')
        self.run_worker(worker_command=script, memory_bytes=128 * 1024**2)
        self.assertEqual(json.loads(marker.read_text()), [128 * 1024**2] * 2)

    def test_missing_resource_control_refused_before_spawn(self):
        with mock.patch.object(worker.boundary, 'resource', None), \
             mock.patch.object(worker.subprocess, 'Popen') as spawn:
            with self.assertRaises(worker.OCIWorkerError):
                self.run_worker()
            spawn.assert_not_called()

    def test_partial_limit_results_remain_valid_inventory(self):
        for limits in (OCILimits(max_json_bytes=1), OCILimits(max_descriptors=1),
                       OCILimits(max_expanded_bytes=512), OCILimits(max_inventory_bytes=100),
                       OCILimits(max_member_bytes=10), OCILimits(max_blob_bytes=1)):
            with self.subTest(limits=limits):
                result = self.run_worker(limits=limits)
                self.assertEqual(result['outcome'], 'complete', result)
                self.assertEqual(result['result']['coverage']['status'], 'partial')
                self.assertTrue(result['result']['issues'])

    def test_saturated_cleanup_ledger_keeps_all_details_and_root(self):
        def saturate(e):
            rows = [dict(path=str(self.scratch / str(i)), reason='retained') for i in range(31)]
            rows.append(dict(path=str(self.scratch), reason='cleanup-ledger-limit'))
            e['cleanup'] = dict(leftovers=rows, individual_paths_complete=False)
            e['result']['cleanup'] = dict(status='retained', individual_paths_complete=False,
                leftovers=[dict(row, identity_verified=False) for row in rows])
        result = self.run_worker(worker_command=self.forged(saturate))
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(len(result['cleanup']['leftovers']), 32)
        self.assertFalse(result['cleanup']['individual_paths_complete'])

    def test_startup_consumes_existing_deadline(self):
        original = subprocess.Popen
        def delayed(*args, **kwargs):
            time.sleep(.15)
            return original(*args, **kwargs)
        with mock.patch.object(worker.subprocess, 'Popen', side_effect=delayed):
            result = self.run_worker(worker_command=self.fake('import time;time.sleep(30)'),
                                     deadline=time.monotonic() + .1)
        self.assertEqual(result['error'], 'deadline-exceeded')
        self.assert_reaped(result['worker_pid'])

    def test_signals_precede_reap_and_lost_identity_never_signals(self):
        original = os.killpg
        signals = []
        def owned_signal(pid, sig):
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            signals.append(pid)
            return original(pid, sig)
        with mock.patch.object(worker.boundary.os, 'killpg', side_effect=owned_signal):
            result = self.run_worker()
        self.assertTrue(signals)
        self.assertEqual(result['cleanup_result']['returncode'], 0)
        reaped = []
        def externally_reaped(proc):
            proc.wait(timeout=5)
            reaped.append(proc.pid)
            return 'lost'
        with mock.patch.object(worker.boundary, '_probe', side_effect=externally_reaped), \
             mock.patch.object(worker.boundary.os, 'killpg') as kill:
            result = self.run_worker(worker_command=self.fake('pass'))
        self.assertTrue(reaped)
        self.assert_reaped(result['worker_pid'])
        kill.assert_not_called()
        self.assertEqual(result['error'], 'worker-cleanup-unconfirmed')
        self.assertFalse(result['cleanup_result']['worker_stopped'])
        self.assertFalse(result['cleanup']['individual_paths_complete'])

    def test_repeated_real_runs_do_not_leak_descriptors(self):
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(3):
            self.assertEqual(self.run_worker()['outcome'], 'complete')
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_isolated_worker_does_not_write_into_copied_skill(self):
        copied = self.base / 'skill'
        shutil.copytree(SCRIPTS.parent, copied, ignore=shutil.ignore_patterns('__pycache__'))
        def hashes():
            return {str(p.relative_to(copied)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in copied.rglob('*') if p.is_file()}
        before = hashes()
        command = [sys.executable, '-B', '-I', '-S', str(copied / 'scripts/mdi/oci_worker.py'), '--worker']
        with mock.patch.dict(os.environ, {'PYTHONPATH': str(self.base), 'PYTHONSTARTUP': 'TOPSECRET',
                                         'HTTP_PROXY': 'http://secret', 'JAVA_TOOL_OPTIONS': 'bad'}):
            result = self.run_worker(worker_command=command)
        self.assertEqual(result['outcome'], 'complete', result)
        self.assertEqual(hashes(), before)


if __name__ == '__main__':
    unittest.main()

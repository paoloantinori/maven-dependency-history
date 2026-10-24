"""File-backed discovery integration; local ZIP fixtures and trusted workers only."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests.test_zip_preflight import zip64_bytes

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'maven-dependency-investigator' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from mdi import candidates
from tests import child_env


POM = b'<project><modelVersion>4.0.0</modelVersion><groupId>fixture</groupId><artifactId>app</artifactId><version>1</version></project>'
METADATA = 'META-INF/maven/fixture/library/pom.properties'
PROPERTIES = b'groupId=fixture\nartifactId=library\nversion=1\n'


def library():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(METADATA, PROPERTIES)
    return stream.getvalue()


class FileDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'pom.xml').write_bytes(POM)
        self.target = self.repo / 'target'
        self.target.mkdir()
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir(mode=0o700)

    def discover(self, **kwargs):
        return candidates.discover_candidates(
            self.repo, 'fixture:library:1', archive_scratch_base=self.scratch, **kwargs)

    def test_zip64_nested_metadata_and_identity_survive_real_worker(self):
        outer = self.target / 'app.jar'
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.writestr('BOOT-INF/lib/library.jar', zip64_bytes(library()))
        result = self.discover()
        self.assertEqual(len(result['candidates']), 1)
        candidate = result['candidates'][0]
        self.assertEqual(candidate['coordinates']['artifactId'], 'library')
        self.assertEqual(candidate['copies'][0]['inspection_status'], 'complete')
        self.assertEqual(len(candidate['copies'][0]['sha256']), 64)
        coverage = result['coverage']['archives']
        self.assertEqual(coverage['backend'], 'isolated-file')
        self.assertEqual(coverage['reader_limits']['max_input_bytes'], 2 * 1024**3)
        self.assertEqual(coverage['pipeline_observations'][0]['cleanup_result']['status'], 'complete')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_isolated_reader_preserves_exclamation_names_and_duplicate_chains(self):
        old_repo = self.repo
        self.repo = self.root / 'repo!part'
        old_repo.rename(self.repo)
        self.target = self.repo / 'target'
        empty = io.BytesIO()
        with zipfile.ZipFile(empty, 'w'):
            pass
        outer = self.target / 'needle!outer.jar'
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message='Duplicate name:', category=UserWarning)
            with zipfile.ZipFile(outer, 'w') as archive:
                archive.writestr('lib/needle!inner.jar', empty.getvalue())
                archive.writestr('lib/needle!inner.jar', empty.getvalue())
        result = candidates.discover_candidates(
            self.repo, 'needle', artifact=outer, archive_scratch_base=self.scratch)
        copies = [candidate['copies'][0] for candidate in result['candidates']]
        self.assertEqual(sorted(copy['entry_chain'] for copy in copies), [[], [0], [1]])
        self.assertEqual({copy['display_path'] for copy in copies},
                         {'needle!outer.jar', 'needle!outer.jar!lib/needle!inner.jar'})
        self.assertEqual(len({candidate['id'] for candidate in result['candidates']}), 3)
        self.assertEqual(result['coverage']['archives']['captured'], 1)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_sparse_outer_larger_than_old_cap_is_inspected(self):
        outer = self.target / 'large.jar'
        with outer.open('wb') as stream:
            stream.seek(65 * 1024**2)
            stream.write(library())
        result = self.discover(artifact=outer)
        self.assertEqual(len(result['candidates']), 1)
        coverage = result['coverage']['archives']
        self.assertEqual(coverage['captured'], 1)
        observation = coverage['pipeline_observations'][0]
        self.assertTrue(observation['capture']['complete'])
        self.assertGreater(observation['capture']['size'], 64 * 1024**2)
        self.assertEqual(result['candidates'][0]['copies'][0]['inspection_status'], 'complete')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_shared_deadline_and_original_descriptor_not_path_reopen(self):
        for name in ('a.jar', 'b.jar'):
            (self.target / name).write_bytes(library())
        deadlines = []
        now = [100.0]
        offsets = []

        def inspect(fd, **kwargs):
            deadlines.append(kwargs['deadline'])
            offsets.append(os.lseek(fd, 0, os.SEEK_CUR))
            self.assertEqual(os.pread(fd, 2, 0), b'PK')
            now[0] += 0.6
            return {'outcome': 'partial', 'result': None, 'capture': None,
                    'cleanup_result': {'status': 'not-created', 'leftovers': []},
                    'worker_cleanup': None, 'quota': {},
                    'issues': [{'code': 'deadline-exceeded'}], 'limitations': []}

        with mock.patch.object(candidates.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(candidates, 'inspect_archive_pipeline', side_effect=inspect):
            result = self.discover(archive_seconds=1)
        self.assertEqual(deadlines, [101.0, 101.0])
        self.assertEqual(offsets, [0, 0])
        self.assertIn('archive-stage-deadline', result['coverage']['archives']['truncated'])
        self.assertEqual(result['candidates'], [])

    def test_failed_explicit_pipeline_is_not_implicitly_retried(self):
        outer = self.target / 'library.jar'
        outer.write_bytes(library())
        fake = {'outcome': 'failed', 'result': None, 'capture': None,
                'cleanup_result': {'status': 'retained', 'leftovers': [{'path': str(self.scratch), 'reason': 'worker-cleanup-unconfirmed'}]},
                'worker_cleanup': None, 'quota': {},
                'issues': [{'code': 'worker-cleanup-unconfirmed'}], 'limitations': []}
        with mock.patch.object(candidates, 'inspect_archive_pipeline', return_value=fake) as run:
            result = self.discover(artifact=outer)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['coverage']['archives']['pipeline_observations'][0]['cleanup_result'], fake['cleanup_result'])
        self.assertEqual(result['candidates'], [])

    def test_expired_stage_starts_no_capture(self):
        (self.target / 'a.jar').write_bytes(library())
        # Allow stage creation at 100; every subsequent clock read is expired.
        times = iter([100.0])
        with mock.patch.object(candidates, 'checkout_context', return_value={'status': 'unavailable'}), \
             mock.patch.object(candidates.time, 'monotonic', side_effect=lambda: next(times, 102.0)), \
             mock.patch.object(candidates, 'inspect_archive_pipeline') as inspect:
            result = self.discover(archive_seconds=1)
        inspect.assert_not_called()
        self.assertIn('archive-stage-deadline', result['coverage']['archives']['truncated'])

    def test_unsafe_scratch_and_nonregular_archive_refused(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.scratch, target_is_directory=True)
        with self.assertRaises(OSError):
            candidates.discover_candidates(self.repo, 'library', archive_scratch_base=alias)
        with self.assertRaises(ValueError):
            candidates.discover_candidates(self.repo, 'library', archive_scratch_base=self.target)
        os.mkfifo(self.target / 'bad.jar')
        (self.target / 'good.jar').write_bytes(library())
        result = self.discover()
        self.assertEqual(len(result['candidates']), 1)
        self.assertTrue(any(item['code'] == 'target-entry-failed' for item in result['issues']))

    def test_open_archive_fd_survives_target_directory_replacement(self):
        outer = self.target / 'library.jar'
        outer.write_bytes(library())
        moved = self.repo / 'old-target'
        original_pipeline = candidates.inspect_archive_pipeline

        def inspect(fd, **kwargs):
            self.target.rename(moved)
            self.target.mkdir()
            (self.target / 'library.jar').write_bytes(b'not the opened archive')
            return original_pipeline(fd, **kwargs)

        with mock.patch.object(candidates, 'inspect_archive_pipeline', side_effect=inspect):
            result = self.discover()
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['coordinates']['version'], '1')
        self.assertEqual((self.target / 'library.jar').read_bytes(), b'not the opened archive')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_scratch_directory_cleanup_preserves_primary_interrupt(self):
        from mdi import cli
        base = self.root / 'state'
        base.mkdir()
        original_close = os.close
        before = len(os.listdir('/proc/self/fd'))
        interrupted = KeyboardInterrupt('original')
        manager = cli._archive_scratch(str(base))
        manager.__enter__()
        def close_and_fail(fd):
            original_close(fd)
            raise OSError('injected close failure')
        with mock.patch.object(cli.os, 'close', side_effect=close_and_fail):
            with self.assertRaises(KeyboardInterrupt) as caught:
                try:
                    raise interrupted
                except KeyboardInterrupt:
                    self.assertFalse(manager.__exit__(*sys.exc_info()))
                    raise
        self.assertIs(caught.exception, interrupted)
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_overlapping_scratch_context_refused_without_touching_owner(self):
        from mdi import cli
        from mdi.runstore import RunConflict
        base = self.root / 'state'
        base.mkdir()
        with cli._archive_scratch(str(base)) as scratch:
            marker = Path(scratch) / 'owned-sentinel'
            marker.write_text('unchanged')
            with self.assertRaises(RunConflict):
                with cli._archive_scratch(str(base)):
                    self.fail('concurrent caller acquired scratch')
            self.assertEqual(marker.read_text(), 'unchanged')
            marker.unlink()
            self.assertTrue(Path(scratch).is_dir())
        self.assertFalse((base / 'archive-scratch').exists())
        # A subsequent discovery can acquire the same base after cleanup.
        with cli._archive_scratch(str(base)) as scratch:
            self.assertTrue(Path(scratch).is_dir())
        self.assertEqual(list(base.iterdir()), [])

    def test_concurrent_cli_discovery_refused_before_inspection(self):
        from mdi import cli
        base = self.root / 'state'
        base.mkdir()
        (self.target / 'library.jar').write_bytes(library())
        with cli._archive_scratch(str(base)) as scratch:
            marker = Path(scratch) / 'owned-sentinel'
            marker.write_text('unchanged')
            command = [sys.executable, str(SCRIPTS / 'investigate.py'), 'discover',
                       '--repo', str(self.repo), '--query', 'fixture:library:1',
                       '--state-dir', str(base)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                   env=child_env())
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn('busy', json.loads(result.stdout)['error']['message'])
            self.assertEqual(marker.read_text(), 'unchanged')
            self.assertEqual(sorted(p.name for p in base.iterdir()), ['archive-scratch'])
            self.assertEqual(sorted(p.name for p in Path(scratch).iterdir()), ['owned-sentinel'])
            marker.unlink()

    def test_scratch_lock_unavailable_refuses_before_directory_creation(self):
        from mdi import cli
        base = self.root / 'state'
        base.mkdir()
        before = len(os.listdir('/proc/self/fd'))
        with mock.patch.object(cli.fcntl, 'flock', side_effect=OSError('unsupported')):
            with self.assertRaises(cli.CliError):
                with cli._archive_scratch(str(base)):
                    self.fail('inspection entered without locking')
        self.assertEqual(list(base.iterdir()), [])
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def test_scratch_lock_released_after_interruption(self):
        from mdi import cli
        base = self.root / 'state'
        base.mkdir()
        with self.assertRaises(KeyboardInterrupt):
            with cli._archive_scratch(str(base)):
                raise KeyboardInterrupt('stop')
        with cli._archive_scratch(str(base)) as scratch:
            self.assertTrue(Path(scratch).is_dir())
        self.assertEqual(list(base.iterdir()), [])

    def test_scratch_lock_held_through_directory_removal(self):
        from mdi import cli
        from mdi.runstore import RunConflict
        base = self.root / 'state'
        base.mkdir()
        real_rmdir = os.rmdir
        attempts = []

        def competing_removal(path, *args, **kwargs):
            if path == 'archive-scratch':
                with self.assertRaises(RunConflict):
                    with cli._archive_scratch(str(base)):
                        self.fail('lock released before directory removal')
                attempts.append(path)
            return real_rmdir(path, *args, **kwargs)

        with mock.patch.object(cli.os, 'rmdir', side_effect=competing_removal):
            with cli._archive_scratch(str(base)):
                pass
        self.assertEqual(attempts, ['archive-scratch'])
        self.assertEqual(list(base.iterdir()), [])

    def test_cli_default_uses_large_pipeline_and_outside_scratch(self):
        (self.target / 'library.jar').write_bytes(zip64_bytes(library()))
        state = self.root / 'state'
        command = [sys.executable, str(SCRIPTS / 'investigate.py'), 'discover',
                   '--repo', str(self.repo), '--query', 'fixture:library:1', '--state-dir', str(state)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                               env=child_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(len(response['candidates']), 1)
        report = json.loads((Path(response['run_dir']) / 'report-00000001.json').read_text())
        context = next(ev for ev in report['evidence'] if ev['category'] == 'discovery-context')
        coverage = context['payload']['coverage']['archives']
        self.assertEqual(coverage['backend'], 'isolated-file')
        self.assertEqual(coverage['stage_seconds'], 120)
        self.assertTrue(all(str(state) in item['capture']['scratch_dir'] for item in coverage['pipeline_observations']))
        self.assertFalse((state / 'archive-scratch').exists())
        self.assertEqual(sorted(p.name for p in self.repo.iterdir()), ['pom.xml', 'target'])


if __name__ == '__main__':
    unittest.main()

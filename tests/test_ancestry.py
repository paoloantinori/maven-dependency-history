"""Local commit-DAG proofs for the pure graph-history comparator."""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'maven-dependency-investigator' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import ancestry
from mdi.gitinspect import _RunOutcome
from mdi.graph_history import compare_graph_snapshots
from tests.test_graph_history import node, snapshot
from tests import child_env


def evidence(identifier, revision):
    return {'id': identifier, 'payload': {'import': {
        'kind': 'dependency-tree', 'provenance': {'revision': revision}}}}


class AncestryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.git = shutil.which('git')
        if self.git is None:
            self.fail('Git is required for ancestry tests; no download or skip')
        self.env = {'PATH': os.defpath, 'HOME': str(self.home), 'LC_ALL': 'C',
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                    'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
                    'GIT_AUTHOR_DATE': '2026-01-01T00:00:00+00:00',
                    'GIT_COMMITTER_DATE': '2026-01-01T00:00:00+00:00'}
        self.run_git('init', '-q')
        self.tree = self.run_git('mktree', input=b'').strip()
        self.a = self.commit('a')
        self.b = self.commit('b', self.a)
        self.c = self.commit('c', self.a)
        self.merge = self.commit('merge', self.b, self.c)
        self.run_git('update-ref', 'refs/heads/main', self.merge)
        self.run_git('symbolic-ref', 'HEAD', 'refs/heads/main')

    def run_git(self, *args, input=None):
        result = subprocess.run([self.git, '-C', str(self.repo), *args], input=input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.env, check=True, timeout=15)
        return result.stdout.decode('ascii')

    def commit(self, label, *parents):
        args = ['commit-tree', self.tree]
        for parent in parents:
            args.extend(['-p', parent])
        return self.run_git(*args, input=(label + '\n').encode()).strip()

    def collect(self, values=None, **kwargs):
        values = values if values is not None else [evidence('a', self.a), evidence('b', self.b)]
        return ancestry.collect_ancestry(self.repo, values, evidence_id='git-proof', **kwargs)

    def test_real_dag_edges_prove_ancestry_not_hash_sort(self):
        values = [evidence('merge', self.merge), evidence('c', self.c),
                  evidence('b', self.b), evidence('a', self.a)]
        before = copy.deepcopy(values)
        result = self.collect(values)
        self.assertEqual(values, before)
        self.assertEqual(result['coverage']['status'], 'complete')
        edges = {(row['before'], row['after']) for row in result['ancestry']}
        self.assertEqual(edges, {('a', 'b'), ('a', 'c'), ('a', 'merge'),
                                 ('b', 'merge'), ('c', 'merge')})
        self.assertNotIn(('b', 'c'), edges)
        self.assertNotIn(('c', 'b'), edges)
        self.assertTrue(all(e['verified'] is True and e['evidence_ids'] == ['git-proof']
                            for e in result['ancestry']))
        self.assertEqual(result['proof_evidence_ids'], ['git-proof'])
        self.assertEqual(result['evidence'][0]['category'], 'git-ancestry')
        self.assertEqual(result['evidence'][0]['source_locator'], str(self.repo))
        self.assertIsNotNone(result['evidence'][0]['observed_at'])
        json.dumps(result, allow_nan=False)

    def test_proofs_are_consumable_by_graph_comparator(self):
        a = snapshot('a', self.a)
        b = snapshot('b', self.b, [node('c')])
        proofs = self.collect([a, b])
        compared = compare_graph_snapshots([b, a], 'example:c',
                    ancestry=proofs['ancestry'], proof_evidence_ids=proofs['proof_evidence_ids'])
        self.assertTrue(compared['events'])
        self.assertEqual(compared['events'][0]['kind'], 'graph-addition')

    def test_unrelated_commits_are_not_ordered(self):
        result = self.collect([evidence('b', self.b), evidence('c', self.c)])
        self.assertEqual(result['ancestry'], [])
        self.assertEqual(result['evidence'][0]['payload']['pairs'][0]['relation'], 'unrelated')

    def test_missing_noncommit_and_unknown_revisions_are_partial(self):
        blob = self.run_git('hash-object', '-w', '--stdin', input=b'not a commit').strip()
        result = self.collect([evidence('missing', 'f' * 40), evidence('blob', blob),
                               evidence('unknown', None), evidence('valid', self.a)])
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(result['ancestry'], [])
        codes = {i['code'] for i in result['issues']}
        self.assertTrue({'revision-unavailable', 'not-a-commit', 'revision-unknown'} <= codes)
        self.assertTrue(all(p['relation'] == 'unknown' for p in result['evidence'][0]['payload']['pairs']))

    def test_refs_abbreviations_and_injection_rejected_without_git(self):
        for value in ('HEAD', self.a[:12], '-h', '--help', self.a + '^{commit}',
                      self.a + '\n', '${REV}', True, {}, ''):
            with self.subTest(value=value), mock.patch.object(ancestry, '_run') as run:
                with self.assertRaises(ancestry.AncestryError):
                    self.collect([evidence('a', value)])
                run.assert_not_called()

    def test_duplicate_ids_and_proof_collision_rejected(self):
        for items in ([evidence('a', self.a)] * 2, [evidence('git-proof', self.a)]):
            with self.assertRaises(ancestry.AncestryError):
                self.collect(items)

    def test_same_revision_has_no_artificial_order(self):
        result = self.collect([evidence('a', self.a), evidence('other', self.a)])
        self.assertEqual(result['ancestry'], [])
        self.assertEqual(result['evidence'][0]['payload']['pairs'][0]['relation'], 'same-revision')
        self.assertEqual(result['coverage']['status'], 'partial')

    def test_shallow_history_is_unknown_even_when_endpoints_present(self):
        (self.repo / '.git' / 'shallow').write_text(self.b + '\n')
        result = self.collect()
        self.assertEqual(result['ancestry'], [])
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertIn('shallow-history', {x['code'] for x in result['issues']})
        self.assertTrue(all(p['relation'] == 'unknown' for p in result['evidence'][0]['payload']['pairs']))

    def test_legacy_grafts_refused(self):
        (self.repo / '.git' / 'info' / 'grafts').write_text(self.b + '\n')
        result = self.collect()
        self.assertEqual(result['ancestry'], [])
        self.assertIn('grafts-present', {x['code'] for x in result['issues']})

    def test_replacement_refs_do_not_rewrite_ancestry(self):
        independent = self.commit('independent')
        self.run_git('replace', self.b, independent)
        result = self.collect()
        self.assertEqual([(x['before'], x['after']) for x in result['ancestry']], [('a', 'b')])

    def test_hooks_filters_and_inherited_options_never_execute(self):
        marker = self.root / 'executed'
        helper = self.root / 'helper'
        helper.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        helper.chmod(0o700)
        for key in ('filter.evil.clean', 'filter.evil.process', 'core.fsmonitor', 'core.pager'):
            self.run_git('config', key, str(helper))
        hookdir = self.root / 'hooks'
        hookdir.mkdir()
        (hookdir / 'post-checkout').symlink_to(helper)
        self.run_git('config', 'core.hooksPath', str(hookdir))
        self.run_git('config', 'remote.origin.url', 'https://must-not-contact.invalid/repo')
        self.run_git('config', 'remote.origin.promisor', 'true')
        index = self.repo / '.git' / 'index'
        before = index.read_bytes() if index.exists() else None
        with mock.patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'alias.merge-base',
                                          'GIT_CONFIG_VALUE_0': '!' + str(helper),
                                          'GIT_EXTERNAL_DIFF': str(helper)}):
            result = self.collect()
            missing = self.collect([evidence('a', self.a), evidence('missing', 'f' * 40)])
        self.assertEqual(result['coverage']['status'], 'complete')
        self.assertEqual(missing['ancestry'], [])
        self.assertFalse(marker.exists())
        self.assertEqual(index.read_bytes() if index.exists() else None, before)

    def test_invalid_limits_refused_before_git(self):
        for kwargs in ({'timeout_seconds': float('nan')}, {'timeout_seconds': float('inf')},
                       {'timeout_seconds': True}, {'max_output_bytes': 0}, {'max_snapshots': True}):
            with self.subTest(kwargs=kwargs), mock.patch.object(ancestry, '_run') as run:
                with self.assertRaises(ancestry.AncestryError):
                    self.collect(**kwargs)
                run.assert_not_called()

    def test_nondefault_sigchld_refuses_before_any_git_command(self):
        # Isolate signal policy from the unittest process and fixture creation.
        script = '''
import json, signal, sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
from mdi import ancestry
repo, first, second, policy = sys.argv[2:]
rows = [{'id': label, 'payload': {'import': {'kind': 'dependency-tree',
        'provenance': {'revision': revision}}}}
        for label, revision in [('b', first), ('c', second)]]
baseline = ancestry.collect_ancestry(repo, rows, evidence_id='proof')
assert baseline['ancestry'] == []
assert baseline['evidence'][0]['payload']['pairs'][0]['relation'] == 'unrelated'
signal.signal(signal.SIGCHLD, signal.SIG_IGN if policy == 'ignore' else lambda *args: None)
original = ancestry._run
calls = []
def counted(*args, **kwargs):
    calls.append(args[0])
    return original(*args, **kwargs)
with mock.patch.object(ancestry, '_run', side_effect=counted):
    result = ancestry.collect_ancestry(repo, rows, evidence_id='proof')
print(json.dumps({'result': result, 'calls': calls}))
'''
        for policy in ('ignore', 'handler'):
            with self.subTest(policy=policy):
                child = subprocess.run([sys.executable, '-c', script, str(SCRIPTS),
                                        str(self.repo), self.b, self.c, policy],
                                       capture_output=True, text=True, timeout=15,
                                       env=child_env())
                self.assertEqual(child.returncode, 0, child.stderr)
                output = json.loads(child.stdout)
                self.assertEqual(output['calls'], [])
                self.assertEqual(output['result']['ancestry'], [])
                self.assertEqual(output['result']['coverage']['status'], 'unavailable')
                self.assertIn('unsupported-child-reaping',
                              {i['code'] for i in output['result']['issues']})

    def test_foreign_or_uninspectable_signal_disposition_refuses(self):
        for value in (None, 123):
            with self.subTest(value=value), mock.patch('signal.getsignal', return_value=value), \
                    mock.patch.object(ancestry, '_run') as run:
                result = self.collect()
                self.assertEqual(result['ancestry'], [])
                self.assertEqual(result['coverage']['status'], 'unavailable')
                run.assert_not_called()

    def test_reaping_policy_change_during_command_invalidates_proof(self):
        import signal
        original = ancestry._run
        policy = signal.SIG_DFL
        def change_policy(argv, *args, **kwargs):
            nonlocal policy
            result = original(argv, *args, **kwargs)
            if 'merge-base' in argv:
                policy = signal.SIG_IGN
                return _RunOutcome('ok', 0, b'', b'', None)
            return result
        with mock.patch('signal.getsignal', side_effect=lambda *_: policy), \
                mock.patch.object(ancestry, '_run', side_effect=change_policy):
            result = self.collect()
        self.assertEqual(result['ancestry'], [])
        self.assertIn('unsupported-child-reaping', {i['code'] for i in result['issues']})

    def test_missing_or_nonscalar_returncode_never_proves_ancestry(self):
        original = ancestry._run
        for rc in (None, False, 0.0):
            def missing(argv, *args, **kwargs):
                if 'merge-base' in argv:
                    return _RunOutcome('ok', rc, b'', b'', None)
                return original(argv, *args, **kwargs)
            with self.subTest(rc=rc), mock.patch.object(ancestry, '_run', side_effect=missing):
                result = self.collect()
                self.assertEqual(result['ancestry'], [])
                self.assertIn('git-exit-status-unavailable', {i['code'] for i in result['issues']})

    def test_transport_timeout_never_becomes_negative_ancestry(self):
        original = ancestry._run
        def timed_out(argv, *args, **kwargs):
            if 'merge-base' in argv:
                return _RunOutcome('timeout', -9, b'', b'', 'private diagnostic')
            return original(argv, *args, **kwargs)
        with mock.patch.object(ancestry, '_run', side_effect=timed_out):
            result = self.collect()
        self.assertEqual(result['ancestry'], [])
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(result['evidence'][0]['payload']['pairs'][0]['relation'], 'unknown')
        self.assertNotIn('private diagnostic', json.dumps(result))

    def test_interrupt_propagates_and_git_receives_only_safe_environment(self):
        original = ancestry._run
        seen = []
        def interrupted(argv, *args, **kwargs):
            seen.append((argv, kwargs['env']))
            if 'merge-base' in argv:
                raise KeyboardInterrupt('stop')
            return original(argv, *args, **kwargs)
        with mock.patch.object(ancestry, '_run', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.collect()
        self.assertTrue(seen)
        for argv, env in seen:
            self.assertTrue(os.path.isabs(argv[0]))
            self.assertIn('--no-lazy-fetch', argv)
            self.assertIn('--no-replace-objects', argv)
            self.assertEqual(env['GIT_NO_LAZY_FETCH'], '1')
            self.assertEqual(env['GIT_NO_REPLACE_OBJECTS'], '1')
            self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
            self.assertNotIn('GIT_CONFIG_COUNT', env)
            self.assertFalse(Path(env['HOME']).exists())

    def test_command_failure_not_negative_ancestry(self):
        original = ancestry._run
        def broken(argv, *args, **kwargs):
            if 'merge-base' in argv:
                return _RunOutcome('ok', 128, b'', b'SECRET', None)
            return original(argv, *args, **kwargs)
        with mock.patch.object(ancestry, '_run', side_effect=broken):
            result = self.collect()
        self.assertEqual(result['ancestry'], [])
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(result['evidence'][0]['payload']['pairs'][0]['relation'], 'unknown')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_output_budget_and_deadline_shared_across_commands(self):
        captured = []
        original = ancestry._run
        def trace(argv, *args, **kwargs):
            captured.append(kwargs)
            return original(argv, *args, **kwargs)
        with mock.patch.object(ancestry, '_run', side_effect=trace):
            self.collect()
        self.assertGreater(len(captured), 4)
        self.assertTrue(all(x['timeout_seconds'] <= captured[0]['timeout_seconds'] for x in captured))
        self.assertLess(captured[-1]['max_output_bytes'], captured[0]['max_output_bytes'])
        result = self.collect(max_output_bytes=8)
        self.assertEqual(result['ancestry'], [])
        self.assertNotEqual(result['coverage']['status'], 'complete')

    def test_final_policy_change_invalidates_collected_edges(self):
        original = ancestry._history_available
        count = 0
        def changed(git):
            nonlocal count
            count += 1
            if count == 2:
                (self.repo / '.git' / 'shallow').write_text(self.b + '\n')
            return original(git)
        with mock.patch.object(ancestry, '_history_available', side_effect=changed):
            result = self.collect()
        self.assertEqual(count, 2)
        self.assertEqual(result['ancestry'], [])
        self.assertEqual(result['evidence'][0]['payload']['pairs'],
                         [{'first': 'a', 'second': 'b', 'relation': 'unknown'}])
        self.assertEqual(result['coverage']['status'], 'partial')

    def test_unknown_hash_format_refused_without_object_probes(self):
        original = ancestry._run
        commands = []
        def unsupported(argv, *args, **kwargs):
            commands.append(argv)
            if '--show-object-format' in argv:
                return _RunOutcome('ok', 0, b'unknown-format\n', b'', None)
            return original(argv, *args, **kwargs)
        with mock.patch.object(ancestry, '_run', side_effect=unsupported):
            result = self.collect()
        self.assertEqual(result['ancestry'], [])
        self.assertFalse(any('cat-file' in a for a in commands))
        self.assertIn('unsupported-object-format', {i['code'] for i in result['issues']})

    def test_record_is_report_compatible_and_detached_from_input(self):
        from mdi.report import new_report, validate_report
        snapshots = [evidence('a', self.a), evidence('b', self.b)]
        result = self.collect(snapshots)
        report = new_report('test-run')
        report['evidence'].extend(result['evidence'])
        validate_report(report)
        result['evidence'][0]['payload']['snapshots'][0]['revision'] = 'changed'
        self.assertEqual(snapshots[0]['payload']['import']['provenance']['revision'], self.a)

    def test_empty_and_single_input_are_insufficient(self):
        for values in ([], [evidence('a', self.a)]):
            result = self.collect(values)
            self.assertEqual(result['coverage']['status'], 'partial')
            self.assertIn('insufficient-snapshots', {x['code'] for x in result['issues']})

    def test_nonroot_and_symlink_checkout_refused(self):
        sub = self.repo / 'sub'
        sub.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        for path in (sub, alias):
            result = ancestry.collect_ancestry(path, [evidence('a', self.a), evidence('b', self.b)],
                                               evidence_id='proof')
            self.assertEqual(result['coverage']['status'], 'unavailable')
            self.assertEqual(result['ancestry'], [])

    def test_sha256_storage_hashes(self):
        other = self.root / 'sha256'
        other.mkdir()
        subprocess.run([self.git, '-C', str(other), 'init', '-q', '--object-format=sha256'],
                       check=True, capture_output=True, env=self.env, timeout=15)
        self.repo = other
        self.tree = self.run_git('mktree', input=b'').strip()
        a = self.commit('a')
        b = self.commit('b', a)
        self.run_git('update-ref', 'refs/heads/main', b)
        self.run_git('symbolic-ref', 'HEAD', 'refs/heads/main')
        result = self.collect([evidence('a', a), evidence('b', b)])
        self.assertEqual(result['coverage']['object_format'], 'sha256')
        self.assertEqual(result['coverage']['status'], 'complete')
        self.assertEqual(len(result['ancestry']), 1)


if __name__ == '__main__':
    unittest.main()

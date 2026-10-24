"""Copied-skill CLI graph history using real local Git and recorded trees."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_cli import CliTestCase, _jar_bytes
from tests.test_graph_history import node
from tests.test_maven import tree_provenance


class HistoryCliTests(CliTestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.tmp))
        self.addCleanup(shutil.rmtree, self.work)
        self.project = self.work / 'project'
        self.project.mkdir()
        self.git = shutil.which('git')
        self.assertIsNotNone(self.git, 'Git is required for local ancestry tests')
        self.env = dict(PATH=os.defpath, HOME=str(self.work), LC_ALL='C',
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='a@example.invalid',
                        GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='c@example.invalid',
                        GIT_AUTHOR_DATE='2026-01-01T00:00:00Z',
                        GIT_COMMITTER_DATE='2026-01-01T00:00:00Z')
        self.git_run('init', '-q')
        self.old = self.commit('1')
        self.new = self.commit('2')
        target = self.project / 'target'
        target.mkdir()
        (target / 'app.jar').write_bytes(_jar_bytes({
            'BOOT-INF/lib/c.jar': _jar_bytes({
                'META-INF/maven/example/c/pom.properties':
                b'groupId=example\nartifactId=c\nversion=1\n'})}))
        discovered = self.invoke('discover', '--repo', self.project, '--query', 'example:c',
                                 '--state-dir', self.work / 'state')
        self.run_dir = discovered['run_dir']
        self.revision = discovered['revision']
        selected = self.invoke('select', '--run-dir', self.run_dir,
                               '--expect-revision', self.revision,
                               '--candidate', discovered['candidates'][0]['id'])
        self.revision = selected['revision']

    def git_run(self, *args):
        return subprocess.run([self.git, '-C', str(self.project), *args], env=self.env,
                              capture_output=True, check=True, timeout=15).stdout.decode().strip()

    def commit(self, version):
        (self.project / 'pom.xml').write_text(
            '<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId>'
            '<artifactId>app</artifactId><version>1</version><dependencies><dependency>'
            '<groupId>example</groupId><artifactId>b</artifactId><version>' + version +
            '</version></dependency></dependencies></project>')
        self.git_run('add', 'pom.xml')
        self.git_run('commit', '-qm', 'B version ' + version)
        return self.git_run('rev-parse', 'HEAD')

    def invoke(self, *args, expected=0):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', XDG_STATE_HOME=str(self.work / 'xdg'))
        process = subprocess.run([*self.inv, *map(str, args)], capture_output=True,
                                 text=True, env=env, timeout=45)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        self.assertEqual(process.stderr, '')
        return json.loads(process.stdout)

    def report(self):
        return self.invoke('report', '--run-dir', self.run_dir)

    def import_tree(self, revision, *, present=False, mode='nonverbose', realm='project'):
        path = self.work / ('tree-' + str(self.revision) + '.json')
        path.write_text(json.dumps(node('app', children=[
            node('b', '2' if present else '1', [node('c')] if present else [])])))
        provenance = tree_provenance()
        provenance.update(revision=revision, module='.', format=mode)
        prov = path.with_suffix('.provenance.json')
        prov.write_text(json.dumps(provenance))
        args = ['import', '--run-dir', self.run_dir,
                '--expect-revision', self.revision, '--report', path,
                '--kind', 'tree-json', '--tree-mode', mode, '--provenance', prov]
        if realm is not None:
            args.extend(['--realm', realm])
        result = self.invoke(*args)
        self.revision = result['revision']

    def collect(self, *, graph=True):
        args = ['collect', '--run-dir', self.run_dir, '--expect-revision', self.revision]
        if graph:
            args.append('--graph-history')
        result = self.invoke(*args)
        self.revision = result['revision']
        return result

    def test_archive_only_transitive_graph_and_inherited_qualifications(self):
        self.assertNotIn('example:c', (self.project / 'pom.xml').read_text())
        self.import_tree(self.old)
        self.import_tree(self.new, present=True)
        before = self.report()
        result = self.collect()
        self.assertEqual(result['history_outcome'], 'not_applicable')
        self.assertEqual(result['graph_history_outcome'], 'partial')
        report = self.report()
        self.assertEqual(report['revision'], before['revision'] + 1)
        graph = next(e for e in report['evidence'] if e['category'] == 'graph-history')
        event, = graph['payload']['comparison']['events']
        self.assertEqual(event['kind'], 'graph-addition')
        self.assertEqual([p['artifactId'] for p in event['path']], ['b', 'c'])
        self.assertFalse(graph['payload']['comparison']['historical_build_verified'])
        self.assertTrue(event['ordering']['evidence_ids'])
        self.assertTrue(any(graph['id'] in q['affected_evidence_ids'] for q in report['qualifications']))
        self.assertEqual(report['evidence'][:len(before['evidence'])], before['evidence'])
        self.assertFalse(any(e['category'] == 'declaration-history-candidates' for e in report['evidence']))
        validated = self.invoke('report', '--run-dir', self.run_dir, '--validate')
        self.assertTrue(validated['validation']['valid'])

    def test_no_snapshots_is_partial_and_recollection_has_fresh_ids(self):
        self.collect()
        before = self.report()
        graph = next(e for e in before['evidence'] if e['category'] == 'graph-history')
        self.assertEqual(graph['payload']['comparison']['events'], [])
        self.assertIn('insufficient-snapshots',
                      [i['code'] for i in graph['payload']['comparison']['issues']])
        self.collect()
        after = self.report()
        ids = [e['id'] for e in after['evidence']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(after['evidence'][:len(before['evidence'])], before['evidence'])
        self.assertEqual(len([s for s in after['stages'] if s['name'] == 'graph-history']), 1)

    def test_default_collect_never_calls_graph_history(self):
        self.import_tree(self.old)
        self.import_tree(self.new, present=True)
        result = self.collect(graph=False)
        self.assertNotIn('graph_history_outcome', result)
        self.assertFalse(any(e['category'] in ('graph-history', 'git-ancestry')
                             for e in self.report()['evidence']))

    def test_stale_collect_rejects_before_ancestry_and_preserves_revision(self):
        before = self.report()
        result = self.invoke('collect', '--run-dir', self.run_dir, '--expect-revision', 0,
                             '--graph-history', expected=3)
        self.assertFalse(result['ok'])
        self.assertEqual(self.report(), before)

    def test_branch_snapshots_do_not_invent_order(self):
        self.git_run('checkout', '-q', '--detach', self.old)
        sibling = self.commit('3')
        self.import_tree(sibling)
        self.import_tree(self.new, present=True)
        self.collect()
        graph = next(e for e in self.report()['evidence'] if e['category'] == 'graph-history')
        self.assertEqual(graph['payload']['comparison']['events'], [])
        self.assertIn('ambiguous-order', [i['code'] for i in graph['payload']['comparison']['issues']])

    def test_unattested_realm_stays_unknown_without_graph_events(self):
        self.import_tree(self.old, realm=None)
        self.import_tree(self.new, present=True, realm=None)
        self.collect()
        graph = next(e for e in self.report()['evidence'] if e['category'] == 'graph-history')
        self.assertEqual(graph['payload']['comparison']['events'], [])
        self.assertIn('unknown-subject-context',
                      [i['code'] for i in graph['payload']['comparison']['issues']])

    def test_selected_declaration_poms_are_used_without_inventing_target_paths(self):
        found = self.invoke('discover', '--repo', self.project, '--query', 'example:b',
                            '--state-dir', self.work / 'decl-state')
        self.run_dir, self.revision = found['run_dir'], found['revision']
        selected = self.invoke('select', '--run-dir', self.run_dir,
                               '--expect-revision', self.revision,
                               '--candidate', found['candidates'][0]['id'])
        self.revision = selected['revision']
        self.import_tree(self.old)
        self.import_tree(self.new, present=True)
        self.collect()
        report = self.report()
        declaration = next(e for e in report['evidence']
                           if e['category'] == 'declaration-history-candidates')
        self.assertEqual(declaration['payload']['observation']['coverage']['paths'], ['pom.xml'])
        graph = next(e for e in report['evidence'] if e['category'] == 'graph-history')
        self.assertEqual(graph['payload']['comparison']['events'][0]['kind'], 'graph-version-change')

    def test_maven_realm_flag_cannot_override_runtime_observation_scope(self):
        path = self.work / 'runtime.json'
        path.write_text('{}')
        before = self.report()
        result = self.invoke('import', '--run-dir', self.run_dir,
                             '--expect-revision', self.revision, '--report', path,
                             '--kind', 'runtime-observations', '--realm', 'project', expected=2)
        self.assertIn('--realm', result['error']['message'])
        self.assertEqual(self.report(), before)

    def test_diagnostic_snapshot_remains_gap(self):
        self.import_tree(self.old, mode='verbose')
        self.import_tree(self.new, present=True)
        self.collect()
        graph = next(e for e in self.report()['evidence'] if e['category'] == 'graph-history')
        self.assertEqual(graph['payload']['comparison']['events'], [])


class HistoryCliPreflightTests(unittest.TestCase):
    def test_stale_revision_never_invokes_history_collectors(self):
        from mdi import cli
        from mdi.runstore import RunConflict
        with mock.patch.object(cli, '_open_run', return_value=object()), \
                mock.patch.object(cli, '_checked_load', side_effect=RunConflict('stale')), \
                mock.patch.object(cli, '_history_stage') as ordinary, \
                mock.patch.object(cli, 'collect_graph_history') as graph, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(['collect', '--run-dir', '/unused', '--expect-revision', '0',
                             '--graph-history'])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())['error']['exit_code'], 3)
        ordinary.assert_not_called()
        graph.assert_not_called()

    def test_graph_collector_budgets_and_same_revision_commit(self):
        from mdi import cli
        from tests.test_history_pipeline import HistoryPipelineTests
        fixture = HistoryPipelineTests()
        fixture.setUp()
        try:
            report = fixture.report
            paths = ['pom.xml']
            with mock.patch.object(cli, '_selected_pom_paths', return_value=paths), \
                    mock.patch.object(cli, 'collect_graph_history',
                                      wraps=cli.collect_graph_history) as collect:
                draft, outcome = cli._graph_history_stage(report, str(fixture.repo), 'example:c')
            self.assertEqual(draft['revision'], report['revision'])
            self.assertEqual(outcome, 'partial')
            self.assertEqual(collect.call_args.kwargs['timeout_seconds'], 10)
            self.assertEqual(collect.call_args.kwargs['max_output_bytes'], 1024 * 1024)
            self.assertEqual(collect.call_args.kwargs['max_commits'], 64)
            self.assertEqual(collect.call_args.kwargs['pom_paths'], paths)
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()

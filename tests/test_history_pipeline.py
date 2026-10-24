"""History orchestration over real local Git and imported snapshot evidence."""
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

from mdi import history_pipeline
from mdi.graph_history import HistoryLimits
from mdi.report import ReportError, new_report, validate_report, validate_transition
from tests.test_graph_history import node, snapshot


class HistoryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        self.git = shutil.which('git')
        self.assertIsNotNone(self.git, 'Git required; no download or skip')
        self.env = dict(PATH=os.defpath, HOME=self.tmp.name, LC_ALL='C',
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='a@example.invalid',
                        GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='c@example.invalid',
                        GIT_AUTHOR_DATE='2026-01-01T00:00:00+00:00',
                        GIT_COMMITTER_DATE='2026-01-01T00:00:00+00:00')
        self.run_git('init', '-q')
        self.a = self.commit('1')
        self.b = self.commit('2')
        self.report = new_report('history-run', query='example:c')
        self.report['subjects'] = [dict(id='selected', realm='project', module='.',
                                       coordinates='example:c:1')]
        self.report['selection'] = dict(mode='single', candidate_ids=['selected'])
        self.report['evidence'] = [snapshot('old', self.a), snapshot('new', self.b, [node('c')], b_version='2')]
        for record in self.report['evidence']:
            record['category'] = 'imported-tree-json'
        self.report['qualifications'] = [dict(id='attestation', affected_evidence_ids=['new'],
            kind='caller-attested-provenance', reason='producer context is caller-attested',
            observed_at='2026-01-01T00:00:00Z')]
        validate_report(self.report)

    def run_git(self, *args, input=None):
        return subprocess.run([self.git, '-C', str(self.repo), *args], input=input,
            env=self.env, check=True, capture_output=True, timeout=15).stdout.decode().strip()

    def commit(self, version):
        self.repo.joinpath('pom.xml').write_text(
            '<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId>'
            '<artifactId>app</artifactId><version>1</version><dependencies><dependency>'
            '<groupId>example</groupId><artifactId>b</artifactId><version>' + version +
            '</version></dependency></dependencies></project>')
        self.run_git('add', 'pom.xml')
        self.run_git('commit', '-q', '-m', 'b version ' + version)
        return self.run_git('rev-parse', 'HEAD')

    def collect(self, **kwargs):
        return history_pipeline.collect_graph_history(self.report, self.repo, 'example:c',
            ancestry_evidence_id='ancestry', graph_evidence_id='graphs', **kwargs)

    def test_real_ancestry_transitive_graph_and_candidate_declaration_bridge(self):
        original = copy.deepcopy(self.report)
        result = self.collect(pom_paths=['pom.xml'], declaration_evidence_id='declarations')
        self.assertEqual(self.report, original)
        graph = result['graph']
        event, = graph['events']
        self.assertEqual(event['kind'], 'graph-addition')
        self.assertEqual(event['evidence_ids'], ['old', 'new'])
        self.assertEqual(event['ordering']['evidence_ids'], ['ancestry'])
        self.assertFalse(graph['historical_build_verified'])
        self.assertEqual(result['declarations']['coverage']['introducing_dependencies'][0]['artifact_id'], 'b')
        leads = [e for e in result['declarations']['events'] if e['kind'] == 'candidate-introduction']
        self.assertTrue(leads)
        self.assertNotIn('example:c', self.repo.joinpath('pom.xml').read_text())
        draft = result['draft']
        self.assertEqual(draft['selection'], original['selection'])
        self.assertEqual(draft['qualifications'][:len(original['qualifications'])], original['qualifications'])
        self.assertEqual(draft['evidence'][:2], original['evidence'])
        validate_report(draft)
        successor = copy.deepcopy(draft)
        successor['revision'] += 1
        validate_transition(original, successor)
        payload = next(e['payload'] for e in draft['evidence'] if e['id'] == 'graphs')
        self.assertEqual(payload['source_qualifications'], original['qualifications'])
        self.assertIn('candidate', ' '.join(payload['limitations']))

    def test_empty_and_single_snapshots_are_partial_not_absence(self):
        for size in (0, 1):
            with self.subTest(size=size):
                self.report['evidence'] = self.report['evidence'][:size]
                self.report['qualifications'] = []
                result = self.collect()
                self.assertEqual(result['stage']['outcome'], 'partial')
                self.assertEqual(result['graph']['events'], [])
                self.assertIn('insufficient-snapshots', {i['code'] for i in result['graph']['issues']})

    def test_diagnostic_snapshot_remains_gap(self):
        self.report['evidence'][0] = snapshot('old', self.a, verbose=True)
        self.report['evidence'][0]['category'] = 'imported-tree-json'
        result = self.collect()
        self.assertEqual(result['graph']['events'], [])
        self.assertEqual(result['stage']['outcome'], 'partial')
        self.assertIn('diagnostic-snapshot', {i['code'] for i in result['graph']['issues']})

    def test_missing_mode_remains_diagnostic(self):
        del self.report['evidence'][0]['payload']['import']['mode']
        self.assertEqual(self.collect()['graph']['events'], [])

    def test_branches_stay_unordered(self):
        self.run_git('checkout', '-q', '--detach', self.a)
        sibling = self.commit('3')
        self.report['evidence'][0]['payload']['import']['provenance']['revision'] = sibling
        result = self.collect()
        self.assertEqual(result['graph']['events'], [])
        self.assertIn('ambiguous-order', {i['code'] for i in result['graph']['issues']})

    def test_shallow_history_yields_no_verified_events(self):
        self.repo.joinpath('.git', 'shallow').write_text(self.b + '\n')
        result = self.collect()
        self.assertEqual(result['graph']['events'], [])
        self.assertEqual(result['stage']['outcome'], 'partial')
        self.assertIn('shallow-history', {i['code'] for i in result['ancestry']['issues']})

    def test_collision_or_unselected_query_refused_before_git(self):
        with mock.patch.object(history_pipeline, 'collect_ancestry') as collect:
            for overrides in ({'graph_evidence_id': 'old'}, {'graph_evidence_id': 'ancestry'}):
                args = dict(ancestry_evidence_id='ancestry', graph_evidence_id='graphs')
                args.update(overrides)
                with self.assertRaises(history_pipeline.HistoryPipelineError):
                    history_pipeline.collect_graph_history(self.report, self.repo, 'example:c', **args)
            with self.assertRaises(history_pipeline.HistoryPipelineError):
                history_pipeline.collect_graph_history(self.report, self.repo, 'example:unselected',
                    ancestry_evidence_id='a', graph_evidence_id='g')
            collect.assert_not_called()

    def test_other_module_and_realm_snapshots_not_compared(self):
        foreign = snapshot('foreign', self.a, module='elsewhere')
        foreign['category'] = 'imported-tree-json'
        self.report['evidence'].append(foreign)
        result = self.collect()
        self.assertEqual(result['graph']['snapshot_ids'], ['old', 'new'])
        self.assertEqual(result['stage']['coverage']['excluded_snapshot_ids'], ['foreign'])

    def test_limits_forwarded_and_malformed_snapshot_rejected_before_git(self):
        limits = HistoryLimits(max_snapshots=1)
        with mock.patch.object(history_pipeline, 'collect_ancestry') as collect:
            with self.assertRaises(history_pipeline.HistoryPipelineError):
                self.collect(limits=limits)
            collect.assert_not_called()
        self.report['evidence'][0]['payload']['import']['evidence']['nodes'] = []
        with mock.patch.object(history_pipeline, 'collect_ancestry') as collect:
            with self.assertRaises(history_pipeline.HistoryPipelineError):
                self.collect()
            collect.assert_not_called()

    def test_selected_subject_without_version_supports_unversioned_query(self):
        self.report['subjects'][0]['coordinates'] = 'example:c'
        self.assertTrue(self.collect()['graph']['events'])
        with self.assertRaises(history_pipeline.HistoryPipelineError):
            history_pipeline.collect_graph_history(self.report, self.repo, 'example:c:1',
                ancestry_evidence_id='ancestry', graph_evidence_id='graphs')

    def test_bare_query_cannot_add_unselected_group(self):
        other = node('c')
        other['groupId'] = 'othergroup'
        self.report['evidence'][1] = snapshot('new', self.b, [other])
        self.report['evidence'][1]['category'] = 'imported-tree-json'
        with mock.patch.object(history_pipeline, 'inspect_history',
                               wraps=history_pipeline.inspect_history) as declarations:
            result = history_pipeline.collect_graph_history(
                self.report, self.repo, 'c', ancestry_evidence_id='ancestry',
                graph_evidence_id='graphs', declaration_evidence_id='declarations',
                pom_paths=['pom.xml'])
        self.assertEqual(result['graph']['events'], [])
        self.assertEqual(declarations.call_args.args[1], 'example:c')

    def test_versioned_selection_retains_version_upgrade_history(self):
        self.report['evidence'] = [snapshot('old', self.a, [node('c', '1')]),
                                   snapshot('new', self.b, [node('c', '2')])]
        for record in self.report['evidence']:
            record['category'] = 'imported-tree-json'
        with mock.patch.object(history_pipeline, 'inspect_history',
                               wraps=history_pipeline.inspect_history) as declarations:
            result = history_pipeline.collect_graph_history(
                self.report, self.repo, 'example:c:1', ancestry_evidence_id='ancestry',
                graph_evidence_id='graphs', declaration_evidence_id='declarations',
                pom_paths=['pom.xml'])
        event, = result['graph']['events']
        self.assertEqual(event['kind'], 'graph-version-change')
        self.assertEqual((event['before_version'], event['after_version']), ('1', '2'))
        self.assertEqual(declarations.call_args.args[1], 'example:c')

    def test_multiple_selected_groups_do_not_cross_module_scopes(self):
        self.report['subjects'].append(dict(id='other', realm='project', module='sub',
                                           coordinates='othergroup:c:1'))
        self.report['selection'] = dict(mode='all', candidate_ids=['selected', 'other'])
        other = node('c')
        other['groupId'] = 'othergroup'
        root_old = snapshot('root-old', self.a)
        root_new = snapshot('root-new', self.b, [node('c'), other])
        sub_old = snapshot('sub-old', self.a, module='sub')
        sub_new = snapshot('sub-new', self.b, [node('c'), other], module='sub')
        self.report['evidence'] = [root_old, root_new, sub_old, sub_new]
        self.report['qualifications'] = []
        for record in self.report['evidence']:
            record['category'] = 'imported-tree-json'
        result = history_pipeline.collect_graph_history(
            self.report, self.repo, 'c', ancestry_evidence_id='ancestry', graph_evidence_id='graphs')
        events = result['graph']['events']
        self.assertEqual({(e['module'], e['path'][-1]['groupId']) for e in events},
                         {('.', 'example'), ('sub', 'othergroup')})
        self.assertEqual(len(events), 2)
        with mock.patch.object(history_pipeline, 'inspect_history',
                               wraps=history_pipeline.inspect_history) as declarations:
            self.repo.joinpath('sub').mkdir()
            self.repo.joinpath('sub', 'pom.xml').write_text(self.repo.joinpath('pom.xml').read_text())
            history_pipeline.collect_graph_history(
                self.report, self.repo, 'c', ancestry_evidence_id='ancestry', graph_evidence_id='graphs',
                declaration_evidence_id='declarations', pom_paths=['pom.xml', 'sub/pom.xml'])
        self.assertEqual({(call.args[1], tuple(call.kwargs['pom_paths'])) for call in declarations.call_args_list},
                         {('example:c', ('pom.xml',)), ('othergroup:c', ('sub/pom.xml',))})

    def test_unknown_selected_context_is_partial_not_broadened(self):
        self.report['subjects'][0]['realm'] = 'unknown'
        result = self.collect(pom_paths=['pom.xml'], declaration_evidence_id='declarations')
        self.assertEqual(result['stage']['outcome'], 'partial')
        self.assertEqual(result['graph']['events'], [])
        self.assertEqual(result['declarations']['events'], [])

    def test_archive_only_scope_uses_explicit_import_attestation_as_candidate(self):
        self.report['subjects'][0]['realm'] = 'unknown'
        attesting = self.report['evidence'][1]
        attesting['generation_context'].update(realm_attestation='caller-attested', realm='project')
        original = copy.deepcopy(self.report)
        result = self.collect()
        event, = result['graph']['events']
        self.assertEqual(event['kind'], 'graph-addition')
        self.assertEqual(event['selected_scope']['basis'], 'caller-attested-import-candidate')
        self.assertEqual(event['selected_scope']['evidence_ids'], ['new'])
        self.assertEqual(result['stage']['outcome'], 'partial')
        self.assertEqual(result['draft']['subjects'][0]['realm'], 'unknown')
        self.assertEqual(self.report, original)
        inherited, = [q for q in result['draft']['qualifications'] if q['kind'] == 'candidate-history-scope']
        self.assertEqual(inherited['affected_evidence_ids'], ['graphs'])
        self.assertIn('not packaged realm', inherited['reason'])
        self.assertIn('candidate-history-scope: ' + inherited['reason'], event['limitations'])
        bad = copy.deepcopy(result['draft'])
        bad['analyses'].append(dict(id='claim', statement_kind='inference', evidence_ids=['graphs'],
            limitations=['caller-attested-provenance: producer context is caller-attested']))
        with self.assertRaises(ReportError):
            validate_report(bad)

    def test_archive_only_candidate_scope_requires_matching_explicit_fields(self):
        self.report['subjects'][0]['realm'] = 'unknown'
        for change in ('unattested', 'wrong-realm', 'wrong-module', 'missing-module', 'different-ga'):
            with self.subTest(change=change):
                current = copy.deepcopy(self.report)
                record = current['evidence'][1]
                context = record['generation_context']
                context.update(realm_attestation='caller-attested', realm='project')
                if change == 'unattested':
                    del context['realm_attestation']
                elif change == 'wrong-realm':
                    context['realm'] = 'build-plugin'
                elif change == 'wrong-module':
                    context['supplied_provenance']['module'] = 'other'
                elif change == 'missing-module':
                    del context['supplied_provenance']['module']
                else:
                    record['payload']['import']['evidence']['nodes'][-1]['coordinates']['groupId'] = 'other'
                result = history_pipeline.collect_graph_history(current, self.repo, 'example:c',
                    ancestry_evidence_id='ancestry', graph_evidence_id='graphs')
                self.assertEqual(result['graph']['events'], [])
                self.assertIn('selected-subject-scope-unknown', {i['code'] for i in result['graph']['issues']})

    def test_archive_only_multiple_attested_realms_stay_separate_candidates(self):
        self.report['subjects'][0]['realm'] = 'unknown'
        for record in self.report['evidence']:
            record['generation_context'].update(realm_attestation='caller-attested', realm='project')
        plugin_old = snapshot('plugin-old', self.a, realm='build-plugin')
        plugin_new = snapshot('plugin-new', self.b, [node('c')], realm='build-plugin')
        for record in (plugin_old, plugin_new):
            record['category'] = 'imported-tree-json'
            record['generation_context'].update(realm_attestation='caller-attested', realm='build-plugin')
        self.report['evidence'].extend([plugin_old, plugin_new])
        result = self.collect()
        self.assertEqual({e['realm'] for e in result['graph']['events']}, {'project', 'build-plugin'})
        self.assertEqual(len(result['graph']['events']), 2)
        self.assertTrue(all(e['selected_scope']['basis'] == 'caller-attested-import-candidate'
                            for e in result['graph']['events']))
        self.assertEqual(result['draft']['subjects'][0]['realm'], 'unknown')

    def test_known_and_unknown_selected_copy_do_not_duplicate_history_events(self):
        self.report['subjects'].append(dict(id='packaged', realm='unknown', module='.', coordinates='example:c:1'))
        self.report['selection'] = dict(mode='all', candidate_ids=['selected', 'packaged'])
        self.report['evidence'][1]['generation_context'].update(realm_attestation='caller-attested', realm='project')
        result = self.collect()
        self.assertEqual(len(result['graph']['events']), 1)
        self.assertEqual(set(result['graph']['events'][0]['selected_scope']['subject_ids']), {'selected', 'packaged'})

    def test_archive_only_unknown_context_snapshot_stays_a_barrier(self):
        self.report['subjects'][0]['realm'] = 'unknown'
        third = self.commit('3')
        before = snapshot('before', self.a)
        unknown = snapshot('unknown', self.b, module=None)
        after = snapshot('after', third, [node('c')])
        self.report['evidence'] = [before, unknown, after]
        self.report['qualifications'] = []
        for record in self.report['evidence']:
            record['category'] = 'imported-tree-json'
        after['generation_context'].update(realm_attestation='caller-attested', realm='project')
        result = self.collect()
        self.assertEqual(result['graph']['events'], [])
        self.assertIn('unknown', result['graph']['snapshot_ids'])
        self.assertEqual(result['stage']['outcome'], 'partial')

    def test_archive_only_unattested_unknown_snapshots_keep_context_issue(self):
        self.report['subjects'][0]['realm'] = 'unknown'
        for record in self.report['evidence']:
            record['payload']['import']['subject_realm'] = 'unknown'
        result = self.collect()
        self.assertEqual(result['graph']['events'], [])
        self.assertIn('unknown-subject-context', {i['code'] for i in result['graph']['issues']})

    def test_unknown_selected_group_does_not_infer_group_from_snapshot(self):
        self.report['subjects'][0]['coordinates'] = 'c'
        result = history_pipeline.collect_graph_history(self.report, self.repo, 'c',
            ancestry_evidence_id='ancestry', graph_evidence_id='graphs')
        self.assertEqual(result['graph']['events'], [])
        self.assertIn('selected-subject-scope-unknown', {i['code'] for i in result['graph']['issues']})

    def test_multiple_scopes_share_event_limit(self):
        self.report['subjects'].append(dict(id='other', realm='project', module='.',
                                           coordinates='othergroup:c:1'))
        self.report['selection'] = dict(mode='all', candidate_ids=['selected', 'other'])
        other = node('c')
        other['groupId'] = 'othergroup'
        self.report['evidence'][1] = snapshot('new', self.b, [node('c'), other])
        self.report['evidence'][1]['category'] = 'imported-tree-json'
        with self.assertRaises((history_pipeline.HistoryPipelineError, history_pipeline.GraphHistoryError)):
            history_pipeline.collect_graph_history(self.report, self.repo, 'c',
                ancestry_evidence_id='ancestry', graph_evidence_id='graphs',
                limits=HistoryLimits(max_events=1))

    def test_unknown_snapshot_context_is_forwarded_as_barrier(self):
        middle = snapshot('middle', self.a, [node('c')], module=None)
        middle['category'] = 'imported-tree-json'
        self.report['evidence'].insert(1, middle)
        with mock.patch.object(history_pipeline, 'compare_graph_snapshots',
                               wraps=history_pipeline.compare_graph_snapshots) as compare:
            self.collect()
        for call in compare.call_args_list:
            self.assertIn('middle', [e['id'] for e in call.args[0]])

    def test_collector_limits_forwarded(self):
        with mock.patch.object(history_pipeline, 'collect_ancestry',
                               wraps=history_pipeline.collect_ancestry) as collect:
            self.collect(timeout_seconds=3, max_output_bytes=8192,
                         limits=HistoryLimits(max_snapshots=3))
        self.assertEqual(collect.call_args.kwargs['max_snapshots'], 3)
        self.assertEqual(collect.call_args.kwargs['timeout_seconds'], 3)
        self.assertEqual(collect.call_args.kwargs['max_output_bytes'], 8192)

    def test_unknown_context_and_producer_assumption_stay_qualified(self):
        old = self.report['evidence'][0]['payload']
        old['assumed_grammar'] = {'producer': 'maven-dependency-plugin', 'version': '3.11.0'}
        old['provenance_attestation'] = 'unknown'
        old['import']['provenance']['profiles'] = None
        result = self.collect()
        event, = result['graph']['events']
        self.assertEqual(event['provenance_attestations'][0]['attestation'], 'unknown')
        self.assertIn('profiles', event['context_unknown_fields'])
        self.assertEqual(result['stage']['outcome'], 'partial')
        self.report['evidence'][0]['payload']['import']['provenance']['module'] = None
        result = self.collect()
        self.assertEqual(result['graph']['events'], [])
        self.assertIn('old', result['stage']['coverage']['uncertain_context_snapshot_ids'])

    def test_repo_mismatch_and_bad_options_refused_before_git(self):
        self.report['evidence'].append(dict(id='context', category='discovery-context',
            source_locator=str(self.repo), observed_at='2026-01-01T00:00:00Z',
            payload={'repo': str(self.repo / 'other')}))
        with mock.patch.object(history_pipeline, 'collect_ancestry') as collect:
            with self.assertRaises(history_pipeline.HistoryPipelineError):
                self.collect()
            collect.assert_not_called()
        self.report['evidence'].pop()
        for kwargs in ({'timeout_seconds': float('nan')}, {'max_output_bytes': 0},
                       {'pom_paths': 'pom.xml'}, {'declaration_evidence_id': 'd'}):
            with self.subTest(kwargs=kwargs), mock.patch.object(history_pipeline, 'collect_ancestry') as collect:
                with self.assertRaises(history_pipeline.HistoryPipelineError):
                    self.collect(**kwargs)
                collect.assert_not_called()

    def test_declaration_failure_is_bounded_partial_and_does_not_hide_graph(self):
        with mock.patch.object(history_pipeline, 'inspect_history', side_effect=OSError('SECRET')):
            result = self.collect(pom_paths=['pom.xml'], declaration_evidence_id='declarations')
        self.assertEqual(result['stage']['outcome'], 'partial')
        self.assertTrue(result['graph']['events'])
        self.assertNotIn('SECRET', json.dumps(result))
        validate_report(result['draft'])

    def test_source_qualification_applies_to_derived_only_analyses_and_events(self):
        source = self.report['qualifications'][0]
        source.update(kind='changed-input', reason='snapshot bytes changed during observation')
        limitation = 'changed-input: snapshot bytes changed during observation'
        result = self.collect(pom_paths=['pom.xml'], declaration_evidence_id='declarations')
        self.assertIn(limitation, result['graph']['events'][0]['limitations'])
        leads = [e for e in result['declarations']['events'] if e['kind'] == 'candidate-introduction']
        self.assertTrue(leads)
        for lead in leads:
            self.assertIn(limitation, lead['limitations'])
        draft = result['draft']
        self.assertEqual(draft['qualifications'][0], source)
        derived = draft['qualifications'][1:]
        self.assertTrue(derived)
        affected = set().union(*(set(q['affected_evidence_ids']) for q in derived))
        self.assertEqual(affected, {'graphs', 'declarations'})
        for evidence_id in ('graphs', 'declarations'):
            with self.subTest(evidence=evidence_id):
                bad = copy.deepcopy(draft)
                bad['analyses'].append(dict(id='derived-analysis', evidence_ids=[evidence_id],
                    statement_kind='inference', limitations=['not a verified build']))
                with self.assertRaises(ReportError):
                    validate_report(bad)
                bad['analyses'][-1]['limitations'] = [limitation]
                validate_report(bad)

    def test_derived_only_analysis_cannot_omit_source_limitation(self):
        draft = self.collect()['draft']
        draft['analyses'].append(dict(id='derived-analysis', evidence_ids=['graphs'],
            statement_kind='inference', limitations=['not a verified build']))
        with self.assertRaises(ReportError):
            validate_report(draft)

    def test_recollect_qualification_ids_are_fresh_and_old_analyses_stay_valid(self):
        first = self.collect(pom_paths=['pom.xml'], declaration_evidence_id='declarations')['draft']
        inherited = 'caller-attested-provenance: producer context is caller-attested'
        first['analyses'].append(dict(id='old-analysis', evidence_ids=['graphs'],
            statement_kind='inference', limitations=[inherited]))
        original = copy.deepcopy(first)
        again = history_pipeline.collect_graph_history(first, self.repo, 'example:c',
            ancestry_evidence_id='ancestry2', graph_evidence_id='graphs2',
            declaration_evidence_id='declarations2', pom_paths=['pom.xml'])['draft']
        self.assertEqual(first, original)
        self.assertEqual(again['qualifications'][:len(first['qualifications'])], first['qualifications'])
        identifiers = [q['id'] for q in again['qualifications']]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertTrue(any('graphs2' in q['affected_evidence_ids'] for q in again['qualifications']))
        self.assertEqual(again['analyses'], first['analyses'])
        successor = copy.deepcopy(again)
        successor['revision'] += 1
        validate_transition(first, successor)

    def test_recollect_preserves_prior_evidence_and_replaces_only_stage(self):
        result = self.collect()
        original = copy.deepcopy(result['draft'])
        again = history_pipeline.collect_graph_history(original, self.repo, 'example:c',
            ancestry_evidence_id='ancestry2', graph_evidence_id='graphs2')
        self.assertEqual(again['draft']['evidence'][:len(original['evidence'])], original['evidence'])
        self.assertEqual(len([s for s in again['draft']['stages'] if s['name'] == 'graph-history']), 1)
        successor = copy.deepcopy(again['draft'])
        successor['revision'] += 1
        validate_transition(original, successor)


if __name__ == '__main__':
    unittest.main()

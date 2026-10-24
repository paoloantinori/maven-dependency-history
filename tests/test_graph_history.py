"""Pure historical graph comparison over actual Maven importer records."""
import copy
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'maven-dependency-investigator' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.maven import PROVENANCE_FIELDS, import_dependency_tree
from mdi.graph_history import GraphHistoryError, HistoryLimits, compare_graph_snapshots


def node(artifact, version='1', children=()):
    return dict(groupId='example', artifactId=artifact, version=version, type='jar',
                classifier='', scope='compile', optional=False, children=list(children))


def snapshot(eid, revision, children=(), *, timestamp=None, module='.', realm='project',
             verbose=False, profiles=(), b_version='1'):
    provenance = dict.fromkeys(PROVENANCE_FIELDS)
    provenance.update(producer='maven-dependency-plugin', version='3.11.0',
                      format='verbose' if verbose else 'nonverbose', revision=revision,
                      generated_at=timestamp, module=module,
                      profiles=None if profiles is None else list(profiles))
    tree = node('app', children=[node('b', b_version, children)])
    imported = import_dependency_tree(json.dumps(tree).encode(), provenance=provenance,
                                      verbose=verbose, realm=realm)
    return dict(id=eid, category='maven-tree', source_locator=eid+'.json',
                observed_at='2026-09-12T00:00:00Z', generation_context={
                    'attestation': 'caller-attested', 'supplied_provenance': provenance,
                    'assumed_grammar': None}, payload={'import': imported,
                    'provenance_attestation': 'caller-attested', 'assumed_grammar': None})


def edge(before, after):
    return {'before': before, 'after': after, 'evidence_ids': ['ancestry-proof'],
            'verified': True}


def compare(items, edges=(), **kwargs):
    return compare_graph_snapshots(items, 'example:c', ancestry=edges,
                                   proof_evidence_ids=['ancestry-proof'], **kwargs)


class HistoricalGraphTests(unittest.TestCase):
    def test_transitive_addition_cites_two_snapshots_not_shipped_introduction(self):
        old = snapshot('old', 'z-revision', b_version='1')
        new = snapshot('new', 'a-revision', [node('c')], b_version='2')
        result = compare([new, old], [edge('old', 'new')])
        event, = result['events']
        self.assertEqual(event['kind'], 'graph-addition')
        self.assertEqual(event['evidence_ids'], ['old', 'new'])
        self.assertEqual(event['ordering']['evidence_ids'], ['ancestry-proof'])
        self.assertEqual(event['path'][-1]['artifactId'], 'c')
        self.assertEqual(event['path'][-2]['artifactId'], 'b')
        self.assertFalse(result['historical_build_verified'])
        self.assertIn('unobserved', ' '.join(result['limitations']))

    def test_removal_reintroduction_and_version_change(self):
        items = [snapshot('a', 'r1', [node('c')]), snapshot('b', 'r2'),
                 snapshot('c', 'r3', [node('c')]), snapshot('d', 'r4', [node('c', '2')])]
        result = compare(items, [edge('a','b'), edge('b','c'), edge('c','d')])
        self.assertEqual([e['kind'] for e in result['events']],
                         ['graph-removal', 'graph-reintroduction', 'graph-version-change'])
        self.assertEqual(result['events'][-1]['before_version'], '1')
        self.assertEqual(result['events'][-1]['after_version'], '2')

    def test_duplicate_path_multiplicities_not_overwritten(self):
        a = snapshot('a','r1',[node('c'),node('c')])
        b = snapshot('b','r2',[node('c')])
        result = compare([a,b], [edge('a','b')])
        event, = result['events']
        self.assertEqual(event['kind'],'graph-removal')
        self.assertEqual(event['count'],1)
        self.assertEqual(event['before_count'],2)
        self.assertEqual(len(event['before_node_ids']),2)

    def test_ambiguous_versions_are_not_arbitrarily_paired(self):
        a = snapshot('a','r1',[node('c','1'),node('c','2')])
        b = snapshot('b','r2',[node('c','3'),node('c','4')])
        result = compare([a,b], [edge('a','b')])
        self.assertNotIn('graph-version-change', [e['kind'] for e in result['events']])
        self.assertTrue(any(i['code']=='ambiguous-version-pairing' for i in result['issues']))

    def test_no_hash_or_input_order_inference(self):
        self.assertEqual(compare([snapshot('z','zzz'),snapshot('a','aaa',[node('c')])])['events'],[])

    def test_branching_ancestry_stays_unordered(self):
        result = compare([snapshot('a','r1'),snapshot('b','r2',[node('c')]),
                          snapshot('c','r3',[node('c','2')])], [edge('a','b'),edge('a','c')])
        self.assertEqual(result['events'],[])
        self.assertTrue(any(i['code']=='ambiguous-order' for i in result['issues']))

    def test_missing_revision_blocks_ancestry(self):
        result = compare([snapshot('a',None),snapshot('b','r2',[node('c')])], [edge('a','b')])
        self.assertEqual(result['events'],[])
        self.assertTrue(any(i['code']=='missing-revision' for i in result['issues']))

    def test_timestamp_order_is_explicit_temporal_only(self):
        a=snapshot('a',None,timestamp='2026-09-12T01:00:00+01:00')
        b=snapshot('b',None,[node('c')],timestamp='2026-09-12T00:01:00Z')
        result=compare([b,a],ordering='timestamps')
        self.assertEqual(result['events'][0]['ordering']['basis'],'recorded-timestamp-temporal-only')
        self.assertEqual(result['events'][0]['evidence_ids'],['a','b'])

    def test_tied_naive_missing_timestamp_has_no_order(self):
        for stamp in (None,'2026-09-12T00:00:00','2026-09-12T00:00:00Z'):
            with self.subTest(stamp=stamp):
                result=compare([snapshot('a','r1',timestamp='2026-09-12T00:00:00Z'),
                                snapshot('b','r2',[node('c')],timestamp=stamp)],ordering='timestamps')
                self.assertEqual(result['events'],[])

    def test_realms_and_modules_never_merged(self):
        for field,value in (('module','sub'),('realm','build-plugin')):
            result=compare([snapshot('a','r1'),snapshot('b','r2',[node('c')],**{field:value})],
                           [edge('a','b')])
            self.assertEqual(result['events'],[])

    def test_unknown_realm_and_module_not_conflated(self):
        for kwargs in ({'realm':'unknown'},{'module':None}):
            result=compare([snapshot('a','r1',**kwargs),snapshot('b','r2',[node('c')],**kwargs)],
                           [edge('a','b')])
            self.assertEqual(result['events'],[])

    def test_verbose_snapshot_does_not_prove_removal(self):
        result=compare([snapshot('a','r1',[node('c')]), snapshot('b','r2',verbose=True)],
                       [edge('a','b')])
        self.assertEqual(result['events'],[])
        self.assertTrue(any(i['code']=='diagnostic-snapshot' for i in result['issues']))

    def test_unknown_profiles_and_context_change_remain_qualified(self):
        result=compare([snapshot('a','r1',profiles=None),snapshot('b','r2',[node('c')],profiles=['prod'])],
                       [edge('a','b')])
        self.assertEqual(result['status'],'partial')
        self.assertIn('profiles',result['events'][0]['context_unknown_fields'])

    def test_caller_inputs_unchanged_and_output_detached(self):
        items=[snapshot('a','r1'),snapshot('b','r2',[node('c')])]
        original=copy.deepcopy(items)
        result=compare(items,[edge('a','b')])
        self.assertEqual(items,original)
        result['events'][0]['path'][0]['artifactId']='edited'
        self.assertEqual(items,original)

    def test_unknown_interval_breaks_reintroduction_memory(self):
        items=[snapshot('a','r1',[node('c')]),snapshot('b','r2',verbose=True),
               snapshot('c','r3'),snapshot('d','r4',[node('c')])]
        result=compare(items,[edge('a','b'),edge('b','c'),edge('c','d')])
        self.assertEqual([e['kind'] for e in result['events']],['graph-addition'])

    def test_unknown_producer_assumption_retained_in_events(self):
        items=[snapshot('a','r1'),snapshot('b','r2',[node('c')])]
        for s in items:
            s['payload']['provenance_attestation']='unknown'
            s['payload']['assumed_grammar']={'producer':'maven-dependency-plugin'}
        result=compare(items,[edge('a','b')])
        self.assertEqual(result['events'][0]['provenance_attestations'][0]['attestation'],'unknown')
        self.assertEqual(result['events'][0]['provenance_attestations'][0]['assumed_grammar'],
                         {'producer':'maven-dependency-plugin'})

    def test_ancestry_chain_with_redundant_edges_not_hash_sorted(self):
        items=[snapshot('c','aaa',[node('c','2')]),snapshot('b','mmm',[node('c')]),snapshot('a','zzz')]
        result=compare(items,[edge('a','c'),edge('a','b'),edge('b','c')])
        self.assertEqual([e['evidence_ids'] for e in result['events']],[['a','b'],['b','c']])

    def test_path_scope_classifier_changes_do_not_become_version_change(self):
        old=node('c');new=node('c','2');new['classifier']='tests'
        result=compare([snapshot('a','r1',[old]),snapshot('b','r2',[new])],[edge('a','b')])
        self.assertEqual(sorted(e['kind'] for e in result['events']),['graph-addition','graph-removal'])

    def test_two_introducing_paths_remain_distinct(self):
        a=snapshot('a','r1',[node('c')])
        b=snapshot('b','r2',[node('c'),node('d',children=[node('c')])])
        result=compare([a,b],[edge('a','b')])
        event,=result['events']
        self.assertEqual([n['artifactId'] for n in event['path']],['b','d','c'])
        self.assertEqual(event['count'],1)

    def test_optional_boolean_and_string_spellings_are_semantically_equal(self):
        for boolean, spelling in ((False, 'false'), (True, 'true')):
            with self.subTest(optional=spelling):
                first=node('c');first['optional']=boolean
                second=node('c');second['optional']=spelling
                items=[snapshot('a','r1',[first]),snapshot('b','r2',[second])]
                original=copy.deepcopy(items)
                self.assertEqual(compare(items,[edge('a','b')])['events'],[])
                self.assertEqual(items,original)

    def test_optional_raw_provenance_retained_on_real_version_change(self):
        first=node('c');first['optional']=False
        second=node('c','2');second['optional']='false'
        event,=compare([snapshot('a','r1',[first]),snapshot('b','r2',[second])],
                      [edge('a','b')])['events']
        self.assertEqual(event['kind'],'graph-version-change')
        self.assertIs(event['optional_observations']['before'][0]['raw'],False)
        self.assertEqual(event['optional_observations']['after'][0]['raw'],'false')
        self.assertIs(event['optional_observations']['after'][0]['parsed'],False)

    def test_unknown_optional_state_does_not_invent_graph_changes(self):
        c=node('c');c['optional']='unknown'
        items=[snapshot('a','r1',[c]),snapshot('b','r2',[node('c')])]
        result=compare(items,[edge('a','b')])
        self.assertEqual(result['events'],[])
        self.assertEqual(result['status'],'partial')
        self.assertTrue(any(i['code']=='unknown-optional-state' for i in result['issues']))

    def test_unknown_context_snapshot_blocks_false_reintroduction(self):
        for context in ({'module':None}, {'realm':'unknown'}, {'module':None,'realm':'unknown'}):
            with self.subTest(context=context):
                items=[snapshot('a','r1',[node('c')],timestamp='2026-09-12T00:00:00Z'),
                       snapshot('b','r2',timestamp='2026-09-12T00:01:00Z'),
                       snapshot('gap','r3',verbose=True,timestamp='2026-09-12T00:02:00Z',**context),
                       snapshot('d','r4',[node('c')],timestamp='2026-09-12T00:03:00Z')]
                original=copy.deepcopy(items)
                result=compare(items,ordering='timestamps')
                self.assertEqual([e['kind'] for e in result['events']],['graph-removal'])
                self.assertEqual(result['events'][0]['evidence_ids'],['a','b'])
                self.assertEqual(items,original)

    def test_unknown_context_breaks_ancestry_comparison_and_resets_memory(self):
        items=[snapshot('a','r1',[node('c')]),snapshot('b','r2'),
               snapshot('gap','r3',module=None),snapshot('d','r4'),
               snapshot('e','r5',[node('c')])]
        edges=[edge('a','b'),edge('b','gap'),edge('gap','d'),edge('d','e'),edge('b','d')]
        result=compare(items,edges)
        self.assertEqual([e['kind'] for e in result['events']],['graph-removal','graph-addition'])
        self.assertEqual(result['events'][-1]['evidence_ids'],['d','e'])

    def test_unknown_context_between_present_and_absent_cannot_prove_removal(self):
        items=[snapshot('a','r1',[node('c')],timestamp='2026-09-12T00:00:00Z'),
               snapshot('gap','r2',module=None,timestamp='2026-09-12T00:01:00Z'),
               snapshot('b','r3',timestamp='2026-09-12T00:02:00Z')]
        self.assertEqual(compare(items,ordering='timestamps')['events'],[])

    def test_known_foreign_context_does_not_block_relevant_group(self):
        for context in ({'module':'other','realm':'unknown'},
                        {'module':None,'realm':'build-plugin'}):
            with self.subTest(context=context):
                items=[snapshot('a','r1',timestamp='2026-09-12T00:00:00Z'),
                       snapshot('foreign','r2',timestamp='2026-09-12T00:01:00Z',**context),
                       snapshot('b','r3',[node('c')],timestamp='2026-09-12T00:02:00Z')]
                event,=compare(items,ordering='timestamps')['events']
                self.assertEqual(event['kind'],'graph-addition')
                self.assertEqual(event['evidence_ids'],['a','b'])

    def test_malformed_offset_or_excess_precision_cannot_order_snapshots(self):
        invalid=('2026-09-12T00:00:00+00:60','2026-09-12T00:00:00-00:60',
                 '2026-09-12T00:00:00+24:00','2026-09-12T00:00:00.1234567Z',
                 '2026-09-12T00:00:00.0000001+00:00','2026-09-12 00:00:00Z',
                 '20260912T000000Z','2026-09-12T00:00:00+00:00:01',
                 '2026-09-12T00:00:00+00:99')
        for value in invalid:
            with self.subTest(timestamp=value):
                items=[snapshot('a','r1',timestamp=value),
                       snapshot('b','r2',[node('c')],timestamp='2026-09-13T00:00:00Z')]
                result=compare(items,ordering='timestamps')
                self.assertEqual(result['events'],[])
                self.assertTrue(any(i['code']=='missing-or-invalid-timestamp' for i in result['issues']))

    def test_valid_microsecond_timestamp_orders_without_truncation(self):
        items=[snapshot('a','r1',timestamp='2026-09-12T00:00:00.123456+00:00'),
               snapshot('b','r2',[node('c')],timestamp='2026-09-12T00:00:00.123457Z')]
        event,=compare(items,ordering='timestamps')['events']
        self.assertEqual(event['evidence_ids'],['a','b'])

    def test_root_project_not_dependency(self):
        result=compare_graph_snapshots([snapshot('a','r1'),snapshot('b','r2')],'example:app',
                                      ancestry=[edge('a','b')],proof_evidence_ids=['ancestry-proof'])
        self.assertEqual(result['events'],[])

    def test_type_classifier_and_scope_preserved(self):
        c=node('c'); c.update(type='zip',classifier='tests',scope='test')
        event,=compare([snapshot('a','r1'),snapshot('b','r2',[c])],[edge('a','b')])['events']
        self.assertEqual(event['path'][-1]['type'],'zip')
        self.assertEqual(event['path'][-1]['classifier'],'tests')
        self.assertEqual(event['scope'],'test')


class BoundaryTests(unittest.TestCase):
    def test_extra_coordinate_fields_rejected_with_typed_error(self):
        for value in (None, True, {}, 'extra'):
            with self.subTest(value=value):
                item=snapshot('a','r1',[node('c')])
                item['payload']['import']['evidence']['nodes'][-1]['coordinates']['extra']=value
                with self.assertRaises(GraphHistoryError):compare([item])

    def test_empty_snapshots_are_insufficient_not_complete(self):
        result=compare([])
        self.assertEqual(result['events'],[])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['issues'],[{'code':'insufficient-snapshots','evidence_ids':[]}])

    def test_malformed_optional_or_conflicting_parsed_value_rejected(self):
        for value in (None, {}, {'raw':False,'parsed':1}, {'raw':'false','parsed':True},
                      {'raw':False,'parsed':False,'extra':None}):
            with self.subTest(value=value):
                item=snapshot('a','r1',[node('c')])
                item['payload']['import']['evidence']['nodes'][-1]['optional']=value
                with self.assertRaises(GraphHistoryError):compare([item])

    def test_duplicate_snapshot_ids_and_unknown_proof_references(self):
        s=snapshot('a','r')
        cases=[([s,s],[]),([s,snapshot('b','r2')],[dict(edge('a','b'),evidence_ids=['absent'])]),
               ([s],[edge('a','missing')])]
        for items,edges in cases:
            with self.assertRaises(GraphHistoryError): compare(items,edges)

    def test_unverified_ancestry_never_accepted(self):
        for verified in (False,1,'true'):
            with self.assertRaises(GraphHistoryError):
                compare([snapshot('a','r1'),snapshot('b','r2')],
                        [dict(edge('a','b'),verified=verified)])

    def test_duplicate_edges_refused_and_placeholder_graph_not_compared(self):
        items=[snapshot('a','r1'),snapshot('b','r2',[node('c','${version}')])]
        with self.assertRaises(GraphHistoryError):
            compare(items,[edge('a','b'),edge('a','b')])
        result=compare(items,[edge('a','b')])
        self.assertEqual(result['events'],[])
        self.assertEqual(result['status'],'partial')

    def test_cycle_refused(self):
        with self.assertRaises(GraphHistoryError):
            compare([snapshot('a','r1'),snapshot('b','r2')],[edge('a','b'),edge('b','a')])

    def test_dangling_node_parent_and_duplicate_node_refused(self):
        for mutation in ('parent','duplicate'):
            s=snapshot('a','r1',[node('c')]); nodes=s['payload']['import']['evidence']['nodes']
            if mutation=='parent':nodes[-1]['parent']='absent'
            else:nodes.append(copy.deepcopy(nodes[-1]))
            with self.assertRaises(GraphHistoryError):compare([s])

    def test_limits_and_non_json_inputs(self):
        for limit in (True,0,-1):
            with self.assertRaises(GraphHistoryError):HistoryLimits(max_snapshots=limit)
        with self.assertRaises(GraphHistoryError):compare([snapshot('a','r')],limits=HistoryLimits(max_input_bytes=100))
        with self.assertRaises(GraphHistoryError):compare([snapshot('a','r'),snapshot('b','r2')],limits=HistoryLimits(max_snapshots=1))
        s=snapshot('a','r');s['bad']=float('nan')
        with self.assertRaises(GraphHistoryError):compare([s])
        s=snapshot('a','r');s['cycle']=s
        with self.assertRaises(GraphHistoryError):compare([s])

    def test_output_bound_fails_explicitly(self):
        with self.assertRaises(GraphHistoryError):
            compare([snapshot('a','r1'),snapshot('b','r2',[node('c')])],[edge('a','b')],
                    limits=HistoryLimits(max_output_bytes=100))


if __name__=='__main__':unittest.main()

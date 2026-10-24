"""Pure preregistration checks; the closed operations dispatcher stays authoritative."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'maven-dependency-investigator' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import operations, raw_capture_consent as capture
from mdi.runstore import RunStore, RunConflict, CommitUncertain

NAME = 'raw-12345678-1234-1234-1234-123456789abc.bin'
OTHER_NAME = 'raw-22345678-1234-1234-1234-123456789abc.bin'
TOOL = {'name': 'trusted-capture', 'version': 'test-only'}


class CaptureScopeTests(unittest.TestCase):
    def test_exact_stream_and_generated_basename(self):
        for kind, stream in (('maven', 'maven-output'), ('advisory', 'advisory-response')):
            scope = capture.capture_scope('source', kind, max_bytes=128)
            capture.validate_scope(scope)
            self.assertEqual(scope['stream'], stream)
            self.assertNotIn('/', scope['output_name'])

    def test_cap_and_output_restrictions(self):
        for value in (0, -1, True, 1.0, None, capture.MAX_CAPTURE_BYTES + 1):
            with self.subTest(cap=value), self.assertRaises(capture.CaptureConsentError):
                capture.capture_scope('source', 'maven', max_bytes=value)
        for name in ('../outside', '/tmp/raw.bin', 'raw.bin', 'raw-x.bin', NAME + '/x', 'C:\\raw.bin'):
            with self.subTest(name=name), self.assertRaises(capture.CaptureConsentError):
                capture.capture_scope('source', 'maven', max_bytes=1, output_name=name)

    def test_exact_closed_scope(self):
        scope = capture.capture_scope('source', 'maven', max_bytes=1, output_name=NAME)
        for key, value in (('stream', 'advisory-response'), ('kind', 'maven'),
                           ('source_kind', []), ('source_operation_id', '\x00'), ('token', 'secret')):
            with self.subTest(key=key), self.assertRaises(capture.CaptureConsentError):
                capture.validate_scope(dict(scope, **{key: value}))
        with self.assertRaises(capture.CaptureConsentError):
            capture.validate_scope({})


class CaptureRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunStore.create(self.tmp.name, query='example:lib')
        self.source = operations.build_proposal(
            'maven', {'kind': 'maven', 'argv': ['/synthetic/mvn', '-q'], 'cwd': '/synthetic'},
            [{'path': '/synthetic/pom.xml', 'sha256': 'a' * 64}],
            {'name': 'synthetic-maven'}, 'Synthetic test operation; no execution.',
            self.store.run_id, 'source',
        )
        operations.propose(self.store, self.source, 0)
        self.report = self.store.load()
        self.scope = capture.capture_scope('source', 'maven', max_bytes=128, output_name=NAME)

    def capture_record(self):
        # Synthetic in-memory future schema fixture, NEVER persisted by bypassing
        # operations.propose. The report layer permits generic scope objects.
        return {
            'id': 'capture', 'run_id': self.store.run_id, 'state': 'proposed',
            'scope': copy.deepcopy(self.scope), 'input_identities': [],
            'tool_identity': dict(TOOL), 'disclosure': capture._DISCLOSURE,
            'audit': {}, 'evidence_ids': [],
        }

    def pair_report(self):
        report = copy.deepcopy(self.report)
        report['operations'].append(self.capture_record())
        return report

    def approval(self, record):
        # Fixture approvals model separate test-host decisions only.
        return {'run_id': record['run_id'], 'operation_id': record['id'],
                'scope': copy.deepcopy(record['scope']), 'approved': True,
                'approved_by': 'synthetic-test-host', 'reference': 'test fixture'}

    def validate(self, report, **changes):
        source, raw = report['operations'][:2]
        args = dict(source_approval=self.approval(source), capture_approval=self.approval(raw),
                    observed_source_inputs=copy.deepcopy(source['input_identities']),
                    observed_source_tool=copy.deepcopy(source['tool_identity']),
                    observed_capture_tool=copy.deepcopy(raw['tool_identity']))
        args.update(changes)
        return capture.validate_pair(report, raw['id'], **args)

    def test_registration_detached_and_no_store_or_files_changed(self):
        before = self.store.load()
        paths = sorted(p.name for p in Path(self.store.path).iterdir())
        result = capture.validate_registration(self.report, self.scope)
        result['max_bytes'] = 1
        self.assertEqual(self.scope['max_bytes'], 128)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(sorted(p.name for p in Path(self.store.path).iterdir()), paths)

    def test_canonical_builder_registers_through_operations(self):
        before = self.store.load()
        record = capture.build_capture_proposal(self.report, 'source', max_bytes=128,
                                               tool_identity=TOOL, output_name=NAME)
        saved = operations.propose(self.store, record, before['revision'])
        self.assertEqual(saved['operations'][-1], record)
        self.assertEqual(saved['revision'], before['revision'] + 1)

    def test_pair_validation_is_pure_and_not_authority(self):
        report = self.pair_report()
        before = copy.deepcopy(report)
        with mock.patch.object(self.store, 'commit', side_effect=AssertionError('unexpected commit')):
            checked = self.validate(report)
        self.assertFalse(checked['consumed'])
        self.assertEqual(checked['revision'], before['revision'])
        self.assertEqual(report, before)
        self.assertEqual(self.store.load(), self.report)
        checked['scope']['max_bytes'] = 1
        self.assertEqual(report['operations'][1]['scope']['max_bytes'], 128)

    def test_both_approvals_required_and_exact(self):
        for field in ('source_approval', 'capture_approval'):
            for value in (None, {}, {'approved': True}):
                report = self.pair_report()
                before = copy.deepcopy(report)
                with self.subTest(field=field, value=value), self.assertRaises(operations.ApprovalError):
                    self.validate(report, **{field: value})
                self.assertEqual(report, before)
        report = self.pair_report()
        for record_index in (0, 1):
            field = ('source_approval', 'capture_approval')[record_index]
            for key, value in (('approved', False), ('approved', 1), ('run_id', 'other'),
                               ('operation_id', 'sibling'), ('reference', '')):
                approval = self.approval(report['operations'][record_index])
                approval[key] = value
                with self.subTest(field=field, key=key), self.assertRaises(operations.ApprovalError):
                    self.validate(report, **{field: approval})
        approval = self.approval(report['operations'][1])
        approval['scope']['max_bytes'] = True
        with self.assertRaises(operations.ApprovalError):
            self.validate(report, capture_approval=approval)

    def test_observed_identity_changes_refused(self):
        report = self.pair_report()
        for field, value in (('observed_source_inputs', []), ('observed_source_tool', {}),
                             ('observed_capture_tool', {'name': 'different'})):
            with self.subTest(field=field), self.assertRaises(operations.ApprovalError):
                self.validate(report, **{field: value})
        self.assertEqual(self.store.load(), self.report)

    def test_posthoc_and_replay_refused_without_mutation(self):
        for index in (0, 1):
            for state in ('consumed', 'finished', 'denied'):
                report = self.pair_report()
                report['operations'][index]['state'] = state
                before = copy.deepcopy(report)
                with self.subTest(index=index, state=state), self.assertRaises(capture.CaptureConsentError):
                    self.validate(report)
                self.assertEqual(report, before)
        operations.deny_proposal(self.store, 'source', 'fixture denied', 1)
        denied = self.store.load()
        with self.assertRaises(capture.CaptureConsentError):
            capture.validate_registration(denied, self.scope)
        self.assertEqual(self.store.load(), denied)

    def test_source_kind_and_uniqueness_are_enforced(self):
        wrong = dict(self.scope, source_kind='advisory', stream='advisory-response')
        with self.assertRaises(capture.CaptureConsentError):
            capture.validate_registration(self.report, wrong)
        report = self.pair_report()
        with self.assertRaises(capture.CaptureConsentError):
            capture.validate_registration(report, dict(self.scope, output_name=OTHER_NAME))
        sibling = copy.deepcopy(report['operations'][1])
        sibling['id'] = 'other-capture'
        sibling['scope']['output_name'] = OTHER_NAME
        report['operations'].append(sibling)
        with self.assertRaises(capture.CaptureConsentError):
            self.validate(report)

    def test_capture_must_have_fresh_audit_and_sensitive_disclosure(self):
        for field, value in (('audit', {'started': {}}), ('input_identities', self.source['input_identities']),
                             ('disclosure', 'not sufficient')):
            report = self.pair_report()
            report['operations'][1][field] = value
            with self.subTest(field=field), self.assertRaises(capture.CaptureConsentError):
                self.validate(report)


class AtomicCaptureTests(CaptureRegistrationTests):
    def register(self, source_id='source', operation_id='capture', output_name=NAME):
        record = capture.build_capture_proposal(
            self.store.load(), source_id, max_bytes=128, tool_identity=TOOL,
            operation_id=operation_id, output_name=output_name)
        operations.propose(self.store, record, self.store.load()['revision'])
        return record

    def consume(self, raw, **overrides):
        source = operations._find_operation(self.store.load(), raw['scope']['source_operation_id'])
        args = dict(expected_revision=self.store.load()['revision'],
                    source_approval=self.approval(source), capture_approval=self.approval(raw),
                    observed_source_inputs=source['input_identities'],
                    observed_source_tool=source['tool_identity'], observed_capture_inputs=[],
                    observed_capture_tool=raw['tool_identity'])
        args.update(overrides)
        return capture.consume_pair(self.store, raw['id'], **args)

    def test_pair_is_one_commit_and_survives_reload(self):
        raw = self.register()
        before = self.store.load()
        with mock.patch.object(self.store, 'commit', wraps=self.store.commit) as commit:
            result = self.consume(raw)
        self.assertEqual(commit.call_count, 1)
        saved = RunStore.open(self.store.path).load()
        self.assertEqual(saved['revision'], before['revision'] + 1)
        self.assertTrue(result['consumed'])
        for record in saved['operations']:
            self.assertEqual(record['state'], 'consumed')
            self.assertEqual(record['audit']['started']['reference'], 'test fixture')
        self.assertEqual(saved['operations'][0]['audit']['started']['at'],
                         saved['operations'][1]['audit']['started']['at'])
        self.assertEqual(self.store.load(before['revision']), before)
        self.assertFalse(any(p.name.startswith('raw-') for p in Path(self.store.path).iterdir()))
        with self.assertRaises(capture.CaptureConsentError):
            self.consume(raw)

    def test_invalid_pair_changes_neither_record(self):
        raw = self.register()
        for key, bad in (('source_approval', None), ('capture_approval', None),
                         ('observed_source_inputs', []), ('observed_source_tool', {}),
                         ('observed_capture_inputs', [{'path': '/other', 'sha256': 'a' * 64}]),
                         ('observed_capture_tool', {})):
            before = self.store.load()
            with self.subTest(key=key), self.assertRaises(operations.OperationsError):
                self.consume(raw, **{key: bad})
            self.assertEqual(self.store.load(), before)
        wrong = self.approval(raw)
        wrong['scope']['max_bytes'] = 127
        with self.assertRaises(operations.ApprovalError):
            self.consume(raw, capture_approval=wrong)
        wrong = self.approval(raw)
        wrong['run_id'] = 'foreign'
        with self.assertRaises(operations.ApprovalError):
            self.consume(raw, capture_approval=wrong)

    def test_single_consume_cannot_bypass_registered_pair(self):
        raw = self.register()
        for record in (self.source, raw):
            before = self.store.load()
            with self.assertRaises(operations.ApprovalError):
                operations.consume_approval(self.store, record['id'], self.approval(record),
                    record['input_identities'], record['tool_identity'], before['revision'])
            self.assertEqual(self.store.load(), before)

    def test_denied_capture_allows_source_only_without_capture(self):
        raw = self.register()
        operations.deny_proposal(self.store, raw['id'], 'fixture denial', self.store.load()['revision'])
        before = self.store.load()
        with self.assertRaises(capture.CaptureConsentError):
            self.consume(raw)
        operations.consume_approval(self.store, 'source', self.approval(self.source),
            self.source['input_identities'], self.source['tool_identity'], before['revision'])
        self.assertEqual(self.store.load()['operations'][0]['state'], 'consumed')
        self.assertEqual(self.store.load()['operations'][1]['state'], 'denied')

    def test_denied_source_refuses_pair(self):
        raw = self.register()
        operations.deny_proposal(self.store, 'source', 'fixture denial', self.store.load()['revision'])
        before = self.store.load()
        with self.assertRaises(capture.CaptureConsentError):
            self.consume(raw)
        self.assertEqual(self.store.load(), before)

    def test_registration_revalidates_duplicates_and_source_state(self):
        first = capture.build_capture_proposal(self.report, 'source', max_bytes=128,
                    tool_identity=TOOL, operation_id='first', output_name=NAME)
        second = capture.build_capture_proposal(self.report, 'source', max_bytes=128,
                    tool_identity=TOOL, operation_id='second', output_name=OTHER_NAME)
        operations.propose(self.store, first, 1)
        with self.assertRaises(capture.CaptureConsentError):
            operations.propose(self.store, second, 2)
        other = dict(self.source, id='other-source')
        operations.propose(self.store, other, 2)
        record = capture.build_capture_proposal(self.store.load(), 'other-source', max_bytes=128,
                    tool_identity=TOOL, operation_id='other-capture', output_name=OTHER_NAME)
        operations.consume_approval(self.store, 'other-source', self.approval(other),
                    other['input_identities'], other['tool_identity'], 3)
        with self.assertRaises(capture.CaptureConsentError):
            operations.propose(self.store, record, 4)

    def test_sibling_approval_and_stale_revision(self):
        raw = self.register()
        other = dict(self.source, id='sibling')
        operations.propose(self.store, other, self.store.load()['revision'])
        before = self.store.load()
        with self.assertRaises(operations.ApprovalError):
            self.consume(raw, source_approval=self.approval(other))
        with self.assertRaises(RunConflict):
            self.consume(raw, expected_revision=before['revision'] - 1)
        self.assertEqual(self.store.load(), before)

    def test_commit_failure_returns_no_authority(self):
        raw = self.register()
        before = self.store.load()
        with mock.patch.object(self.store, 'commit', side_effect=OSError('before publication')):
            with self.assertRaises(OSError):
                self.consume(raw)
        self.assertEqual(self.store.load(), before)
        commit = self.store.commit
        def crash(draft, expected_revision):
            commit(draft, expected_revision)
            raise KeyboardInterrupt('after publication')
        with mock.patch.object(self.store, 'commit', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.consume(raw)
        self.assertEqual([r['state'] for r in self.store.load()['operations']], ['consumed', 'consumed'])
        with self.assertRaises(capture.CaptureConsentError):
            self.consume(raw)

    def test_uncertain_publication_returns_no_descriptor_and_no_replay(self):
        raw = self.register()
        commit = self.store.commit
        def uncertain(draft, revision):
            saved = commit(draft, revision)
            raise CommitUncertain('directory sync unconfirmed', saved['revision'])
        with mock.patch.object(self.store, 'commit', side_effect=uncertain):
            with self.assertRaises(CommitUncertain):
                self.consume(raw)
        saved = RunStore.open(self.store.path).load()
        self.assertEqual([r['state'] for r in saved['operations']], ['consumed', 'consumed'])
        with self.assertRaises(capture.CaptureConsentError):
            self.consume(raw)

    def test_pending_capture_blocks_real_executor_entrypoints(self):
        from mdi import advisory_executor, maven_executor
        pom = Path(self.tmp.name, 'pom.xml')
        pom.write_text('<project/>')
        sources = (
            maven_executor.maven_proposal(
                self.store, [sys.executable, '-c', 'pass'], self.tmp.name,
                network_destinations='unknown', input_paths=[pom], operation_id='maven-real'),
            advisory_executor.advisory_proposal(self.store, 'example', 'lib', '1', 'advisory-real'),
        )
        for index, source in enumerate(sources):
            operations.propose(self.store, source, self.store.load()['revision'])
            self.register(source['id'], f'capture-{index}',
                          (NAME if index == 0 else OTHER_NAME))
            before = self.store.load()
            with self.subTest(kind=source['scope']['kind']):
                if index == 0:
                    runner = mock.Mock()
                    with self.assertRaises(operations.ApprovalError):
                        maven_executor.execute_maven(self.store, source['id'], self.approval(source),
                            expected_revision=before['revision'], runner=runner)
                    runner.assert_not_called()
                else:
                    transport = mock.Mock()
                    with self.assertRaises(operations.ApprovalError):
                        advisory_executor.execute_advisory(self.store, source['id'], self.approval(source),
                            expected_revision=before['revision'], transport=transport)
                    transport.post.assert_not_called()
            self.assertEqual(self.store.load(), before)

    def test_registration_cannot_bypass_capture_policy(self):
        original = capture.build_capture_proposal(self.report, 'source', max_bytes=128,
                    tool_identity=TOOL, operation_id='capture', output_name=NAME)
        for key, value in (('disclosure', 'raw output is fine'),
                           ('input_identities', self.source['input_identities']),
                           ('run_id', 'foreign'), ('id', 'source')):
            record = copy.deepcopy(original)
            record[key] = value
            before = self.store.load()
            with self.subTest(key=key), self.assertRaises(operations.OperationsError):
                operations.propose(self.store, record, before['revision'])
            self.assertEqual(self.store.load(), before)
        extra = copy.deepcopy(original)
        extra['scope']['secret'] = 'value'
        with self.assertRaises(operations.OperationsError):
            operations.propose(self.store, extra, self.store.load()['revision'])

    def test_denied_registration_still_reserves_output_and_source(self):
        raw = self.register()
        operations.deny_proposal(self.store, raw['id'], 'no capture', self.store.load()['revision'])
        with self.assertRaises(capture.CaptureConsentError):
            self.register(operation_id='retry', output_name=OTHER_NAME)
        source = dict(self.source, id='new-source')
        operations.propose(self.store, source, self.store.load()['revision'])
        with self.assertRaises(capture.CaptureConsentError):
            self.register('new-source', 'new-capture', NAME)

    def test_advisory_pair_and_fresh_source_renewal(self):
        source = operations.build_proposal('advisory',
            {'kind': 'advisory', 'destination': 'https://api.osv.dev/v1/query',
             'query': {'ecosystem': 'Maven', 'name': 'example:lib', 'version': '1'}},
            [], {'name': 'fixture'}, 'Fixture query, not executed.', self.store.run_id, 'advisory')
        operations.propose(self.store, source, 1)
        raw = self.register('advisory')
        self.consume(raw)
        fresh = dict(source, id='fresh')
        operations.propose(self.store, fresh, self.store.load()['revision'])
        new_raw = self.register('fresh', 'new-capture', OTHER_NAME)
        self.consume(new_raw)
        self.assertEqual(self.store.load()['operations'][-1]['state'], 'consumed')


if __name__ == '__main__':
    unittest.main()

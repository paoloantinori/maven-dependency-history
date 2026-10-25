"""CLI raw capture uses synthetic executables and locally mocked transport only."""
import contextlib
import hashlib
import importlib
import io
import json
import os
import stat
import sys
from pathlib import Path
from unittest import mock

from tests.test_cli_operations import OperationsCliTestCase


class RawCaptureCliTests(OperationsCliTestCase):
    def register(self, run_dir, source, cap=12, revision=3):
        return self.parse(self.run_cli(
            'propose', '--run-dir', run_dir, '--expect-revision', revision,
            '--kind', 'raw-capture', '--source-operation', source['id'],
            '--max-capture-bytes', cap))['operation']

    def paired(self, run_dir, source, capture, revision=4, **overrides):
        source_file = self.write_approval(run_dir, source)
        capture_file = self.write_approval(run_dir, capture, **overrides)
        return self.run_cli(
            'execute', '--run-dir', run_dir, '--expect-revision', revision,
            '--operation', source['id'], '--approval-file', source_file,
            '--capture-operation', capture['id'], '--capture-approval-file', capture_file)

    def report(self, run_dir):
        return self.parse(self.run_cli('report', '--run-dir', run_dir))

    def assert_untouched(self, run_dir, revision=4):
        report = self.report(run_dir)
        self.assertEqual(report['revision'], revision)
        self.assertTrue(all(op['state'] == 'proposed' for op in report['operations']))
        self.assertEqual(list(Path(run_dir).glob('raw-*.bin')), [])
        self.assertFalse(self.log.exists())
        self.assertFalse(self.output.exists())

    @contextlib.contextmanager
    def copied_modules(self):
        saved = {key: value for key, value in sys.modules.items()
                 if key == 'mdi' or key.startswith('mdi.')}
        saved_path = sys.path[:]
        for key in saved:
            del sys.modules[key]
        sys.path.insert(0, str(self.skill / 'scripts'))
        try:
            yield importlib.import_module('mdi.cli')
        finally:
            for key in list(sys.modules):
                if key == 'mdi' or key.startswith('mdi.'):
                    del sys.modules[key]
            sys.modules.update(saved)
            sys.path[:] = saved_path

    def test_proposal_is_inert_generated_and_bound_to_sink(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        self.assertEqual(capture['state'], 'proposed')
        self.assertEqual(capture['scope']['source_operation_id'], source['id'])
        self.assertEqual(capture['scope']['source_kind'], 'maven')
        self.assertEqual(capture['scope']['stream'], 'maven-output')
        self.assertEqual(capture['scope']['max_bytes'], 12)
        self.assertRegex(capture['scope']['output_name'], r'^raw-[0-9a-f-]{36}\.bin$')
        self.assertEqual(capture['input_identities'], [])
        with self.copied_modules():
            sink = importlib.import_module('mdi.raw_capture_store')
            self.assertEqual(capture['tool_identity'], sink.TOOL_IDENTITY)
        self.assertNotIn('approved', capture)
        self.assertEqual(list(self.work.glob('approval-*.json')), [])
        self.assert_untouched(run_dir)

    def test_paired_capture_private_bounded_metadata_only_and_replay_refused(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        result = self.parse(self.paired(run_dir, source, capture))
        self.assertEqual(result['outcome'], 'succeeded')
        metadata = result['capture']
        path = Path(run_dir) / capture['scope']['output_name']
        self.assertEqual(metadata['path'], str(path))
        self.assertEqual(path.stat().st_size, 12)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertTrue(metadata['truncated'])
        self.assertIsNone(metadata['stream_sha256'])
        self.assertEqual(metadata['stored_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        report = self.report(run_dir)
        self.assertEqual([op['state'] for op in report['operations']], ['finished', 'finished'])
        for document in (result, report):
            self.assertNotIn('SECRET-OUTPUT', json.dumps(document))
        self.error(self.paired(run_dir, source, capture, revision=result['revision']))
        self.assertEqual(self.log.read_text(), 'executed\n')
        self.assertEqual(path.stat().st_size, 12)

    def test_child_observes_both_consumed_before_output(self):
        run_dir, _, candidate = self.selected_run()
        self.maven.write_text(
            f'#!{sys.executable}\n'
            'import json\nfrom pathlib import Path\n'
            f'root = Path({run_dir!r})\n'
            'reports = [json.loads(p.read_text()) for p in root.glob("*.json")]\n'
            'report = max((r for r in reports if "revision" in r), key=lambda r: r["revision"])\n'
            'assert [op["state"] for op in report["operations"]] == ["consumed", "consumed"]\n'
            f'Path({str(self.log)!r}).write_text("both-consumed")\n'
            'print("SYNTHETIC-RAW", end="")\n', encoding='utf-8')
        source = self.parse(self.run_cli(
            'propose', '--run-dir', run_dir, '--expect-revision', 2, '--kind', 'tree',
            '--candidate', candidate, '--maven', self.maven, '--output', self.output,
            '--input', self.project / 'pom.xml', '--destinations-unknown'))['operation']
        capture = self.register(run_dir, source, cap=13)
        result = self.parse(self.paired(run_dir, source, capture))
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertEqual(self.log.read_text(), 'both-consumed')
        self.assertEqual(result['capture']['status'], 'complete')
        self.assertEqual(result['capture_outcome'], 'succeeded')
        self.assertEqual((Path(run_dir) / capture['scope']['output_name']).read_bytes(), b'SYNTHETIC-RAW')

    def test_explicit_stored_denial_allows_source_only_without_capture(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        with self.copied_modules() as cli:
            operations = importlib.import_module('mdi.operations')
            operations.deny_proposal(cli._open_run(run_dir), capture['id'],
                                     'explicit synthetic fixture denial', 4)
        result = self.parse(self.run_cli(
            'execute', '--run-dir', run_dir, '--expect-revision', 5,
            '--operation', source['id'], '--approval-file', self.write_approval(run_dir, source)))
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertNotIn('capture', result)
        self.assertEqual(list(Path(run_dir).glob('raw-*.bin')), [])
        self.assertEqual(self.report(run_dir)['operations'][1]['state'], 'denied')

    def test_source_only_default_has_no_capture(self):
        run_dir, source = self.propose_and_select()
        result = self.parse(self.run_cli(
            'execute', '--run-dir', run_dir, '--expect-revision', 3,
            '--operation', source['id'], '--approval-file', self.write_approval(run_dir, source)))
        self.assertNotIn('capture', result)
        self.assertEqual(list(Path(run_dir).glob('raw-*.bin')), [])
        self.assertNotIn('SECRET-OUTPUT', json.dumps(self.report(run_dir)))

    def test_pending_capture_never_autoselected_or_denied(self):
        run_dir, source = self.propose_and_select()
        self.register(run_dir, source)
        self.error(self.run_cli(
            'execute', '--run-dir', run_dir, '--expect-revision', 4,
            '--operation', source['id'], '--approval-file', self.write_approval(run_dir, source)))
        self.assert_untouched(run_dir)

    def test_missing_capture_flags_and_files_refuse_before_source(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        common = ['execute', '--run-dir', run_dir, '--expect-revision', 4,
                  '--operation', source['id'], '--approval-file', self.write_approval(run_dir, source)]
        for flags in (['--capture-operation', capture['id']],
                      ['--capture-approval-file', self.write_approval(run_dir, capture)],
                      ['--capture-operation', capture['id'], '--capture-approval-file', self.work / 'missing']):
            with self.subTest(flags=flags):
                self.error(self.run_cli(*common, *flags))
                self.assert_untouched(run_dir)

    def test_denied_wrong_scope_and_wrong_operation_approvals_refuse(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        for override in ({'approved': False}, {'approved': 1},
                         {'operation_id': source['id']}, {'run_id': 'other'},
                         {'scope': dict(capture['scope'], max_bytes=13)}):
            with self.subTest(override=override):
                self.error(self.paired(run_dir, source, capture, **override))
                self.assert_untouched(run_dir)

    def test_wrong_source_approval_and_missing_source_file_refuse(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        common = ['execute', '--run-dir', run_dir, '--expect-revision', 4,
                  '--operation', source['id'], '--capture-operation', capture['id'],
                  '--capture-approval-file', self.write_approval(run_dir, capture)]
        for source_file in (self.work / 'missing',
                            self.write_approval(run_dir, source, approved=False)):
            self.error(self.run_cli(*common, '--approval-file', source_file))
            self.assert_untouched(run_dir)

    def test_capture_for_different_source_refused(self):
        run_dir, source = self.propose_and_select()
        report = self.report(run_dir)
        candidate = report['selection']['candidate_ids'][0]
        other = self.parse(self.run_cli(
            'propose', '--run-dir', run_dir, '--expect-revision', 3, '--kind', 'advisory',
            '--candidate', candidate))['operation']
        capture = self.register(run_dir, other, revision=4)
        self.error(self.paired(run_dir, source, capture, revision=5))
        self.assert_untouched(run_dir, 5)

    def test_direct_capture_execution_refused(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        message = self.error(self.run_cli(
            'execute', '--run-dir', run_dir, '--expect-revision', 4,
            '--operation', capture['id'], '--approval-file', self.write_approval(run_dir, capture)))
        self.assertIn('raw-capture', message)
        self.assert_untouched(run_dir)

    def test_stale_pair_refused(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        result = self.paired(run_dir, source, capture, revision=3)
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assert_untouched(run_dir)

    def test_invalid_proposal_flags_and_caps_leave_source_untouched(self):
        run_dir, source = self.propose_and_select()
        common = ['propose', '--run-dir', run_dir, '--expect-revision', 3, '--kind', 'raw-capture']
        for flags in ([], ['--source-operation', source['id']], ['--max-capture-bytes', 12],
                      ['--source-operation', 'missing', '--max-capture-bytes', 12]):
            with self.subTest(flags=flags):
                self.error(self.run_cli(*common, *flags))
                self.assert_untouched(run_dir, 3)
        valid = ['--source-operation', source['id'], '--max-capture-bytes', 12]
        for flags in (['--candidate', 'anything'], ['--maven', self.maven],
                      ['--output', self.output], ['--output-overwrite'],
                      ['--input', self.project / 'pom.xml'], ['--destination', 'https://example.invalid'],
                      ['--destinations-unknown'], ['--page-token', 'page']):
            with self.subTest(flags=flags):
                self.error(self.run_cli(*common, *valid, *flags))
                self.assert_untouched(run_dir, 3)
        for cap in (0, -1, 67108865, '1.0', '1e3'):
            with self.subTest(cap=cap):
                self.error(self.run_cli(*common, '--source-operation', source['id'], '--max-capture-bytes', cap))
                self.assert_untouched(run_dir, 3)

    def test_existing_source_kinds_still_require_candidate_and_reject_capture_flags(self):
        run_dir, _, candidate = self.selected_run()
        for kind in ('tree', 'effective-pom', 'classpath', 'advisory'):
            common = ['propose', '--run-dir', run_dir, '--expect-revision', 2, '--kind', kind]
            with self.subTest(kind=kind):
                self.assertIn('--candidate', self.error(self.run_cli(*common)))
                for flags in (['--source-operation', 'anything'], ['--max-capture-bytes', 12]):
                    self.error(self.run_cli(*common, '--candidate', candidate, *flags))
        self.assertEqual(self.report(run_dir)['revision'], 2)

    def test_duplicate_registration_and_finished_source_refused(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        common = ['propose', '--run-dir', run_dir, '--kind', 'raw-capture',
                  '--source-operation', source['id'], '--max-capture-bytes', 12]
        self.error(self.run_cli(*common, '--expect-revision', 4))
        result = self.parse(self.paired(run_dir, source, capture))
        self.error(self.run_cli(*common, '--expect-revision', result['revision']))

    def test_changed_source_input_or_capture_tool_refuses_before_spawn(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        pom = self.project / 'pom.xml'
        original = pom.read_text()
        pom.write_text(original + '\n<!-- changed fixture -->\n')
        self.error(self.paired(run_dir, source, capture))
        self.assert_untouched(run_dir)
        run_dir, source = self.propose_and_select()
        with self.copied_modules() as cli:
            consent = importlib.import_module('mdi.raw_capture_consent')
            operations = importlib.import_module('mdi.operations')
            store = cli._open_run(run_dir)
            bad = consent.build_capture_proposal(store.load(), source['id'], max_bytes=12,
                                                  tool_identity={'name': 'wrong-sink'})
            operations.propose(store, bad, 3)
        self.error(self.paired(run_dir, source, bad))
        self.assert_untouched(run_dir)

    def test_strict_capture_approval_json_refuses_before_source(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source)
        source_file = self.write_approval(run_dir, source)
        capture_file = self.write_approval(run_dir, capture)
        for body in ('[]', '{"approved":true,"approved":false}', '{"approved":NaN}', '{'):
            with self.subTest(body=body):
                capture_file.write_text(body)
                self.error(self.run_cli(
                    'execute', '--run-dir', run_dir, '--expect-revision', 4,
                    '--operation', source['id'], '--approval-file', source_file,
                    '--capture-operation', capture['id'], '--capture-approval-file', capture_file))
                self.assert_untouched(run_dir)

    def test_publication_failure_returns_capture_recovery_metadata_not_bytes(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source, cap=1024)
        source_file = self.write_approval(run_dir, source)
        capture_file = self.write_approval(run_dir, capture)
        with self.copied_modules() as cli:
            original = cli.RunStore.commit
            def fail_evidence(store, draft, expected_revision):
                if expected_revision == 5:
                    raise RuntimeError('SECRET-ERROR-MARKER')
                return original(store, draft, expected_revision)
            output = io.StringIO()
            with mock.patch.object(cli.RunStore, 'commit', fail_evidence):
                with contextlib.redirect_stdout(output):
                    code = cli.main(['execute', '--run-dir', run_dir, '--expect-revision', '4',
                                     '--operation', source['id'], '--approval-file', str(source_file),
                                     '--capture-operation', capture['id'], '--capture-approval-file', str(capture_file)])
        self.assertEqual(code, 4, output.getvalue())
        result = json.loads(output.getvalue())
        path = Path(run_dir) / capture['scope']['output_name']
        self.assertEqual(result['capture']['path'], str(path))
        self.assertEqual(result['capture']['status'], 'complete')
        self.assertTrue(result['capture']['retained'])
        self.assertIn(b'SECRET-OUTPUT', path.read_bytes())
        self.assertNotIn('SECRET', output.getvalue())
        self.assertEqual([op['state'] for op in self.report(run_dir)['operations']], ['consumed', 'consumed'])

    def test_advisory_finalization_conflicts_preserve_capture_recovery(self):
        for failing_revision in (6, 7):
            with self.subTest(failing_revision=failing_revision):
                run_dir, source = self.propose_and_select('advisory')
                capture = self.register(run_dir, source, cap=1024)
                source_file = self.write_approval(run_dir, source)
                capture_file = self.write_approval(run_dir, capture)
                body = b'{"vulns":[],"unused":"SECRET-RESPONSE"}'
                with self.copied_modules() as cli:
                    executor = importlib.import_module('mdi.advisory_executor')
                    original = cli.RunStore.commit
                    def fail_finalization(store, draft, expected_revision):
                        if expected_revision == failing_revision:
                            raise cli.RunConflict('synthetic finalization conflict')
                        return original(store, draft, expected_revision)
                    transport = mock.Mock()
                    transport.post.return_value = body
                    output = io.StringIO()
                    with mock.patch.object(executor, 'DeadlineTransport', return_value=transport), \
                            mock.patch.object(cli.RunStore, 'commit', fail_finalization), \
                            contextlib.redirect_stdout(output):
                        code = cli.main(['execute', '--run-dir', run_dir, '--expect-revision', '4',
                                         '--operation', source['id'], '--approval-file', str(source_file),
                                         '--capture-operation', capture['id'],
                                         '--capture-approval-file', str(capture_file)])
                    transport.post.assert_called_once()
                self.assertEqual(code, 3, output.getvalue())
                result = json.loads(output.getvalue())
                path = Path(run_dir) / capture['scope']['output_name']
                self.assertEqual(result['capture']['path'], str(path))
                self.assertTrue(result['capture']['retained'])
                self.assertEqual(path.read_bytes(), body)
                self.assertNotIn('SECRET-RESPONSE', output.getvalue())
                report = self.report(run_dir)
                self.assertEqual(report['revision'], failing_revision)
                self.assertEqual([op['state'] for op in report['operations']],
                                 ['consumed' if failing_revision == 6 else 'finished', 'consumed'])
                self.assertTrue(any(e['category'] == 'raw-capture' for e in report['evidence']))

    def test_capture_with_policy_source_and_real_capture_approval(self):
        run_dir, source = self.propose_and_select()
        capture = self.register(run_dir, source, cap=4096)
        capture_file = self.write_approval(run_dir, capture)
        proc = self.run_cli(
            'execute', '--run-dir', run_dir, '--expect-revision', '4',
            '--operation', source['id'],
            '--capture-operation', capture['id'], '--capture-approval-file', capture_file)
        doc = self.parse(proc)
        self.assertEqual(doc['outcome'], 'succeeded')
        self.assertEqual(doc['capture_outcome'], 'succeeded')
        report = self.report(run_dir)
        states = {op['id']: op for op in report['operations']}
        self.assertEqual(states[source['id']]['audit']['started']['approved_by'],
                         'standing-diagnostic-policy')
        self.assertNotEqual(states[capture['id']]['audit']['started']['approved_by'],
                            'standing-diagnostic-policy')
        self.assertEqual(states[capture['id']]['audit']['started']['approved_by'],
                         'the host user')

    def test_advisory_without_approval_file_runs_under_standing_policy(self):
        run_dir, source = self.propose_and_select('advisory')
        body = b'{"vulns": []}'
        case = self
        with self.copied_modules() as cli:
            executor = importlib.import_module('mdi.advisory_executor')
            class Transport:
                def post(self, *args):
                    return body
            output = io.StringIO()
            with mock.patch.object(executor, 'DeadlineTransport', return_value=Transport()), \
                    contextlib.redirect_stdout(output):
                code = cli.main(['execute', '--run-dir', run_dir, '--expect-revision', '3',
                                 '--operation', source['id']])
            case.assertEqual(code, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result['outcome'], 'succeeded')
        report = self.report(run_dir)
        finished = {op['id']: op for op in report['operations']}[source['id']]
        self.assertEqual(finished['audit']['started']['approved_by'],
                         'standing-diagnostic-policy')
        self.assertNotIn('SECRET-RESPONSE', output.getvalue())

    def test_advisory_pair_consumed_before_mock_transport_and_no_raw_in_json(self):
        run_dir, source = self.propose_and_select('advisory')
        capture = self.register(run_dir, source, cap=1024)
        source_file = self.write_approval(run_dir, source)
        capture_file = self.write_approval(run_dir, capture)
        case = self
        body = b'{ "vulns": [], "unused": "SECRET-RESPONSE-MARKER" }'
        with self.copied_modules() as cli:
            executor = importlib.import_module('mdi.advisory_executor')
            class Transport:
                def post(self, *args):
                    report = cli._open_run(run_dir).load()
                    case.assertEqual([op['state'] for op in report['operations']], ['consumed', 'consumed'])
                    return body
            output = io.StringIO()
            with mock.patch.object(executor, 'DeadlineTransport', return_value=Transport()) as transport:
                with contextlib.redirect_stdout(output):
                    code = cli.main(['execute', '--run-dir', run_dir, '--expect-revision', '4',
                                     '--operation', source['id'], '--approval-file', str(source_file),
                                     '--capture-operation', capture['id'], '--capture-approval-file', str(capture_file)])
                self.assertEqual(code, 0, output.getvalue())
                transport.assert_called_once()
        result = json.loads(output.getvalue())
        self.assertEqual(result['capture']['status'], 'complete')
        self.assertEqual(result['capture_outcome'], 'succeeded')
        self.assertEqual((Path(run_dir) / capture['scope']['output_name']).read_bytes(), body)
        self.assertNotIn('SECRET-RESPONSE', output.getvalue())
        self.assertNotIn('SECRET-RESPONSE', json.dumps(self.report(run_dir)))

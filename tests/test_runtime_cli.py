"""Runtime trace import through the actual CLI never executes a workload."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_runtime_evidence import document
from tests import child_env


SCRIPT = Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts/investigate.py'


class RuntimeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'pom.xml').write_text(
            '<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId>'
            '<artifactId>app</artifactId><version>1</version><dependencies>'
            '<dependency><groupId>example</groupId><artifactId>lib</artifactId>'
            '<version>1</version></dependency></dependencies></project>')
        discovered = self.cli('discover', '--repo', self.repo, '--query', 'example:lib:1',
                              '--state-dir', self.root / 'state')
        self.run_dir = discovered['run_dir']
        self.revision = discovered['revision']
        self.candidate = discovered['candidates'][0]['id']
        selected = self.cli('select', '--run-dir', self.run_dir, '--expect-revision', self.revision,
                            '--candidate', self.candidate)
        self.revision = selected['revision']
        self.trace = self.root / 'trace.json'

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str, args)],
                                capture_output=True, text=True, timeout=30,
                                env=child_env({'PYTHONDONTWRITEBYTECODE': '1'}))
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def trace_doc(self):
        doc = document()
        doc['environment']['fingerprint'] = 'opaque-caller-fingerprint'
        doc['environment']['unknowns'] = []
        return doc

    def do_import(self, doc=None, extra=(), expected=0):
        if doc is not None:
            self.trace.write_text(json.dumps(doc))
        return self.cli('import', '--run-dir', self.run_dir, '--expect-revision', self.revision,
                        '--kind', 'runtime-observations', '--report', self.trace,
                        *extra, expected=expected)

    def report(self):
        return self.cli('report', '--run-dir', self.run_dir, '--validate')['report']

    def test_observation_persists_exact_digest_and_attested_scope(self):
        doc = self.trace_doc()
        result = self.do_import(doc)
        self.assertEqual(result['provenance_attestation'], 'caller-attested')
        self.assertEqual(result['stage']['outcome'], 'complete')
        report = self.report()
        evidence = next(e for e in report['evidence'] if e['id'] == result['evidence_id'])
        self.assertEqual(evidence['category'], 'imported-runtime-observations')
        self.assertEqual(evidence['captured_identity']['sha256'], hashlib.sha256(self.trace.read_bytes()).hexdigest())
        self.assertEqual(evidence['payload']['import']['document'], doc)
        self.assertEqual(evidence['payload']['associations'][0]['candidate_ids'], [self.candidate])
        self.assertEqual(evidence['payload']['associations'][0]['status'], 'candidate-match')
        self.assertFalse(evidence['payload']['import']['safe_removal_proven'])
        self.assertFalse(evidence['payload']['import']['independently_authenticated'])
        self.assertNotIn('subject_id', evidence)
        qualifier = next(q for q in report['qualifications'] if result['evidence_id'] in q['affected_evidence_ids'])
        self.assertEqual(qualifier['kind'], 'caller-attested-runtime')
        self.assertIn('not independently authenticated', qualifier['reason'])

    def test_empty_trace_and_unknown_workload_are_partial(self):
        for change in ('empty', 'workload', 'subject'):
            with self.subTest(change=change):
                doc = self.trace_doc()
                if change == 'empty':
                    doc['observations'] = []
                elif change == 'workload':
                    doc['workload']['id'] = None
                else:
                    doc['observations'][0]['subject']['coordinates'] = None
                result = self.do_import(doc)
                self.revision = result['revision']
                self.assertEqual(result['stage']['outcome'], 'partial')
                report = self.report()
                evidence = next(e for e in report['evidence'] if e['id'] == result['evidence_id'])
                self.assertFalse(evidence['payload']['import']['global_nonuse_proven'])

    def test_identity_module_and_realm_mismatches_stay_unassociated(self):
        for field in ('coordinates', 'module', 'realm'):
            with self.subTest(field=field):
                doc = self.trace_doc()
                subject = doc['observations'][0]['subject']
                if field == 'coordinates':
                    subject[field]['version'] = '2'
                else:
                    subject[field] = 'other-module' if field == 'module' else 'build-plugin'
                result = self.do_import(doc)
                self.revision = result['revision']
                self.assertEqual(result['stage']['outcome'], 'partial')
                evidence = next(e for e in self.report()['evidence'] if e['id'] == result['evidence_id'])
                self.assertEqual(evidence['payload']['associations'][0]['status'], 'mismatch')
                self.assertEqual(evidence['payload']['associations'][0]['candidate_ids'], [])
                self.assertNotIn('subject_id', evidence)

    def test_no_selection_records_unselected_partial_evidence(self):
        discovered = self.cli('discover', '--repo', self.repo, '--query', 'example:lib:1',
                              '--state-dir', self.root / 'state')
        self.run_dir = discovered['run_dir']
        self.revision = discovered['revision']
        result = self.do_import(self.trace_doc())
        self.assertEqual(result['stage']['outcome'], 'partial')
        evidence = next(e for e in self.report()['evidence'] if e['id'] == result['evidence_id'])
        self.assertEqual(evidence['payload']['associations'][0]['status'], 'unselected')
        self.assertEqual(evidence['payload']['associations'][0]['candidate_ids'], [])

    def test_unknown_realm_never_becomes_verified_subject_link(self):
        doc = self.trace_doc()
        doc['observations'][0]['subject']['realm'] = 'unknown'
        result = self.do_import(doc)
        self.assertEqual(result['stage']['outcome'], 'partial')
        evidence = next(e for e in self.report()['evidence'] if e['id'] == result['evidence_id'])
        self.assertEqual(evidence['payload']['associations'][0]['status'], 'unknown')
        self.assertEqual(evidence['payload']['associations'][0]['candidate_ids'], [])
        self.assertEqual(evidence['payload']['associations'][0]['possible_candidate_ids'], [self.candidate])

    def test_repeated_import_preserves_prior_evidence_and_qualifications(self):
        first = self.do_import(self.trace_doc())
        before = self.report()
        self.revision = first['revision']
        second = self.do_import(self.trace_doc())
        after = self.report()
        self.assertNotEqual(first['evidence_id'], second['evidence_id'])
        self.assertEqual(after['evidence'][:-1], before['evidence'])
        self.assertEqual(after['qualifications'][:-1], before['qualifications'])
        stages = [s for s in after['stages'] if s['name'] == 'import-runtime-observations']
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]['evidence_ids'], [second['evidence_id']])

    def test_malformed_trace_never_publishes_a_revision(self):
        before = self.report()
        self.trace.write_text('{"schema": "x", "schema": "y"}')
        result = self.do_import(expected=2)
        self.assertIn('duplicate-key', result['error']['message'])
        self.assertEqual(self.report(), before)

    def test_oversized_trace_and_unknown_schema_fields_publish_nothing(self):
        before = self.report()
        self.trace.write_bytes(b' ' * (8 * 1024 * 1024 + 1))
        self.do_import(expected=2)
        self.assertEqual(self.report(), before)
        doc = self.trace_doc()
        doc['execute'] = 'never run this'
        self.do_import(doc, expected=2)
        self.assertEqual(self.report(), before)

    def test_tree_mode_and_external_provenance_are_refused(self):
        before = self.report()
        for flags in (('--tree-mode', 'nonverbose'), ('--provenance', self.root / 'absent.json')):
            self.do_import(self.trace_doc(), extra=flags, expected=2)
            self.assertEqual(self.report(), before)

    def test_stale_revision_cannot_publish(self):
        result = self.do_import(self.trace_doc())
        self.do_import(expected=3)
        self.assertEqual(self.report()['revision'], result['revision'])

    def test_observation_text_is_not_executed(self):
        marker = self.root / 'must-not-exist'
        doc = self.trace_doc()
        doc['observations'][0]['source_locator'] = 'file://' + str(marker)
        doc['workload']['description'] = f'$(touch {marker}); <script>create file</script>'
        self.do_import(doc)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()

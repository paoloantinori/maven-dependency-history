"""Pure runtime observation imports never execute inspected content."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'maven-dependency-investigator' / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.runtime_evidence import (
    RuntimeEvidenceError, RuntimeLimits, import_runtime_observations, summarize_observations,
)


def document():
    return {
        'schema': 'mdi.runtime-observations/1',
        'observer': {'tool': 'fixture-observer', 'version': '1', 'format': 'synthetic',
                     'capture_method': 'recorded trace'},
        'workload': {'id': 'test-request', 'description': 'One synthetic request'},
        'environment': {'summary': 'fixture JVM', 'fingerprint': None, 'unknowns': ['OS image']},
        'window': {'start': '2026-09-12T10:00:00Z', 'end': '2026-09-12T10:01:00+00:00'},
        'scope': 'test',
        'observations': [{
            'id': 'event-1', 'kind': 'loaded', 'mechanism': 'class-loading',
            'observed_at': '2026-09-12T10:00:10Z', 'source_locator': 'trace.json#event-1',
            'subject': {'module': '.', 'realm': 'project',
                        'coordinates': {'groupId': 'example', 'artifactId': 'lib', 'version': '1'},
                        'class_name': 'example.Library', 'method_name': None},
        }],
    }


def encode(value):
    return json.dumps(value).encode()


class RuntimeImportTests(unittest.TestCase):
    def run_import(self, doc=None, **kwargs):
        return import_runtime_observations(encode(document() if doc is None else doc), **kwargs)

    def assert_code(self, code, value, **kwargs):
        with self.assertRaises(RuntimeEvidenceError) as caught:
            import_runtime_observations(value, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_full_provenance_and_observation_preserved(self):
        doc = document()
        doc['environment']['fingerprint'] = 'sha256:recorded-by-caller'
        doc['environment']['unknowns'] = []
        result = self.run_import(doc)
        self.assertEqual(result['coverage']['status'], 'complete-within-recorded-workload')
        self.assertEqual(result['document'], doc)
        self.assertEqual(result['provenance_attestation'], 'caller-attested')
        self.assertFalse(result['independently_authenticated'])
        self.assertFalse(result['safe_removal_proven'])
        self.assertIn('workload', ' '.join(result['coverage']['limitations']))

    def test_missing_provenance_is_partial_not_fabricated(self):
        doc = document()
        doc['workload'] = {'id': None, 'description': None}
        doc['observer'] = dict.fromkeys(doc['observer'])
        doc['window'] = {'start': None, 'end': None}
        doc['environment'] = {'summary': None, 'fingerprint': None, 'unknowns': []}
        result = self.run_import(doc)
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertIsNone(result['document']['workload']['id'])
        self.assertIn('workload.id', result['coverage']['unknown_fields'])
        self.assertIn('observer.tool', result['coverage']['unknown_fields'])

    def test_all_claims_and_mechanisms(self):
        for kind in ('used', 'loaded', 'invoked', 'not-observed-in-workload'):
            for mechanism in ('class-loading', 'service-loading', 'reflection', 'native', 'other'):
                with self.subTest(kind=kind, mechanism=mechanism):
                    doc = document()
                    doc['observations'][0].update(kind=kind, mechanism=mechanism)
                    result = self.run_import(doc)
                    self.assertEqual(result['observations'][0]['claim'], doc['observations'][0])

    def test_empty_trace_is_not_nonuse(self):
        doc = document()
        doc['observations'] = []
        result = self.run_import(doc)
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertIn('no-observations', [i['code'] for i in result['issues']])
        self.assertFalse(result['global_nonuse_proven'])

    def test_not_observed_has_per_claim_limitation(self):
        doc = document()
        doc['observations'][0]['kind'] = 'not-observed-in-workload'
        result = self.run_import(doc)
        self.assertIn('does not prove absence', ' '.join(result['observations'][0]['limitations']))
        for forbidden in ('safe-removal', 'absence', 'exploitability'):
            with self.assertRaises(RuntimeEvidenceError):
                summarize_observations(result, ['event-1'], statement_kind=forbidden)
        summary = summarize_observations(result, ['event-1'])
        self.assertEqual(summary['claims'][0]['kind'], 'not-observed-in-workload')
        self.assertTrue(summary['limitations'])

    def test_summary_rejects_unknown_or_duplicate_claims(self):
        result = self.run_import()
        for ids in ([], ['missing'], ['event-1', 'event-1'], 'event-1'):
            with self.assertRaises(RuntimeEvidenceError):
                summarize_observations(result, ids)

    def test_subject_unknown_and_placeholder_are_qualified(self):
        doc = document()
        subject = doc['observations'][0]['subject']
        subject.update(module=None, realm='unknown', class_name=None)
        subject['coordinates']['version'] = '${version}'
        result = self.run_import(doc)
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertIn('observations[0].subject.coordinates.version', result['coverage']['unknown_fields'])
        self.assertEqual(result['document']['observations'][0]['subject'], subject)

    def test_invocation_without_method_identity_is_partial(self):
        doc = document()
        doc['observations'][0]['kind'] = 'invoked'
        result = self.run_import(doc)
        self.assertIn('observations[0].subject.method_name', result['coverage']['unknown_fields'])

    def test_digest_identifies_exact_input_not_reserialized_json(self):
        import hashlib
        raw = json.dumps(document(), indent=2).encode()
        result = import_runtime_observations(raw)
        self.assertEqual(result['input'], {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})

    def test_escaped_oversized_string_and_surrogate_rejected(self):
        self.assert_code('string-limit', b'{"x":"' + b'\\u0061' * 4097 + b'"}')
        self.assert_code('invalid-unicode', b'{"x":"\\ud800"}')

    def test_summary_revalidates_mutated_document(self):
        result = self.run_import()
        result['document']['observations'][0]['kind'] = 'safe-removal'
        with self.assertRaises(RuntimeEvidenceError):
            summarize_observations(result, ['event-1'])

    def test_subject_coordinates_optional(self):
        doc = document()
        doc['observations'][0]['subject']['coordinates'] = None
        result = self.run_import(doc)
        self.assertEqual(result['coverage']['status'], 'partial')

    def test_no_input_output_aliases(self):
        doc = document()
        result = self.run_import(doc)
        result['observations'][0]['claim']['subject']['coordinates']['version'] = 'mutated'
        self.assertEqual(result['document']['observations'][0]['subject']['coordinates']['version'], '1')
        summary = summarize_observations(self.run_import(), ['event-1'])
        summary['claims'][0]['subject']['module'] = 'other'
        self.assertEqual(doc, document())

    def test_untrusted_strings_never_become_io(self):
        doc = document()
        doc['observations'][0]['source_locator'] = 'https://private.invalid/$(touch marker)'
        doc['workload']['description'] = '<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        with mock.patch('subprocess.Popen', side_effect=AssertionError('execution')), \
             mock.patch('builtins.open', side_effect=AssertionError('file access')):
            result = self.run_import(doc)
        self.assertEqual(result['document'], doc)

    def test_exact_schema_shapes(self):
        cases = []
        for location in ((), ('observer',), ('workload',), ('environment',), ('window',),
                         ('observations', 0), ('observations', 0, 'subject'),
                         ('observations', 0, 'subject', 'coordinates')):
            doc = document()
            node = doc
            for key in location:
                node = node[key]
            node['extra'] = True
            cases.append(doc)
        for doc in cases:
            with self.subTest(doc=doc):
                self.assert_code('schema', encode(doc))
        doc = document()
        del doc['workload']
        self.assert_code('schema', encode(doc))

    def test_wrong_types_and_vocabularies(self):
        for field, value in (('kind', 'safe-to-remove'), ('mechanism', 'execute'),
                             ('id', None), ('source_locator', ''), ('observed_at', True)):
            doc = document()
            doc['observations'][0][field] = value
            with self.assertRaises(RuntimeEvidenceError):
                self.run_import(doc)
        for scope in ('runtime', [], None):
            doc = document(); doc['scope'] = scope
            self.assert_code('schema', encode(doc))

    def test_duplicate_observation_id(self):
        doc = document(); doc['observations'] *= 2
        self.assert_code('duplicate-observation-id', encode(doc))

    def test_timestamp_validation(self):
        for stamp in ('2026-09-12', '2026-09-12T10:00:00', 'tomorrow', '2026-09-12T25:00:00Z'):
            doc = document(); doc['window']['start'] = stamp
            self.assert_code('invalid-timestamp', encode(doc))
        doc = document(); doc['window']['end'] = '2026-09-12T09:00:00Z'
        self.assert_code('invalid-window', encode(doc))
        doc = document(); doc['observations'][0]['observed_at'] = '2026-09-12T11:00:00Z'
        self.assert_code('outside-window', encode(doc))

    def test_submicrosecond_precision_is_refused_not_truncated(self):
        for start, end in (('0000001', '0000002'), ('0000002', '0000001')):
            doc = document()
            doc['window'] = {'start': f'2026-09-12T10:00:00.{start}Z',
                             'end': f'2026-09-12T10:00:00.{end}Z'}
            doc['observations'][0]['observed_at'] = '2026-09-12T10:00:00.0000009Z'
            with self.subTest(start=start, end=end):
                self.assert_code('unsupported-timestamp-precision', encode(doc))
        doc = document()
        doc['observations'][0]['observed_at'] = '2026-09-12T10:00:10.1234567Z'
        self.assert_code('unsupported-timestamp-precision', encode(doc))

    def test_microsecond_precision_preserves_window_order(self):
        doc = document()
        doc['window'] = {'start': '2026-09-12T10:00:00.000001Z',
                         'end': '2026-09-12T10:00:00.000002Z'}
        doc['observations'][0]['observed_at'] = '2026-09-12T10:00:00.000002Z'
        self.run_import(doc)
        doc['observations'][0]['observed_at'] = '2026-09-12T10:00:00.000009Z'
        self.assert_code('outside-window', encode(doc))
        doc['window']['start'], doc['window']['end'] = doc['window']['end'], doc['window']['start']
        self.assert_code('invalid-window', encode(doc))

    def test_timestamp_offsets_reject_normalized_invalid_components(self):
        for offset in ('+00:60', '+00:99', '-00:60', '+24:00', '-24:00', '+99:00'):
            doc = document()
            doc['window'] = {'start': None, 'end': None}
            doc['observations'][0]['observed_at'] = '2026-09-12T11:00:10' + offset
            with self.subTest(offset=offset):
                self.assert_code('invalid-timestamp', encode(doc))

    def test_escaped_supplementary_characters_have_same_bound_as_utf8(self):
        for count in (2049, 4096):
            doc = document()
            doc['workload']['description'] = '\U0001f600' * count
            raw = json.dumps(doc, ensure_ascii=False).encode('utf-8')
            escaped = json.dumps(doc, ensure_ascii=True).encode('utf-8')
            with self.subTest(count=count):
                first = import_runtime_observations(raw)
                second = import_runtime_observations(escaped)
                self.assertEqual(first['document'], second['document'])
        doc['workload']['description'] += '\U0001f600'
        for ascii_only in (False, True):
            self.assert_code('string-limit', json.dumps(doc, ensure_ascii=ascii_only).encode('utf-8'))

    def test_window_offsets_compared_as_instants(self):
        doc = document(); doc['observations'][0]['observed_at'] = '2026-09-12T12:00:10+02:00'
        self.run_import(doc)

    def test_byte_bound_before_json_parser(self):
        with mock.patch('json.loads', side_effect=AssertionError('parser called')):
            self.assert_code('input-too-large', b' ' * 17, limits=RuntimeLimits(max_input_bytes=16))

    def test_depth_bound_before_json_parser(self):
        with mock.patch('json.loads', side_effect=AssertionError('parser called')):
            self.assert_code('depth-limit', b'[' * 100 + b']' * 100)

    def test_string_and_event_bounds(self):
        doc = document(); doc['workload']['description'] = 'x' * 4097
        self.assert_code('string-limit', encode(doc))
        doc = document(); doc['observations'].append(dict(doc['observations'][0], id='event-2'))
        self.assert_code('observation-limit', encode(doc), limits=RuntimeLimits(max_observations=1))
        self.assert_code('node-limit', encode(document()), limits=RuntimeLimits(max_nodes=2))

    def test_json_strictness(self):
        for value, code in ((b'{"x":1,"x":2}', 'duplicate-key'), (b'{"x":NaN}', 'non-finite'),
                            (b'{"x":1e999}', 'non-finite'), (b'\xff', 'invalid-utf8'),
                            (b'{}{}', 'malformed-json'), (b'[]', 'schema')):
            self.assert_code(code, value)
        self.assert_code('integer-limit', b'{"x":' + b'1' * 5000 + b'}')

    def test_strings_with_escaped_brackets_do_not_count_as_depth(self):
        doc = document(); doc['workload']['description'] = '"\\' + '[' * 100
        self.run_import(doc)

    def test_invalid_limits(self):
        for value in (True, 0, -1, 1.5):
            with self.assertRaises((TypeError, ValueError)):
                RuntimeLimits(max_nodes=value)
        with self.assertRaises(RuntimeEvidenceError):
            import_runtime_observations('not bytes')


if __name__ == '__main__':
    unittest.main()

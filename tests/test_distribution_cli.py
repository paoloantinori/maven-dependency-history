"""Local distribution/OCI collection through the shipped CLI; no pulls."""
import contextlib
import hashlib
import io
import json
import os
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests.test_cli import CliTestCase, make_project


def jar_bytes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('META-INF/maven/org.example/b-lib/pom.properties',
                   'groupId=org.example\nartifactId=b-lib\nversion=1.0.0\n')
    return out.getvalue()


def distribution_bytes(gzip=False, whiteout=False):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz' if gzip else 'w', format=tarfile.USTAR_FORMAT) as t:
        data = jar_bytes()
        info = tarfile.TarInfo('lib/b.jar')
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
        if whiteout:
            t.addfile(tarfile.TarInfo('lib/.wh.b.jar'), io.BytesIO())
    return out.getvalue()


def layer_bytes(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def make_layout(path, ambiguous=False, layers=None):
    path.mkdir()
    (path / 'blobs' / 'sha256').mkdir(parents=True)
    (path / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    def blob(media, data):
        digest = hashlib.sha256(data).hexdigest()
        (path / 'blobs' / 'sha256' / digest).write_bytes(data)
        return dict(mediaType=media, digest='sha256:' + digest, size=len(data))
    config = blob('application/vnd.oci.image.config.v1+json',
                  b'{"os":"linux","architecture":"amd64","config":{"Env":["SECRET=hidden"]}}')
    layer_descriptors = [blob('application/vnd.oci.image.layer.v1.tar', data)
                         for data in (layers if layers is not None else [distribution_bytes(whiteout=True)])]
    def image(layers):
        return blob('application/vnd.oci.image.manifest.v1+json', json.dumps(dict(
            schemaVersion=2, mediaType='application/vnd.oci.image.manifest.v1+json',
            config=config, layers=layers)).encode())
    manifest = image(layer_descriptors)
    manifests = [manifest, image([])] if ambiguous else [manifest]
    (path / 'index.json').write_text(json.dumps(dict(schemaVersion=2,
        mediaType='application/vnd.oci.image.index.v1+json', manifests=manifests)))
    return manifest['digest']


class DistributionCliTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.work / 'project')
        self.initial, self.run_dir, self.candidate = self.selected_run(
            self.project, 'org.example:b-lib:1.0.0')

    def collect(self, *args, revision=2):
        return self.run_cli('collect', '--run-dir', self.run_dir,
                            '--expect-revision', revision, *args)

    def evidence(self):
        report = self.parse(self.run_cli('report', '--run-dir', self.run_dir, '--validate'))
        self.assertTrue(report['validation']['valid'])
        return report['report'], next(e for e in report['report']['evidence']
                                     if e['category'] == 'distribution-inspection')

    def test_tar_and_gzip_are_observations_not_subjects(self):
        for number, fmt in enumerate(('tar', 'tar.gz')):
            with self.subTest(format=fmt):
                source = self.work / ('bundle.' + fmt)
                source.write_bytes(distribution_bytes(gzip=fmt == 'tar.gz'))
                result = self.parse(self.collect('--distribution', source,
                    '--distribution-format', fmt, revision=2 + number))
                self.assertEqual(result['distribution_outcome'], 'complete')
        report, evidence = self.evidence()
        self.assertEqual(len(report['subjects']), 1)
        associations = evidence['payload']['associations']
        self.assertEqual(associations[0]['candidate_ids'], [self.candidate])
        self.assertEqual(associations[0]['basis'], 'metadata-candidate-only')
        self.assertEqual(associations[0]['realm'], 'unknown')
        self.assertNotIn('subject_id', evidence)
        self.assertTrue(any(evidence['id'] in q['affected_evidence_ids'] for q in report['qualifications']))

    def test_oci_whiteouts_never_claim_final_filesystem(self):
        layout = self.work / 'image'
        make_layout(layout)
        self.parse(self.collect('--oci-layout', layout))
        report, evidence = self.evidence()
        self.assertFalse(evidence['payload']['merged_filesystem'])
        self.assertIn('layer-observations-not-final-filesystem', evidence['payload']['limitations'])
        self.assertNotIn('SECRET=hidden', json.dumps(report))
        stage = next(s for s in report['stages'] if s['name'] == 'distribution')
        self.assertEqual(stage['outcome'], 'partial')

    def visibility_evidence(self, report):
        return [e for e in report['evidence'] if e['category'] == 'oci-visibility'][-1]

    def test_oci_whiteout_removes_visible_candidate_not_layer_observation(self):
        layout = self.work / 'layered'
        make_layout(layout, layers=[layer_bytes([('lib/b.jar', jar_bytes())]),
                                   layer_bytes([('.wh.lib', b'')])])
        self.parse(self.collect('--oci-layout', layout))
        report, source = self.evidence()
        visible = self.visibility_evidence(report)
        self.assertEqual(len(source['payload']['associations']), 1)
        self.assertEqual(visible['payload']['associations'], [])
        self.assertEqual(visible['payload']['visibility']['java_candidates'], [])
        self.assertEqual(visible['payload']['visibility']['coverage']['status'],
                         'complete-within-recorded-layers')
        self.assertEqual(visible['payload']['source_evidence_ids'], [source['id']])
        self.assertNotIn('subject_id', visible)
        self.assertFalse(visible['payload']['merged_filesystem'])
        stage = next(s for s in report['stages'] if s['name'] == 'distribution')
        self.assertIn(visible['id'], stage['evidence_ids'])
        self.assertEqual(stage['outcome'], 'partial')

    def test_oci_jar_named_whiteout_removes_visible_candidate_precisely(self):
        layout = self.work / 'jar-whiteout'
        make_layout(layout, layers=[layer_bytes([('lib/b.jar', jar_bytes())]),
                                   layer_bytes([('lib/.wh.b.jar', b'')])])
        self.parse(self.collect('--oci-layout', layout))
        report, source = self.evidence()
        visible = self.visibility_evidence(report)
        self.assertEqual(len(source['payload']['associations']), 1)
        self.assertEqual(source['payload']['associations'][0]['candidate_ids'], [self.candidate])
        self.assertEqual(source['payload']['associations'][0]['layer_index'], 0)
        self.assertEqual(visible['payload']['visibility']['coverage']['status'],
                         'complete-within-recorded-layers')
        self.assertEqual(visible['payload']['associations'], [])
        self.assertEqual(visible['payload']['visibility']['java_candidates'], [])
        original = next(row for row in visible['payload']['visibility']['entries']
                        if row['path'] == 'lib/b.jar')
        self.assertEqual(original['visibility'], 'removed')
        self.assertEqual(original['reason'], 'whiteout')
        self.assertEqual(original['changed_by'], {'layer_index': 1, 'member_index': 0})
        self.assertFalse(visible['payload']['merged_filesystem'])
        stage = next(s for s in report['stages'] if s['name'] == 'distribution')
        self.assertEqual(stage['outcome'], 'partial')
        self.assertTrue(any(q['kind'] == 'recorded-layer-visibility'
                            and visible['id'] in q['affected_evidence_ids']
                            for q in report['qualifications']))

    def test_oci_opaque_and_replacement_use_only_visible_members(self):
        for number, upper in enumerate((
                [('lib/.wh..wh..opq', b''), ('lib/new.jar', jar_bytes())],
                [('lib/b.jar', jar_bytes())])):
            with self.subTest(upper=upper):
                layout = self.work / f'layered-{number}'
                make_layout(layout, layers=[layer_bytes([('lib/b.jar', jar_bytes())]),
                                           layer_bytes(upper)])
                self.parse(self.collect('--oci-layout', layout, revision=2 + number))
                report, _source = self.evidence()
                visible = self.visibility_evidence(report)
                self.assertEqual(len(visible['payload']['associations']), 1)
                association = visible['payload']['associations'][0]
                self.assertEqual(association['layer_index'], 1)
                self.assertEqual(association['candidate_ids'], [self.candidate])
                self.assertEqual(association['realm'], 'unknown')
                self.assertEqual(association['basis'], 'visible-member-metadata-candidate-only')
                self.assertEqual(visible['payload']['visibility']['coverage']['status'],
                                 'complete-within-recorded-layers')

    def test_oci_unsupported_or_duplicate_layer_exports_no_visible_candidates(self):
        for number, members in enumerate((
                [('lib/b.jar', jar_bytes()), ('lib/b.jar', jar_bytes())],
                [('.wh.hidden/b.jar', jar_bytes())])):
            with self.subTest(case=number):
                layout = self.work / f'uncertain-{number}'
                make_layout(layout, layers=[layer_bytes(members)])
                self.parse(self.collect('--oci-layout', layout, revision=2 + number))
                report, _source = self.evidence()
                visible = self.visibility_evidence(report)
                self.assertEqual(visible['payload']['visibility']['coverage']['status'], 'partial')
                self.assertEqual(visible['payload']['associations'], [])
                self.assertEqual(visible['payload']['visibility']['java_candidates'], [])

    def test_oci_visibility_qualifications_and_recollection_are_append_only(self):
        layout = self.work / 'qualified'
        make_layout(layout, layers=[layer_bytes([('lib/b.jar', jar_bytes())])])
        self.parse(self.collect('--oci-layout', layout))
        before, source = self.evidence()
        visible = self.visibility_evidence(before)
        inherited = [q for q in before['qualifications']
                     if visible['id'] in q['affected_evidence_ids']]
        self.assertTrue(any(q['kind'] == 'unverified-distribution-attribution' for q in inherited))
        self.assertTrue(any(q['kind'] == 'recorded-layer-visibility' for q in inherited))
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import report as report_module
        unqualified = json.loads(json.dumps(before))
        unqualified['analyses'].append(dict(id='visibility-analysis', evidence_ids=[visible['id']],
            statement_kind='observation-summary', statement='Candidate found', limitations=['generic']))
        with self.assertRaises(report_module.ReportError):
            report_module.validate_report(unqualified)
        unqualified['analyses'][-1]['limitations'] = [report_module._qualification_text(q) for q in inherited]
        report_module.validate_report(unqualified)
        self.parse(self.collect('--oci-layout', layout, revision=3))
        after, _ = self.evidence()
        self.assertEqual(after['evidence'][:len(before['evidence'])], before['evidence'])
        self.assertEqual(after['qualifications'][:len(before['qualifications'])], before['qualifications'])
        self.assertNotEqual(self.visibility_evidence(after)['id'], visible['id'])
        self.assertEqual(len({q['id'] for q in after['qualifications']}), len(after['qualifications']))
        self.assertEqual(after['subjects'], before['subjects'])

    def test_oci_visibility_rejection_preserves_inspection_and_cleanup(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        layout = self.work / 'visibility-rejected'
        make_layout(layout, layers=[layer_bytes([('lib/b.jar', jar_bytes())])])
        with mock.patch.object(cli, 'compose_visibility',
                               side_effect=cli.VisibilityError('untrusted parser detail')):
            _, code, result = self.in_process('--oci-layout', layout)
        self.assertEqual(code, 0)
        self.assertEqual(result['distribution_outcome'], 'partial')
        report, source = self.evidence()
        visible = self.visibility_evidence(report)
        self.assertEqual(len(source['payload']['associations']), 1)
        self.assertEqual(source['payload']['cleanup']['leftovers'], [])
        self.assertIsNone(visible['payload']['visibility'])
        self.assertEqual(visible['payload']['associations'], [])
        self.assertEqual(visible['payload']['issues'], [{'code': 'oci-visibility-invalid-inventory'}])
        self.assertNotIn('untrusted parser detail', json.dumps(report))

    def test_oci_multiple_manifests_require_explicit_choice(self):
        layout = self.work / 'images'
        digest = make_layout(layout, ambiguous=True)
        self.assertEqual(self.parse(self.collect('--oci-layout', layout))['distribution_outcome'], 'partial')
        self.parse(self.collect('--oci-layout', layout, '--oci-manifest', digest, revision=3))
        report, _ = self.evidence()
        last = [e for e in report['evidence'] if e['category'] == 'distribution-inspection'][-1]
        self.assertEqual(last['payload']['exchange']['result']['selected_manifest'], digest)

    def test_missing_invalid_and_symlink_inputs_are_explicit(self):
        bad = self.work / 'bad.tar'
        bad.write_bytes(b'not a tar')
        self.assertNotEqual(self.parse(self.collect('--distribution', bad,
            '--distribution-format', 'tar'))['distribution_outcome'], 'complete')
        link = self.work / 'link.tar'
        link.symlink_to(bad)
        self.assertEqual(self.collect('--distribution', link, '--distribution-format', 'tar', revision=3).returncode, 2)
        report = self.parse(self.run_cli('report', '--run-dir', self.run_dir))
        self.assertEqual(report['revision'], 3)

    def test_invalid_flags_and_stale_revision_do_not_inspect(self):
        missing = self.work / 'absent'
        for flags in (('--distribution', missing), ('--distribution-format', 'tar'),
                      ('--oci-manifest', 'sha256:' + '0' * 64),
                      ('--distribution', missing, '--distribution-format', 'tar', '--oci-layout', missing)):
            with self.subTest(flags=flags):
                self.assertEqual(self.collect(*flags).returncode, 2)
        stale = self.collect('--distribution', missing, '--distribution-format', 'tar', revision=1)
        self.assertEqual(stale.returncode, 3)
        self.assertFalse((self.state / 'archive-scratch').exists())

    def test_foreign_component_is_not_associated(self):
        source = self.work / 'empty.tar'
        source.write_bytes(distribution_bytes())
        # A valid unmatched selection must not be silently retargeted.
        other = make_project(self.work / 'other', deps=(('org.example', 'other', '1'),))
        _, self.run_dir, _ = self.selected_run(other, 'org.example:other:1')
        self.parse(self.collect('--distribution', source, '--distribution-format', 'tar'))
        _, e = self.evidence()
        self.assertEqual(e['payload']['associations'][0]['candidate_ids'], [])
        self.assertEqual(e['payload']['association_status'], 'partial')

    def in_process(self, *args):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(['collect', '--run-dir', self.run_dir, '--expect-revision', '2',
                             *map(str, args)])
        return cli, code, json.loads(output.getvalue())

    def test_worker_timeout_retains_cleanup_and_never_retries(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        source = self.work / 'bundle.tar'
        source.write_bytes(distribution_bytes())
        calls = []
        def timed_out(fd, **kwargs):
            calls.append(kwargs)
            return dict(outcome='failed', result=None, error='deadline-exceeded',
                        cleanup={'leftovers': [{'path': kwargs['scratch_base'], 'reason': 'recovery'}],
                                 'individual_paths_complete': False},
                        cleanup_result={'worker_stopped': False, 'leader_reaped': False, 'status': 'unconfirmed'})
        with mock.patch.object(cli, 'inspect_distribution_isolated', side_effect=timed_out):
            _, code, result = self.in_process('--distribution', source, '--distribution-format', 'tar')
        self.assertEqual(code, 0)
        self.assertEqual(result['distribution_outcome'], 'partial')
        self.assertEqual(len(calls), 1)
        self.assertTrue(Path(calls[0]['scratch_base']).is_dir())
        _, evidence = self.evidence()
        self.assertTrue(evidence['payload']['cleanup']['leftovers'])

    def test_source_mutation_after_hash_does_not_claim_complete_identity(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        original = cli.inspect_distribution_isolated
        source = self.work / 'bundle.tar'
        source.write_bytes(distribution_bytes())
        def mutate(fd, **kwargs):
            with source.open('ab') as stream:
                stream.write(b'changed')
            return original(fd, **kwargs)
        with mock.patch.object(cli, 'inspect_distribution_isolated', side_effect=mutate):
            _, code, result = self.in_process('--distribution', source, '--distribution-format', 'tar')
        self.assertEqual(code, 0)
        self.assertEqual(result['distribution_outcome'], 'partial')
        _, evidence = self.evidence()
        self.assertIsNone(evidence['payload']['exchange']['result'])
        self.assertEqual(evidence['payload']['associations'], [])

    def test_selected_unknown_realm_remains_unknown(self):
        source = self.work / 'bundle.tar'
        source.write_bytes(distribution_bytes())
        _, self.run_dir, selected = self.selected_run(self.project, 'org.example:c-lib:2.0.0')
        self.parse(self.collect('--distribution', source, '--distribution-format', 'tar'))
        report, _ = self.evidence()
        self.assertEqual(next(s for s in report['subjects'] if s['id'] == selected)['realm'], 'unknown')

    def test_state_inside_checkout_refused_before_worker(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        # Simulate resumed context changed by a caller: no scratch may be made there.
        with mock.patch.object(cli, '_find_context', return_value={
                'payload': {'repo': str(self.state), 'query': {'raw': 'b-lib'}}}), \
                mock.patch.object(cli, 'inspect_distribution_isolated') as worker:
            _, code, _ = self.in_process('--distribution', self.work / 'unused.tar', '--distribution-format', 'tar')
        self.assertEqual(code, 2)
        worker.assert_not_called()
        self.assertFalse((self.state / 'archive-scratch').exists())

    def test_publication_failure_still_returns_cleanup_obligations(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        source = self.work / 'bundle.tar'
        source.write_bytes(distribution_bytes())
        def incomplete(fd, **kwargs):
            return dict(outcome='failed', result=None, error='deadline-exceeded',
                cleanup={'leftovers': [{'path': kwargs['scratch_base'], 'reason': 'recovery'}],
                         'individual_paths_complete': False},
                cleanup_result={'worker_stopped': False, 'leader_reaped': False, 'status': 'unconfirmed'})
        with mock.patch.object(cli, 'inspect_distribution_isolated', side_effect=incomplete), \
                mock.patch.object(cli.RunStore, 'commit', side_effect=cli.RunConflict('conflict')):
            _, code, result = self.in_process('--distribution', source, '--distribution-format', 'tar')
        self.assertEqual(code, 3)
        self.assertTrue(result['cleanup']['leftovers'])
        self.assertTrue(Path(result['cleanup']['leftovers'][0]['path']).is_dir())

    def test_distribution_input_is_not_opened_when_revision_is_stale(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        missing = self.work / 'not-there.tar'
        output = io.StringIO()
        with mock.patch.object(cli, 'inspect_distribution_isolated') as worker, contextlib.redirect_stdout(output):
            code = cli.main(['collect', '--run-dir', self.run_dir, '--expect-revision', '1',
                             '--distribution', str(missing), '--distribution-format', 'tar'])
        self.assertEqual(code, 3)
        worker.assert_not_called()

    def test_worker_interrupt_keeps_exact_exception_and_recovery_path(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        source = self.work / 'bundle.tar'
        source.write_bytes(distribution_bytes())
        interrupt = KeyboardInterrupt()
        with mock.patch.object(cli, 'inspect_distribution_isolated', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.in_process('--distribution', source, '--distribution-format', 'tar')
        self.assertIs(caught.exception, interrupt)
        self.assertTrue(interrupt.collection_cleanup['leftovers'])
        self.assertTrue(Path(interrupt.collection_cleanup['recovery_root']).is_dir())
        report = self.parse(self.run_cli('report', '--run-dir', self.run_dir))
        self.assertEqual(report['revision'], 2)

    def test_cleanup_interrupt_preserves_worker_interrupt_after_actual_removal(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        source = self.work / 'bundle.tar'
        source.write_bytes(distribution_bytes())
        primary, secondary = KeyboardInterrupt('worker'), KeyboardInterrupt('cleanup')
        primary.distribution_cleanup = {'leftovers': [], 'individual_paths_complete': True,
            'worker': {'worker_stopped': True, 'leader_reaped': True, 'status': 'confirmed'}}
        original_rmdir = os.rmdir
        removals = []
        def remove_then_interrupt(path, *args, **kwargs):
            original_rmdir(path, *args, **kwargs)
            if str(path).startswith('mdi-spool-'):
                removals.append(path)
                raise secondary
        before = set(os.listdir('/proc/self/fd')) if Path('/proc/self/fd').exists() else None
        with mock.patch.object(cli, 'inspect_distribution_isolated', side_effect=primary), \
                mock.patch.object(cli.os, 'rmdir', side_effect=remove_then_interrupt):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.in_process('--distribution', source, '--distribution-format', 'tar')
        self.assertIs(caught.exception, primary)
        self.assertEqual(len(removals), 1)
        self.assertTrue(primary.collection_cleanup['leftovers'])
        self.assertFalse(primary.collection_cleanup['individual_paths_complete'])
        self.assertTrue(any('cleanup' in note for note in primary.__notes__))
        if before is not None:
            self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_standalone_cleanup_interrupt_has_recovery_metadata(self):
        scripts = str(Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts')
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from mdi import cli
        source = self.work / 'bundle.tar'
        source.write_bytes(distribution_bytes())
        interrupt = KeyboardInterrupt('cleanup only')
        original_rmdir = os.rmdir
        removals = []
        def remove_then_interrupt(path, *args, **kwargs):
            original_rmdir(path, *args, **kwargs)
            if str(path).startswith('mdi-spool-'):
                removals.append(path)
                raise interrupt
        exchange = dict(outcome='failed', result=None, error='worker-failed',
            cleanup={'leftovers': [], 'individual_paths_complete': True},
            cleanup_result={'worker_stopped': True, 'leader_reaped': True, 'status': 'confirmed'})
        before = set(os.listdir('/proc/self/fd')) if Path('/proc/self/fd').exists() else None
        with mock.patch.object(cli, 'inspect_distribution_isolated', return_value=exchange), \
                mock.patch.object(cli.os, 'rmdir', side_effect=remove_then_interrupt):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.in_process('--distribution', source, '--distribution-format', 'tar')
        self.assertIs(caught.exception, interrupt)
        self.assertEqual(len(removals), 1)
        self.assertTrue(interrupt.collection_cleanup['leftovers'])
        self.assertFalse(interrupt.collection_cleanup['individual_paths_complete'])
        if before is not None:
            self.assertEqual(set(os.listdir('/proc/self/fd')), before)

    def test_retained_or_foreign_scratch_is_not_deleted(self):
        scratch = self.state / 'archive-scratch'
        scratch.mkdir(mode=0o700)
        sentinel = scratch / 'unrelated'
        sentinel.write_bytes(b'keep')
        source = self.work / 'bundle.tar'
        source.write_bytes(distribution_bytes())
        self.assertEqual(self.parse(self.collect('--distribution', source,
            '--distribution-format', 'tar'))['distribution_outcome'], 'partial')
        self.assertEqual(sentinel.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()

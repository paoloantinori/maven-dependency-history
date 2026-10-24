"""Shaded correspondence through the copied skill and real archive worker."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests.test_cli import CliTestCase, _jar_bytes
from tests import child_env


class ShadedCliTests(CliTestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.tmp))
        self.addCleanup(shutil.rmtree, self.work)
        self.project = self.work / 'project'
        self.project.mkdir()
        (self.project / 'pom.xml').write_text('<project><groupId>example</groupId><artifactId>app</artifactId><version>1</version></project>')
        (self.project / 'target').mkdir()
        self.metadata = {'META-INF/maven/example/lib/pom.properties': b'groupId=example\nartifactId=lib\nversion=1\n'}
        self.contents = dict(self.metadata, **{'example/C.class': b'fixture class bytes'})
        self.reference = self.work / 'reference.jar'
        self.reference_bytes = _jar_bytes(self.contents)
        self.reference.write_bytes(self.reference_bytes)
        self.artifact = self.project / 'target' / 'app.jar'
        self.artifact.write_bytes(_jar_bytes(self.contents))

    def invoke(self, *args, expected=0):
        result = subprocess.run([*self.inv, *map(str, args)], capture_output=True, text=True,
                                env=child_env({'PYTHONDONTWRITEBYTECODE': '1'}), timeout=45)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def discover(self, query='example:lib'):
        result = self.invoke('discover', '--repo', self.project, '--query', query,
                             '--state-dir', self.work / 'state')
        self.run_dir = result['run_dir']
        selected = self.invoke('select', '--run-dir', self.run_dir, '--expect-revision', result['revision'], '--all')
        self.revision = selected['revision']
        return result

    def collect(self, *extra, expected=0):
        result = self.invoke('collect', '--run-dir', self.run_dir, '--expect-revision', self.revision,
                             '--reference-jar', self.reference, *extra, expected=expected)
        if expected == 0:
            self.revision = result['revision']
        return result

    def report(self):
        return self.invoke('report', '--run-dir', self.run_dir)

    def matches(self):
        return [e for e in self.report()['evidence'] if e['category'] == 'shaded-entry-correspondence']

    def test_complete_reference_outside_checkout_and_recollect(self):
        self.discover()
        result = self.collect()
        self.assertEqual(result['shaded_outcome'], 'partial')
        evidence, = self.matches()
        match = evidence['payload']['comparison']
        self.assertEqual(match['verdict'], 'complete-entry-content-correspondence')
        self.assertFalse(match['artifact_byte_identity'])
        self.assertFalse(match['relocation_proven'])
        self.assertEqual(evidence['payload']['attribution']['packaging_configuration']['status'], 'unresolved')
        self.assertTrue(evidence['payload']['inspection_evidence_ids'])
        before = self.report()
        self.collect()
        after = self.report()
        self.assertEqual(after['evidence'][:len(before['evidence'])], before['evidence'])
        self.assertEqual(len(self.matches()), 2)
        self.assertTrue(self.invoke('report', '--run-dir', self.run_dir, '--validate')['validation']['valid'])
        self.assertEqual(self.reference.read_bytes(), self.reference_bytes)

    def test_partial_and_conflicting_bytes_are_not_complete(self):
        self.artifact.write_bytes(_jar_bytes(dict(self.metadata, **{'example/C.class': b'different'})))
        self.discover()
        self.assertEqual(self.collect()['shaded_outcome'], 'partial')
        self.assertEqual(self.matches()[0]['payload']['comparison']['coverage']['conflicts'], 1)

    def test_nested_selected_copy_not_outer_container(self):
        self.artifact.write_bytes(_jar_bytes({'BOOT-INF/lib/lib.jar': _jar_bytes(self.contents),
                                             'unrelated.txt': b'outer-only'}))
        self.discover()
        self.collect()
        match = self.matches()[0]['payload']['comparison']
        self.assertEqual(match['container']['entry_chain'], [0])
        self.assertEqual(match['coverage']['container_entries'], 2)

    def test_duplicate_nested_copies_remain_separate(self):
        self.artifact.write_bytes(_jar_bytes({'a/lib.jar': _jar_bytes(self.contents),
                                             'b/lib.jar': _jar_bytes(self.contents)}))
        self.discover()
        self.collect()
        matches = self.matches()
        self.assertEqual(len(matches), 2)
        self.assertEqual({tuple(e['payload']['comparison']['container']['entry_chain']) for e in matches}, {(0,), (1,)})

    def test_changed_selected_copy_is_not_silently_reassociated(self):
        self.discover()
        self.artifact.write_bytes(_jar_bytes(dict(self.metadata, **{'example/C.class': b'changed'})))
        self.assertEqual(self.collect()['shaded_outcome'], 'partial')
        self.assertEqual(self.matches(), [])
        stage = next(s for s in self.report()['stages'] if s['name'] == 'shaded')
        self.assertIn('selected-copy-changed', ' '.join(stage['reasons']))

    def test_declaration_only_has_no_packaged_target(self):
        (self.project / 'pom.xml').write_text('<project><dependencies><dependency><groupId>example</groupId><artifactId>declared</artifactId><version>1</version></dependency></dependencies></project>')
        self.discover('example:declared')
        self.assertEqual(self.collect()['shaded_outcome'], 'not_applicable')
        self.assertEqual(self.matches(), [])

    def test_stale_revision_does_not_inspect_reference(self):
        self.discover()
        self.reference.unlink()
        self.revision -= 1
        self.collect(expected=3)

    def test_reference_symlink_refused_as_partial_without_following(self):
        original = self.reference.with_name('actual.jar')
        self.reference.rename(original)
        self.reference.symlink_to(original)
        self.discover()
        self.assertEqual(self.collect()['shaded_outcome'], 'partial')
        self.assertEqual(self.matches(), [])

    def test_matching_graph_is_only_a_qualified_candidate_lead(self):
        from tests.test_maven import tree_provenance
        self.discover()
        tree = self.work / 'tree.json'
        node = {'groupId': 'example', 'artifactId': 'lib', 'version': '1',
                'type': 'jar', 'scope': 'compile', 'classifier': '',
                'optional': 'false', 'children': []}
        tree.write_text(json.dumps(node))
        provenance = tree_provenance()
        provenance['module'] = '.'
        prov = self.work / 'provenance.json'
        prov.write_text(json.dumps(provenance))
        imported = self.invoke('import', '--run-dir', self.run_dir, '--expect-revision', self.revision,
                               '--report', tree, '--kind', 'tree-json', '--tree-mode', 'nonverbose',
                               '--realm', 'project', '--provenance', prov)
        self.revision = imported['revision']
        self.assertEqual(self.collect()['shaded_outcome'], 'partial')
        evidence, = self.matches()
        self.assertEqual(evidence['payload']['attribution']['inclusion_graph']['status'], 'candidate-only')
        self.assertIn(imported['evidence_id'], evidence['payload']['source_evidence_ids'])
        original = [q for q in self.report()['qualifications']
                    if imported['evidence_id'] in q['affected_evidence_ids']]
        derived = [q for q in self.report()['qualifications']
                   if evidence['id'] in q['affected_evidence_ids']]
        self.assertTrue(original)
        self.assertTrue(all(any(q['reason'] == parent['reason'] and q['kind'] == parent['kind']
                                for q in derived) for parent in original))

    def test_missing_reference_entries_are_partial_not_absent_dependency(self):
        self.artifact.write_bytes(_jar_bytes(self.metadata))
        self.discover()
        self.assertEqual(self.collect()['shaded_outcome'], 'partial')
        comparison = self.matches()[0]['payload']['comparison']
        self.assertEqual(comparison['coverage']['missing'], 1)
        self.assertFalse(comparison['artifact_byte_identity'])

    def test_duplicate_entry_names_accounted_by_index(self):
        import io
        import warnings
        import zipfile
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(buffer, 'w') as archive:
                for name, data in self.contents.items():
                    archive.writestr(name, data)
                archive.writestr('example/C.class', b'fixture class bytes')
        self.artifact.write_bytes(buffer.getvalue())
        self.reference.write_bytes(buffer.getvalue())
        self.discover()
        self.collect()
        rows = self.matches()[0]['payload']['comparison']['matches']
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r['container']['entry_index'] for r in rows}), 3)

    def shade_pom(self, configuration, *, management=False, profile=False):
        plugin = ('<plugin><groupId>org.apache.maven.plugins</groupId>'
                  '<artifactId>maven-shade-plugin</artifactId><version>3</version>'
                  '<configuration>' + configuration + '</configuration></plugin>')
        plugins = '<plugins>' + plugin + '</plugins>'
        build = '<build>' + ('<pluginManagement>' + plugins + '</pluginManagement>'
                             if management else plugins) + '</build>'
        if profile:
            build = '<profiles><profile><id>optional-shade</id>' + build + '</profile></profiles>'
        return '<project>' + build + '</project>'

    def test_raw_config_links_changed_class_paths_without_relocation_proof(self):
        config = ('<relocations><relocation><pattern>example</pattern>'
                  '<shadedPattern>moved.example</shadedPattern></relocation></relocations>')
        (self.project / 'pom.xml').write_text(self.shade_pom(config))
        self.artifact.write_bytes(_jar_bytes(dict(self.metadata, **{'moved/example/C.class': b'rewritten bytes'})))
        self.discover()
        self.collect()
        evidence, = self.matches()
        attribution = evidence['payload']['attribution']['packaging_configuration']
        self.assertEqual(attribution['status'], 'candidate-only')
        configs = [e for e in self.report()['evidence'] if e['category'] == 'observed-shade-configuration']
        observed, = configs
        self.assertIn(observed['id'], evidence['payload']['source_evidence_ids'])
        self.assertEqual(observed['captured_identity']['sha256'], observed['payload']['observation']['capture']['sha256'])
        relocation = observed['payload']['observation']['configurations'][0]['relocations'][0]
        self.assertEqual(relocation['path_candidates'][0]['original'], 'example/C.class')
        self.assertEqual(relocation['path_candidates'][0]['packaged'], 'moved/example/C.class')
        self.assertFalse(observed['payload']['observation']['relocation_proven'])
        self.assertFalse(evidence['payload']['comparison']['artifact_byte_identity'])
        self.assertFalse(evidence['payload']['comparison']['relocation_proven'])
        self.assertTrue(attribution['enclosing_build_verified'] is False)
        qualifiers = self.report()['qualifications']
        self.assertTrue(any(q['kind'] == 'unverified-shade-configuration'
                            and evidence['id'] in q['affected_evidence_ids'] for q in qualifiers))

    def test_config_qualification_enforced_on_derived_only_analysis(self):
        import copy
        from tests.test_report import ReportError, validate_report
        config = '<relocations><relocation><pattern>example</pattern><shadedPattern>moved</shadedPattern></relocation></relocations>'
        (self.project / 'pom.xml').write_text(self.shade_pom(config))
        self.discover()
        self.collect()
        report = self.report()
        comparison = next(e for e in report['evidence'] if e['category'] == 'shaded-entry-correspondence')
        draft = copy.deepcopy(report)
        draft['analyses'].append({'id': 'analysis-config', 'statement_kind': 'inference',
                                  'evidence_ids': [comparison['id']], 'statement': 'candidate only',
                                  'limitations': ['generic incomplete context']})
        with self.assertRaises(ReportError):
            validate_report(draft)
        from mdi.report import _qualification_text
        draft['analyses'][0]['limitations'].extend(
            _qualification_text(q) for q in draft['qualifications']
            if comparison['id'] in q['affected_evidence_ids'])
        validate_report(draft)

    def test_config_links_preserve_literal_bangs_and_duplicate_indices(self):
        import io
        import warnings
        import zipfile
        config = '<relocations><relocation><pattern>example</pattern><shadedPattern>moved</shadedPattern></relocation></relocations>'
        (self.project / 'pom.xml').write_text(self.shade_pom(config))
        self.reference.write_bytes(_jar_bytes({'example/C!other.class': b'original'}))
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(buffer, 'w') as archive:
                for name, data in self.metadata.items():
                    archive.writestr(name, data)
                archive.writestr('moved/C!other.class', b'rewritten-1')
                archive.writestr('moved/C!other.class', b'rewritten-2')
        self.artifact.write_bytes(buffer.getvalue())
        self.discover()
        self.collect()
        config_record = next(e for e in self.report()['evidence'] if e['category'] == 'observed-shade-configuration')
        pairs = config_record['payload']['path_pair_observations']
        self.assertEqual({p['packaged']['entry_index'] for p in pairs}, {1, 2})
        self.assertTrue(all(p['original']['entry_name'] == 'example/C!other.class' for p in pairs))
        links = config_record['payload']['observation']['configurations'][0]['relocations'][0]['path_candidates']
        self.assertEqual(len(links), 2)
        self.assertTrue(all(p['packaged'] == 'moved/C!other.class' for p in links))
        self.assertTrue(all(p['relocation_proven'] is False for p in links))

    def test_management_profile_and_placeholders_remain_raw_unexecuted(self):
        config = ('<relocations><relocation><pattern>${original}</pattern>'
                  '<shadedPattern>moved</shadedPattern></relocation></relocations>')
        (self.project / 'pom.xml').write_text(self.shade_pom(config, management=True, profile=True))
        self.discover()
        self.collect()
        observed = next(e for e in self.report()['evidence'] if e['category'] == 'observed-shade-configuration')
        row, = observed['payload']['observation']['configurations']
        self.assertTrue(row['management_only'])
        self.assertEqual(row['profile']['activation'], 'unknown')
        self.assertFalse(row['execution_proven'])
        self.assertEqual(row['relocations'][0]['pattern'], '${original}')
        self.assertEqual(row['relocations'][0]['path_candidates'], [])

    def test_execution_configuration_and_filters_preserve_raw_scope(self):
        pom = ('<project><build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>'
               '<artifactId>maven-shade-plugin</artifactId><executions><execution><id>bundle</id>'
               '<phase>package</phase><goals><goal>shade</goal></goals><configuration>'
               '<artifactSet><includes><include>example:lib</include></includes></artifactSet>'
               '<filters><filter><artifact>example:lib</artifact><excludes>'
               '<exclude>META-INF/*.SF</exclude></excludes></filter></filters>'
               '</configuration></execution></executions></plugin></plugins></build></project>')
        (self.project / 'pom.xml').write_text(pom)
        self.discover()
        self.collect()
        observed = next(e for e in self.report()['evidence'] if e['category'] == 'observed-shade-configuration')
        row, = observed['payload']['observation']['configurations']
        self.assertEqual(row['execution']['id'], 'bundle')
        self.assertEqual(row['execution']['phase'], 'package')
        self.assertFalse(row['execution_proven'])
        self.assertEqual([v['value'] for v in row['artifact_sets'][0]['includes']], ['example:lib'])
        self.assertEqual([v['value'] for v in row['filters'][0]['excludes']], ['META-INF/*.SF'])

    def test_unrelated_plugin_does_not_supply_shading_identity(self):
        (self.project / 'pom.xml').write_text(self.shade_pom('<relocations/>').replace(
            'org.apache.maven.plugins', 'unrelated.vendor'))
        self.discover()
        self.collect()
        config = self.matches()[0]['payload']['attribution']['packaging_configuration']
        self.assertEqual(config['status'], 'unresolved')
        observed = next(e for e in self.report()['evidence'] if e['category'] == 'observed-shade-configuration')
        self.assertEqual(observed['payload']['observation']['configurations'], [])

    def test_malformed_or_missing_pom_preserves_comparison_as_partial(self):
        self.discover()
        (self.project / 'pom.xml').write_text('<project><broken>')
        self.assertEqual(self.collect()['shaded_outcome'], 'partial')
        evidence = self.matches()[-1]
        self.assertEqual(evidence['payload']['attribution']['packaging_configuration']['status'], 'unresolved')
        self.assertEqual(evidence['payload']['comparison']['verdict'], 'complete-entry-content-correspondence')
        (self.project / 'pom.xml').unlink()
        self.assertEqual(self.collect()['shaded_outcome'], 'partial')
        self.assertTrue(self.invoke('report', '--run-dir', self.run_dir, '--validate')['validation']['valid'])

    def test_module_pom_does_not_use_unrelated_root_shade_config(self):
        (self.project / 'pom.xml').write_text(self.shade_pom('<relocations/>').replace(
            '</project>', '<modules><module>child</module></modules></project>'))
        child = self.project / 'child'
        child.mkdir()
        (child / 'pom.xml').write_text('<project/>')
        (child / 'target').mkdir()
        self.artifact.rename(child / 'target' / 'app.jar')
        self.discover()
        self.collect()
        evidence, = self.matches()
        self.assertEqual(evidence['payload']['attribution']['packaging_configuration']['status'], 'unresolved')
        configs = [e for e in self.report()['evidence'] if e['category'] == 'observed-shade-configuration']
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]['source_locator'], str(child / 'pom.xml'))
        self.assertEqual(configs[0]['payload']['observation']['configurations'], [])

    def test_symlink_module_pom_never_followed(self):
        self.discover()
        outside = self.work / 'outside.xml'
        outside.write_text(self.shade_pom('<relocations/>'))
        (self.project / 'pom.xml').unlink()
        (self.project / 'pom.xml').symlink_to(outside)
        self.assertEqual(self.collect()['shaded_outcome'], 'partial')
        self.assertFalse(any((e['payload'].get('observation') or {}).get('configurations')
                             for e in self.report()['evidence'] if e['category'] == 'observed-shade-configuration'))

    def test_reference_count_is_bounded_before_collection(self):
        self.discover()
        before = self.report()
        self.collect(*sum((['--reference-jar', str(self.reference)] for _ in range(8)), []), expected=2)
        self.assertEqual(self.report(), before)

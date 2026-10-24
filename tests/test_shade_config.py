"""Pure captured-POM shading configuration observations."""
import copy
import hashlib
import json
import sys
import tracemalloc
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import shade_config
from mdi.shade_config import ConfigError, ConfigLimits, inspect_shade_config


CONFIG = '''<configuration><relocations><relocation><pattern>a.b</pattern>
<shadedPattern>x.y</shadedPattern><includes><include>a.b.*</include></includes>
<excludes><exclude>a.b.Keep</exclude></excludes></relocation></relocations>
<artifactSet><includes><include>g:a</include></includes><excludes><exclude>g:b</exclude></excludes></artifactSet>
<filters><filter><artifact>*:*</artifact><includes><include>**/*.class</include></includes>
<excludes><exclude>META-INF/*.SF</exclude></excludes></filter></filters></configuration>'''


def plugin(config=CONFIG, group='org.apache.maven.plugins', extra=''):
    group_xml = '' if group is None else f'<groupId>{group}</groupId>'
    return f'<plugin>{group_xml}<artifactId>maven-shade-plugin</artifactId><version>3.x</version>{config}{extra}</plugin>'


def pom(body, ns='http://maven.apache.org/POM/4.0.0'):
    return f'<project xmlns="{ns}">{body}</project>'.encode()


def build(p):
    return '<build><plugins>' + p + '</plugins></build>'


class ShadeConfigTests(unittest.TestCase):
    def inspect(self, data, **kw):
        return inspect_shade_config(data, source_path='/checkout/pom.xml', module='.', **kw)

    def test_default_and_empty_namespace(self):
        for ns in ('', 'http://maven.apache.org/POM/4.0.0'):
            data = pom(build(plugin()), ns)
            result = self.inspect(data)
            row = result['configurations'][0]
            self.assertEqual(result['capture']['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(row['relocations'][0]['pattern'], 'a.b')
            self.assertEqual(row['relocations'][0]['shaded_pattern'], 'x.y')
            self.assertEqual(row['relocations'][0]['includes'][0]['value'], 'a.b.*')
            self.assertEqual(row['artifact_sets'][0]['excludes'][0]['value'], 'g:b')
            self.assertEqual(row['filters'][0]['excludes'][0]['value'], 'META-INF/*.SF')
            self.assertFalse(row['execution_proven'])
            self.assertFalse(result['relocation_proven'])
            self.assertEqual(row['source']['xml_path'], 'project/build/plugins/plugin/configuration')

    def test_executions_and_plugin_configuration_stay_separate(self):
        executions = '<executions>' + ''.join('<execution><id>same</id><phase>package</phase><goals><goal>shade</goal></goals>' + CONFIG + '</execution>' for _ in range(2)) + '</executions>'
        r = self.inspect(pom(build(plugin(extra=executions))))
        rows = r['configurations']
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[0]['execution'])
        self.assertEqual(rows[1]['execution']['id'], 'same')
        self.assertEqual(rows[2]['execution']['goals'][0]['value'], 'shade')
        self.assertIn('execution[2]', rows[2]['source']['xml_path'])
        self.assertEqual(len({x['source']['xml_path'] for x in rows}), 3)

    def test_duplicate_profile_ids_have_distinct_locators(self):
        body = '<profiles>' + ''.join('<profile><id>p</id>' + build(plugin()) + '</profile>' for _ in range(2)) + '</profiles>'
        rows = self.inspect(pom(body))['configurations']
        self.assertEqual([x['profile']['id'] for x in rows], ['p', 'p'])
        self.assertEqual(rows[0]['profile']['activation'], 'unknown')
        self.assertNotEqual(rows[0]['profile']['source'], rows[1]['profile']['source'])

    def test_unique_profile_locator_matches_discovery(self):
        rows = self.inspect(pom('<profiles><profile><id>p</id>' + build(plugin()) + '</profile></profiles>'))['configurations']
        self.assertEqual(rows[0]['profile']['source'], "project/profiles/profile[@id='p']")

    def test_management_and_missing_group_are_not_execution_proof(self):
        body = '<build><pluginManagement><plugins>' + plugin(group=None) + '</plugins></pluginManagement></build>'
        r = self.inspect(pom(body))
        self.assertTrue(r['configurations'][0]['management_only'])
        self.assertIsNone(r['configurations'][0]['plugin']['groupId'])
        self.assertEqual(r['configurations'][0]['plugin_identity'], 'candidate-default-group')
        self.assertEqual(r['coverage']['status'], 'partial')

    def test_foreign_namespace_and_foreign_group_do_not_match(self):
        r = self.inspect(pom(build(plugin(group='other.vendor')) + '<foreign xmlns="urn:fake">' + build(plugin()) + '</foreign>'))
        self.assertEqual(r['configurations'], [])
        spoof = '<build xmlns="urn:fake"><plugins>' + plugin() + '</plugins></build>'
        self.assertEqual(self.inspect(pom(spoof))['configurations'], [])

    def test_invalid_explicit_group_never_becomes_default_group_candidate(self):
        for group_fields in (
            '<groupId>foreign.one</groupId><groupId>foreign.two</groupId>',
            '<groupId><nested>foreign.one</nested></groupId>',
            '<groupId/>',
        ):
            with self.subTest(group_fields=group_fields):
                p = '<plugin>' + group_fields + '<artifactId>maven-shade-plugin</artifactId>' + CONFIG + '</plugin>'
                result = self.inspect(pom(build(p)))
                self.assertEqual(result['configurations'], [])
                self.assertTrue(any(i['code'] == 'invalid-plugin-group' for i in result['issues']))
                self.assertEqual(result['coverage']['status'], 'partial')

    def test_node_budget_rejects_before_child_locator_allocations(self):
        root = ET.Element('project')
        for _ in range(100000):
            ET.SubElement(root, 'item')
        tracemalloc.start()
        try:
            with self.assertRaises(ConfigError) as caught:
                shade_config._Reader(root, '/pom.xml', ConfigLimits(max_nodes=1))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(caught.exception.code, 'node-limit')
        self.assertLess(peak, 256 * 1024)

    def test_scalar_budget_rejects_before_joining_all_fragments(self):
        root = ET.Element('project')
        scalar = ET.SubElement(root, 'id')
        scalar.text = 'x' * 1000
        for _ in range(4000):
            child = ET.Comment('c')
            child.tail = 'y' * 1000
            scalar.append(child)
        reader = shade_config._Reader(root, '/pom.xml', ConfigLimits(max_text=1024))
        tracemalloc.start()
        try:
            with self.assertRaises(ConfigError) as caught:
                reader.value(scalar)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(caught.exception.code, 'text-limit')
        self.assertLess(peak, 256 * 1024)

    def test_foreign_scalar_never_contributes_identity(self):
        p = '<plugin><artifactId><x xmlns="urn:fake">maven-shade-plugin</x></artifactId>' + CONFIG + '</plugin>'
        r = self.inspect(pom(build(p)))
        self.assertEqual(r['configurations'], [])
        self.assertTrue(any(i['code'] == 'non-scalar-field' for i in r['issues']))

    def test_partial_fields_placeholders_and_duplicate_scalars(self):
        config = '<configuration><relocations><relocation><pattern>${base}</pattern></relocation><relocation><pattern>a</pattern><pattern>b</pattern><shadedPattern>c</shadedPattern></relocation></relocations></configuration>'
        r = self.inspect(pom(build(plugin(config))))
        self.assertEqual(r['configurations'][0]['relocations'][0]['pattern'], '${base}')
        codes = {i['code'] for i in r['issues']}
        self.assertTrue({'unresolved-property', 'incomplete-relocation', 'duplicate-scalar'}.issubset(codes))
        self.assertIsNone(r['configurations'][0]['relocations'][1]['pattern'])

    def test_empty_configuration_does_not_prove_no_shading(self):
        r = self.inspect(pom(build(plugin(''))))
        self.assertEqual(r['coverage']['plugins_observed'], 1)
        self.assertEqual(r['configurations'], [])
        self.assertIn('inheritance', ' '.join(r['coverage']['limitations']))

    def test_duplicate_config_and_filter_entries_retained(self):
        r = self.inspect(pom(build(plugin(CONFIG + CONFIG))))
        self.assertEqual(len(r['configurations']), 2)
        self.assertTrue(r['configurations'][1]['source']['xml_path'].endswith('configuration[2]'))

    def test_capture_metadata_caller_attested(self):
        producer = {'tool': 'capture-client', 'version': '1'}
        r = self.inspect(pom(build(plugin())), producer=producer, observed_at='caller-time')
        before = copy.deepcopy(r)
        producer['tool'] = 'changed'
        self.assertEqual(r, before)
        self.assertEqual(r['capture']['provenance_basis'], 'caller-supplied')
        self.assertEqual(r['capture']['observed_at'], 'caller-time')

    def test_link_evidence_only_explicit_observed_path_pairs(self):
        r = self.inspect(pom(build(plugin())), observed_paths=[{'original': 'a/b/C.class', 'packaged': 'x/y/C.class'}])
        links = r['configurations'][0]['relocations'][0]['path_candidates']
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['basis'], 'literal-prefix-candidate')
        self.assertFalse(links[0]['relocation_proven'])
        self.assertFalse(r['artifact_identity_proven'])

    def test_output_json_safe_and_path_pairs_detached(self):
        paths = [{'original': 'a/b/C.class', 'packaged': 'x/y/C.class'}]
        before = copy.deepcopy(paths)
        r = self.inspect(pom(build(plugin())), observed_paths=paths)
        json.dumps(r, allow_nan=False)
        r['configurations'][0]['relocations'][0]['path_candidates'][0]['original'] = 'changed'
        self.assertEqual(paths, before)

    def test_long_constructed_locator_is_bounded(self):
        data = pom('<abcdefghij>' * 10 + '</abcdefghij>' * 10)
        with self.assertRaises(ConfigError) as caught:
            self.inspect(data, limits=ConfigLimits(max_text=100))
        self.assertEqual(caught.exception.code, 'text-limit')

    def test_path_links_not_substring_or_glob_semantics(self):
        r = self.inspect(pom(build(plugin())), observed_paths=[{'original': 'a/bad/C.class', 'packaged': 'x/y/C.class'}, {'original': 'a/b/C.class', 'packaged': 'x/y/D.class'}])
        self.assertEqual(r['configurations'][0]['relocations'][0]['path_candidates'], [])

    def test_paths_do_not_infer_missing_patterns(self):
        cfg = '<configuration><relocations><relocation><pattern>${base}</pattern><shadedPattern>x.y</shadedPattern></relocation></relocations></configuration>'
        r = self.inspect(pom(build(plugin(cfg))), observed_paths=[{'original': 'a/b/C.class', 'packaged': 'x/y/C.class'}])
        self.assertEqual(r['configurations'][0]['relocations'][0]['path_candidates'], [])

    def test_xml_rejections(self):
        for data in (b'<!DOCTYPE project [<!ENTITY x "bad">]><project/>', b'<x/>', b'<project xmlns="urn:fake"/>', b'<project>'):
            with self.subTest(data=data), self.assertRaises(ConfigError):
                self.inspect(data)

    def test_depth_rejected_by_shared_parser(self):
        with self.assertRaises(ConfigError) as caught:
            self.inspect(b'<project>' + b'<x>' * 64 + b'</x>' * 64 + b'</project>')
        self.assertEqual(caught.exception.code, 'unsafe-xml')

    def test_limits(self):
        data = pom(build(plugin()))
        for limits in (ConfigLimits(max_bytes=10), ConfigLimits(max_nodes=3), ConfigLimits(max_text=2), ConfigLimits(max_records=1)):
            with self.subTest(limits=limits), self.assertRaises(ConfigError):
                self.inspect(data, limits=limits)
        for value in (True, 0, -1, 1.5):
            with self.assertRaises(ValueError):
                ConfigLimits(max_nodes=value)

    def test_path_and_metadata_limits(self):
        data = pom(build(plugin()))
        for kwargs in ({'observed_paths': [{'original':'a','packaged':'b','extra':1}]}, {'observed_paths':[{'original':None,'packaged':'b'}]}, {'producer':{'secret':'x'}}, {'observed_paths':[{'original':'a','packaged':'b'}]*3, 'limits':ConfigLimits(max_paths=2)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError):
                self.inspect(data, **kwargs)


if __name__ == '__main__':
    unittest.main()

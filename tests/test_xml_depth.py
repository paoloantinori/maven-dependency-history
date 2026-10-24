"""Regression tests for the proposed pre-construction XML depth ceiling.

The root is element level one. These tests intentionally precede the
production change; the over-limit cases must fail until xmlsafe enforces
its hard ceiling in the Expat safety pass, before constructing an ET tree.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi import discover, history, maven, xmlsafe
from mdi.limits import Limits
from tests.history_fixtures import STEPS, dependency_history_fixture
from tests.test_maven import EFFECTIVE_POM, pom_provenance


CEILING = 64
PLUGIN_CONFIGURATION = b'''<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion><groupId>example</groupId>
  <artifactId>configured</artifactId><version>1</version>
  <profiles><profile><id>integration</id><build><plugins><plugin>
    <groupId>example</groupId><artifactId>plugin</artifactId><version>1</version>
    <configuration><rules><rule><includes><include>example.*</include></includes>
    </rule></rules></configuration>
  </plugin></plugins></build></profile></profiles>
</project>'''


def nested_document(depth, encoding="utf-8", decorations=False):
    declaration = '<?xml version="1.0" encoding="' + encoding + '"?>'
    prefix = '<project xmlns="http://maven.apache.org/POM/4.0.0">'
    marker = '<!-- not an element --><?probe value?>' if decorations else ''
    text = declaration + prefix + marker + ('<node>' + marker) * (depth - 1)
    text += 'value' + '</node>' * (depth - 1) + '</project>'
    return text.encode(encoding)


def element_depth(root):
    stack = [(root, 1)]
    deepest = 0
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        stack.extend((child, depth + 1) for child in node if isinstance(child.tag, str))
    return deepest


def compatibility_corpus():
    documents = [EFFECTIVE_POM, PLUGIN_CONFIGURATION]
    documents.extend(
        text.encode("utf-8")
        for _key, _subject, _date, updates in STEPS
        for path, text in updates.items()
        if path.endswith("pom.xml")
    )
    return list(dict.fromkeys(documents))


class XmlDepthBoundaryTests(unittest.TestCase):
    def test_level_65_refused_before_elementtree_construction(self):
        for encoding in ("utf-8", "utf-16", "utf-16le", "utf-16be"):
            with self.subTest(encoding=encoding):
                with mock.patch.object(
                    xmlsafe.ET, "XMLParser",
                    side_effect=AssertionError("ElementTree construction started before depth rejection"),
                ) as builder:
                    with self.assertRaises(xmlsafe.UnsafeXML):
                        xmlsafe.parse_xml(nested_document(CEILING + 1, encoding))
                    builder.assert_not_called()

    def test_byte_limit_cannot_relax_depth_ceiling(self):
        data = nested_document(CEILING + 1)
        with self.assertRaises(xmlsafe.UnsafeXML):
            xmlsafe.parse_xml(data, limits=Limits(xml_bytes=len(data) + 1024))

    def test_exactly_64_levels_accepts_all_supported_unicode_encodings(self):
        for encoding in ("utf-8", "utf-16", "utf-16le", "utf-16be"):
            with self.subTest(encoding=encoding):
                root = xmlsafe.parse_xml(nested_document(CEILING, encoding))
                self.assertEqual(element_depth(root), CEILING)
                self.assertEqual(''.join(root.itertext()), 'value')

    def test_comments_pis_and_xml_declaration_do_not_consume_element_depth(self):
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding):
                root = xmlsafe.parse_xml(nested_document(CEILING, encoding, decorations=True))
                self.assertEqual(element_depth(root), CEILING)
                self.assertTrue(any(node.tag is ET.Comment for node in root.iter()))

    def test_wide_siblings_reset_depth_instead_of_accumulating_it(self):
        data = b'<project>' + b'<child><leaf/></child>' * 256 + b'</project>'
        root = xmlsafe.parse_xml(data)
        self.assertEqual(element_depth(root), 3)
        self.assertEqual(len(root), 256)


class XmlDepthCompatibilityTests(unittest.TestCase):
    def test_existing_pom_and_profile_plugin_corpus_stays_within_ceiling(self):
        for index, data in enumerate(compatibility_corpus()):
            with self.subTest(document=index):
                root = xmlsafe.parse_xml(data)
                self.assertLessEqual(element_depth(root), CEILING)

    def test_corpus_through_discovery_and_effective_pom_import(self):
        for index, data in enumerate(compatibility_corpus()):
            with self.subTest(document=index), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, 'pom.xml').write_bytes(data)
                with mock.patch.object(discover, 'parse_xml', wraps=xmlsafe.parse_xml) as parser:
                    discovered = discover.discover_declarations(tmp)
                parser.assert_called()
                self.assertFalse(
                    any('xml' in issue['code'] or 'malformed' in issue['code']
                        for issue in discovered['issues']), discovered['issues'],
                )
                with mock.patch.object(maven, 'parse_xml', wraps=xmlsafe.parse_xml) as parser:
                    imported = maven.import_effective_pom(data, provenance=pom_provenance())
                parser.assert_called_once()
                self.assertEqual(imported['kind'], 'effective-pom')

    def test_existing_git_history_poms_use_shared_parser_within_ceiling(self):
        depths = []

        def parse_and_measure(data, limits=Limits()):
            root = xmlsafe.parse_xml(data, limits=limits)
            depths.append(element_depth(root))
            self.assertLessEqual(depths[-1], CEILING)
            return root

        with dependency_history_fixture() as fixture:
            with mock.patch.object(history, 'parse_xml', side_effect=parse_and_measure):
                result = history.inspect_history(
                    fixture.repo_dir, 'com.example:lib-b',
                    pom_paths=['pom.xml', 'app/pom.xml'],
                )
        self.assertTrue(depths, 'history must actually parse historical POM blobs')
        self.assertEqual(result['coverage']['status'], 'complete', result)
        self.assertTrue(result['events'])


if __name__ == '__main__':
    unittest.main()

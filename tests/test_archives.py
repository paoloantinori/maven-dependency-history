"""Tests for mdi.archives bounded inspection of one JAR/WAR and nested JARs."""

import contextlib
import hashlib
import io
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.archives import ArchiveLimits, inspect_archive
from mdi.capture import CaptureError
import mdi.archives as archives_module

PREVIEW_CAP = archives_module._RAW_EVIDENCE_CAP


def build_zip(items, compression=zipfile.ZIP_STORED):
    """Zip bytes from (name-or-ZipInfo, data) pairs; fixtures only."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in items:
            zf.writestr(name, data)
    return buf.getvalue()


def pom_properties(group, artifact, version):
    return f"groupId={group}\nartifactId={artifact}\nversion={version}\n".encode("ascii")


def metadata_with_tail(tail, head, tail_start):
    """pom.properties bytes where the comment filler ends at `tail_start`,
    so tail content sits past (or straddling) the preview boundary."""
    filler_len = tail_start - len(head)
    assert filler_len >= 2, "head already reaches the wanted tail start"
    filler = b"#" + b"a" * (filler_len - 2) + b"\n"
    assert len(head) + len(filler) == tail_start
    return head + filler + tail


def jar_with_pom(group, artifact, version, extra=()):
    items = [("com/example/Thing.class", b"\xca\xfe\xba\xbe" + bytes(range(64)))]
    items.extend(extra)
    items.append((f"META-INF/maven/{group}/{artifact}/pom.properties",
                  pom_properties(group, artifact, version)))
    return build_zip(items, zipfile.ZIP_DEFLATED)


def nul_name_info(name_with_nul):
    zi = zipfile.ZipInfo("pad.txt")
    zi.filename = name_with_nul
    return zi


def eocd_fields(data):
    sig, disk, cddisk, nthis, ntot, size_cd, off_cd, clen = struct.unpack("<4s4H2LH", data[-22:])
    assert sig == b"PK\x05\x06" and clen == 0, "fixture needs a comment-free EOCD"
    return {"disk": disk, "cd_disk": cddisk, "count_this": nthis, "count_total": ntot,
            "size_cd": size_cd, "offset_cd": off_cd}


def with_eocd(data, **fields):
    mut = bytearray(data)
    base = len(mut) - 22
    for key, value in fields.items():
        if key in ("size_cd", "offset_cd"):
            struct.pack_into("<L", mut, base + {  # noqa: E501
                "size_cd": 12, "offset_cd": 16}[key], value)
        else:
            struct.pack_into("<H", mut, base + {
                "disk": 4, "cd_disk": 6, "count_this": 8, "count_total": 10}[key], value)
    return bytes(mut)


def cd_record_offset(data, name):
    fields = eocd_fields(data)
    pos = fields["offset_cd"]
    for _ in range(fields["count_total"]):
        fnlen, = struct.unpack_from("<H", data, pos + 28)
        if data[pos + 46:pos + 46 + fnlen].decode("ascii") == name:
            return pos
        extra_len, comment_len = struct.unpack_from("<HH", data, pos + 30)
        pos += 46 + fnlen + extra_len + comment_len
    raise LookupError(name)


def with_cd_usize(data, name, value):
    mut = bytearray(data)
    struct.pack_into("<L", mut, cd_record_offset(data, name) + 24, value)
    return bytes(mut)


def with_cd_flags(data, name, mask):
    mut = bytearray(data)
    pos = cd_record_offset(data, name)
    flags, = struct.unpack_from("<H", mut, pos + 8)
    struct.pack_into("<H", mut, pos + 8, flags | mask)
    return bytes(mut)


def as_zip64(data):
    fields = eocd_fields(data)
    cd_end = fields["offset_cd"] + fields["size_cd"]
    rec = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0,
                      fields["count_total"], fields["count_total"],
                      fields["size_cd"], fields["offset_cd"])
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, cd_end, 1)
    return data[:cd_end] + rec + locator + data[-22:]


def issues_by_code(result):
    return {issue["code"]: issue for issue in result["issues"]}


def entry_named(result, name):
    matches = [e for e in result["entries"] if e["name"] == name]
    assert matches, f"no entry named {name!r}"
    return matches


def archive_by_id(result, archive_id):
    matches = [a for a in result["archives"] if a["archive_id"] == archive_id]
    assert matches, f"no archive {archive_id!r}"
    return matches[0]


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, name, data):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def inspect(self, path, **limit_overrides):
        limits = ArchiveLimits(**limit_overrides) if limit_overrides else ArchiveLimits()
        return inspect_archive(path, limits=limits)


class LimitValidationTest(ArchiveTestCase):
    def test_int_fields_reject_non_positive(self):
        for field in ("max_input_bytes", "max_expanded_bytes", "max_entry_bytes",
                      "max_entries", "max_depth", "max_metadata_bytes"):
            for bad in (0, -1):
                with self.assertRaises(ValueError, msg=f"{field}={bad}"):
                    ArchiveLimits(**{field: bad})

    def test_int_fields_reject_bool_and_non_int(self):
        for field in ("max_input_bytes", "max_expanded_bytes", "max_entry_bytes",
                      "max_entries", "max_depth", "max_metadata_bytes"):
            for bad in (True, "16", 1.0, None):
                with self.assertRaises(TypeError, msg=f"{field}={bad!r}"):
                    ArchiveLimits(**{field: bad})

    def test_max_seconds_positive_number(self):
        for bad in (0, -0.5):
            with self.assertRaises(ValueError):
                ArchiveLimits(max_seconds=bad)
        for bad in (True, None, "30"):
            with self.assertRaises(TypeError):
                ArchiveLimits(max_seconds=bad)
        ArchiveLimits(max_seconds=1)
        ArchiveLimits(max_seconds=0.5)

    def test_frozen(self):
        limits = ArchiveLimits()
        with self.assertRaises(FrozenInstanceError):
            limits.max_entries = 5

    def test_wrong_limits_type(self):
        with self.assertRaises(TypeError):
            inspect_archive(self.tmp / "x.jar", limits={"max_entries": 1})


class OuterCaptureTest(ArchiveTestCase):
    def test_symlink_raises_capture_error(self):
        target = self.write("real.jar", jar_with_pom("g", "a", "1"))
        link = self.tmp / "link.jar"
        link.symlink_to(target)
        with self.assertRaises(CaptureError):
            inspect_archive(link)

    def test_directory_and_missing_raise_capture_error(self):
        with self.assertRaises(CaptureError):
            inspect_archive(self.tmp)
        with self.assertRaises(CaptureError):
            inspect_archive(self.tmp / "gone.jar")

    def test_oversized_input_clearly_rejected(self):
        data = jar_with_pom("g", "a", "1")
        path = self.write("big.jar", data)
        result = self.inspect(path, max_input_bytes=16)
        self.assertEqual(result["coverage"]["status"], "rejected")
        self.assertEqual(result["archives"], [])
        self.assertEqual(result["entries"], [])
        self.assertIn("input-truncated", issues_by_code(result))
        self.assertTrue(result["coverage"]["input_truncated"])


class CaptureIntegrationTest(ArchiveTestCase):
    """End-to-end contract with mdi.capture: inspect_archive classifies
    incomplete captures by capture.py's diagnostic wording (see the
    compatibility note in inspect_archive). These tests run the real
    capture_file, so rewording capture's reason strings must fail here."""

    def test_oversized_archive_truncated_through_real_capture(self):
        data = jar_with_pom("com.example", "big", "1.0")
        path = self.write("huge.jar", data)
        result = self.inspect(path, max_input_bytes=8)
        self.assertEqual(result["coverage"]["status"], "rejected")
        self.assertTrue(result["coverage"]["input_truncated"])
        self.assertIn("input-truncated", issues_by_code(result))
        self.assertEqual(result["archives"], [])
        self.assertEqual(result["entries"], [])
        self.assertIsNone(result["coverage"]["outer_capture"]["sha256"])

    def test_changed_during_read_reported_as_input_changed(self):
        path = self.write("drift.jar", jar_with_pom("com.example", "drift", "1.0"))
        st = os.stat(path)
        drifted = SimpleNamespace(st_mode=st.st_mode, st_dev=st.st_dev, st_ino=st.st_ino,
                                  st_size=st.st_size, st_mtime_ns=st.st_mtime_ns + 1)
        with mock.patch("mdi.capture._fstat", side_effect=[st, drifted]):
            result = inspect_archive(path, limits=ArchiveLimits())
        self.assertEqual(result["coverage"]["status"], "rejected")
        self.assertFalse(result["coverage"]["input_truncated"])
        self.assertIn("input-changed", issues_by_code(result))
        self.assertEqual(result["archives"], [])
        self.assertEqual(result["entries"], [])
        self.assertIsNone(result["coverage"]["outer_capture"]["sha256"])


class PlainJarTest(ArchiveTestCase):
    def test_identity_candidate_and_entry_hashes(self):
        data = jar_with_pom("com.example", "lib", "1.2.3")
        path = self.write("lib.jar", data)
        result = self.inspect(path)
        self.assertEqual(len(result["archives"]), 1)
        record = result["archives"][0]
        self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(record["entry_chain"], [])
        self.assertEqual(record["inspection_status"], "complete")
        identity = record["identity"]
        self.assertEqual(identity["status"], "candidate")
        self.assertFalse(identity["conflict"])
        self.assertEqual(identity["candidates"][0]["groupId"], "com.example")
        self.assertEqual(identity["candidates"][0]["version"], "1.2.3")
        classes = entry_named(result, "com/example/Thing.class")
        self.assertEqual(classes[0]["sha256"],
                         hashlib.sha256(b"\xca\xfe\xba\xbe" + bytes(range(64))).hexdigest())
        self.assertEqual(classes[0]["status"], "hashed")
        self.assertEqual(result["coverage"]["status"], "inspected")

    def test_directory_entry_distinguished(self):
        data = build_zip([("META-INF/", b""), ("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")])
        path = self.write("d.jar", data)
        result = self.inspect(path)
        dirs = [e for e in result["entries"] if e["kind"] == "directory"]
        files = [e for e in result["entries"] if e["kind"] == "file"]
        self.assertEqual([d["name"] for d in dirs], ["META-INF/"])
        self.assertEqual(dirs[0]["status"], "recorded")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["sha256"],
                         hashlib.sha256(b"Manifest-Version: 1.0\n").hexdigest())

    def test_missing_metadata_is_unknown(self):
        data = build_zip([("a.txt", b"hello")])
        result = self.inspect(self.write("x.jar", data))
        identity = result["archives"][0]["identity"]
        self.assertEqual(identity["status"], "unknown")
        self.assertEqual(identity["candidates"], [])

    def test_pom_xml_is_ordinary_entry(self):
        data = build_zip([("META-INF/maven/g/a/pom.xml", b"<project/>")])
        result = self.inspect(self.write("x.jar", data))
        self.assertEqual(result["archives"][0]["identity"]["status"], "unknown")
        entry = entry_named(result, "META-INF/maven/g/a/pom.xml")[0]
        self.assertEqual(entry["status"], "hashed")

    def test_identity_independent_of_inspection_completeness(self):
        items = [("META-INF/maven/g/a/pom.properties", pom_properties("g", "a", "1")),
                 ("big.bin", b"\x00" * 200)]
        data = build_zip(items)
        result = self.inspect(self.write("x.jar", data), max_entry_bytes=64)
        record = result["archives"][0]
        self.assertEqual(record["identity"]["status"], "candidate")
        self.assertEqual(record["inspection_status"], "partial")
        self.assertEqual(entry_named(result, "big.bin")[0]["status"], "too-large")


class NestedArchiveTest(ArchiveTestCase):
    def test_war_web_inf_lib_nested_jar(self):
        inner = jar_with_pom("org.inner", "inner-lib", "2.0")
        war = build_zip([("WEB-INF/web.xml", b"<web-app/>"),
                         ("WEB-INF/lib/inner.jar", inner),
                         ("META-INF/maven/com.out/war/pom.properties",
                          pom_properties("com.out", "war", "3.1"))])
        result = self.inspect(self.write("app.war", war))
        self.assertEqual(len(result["archives"]), 2)
        inner_index = entry_named(result, "WEB-INF/lib/inner.jar")[0]["index"]
        nested = archive_by_id(result, f"app.war[{inner_index}]")
        self.assertEqual(nested["entry_chain"], [inner_index])
        self.assertEqual(nested["sha256"], hashlib.sha256(inner).hexdigest())
        self.assertEqual(nested["path"], str(self.tmp / "app.war"))
        self.assertIn("!", nested["display_path"])
        self.assertEqual(nested["identity"]["candidates"][0]["groupId"], "org.inner")
        outer = result["archives"][0]
        self.assertEqual(outer["identity"]["candidates"][0]["artifactId"], "war")

    def test_boot_nested_two_levels(self):
        leaf = jar_with_pom("c.group", "c", "1")
        middle = build_zip([("BOOT-INF/lib/c.jar", leaf)])
        boot = build_zip([("BOOT-INF/lib/b.jar", middle),
                          ("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")])
        result = self.inspect(self.write("boot.jar", boot))
        self.assertEqual(len(result["archives"]), 3)
        b_index = entry_named(result, "BOOT-INF/lib/b.jar")[0]["index"]
        c_index = None
        for entry in result["entries"]:
            if entry["name"] == "BOOT-INF/lib/c.jar" and entry["archive_id"] == f"boot.jar[{b_index}]":
                c_index = entry["index"]
        self.assertIsNotNone(c_index)
        leaf_record = archive_by_id(result, f"boot.jar[{b_index}][{c_index}]")
        self.assertEqual(leaf_record["entry_chain"], [b_index, c_index])
        self.assertEqual(leaf_record["sha256"], hashlib.sha256(leaf).hexdigest())
        self.assertEqual(leaf_record["identity"]["candidates"][0]["artifactId"], "c")

    def test_identical_nested_copies_share_digest_not_identity_of_record(self):
        lib = jar_with_pom("same.group", "lib", "9.9")
        app = build_zip([("WEB-INF/lib/one.jar", lib), ("WEB-INF/lib/two.jar", lib)])
        result = self.inspect(self.write("dup.war", app))
        self.assertEqual(len(result["archives"]), 3)
        one = entry_named(result, "WEB-INF/lib/one.jar")[0]
        two = entry_named(result, "WEB-INF/lib/two.jar")[0]
        self.assertNotEqual(one["index"], two["index"])
        records = [archive_by_id(result, f"dup.war[{one['index']}]"),
                   archive_by_id(result, f"dup.war[{two['index']}]")]
        self.assertEqual(records[0]["sha256"], hashlib.sha256(lib).hexdigest())
        self.assertEqual(records[1]["sha256"], records[0]["sha256"])
        self.assertNotEqual(records[0]["archive_id"], records[1]["archive_id"])

    def test_duplicate_entry_names_kept_separate(self):
        inner_a = jar_with_pom("g", "a", "1")
        inner_b = jar_with_pom("g", "b", "1")
        outer = build_zip([("lib/inner.jar", inner_a), ("lib/inner.jar", inner_b)])
        result = self.inspect(self.write("dup.jar", outer))
        dups = entry_named(result, "lib/inner.jar")
        self.assertEqual(len(dups), 2)
        self.assertEqual({d["archive_id"] for d in dups}, {"dup.jar"})
        self.assertNotEqual(dups[0]["index"], dups[1]["index"])
        digests = {d["sha256"] for d in dups}
        self.assertEqual(digests, {hashlib.sha256(inner_a).hexdigest(),
                                   hashlib.sha256(inner_b).hexdigest()})
        nested = [a for a in result["archives"] if a["entry_chain"]]
        self.assertEqual(len(nested), 2)
        self.assertEqual({a["sha256"] for a in nested}, digests)


class PropertiesParsingTest(ArchiveTestCase):
    def inspect_with_metadata(self, metadata, extra=()):
        data = build_zip([(f"META-INF/maven/g/a/pom.properties", metadata)] + list(extra))
        return self.inspect(self.write("m.jar", data))["archives"][0]["identity"]

    def test_simple_properties_recognized(self):
        metadata = b"#Generated by Maven\ngroupId=g\nartifactId=a\nversion=1\n"
        identity = self.inspect_with_metadata(metadata)
        self.assertEqual(identity["status"], "candidate")
        self.assertEqual(identity["candidates"][0]["version"], "1")

    def test_continuation_marks_unsupported_not_guessed(self):
        identity = self.inspect_with_metadata(b"groupId=g\nartifactId=a\\\nversion=1\n")
        self.assertEqual(identity["status"], "unknown")
        self.assertEqual(identity["candidates"], [])
        self.assertTrue(identity["evidence"])
        self.assertEqual(identity["evidence"][0]["parse"], "unsupported")

    def test_escape_sequence_marks_unsupported(self):
        identity = self.inspect_with_metadata(b"groupId=g\\:x\nartifactId=a\nversion=1\n")
        self.assertEqual(identity["status"], "unknown")

    def test_non_ascii_marks_unsupported(self):
        identity = self.inspect_with_metadata(b"groupId=g\xa0\nartifactId=a\nversion=1\n")
        self.assertEqual(identity["status"], "unknown")

    def test_missing_coordinate_keys_not_a_candidate(self):
        identity = self.inspect_with_metadata(b"other=key\n")
        self.assertEqual(identity["status"], "unknown")
        self.assertEqual(identity["evidence"][0]["parse"], "unsupported")

    def test_conflicting_metadata_keeps_candidates_separate(self):
        data = build_zip([("META-INF/maven/g1/a1/pom.properties", pom_properties("g1", "a1", "1")),
                          ("META-INF/maven/g2/a2/pom.properties", pom_properties("g2", "a2", "2"))])
        identity = self.inspect(self.write("c.jar", data))["archives"][0]["identity"]
        self.assertEqual(identity["status"], "candidate")
        self.assertTrue(identity["conflict"])
        self.assertEqual(len(identity["candidates"]), 2)
        self.assertEqual({c["groupId"] for c in identity["candidates"]}, {"g1", "g2"})

    def test_evidence_raw_is_bounded_text(self):
        identity = self.inspect_with_metadata(b"groupId=g\nartifactId=a\nversion=1\npad=" + b"x" * 8192 + b"\n")
        evidence = identity["evidence"][0]
        self.assertLessEqual(len(evidence["raw"]), PREVIEW_CAP + 64)
        json.dumps(evidence)

    def test_conflicting_key_after_preview_boundary_detected(self):
        head = b"groupId=g\nartifactId=a\nversion=1\n"
        meta = metadata_with_tail(b"version=2\n", head, PREVIEW_CAP)
        identity = self.inspect_with_metadata(meta)
        self.assertEqual(identity["status"], "unknown")
        self.assertEqual(identity["candidates"], [])
        self.assertEqual(identity["evidence"][0]["parse"], "unsupported")
        self.assertEqual(identity["evidence"][0]["reason"], "conflicting duplicate key 'version'")

    def test_value_straddling_preview_boundary_not_fabricated(self):
        head = b"groupId=g\nartifactId=a\n"
        meta = metadata_with_tail(b"version=1.2.3\n", head, PREVIEW_CAP - 9)
        identity = self.inspect_with_metadata(meta)
        self.assertEqual(identity["status"], "candidate")
        self.assertEqual(identity["candidates"][0]["version"], "1.2.3")

    def test_escape_after_preview_boundary_marks_unsupported(self):
        head = b"groupId=g\nartifactId=a\nversion=1\n"
        meta = metadata_with_tail(b"extra=x\\y\n", head, PREVIEW_CAP)
        identity = self.inspect_with_metadata(meta)
        self.assertEqual(identity["status"], "unknown")
        self.assertEqual(identity["evidence"][0]["parse"], "unsupported")
        self.assertEqual(identity["evidence"][0]["reason"], "escape sequence")

    def test_long_value_longer_than_preview_fully_parsed(self):
        long_version = b"1." + b"9" * 6000
        meta = b"groupId=g\nartifactId=a\nversion=" + long_version + b"\n"
        identity = self.inspect_with_metadata(meta)
        self.assertEqual(identity["status"], "candidate")
        self.assertEqual(identity["candidates"][0]["version"].encode("ascii"), long_version)
        evidence = identity["evidence"][0]
        self.assertTrue(evidence["raw_truncated"])
        self.assertLessEqual(len(evidence["raw"]), PREVIEW_CAP + 64)


class NameRejectionTest(ArchiveTestCase):
    def inspect_names(self, items):
        data = build_zip(items)
        result = self.inspect(self.write("names.jar", data))
        return result, {e["name"]: e for e in result["entries"]}

    def test_absolute_path_rejected(self):
        _, entries = self.inspect_names([("/etc/passwd", b"x"), ("ok.txt", b"y")])
        self.assertEqual(entries["/etc/passwd"]["status"], "name-rejected")
        self.assertIsNone(entries["/etc/passwd"]["sha256"])
        self.assertEqual(entries["ok.txt"]["status"], "hashed")

    def test_parent_traversal_rejected(self):
        _, entries = self.inspect_names([("../escape.txt", b"x")])
        self.assertEqual(entries["../escape.txt"]["status"], "name-rejected")

    def test_backslash_rejected(self):
        _, entries = self.inspect_names([("dir\\file.txt", b"x")])
        self.assertEqual(entries["dir\\file.txt"]["status"], "name-rejected")

    def test_control_and_nul_names_rejected(self):
        result, entries = self.inspect_names([
            ("ctl\x01.txt", b"x"),
            (nul_name_info("evil\x00.txt"), b"x"),
            ("fine.txt", b"z"),
        ])
        self.assertEqual(entries["ctl\x01.txt"]["status"], "name-rejected")
        nul_entry = [e for e in result["entries"] if e["name"] == "evil"][0]
        self.assertEqual(nul_entry["status"], "name-rejected")

    def test_symlink_rejected_without_read(self):
        zi = zipfile.ZipInfo("link.txt")
        zi.external_attr = (stat.S_IFLNK | 0o777) << 16
        data = build_zip([(zi, b"target.txt")])
        result = self.inspect(self.write("l.jar", data))
        entry = result["entries"][0]
        self.assertEqual(entry["kind"], "symlink")
        self.assertEqual(entry["status"], "symlink")
        self.assertIsNone(entry["sha256"])


class EntryPolicyTest(ArchiveTestCase):
    def test_encrypted_entry_rejected_and_sibling_read(self):
        data = build_zip([("secret.txt", b"hidden"), ("open.txt", b"visible")])
        patched = with_cd_flags(data, "secret.txt", 0x1)
        result = self.inspect(self.write("e.jar", patched))
        entry = entry_named(result, "secret.txt")[0]
        self.assertEqual(entry["status"], "encrypted")
        self.assertIsNone(entry["sha256"])
        self.assertEqual(entry_named(result, "open.txt")[0]["status"], "hashed")

    def test_unsupported_compression_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("normal.txt", b"plain")
            zf.writestr("packed.txt", b"payload", compress_type=zipfile.ZIP_BZIP2)
        result = self.inspect(self.write("c.jar", buf.getvalue()))
        entry = entry_named(result, "packed.txt")[0]
        self.assertEqual(entry["status"], "unsupported-compression")
        self.assertIsNone(entry["sha256"])
        self.assertEqual(entry_named(result, "normal.txt")[0]["status"], "hashed")

    def test_declared_smaller_than_actual_is_read_error_sibling_survives(self):
        data = build_zip([("first.txt", b"A" * 300),
                          ("lying.txt", b"B" * 300),
                          ("last.txt", b"C" * 300)])
        patched = with_cd_usize(data, "lying.txt", 10)
        result = self.inspect(self.write("r.jar", patched))
        lying = entry_named(result, "lying.txt")[0]
        self.assertEqual(lying["status"], "read-error")
        self.assertIsNone(lying["sha256"])
        self.assertEqual(entry_named(result, "first.txt")[0]["status"], "hashed")
        self.assertEqual(entry_named(result, "last.txt")[0]["status"], "hashed")
        self.assertEqual(result["archives"][0]["inspection_status"], "partial")

    def test_declared_larger_than_actual_is_size_mismatch(self):
        data = build_zip([("odd.txt", b"B" * 300)])
        patched = with_cd_usize(data, "odd.txt", 5000)
        result = self.inspect(self.write("o.jar", patched))
        entry = entry_named(result, "odd.txt")[0]
        self.assertEqual(entry["status"], "size-mismatch")
        self.assertEqual(entry["size_declared"], 5000)
        self.assertEqual(entry["size_actual"], 300)
        self.assertEqual(entry["sha256"], hashlib.sha256(b"B" * 300).hexdigest())


class MalformedTest(ArchiveTestCase):
    def test_plain_bytes_not_a_zip(self):
        path = self.write("not.jar", b"just text, no zip here")
        result = self.inspect(path)
        self.assertEqual(len(result["archives"]), 1)
        record = result["archives"][0]
        self.assertEqual(record["inspection_status"], "malformed")
        self.assertEqual(record["sha256"], hashlib.sha256(b"just text, no zip here").hexdigest())
        self.assertIn("not-a-zip", issues_by_code(result))
        self.assertEqual(result["entries"], [])

    def test_empty_file_not_a_zip(self):
        result = self.inspect(self.write("empty.jar", b""))
        self.assertEqual(result["archives"][0]["inspection_status"], "malformed")

    def test_truncated_zip(self):
        data = jar_with_pom("g", "a", "1")
        result = self.inspect(self.write("cut.jar", data[:len(data) // 2]))
        self.assertEqual(result["archives"][0]["inspection_status"], "malformed")
        self.assertIn("not-a-zip", issues_by_code(result))

    def test_corrupt_central_directory(self):
        data = jar_with_pom("g", "a", "1")
        mut = bytearray(data)
        off = eocd_fields(data)["offset_cd"]
        mut[off] ^= 0xFF  # break the first central directory signature
        result = self.inspect(self.write("bad.jar", bytes(mut)))
        self.assertEqual(result["archives"][0]["inspection_status"], "malformed")
        self.assertIn("malformed-zip", issues_by_code(result))


class PreflightLimitTest(ArchiveTestCase):
    def test_central_directory_budget_enforced_before_zipfile(self):
        data = jar_with_pom("g", "a", "1")
        lying = with_eocd(data, size_cd=0xFFFFFFFF)
        path = self.write("meta.jar", lying)
        with mock.patch.object(archives_module.zipfile, "ZipFile") as zf_mock:
            result = inspect_archive(path, limits=ArchiveLimits(max_metadata_bytes=1024))
        zf_mock.assert_not_called()
        self.assertIn("central-directory-too-large", issues_by_code(result))
        record = result["archives"][0]
        self.assertEqual(record["inspection_status"], "rejected")
        self.assertEqual(result["entries"], [])
        self.assertIsNotNone(record["sha256"])

    def test_zip64_declared_unsupported(self):
        data = jar_with_pom("g", "a", "1")
        result = self.inspect(self.write("z64.jar", as_zip64(data)))
        self.assertIn("zip64-unsupported", issues_by_code(result))
        record = result["archives"][0]
        self.assertEqual(record["inspection_status"], "unsupported")
        self.assertEqual(result["entries"], [])
        self.assertIsNotNone(record["sha256"])

    def test_multi_disk_declared_unsupported(self):
        data = jar_with_pom("g", "a", "1")
        result = self.inspect(self.write("disk.jar", with_eocd(data, disk=1, cd_disk=1)))
        self.assertIn("multi-disk", issues_by_code(result))
        self.assertEqual(result["archives"][0]["inspection_status"], "unsupported")

    def test_declared_entry_count_over_budget_rejected(self):
        data = build_zip([("a.txt", b"1"), ("b.txt", b"2"), ("c.txt", b"3")])
        result = self.inspect(self.write("n.jar", data), max_entries=2)
        self.assertIn("entries-limit", issues_by_code(result))
        self.assertEqual(result["archives"][0]["inspection_status"], "rejected")
        self.assertEqual(result["entries"], [])

    def test_lying_small_count_then_actual_over_budget(self):
        data = build_zip([("a.txt", b"1"), ("b.txt", b"2"), ("c.txt", b"3")])
        lying = with_eocd(data, count_this=1, count_total=1)
        result = self.inspect(self.write("lie.jar", lying), max_entries=2)
        self.assertIn("entries-limit", issues_by_code(result))
        self.assertEqual(result["archives"][0]["inspection_status"], "rejected")
        self.assertEqual(result["entries"], [])

    def test_count_mismatch_within_budget_reported_but_kept(self):
        data = build_zip([("a.txt", b"1"), ("b.txt", b"2"), ("c.txt", b"3")])
        lying = with_eocd(data, count_this=1, count_total=1)
        result = self.inspect(self.write("mm.jar", lying))
        self.assertIn("entries-count-mismatch", issues_by_code(result))
        self.assertEqual(len(result["entries"]), 3)
        self.assertEqual(result["archives"][0]["inspection_status"], "complete")

    def test_entry_budget_shared_across_nesting(self):
        inner = build_zip([("i1.txt", b"1"), ("i2.txt", b"2")])
        outer = build_zip([("before.txt", b"0"), ("lib/inner.jar", inner)])
        result = self.inspect(self.write("shared.jar", outer), max_entries=3)
        outer_entries = [e for e in result["entries"] if e["archive_id"] == "shared.jar"]
        self.assertEqual(len(outer_entries), 2)
        nested = [a for a in result["archives"] if a["entry_chain"]]
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]["inspection_status"], "rejected")
        self.assertIn("entries-limit", issues_by_code(result))


class NestedBudgetRegressionTest(ArchiveTestCase):
    def test_parent_rechecked_after_recursion_consumed_budget(self):
        inner = build_zip([("child.txt", b"c")])
        outer = build_zip([("lib/inner.jar", inner), ("after.txt", b"a")])
        result = self.inspect(self.write("p.jar", outer), max_entries=2)
        self.assertEqual(result["coverage"]["entries_budget_used"], 2)
        names = [e["name"] for e in result["entries"]]
        self.assertIn("lib/inner.jar", names)
        self.assertIn("child.txt", names)
        self.assertNotIn("after.txt", names)
        self.assertIn("entries-limit", issues_by_code(result))
        self.assertEqual(result["archives"][0]["inspection_status"], "partial")
        inner_record = [a for a in result["archives"] if a["entry_chain"]][0]
        self.assertEqual(inner_record["inspection_status"], "complete")
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_deeper_siblings_exact_boundary(self):
        a = build_zip([("child.txt", b"a")])
        b = build_zip([("child.txt", b"b")])
        outer = build_zip([("lib/a.jar", a), ("lib/b.jar", b), ("tail.txt", b"t")])
        result = self.inspect(self.write("d.jar", outer), max_entries=4)
        self.assertEqual(result["coverage"]["entries_budget_used"], 4)
        names = [e["name"] for e in result["entries"]]
        self.assertNotIn("tail.txt", names)
        self.assertEqual(names.count("child.txt"), 2)
        self.assertIn("lib/a.jar", names)
        self.assertIn("lib/b.jar", names)
        self.assertIn("entries-limit", issues_by_code(result))
        self.assertEqual(result["archives"][0]["inspection_status"], "partial")
        for record in result["archives"][1:]:
            self.assertEqual(record["inspection_status"], "complete")


class ByteBudgetTest(ArchiveTestCase):
    def test_entry_exactly_at_cap_hashed(self):
        data = build_zip([("edge.bin", b"x" * 10)])
        result = self.inspect(self.write("e.jar", data), max_entry_bytes=10)
        entry = entry_named(result, "edge.bin")[0]
        self.assertEqual(entry["status"], "hashed")
        self.assertEqual(entry["sha256"], hashlib.sha256(b"x" * 10).hexdigest())

    def test_entry_one_over_cap_too_large(self):
        data = build_zip([("over.bin", b"x" * 11), ("ok.bin", b"y" * 4)])
        result = self.inspect(self.write("o.jar", data), max_entry_bytes=10)
        entry = entry_named(result, "over.bin")[0]
        self.assertEqual(entry["status"], "too-large")
        self.assertIsNone(entry["sha256"])
        self.assertEqual(entry["size_actual"], 11)
        self.assertEqual(entry_named(result, "ok.bin")[0]["status"], "hashed")

    def test_expanded_budget_shared_across_entries(self):
        data = build_zip([("a.bin", b"1" * 10), ("b.bin", b"2" * 10), ("c.bin", b"3" * 5)])
        result = self.inspect(self.write("b.jar", data), max_expanded_bytes=20)
        a, b, c = (entry_named(result, n)[0] for n in ("a.bin", "b.bin", "c.bin"))
        self.assertEqual(a["status"], "hashed")
        self.assertEqual(b["status"], "hashed")
        self.assertEqual(c["status"], "expanded-budget")
        self.assertIsNone(c["sha256"])
        self.assertGreater(result["coverage"]["expanded_bytes_used"], 20)

    def test_nested_container_charged_again(self):
        inner = build_zip([("f.txt", b"payload")])
        outer = build_zip([("lib/inner.jar", inner)])
        result = self.inspect(self.write("d.jar", outer), max_expanded_bytes=len(inner))
        used = result["coverage"]["expanded_bytes_used"]
        self.assertEqual(used, len(inner) * 2)
        nested = [a for a in result["archives"] if a["entry_chain"]]
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]["sha256"], hashlib.sha256(inner).hexdigest())
        self.assertEqual(nested[0]["inspection_status"], "partial")
        inner_entry = [e for e in result["entries"] if e["name"] == "f.txt"]
        self.assertEqual(inner_entry[0]["status"], "expanded-budget")


class DepthAndDeadlineTest(ArchiveTestCase):
    def test_depth_budget_shared_across_nesting(self):
        leaf = build_zip([("d.txt", b"leaf")])
        c = build_zip([("lib/d.jar", leaf)])
        b = build_zip([("lib/c.jar", c)])
        a = build_zip([("lib/b.jar", b)])
        result = self.inspect(self.write("a.jar", a), max_depth=2)
        self.assertEqual(len(result["archives"]), 3)
        entry = entry_named(result, "lib/d.jar")[0]
        self.assertEqual(entry["status"], "depth-limit")
        deepest = [arc for arc in result["archives"] if len(arc["entry_chain"]) == 2][0]
        self.assertEqual(deepest["inspection_status"], "partial")

    def test_deadline_stops_with_issue(self):
        data = jar_with_pom("g", "a", "1")
        path = self.write("slow.jar", data)
        clock = iter([0.0, 100.0])
        fake = lambda: next(clock, 1e9)  # noqa: E731
        with mock.patch.object(archives_module, "_monotonic", fake):
            result = inspect_archive(path, limits=ArchiveLimits(max_seconds=1))
        self.assertIn("deadline", issues_by_code(result))
        self.assertTrue(result["coverage"]["deadline_exceeded"])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["archives"][0]["inspection_status"], "skipped")
        self.assertEqual(result["entries"], [])


class ResultShapeTest(ArchiveTestCase):
    def test_json_serializable(self):
        inner = jar_with_pom("g", "inner", "1")
        war = build_zip([("WEB-INF/lib/inner.jar", inner), ("odd\\name.txt", b"x")])
        result = self.inspect(self.write("shape.war", war))
        text = json.dumps(result)
        self.assertIn("archives", text)
        coverage = result["coverage"]
        self.assertEqual(coverage["outer_capture"]["nofollow"], "final-component")
        self.assertEqual(coverage["outer_capture"]["ancestors"], "caller-responsibility")
        for field in ("archives_total", "archives_complete", "entries_recorded",
                      "entries_hashed", "expanded_bytes_used", "expanded_bytes_limit",
                      "entries_budget_used", "entries_budget_limit", "input_truncated",
                      "deadline_exceeded", "stopped_early"):
            self.assertIn(field, coverage)

    def test_no_disk_writes_network_or_processes(self):
        inner = jar_with_pom("g", "inner", "1")
        war = build_zip([("WEB-INF/lib/inner.jar", inner)])
        path = self.write("side.war", war)
        before = sorted(p.name for p in self.tmp.iterdir())
        patches = [
            mock.patch.object(subprocess, "Popen"),
            mock.patch.object(subprocess, "run"),
            mock.patch.object(os, "system"),
            mock.patch.object(os, "popen"),
            mock.patch.object(socket, "socket"),
        ]
        with contextlib.ExitStack() as stack:
            guards = [stack.enter_context(patch) for patch in patches]
            result = inspect_archive(path, limits=ArchiveLimits())
        for guard in guards:
            guard.assert_not_called()
        after = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(before, after)
        self.assertEqual(result["coverage"]["status"], "inspected")


class CapturedArchiveTest(ArchiveTestCase):
    """inspect_captured_archive: the descriptor-captured entry point."""

    def inspect_captured(self, data, limits=None, **captured_overrides):
        import hashlib as _hl
        from dataclasses import replace
        from mdi.capture import CapturedFile, FileIdentity
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        ident = FileIdentity(dev=1, ino=2, size=len(data), mtime_ns=0)
        captured = CapturedFile(path="/fixture/app.jar", data=data,
                                sha256=_hl.sha256(data).hexdigest(), complete=True,
                                reason=None, observed_at=now,
                                stat_before=ident, stat_after=ident)
        captured = replace(captured, **captured_overrides)
        from mdi.archives import inspect_captured_archive
        return inspect_captured_archive(captured, limits=limits or ArchiveLimits())

    def test_matches_inspect_archive_result(self):
        data = build_zip([("META-INF/maven/g/a/pom.properties",
                           pom_properties("g", "a", "1"))])
        path = self.write("app.jar", data)
        direct = self.inspect(path)
        from mdi.capture import capture_file
        captured = capture_file(str(path), ArchiveLimits().max_input_bytes)
        from mdi.archives import inspect_captured_archive
        via_captured = inspect_captured_archive(captured)
        self.assertEqual([a["archive_id"] for a in direct["archives"]],
                         [a["archive_id"] for a in via_captured["archives"]])
        self.assertEqual(direct["coverage"]["archives_total"],
                         via_captured["coverage"]["archives_total"])
        self.assertEqual(via_captured["coverage"]["status"], "inspected")

    def test_rejects_non_capturedfile(self):
        from mdi.archives import inspect_captured_archive
        with self.assertRaises(TypeError):
            inspect_captured_archive(b"bytes")

    def test_rejects_wrong_limits_type(self):
        from mdi.archives import inspect_captured_archive
        data = build_zip([("x", b"y")])
        with self.assertRaises(TypeError):
            self.inspect_captured(data, limits=object())

    def test_data_larger_than_input_limit_is_rejected(self):
        data = build_zip([("x", b"y")])
        result = self.inspect_captured(data, limits=ArchiveLimits(max_input_bytes=4))
        self.assertEqual(result["coverage"]["status"], "rejected")
        self.assertIn("input-exceeds-limit", issues_by_code(result))

    def test_digest_mismatch_is_rejected(self):
        data = build_zip([("META-INF/maven/g/a/pom.properties",
                           pom_properties("g", "a", "1"))])
        result = self.inspect_captured(data, sha256="0" * 64)
        self.assertEqual(result["coverage"]["status"], "rejected")
        self.assertIn("input-digest-mismatch", issues_by_code(result))
        self.assertEqual(result["archives"], [])

    def test_incomplete_capture_rejected_with_reason_code(self):
        data = build_zip([("x", b"y")])
        result = self.inspect_captured(data, complete=False,
                                       reason="truncated at max_bytes=4",
                                       sha256=None)
        self.assertEqual(result["coverage"]["status"], "rejected")
        self.assertIn("input-truncated", issues_by_code(result))


if __name__ == "__main__":
    unittest.main()

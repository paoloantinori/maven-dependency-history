"""Structural ZIP preflight over captured files, without ZipInfo allocation."""

import io
import os
import struct
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.zip_preflight import PreflightError, preflight_zip


def archive_bytes(names=("one", "two"), comment=b""):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        for name in names:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.extra = b"\xfe\xca\x04\x00PK\x01\x02"
            info.comment = b"PK\x01\x02"
            archive.writestr(info, b"payload")
        archive.comment = comment
    return output.getvalue()


def patch_end(data, **fields):
    offset = data.rfind(zipfile.stringEndArchive)
    end = list(struct.unpack(zipfile.structEndArchive, data[offset:offset + zipfile.sizeEndCentDir]))
    for name, value in fields.items():
        end[getattr(zipfile, "_ECD_" + name)] = value
    return data[:offset] + struct.pack(zipfile.structEndArchive, *end) + data[offset + zipfile.sizeEndCentDir:]


def zip64_bytes(data):
    """Wrap ordinary central records with standard ZIP64 end records."""
    offset = data.rfind(zipfile.stringEndArchive)
    end = list(struct.unpack(zipfile.structEndArchive, data[offset:offset + zipfile.sizeEndCentDir]))
    record = struct.pack(
        zipfile.structEndArchive64, zipfile.stringEndArchive64,
        zipfile.sizeEndCentDir64 - 12, 45, 45, 0, 0,
        end[zipfile._ECD_ENTRIES_TOTAL], end[zipfile._ECD_ENTRIES_TOTAL],
        end[zipfile._ECD_SIZE], end[zipfile._ECD_OFFSET],
    )
    locator = struct.pack(zipfile.structEndArchive64Locator,
                          zipfile.stringEndArchive64Locator, 0, offset, 1)
    end[zipfile._ECD_ENTRIES_TOTAL] = 0xffff
    end[zipfile._ECD_ENTRIES_THIS_DISK] = 0xffff
    end[zipfile._ECD_SIZE] = 0xffffffff
    end[zipfile._ECD_OFFSET] = 0xffffffff
    return data[:offset] + record + locator + struct.pack(zipfile.structEndArchive, *end)


class ZipPreflightTests(unittest.TestCase):
    def check(self, data=None, **kwargs):
        return preflight_zip(io.BytesIO(archive_bytes() if data is None else data), **kwargs)

    def assert_code(self, code, data=None, **kwargs):
        with self.assertRaises(PreflightError) as caught:
            self.check(data, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_normal_inventory_without_zipinfo_allocation(self):
        data = archive_bytes(("onePK\x01\x02", "two"))
        with mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("allocated ZipFile")), \
             mock.patch.object(zipfile, "ZipInfo", side_effect=AssertionError("allocated ZipInfo")):
            result = self.check(data)
        self.assertEqual(result["format"], "zip")
        self.assertEqual(result["entries_actual"], 2)
        self.assertEqual(result["entries_declared"], 2)
        self.assertTrue(result["count_matches"])
        self.assertEqual(result["file_size"], len(data))
        self.assertEqual(result["prepended_bytes"], 0)

    def test_embedded_signatures_are_not_counted(self):
        self.assertEqual(self.check(archive_bytes(("PK\x01\x02",)), max_entries=1)["entries_actual"], 1)

    def test_count_lies_are_reported_but_do_not_define_allocation_bound(self):
        for declared in (0, 40000):
            with self.subTest(declared=declared):
                result = self.check(patch_end(archive_bytes(), ENTRIES_TOTAL=declared), max_entries=2)
                self.assertFalse(result["count_matches"])
                self.assertEqual(result["entries_actual"], 2)
                self.assertEqual(result["entries_declared"], declared)
        self.assert_code("entries-limit", patch_end(archive_bytes(), ENTRIES_TOTAL=0), max_entries=1)

    def test_exact_budget_and_empty_archive(self):
        self.assertEqual(self.check(max_entries=2)["entries_actual"], 2)
        self.assert_code("entries-limit", max_entries=1)
        self.assertEqual(self.check(archive_bytes(()), max_entries=0)["entries_actual"], 0)
        self.assert_code("entries-limit", max_entries=0)

    def test_metadata_cap_checked_before_walking(self):
        data = archive_bytes()
        size = self.check(data)["central_directory_bytes"]
        self.assertEqual(self.check(data, max_metadata_bytes=size)["entries_actual"], 2)
        self.assert_code("central-directory-too-large", data, max_metadata_bytes=size - 1)

    def test_sfx_offsets(self):
        prefix = b"synthetic executable stub\n" * 3
        data = archive_bytes()
        plain = self.check(data)
        result = self.check(prefix + data)
        self.assertEqual(result["prepended_bytes"], len(prefix))
        self.assertEqual(result["central_directory_offset"], plain["central_directory_offset"] + len(prefix))

    def test_zip64_and_prepended_zip64(self):
        data = zip64_bytes(archive_bytes())
        for prefix in (b"", b"SFX stub\n" * 30):
            with self.subTest(prefix=len(prefix)):
                with zipfile.ZipFile(io.BytesIO(prefix + data)) as archive:
                    self.assertEqual(len(archive.infolist()), 2)
                result = self.check(prefix + data)
                self.assertEqual(result["format"], "zip64")
                self.assertEqual(result["entries_actual"], 2)
                self.assertEqual(result["prepended_bytes"], len(prefix))

    def test_zip64_actual_count_above_65535_is_bounded_before_allocation(self):
        single = archive_bytes(("x",))
        end_at = single.rfind(zipfile.stringEndArchive)
        end = list(struct.unpack(zipfile.structEndArchive, single[end_at:]))
        central_at = end[zipfile._ECD_OFFSET]
        header = list(struct.unpack(zipfile.structCentralDir,
                                   single[central_at:central_at + zipfile.sizeCentralDir]))
        header[zipfile._CD_EXTRA_FIELD_LENGTH] = 0
        header[zipfile._CD_COMMENT_LENGTH] = 0
        central = (struct.pack(zipfile.structCentralDir, *header) + b"x") * 65537
        # This is a directory-only metadata fixture, not a valid member-data
        # fixture: all records refer to the one retained local header.
        end[zipfile._ECD_SIZE] = len(central)
        end[zipfile._ECD_ENTRIES_TOTAL] = 65535
        end[zipfile._ECD_ENTRIES_THIS_DISK] = 65535
        data = zip64_bytes(single[:central_at] + central + struct.pack(zipfile.structEndArchive, *end))
        location = data.rfind(zipfile.stringEndArchive64)
        record = list(struct.unpack(zipfile.structEndArchive64,
                                    data[location:location + zipfile.sizeEndCentDir64]))
        record[6] = record[7] = 65537
        data = data[:location] + struct.pack(zipfile.structEndArchive64, *record) + data[location + zipfile.sizeEndCentDir64:]
        with mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("premature allocation")):
            self.assert_code("entries-limit", data)
            result = self.check(data, max_entries=65537)
        self.assertEqual(result["entries_actual"], 65537)
        self.assertTrue(result["count_matches"])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(len(archive.infolist()), result["entries_actual"])

    def test_multidisk_end_record_and_member(self):
        self.assert_code("multi-disk", patch_end(archive_bytes(), DISK_NUMBER=1))
        self.assert_code("multi-disk", patch_end(archive_bytes(), DISK_START=1))
        data = bytearray(archive_bytes())
        offset = self.check(bytes(data))["central_directory_offset"]
        header = list(struct.unpack(zipfile.structCentralDir, data[offset:offset + zipfile.sizeCentralDir]))
        header[zipfile._CD_DISK_NUMBER_START] = 1
        data[offset:offset + zipfile.sizeCentralDir] = struct.pack(zipfile.structCentralDir, *header)
        self.assert_code("multi-disk", bytes(data))

    def test_malformed_spans_and_records(self):
        self.assert_code("not-a-zip", b"not zip")
        self.assert_code("central-directory-out-of-range", patch_end(archive_bytes(), SIZE=999999))
        data = bytearray(archive_bytes())
        offset = self.check(bytes(data))["central_directory_offset"]
        data[offset] = 0
        self.assert_code("malformed-central-directory", bytes(data))
        data = bytearray(archive_bytes())
        header = list(struct.unpack(zipfile.structCentralDir, data[offset:offset + zipfile.sizeCentralDir]))
        header[zipfile._CD_FILENAME_LENGTH] = 65535
        data[offset:offset + zipfile.sizeCentralDir] = struct.pack(zipfile.structCentralDir, *header)
        self.assert_code("malformed-central-directory", bytes(data))
        # Keeping an otherwise valid signature in the tail cannot make a
        # truncated declared header parse as a complete central directory.
        self.assert_code("malformed-central-directory", patch_end(archive_bytes(), SIZE=10))

    def test_tail_comment_and_short_inputs(self):
        self.assertEqual(self.check(archive_bytes(comment=b"c" * 65535))["entries_actual"], 2)
        for length in (0, 1, 21):
            self.assert_code("not-a-zip", b"x" * length)

    def test_file_object_offset_restored_on_success_and_error(self):
        source = io.BytesIO(archive_bytes())
        source.seek(7)
        self.assertEqual(preflight_zip(source)["entries_actual"], 2)
        self.assertEqual(source.tell(), 7)
        with self.assertRaises(PreflightError):
            preflight_zip(source, max_entries=1)
        self.assertEqual(source.tell(), 7)
        self.assertFalse(source.closed)

    def test_fd_offset_and_ownership_unchanged(self):
        with tempfile.TemporaryFile() as source:
            source.write(archive_bytes())
            source.flush()
            source.seek(9)
            for fail in (False, True):
                if fail:
                    with self.assertRaises(PreflightError):
                        preflight_zip(source.fileno(), max_entries=1)
                else:
                    self.assertEqual(preflight_zip(source.fileno())["entries_actual"], 2)
                self.assertEqual(os.lseek(source.fileno(), 0, os.SEEK_CUR), 9)
                os.fstat(source.fileno())

    def test_regular_file_object_buffer_position_preserved(self):
        with tempfile.TemporaryFile() as source:
            source.write(archive_bytes())
            source.seek(5)
            preflight_zip(source)
            self.assertEqual(source.tell(), 5)
            self.assertEqual(source.read(3), archive_bytes()[5:8])

    def test_pipe_refused_without_reading_or_closing(self):
        reader, writer = os.pipe()
        try:
            with self.assertRaises(PreflightError) as caught:
                preflight_zip(reader)
            self.assertEqual(caught.exception.code, "invalid-source")
            os.fstat(reader)
        finally:
            os.close(reader)
            os.close(writer)

    def test_expired_deadline_and_deadline_during_io(self):
        self.assert_code("deadline-exceeded", deadline=time.monotonic() - 1)
        with mock.patch("mdi.zip_preflight.time.monotonic", side_effect=[0.0, 2.0]):
            self.assert_code("deadline-exceeded", deadline=1.0)

    def test_missing_stdlib_helper_fails_closed(self):
        with mock.patch.object(zipfile, "_EndRecData", None):
            self.assert_code("stdlib-preflight-unavailable")

    def test_unbounded_stdlib_read_is_refused(self):
        with mock.patch.object(zipfile, "_EndRecData", side_effect=lambda source: source.read()):
            self.assert_code("unbounded-read")

    def test_file_mutation_fails_closed_without_moving_descriptor(self):
        with tempfile.TemporaryFile() as source:
            source.write(archive_bytes())
            source.flush()
            source.seek(4)
            original_pread = os.pread
            changed = False

            def mutate(fd, count, offset):
                nonlocal changed
                data = original_pread(fd, count, offset)
                if not changed:
                    changed = True
                    os.ftruncate(fd, os.fstat(fd).st_size + 1)
                return data

            with mock.patch("mdi.zip_preflight.os.pread", side_effect=mutate):
                with self.assertRaises(PreflightError) as caught:
                    preflight_zip(source.fileno())
            self.assertEqual(caught.exception.code, "input-changed")
            self.assertEqual(source.tell(), 4)

    def test_short_object_read_and_interrupt_restore_cursor(self):
        class ShortRead(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 1))

        source = ShortRead(archive_bytes())
        source.seek(3)
        with self.assertRaises(PreflightError):
            preflight_zip(source)
        self.assertEqual(source.tell(), 3)
        source = io.BytesIO(archive_bytes())
        source.seek(5)
        with mock.patch.object(zipfile, "_EndRecData", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                preflight_zip(source)
        self.assertEqual(source.tell(), 5)
        self.assertFalse(source.closed)

    def test_all_reads_are_bounded_without_reading_member_payload(self):
        class RecordingSource(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.sizes = []

            def read(self, size=-1):
                self.sizes.append(size)
                return super().read(size)

        source = RecordingSource(archive_bytes())
        result = preflight_zip(source)
        self.assertTrue(all(0 <= size <= 65557 for size in source.sizes))
        self.assertEqual(source.sizes.count(zipfile.sizeCentralDir), result["entries_actual"])
        self.assertEqual(source.tell(), 0)

    def test_argument_validation(self):
        for name, values in (("max_entries", (-1, True, 2.5)),
                             ("max_metadata_bytes", (0, -1, True, 2.5)),
                             ("deadline", (float("inf"), float("nan"), True, "1"))):
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.check(**{name: value})


if __name__ == "__main__":
    unittest.main()

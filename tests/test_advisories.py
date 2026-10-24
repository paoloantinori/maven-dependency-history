"""Tests for mdi.advisories: OSV query construction and recorded-response mapping.

Fixtures are minimal factual extracts of the public OSV response for
org.apache.logging.log4j:log4j-core (saved probe
osv-log4j-response.json, 2026-09-10): advisory ids, aliases, branch
intervals and version lists are copied verbatim from that response.
"""

import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "maven-dependency-investigator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.advisories import (
    FUZZY_VERSION_CAVEAT,
    OSV_QUERY_ENDPOINT,
    AdvisoryError,
    AdvisoryLimits,
    build_query,
    map_advisory,
    map_response,
    transport_error_result,
)

GROUP = "org.apache.logging.log4j"
ARTIFACT = "log4j-core"
VERSION = "2.14.1"
RETRIEVED = "2026-09-12T08:30:00.000000Z"

GAV_QUERY = {
    "package": {"name": "org.apache.logging.log4j:log4j-core", "ecosystem": "Maven"},
    "version": "2.14.1",
}

ADVISORY_URL = (
    "https://github.com/github/advisory-database/blob/main/advisories/github-reviewed/"
    "2021/12/GHSA-jfh8-c2jp-5v3q/GHSA-jfh8-c2jp-5v3q.json"
)

# Factual extract of GHSA-jfh8-c2jp-5v3q (CVE-2021-44228): the three real
# branch intervals for the queried package, plus affected entries for two
# other Maven packages (one ranges-based, one versions-only), real top-level
# metadata, and unknown extra fields that must be ignored.
LOG4SHELL_PAGE = {
    "vulns": [
        {
            "id": "GHSA-jfh8-c2jp-5v3q",
            "aliases": ["CVE-2021-44228"],
            "modified": "2025-10-22T19:37:02.616807Z",
            "published": "2021-12-10T00:40:56Z",
            "summary": "Remote code injection in Log4j",
            "details": "JNDI features used in configuration, log messages, and parameters do not protect against attacker controlled LDAP and other JNDI related endpoints.",
            "severity": [
                {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H/E:H"}
            ],
            "references": [
                {"type": "ADVISORY", "url": "https://nvd.nist.gov/vuln/detail/CVE-2021-44228"},
                {"type": "PACKAGE", "url": "https://github.com/apache/logging-log4j2"},
            ],
            "database_specific": {
                "github_reviewed": True,
                "severity": "CRITICAL",
                "cwe_ids": ["CWE-20", "CWE-502"],
            },
            "schema_version": "1.9.0",
            "affected": [
                {
                    "package": {
                        "name": "org.apache.logging.log4j:log4j-core",
                        "ecosystem": "Maven",
                        "purl": "pkg:maven/org.apache.logging.log4j/log4j-core",
                    },
                    "ranges": [
                        {"type": "ECOSYSTEM", "events": [{"introduced": "2.13.0"}, {"fixed": "2.15.0"}]}
                    ],
                    "versions": ["2.13.0", "2.13.1", "2.13.2", "2.13.3", "2.14.0", "2.14.1"],
                    "database_specific": {"source": ADVISORY_URL},
                },
                {
                    "package": {
                        "name": "org.apache.logging.log4j:log4j-core",
                        "ecosystem": "Maven",
                        "purl": "pkg:maven/org.apache.logging.log4j/log4j-core",
                    },
                    "ranges": [
                        {"type": "ECOSYSTEM", "events": [{"introduced": "2.0-beta9"}, {"fixed": "2.3.1"}]}
                    ],
                    "versions": ["2.0", "2.0-beta9", "2.0-rc1", "2.0-rc2", "2.0.1", "2.0.2", "2.1", "2.2", "2.3"],
                    "database_specific": {"source": ADVISORY_URL},
                },
                {
                    "package": {
                        "name": "org.apache.logging.log4j:log4j-core",
                        "ecosystem": "Maven",
                        "purl": "pkg:maven/org.apache.logging.log4j/log4j-core",
                    },
                    "ranges": [
                        {"type": "ECOSYSTEM", "events": [{"introduced": "2.4"}, {"fixed": "2.12.2"}]}
                    ],
                    "versions": ["2.4", "2.4.1", "2.5", "2.12.1"],
                    "database_specific": {"source": ADVISORY_URL},
                },
                {
                    "package": {
                        "name": "com.guicedee.services:log4j-core",
                        "ecosystem": "Maven",
                        "purl": "pkg:maven/com.guicedee.services/log4j-core",
                    },
                    "ranges": [
                        {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"last_affected": "1.2.1.2-jre17"}]}
                    ],
                    "versions": ["1.0.10.0", "1.1.1.1-jre14"],
                    "database_specific": {"source": ADVISORY_URL},
                },
                {
                    "package": {
                        "name": "org.xbib.elasticsearch:log4j",
                        "ecosystem": "Maven",
                        "purl": "pkg:maven/org.xbib.elasticsearch/log4j",
                    },
                    "versions": ["6.3.2.1"],
                    "database_specific": {"source": ADVISORY_URL},
                },
            ],
        }
    ]
}

PAGE_BYTES = json.dumps(LOG4SHELL_PAGE).encode()


def minimal_advisory_page():
    return {
        "vulns": [
            {
                "id": "GHSA-minimal",
                "affected": [
                    {
                        "package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "Maven"},
                        "ranges": [
                            {"type": "ECOSYSTEM", "events": [{"introduced": "1.0.0"}, {"fixed": "1.0.1"}]}
                        ],
                    }
                ],
            }
        ]
    }


class NoIoSpy:
    """Context manager failing the test if files or sockets are touched."""

    def __enter__(self):
        self._open = mock.patch("builtins.open")
        self._socket = mock.patch("socket.socket")
        fake_open = self._open.__enter__()
        fake_socket = self._socket.__enter__()
        self.fakes = (fake_open, fake_socket)
        return self

    def __exit__(self, *exc):
        for fake in self.fakes:
            fake.assert_not_called()
        self._open.__exit__(*exc)
        self._socket.__exit__(*exc)
        return False


class BuildQueryTest(unittest.TestCase):
    def test_exact_query_shape(self):
        self.assertEqual(build_query(GROUP, ARTIFACT, VERSION), GAV_QUERY)

    def test_no_page_token_key_unless_given(self):
        self.assertNotIn("page_token", build_query(GROUP, ARTIFACT, VERSION))

    def test_followup_token_round_trips_opaque(self):
        token = "YmFzZTY0L3Rva2Vu=="
        query = build_query(GROUP, ARTIFACT, VERSION, page_token=token)
        self.assertEqual(
            query,
            {**GAV_QUERY, "page_token": "YmFzZTY0L3Rva2Vu=="},
        )
        self.assertEqual(query["page_token"], token)

    def test_rejects_non_string_coordinates(self):
        for bad in (None, 12, b"log4j-core", ["g"], {"g": "a"}):
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    build_query(bad, ARTIFACT, VERSION)
                with self.assertRaises(TypeError):
                    build_query(GROUP, bad, VERSION)
                with self.assertRaises(TypeError):
                    build_query(GROUP, ARTIFACT, bad)

    def test_rejects_blank_coordinates(self):
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(AdvisoryError):
                    build_query(bad, ARTIFACT, VERSION)
                with self.assertRaises(AdvisoryError):
                    build_query(GROUP, bad, VERSION)
                with self.assertRaises(AdvisoryError):
                    build_query(GROUP, ARTIFACT, bad)

    def test_rejects_placeholder_coordinates(self):
        for bad in ("${project.groupId}", "org.${x}", "${version}"):
            with self.subTest(bad=bad):
                with self.assertRaises(AdvisoryError):
                    build_query(bad, ARTIFACT, VERSION)
                with self.assertRaises(AdvisoryError):
                    build_query(GROUP, ARTIFACT, bad)

    def test_rejects_whitespace_inside_coordinates(self):
        for bad in (" org.apache", "org .apache", "log4j core", "2.14.1 "):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(AdvisoryError):
                    build_query(GROUP, ARTIFACT, bad)
                with self.assertRaises(AdvisoryError):
                    build_query(bad, ARTIFACT, VERSION)

    def test_rejects_bad_page_token(self):
        for bad in (5, "", "  ", "to ken", "${next}"):
            with self.subTest(bad=bad):
                with self.assertRaises((TypeError, AdvisoryError)):
                    build_query(GROUP, ARTIFACT, VERSION, page_token=bad)

    def test_rejects_oversized_coordinates_and_token(self):
        limits = AdvisoryLimits(max_string_length=8)
        with self.assertRaises(AdvisoryError):
            build_query(GROUP, ARTIFACT, VERSION, limits=limits)
        with self.assertRaises(AdvisoryError):
            build_query(GROUP, ARTIFACT, VERSION, page_token="x" * 9, limits=limits)

    def test_fresh_dict_per_call(self):
        first = build_query(GROUP, ARTIFACT, VERSION)
        second = build_query(GROUP, ARTIFACT, VERSION)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["version"] = "mutated"
        self.assertNotEqual(first["version"], second["version"])


class AdvisoryLimitsTest(unittest.TestCase):
    def test_defaults(self):
        limits = AdvisoryLimits()
        self.assertEqual(limits.response_bytes, 1024 * 1024)
        self.assertEqual(limits.max_advisories, 1000)
        self.assertEqual(limits.max_affected_per_advisory, 100)

    def test_frozen(self):
        with self.assertRaises(Exception):
            AdvisoryLimits().max_advisories = 5

    def test_rejects_non_positive(self):
        for field in ("response_bytes", "max_advisories", "max_string_length"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    AdvisoryLimits(**{field: 0})
                with self.assertRaises(ValueError):
                    AdvisoryLimits(**{field: -1})

    def test_rejects_bool_and_non_int(self):
        for field in ("response_bytes", "max_advisories", "max_string_length"):
            with self.subTest(field=field):
                for bad in (True, "5", 1.0, None):
                    with self.assertRaises(TypeError):
                        AdvisoryLimits(**{field: bad})


class MapSavedShapeTest(unittest.TestCase):
    """The factual CVE-2021-44228 extract, mapped end to end."""

    def setUp(self):
        with NoIoSpy():
            self.result = map_response(PAGE_BYTES, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_page_status_and_provenance(self):
        self.assertEqual(self.result["status"], "matches")
        self.assertEqual(self.result["provider"], "OSV")
        self.assertEqual(self.result["query"], GAV_QUERY)
        self.assertEqual(self.result["retrieved_at"], RETRIEVED)
        self.assertIs(self.result["provider_reported"], True)
        self.assertIn("fuzzy", self.result["caveat"])
        self.assertEqual(self.result["caveat"], FUZZY_VERSION_CAVEAT)
        self.assertEqual(self.result["source_url"], OSV_QUERY_ENDPOINT)
        self.assertIsNone(self.result["next_page_token"])
        self.assertEqual(self.result["page_scope"], "single-response-page")
        self.assertIs(self.result["is_continuation"], False)
        self.assertIs(self.result["more_pages"], False)

    def test_counts_declare_exclusions(self):
        counts = self.result["counts"]
        self.assertEqual(counts["returned_advisories"], 1)
        self.assertEqual(counts["mapped_advisories"], 1)
        self.assertEqual(counts["excluded_affected_entries"], 2)

    def test_advisory_top_level_fields_kept(self):
        (advisory,) = self.result["advisories"]
        self.assertEqual(advisory["id"], "GHSA-jfh8-c2jp-5v3q")
        self.assertEqual(advisory["aliases"], ["CVE-2021-44228"])
        self.assertEqual(advisory["modified"], "2025-10-22T19:37:02.616807Z")
        self.assertEqual(advisory["published"], "2021-12-10T00:40:56Z")
        self.assertEqual(advisory["summary"], "Remote code injection in Log4j")
        self.assertEqual(
            advisory["severity"],
            [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H/E:H"}],
        )
        self.assertEqual(
            advisory["references"],
            [
                {"type": "ADVISORY", "url": "https://nvd.nist.gov/vuln/detail/CVE-2021-44228"},
                {"type": "PACKAGE", "url": "https://github.com/apache/logging-log4j2"},
            ],
        )
        self.assertEqual(advisory["excluded_affected_count"], 2)
        self.assertIs(advisory["provider_reported"], True)

    def test_unknown_fields_ignored_with_explicit_selection(self):
        (advisory,) = self.result["advisories"]
        for dropped in ("details", "schema_version", "database_specific"):
            self.assertNotIn(dropped, advisory)
        self.assertEqual(
            self.result["advisory_fields_preserved"],
            ["id", "aliases", "modified", "published", "summary", "severity", "references", "affected"],
        )

    def test_all_three_branch_intervals_retained_unflattened(self):
        (advisory,) = self.result["advisories"]
        affected = advisory["affected"]
        self.assertEqual(len(affected), 3)
        intervals = [
            (entry["ranges"][0]["events"][0]["introduced"], entry["ranges"][0]["events"][1]["fixed"])
            for entry in affected
        ]
        self.assertEqual(
            intervals,
            [("2.13.0", "2.15.0"), ("2.0-beta9", "2.3.1"), ("2.4", "2.12.2")],
        )
        # Entries stay separate objects: same package, distinct intervals.
        self.assertEqual(len({id(entry) for entry in affected}), 3)

    def test_events_and_versions_preserved_verbatim(self):
        (advisory,) = self.result["advisories"]
        first = advisory["affected"][0]
        self.assertEqual(
            first["ranges"],
            [{"type": "ECOSYSTEM", "events": [{"introduced": "2.13.0"}, {"fixed": "2.15.0"}]}],
        )
        self.assertEqual(first["versions"], ["2.13.0", "2.13.1", "2.13.2", "2.13.3", "2.14.0", "2.14.1"])
        self.assertIn("2.0-beta9", advisory["affected"][1]["versions"])
        self.assertEqual(
            first["package"],
            {
                "name": "org.apache.logging.log4j:log4j-core",
                "ecosystem": "Maven",
                "purl": "pkg:maven/org.apache.logging.log4j/log4j-core",
            },
        )
        self.assertEqual(first["database_specific"], {"source": ADVISORY_URL})
        self.assertIsNone(first["ecosystem_specific"])

    def test_no_other_package_applicability_in_result(self):
        (advisory,) = self.result["advisories"]
        names = {entry["package"]["name"] for entry in advisory["affected"]}
        self.assertEqual(names, {"org.apache.logging.log4j:log4j-core"})
        self.assertNotIn("com.guicedee.services:log4j-core", json.dumps(self.result["advisories"]))
        self.assertNotIn("org.xbib.elasticsearch:log4j", json.dumps(self.result["advisories"]))

    def test_evidence_block(self):
        (advisory,) = self.result["advisories"]
        self.assertEqual(
            advisory["evidence"],
            {
                "id": "GHSA-jfh8-c2jp-5v3q",
                "source_url": OSV_QUERY_ENDPOINT,
                "provider": "OSV",
                "query": GAV_QUERY,
                "modified": "2025-10-22T19:37:02.616807Z",
                "retrieved_at": RETRIEVED,
            },
        )

    def test_result_is_json_safe(self):
        encoded = json.dumps(self.result, allow_nan=False)
        self.assertEqual(json.loads(encoded), self.result)


class PageStatusTest(unittest.TestCase):
    def test_empty_object_is_no_known_matches(self):
        with NoIoSpy():
            result = map_response(b"{}", GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(result["status"], "no-known-matches")
        self.assertEqual(result["advisories"], [])
        self.assertIsNone(result["next_page_token"])
        self.assertEqual(result["counts"], {"returned_advisories": 0, "mapped_advisories": 0, "excluded_affected_entries": 0})

    def test_empty_vulns_list_is_no_known_matches(self):
        result = map_response(b'{"vulns": []}', GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(result["status"], "no-known-matches")

    def test_continuation_makes_page_partial(self):
        page = dict(minimal_advisory_page(), next_page_token="cGFnZS0y")
        result = map_response(json.dumps(page).encode(), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["next_page_token"], "cGFnZS0y")
        self.assertEqual(len(result["advisories"]), 1)

    def test_token_only_page_is_partial_not_success(self):
        result = map_response(b'{"next_page_token": "cGFnZS0y"}', GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["advisories"], [])
        self.assertEqual(result["next_page_token"], "cGFnZS0y")

    def test_followup_query_uses_same_identity_and_token(self):
        page = dict(minimal_advisory_page(), next_page_token="cGFnZS0y")
        result = map_response(json.dumps(page).encode(), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        followup = build_query(GROUP, ARTIFACT, VERSION, page_token=result["next_page_token"])
        self.assertEqual(followup["package"], GAV_QUERY["package"])
        self.assertEqual(followup["version"], VERSION)
        self.assertEqual(followup["page_token"], "cGFnZS0y")

    def test_blank_next_page_token_rejected(self):
        for bad in (b'{"next_page_token": ""}', b'{"next_page_token": " "}', b'{"next_page_token": 5}'):
            with self.subTest(bad=bad):
                with self.assertRaises(AdvisoryError):
                    map_response(bad, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)


class RequestPageTokenTest(unittest.TestCase):
    """Mapping a continuation page must record the query actually sent."""

    TOKEN = "cGFnZS0y"

    def page2(self, page=None):
        body = page if page is not None else minimal_advisory_page()
        with NoIoSpy():
            return map_response(
                json.dumps(body).encode(),
                GROUP,
                ARTIFACT,
                VERSION,
                request_page_token=self.TOKEN,
                retrieved_at=RETRIEVED,
            )

    def test_request_token_in_result_query_and_exact_gav_preserved(self):
        result = self.page2()
        self.assertEqual(
            result["query"],
            {**GAV_QUERY, "page_token": self.TOKEN},
        )
        self.assertEqual(result["query"]["package"], GAV_QUERY["package"])
        self.assertEqual(result["query"]["version"], VERSION)
        self.assertEqual(result["query"]["package"]["ecosystem"], "Maven")

    def test_request_token_in_every_advisory_evidence_query(self):
        result = self.page2()
        (advisory,) = result["advisories"]
        self.assertEqual(
            advisory["evidence"]["query"],
            {**GAV_QUERY, "page_token": self.TOKEN},
        )
        self.assertEqual(advisory["evidence"]["retrieved_at"], RETRIEVED)

    def test_page2_metadata_marks_continuation(self):
        result = self.page2()
        self.assertIs(result["is_continuation"], True)
        self.assertIs(result["more_pages"], False)
        self.assertEqual(result["page_scope"], "single-response-page")
        self.assertEqual(result["status"], "matches")

    def test_continuation_with_next_token_is_partial(self):
        result = self.page2(dict(minimal_advisory_page(), next_page_token="cGFnZS0z"))
        self.assertEqual(result["status"], "partial")
        self.assertIs(result["is_continuation"], True)
        self.assertIs(result["more_pages"], True)
        self.assertEqual(result["next_page_token"], "cGFnZS0z")

    def test_empty_final_continuation_page_is_page_scoped_not_query_empty(self):
        # Last page of a paginated query, empty: the record must never read
        # as "the whole query has no vulnerabilities" without aggregation.
        result = self.page2({})
        self.assertEqual(result["status"], "no-known-matches")
        self.assertIs(result["is_continuation"], True)
        self.assertIs(result["more_pages"], False)
        self.assertEqual(result["page_scope"], "single-response-page")
        self.assertEqual(result["counts"]["returned_advisories"], 0)
        self.assertEqual(result["query"], {**GAV_QUERY, "page_token": self.TOKEN})

    def test_first_page_without_request_token_is_not_continuation(self):
        with NoIoSpy():
            result = map_response(b"{}", GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertIs(result["is_continuation"], False)
        self.assertIs(result["more_pages"], False)
        self.assertEqual(result["query"], GAV_QUERY)

    def test_invalid_request_token_rejected(self):
        for bad in (5, "", "  ", "to ken", "${next}", "x" * 33):
            with self.subTest(bad=bad):
                with self.assertRaises((TypeError, AdvisoryError)):
                    map_response(
                        b"{}",
                        GROUP,
                        ARTIFACT,
                        VERSION,
                        request_page_token=bad,
                        retrieved_at=RETRIEVED,
                        limits=AdvisoryLimits(max_string_length=32),
                    )
                with self.assertRaises((TypeError, AdvisoryError)):
                    map_advisory(
                        minimal_advisory_page()["vulns"][0],
                        GROUP,
                        ARTIFACT,
                        VERSION,
                        request_page_token=bad,
                        retrieved_at=RETRIEVED,
                        limits=AdvisoryLimits(max_string_length=32),
                    )

    def test_map_advisory_request_token_in_evidence(self):
        raw = minimal_advisory_page()["vulns"][0]
        record = map_advisory(raw, GROUP, ARTIFACT, VERSION, request_page_token=self.TOKEN, retrieved_at=RETRIEVED)
        self.assertEqual(record["evidence"]["query"], {**GAV_QUERY, "page_token": self.TOKEN})

    def test_no_input_mutation_with_request_token(self):
        raw = copy.deepcopy(LOG4SHELL_PAGE["vulns"][0])
        before = copy.deepcopy(raw)
        with NoIoSpy():
            map_advisory(raw, GROUP, ARTIFACT, VERSION, request_page_token=self.TOKEN, retrieved_at=RETRIEVED)
        self.assertEqual(raw, before)


class PositionalCompatTest(unittest.TestCase):
    """The token parameter must not have shifted the original arg slots."""

    def test_old_positional_retrieved_at_still_lands_in_retrieved_at(self):
        with NoIoSpy():
            result = map_response(PAGE_BYTES, GROUP, ARTIFACT, VERSION, RETRIEVED)
        self.assertEqual(result["retrieved_at"], RETRIEVED)
        self.assertEqual(result["query"], GAV_QUERY)
        self.assertIs(result["is_continuation"], False)
        (advisory,) = result["advisories"]
        self.assertEqual(advisory["evidence"]["retrieved_at"], RETRIEVED)
        self.assertEqual(advisory["evidence"]["query"], GAV_QUERY)

    def test_old_positional_limits_still_lands_in_limits(self):
        # A timestamp in slot 5 and limits in slot 6: the token must not
        # have stolen either slot.
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(
                PAGE_BYTES,
                GROUP,
                ARTIFACT,
                VERSION,
                RETRIEVED,
                AdvisoryLimits(response_bytes=8),
            )
        self.assertEqual(ctx.exception.code, "response-too-large")
        result = map_response(
            PAGE_BYTES,
            GROUP,
            ARTIFACT,
            VERSION,
            RETRIEVED,
            AdvisoryLimits(response_bytes=len(PAGE_BYTES)),
        )
        self.assertEqual(result["retrieved_at"], RETRIEVED)

    def test_request_token_is_keyword_only(self):
        for bad_call in (
            lambda: map_response(b"{}", GROUP, ARTIFACT, VERSION, RETRIEVED, AdvisoryLimits(), "tok"),
            lambda: map_advisory(
                minimal_advisory_page()["vulns"][0], GROUP, ARTIFACT, VERSION, RETRIEVED, AdvisoryLimits(), "tok"
            ),
        ):
            with self.subTest(bad_call=bad_call):
                with self.assertRaises(TypeError):
                    bad_call()

    def test_existing_keyword_callers_still_work(self):
        with NoIoSpy():
            result = map_response(
                b"{}",
                GROUP,
                ARTIFACT,
                VERSION,
                retrieved_at=RETRIEVED,
                limits=AdvisoryLimits(),
                request_page_token="dG9rZW4=",
            )
        self.assertEqual(result["query"], {**GAV_QUERY, "page_token": "dG9rZW4="})
        self.assertIs(result["is_continuation"], True)


class IdentityMismatchTest(unittest.TestCase):
    """A response whose advisories never mention the queried package is refused."""

    def test_other_packages_only_is_refused_not_no_matches(self):
        page = {
            "vulns": [
                {
                    "id": "GHSA-other",
                    "affected": [
                        {
                            "package": {"name": "com.guicedee.services:log4j-core", "ecosystem": "Maven"},
                            "versions": ["1.0.10.0"],
                        }
                    ],
                }
            ]
        }
        with NoIoSpy():
            with self.assertRaises(AdvisoryError) as ctx:
                map_response(json.dumps(page).encode(), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(ctx.exception.code, "identity-not-in-advisory")
        self.assertIn("GHSA-other", str(ctx.exception))

    def test_same_name_other_ecosystem_is_not_relevant(self):
        page = {
            "vulns": [
                {
                    "id": "GHSA-pypi",
                    "affected": [
                        {
                            "package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "PyPI"},
                            "versions": ["2.14.1"],
                        }
                    ],
                }
            ]
        }
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(json.dumps(page).encode(), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(ctx.exception.code, "identity-not-in-advisory")

    def test_advisory_without_affected_is_refused(self):
        page = {"vulns": [{"id": "GHSA-bare", "summary": "no affected block"}]}
        with self.assertRaises(AdvisoryError):
            map_response(json.dumps(page).encode(), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_blank_identity_refused_regardless_of_page(self):
        for bad_identity in ((("", ARTIFACT, VERSION)), ((GROUP, "", VERSION)), ((GROUP, ARTIFACT, ""))):
            with self.subTest(bad_identity=bad_identity):
                with self.assertRaises(AdvisoryError):
                    map_response(b"{}", *bad_identity, retrieved_at=RETRIEVED)

    def test_non_string_identity_refused(self):
        with self.assertRaises(TypeError):
            map_response(b"{}", None, ARTIFACT, VERSION, retrieved_at=RETRIEVED)


class MalformedPageTest(unittest.TestCase):
    def test_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            map_response("{}", GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_rejects_invalid_utf8(self):
        with NoIoSpy():
            with self.assertRaises(AdvisoryError) as ctx:
                map_response(b'{"vulns": "\xff\xfe"}', GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(ctx.exception.code, "invalid-utf8")

    def test_rejects_duplicate_keys(self):
        body = b'{"vulns": [], "vulns": []}'
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(ctx.exception.code, "duplicate-key")

    def test_rejects_duplicate_keys_deep_in_advisory(self):
        body = b'{"vulns": [{"id": "A", "id": "B"}]}'
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(ctx.exception.code, "duplicate-key")

    def test_rejects_nonfinite_numbers(self):
        for body in (
            b'{"vulns": [{"id": "A", "details": NaN}]}',
            b'{"vulns": [{"id": "A", "details": Infinity}]}',
            b'{"vulns": [{"id": "A", "details": 1e999}]}',
        ):
            with self.subTest(body=body):
                with self.assertRaises(AdvisoryError) as ctx:
                    map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
                self.assertEqual(ctx.exception.code, "non-finite-number")

    def test_rejects_malformed_json(self):
        for body in (b"{", b"not json", b'{"vulns": ', b"\x00{}"):
            with self.subTest(body=body):
                with self.assertRaises(AdvisoryError):
                    map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_rejects_non_object_top_level(self):
        for body in (b"[]", b'"vulns"', b"null", b"3"):
            with self.subTest(body=body):
                with self.assertRaises(AdvisoryError):
                    map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_rejects_unknown_outer_keys(self):
        body = b'{"vulns": [], "results": []}'
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(ctx.exception.code, "unexpected-key")

    def test_rejects_wrong_outer_types(self):
        for body in (b'{"vulns": 3}', b'{"vulns": {}}', b'{"vulns": [1]}', b'{"vulns": [null]}'):
            with self.subTest(body=body):
                with self.assertRaises(AdvisoryError):
                    map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_rejects_advisory_with_bad_id(self):
        for body in (
            b'{"vulns": [{"aliases": ["CVE-1"]}]}',
            b'{"vulns": [{"id": ""}]}',
            b'{"vulns": [{"id": 7}]}',
        ):
            with self.subTest(body=body):
                with self.assertRaises(AdvisoryError):
                    map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_rejects_wrong_advisory_field_types(self):
        base = {"vulns": [{"id": "GHSA-x", "affected": minimal_advisory_page()["vulns"][0]["affected"]}]}
        for field, bad in (
            ("aliases", "CVE-1"),
            ("aliases", [1]),
            ("modified", 2025),
            ("summary", ["s"]),
            ("severity", "HIGH"),
            ("severity", [3]),
            ("severity", [{"type": 3, "score": "x"}]),
            ("references", "https://x"),
            ("references", [{"type": "WEB", "url": 5}]),
            ("references", [{"type": "WEB"}]),
        ):
            with self.subTest(field=field, bad=bad):
                page = copy.deepcopy(base)
                page["vulns"][0][field] = bad
                with self.assertRaises(AdvisoryError):
                    map_response(json.dumps(page).encode(), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_rejects_wrong_affected_shapes(self):
        def page_with_affected(affected):
            return json.dumps({"vulns": [{"id": "GHSA-x", "affected": affected}]}).encode()

        good = minimal_advisory_page()["vulns"][0]["affected"]
        for bad in (
            "not-a-list",
            [None],
            [{"package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "Maven"}, "ranges": "ECOSYSTEM"}],
            [{"package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "Maven"}, "ranges": [{"type": 1, "events": []}]}],
            [{"package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "Maven"}, "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": 17}]}]}],
            [{"package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "Maven"}, "versions": [2]}],
            [{"package": {"ecosystem": "Maven"}}],
            [{"package": {"name": f"{GROUP}:{ARTIFACT}"}}],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(AdvisoryError):
                    map_response(page_with_affected(bad), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        # Sanity: the good shape still maps.
        with NoIoSpy():
            map_response(page_with_affected(good), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)


class BoundsTest(unittest.TestCase):
    def page(self, **advisory_extra):
        advisory = {
            "id": "GHSA-x",
            "affected": minimal_advisory_page()["vulns"][0]["affected"],
        }
        advisory.update(advisory_extra)
        return json.dumps({"vulns": [advisory]}).encode()

    def test_oversize_response_bytes(self):
        with NoIoSpy():
            with self.assertRaises(AdvisoryError) as ctx:
                map_response(PAGE_BYTES, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED, limits=AdvisoryLimits(response_bytes=8))
        self.assertEqual(ctx.exception.code, "response-too-large")

    def test_exact_limit_accepted(self):
        with NoIoSpy():
            map_response(PAGE_BYTES, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED, limits=AdvisoryLimits(response_bytes=len(PAGE_BYTES)))

    def test_too_many_advisories(self):
        page = json.dumps({"vulns": [dict(minimal_advisory_page()["vulns"][0], id=f"GHSA-{i}") for i in range(3)]}).encode()
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(page, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED, limits=AdvisoryLimits(max_advisories=2))
        self.assertEqual(ctx.exception.code, "too-many-advisories")

    def test_too_many_relevant_affected_entries(self):
        entry = minimal_advisory_page()["vulns"][0]["affected"][0]
        page = json.dumps({"vulns": [{"id": "GHSA-x", "affected": [copy.deepcopy(entry), copy.deepcopy(entry)]}]}).encode()
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(page, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED, limits=AdvisoryLimits(max_affected_per_advisory=1))
        self.assertEqual(ctx.exception.code, "too-many-affected-entries")

    def test_too_many_references(self):
        refs = [{"type": "WEB", "url": f"https://x/{i}"} for i in range(3)]
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(self.page(references=refs), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED, limits=AdvisoryLimits(max_references_per_advisory=2))
        self.assertEqual(ctx.exception.code, "too-many-references")

    def test_too_many_versions(self):
        affected = [{
            "package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "Maven"},
            "versions": ["1.0.0", "1.0.1", "1.0.2"],
        }]
        page = json.dumps({"vulns": [{"id": "GHSA-x", "affected": affected}]}).encode()
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(page, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED, limits=AdvisoryLimits(max_versions_per_affected=2))
        self.assertEqual(ctx.exception.code, "too-many-versions")

    def test_oversize_string_field(self):
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(self.page(summary="x" * 33), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED, limits=AdvisoryLimits(max_string_length=32))
        self.assertEqual(ctx.exception.code, "string-too-long")

    def test_oversize_token_in_recorded_page(self):
        page = json.dumps({"next_page_token": "t" * 33}).encode()
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(page, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED, limits=AdvisoryLimits(max_string_length=32))
        self.assertEqual(ctx.exception.code, "string-too-long")


class DeepNestingTest(unittest.TestCase):
    def _nested_page(self, depth):
        value = 0
        for _ in range(depth):
            value = [value]
        advisory = {
            "id": "GHSA-deep",
            "affected": [{
                "package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "Maven"},
                "database_specific": {"nested": value},
            }],
        }
        return json.dumps({"vulns": [advisory]}).encode()

    def test_moderate_nesting_accepted(self):
        with NoIoSpy():
            result = map_response(self._nested_page(4), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(result["status"], "matches")

    def test_deep_nesting_rejected_before_recursion_blows_up(self):
        with self.assertRaises(AdvisoryError) as ctx:
            map_response(self._nested_page(200), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(ctx.exception.code, "too-deeply-nested")

    def test_parser_level_bomb_rejected(self):
        body = b'{"vulns": [{"id": "A", "database_specific": ' + b"[" * 100000 + b"1" + b"]" * 100000 + b"}]}"
        with self.assertRaises(AdvisoryError):
            map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)


class IntegerDigitLimitTest(unittest.TestCase):
    """json.loads raises plain ValueError past CPython's int digit limit."""

    def _page_with_int(self, digits: int) -> bytes:
        number = b"9" * digits
        body = b'{"vulns": [{"id": "GHSA-x", "affected": [{"package": {"name": "'
        body += f"{GROUP}:{ARTIFACT}".encode()
        body += b'", "ecosystem": "Maven"}, "database_specific": {"n": ' + number + b"}}]}]}"
        return body

    def test_digit_bomb_within_byte_cap_rejected_with_stable_code(self):
        body = self._page_with_int(5000)
        # The bomb is small: the byte cap is not what stops it.
        self.assertLess(len(body), 1024 * 1024)
        with NoIoSpy():
            with self.assertRaises(AdvisoryError) as ctx:
                map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(ctx.exception.code, "integer-too-large")

    def test_runtime_digit_limit_variation_normalized_and_restored(self):
        # The interpreter's digit threshold varies by version/config: pin it
        # low, prove the refusal is still a coded AdvisoryError, then restore.
        original = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(640)
            # 700 digits: under our max_string_length hook bound (4096), so
            # only the interpreter limit can refuse it.
            body = self._page_with_int(700)
            with self.assertRaises(AdvisoryError) as ctx:
                map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
            self.assertEqual(ctx.exception.code, "integer-too-large")
        finally:
            sys.set_int_max_str_digits(original)
        self.assertEqual(sys.get_int_max_str_digits(), original)

    def test_normal_integers_still_map(self):
        body = self._page_with_int(4)
        with NoIoSpy():
            result = map_response(body, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(result["status"], "matches")
        self.assertEqual(
            result["advisories"][0]["affected"][0]["database_specific"], {"n": 9999}
        )

    def test_bools_and_finite_floats_in_specifics_preserved(self):
        page = {
            "vulns": [
                {
                    "id": "GHSA-types",
                    "affected": [
                        {
                            "package": {"name": f"{GROUP}:{ARTIFACT}", "ecosystem": "Maven"},
                            "database_specific": {
                                "flag": True,
                                "off": False,
                                "ratio": 0.5,
                                "count": 3,
                                "none": None,
                            },
                        }
                    ],
                }
            ]
        }
        result = map_response(json.dumps(page).encode(), GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(
            result["advisories"][0]["affected"][0]["database_specific"],
            {"flag": True, "off": False, "ratio": 0.5, "count": 3, "none": None},
        )
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)


class RetrievedAtTest(unittest.TestCase):
    def test_default_injects_actual_utc_per_invocation(self):
        with NoIoSpy():
            first = map_response(b"{}", GROUP, ARTIFACT, VERSION)
            second = map_response(b"{}", GROUP, ARTIFACT, VERSION)
        for result in (first, second):
            stamp = result["retrieved_at"]
            self.assertIsInstance(stamp, str)
            self.assertTrue(stamp.endswith("Z"), stamp)
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(parsed))
        self.assertNotEqual(first["retrieved_at"], second["retrieved_at"])

    def test_invalid_retrieved_at_rejected(self):
        for bad in (5, "not-a-timestamp", "2026-13-45T99:99:99Z"):
            with self.subTest(bad=bad):
                with self.assertRaises(AdvisoryError):
                    map_response(b"{}", GROUP, ARTIFACT, VERSION, retrieved_at=bad)


class MapAdvisoryTest(unittest.TestCase):
    def test_does_not_mutate_input(self):
        raw = copy.deepcopy(LOG4SHELL_PAGE["vulns"][0])
        before = copy.deepcopy(raw)
        with NoIoSpy():
            map_advisory(raw, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertEqual(raw, before)

    def test_rejects_non_dict(self):
        with self.assertRaises(AdvisoryError):
            map_advisory(["not", "a", "dict"], GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)

    def test_optional_fields_absent_become_none(self):
        raw = minimal_advisory_page()["vulns"][0]
        record = map_advisory(raw, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
        self.assertIsNone(record["aliases"])
        self.assertIsNone(record["modified"])
        self.assertIsNone(record["published"])
        self.assertIsNone(record["summary"])
        self.assertIsNone(record["severity"])
        self.assertIsNone(record["references"])
        # affected entry without versions keeps None, not a fabricated list
        self.assertIsNone(record["affected"][0]["versions"])


class TransportErrorResultTest(unittest.TestCase):
    def test_distinct_status_and_fields(self):
        with NoIoSpy():
            result = transport_error_result(GROUP, ARTIFACT, VERSION, RuntimeError("connection reset"))
        self.assertEqual(result["status"], "transport-error")
        self.assertEqual(result["provider"], "OSV")
        self.assertEqual(result["query"], GAV_QUERY)
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertIn("connection reset", result["error"])
        self.assertEqual(result["advisories"], [])
        self.assertIn("retrieved_at", result)

    def test_status_disjoint_from_page_statuses(self):
        error_result = transport_error_result(GROUP, ARTIFACT, VERSION, OSError("boom"))
        page_statuses = {"matches", "no-known-matches", "partial"}
        self.assertNotIn(error_result["status"], page_statuses)

    def test_no_exception_swallowed_here(self):
        # The helper builds a record from an exception; it never catches one.
        with self.assertRaises(ZeroDivisionError):
            raise ZeroDivisionError("proof the module does not trap caller exceptions")

    def test_failed_continuation_carries_exact_token_and_timestamp(self):
        exc = RuntimeError("connection reset mid-page")
        token = "cGFnZS0y"
        result = transport_error_result(
            GROUP,
            ARTIFACT,
            VERSION,
            exc,
            retrieved_at=RETRIEVED,
            request_page_token=token,
        )
        self.assertEqual(result["query"], {**GAV_QUERY, "page_token": token})
        self.assertIs(result["is_continuation"], True)
        self.assertEqual(result["page_scope"], "attempted-single-response-page")
        # A dead transport knows nothing about further pages: unknown, never False.
        self.assertIsNone(result["more_pages"])
        self.assertEqual(result["retrieved_at"], RETRIEVED)
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertIn("connection reset mid-page", result["error"])

    def test_failed_first_page_more_pages_unknown_too(self):
        result = transport_error_result(GROUP, ARTIFACT, VERSION, OSError("offline"), retrieved_at=RETRIEVED)
        self.assertIsNone(result["more_pages"])
        self.assertIs(result["is_continuation"], False)
        self.assertEqual(result["page_scope"], "attempted-single-response-page")
        self.assertEqual(result["query"], GAV_QUERY)

    def test_transport_result_no_mutation_of_inputs(self):
        exc = ValueError("boom")
        token = "dG9rZW4="
        first = transport_error_result(GROUP, ARTIFACT, VERSION, exc, retrieved_at=RETRIEVED, request_page_token=token)
        second = transport_error_result(GROUP, ARTIFACT, VERSION, exc, retrieved_at=RETRIEVED, request_page_token=token)
        self.assertEqual(exc.args, ("boom",))
        self.assertEqual(token, "dG9rZW4=")
        self.assertEqual(first, second)
        self.assertIsNot(first["query"], second["query"])
        first["query"]["page_token"] = "mutated"
        self.assertEqual(second["query"]["page_token"], "dG9rZW4=")


class PurityTest(unittest.TestCase):
    def test_mapping_never_touches_files_or_network(self):
        with NoIoSpy():
            build_query(GROUP, ARTIFACT, VERSION, page_token="tok")
            map_response(PAGE_BYTES, GROUP, ARTIFACT, VERSION, retrieved_at=RETRIEVED)
            transport_error_result(GROUP, ARTIFACT, VERSION, OSError("offline"))


if __name__ == "__main__":
    unittest.main()

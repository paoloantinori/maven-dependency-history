# Java test prerequisites: pinned helper dependency jars

Date: 2026-09-12
Tracking: TASK-21.5
Status: Current setup contract for the Java integration test slices.

## What the tests need

Four test modules compile or execute the Java usage helper and therefore
need its pinned runtime jars on disk before they run:

- `tests/test_usage_helper.py` (helper CLI slice)
- `tests/test_usage.py` (Python runner slice)
- `tests/test_usage_pipeline.py` (staging integration slice)
- `tests/test_helper_packaging.py` (offline packaging slice)

The authoritative pin list is the manifest
`skills/maven-dependency-investigator/helper/DEPENDENCIES.json`
(schema `mdi.helper-dependencies/2`). The shared runtime lookup
`tests/java_support.py` is the only place the four integration modules
obtain jar pins; it replaces the four per-module `PINNED_JARS` dicts that
each duplicated the digest list next to a session-specific path. Independent
expected-value assertions are intentionally retained: the crosscheck table
in `tests/test_java_support.py` pins what the manifest must contain, and
`tests/test_helper_packaging.py` pins the analyzer license-text digests it
asserts on. No test downloads, fetches or builds anything.

## Exact setup

Create a directory once that holds the pinned jars, then point the
environment variable at it and run the suite:

```bash
export MDI_TEST_DEPENDENCY_DIR=/absolute/path/to/pinned-jars
python3 -m unittest tests.test_usage_helper tests.test_usage \
    tests.test_usage_pipeline tests.test_helper_packaging -q
```

The directory must already contain every jar listed in the manifest with the
exact pinned bytes. The tests never download anything and have no fallback
location. One way to produce the jars once, outside the test suite, is
Maven's dependency plugin (`dependency:copy-dependencies` for the helper
runtime closure: `org.apache.maven.shared:maven-dependency-analyzer:1.17.2`
plus `org.ow2.asm:asm:9.10.1`, `com.google.code.gson:gson:2.14.0` and
`com.google.errorprone:error_prone_annotations:2.48.0`), or copy the jars
from a trusted internal location. Verify the copies against the manifest
pins before use; the tests repeat that verification themselves.

## What the tests verify

In each module's `setUpClass`, `tests/java_support.py` resolves
`MDI_TEST_DEPENDENCY_DIR`, loads the manifest, rejects unsafe pin shapes
(path-like filenames, malformed digests, unknown schema) and recomputes the
sha256 of every jar before any Java tool runs. A jar that is missing or
tampered fails the class; it is never silently accepted. Classpaths are
joined with `os.pathsep`, never a hardcoded separator.

Pins currently listed by the manifest (the manifest remains authoritative):

| Filename | sha256 |
| --- | --- |
| `maven-dependency-analyzer-1.17.2.jar` | `f4d83b15827ca61325718a5e80f57205da0567ab311bf0ec1646c8106f78255d` |
| `asm-9.10.1.jar` | `ed825d10ab1399c8c0cb669e688cf0c8c82629b4c8399b58352b68e92ca10fcb` |
| `gson-2.14.0.jar` | `2cbd119bf1961c28788310963dc80ba65f58cdeec1dd139c8bdb1240faa2c36f` |
| `error_prone_annotations-2.48.0.jar` | `b49c5c958316ed67a09c699dda9aa749caf434d51d863dea599ef36a49b9c855` |

## Behavior when the prerequisite is missing

Importing the test modules and unittest discovery never consult the
environment and never touch the filesystem for jars, so a fresh checkout can
always collect the suite. When a Java integration module actually runs
without `MDI_TEST_DEPENDENCY_DIR` set, or with a directory that fails the
sha256 verification, the class fails fast in `setUpClass` with an actionable
`RuntimeError` naming the variable, the manifest and the failing file. The
tests do not skip and do not fake success. Example failure without the
variable:

```text
tests.java_support.DependencyError: environment variable MDI_TEST_DEPENDENCY_DIR is not set: the Java integration tests require the pinned helper jars listed in <repo>/skills/maven-dependency-investigator/helper/DEPENDENCIES.json and never download them. Produce the jars once with Maven (dependency:copy-dependencies for the helper runtime closure) or copy them from a trusted location, then export MDI_TEST_DEPENDENCY_DIR=/absolute/path/to/pinned-jars
```

Everything that does not need Java stays runnable with the variable unset,
including `tests/test_java_support.py`, which exercises the resolution logic
itself against crafted manifests and fake jar directories only.

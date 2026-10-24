"""Shared dependency resolution for the Java helper test slices.

The pinned jars are never fetched, downloaded or built by the tests. The
operator supplies a pre-existing directory of jars through the
MDI_TEST_DEPENDENCY_DIR environment variable, and this module verifies every
jar against the authoritative pin manifest
skills/maven-dependency-investigator/helper/DEPENDENCIES.json (schema
mdi.helper-dependencies/2) before any test compiles or runs Java.

Importing this module, or importing any test module that uses it, performs
no filesystem access and never fails when the variable is unset. Resolution
happens explicitly in setUpClass (or a fixture call) so a missing
prerequisite raises DependencyError, an actionable RuntimeError subclass,
never unittest.SkipTest and never an import-time failure.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from tests import child_env  # noqa: E402  (canonical child-env policy)

REPO = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (REPO / "skills" / "maven-dependency-investigator" / "helper"
                 / "DEPENDENCIES.json")
ENV_VAR = "MDI_TEST_DEPENDENCY_DIR"
EXPECTED_SCHEMA = "mdi.helper-dependencies/2"

# A pin addresses a plain jar filename inside the dependency directory; path
# separators, traversal, hidden or non-jar names are rejected so a manifest
# entry can never point at an arbitrary filesystem path.
_FILENAME_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]*\.jar\Z")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


class DependencyError(RuntimeError):
    """A mandatory Java test prerequisite is missing or unverified.

    The message is actionable: it names the environment variable, the pin
    manifest and the failing file. It is never a skip.
    """


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dependency_directory(environ=None):
    """Return the directory of pre-existing pinned jars.

    Read from MDI_TEST_DEPENDENCY_DIR (os.environ, or the explicit mapping).
    Raises DependencyError when the variable is unset or blank; there is no
    default, no fallback and no download.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_VAR, "")
    if not raw.strip():
        raise DependencyError(
            "environment variable %s is not set: the Java integration tests "
            "require the pinned helper jars listed in %s and never download "
            "them. Produce the jars once with Maven (dependency:copy-"
            "dependencies for the helper runtime closure) or copy them from a "
            "trusted location, then export %s=/absolute/path/to/pinned-jars"
            % (ENV_VAR, MANIFEST_PATH, ENV_VAR))
    return Path(raw)


def manifest_pins(manifest_path=MANIFEST_PATH):
    """Return filename to sha256 pins from the manifest, in manifest order.

    Raises DependencyError, naming the manifest path and the underlying
    cause, when the manifest is missing or unreadable (OSError), not valid
    UTF-8, not valid JSON, has an unexpected schema, or carries unsafe pin
    shapes. Only the precise failure exceptions are caught; nothing broad.
    Only parsing happens here: no filesystem outside the manifest is
    touched.
    """
    try:
        fh = open(manifest_path, "r", encoding="utf-8")
    except OSError as exc:
        raise DependencyError("dependency manifest %s cannot be read: %s"
                              % (manifest_path, exc)) from exc
    try:
        with fh:
            data = json.load(fh)
    except UnicodeDecodeError as exc:
        raise DependencyError("dependency manifest %s is not valid UTF-8: %s"
                              % (manifest_path, exc)) from exc
    except json.JSONDecodeError as exc:
        raise DependencyError("dependency manifest %s is not valid JSON: %s"
                              % (manifest_path, exc)) from exc
    if not isinstance(data, dict) or data.get("schema") != EXPECTED_SCHEMA:
        found = data.get("schema") if isinstance(data, dict) else None
        raise DependencyError(
            "dependency manifest %s has schema %r, expected %r"
            % (manifest_path, found, EXPECTED_SCHEMA))
    deps = data.get("dependencies")
    if not isinstance(deps, list) or not deps:
        raise DependencyError("dependency manifest %s lists no dependencies"
                              % manifest_path)
    pins = {}
    for dep in deps:
        name = dep.get("filename") if isinstance(dep, dict) else None
        if not isinstance(name, str) or not _FILENAME_PATTERN.match(name):
            raise DependencyError(
                "dependency manifest %s has an unsafe or malformed jar "
                "filename %r: pins are plain jar filenames, never paths"
                % (manifest_path, name))
        digest = dep.get("sha256")
        if not isinstance(digest, str) or not _SHA256_PATTERN.match(digest):
            raise DependencyError(
                "dependency manifest %s entry %s lacks a 64 character "
                "lowercase hex sha256 pin (got %r)" % (manifest_path, name, digest))
        if name in pins:
            raise DependencyError("dependency manifest %s lists %s twice"
                                  % (manifest_path, name))
        pins[name] = digest
    return pins


def verified_dependencies(directory=None, manifest_path=MANIFEST_PATH):
    """Verify every pinned jar and return the pins in manifest order.

    Loads the manifest pins, then checks that each file exists in `directory`
    (MDI_TEST_DEPENDENCY_DIR when not given explicitly) and hashes to its
    pinned sha256. Raises DependencyError, with an actionable message, when
    the environment variable is unset, the directory is missing, a jar is
    absent or its bytes do not match the pin. Returns the pins as a mapping
    in manifest order; jar order is deterministic.
    """
    if directory is None:
        directory = dependency_directory()
    directory = Path(directory)
    pins = manifest_pins(manifest_path)
    if not directory.is_dir():
        raise DependencyError(
            "dependency directory %s does not exist or is not a directory; "
            "set %s to the directory holding the pinned jars listed in %s"
            % (directory, ENV_VAR, manifest_path))
    for name, pinned in pins.items():
        jar = directory / name
        if not jar.is_file():
            raise DependencyError(
                "pinned dependency jar missing: %s (required by %s); set %s "
                "to the directory containing all pinned jars; the tests "
                "never download anything" % (jar, manifest_path, ENV_VAR))
        actual = _sha256_file(jar)
        if actual != pinned:
            raise DependencyError(
                "%s has sha256 %s but %s pins %s; replace the file with the "
                "exact pinned artifact" % (jar, actual, manifest_path, pinned))
    return pins

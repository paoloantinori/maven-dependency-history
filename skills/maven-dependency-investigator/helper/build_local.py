#!/usr/bin/env python3
"""Deterministic offline build of the mdi.UsageHelper helper.jar (TASK-21.1).

Maintainer-time tool ONLY. It is never invoked during an investigation: the
investigation-time runner (scripts/mdi/usage.py) consumes an already built
helper.jar with its lib/ directory; users do not compile or download anything
at investigation time. This script performs no network access, no Maven and no
downloads: every jar it consumes is an explicit, sha256-pinned input.

Invocation (any cwd, Python 3.11+ standard library only):

    python3 skills/maven-dependency-investigator/helper/build_local.py \
        --dependency-dir DIR_WITH_PINNED_JARS \
        --output-dir FRESH_DIR \
        [--javac /path/to/javac] [--java /path/to/java] \
        [--manifest /path/to/DEPENDENCIES.json]

Known prerequisites:
  - A JDK whose javac supports --release 17 (tested with Temurin 21.0.11);
    --javac defaults to "javac" on PATH. --java is recorded in BUILD-INFO
    only (the build itself never runs java); if absent it is recorded null.
  - --dependency-dir must contain the jars pinned in DEPENDENCIES.json
    (default: DEPENDENCIES.json next to this script). Each jar is verified
    against its pinned sha256 BEFORE javac runs; any mismatch or missing file
    aborts the build with exit 2 and no output directory is created.
  - License texts pinned in DEPENDENCIES.json come in two kinds:
    "jar-entry" (extracted verbatim from the actual dependency jar) and
    "vendored-file" (a plain-text file already committed under LICENSES/
    next to the manifest, retrieved once from its official source; see
    LICENSES/README.md). Vendored paths are manifest-relative and validated:
    no absolute paths, no "..", no non-canonical forms, no symlink anywhere
    on the path, and the resolved file must stay under the manifest
    directory. Every vendored file is sha256-verified BEFORE javac runs,
    exactly like the jars; missing or tampered files abort with exit 2 and
    no output directory. The build never fetches anything: vendored texts
    must already be on disk.
  - --output-dir must not exist: an existing path, including any symlink
    (dangling ones too, checked with lexists before resolution so the build
    is never retargeted through a link), is refused untouched (exit 2). If
    the build fails after creating it, the fresh directory is removed again,
    so a retry never collides with a partial result.

Output layout:
    helper.jar           thin deterministic jar (own classes + fixed manifest)
    lib/                 byte-for-byte pristine copies of the pinned jars
    licenses/<stem>/     license/notice texts: extracted verbatim from the
                         actual jars (jar-entry) or copied byte-for-byte from
                         the vendored files (vendored-file); dependencies
                         whose license text is pinned nowhere are reported
                         as explicit BLOCKERs (stderr + BUILD-INFO.json),
                         never filled with invented boilerplate
    BUILD-INFO.json      source hash, tool versions, flags, input and output
                         hashes; no wall-clock timestamps
    SHA256SUMS           sha256 of every packaged file

Determinism: identical source, pinned inputs and toolchain produce a
byte-identical helper.jar (fixed sorted entry order, fixed 1980-01-01 entry
timestamps, fixed 0644 permissions, no toolchain data inside the jar).
Cross-JDK byte identity is NOT claimed; BUILD-INFO.json records the exact
javac used. The compiler runs with a sanitized environment (PATH, LANG,
LC_ALL only) so inherited JVM/agent options cannot alter the build.
"""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath

SCHEMA_BUILD_INFO = "mdi.helper-build-info/1"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
SANITIZED_ENV_KEYS = ("PATH", "LANG", "LC_ALL")
FLAGS = ["--release", "17", "-encoding", "UTF-8"]
HELPER_MAIN = "mdi.UsageHelper"
SCRIPT_DIR = Path(__file__).resolve().parent
HELPER_SRC = SCRIPT_DIR / "src" / "main" / "java" / "mdi" / "UsageHelper.java"


class BuildError(Exception):
    """Expected failure: input, tool or packaging problem (exit 2)."""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitized_env():
    env = {k: os.environ[k] for k in SANITIZED_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", os.defpath)
    return env


def load_manifest(path):
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BuildError("cannot read dependency manifest %s: %s" % (path, e))
    deps = manifest.get("dependencies")
    if manifest.get("schema") != "mdi.helper-dependencies/2" or not isinstance(deps, list):
        raise BuildError("unexpected manifest schema in %s" % path)
    names = set()
    for dep in deps:
        name = dep.get("filename")
        if not name or name in names:
            raise BuildError("manifest dependency without a unique filename: %r" % (dep,))
        names.add(name)
        if len(dep.get("sha256", "")) != 64:
            raise BuildError("manifest entry %s lacks a sha256 pin" % name)
        for role, text in sorted(((dep.get("license") or {}).get("texts") or {}).items()):
            what = "%s!%s" % (name, role)
            kind = text.get("kind")
            if kind == "jar-entry":
                if not text.get("entry") or len(text.get("sha256", "")) != 64:
                    raise BuildError("jar-entry license text %s lacks entry or sha256" % what)
            elif kind == "vendored-file":
                if not isinstance(text.get("path"), str) or len(text.get("sha256", "")) != 64:
                    raise BuildError("vendored-file license text %s lacks path or sha256" % what)
            else:
                raise BuildError("license text %s has unknown kind %r (expected "
                                 "jar-entry or vendored-file)" % (what, kind))
    return deps


def verify_pinned_inputs(dep_dir, deps):
    """Verify every pinned jar before javac ever runs."""
    verified = []
    for dep in deps:
        path = dep_dir / dep["filename"]
        if not path.is_file():
            raise BuildError("missing pinned dependency input: %s" % path)
        actual = sha256_file(path)
        if actual != dep["sha256"]:
            raise BuildError(
                "sha256 mismatch for %s: found %s, manifest pins %s"
                % (path, actual, dep["sha256"]))
        verified.append((dep, path))
    return verified


def resolve_vendored_path(manifest_dir, rel, what):
    """Resolve a manifest-relative vendored license path, fail-closed.

    Accepted paths are canonical relative POSIX paths under the manifest
    directory: no absolute paths, no ".." components, no redundant separators
    or "." components, and no symlink anywhere from the manifest directory
    down to the file (so neither the file nor a parent directory can retarget
    the read outside the vendored tree).
    """
    if not isinstance(rel, str) or not rel or "\\" in rel:
        raise BuildError("%s: vendored license path must be a plain relative "
                         "POSIX path, got %r" % (what, rel))
    posix = PurePosixPath(rel)
    if posix.is_absolute() or str(posix) != rel or ".." in posix.parts:
        raise BuildError("%s: vendored license path must be canonical and "
                         "relative to the manifest, got %r" % (what, rel))
    current = manifest_dir
    for part in posix.parts:
        current = current / part
        if os.path.islink(current):
            raise BuildError("%s: vendored license path traverses a symlink: %s"
                             % (what, current))
    resolved = current.resolve()
    root = manifest_dir.resolve()
    if resolved == root or root not in resolved.parents:
        raise BuildError("%s: vendored license path escapes the manifest "
                         "directory: %r" % (what, rel))
    return resolved


def verify_vendored_texts(manifest_dir, deps):
    """Verify every vendored license text before javac or output creation."""
    vendored = {}
    for dep in deps:
        texts = (dep.get("license") or {}).get("texts") or {}
        for role in sorted(texts):
            text = texts[role]
            if text.get("kind") != "vendored-file":
                continue
            what = "%s!%s" % (dep["filename"], role)
            path = resolve_vendored_path(manifest_dir, text["path"], what)
            if not path.is_file():
                raise BuildError("missing vendored license file for %s: %s "
                                 "(pinned in the manifest, expected on disk "
                                 "next to it; the build never downloads)"
                                 % (what, path))
            actual = sha256_file(path)
            if actual != text["sha256"]:
                raise BuildError("vendored license pin mismatch for %s: %s "
                                 "has sha256 %s, manifest pins %s"
                                 % (what, path, actual, text["sha256"]))
            vendored[what] = (path, actual)
    return vendored


def resolve_tool(name, required=True):
    resolved = shutil.which(name)
    if resolved is None:
        if required:
            raise BuildError("tool not found on PATH: %s" % name)
        return None
    return resolved


def tool_version(path):
    result = subprocess.run([path, "--version"], capture_output=True, text=True,
                            env=sanitized_env(), timeout=60)
    return (result.stdout or result.stderr).strip()


def compile_helper(javac, verified, work_dir):
    classes = work_dir / "classes"
    classes.mkdir()
    classpath = os.pathsep.join(str(path) for _, path in verified)
    argv = [javac, *FLAGS, "-classpath", classpath,
            "-d", str(classes), str(HELPER_SRC)]
    result = subprocess.run(argv, capture_output=True, text=True,
                            env=sanitized_env(), timeout=600)
    if result.returncode != 0:
        raise BuildError("javac failed (%d): %s" % (result.returncode, result.stderr.strip()))
    return classes


def write_helper_jar(jar_path, classes_dir):
    manifest = ("\r\n".join([
        "Manifest-Version: 1.0",
        "Created-By: mdi helper build_local.py (deterministic thin jar)",
        "Main-Class: %s" % HELPER_MAIN,
    ]) + "\r\n\r\n").encode("utf-8")
    entries = [("META-INF/MANIFEST.MF", manifest)]
    for path in classes_dir.rglob("*.class"):
        entries.append((path.relative_to(classes_dir).as_posix(),
                        path.read_bytes()))
    entries.sort(key=lambda item: item[0])
    with zipfile.ZipFile(jar_path, "w") as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)


def extract_licenses(out_dir, verified, vendored):
    """Copy license texts verbatim (jar entries or vendored files); blockers."""
    written, blockers = {}, []
    for dep, path in verified:
        license_info = dep.get("license") or {}
        texts = license_info.get("texts")
        if not texts:
            blockers.append({
                "coordinates": dep.get("coordinates"),
                "filename": dep["filename"],
                "license_declared": license_info.get("name"),
                "reason": license_info.get("blocker", "no license text available"),
            })
            continue
        stem = dep["filename"][: -len(".jar")]
        with ExitStack() as stack:
            archive = None
            for role in sorted(texts):
                source = texts[role]
                what = "%s!%s" % (dep["filename"], role)
                if source["kind"] == "vendored-file":
                    data = vendored[what][0].read_bytes()
                else:
                    if archive is None:
                        archive = stack.enter_context(zipfile.ZipFile(path))
                    data = archive.read(source["entry"])
                actual = hashlib.sha256(data).hexdigest()
                if actual != source["sha256"]:
                    raise BuildError(
                        "license pin mismatch for %s (%s): %s != %s"
                        % (what, source.get("entry") or source.get("path"),
                           actual, source["sha256"]))
                target = out_dir / "licenses" / stem / role.upper()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                written[target.relative_to(out_dir).as_posix()] = actual
    return written, blockers


def build(args):
    if not args.dependency_dir.is_dir():
        raise BuildError("--dependency-dir is not a directory: %s" % args.dependency_dir)
    if not HELPER_SRC.is_file():
        raise BuildError("helper source not found: %s" % HELPER_SRC)
    deps = load_manifest(args.manifest)
    verified = verify_pinned_inputs(args.dependency_dir, deps)
    vendored = verify_vendored_texts(args.manifest.parent, deps)
    # lexists (not Path.exists, which follows symlinks): a dangling symlink at
    # the output location must be refused like any existing path, before any
    # resolution would silently retarget the build through it.
    if os.path.lexists(args.output_dir):
        raise BuildError("output path already exists or is a symlink, refusing "
                         "to touch it: %s" % args.output_dir)
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True)
    try:
        return package(args, deps, verified, vendored)
    except BaseException:
        shutil.rmtree(args.output_dir, ignore_errors=True)
        print("build_local: removed partial output directory %s" % args.output_dir,
              file=sys.stderr)
        raise


def package(args, deps, verified, vendored):
    javac = resolve_tool(args.javac)
    java = resolve_tool(args.java, required=False)
    out = args.output_dir
    with tempfile.TemporaryDirectory(prefix="mdi-helper-build-") as work:
        write_helper_jar(out / "helper.jar",
                         compile_helper(javac, verified, Path(work)))
    inputs, lib_hashes = [], {}
    (out / "lib").mkdir()
    for dep, path in verified:
        shutil.copyfile(path, out / "lib" / dep["filename"])
        copied = sha256_file(out / "lib" / dep["filename"])
        if copied != dep["sha256"]:
            raise BuildError("copied jar diverged from its pin: %s" % dep["filename"])
        lib_hashes["lib/" + dep["filename"]] = copied
        inputs.append({"filename": dep["filename"], "sha256": dep["sha256"]})
    licenses, blockers = extract_licenses(out, verified, vendored)
    vendored_texts = []
    for dep in deps:
        texts = (dep.get("license") or {}).get("texts") or {}
        for role in sorted(texts):
            if texts[role].get("kind") != "vendored-file":
                continue
            vendored_texts.append({
                "dependency": dep["filename"],
                "role": role,
                "path": texts[role]["path"],
                "sha256": vendored["%s!%s" % (dep["filename"], role)][1],
            })
    outputs = {"helper.jar": sha256_file(out / "helper.jar"), "lib": lib_hashes,
               "licenses": licenses}
    info = {
        "schema": SCHEMA_BUILD_INFO,
        "helper_source": {"path": "src/main/java/mdi/UsageHelper.java",
                          "sha256": sha256_file(HELPER_SRC)},
        "manifest": {"path": "DEPENDENCIES.json",
                     "sha256": sha256_file(args.manifest)},
        "javac": {"path": javac, "version_output": tool_version(javac),
                  "classpath_order": [d["filename"] for d in deps],
                  "env_keys": list(SANITIZED_ENV_KEYS)},
        "java": {"path": java, "version_output": tool_version(java) if java else None},
        "python": {"version": sys.version, "zlib_version": zlib.ZLIB_VERSION,
                   "zlib_runtime_version": zlib.ZLIB_RUNTIME_VERSION},
        "flags": FLAGS,
        "inputs": inputs,
        "vendored_license_texts": vendored_texts,
        "outputs": outputs,
        "license_blockers": blockers,
        "determinism": {
            "helper.jar": "sorted entry order, fixed 1980-01-01T00:00:00 entry "
                          "timestamps, fixed 0644 permissions, no wall-clock or "
                          "toolchain data inside the jar",
            "scope": "byte-identical for identical source, pinned inputs and "
                     "toolchain (compiler and compression implementation, see "
                     "python.zlib fields); cross-JDK byte identity is NOT claimed",
        },
    }
    (out / "BUILD-INFO.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8")
    sums = []
    for path in sorted(p for p in out.rglob("*") if p.is_file()
                       and p.name != "SHA256SUMS"):
        sums.append("%s  %s" % (sha256_file(path), path.relative_to(out).as_posix()))
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    for blocker in blockers:
        print("BLOCKER: no license text for %s (%s): %s"
              % (blocker["filename"], blocker["license_declared"],
                 blocker["reason"]), file=sys.stderr)
    print("packaged %s helper.jar sha256=%s (license blockers: %d)"
          % (out, outputs["helper.jar"], len(blockers)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deterministic offline packaging of the mdi usage helper.")
    parser.add_argument("--dependency-dir", required=True, type=Path,
                        help="directory holding exactly the pinned dependency jars")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="fresh directory for the package; must not exist")
    parser.add_argument("--javac", default="javac",
                        help="javac to use (default: javac on PATH)")
    parser.add_argument("--java", default="java",
                        help="java to record in BUILD-INFO (default: java on PATH)")
    parser.add_argument("--manifest", type=Path,
                        default=SCRIPT_DIR / "DEPENDENCIES.json",
                        help="dependency manifest (default: DEPENDENCIES.json "
                             "next to this script)")
    args = parser.parse_args(argv)
    args.dependency_dir = args.dependency_dir.resolve()
    try:
        return build(args)
    except BuildError as e:
        print("build_local: error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Read-only discovery of dependency declarations across a Maven checkout.

discover_declarations(repo) reads the root pom.xml and every locally declared
module POM recursively (modules declared inside profiles are visited too and
recorded as candidates whose activation is unknown) and returns a JSON-safe
result of module metadata, raw declarations, issues and explicit coverage. No
Maven resolution, property interpolation, profile activation, inheritance or
packaging is performed: coordinate strings are preserved exactly as written,
including unresolved ${...} placeholders.

Reading policy, stated once and enforced everywhere: the checkout root is
opened by walking its lexical absolute path from the filesystem root one
component at a time with O_DIRECTORY|O_NOFOLLOW, so a root reachable only
through a symlinked component (the root itself included) is rejected instead
of followed; every module directory is opened relative to that root
descriptor with the same flags, so no symlink below the root is traversed
either; each pom.xml is opened with O_NOFOLLOW|O_NONBLOCK relative to its
module directory descriptor and must be a regular file. POM bytes are
captured with mdi.capture semantics (bounded read, stat consistency before
and after, digest only for complete captures) as mdi.capture.CapturedFile
values: capture_file() is path-based and cannot consume a directory
descriptor, so the descriptor-relative open and bounded read live here
instead of coupling discovery to capture or runstore internals. A POM whose
root element is not exactly project in the empty namespace or the accepted
Maven 4.0.0 namespace is rejected with a nonproject-root issue and never
traversed, and structural children are matched only within that document
namespace, so foreign-namespace lookalikes are ignored. Profile-gated
content keeps activation 'unknown' even when the profile carries no id: the
profile is then identified by its stable logical locator, and so is every
profile sharing a duplicated id, so provenance locators stay distinct; its
properties stay scoped inside its profile metadata instead of flattening
over the module's. Module paths escaping the checkout ('..' past the root, absolute
spellings, empty values) are rejected with an issue. A broken module never
stops the others, module cycles and duplicates are reported instead of
re-walked, traversal is bounded by max_modules/max_depth/xml_bytes, and
nothing is ever written.
"""

import errno
import hashlib
import os
import stat
from datetime import datetime, timezone

from mdi.capture import CapturedFile, CaptureError, FileIdentity
from mdi.limits import Limits
from mdi.xmlsafe import UnsafeXML, parse_xml

_POM_NAME = "pom.xml"
_MAVEN_NS = "http://maven.apache.org/POM/4.0.0"
_DEFAULT_XML_BYTES = 8 * 1024 * 1024
_DECL_FIELDS = ("groupId", "artifactId", "version", "scope", "type", "classifier", "optional")
_PLUGIN_FIELDS = ("groupId", "artifactId", "version")
_DIR_OPEN_STATUS = {errno.ENOENT: ("missing", "module-dir-missing"),
                    errno.ELOOP: ("rejected", "module-dir-rejected"),
                    errno.ENOTDIR: ("rejected", "module-dir-rejected")}
_POM_OPEN_STATUS = {errno.ENOENT: ("missing", "missing-pom"),
                    errno.ELOOP: ("rejected", "pom-symlink-rejected")}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_tag(tag: str):
    """Return (namespace, local_name); '' namespace when the tag carries none."""
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def _kids(elem, name, ns):
    """Children whose namespace is exactly `ns` and local name matches."""
    return [child for child in elem
            if isinstance(child.tag, str) and _split_tag(child.tag) == (ns, name)]


def _first_child(elem, name, ns):
    """First matching child in document order; returns early instead of
    materializing every match like _kids."""
    for child in elem:
        if isinstance(child.tag, str) and _split_tag(child.tag) == (ns, name):
            return child
    return None


def _text(elem, name, ns):
    child = _first_child(elem, name, ns)
    return None if child is None else "".join(child.itertext()).strip()


def _fields(elem, names, ns):
    return {name: _text(elem, name, ns) for name in names}


def _tag_positions(parent):
    """One linear pass over one parent's children: id(child) maps to its
    0-based position among same-tag (namespace-qualified) siblings. Element
    ids are stable here because the parsed tree outlives extraction."""
    seen, table = {}, {}
    for child in parent:
        if isinstance(child.tag, str):
            count = seen.get(child.tag, 0)
            seen[child.tag] = count + 1
            table[id(child)] = count
    return table


def _root_problem(elem):
    """None only for a root that is exactly project in the empty namespace or
    the accepted Maven namespace; otherwise a detail string."""
    namespace, local = _split_tag(elem.tag)
    if local == "project" and namespace in ("", _MAVEN_NS):
        return None
    return (f"root element {local!r} in namespace {namespace or 'none'} is not project "
            f"in the empty namespace or {_MAVEN_NS}")


def _lexical_abs(repo) -> str:
    try:
        text = os.fspath(repo)
    except TypeError as exc:
        raise TypeError(f"repo must be str or os.PathLike, got {type(repo).__name__}") from exc
    if isinstance(text, bytes):
        raise TypeError("repo must be text, not bytes")
    return os.path.abspath(text)


def _platform_detail():
    missing = [name for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK") if not getattr(os, name, 0)]
    if missing:
        return "missing " + ", ".join(missing)
    if os.open not in os.supports_dir_fd:
        return "os.open does not support dir_fd on this platform"
    return None


def _dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_DIRECTORY | os.O_NOFOLLOW


def _file_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not no_follow or not nonblock:
        raise CaptureError("safe POM capture needs O_NOFOLLOW and O_NONBLOCK; unsupported on this platform")
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow | nonblock


def _open_part(dir_fd: int, part: str, where: str) -> int:
    try:
        return os.open(part, _dir_flags(), dir_fd=dir_fd)
    except OSError as exc:
        raise OSError(exc.errno, f"cannot open {where} component {part!r}: {exc.strerror or exc}") from exc


def _open_root_fd(abs_root: str) -> int:
    """Open the checkout root, refusing every symlinked path component."""
    fd = os.open(os.sep, _dir_flags())
    walked = ""
    try:
        for part in abs_root.split(os.sep):
            if not part:
                continue
            nxt = _open_part(fd, part, f"{abs_root} (at {walked or os.sep})")
            os.close(fd)
            fd = nxt
            walked = walked + os.sep + part
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_module_dir(root_fd: int, parts, module_abs: str):
    """Open a module directory beneath root_fd; returns (fd, owned_by_caller)."""
    fd = root_fd
    owned = False
    try:
        for part in parts:
            nxt = _open_part(fd, part, module_abs)
            if owned:
                os.close(fd)
            fd = nxt
            owned = True
        return fd, owned
    except BaseException:
        if owned:
            os.close(fd)
        raise


def _capture_at(dir_fd: int, name: str, subject: str, max_bytes: int) -> CapturedFile:
    """Capture one regular file relative to dir_fd with mdi.capture semantics."""
    fd = os.open(name, _file_flags(), dir_fd=dir_fd)
    try:
        before_raw = os.fstat(fd)
        if not stat.S_ISREG(before_raw.st_mode):
            raise CaptureError(f"not a regular file: {subject}")
        chunks, remaining, eof = [], max_bytes + 1, False
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                eof = True
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_raw = os.fstat(fd)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if not eof:
        data = data[:max_bytes]
    before = FileIdentity.from_stat(before_raw)
    after = FileIdentity.from_stat(after_raw)
    complete = eof and before == after
    notes = []
    if not eof:
        notes.append(f"truncated at max_bytes={max_bytes}")
    if before != after:
        notes.append("source changed during read")
    return CapturedFile(
        path=subject,
        data=data,
        sha256=hashlib.sha256(data).hexdigest() if complete else None,
        complete=complete,
        reason="; ".join(notes) if notes else None,
        observed_at=_now(),
        stat_before=before,
        stat_after=after,
    )


def _validate_relative_parts(parts) -> tuple:
    """Module-path parts for the public descriptor API: plain names only.

    Empty parts, '.', '..' (absolute or root-escaping spellings), '/' and NUL
    bytes are rejected, so a caller of open_module_dir can never open anything
    outside the checkout root. The separator policy is POSIX: '/' is the only
    separator, a literal backslash is an ordinary name character (never a
    separator and never silently widened into path support), and actual NUL
    bytes are rejected here, before any os.open call.
    """
    if isinstance(parts, (str, bytes)):
        raise TypeError("parts must be a sequence of path components, not a string")
    validated = tuple(parts)
    for part in validated:
        if not isinstance(part, str) or not part:
            raise ValueError(f"module part must be a nonempty string, got {part!r}")
        if part in (".", "..") or "/" in part or "\x00" in part:
            raise ValueError(f"module part {part!r} must be a plain directory name")
    return validated


def open_checkout_fd(repo) -> int:
    """Open the checkout root without following any symlinked component.

    The lexical absolute path of `repo` is walked from the filesystem root
    with O_DIRECTORY|O_NOFOLLOW; a root reachable only through a symlinked
    component raises OSError instead of being followed. The caller owns the
    returned descriptor and must close it.
    """
    return _open_root_fd(_lexical_abs(repo))


def open_module_dir(root_fd: int, parts, subject: str) -> int:
    """Open one module directory relative to the checkout root descriptor.

    `parts` are plain directory names (see _validate_relative_parts); the
    empty sequence returns a new descriptor for the root itself (a duplicate,
    so caller ownership is uniform). Every component is opened with
    O_DIRECTORY|O_NOFOLLOW relative to root_fd, so no symlink below the root
    is traversed. The caller owns the returned descriptor and must close it.
    `subject` is the logical path used in error messages only.
    """
    validated = _validate_relative_parts(parts)
    if not validated:
        return os.dup(root_fd)
    module_abs = subject or "module"
    fd, owned = _open_module_dir(root_fd, validated, module_abs)
    assert owned, "nonempty parts always transfer ownership"
    return fd


def capture_at(dir_fd: int, name: str, subject: str, max_bytes: int) -> CapturedFile:
    """Capture one regular file relative to dir_fd with mdi.capture semantics.

    `name` must be a single leaf component (never empty, '.', '..', a path
    with separators, or a NUL-bearing string) and max_bytes a positive
    non-bool int; the file is opened O_NOFOLLOW|O_NONBLOCK relative to the
    held dir_fd, so a swapped ancestor directory cannot redirect the read,
    and completeness requires an unchanged fstat identity on that same
    descriptor. `subject` is the logical path recorded for provenance only
    and is never reopened.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError(f"max_bytes must be int, got {type(max_bytes).__name__}")
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be positive, got {max_bytes}")
    if not isinstance(name, str) or not name:
        raise ValueError(f"name must be a nonempty leaf component, got {name!r}")
    if name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError(f"name {name!r} must be a single path component")
    return _capture_at(dir_fd, name, subject, max_bytes)


def _visit_module(root_fd: int, parts, root_abs: str, limits: Limits):
    """Read and parse one module POM. Returns (status, pom_meta, elem, issue)."""
    module_abs = os.path.join(root_abs, *parts)
    pom_abs = os.path.join(module_abs, _POM_NAME)
    try:
        dir_fd, owned = _open_module_dir(root_fd, parts, module_abs)
    except OSError as exc:
        status, code = _DIR_OPEN_STATUS.get(exc.errno, ("unreadable", "module-dir-unreadable"))
        return status, None, None, (code, exc.strerror or str(exc))
    try:
        try:
            capture = _capture_at(dir_fd, _POM_NAME, pom_abs, limits.xml_bytes)
        except OSError as exc:
            status, code = _POM_OPEN_STATUS.get(exc.errno, ("unreadable", "pom-unreadable"))
            return status, None, None, (code, exc.strerror or str(exc))
        except CaptureError as exc:
            return "rejected", None, None, ("pom-not-regular", str(exc))
        meta = {
            "path": capture.path,
            "bytes": len(capture.data),
            "sha256": capture.sha256,
            "complete": capture.complete,
            "observed_at": capture.observed_at,
            "reason": capture.reason,
        }
        if not capture.complete:
            return "incomplete", meta, None, ("incomplete-pom", capture.reason or "incomplete capture")
        try:
            elem = parse_xml(capture.data, limits=limits)
        except UnsafeXML as exc:
            return "malformed", meta, None, ("malformed-pom", str(exc))
        return "read", meta, elem, None
    finally:
        if owned:
            os.close(dir_fd)


def _extract_project(elem, module_id: str, pom_path: str):
    """Walk one parsed POM in document order.

    Returns (declarations, module_refs, meta): raw declaration records, module
    references preserving document order and the profile record they came
    from, and the module metadata observable without any Maven evaluation.
    Structural children match only the document namespace (the root's), and
    every element inside a profile is conditional: activation stays 'unknown'
    and the profile is identified by id plus logical source locator, with id
    itself optional (None for id-less profiles).
    """
    doc_ns = _split_tag(elem.tag)[0]
    declarations, refs = [], []
    meta = {
        "coordinates": _fields(elem, ("groupId", "artifactId", "version", "packaging"), doc_ns),
        "parent": None,
        "properties": {},
        "profiles": [],
        "modules": [],
        "profile_modules": [],
    }
    parent_elem = _first_child(elem, "parent", doc_ns)
    if parent_elem is not None:
        meta["parent"] = _fields(parent_elem, _PLUGIN_FIELDS + ("relativePath",), doc_ns)
    positions = {}

    def _locate(parent_path, parent, child):
        """Logical locator; the first same-tag sibling carries no index and
        repeats carry a 1-based one. Each parent's children are enumerated
        exactly once regardless of how many descendants get locators."""
        table = positions.get(id(parent))
        if table is None:
            table = _tag_positions(parent)
            positions[id(parent)] = table
        ordinal = table[id(child)]
        suffix = f"[{ordinal + 1}]" if ordinal else ""
        return f"{parent_path}/{_split_tag(child.tag)[1]}{suffix}"

    def _profile_source(profile):
        return None if profile is None else {"id": profile["id"], "source": profile["source"]}

    def declaration(kind, realm, management, owning_plugin, profile, fields, path):
        declarations.append({
            "module": module_id,
            "kind": kind,
            "realm": realm,
            "management_only": management,
            "owning_plugin": owning_plugin,
            "profile": _profile_source(profile),
            "activation": "unknown" if profile is not None else None,
            "coordinates": fields,
            "source": {"pom": pom_path, "xml_path": path},
        })

    def add_dependency(parent, path, *, management, owning_plugin, profile):
        for dep in _kids(parent, "dependency", doc_ns):
            fields = _fields(dep, _DECL_FIELDS, doc_ns)
            if owning_plugin is not None:
                kind, realm = "plugin-dependency", "build-plugin"
            elif management and (fields["scope"] or "").strip().lower() == "import":
                kind, realm = "bom-import", "project"
            elif management:
                kind, realm = "managed-dependency", "project"
            else:
                kind, realm = "dependency", "project"
            declaration(kind, realm, management, owning_plugin, profile,
                        fields, _locate(path, parent, dep))

    def add_plugins(parent, path, *, management, profile):
        for plugin in _kids(parent, "plugin", doc_ns):
            plugin_path = _locate(path, parent, plugin)
            plugin_coords = _fields(plugin, _PLUGIN_FIELDS, doc_ns)
            declaration("plugin", "build-plugin", management, None, profile,
                        _fields(plugin, _DECL_FIELDS, doc_ns), plugin_path)
            for deps in _kids(plugin, "dependencies", doc_ns):
                add_dependency(deps, _locate(plugin_path, plugin, deps),
                               management=management, owning_plugin=plugin_coords,
                               profile=profile)

    def walk(container, path, profile, top):
        properties_target = meta["properties"] if profile is None else profile["properties"]
        for child in container:
            if not isinstance(child.tag, str):
                continue
            child_ns, name = _split_tag(child.tag)
            if child_ns != doc_ns:
                continue
            child_path = _locate(path, container, child)
            if name == "modules":
                for module_elem in _kids(child, "module", doc_ns):
                    raw = "".join(module_elem.itertext()).strip()
                    if profile is None:
                        meta["modules"].append(raw)
                    else:
                        meta["profile_modules"].append({"profile": _profile_source(profile),
                                                        "path": raw})
                    refs.append({"raw": raw, "profile": _profile_source(profile)})
            elif name == "dependencies":
                add_dependency(child, child_path, management=False,
                               owning_plugin=None, profile=profile)
            elif name == "dependencyManagement":
                for deps in _kids(child, "dependencies", doc_ns):
                    add_dependency(deps, _locate(child_path, child, deps), management=True,
                                   owning_plugin=None, profile=profile)
            elif name == "build":
                for section in child:
                    if not isinstance(section.tag, str):
                        continue
                    section_ns, section_name = _split_tag(section.tag)
                    if section_ns != doc_ns:
                        continue
                    section_path = _locate(child_path, child, section)
                    if section_name == "plugins":
                        add_plugins(section, section_path, management=False, profile=profile)
                    elif section_name == "pluginManagement":
                        for plugins_elem in _kids(section, "plugins", doc_ns):
                            add_plugins(plugins_elem, _locate(section_path, section, plugins_elem),
                                        management=True, profile=profile)
            elif name == "profiles" and top:
                identified = [(profile_elem, _text(profile_elem, "id", doc_ns) or None)
                              for profile_elem in _kids(child, "profile", doc_ns)]
                id_counts = {}
                for _profile_elem, pid in identified:
                    if pid is not None:
                        id_counts[pid] = id_counts.get(pid, 0) + 1
                for profile_elem, pid in identified:
                    id_unique = pid is not None and "'" not in pid and id_counts[pid] == 1
                    profile_path = (f"{child_path}/profile[@id='{pid}']" if id_unique
                                    else _locate(child_path, child, profile_elem))
                    record = {"id": pid, "source": profile_path,
                              "activation": "unknown", "properties": {}}
                    meta["profiles"].append(record)
                    walk(profile_elem, profile_path, record, False)
            elif name == "properties":
                for prop in child:
                    if isinstance(prop.tag, str):
                        prop_ns, prop_name = _split_tag(prop.tag)
                        if prop_ns == doc_ns:
                            properties_target[prop_name] = "".join(prop.itertext()).strip()

    walk(elem, _split_tag(elem.tag)[1] or "project", None, True)
    return declarations, refs, meta


def _resolve_ref(base_parts, raw):
    """Resolve one <module> raw string against the declaring directory.

    Returns (parts, None) on success or (None, issue_code) for invalid,
    absolute or root-escaping spellings. No property evaluation is done.
    """
    if not raw or "\0" in raw:
        return None, "invalid-module-path"
    if raw.startswith("/") or os.path.isabs(raw):
        return None, "absolute-module-path"
    stack = list(base_parts)
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return None, "outside-root-module-path"
            stack.pop()
        else:
            stack.append(part)
    return stack, None


def _is_ancestor(target: str, start: str, modules) -> bool:
    parent_of = {record["id"]: record["declared_by"] for record in modules}
    current = start
    while current is not None:
        if current == target:
            return True
        current = parent_of.get(current)
    return False


def _module_record(task) -> dict:
    return {
        "id": task["id"],
        "depth": task["depth"],
        "declared_by": task["declared_by"],
        "declared_in_profile": task["profile"],
        "activation": "unknown" if task["profile"] else None,
        "status": None,
        "pom": None,
        "coordinates": None,
        "parent": None,
        "properties": None,
        "profiles": None,
        "modules": None,
        "profile_modules": None,
    }


def discover_declarations(repo, *, max_modules=128, max_depth=16, xml_bytes=_DEFAULT_XML_BYTES) -> dict:
    """Discover raw declarations in the Maven checkout rooted at `repo`.

    Returns a JSON-safe {'modules', 'declarations', 'issues', 'coverage'}
    result. Declarations are raw observations only: nothing is resolved,
    interpolated, activated or inherited, and coverage states those limits
    explicitly.
    """
    for label, value in (("max_modules", max_modules), ("max_depth", max_depth)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{label} must be int, got {type(value).__name__}")
    if max_modules <= 0:
        raise ValueError(f"max_modules must be positive, got {max_modules}")
    if max_depth < 0:
        raise ValueError(f"max_depth must not be negative, got {max_depth}")
    limits = Limits(xml_bytes=xml_bytes)
    started_at = _now()
    modules, declarations, issues = [], [], []

    def add_issue(code, module_id, detail):
        issues.append({"code": code, "module": module_id, "detail": detail})

    root_abs = _lexical_abs(repo)
    root_task = {"id": ".", "parts": (), "depth": 0, "declared_by": None, "profile": None}
    platform = _platform_detail()
    if platform is not None:
        add_issue("unsupported-platform", None, f"safe descriptor-relative reads unavailable: {platform}")
    else:
        try:
            root_fd = _open_root_fd(root_abs)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            if exc.errno == errno.ELOOP:
                reason = f"symlinked path component refused on the way to {root_abs}: {reason}"
            elif exc.errno == errno.ENOENT:
                reason = f"checkout root does not exist: {root_abs}: {reason}"
            elif exc.errno == errno.ENOTDIR:
                reason = (f"checkout root path component is not a directory "
                          f"(symlinked directories are refused, not followed): {root_abs}: {reason}")
            add_issue("root-rejected", ".", reason)
            record = _module_record(root_task)
            record["status"] = "missing" if exc.errno == errno.ENOENT else "rejected"
            modules.append(record)
        else:
            try:
                stack = [root_task]
                scheduled = {"."}
                while stack:
                    task = stack.pop()
                    record = _module_record(task)
                    modules.append(record)
                    status, pom_meta, elem, problem = _visit_module(root_fd, task["parts"], root_abs, limits)
                    record["status"] = status
                    record["pom"] = pom_meta
                    if problem is not None:
                        add_issue(problem[0], task["id"], problem[1])
                    refs = []
                    if elem is not None:
                        root_problem = _root_problem(elem)
                        if root_problem is not None:
                            record["status"] = "rejected"
                            add_issue("nonproject-root", task["id"], root_problem)
                        else:
                            found, refs, meta = _extract_project(elem, task["id"], pom_meta["path"])
                            declarations.extend(found)
                            record.update(meta)
                    children = []
                    for ref in refs:
                        parts, bad = _resolve_ref(task["parts"], ref["raw"])
                        if bad is not None:
                            add_issue(bad, task["id"], f"module ref {ref['raw']!r} rejected")
                            continue
                        identity = "/".join(parts) or "."
                        if identity in scheduled:
                            code = ("module-cycle" if _is_ancestor(identity, task["id"], modules)
                                    else "duplicate-module")
                            add_issue(code, task["id"],
                                      f"module ref {ref['raw']!r} resolves to already scheduled {identity!r}")
                            continue
                        if task["depth"] + 1 > max_depth:
                            add_issue("depth-limit", task["id"],
                                      f"module {identity!r} beyond max_depth={max_depth}")
                            continue
                        if len(modules) + len(stack) + len(children) + 1 > max_modules:
                            add_issue("module-limit", task["id"],
                                      f"module {identity!r} exceeds max_modules={max_modules}")
                            continue
                        scheduled.add(identity)
                        children.append({"id": identity, "parts": tuple(parts),
                                         "depth": task["depth"] + 1,
                                         "declared_by": task["id"],
                                         "profile": ref["profile"]})
                    stack.extend(reversed(children))
            finally:
                os.close(root_fd)

    coverage = {
        "scope": "declarations-only",
        "resolved_graph": "not-computed",
        "packaged_content": "not-inspected",
        "counts": {
            "modules": len(modules),
            "modules_read": sum(1 for record in modules if record["status"] == "read"),
            "declarations": len(declarations),
            "issues": len(issues),
        },
        "limits": {"max_modules": max_modules, "max_depth": max_depth, "xml_bytes": xml_bytes},
        "started_at": started_at,
        "finished_at": _now(),
        "limitations": [
            "declarations only: no Maven resolution, property interpolation, profile activation, inheritance or packaging is performed",
            "coordinate strings are raw POM text; unresolved ${...} placeholders are preserved as written",
            "profile-gated declarations and modules carry activation 'unknown'; inactive profiles are still recorded",
            "parent references are metadata only; parent POMs outside the module tree are never read",
            "the checkout root and every module path are opened without following symlinks; a root reachable only through a symlinked component is rejected",
            "each POM is captured independently; the module set is not an atomic snapshot of a concurrently changing checkout",
        ],
    }
    return {"modules": modules, "declarations": declarations, "issues": issues, "coverage": coverage}

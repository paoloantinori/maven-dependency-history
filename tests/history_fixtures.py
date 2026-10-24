"""Deterministic temporary Git history fixtures for Maven POM histories (TASK-21.2).

Fixed identities, historic dates and byte-identical content make commit ids
reproducible across runs. Upstream POM snapshots carry candidate evidence
only, never a resolved or shipped fact. Git runs by absolute path, list
argv, no shell, sanitized env: isolated HOME, no global/system config, no
inherited GIT_* vars, hooks disabled, signing and autogc off. Shallow clones
are deferred; `missing_history_fixture` covers the .git-absent case.
"""
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

AUTHOR_NAME, AUTHOR_EMAIL = "Alice Author", "alice@example.com"
COMMITTER_NAME, COMMITTER_EMAIL = "Carl Committer", "carl@example.com"

POM_RELPATH = "app/pom.xml"
PARENT_POM_RELPATH = "pom.xml"
APP_CODE_RELPATH = "app/src/main/java/com/example/App.java"
UPSTREAM_FILES = frozenset({"lib-b-1.0.0.pom", "lib-b-2.0.0.pom", "snapshots.json"})
COMMENT = "  <!-- lib-b is pinned for the demo console -->\n"
PROPS = "  <properties>\n    <lib-b.version>3.0.0</lib-b.version>\n  </properties>\n"


def _dep(groupId, artifactId, version):
    return f"""    <dependency>
      <groupId>{groupId}</groupId>
      <artifactId>{artifactId}</artifactId>
      <version>{version}</version>
    </dependency>
"""


LIB_B_1 = _dep("com.example", "lib-b", "1.0.0")
LIB_B_2 = _dep("com.example", "lib-b", "2.0.0")
LIB_B_PROP = _dep("com.example", "lib-b", "${lib-b.version}")
LIB_C = _dep("com.example", "lib-c", "1.0.0")


def _app_pom(dep="", comment="", props=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.example</groupId>
    <artifactId>demo-parent</artifactId>
    <version>1.0.0</version>
  </parent>
  <artifactId>demo-app</artifactId>
{comment}{props}  <dependencies>
{dep}  </dependencies>
</project>
"""


def _parent_pom(bom_version):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo-parent</artifactId>
  <version>1.0.0</version>
  <packaging>pom</packaging>
  <modules>
    <module>app</module>
  </modules>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.example</groupId>
        <artifactId>corp-bom</artifactId>
        <version>{bom_version}</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>
</project>
"""


def _upstream_pom(version, dep=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>lib-b</artifactId>
  <version>{version}</version>
  <dependencies>
{dep}  </dependencies>
</project>
"""


APP_CODE_V1 = 'package com.example;\n\npublic class App {\n    public String greet() {\n        return "hello";\n    }\n}\n'
APP_CODE_V2 = 'package com.example;\n\npublic class App {\n    /** Console entry point. */\n    public String greet() {\n        return "hello, world";\n    }\n}\n'

SNAPSHOTS = {
    "purpose": "Local upstream POM snapshots explaining candidate relationships in the fixture.",
    "claim": "Candidate evidence only; describes a candidate introduction, not a resolved or shipped fact.",
    "snapshots": [
        {"file": "lib-b-1.0.0.pom", "coordinates": "com.example:lib-b:1.0.0", "dependsOn": []},
        {"file": "lib-b-2.0.0.pom", "coordinates": "com.example:lib-b:2.0.0", "dependsOn": ["com.example:lib-c:1.0.0"]},
    ],
    "candidateFor": {"artifactId": "lib-c", "candidateIntroductionCommit": "upgrade_lib_b_2", "relation": "declared in the lib-b 2.0.0 upstream POM; lib-c never named in app/pom.xml"},
}

EVENTS = {
    "initial_poms": "Creates parent pom.xml and module app/pom.xml with no external dependency.",
    "add_lib_b": "Direct introduction of com.example:lib-b 1.0.0 in app/pom.xml.",
    "comment_and_code_only": "Comment-only app/pom.xml touch plus a code edit; the last modification of the code file differs from the introduction commit.",
    "remove_lib_b": "Removal of com.example:lib-b from app/pom.xml.",
    "reintroduce_lib_b": "Reintroduction of com.example:lib-b 1.0.0 in app/pom.xml as a distinct commit from add_lib_b.",
    "upgrade_lib_b_2": "Version-only upgrade of com.example:lib-b from 1.0.0 to 2.0.0 in app/pom.xml; artifactId unchanged. Candidate introduction point for transitive com.example:lib-c: lib-c is never named in any app/pom.xml revision, and upstream snapshots give candidate evidence only, not a resolved or shipped fact.",
    "property_version_bump": "Property-controlled version change: the lib-b.version property 3.0.0 drives ${lib-b.version}; additional candidate lead.",
    "bom_upgrade": "BOM upgrade: corp-bom 1.0.0 to 2.0.0 in parent pom.xml only; additional candidate lead, app/pom.xml untouched.",
}

STEPS = (
    ("initial_poms", "Initial parent and module POMs", "2024-01-15T10:00:00+00:00",
     {PARENT_POM_RELPATH: _parent_pom("1.0.0"), POM_RELPATH: _app_pom(), APP_CODE_RELPATH: APP_CODE_V1}),
    ("add_lib_b", "Add lib-b 1.0.0 to the app module", "2024-02-01T09:30:00+00:00", {POM_RELPATH: _app_pom(dep=LIB_B_1)}),
    ("comment_and_code_only", "Document pin and tweak greeting code", "2024-02-20T14:45:00+00:00",
     {POM_RELPATH: _app_pom(dep=LIB_B_1, comment=COMMENT), APP_CODE_RELPATH: APP_CODE_V2}),
    ("remove_lib_b", "Drop lib-b from the app module", "2024-03-05T11:15:00+00:00", {POM_RELPATH: _app_pom(comment=COMMENT)}),
    ("reintroduce_lib_b", "Bring lib-b 1.0.0 back", "2024-03-18T16:20:00+00:00", {POM_RELPATH: _app_pom(dep=LIB_B_1, comment=COMMENT)}),
    ("upgrade_lib_b_2", "Upgrade lib-b to 2.0.0", "2024-04-02T08:05:00+00:00", {POM_RELPATH: _app_pom(dep=LIB_B_2, comment=COMMENT)}),
    ("property_version_bump", "Move lib-b version to a property", "2024-04-22T13:40:00+00:00",
     {POM_RELPATH: _app_pom(dep=LIB_B_PROP, comment=COMMENT, props=PROPS)}),
    ("bom_upgrade", "Import corp-bom 2.0.0", "2024-05-10T09:55:00+00:00", {PARENT_POM_RELPATH: _parent_pom("2.0.0")}),
)
SUBJECTS = {key: subject for key, subject, _, _ in STEPS}
DATES = {key: date for key, _, date, _ in STEPS}


@dataclass(frozen=True)
class DependencyHistoryFixture:
    data_root: Path
    repo_dir: Path
    upstream_dir: Path
    git_binary: str
    env: dict
    commits: dict
    events: dict
    pom_relpath: str = POM_RELPATH
    parent_pom_relpath: str = PARENT_POM_RELPATH

    @property
    def commit_order(self):
        return tuple(self.commits)

    def run(self, *args, check=True, env_extra=None):
        done = subprocess.run(
            [self.git_binary, "-C", str(self.repo_dir), *args], env={**self.env, **(env_extra or {})},
            text=True, capture_output=True, check=False,
        )
        if check and done.returncode:
            raise subprocess.CalledProcessError(done.returncode, done.args, done.stdout, done.stderr)
        return done

    def text(self, *args):
        return self.run(*args).stdout

    def show(self, rev, relpath):
        return self.text("show", f"{rev}:{relpath}")


def _git_binary():
    found = shutil.which("git")
    if not found:
        raise RuntimeError("git executable not found on PATH")
    return os.path.abspath(found)


def _clean_env(root):
    for name in ("home", "git-templates", "empty-hooks"):
        (root / name).mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(root / "home"),
        "TZ": "UTC", "LC_ALL": "C", "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_COUNT": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_TEMPLATE_DIR": str(root / "git-templates"),
    }


def _identity_env(date):
    roles = (("AUTHOR", AUTHOR_NAME, AUTHOR_EMAIL), ("COMMITTER", COMMITTER_NAME, COMMITTER_EMAIL))
    return {f"GIT_{role}_{field}": value for role, name, mail in roles
            for field, value in (("NAME", name), ("EMAIL", mail), ("DATE", date))}


def _build(root):
    repo = root / "repo"
    upstream = root / "upstream"
    repo.mkdir()
    upstream.mkdir()
    fixture = DependencyHistoryFixture(root, repo, upstream, _git_binary(), _clean_env(root), {}, dict(EVENTS))
    fixture.run("init", "--initial-branch=main", "--quiet")
    for key, value in (
        ("core.hooksPath", str(root / "empty-hooks")), ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
        ("gc.auto", "0"), ("gc.autoDetach", "false"), ("core.autocrlf", "false"),
        ("user.name", AUTHOR_NAME), ("user.email", AUTHOR_EMAIL),
    ):
        fixture.run("config", key, value)
    files = {}
    for key, subject, date, updates in STEPS:
        files.update(updates)
        for relpath, content in files.items():
            target = repo / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        fixture.run("add", "-A")
        fixture.run("commit", "--no-verify", "--quiet", "-m", subject, env_extra=_identity_env(date))
        fixture.commits[key] = fixture.run("rev-parse", "HEAD").stdout.strip()
    (upstream / "lib-b-1.0.0.pom").write_text(_upstream_pom("1.0.0"), encoding="utf-8")
    (upstream / "lib-b-2.0.0.pom").write_text(_upstream_pom("2.0.0", dep=LIB_C), encoding="utf-8")
    (upstream / "snapshots.json").write_text(json.dumps(SNAPSHOTS, indent=2) + "\n", encoding="utf-8")
    return fixture


@contextmanager
def dependency_history_fixture():
    root = Path(tempfile.mkdtemp(prefix="mdi-history-fixture-"))
    try:
        yield _build(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def missing_history_fixture():
    with dependency_history_fixture() as fixture:
        shutil.rmtree(fixture.repo_dir / ".git")
        yield replace(fixture, commits={})

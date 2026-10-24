# Third-party notices for the mdi usage helper

Scope: the runtime closure packaged by `build_local.py` (`helper.jar` plus the
jars under `lib/`). Every statement below was read from the actual local
artifacts on 2026-09-11: jar entry contents and the published POMs, all
sha256-pinned in `DEPENDENCIES.json`. The three license texts that no jar
embeds were retrieved verbatim from their official sources over public HTTPS
on 2026-09-11, with explicit user authorization for that one-time download;
they are vendored under `LICENSES/` (see `LICENSES/README.md`), pinned by
sha256 in `DEPENDENCIES.json`, and copied byte-for-byte into the package by
`build_local.py`, which itself performs no network access. No license text
has been invented, paraphrased or reconstructed. These notices record text
provenance only; they are not a legal-clearance determination, and they do
not by themselves assert that the packaged helper is releasable.

The four jars below are the complete runtime closure: the analyzer is declared
directly, its published POM declares `org.ow2.asm:asm:9.10.1` (compile scope),
gson is declared directly, and its published POM declares
`com.google.errorprone:error_prone_annotations:2.48.0` (compile scope). The
closure was observed in the pinned dependency directory and verified by
sha256; no other jar is on the helper classpath.

## 1. org.apache.maven.shared:maven-dependency-analyzer:1.17.2

- Jar: `maven-dependency-analyzer-1.17.2.jar`, sha256
  `f4d83b15827ca61325718a5e80f57205da0567ab311bf0ec1646c8106f78255d`.
- Published POM (sha256
  `160f890cc71ee994c22f66cd8f94f52bb2de50606a0d9a71b68a907086d1dea8`)
  declares no `<licenses>` element; the artifact itself carries the full
  license and notice:
  - `META-INF/LICENSE`: full Apache License, Version 2.0 text, sha256
    `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`,
    verified byte-identical to the `LICENSE` file of the exact-tag source
    checkout (git tag `maven-dependency-analyzer-1.17.2`).
  - `META-INF/NOTICE` (sha256
    `4e6fe9752f1fac810d2152ef2f6850ded2bad8ed1f46c39f60e98a7268bb98f9`),
    reproduced verbatim:

        Apache Maven Dependency Analyzer
        Copyright 2002-2026 The Apache Software Foundation


        This product includes software developed at
        The Apache Software Foundation (http://www.apache.org/).

- `build_local.py` extracts both entries verbatim into
  `licenses/maven-dependency-analyzer-1.17.2/` (as `LICENSE` and `NOTICE`).

## 2. org.ow2.asm:asm:9.10.1

- Jar: `asm-9.10.1.jar`, sha256
  `ed825d10ab1399c8c0cb669e688cf0c8c82629b4c8399b58352b68e92ca10fcb`.
- Published POM (sha256
  `e1b1c3832ca3ac0a9e563c405f4c760118b5556a4920daee14058886197e094d`)
  declares license `BSD-3-Clause` with URL `https://asm.ow2.io/license.html`
  and `<scm>` `https://gitlab.ow2.org/asm/asm/`.
- The jar embeds no license or notice text. The BSD-3-Clause text is vendored
  in `LICENSES/asm-9.10.1-BSD-3-Clause.txt`, sha256
  `293b6af371eee28b0ff16f0334ea19e20a3d5522143faa4b95b346855507879a`
  (1598 bytes), retrieved 2026-09-11 19:48 UTC over public HTTPS from the
  exact-tag source of the pinned artifact:
  `https://gitlab.ow2.org/asm/asm/-/raw/ASM_9_10_1/LICENSE.txt`, where tag
  `ASM_9_10_1` points at commit
  `38c169b1d7609085c8fdcede23767899ae4e1d77` ("Set version to 9.10.1.") in
  the repository the POM itself declares. The declared license page
  `https://asm.ow2.io/license.html` (fetched 2026-09-11 19:45 UTC) embeds
  the same 25-line text in a `<pre>` block; it is whitespace-normalized
  identical to the vendored file, which preserves the exact-tag bytes
  unmodified, including the copyright line
  `Copyright (c) 2000-2011 INRIA, France Telecom`.
- `build_local.py` sha256-verifies the vendored file before javac and copies
  it byte-for-byte into `licenses/asm-9.10.1/LICENSE`.

## 3. com.google.code.gson:gson:2.14.0

- Jar: `gson-2.14.0.jar`, sha256
  `2cbd119bf1961c28788310963dc80ba65f58cdeec1dd139c8bdb1240faa2c36f`.
- Published POM (sha256
  `1fa0122c0e3733192642861c36be4c6f799a795327a23bdd2922691b6e9ba480`)
  declares license `Apache-2.0` with URL
  `https://www.apache.org/licenses/LICENSE-2.0.txt`.
- The jar embeds no license or notice entry anywhere. The canonical Apache
  License 2.0 text is vendored in `LICENSES/apache-2.0.txt`, sha256
  `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`
  (11358 bytes), retrieved 2026-09-11 19:45 UTC over public HTTPS from
  exactly the URL the POM declares. That canonical file is byte-identical
  (same sha256) to the `META-INF/LICENSE` embedded in the pinned analyzer
  jar, so all three Apache-2.0 dependencies in this package carry the same
  verified text. Since the gson jar embeds no NOTICE file, no NOTICE text
  is reproduced for it.
- `build_local.py` copies the vendored file byte-for-byte into
  `licenses/gson-2.14.0/LICENSE`.

## 4. com.google.errorprone:error_prone_annotations:2.48.0

- Jar: `error_prone_annotations-2.48.0.jar`, sha256
  `b49c5c958316ed67a09c699dda9aa749caf434d51d863dea599ef36a49b9c855`.
- Published POM (sha256
  `189e09ad0489c325e90daaa9d5c8f406330602fa366609bca07cbcdad03ab9da`)
  declares license `Apache 2.0` (as spelled there) with URL
  `http://www.apache.org/licenses/LICENSE-2.0.txt`.
- The jar embeds no license or notice entry anywhere. Its declared URL is
  the same canonical Apache file as gson's, over `http://`; retrieval used
  the `https://` upgrade of that exact URL (2026-09-11 19:45 UTC) and
  yielded the byte-identical canonical text, so this dependency shares
  `LICENSES/apache-2.0.txt` with gson, each pinned separately in
  `DEPENDENCIES.json`. Since the jar embeds no NOTICE file, no NOTICE text
  is reproduced for it.
- `build_local.py` copies the vendored file byte-for-byte into
  `licenses/error_prone_annotations-2.48.0/LICENSE`.

## Blocker status

The three former blockers (missing ASM/Gson/error-prone license texts) are
resolved with the recorded provenance above; `build_local.py` now reports
zero license blockers and packages a LICENSE for every dependency. The
mechanism is retained: any future dependency whose text is pinned nowhere
is still reported as an explicit BLOCKER (stderr and `BUILD-INFO.json`)
instead of being filled with boilerplate. Resolving the text-provenance
blockers is a provenance statement, not a legal-clearance decision: whether
the package may actually be redistributed remains with the project owner.

## Build prerequisites and invocation

Documented in the `build_local.py` module docstring: Python 3.11+ standard
library, a local javac supporting `--release 17`, and the pinned jars supplied
explicitly through `--dependency-dir`; the output directory must not exist.
`DEPENDENCIES.json` pins identities only and carries no host paths or
defaults; vendored license texts live under `LICENSES/` next to it and are
addressed by manifest-relative paths that the builder validates (no
absolute paths, no traversal, no symlinks). The script is a maintainer-time
tool: it is never run during an investigation, it never downloads anything,
and the investigation-time runner consumes the already built `helper.jar`
and `lib/` as-is.

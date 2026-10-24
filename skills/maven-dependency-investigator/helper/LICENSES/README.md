# Vendored license texts

License texts for dependencies whose pinned jars embed no license bytes.
Each file was retrieved once, over public HTTPS, from the official source
recorded below (explicit user authorization, 2026-09-11); nothing is fetched
at build time. `build_local.py` copies these files byte-for-byte into
`licenses/<dependency>/LICENSE` of the package and sha256-pins them through
`../DEPENDENCIES.json` (the `vendored-file` text kind; the manifest is the
authoritative pin). The files themselves are verbatim; nothing was edited,
re-wrapped or reconstructed.

## apache-2.0.txt

- sha256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`,
  11358 bytes, LF line endings.
- Retrieved 2026-09-11 (19:45 UTC) from
  `https://www.apache.org/licenses/LICENSE-2.0.txt` (HTTP 200, text/plain).
- Canonical Apache License 2.0 text; byte-identical (same sha256) to the
  `META-INF/LICENSE` entry embedded in the pinned
  `maven-dependency-analyzer-1.17.2.jar`.
- Used for `com.google.code.gson:gson:2.14.0`, whose published POM declares
  exactly this URL, and for
  `com.google.errorprone:error_prone_annotations:2.48.0`, whose POM declares
  the same file over `http://` (retrieved over `https://`; same canonical
  file). Neither jar embeds any NOTICE or license entry.

## asm-9.10.1-BSD-3-Clause.txt

- sha256 `293b6af371eee28b0ff16f0334ea19e20a3d5522143faa4b95b346855507879a`,
  1598 bytes, LF line endings.
- Retrieved 2026-09-11 (19:48 UTC) from the exact-tag source of the pinned
  artifact: `https://gitlab.ow2.org/asm/asm/-/raw/ASM_9_10_1/LICENSE.txt`
  (HTTP 200, text/plain). The repository
  `https://gitlab.ow2.org/asm/asm` is the official source, declared by the
  pinned POM's own `<scm>` element; tag `ASM_9_10_1` points at commit
  `38c169b1d7609085c8fdcede23767899ae4e1d77` ("Set version to 9.10.1.").
- The POM declares the license as `BSD-3-Clause` with URL
  `https://asm.ow2.io/license.html`; that page (fetched 2026-09-11, 19:45 UTC)
  embeds the same 25-line text in a `<pre>` block, whitespace-normalized
  identical to this file. The exact-tag plain-text file is the copy vendored
  here, unmodified.

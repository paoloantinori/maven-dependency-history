# Maven investigator feasibility evidence

Date: 2026-09-10
Tracking: TASK-18
Status: Feasibility probes verified; completion review pending. This is not a production-readiness claim.

## Scope and consent

The user approved a batch building our helper and synthetic Maven projects in temporary directories, downloading pinned public artifacts from Maven Central, and querying OSV with public coordinates. They separately approved the later verbose effective-POM invocation. No project commit or push was authorized or performed. Synthetic Git fixture commits were confined to a temporary test repository.

Probe root: `/tmp/maven-investigator-probe.WKTQpn/`. Helper source, fixtures, build logs, isolated Maven repository and outputs are under `helper-probe/`. These temporary files are local supporting artifacts, not a portable released fixture suite. Product tasks must retain reproducible fixtures before release.

## Real upstream reference proof

The temporary Java helper wraps Apache maven-dependency-analyzer 1.17.2 through:

```java
new ASMDependencyAnalyzer().analyzeUsages(url, new ClassesPatterns());
new DefaultClassAnalyzer().analyze(url, new ClassesPatterns());
```

Exact-tag source and published JAR signatures were checked; the downloaded analyzer matches the inspected research JAR byte for byte (SHA256 `f4d83b15827ca61325718a5e80f57205da0567ab311bf0ec1646c8106f78255d`). No custom bytecode parser was used. Resolved helper dependencies were analyzer 1.17.2, ASM 9.10.1, Gson 2.14.0 and error_prone_annotations 2.48.0. These are observed probe pins, not claims about latest versions.

The fixture is application A referring to B, B referring to C, and no direct A reference to C. Test-first RED logs show the absent helper class; after implementation, both JAR-root and exploded-directory-root tests passed. The parent reran:

```text
python3 /tmp/maven-investigator-probe.WKTQpn/helper-probe/test/test_helper.py -v
Ran 2 tests
OK
```

The independent compliance reviewer also executed the helper and confirmed the same edges. An independent quality reviewer approved the bounded proof. Both approvals explicitly exclude production readiness.

The probe's duplicate ownership is first-wins with an additional duplicate-owner list. That is not acceptable production attribution; TASK-21 requires ambiguity-preserving ownership and duplicate-class fixtures. Multi-release JAR handling was not exercised. Artifact edges omit all references within the same input entry; raw class usages retain them. Empty helper stdout logs are not the evidence: JSON in `out/usage-report-dir.json` and `out/usage-report-jar.json`, plus repeated live tests, are the substantive evidence.

## Maven producer behavior

All probe Maven runs used a temporary local repository and the same isolated settings file for both user and global settings. The settings contain a Central-only mirror. Personal Maven settings were not supplied. The later parent invocation also cleared Maven/JVM option environment overrides. No claim that directory mtime alone proves absence of writes is made.

Dependency Plugin 3.11.0 was exercised with non-verbose and verbose text/JSON trees and build-classpath. A synthetic conflict selects direct Gson 2.14.0 over transitive Gson 2.10.1.

- Non-verbose JSON omits the losing version; classpath output corroborates the selected version for this fixture/context.
- Verbose JSON includes the losing version without omission/conflict metadata. It must remain diagnostic and cannot establish resolved inclusion by itself.
- Duplicate-version spotting is not a sound substitute for producer conflict-state evidence.
- The observed JSON optional field is a string, not a boolean.
- Multi-module output placement and detailed profile/BOM mediation remain product integration-test obligations.

Help Plugin 3.5.2 ordinary effective-pom succeeded but did not establish origin metadata. The separately authorized parent run added `-Dverbose=true` and wrote `out/effective-pom-verbose.xml`. The log contains BUILD SUCCESS. A comment-preserving XML parse verifies origins for Gson fields at fixture lines 18, 19 and 20 and converter-gson fields at lines 23, 24 and 25. These locations match the inspected fixture POM.

Verbose XML SHA256: `f08ef4e7fea52547be46ac35c0f06edd4900ee5debe056f46aca870806c0e4b8`.

This establishes direct declaration origins in the fixture, not complete parent/BOM/profile behavior.

## Security lookup

Official query documentation was searched and read at https://google.github.io/osv.dev/post-v1-query/. The authorized request was:

```json
{"package":{"ecosystem":"Maven","name":"org.apache.logging.log4j:log4j-core"},"version":"2.14.1"}
```

A bounded HTTPS POST to `https://api.osv.dev/v1/query`, without redirect following, returned advisory records including alias CVE-2021-44228. Its affected entries preserve separate release-branch ranges. The response had no continuation token. The provider documents pagination and fuzzy upstream-version matching; retain the exact submitted identity and label applicability provider-reported, not independently proved exact-version equivalence.

Response path: `osv-log4j-response.json`. SHA256: `955f2ff3ac97c8ba2c9fdbd6fad5cb38c3366b0842b8c81a559647576e19836c`.

Actual modification field is `modified`, not the candidate plan's guessed `lastModified`. Product tests must cover pagination, disjoint intervals, denied requests, errors and destination changes without making live requests by default.

## Local safety mechanisms

```text
python3 /tmp/maven-investigator-probe.WKTQpn/test_xml_probe.py
Ran 4 tests
OK

python3 /tmp/maven-investigator-probe.WKTQpn/test_git_probe.py
Ran 1 test
OK
```

XML tests reject DTD/entity constructs through encoding-aware Expat callbacks on UTF-8, UTF-16 and UTF-16BE, including a long prolog. Ordinary Unicode and comments survive, XInclude remains unexpanded, and oversized/malformed inputs are rejected. This is a mechanism probe, not proof of complete resource-bounded production XML inspection.

Git log/show/blame/status were tested individually in a synthetic hostile repository with textconv, external-diff, fsmonitor and pager helpers configured to write a sentinel. Sanitized environment and command-specific controls prevented those markers and left index content unchanged. Runtime compatibility and missing-object no-lazy-fetch behavior still need product fixtures; these checks do not prove every Git operation safe.

A local synthetic Boot-style archive containing `BOOT-INF/lib/libB.jar` and `libC.jar` was read with zipfile. Nested bytes matched original digests and class entries were observable. No Spring Boot launcher ran. Full cumulative archive resource limits, traversal rejection and production discovery remain TASK-20 obligations.

## Handoff

TASK-19 can use the proved APIs and safety mechanisms after TASK-18 gates close. Do not copy the temporary helper wholesale as production code. Preserve the approved report/permission contracts, reuse fixture lessons, and implement required resource/identity constraints with tests. TASK-20, TASK-21 and TASK-22 record the archive, usage and producer limitations above.

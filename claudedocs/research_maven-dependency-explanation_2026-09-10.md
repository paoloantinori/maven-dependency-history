# Research Report: Explaining Maven Dependencies to a Coding Agent

**Date:** 2026-09-10.  
**Depth:** Exhaustive.  
**Confidence:** HIGH for documented component capabilities; MEDIUM for the bounded assessment of an unmet end-to-end need.  
**Scope:** Existing tools, reusable capabilities, limitations, and evidence for a later brainstorming session. No implementation or architecture selection.

## Executive summary

**Much of this idea already exists, but as separate capabilities rather than a single verified end-to-end Maven explanation.** Maven supplies dependency paths and declaration origins; Apache's analyzer and DepClean supply usage evidence; Git and dependency-review systems supply change history; security vendors already expose dependency intelligence to coding agents. The strongest potential distinction is a source-cited explanation connecting the actual shipped component, its technical use, its introduction, recorded intent, and security context. I did not find a publicly documented tool demonstrating that complete combination; that is a bounded search result, not proof of novelty. [3][8][9][25][28][33][35]

## 1. What does “why is this dependency here?” mean?

These are different questions, requiring different evidence:

- **Inclusion:** Which direct and transitive relationships introduce the artifact?
- **Selection:** Why this version, scope, type, and classifier rather than another?
- **Declaration origin:** Which module, parent POM, imported BOM, property, or active profile controls it?
- **Delivery:** Is the library actually inside the shipped JAR, WAR, image, or distribution, rather than merely on a build classpath?
- **Usage:** Which application or library classes reference it? Is the evidence from production code, tests, configuration, or an observed execution?
- **Purpose:** What capability does that usage implement in this application?
- **History:** When did the declaration, selected version, transitive relationship, and packaged presence first appear?
- **Intent and attribution:** What did the relevant commit, PR, issue, or design document actually say? Who authored the change, and who approved it?
- **Security:** Which advisories affect the exact component, what usage is reachable, and what uncertainty remains?

This decomposition is research synthesis, not a proposed architecture. Maven's own documentation distinguishes dependency mediation, management, scopes, exclusions, and optional dependencies. Resolver further distinguishes collection, conflict resolution, classpath flattening, and artifact retrieval. [1][2]

A BOM can control a version without itself introducing the corresponding runtime library. Project dependency management also does not govern plugin dependencies. Confusing either distinction would produce a convincing but incorrect answer. [1]

## 2. Existing Maven building blocks

### Maven Dependency Plugin

The official documentation already uses almost the user's wording: filtering the tree can explain “why Velocity is being used,” then shows the chain that brings it in. [3]

Documentation-backed examples, not commands executed against a sample application in this research:

```bash
# Show the dependency path for an artifact.
mvn dependency:tree -Dincludes=groupId:artifactId

# Include omitted nodes for resolution investigation.
mvn dependency:tree -Dverbose

# Produce a machine-readable dependency tree.
mvn dependency:tree -DoutputType=json -DoutputFile=dependency-tree.json

# Analyze declared and observed bytecode dependencies.
mvn dependency:analyze

# Show effective declarations with origin comments.
mvn help:effective-pom -Dverbose

# Identify the build's active profiles.
mvn help:active-profiles
```

`dependency:tree` supports filtering and structured output. The goal reference documents JSON support since plugin version 3.7.0; the documentation fetched for this research identifies the plugin as 3.11.0 and its example site publication date as 2026-05-24. Pinning a tested plugin version would be necessary in an implementation rather than assuming whatever version a project resolves has the same output contract. [3][4]

`dependency:analyze` distinguishes used-and-declared, used-but-undeclared, and declared-but-unused dependencies. Its default analyzer operates on bytecode. The standalone goal invokes `test-compile`; `analyze-only` is the lifecycle-oriented alternative. Therefore this is not just passive POM parsing, and running it on an untrusted repository deserves the same caution as executing a build. [5][6]

Important output limitation: `outputXML` and `scriptableOutput` are documented for missing declarations, not as comprehensive structured usage reports. In the inspected plugin release, verbose output prints classes for used-but-undeclared dependencies, while used-and-declared dependencies are printed as artifacts. The richer class-usage API described below is not fully surfaced by this CLI output. I checked that distinction in release-tag source and the published plugin bytecode. The `analyzer` extension point can substitute another analyzer. Do not assume ordinary goal output provides source locations, method-level reachability, or business purpose. [5][26]

Other useful goals include `dependency:build-classpath`, `dependency:resolve-sources`, and `dependency:analyze-dep-mgt`. `dependency:resolve-plugins` separately resolves project plugins, reports, and their dependencies. [7]

**Confidence: HIGH for the documented capabilities.** No representative Maven application was built in this research.

### Maven Help Plugin

`help:effective-pom -Dverbose` is particularly relevant: it displays the effective model with active profiles applied and adds comments identifying element origins. `help:active-profiles` identifies active profiles and their sources. These answer “where did this declaration come from?” rather than “what was the author's intention?” [8]

An effective POM is build-context evidence, not a substitute for the resolved dependency tree or an inspection of the final archive.

### Apache's analyzer already has class-to-artifact usage data

The lower-level `maven-dependency-analyzer` deserves separate attention from its CLI wrapper. Its `ProjectDependencyAnalysis` API exposes `getUsedDeclaredArtifactsWithUsages()` and `getUsedUndeclaredArtifactsWithUsages()`. Each `DependencyUsage` identifies the referenced dependency class and the project class using it through `getDependencyClass()` and `getUsedBy()`. [25]

I verified these declarations in the exact `1.17.2` release source and confirmed their signatures with `javap` against the published Maven Central JAR. This is direct evidence of a reusable class-to-artifact explanation building block. It does not establish method call paths, source line locations, framework configuration coverage, or business intent. It also does not mean every version of the Maven Dependency Plugin bundles that analyzer release. [25]

**Confidence: HIGH for API availability in the inspected artifact.** This substantially weakens any claim that class-to-dependency explanation requires writing a new analyzer from scratch.

### DepClean: a strong Maven transitive-usage lead

DepClean is explicitly aimed at detecting unused direct, transitive, and inherited dependencies. Its documented outputs include a dependency tree enriched with usage metadata and an optional CSV described as a static call graph. The JSON implementation includes artifact coordinates, scope, classifier, size, status, all types, and used types. [9]

This makes it substantially closer to the requested feature than a tree visualizer alone. It also has an academic foundation, *A Comprehensive Study of Bloated Dependencies in the Maven Ecosystem*, and a companion visualization project, DepClean Web. The study was submitted to arXiv on 2020-01-21 and published in Empirical Software Engineering in 2021. Its historical experimental results should not be generalized into current prevalence claims. [10][11]

**Verification beyond the README:** I checked the `2.2.0` release tag and downloaded the corresponding Maven Central plugin artifact. The compiled plugin contains `createResultJson` and `createCallGraphCsv`; source inspection confirms JSON usage fields. This verifies that the interfaces are present, not their accuracy on a real application. [12]

**Integration caveat:** The release source and artifact expose inconsistent CSV descriptions and serialization. The generated header names `OriginClass,TargetClass,OriginDependency,TargetDependency`, while the Maven adapter's writer constructs a row from a source string, target string, and current artifact coordinate. Released bytecode confirms that writer shape. Consequently, the CSV must not be treated as a validated method-level or artifact-attribution contract. This is a research qualification, not a tested end-to-end failure report. [12]

**Confidence: HIGH that DepClean is relevant and exposes usage data; MEDIUM on suitability as a reliable drop-in explanation backend without fixture-based evaluation.**

### Other relevant tools

- **JDK `jdeps`:** Reports package-level and class-level dependencies, supports recursive and inverse analysis, and can emit DOT. Useful for identifying which bytecode references which library classes; not an explanation of developer intent or a function-level exploitability proof. [13]
- **Ferstl `depgraph-maven-plugin`:** Provides module, aggregate, reactor, and arbitrary-artifact graphs, including JSON. Its documentation explicitly warns that aggregate graphs cannot meaningfully preserve all per-module conflict and duplicate perspectives. Useful presentation and graph-export precedent. [14]
- **DepTrim:** Specializes dependency archives to retain needed classes and interfaces. Relevant adjacent research, but its mutation-oriented purpose is different from explaining a dependency. [15]
- **Moditect Deptective:** Validates intended package relationships during compilation. It is an architectural-boundary tool, not a Maven dependency-history explainer. [16]
- **SootUp:** Supplies bytecode analysis and call-graph construction. This is a lower-level analysis framework to investigate if class references are insufficient, not a ready-made end-to-end feature. [17]
- **MojoHaus License Maven Plugin:** Produces dependency/license inventories, including multi-module reports. These are license evidence inputs, not a legal compatibility verdict. [18]
- **Maven Artifact Plugin:** Records build results in buildinfo files and compares produced artifacts against published references. It also documents experimental build-output and Reproducible Central reports. Useful evidence for identity and reproducibility, but not an explanation of application purpose or a security guarantee. [23]

## 3. Strong precedents outside Maven

### Gradle's native dependency insight

Gradle's `dependencyInsight` identifies a dependency's version-selection reason and origin within a particular configuration. More importantly for the intent question, Gradle permits an explicit `because(...)` reason on dependency declarations and displays it in the insight report. [19]

This is a useful distinction: an explanation can be **recorded by the developer** rather than **reconstructed by an agent**. Neither a coordinate nor a call graph contains a missing business justification.

### Autonomous Apps Dependency Analysis Gradle Plugin

Its `reason` task is a particularly close interaction precedent. The README shows a request about a specific dependency, a shortest dependency path, and usage evidence such as exposing a class through an API. It also reports unused dependencies, used transitives, configuration problems, and unused annotation processors. [20]

This is a Gradle plugin, not something directly applicable to Maven. Nevertheless, “ask about dependency X and receive the dependency path plus usage reasons” is demonstrably existing functionality, not a novel interaction in itself.

**Confidence: HIGH for these documented precedents.**

## 4. Why the final artifact matters

A dependency graph and a shipping inventory are not interchangeable.

Spring Boot's Maven documentation describes nested application dependencies in executable archives and explicitly includes `provided` dependencies during repackaging. Therefore “provided means absent from the shipped output” is not a generally valid rule. Maven Shade can relocate classes and rewrite bytecode, so an embedded copy need not appear under its original package names or as a separate dependency JAR. [21][22]

The Spring Boot page also contains a documentation conflict concerning optional dependencies: one section says they are excluded by default and exposes `includeOptional`, while the dependency-exclusion section says optional dependencies are included. This research does not choose one statement as authoritative for every version. Any concrete assessment must check the project's plugin version/configuration and inspect its actual artifact. [21]

An explanation should therefore identify which object it describes: declared model, resolved compile/test/runtime classpath, plugin environment, packaged archive, or observed running process. This is a correctness requirement inferred from the documented distinctions, not a commitment to implement every mode.

## 5. Limits of “which code uses it?”

The Maven analyzer explicitly documents missed uses involving constants, source-retention annotations, and other references absent from bytecode. Its dependency-analysis guide also names runtime reflection as a cause of necessary libraries being reported unused. Java's `ServiceLoader` documentation provides a concrete configuration-based case: classpath providers are named in `META-INF/services` resources and loaded on demand. [5][6][24]

Consequences for interpreting any usage evidence:

- A Maven dependency edge means that dependency metadata introduces a component. It is not a call edge.
- A class reference is stronger evidence of technical usage, but it does not establish that a particular vulnerable method runs.
- No detected reference means “not found by this analysis,” not “safe to delete.” Framework configuration, service discovery, reflection, native loading, generated code, and test coverage need separate consideration.
- Observing a runtime use proves that use occurred in the observed workload. Not observing it does not prove it never occurs.
- “What is it used for?” requires connecting usage sites to application behavior. A library's generic description is not evidence that this project uses that feature.

The last points are analytical implications, not measured accuracy claims about any named product.

## 6. History, attribution, and intent

### Git provides evidence, not an automatic explanation

`git blame` identifies the revision and author that last modified a surviving line. Its manual explicitly warns that deleted or replaced lines require other tools. Git's pickaxe search locates changes containing additions/removals of a string; this is a starting point for finding a dependency's introduction, not a guarantee that the oldest matching POM line represents its first inclusion. [27]

A defensible historical answer has to distinguish:

- The commit adding an explicit declaration.
- The parent, BOM, profile, or direct-dependency change that first brought a transitive component into the resolved graph.
- The change selecting the current version.
- The first build or release actually containing the component.
- The current author of a line, the original change author, the committer, and a reviewer or decision-maker.

These events can differ. A library may enter through an upgrade without its name ever appearing in the application's POM. A later formatting change can move blame without changing the dependency decision. These are consequences of combining Git's line-oriented history with Maven's transitive resolution model. [1][27]

**Intent must have an evidence label.** A commit message, PR discussion, issue, ADR, or POM comment can record a reason. Code can support a technical-purpose inference. Neither proves an undocumented intention. “No recorded rationale found” is a legitimate answer; inventing one is not.

### GitHub Dependency Review is a meaningful partial solution

GitHub documents dependency additions, removals, updates, release dates, and vulnerability information in dependency reviews. Its dependency-review API compares dependency changes between commits and includes dependency-submission data. Automatic submission supports Maven transitive dependency data. This is useful change evidence, but not historical source-use analysis or an automatic explanation of the author's motivation. Availability of historical snapshots and successful resolution still matters. [28]

### Freshli and Dependency History Maven

Freshli explicitly tracks historical metrics about dependency freshness. Its ecosystem includes a Java language agent, a `dependency-history-maven` library, and a fork of the Versions Maven Plugin described as resolving ranges to versions available on a particular date. [29]

**The name is easy to misread:** Dependency History Maven retrieves a package's *release history from a Maven repository*. It does not claim to recover when your application introduced that package or why. Its README also warns that alternate-repository queries still consult Maven Central and select the oldest date found. That is a material provenance and privacy qualification. [29]

Freshli is relevant prior art for reconstructing dependency evolution. Its README contains older roadmap language; this research did not verify a maintained, production-ready hosted service or current compatibility matrix.

Historical reconstruction also needs the historical build context. Maven's reproducibility documentation requires stable dependencies and identifies environment sensitivity. Today's rebuild of an old checkout is evidence of today's resolution, not necessarily the original delivered binary. [23]

**Confidence: HIGH for these documented primitives; MEDIUM for practical recovery of arbitrary projects' historical graphs; LOW for human intent without recorded evidence.**

## 7. Security position: more than a vulnerability list

### Existing open-source foundations

**OWASP Dependency-Check** has a Maven plugin and identifies known vulnerable components. Its documentation explains evidence-based identification and explicitly warns of false positives and false negatives. A finding is not proof of an executable attack path. [30]

**CycloneDX Maven Plugin and OWASP Dependency-Track** cover a different layer. The plugin generates an SBOM containing direct and transitive dependencies, with scope and module configuration. Dependency-Track consumes SBOMs, tracks components across application versions, monitors vulnerabilities and license/operational risk, and supports VEX. An SBOM generated from a build model must not automatically be described as a forensic inventory of the delivered archive. VEX carries an assessment; its existence does not itself prove the assessment. [31]

**OSV-Scanner** documents transitive Maven POM scanning using the deps.dev resolver, with options for native Maven repository data. Its current support page explicitly says test dependencies are not supported in that computed Maven graph. That makes it useful for security lookup but not an unquestioned replacement for the project's own Maven resolution. [32]

OSV also has Java JAR reachability and experimental MCP precedents in its changelog. A significant documentation trap emerged: the source-scanning call-analysis page lists Go and Rust, while the changelog separately records Java JAR reachability. Therefore “OSV has no Java reachability” would be wrong, but “ordinary POM scanning gives complete Java function reachability” is also unsupported. JAR-mode accuracy and its precise contract were not tested here. [32]

**Eclipse Steady, formerly Vulas**, is strong prior art for Java vulnerability assessment using static and dynamic execution evidence, with a Maven scan client. Its own README warns about reliance on Project KB and limited coverage in the period described by that disclaimer, and notes the operational cost of its backend. Treat it as research and reuse inspiration, not a verified current comprehensive vulnerability feed. [34]

### Commercial overlap is substantial

**Endor Labs** documents Maven dependency resolution, Java call graphs, dependency-level and function-level reachability, and an MCP interface. Its Java guide distinguishes quick scans from deep scans and requires build artifacts for deep analysis. It explicitly lists unsupported reflection and callback analysis. The broader reachability page uses stronger language about exploitability than those limitations justify as a universal guarantee. Report the call-path evidence and its coverage, not “unreachable therefore safe.” [33]

**Snyk** documents Java/Maven reachability and a coding-agent MCP server. Its reachability documentation explicitly warns that no path found does not establish impossibility and that results can change as code or analysis changes. It also distinguishes availability by integration; the fetched page lists Java/Maven as generally available while some CLI/CI integrations remain early access. MCP availability does not prove every reachability feature is exposed through every integration. [35]

### What a useful security explanation would separate

This is a synthesis of evidence categories, not an implemented scoring system:

- Exact component identity, selected version, and artifact provenance.
- Advisory identifiers, affected ranges, fixed versions, and evidence timestamp.
- Whether the component is shipped, build-only, test-only, container-provided, or observed at runtime.
- Dependency-level usage versus a path to a vulnerable function.
- Analysis gaps such as reflection, incomplete builds, unsupported formats, or missing vulnerability mappings.
- Exploitation prerequisites and deployment context, rather than CVSS alone.
- License metadata, project maintenance, and supply-chain indicators, without collapsing them into a claim that a library is trustworthy.

**Confidence: HIGH that the ingredients exist; no security scanner's accuracy was benchmarked here.**

## 8. Closest coding-agent and product precedents

### Endor Labs MCP and Agent Kit

This is the strongest broad commercial overlap found. The MCP tool list includes dependency vulnerability/risk checks, vulnerability details, resource retrieval, and scans. Its Agent Kit additionally documents dependency-reviewer, vulnerability-explainer, upgrade-investigator, and remediation workflows with evidence requirements. Those are already natural-language dependency investigation workflows, not just scanner output pasted into a chat. [33]

The inspected documentation does not demonstrate the complete requested history/intent/packaged-use explanation for a Maven dependency. It is nevertheless a competitor to examine before claiming a new “dependency intelligence agent” category.

### Sonatype Guide

Sonatype explicitly markets agentic dependency management and an MCP server connecting coding assistants to component and vulnerability intelligence. This substantially overlaps dependency selection, maintenance, and security questions. The fetched product page also marks an AI dependency-management agent section “Coming Soon,” so that roadmap item must not be presented as a shipped capability. No source-backed project-intent reconstruction was demonstrated in the material inspected. [36]

### Socket MCP

Socket's official MCP README explicitly includes Maven among package-security scoring ecosystems. It exposes package scoring and security metadata to assistants. That proves Maven-aware agent security intelligence exists; it does not prove local Maven graph reconstruction, class-use analysis, or Git-intent recovery. The README calls the project early development. [37]

### SafeDep vet

SafeDep documents Maven scanning, experimental source-usage enrichment, Java analysis, and MCP integration. But its MCP-specific documentation lists npm and PyPI as supported ecosystems, while the broader scanner documentation lists Maven. **Do not combine separate product support lists into a claim of Maven MCP support.** It is relevant adjacent tooling, with that interface-specific coverage gap explicitly unresolved. [38]

### Open-source Maven MCP servers

Bigsy's `maven-mcp-server` documents version lookup, existence checks, and release listing against Maven Central. Those functions are useful for preventing invented coordinates, but they do not answer which local code uses a dependency, what brought it into a build, or who introduced it. A project called “Maven MCP” is not necessarily a Maven project-analysis engine. [39]

OSV's experimental MCP server is another relevant open-source security interface, supported by its own changelog. [32]

### IDEs and public package intelligence

IntelliJ IDEA already documents Maven dependency analysis for resolved, unresolved, conflicting, and transitive dependencies, scope filtering, usage inspection, and navigation to POM declarations. This is an important baseline for developer expectations, although the inspected page does not establish an end-to-end historical rationale feature. [40]

Google's **deps.dev** provides Maven package/dependency graphs, version comparisons, advisory information, license metadata, and project information through a public API. It is a strong enrichment source. Its public package graph is not your private application's resolved graph, and its API expressly cautions that license metadata is not legal advice. [41]

**Confidence: HIGH for documented interfaces and their stated boundaries; MEDIUM for comparative product fit, since no authenticated vendor trial was run.**

## 9. What remains potentially distinctive?

The research supports a narrower opportunity than “an AI that explains dependencies.” Existing tools already cover much of that space.

The strongest unverified opportunity is **a traceable explanation connecting the build, delivered artifact, code usage, and decision history**. The answer would identify which facts come from Maven, which from bytecode or source, which from Git/PR evidence, and which from current security intelligence. Historical intent and reliable cross-source attribution appear less directly served by the inspected products than graph display or vulnerability lookup.

Three conclusions follow from the evidence:

1. **A new Maven resolver is not the missing capability.** Maven and its ecosystem already expose resolved graphs, effective-model origins, and reusable analysis APIs. [2][4][8][25]
2. **A generic MCP wrapper or CVE lookup is not a strong distinction.** Multiple official vendor and open-source interfaces already provide this. [32][33][35][37][39]
3. **An “unused” label is not enough.** The useful explanation distinguishes observed use, static evidence, framework mechanisms, and unknowns, and connects those to the actual shipped artifact and recorded history. [6][21][27][33][35]

For the later brainstorming session, the most informative comparison set is Apache's analyzer, DepClean, Gradle's `reason` and `because` features, Freshli, Endor Labs Agent Kit, and the OSV/CycloneDX/Dependency-Track stack. This is a research shortlist, not a selected implementation stack.

## Confidence assessment and verification

**HIGH:** Dependency-path tooling, effective-POM origins, class-usage APIs, the named security/agent interfaces, and the documented distinction between graph and delivered artifact. These are supported by primary documentation; the key Apache/DepClean interfaces were additionally checked against exact-tag source and published JARs.

**MEDIUM:** The claim that a source-cited, end-to-end Maven explanation combining usage, shipment, history, intent, and security remains underserved. Broad and targeted searches found substantial partial solutions, but public documentation cannot prove the absence of a private product or a newer undocumented feature.

**LOW without project evidence:** Recovering actual human intent, identifying the original decision-maker from line blame, reproducing an old graph from today's checkout alone, and declaring safe removal or non-exploitability from missing static references.

Adversarial checks rejected these tempting conclusions:

- “Only DepClean can provide class-to-library usage.” Apache's analyzer exposes it too. [25]
- “Provided dependencies cannot ship.” Spring Boot repackaging disproves this. [21]
- “No Java reachability in OSV.” Its changelog records JAR reachability separately from source-mode documentation. [32]
- “Maven support in a scanner means Maven support in its MCP interface.” SafeDep's interface-specific documentation does not support that inference. [38]
- “There are no existing dependency agents.” Endor, Snyk, Sonatype, Socket, and OSV disprove that broad claim. [32][33][35][36][37]

This was exhaustive web/documentation research, including multi-hop source and artifact verification, not a performance benchmark or a product trial. No project build, security scan, historical reconstruction, or implementation was performed. Documentation-only work is gate-exempt from code simplification, code review, and test-suite gates; report citations and scope were checked mechanically. Commercial pricing, accuracy statistics, and market-size estimates were intentionally not used.

## Questions to carry into brainstorming

These questions are research implications, not settled product requirements:

1. Is the primary object a resolved Maven project or a shipped artifact? Both are useful, but their evidence differs.
2. Is the differentiator technical usage, historical rationale, security interpretation, or a cited explanation connecting them?
3. Can every answer distinguish an observed fact, a documented intention, an inference, and unavailable evidence?
4. How much evidence must be local? Private repositories, source code, build settings, PR discussions, and dependency coordinates can be sensitive.
5. Which build contexts and dynamic frameworks must be represented before “unused” is meaningful?

## Sources

Unless otherwise specified, sources are living documentation or READMEs with no dependable publication date; accessed 2026-09-10. A documented feature is not equivalent to a locally tested product.

1. Maven dependency mechanism: https://maven.apache.org/guides/introduction/introduction-to-dependency-mechanism.html
2. Maven Resolver operation: https://maven.apache.org/resolver/how-resolver-works.html
3. Dependency-tree filtering example. Site publication: 2026-05-24. https://maven.apache.org/plugins/maven-dependency-plugin/examples/filtering-the-dependency-tree.html
4. Dependency-tree goal and formats: https://maven.apache.org/plugins/maven-dependency-plugin/tree-mojo.html and https://maven.apache.org/plugins/maven-dependency-plugin/examples/tree-mojo.html
5. Dependency analysis and default analyzer: https://maven.apache.org/plugins/maven-dependency-plugin/analyze-mojo.html and https://maven.apache.org/shared/maven-dependency-analyzer/
6. Analysis false positives and lifecycle usage: https://maven.apache.org/plugins/maven-dependency-plugin/examples/exclude-dependencies-from-dependency-analysis.html
7. Other dependency-plugin goals: https://maven.apache.org/plugins/maven-dependency-plugin/usage.html and https://maven.apache.org/plugins/maven-dependency-plugin/resolve-plugins-mojo.html
8. Effective POM and profile origins: https://maven.apache.org/plugins/maven-help-plugin/effective-pom-mojo.html and https://maven.apache.org/plugins/maven-help-plugin/usage.html
9. DepClean: https://github.com/ASSERT-KTH/depclean
10. DepClean study. Submitted 2020-01-21; journal publication 2021. https://arxiv.org/abs/2001.07808
11. DepClean Web: https://github.com/ASSERT-KTH/depclean-web
12. DepClean release evidence: https://github.com/ASSERT-KTH/depclean/tree/2.2.0 ; https://github.com/ASSERT-KTH/depclean/blob/2.2.0/depclean-maven-plugin/src/main/java/se/kth/depclean/util/json/NodeAdapter.java ; https://github.com/ASSERT-KTH/depclean/blob/2.2.0/depclean-core/src/main/java/se/kth/depclean/core/DepCleanManager.java ; https://repo.maven.apache.org/maven2/se/kth/castor/depclean-maven-plugin/2.2.0/depclean-maven-plugin-2.2.0.jar ; https://repo.maven.apache.org/maven2/se/kth/castor/depclean-core/2.2.0/depclean-core-2.2.0.jar
13. Official JDK dependency-analysis tutorial: https://dev.java/learn/jvm/tools/core/jdeps/
14. Ferstl dependency graph plugin: https://github.com/ferstl/depgraph-maven-plugin
15. DepTrim: https://github.com/ASSERT-KTH/deptrim
16. Moditect Deptective: https://github.com/moditect/deptective
17. SootUp: https://github.com/soot-oss/SootUp
18. License Maven Plugin: https://www.mojohaus.org/license-maven-plugin/
19. Gradle insight and recorded reasons: https://docs.gradle.org/current/userguide/viewing_debugging_dependencies.html and https://docs.gradle.org/current/userguide/declaring_dependencies_basics.html
20. Autonomous Apps dependency analysis and `reason`: https://github.com/autonomousapps/dependency-analysis-gradle-plugin
21. Spring Boot archive packaging: https://docs.spring.io/spring-boot/maven-plugin/packaging.html
22. Maven Shade relocation: https://maven.apache.org/plugins/maven-shade-plugin/examples/class-relocation.html
23. Maven Artifact Plugin and reproducibility: https://maven.apache.org/plugins/maven-artifact-plugin/ ; https://maven.apache.org/plugins/maven-artifact-plugin/describe-build-output-mojo.html ; https://maven.apache.org/plugins/maven-artifact-plugin/reproducible-central-mojo.html ; https://maven.apache.org/guides/mini/guide-reproducible-builds.html
24. Java ServiceLoader API: https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/ServiceLoader.html
25. Apache analyzer usage API and inspected release: https://maven.apache.org/shared/maven-dependency-analyzer/apidocs/org/apache/maven/shared/dependency/analyzer/ProjectDependencyAnalysis.html ; https://maven.apache.org/shared/maven-dependency-analyzer/apidocs/org/apache/maven/shared/dependency/analyzer/DependencyUsage.html ; https://github.com/apache/maven-dependency-analyzer/tree/maven-dependency-analyzer-1.17.2 ; https://repo.maven.apache.org/maven2/org/apache/maven/shared/maven-dependency-analyzer/1.17.2/maven-dependency-analyzer-1.17.2.jar
26. Inspected Maven Dependency Plugin CLI implementation: https://github.com/apache/maven-dependency-plugin/blob/maven-dependency-plugin-3.11.0/src/main/java/org/apache/maven/plugins/dependency/analyze/AbstractAnalyzeMojo.java ; https://repo.maven.apache.org/maven2/org/apache/maven/plugins/maven-dependency-plugin/3.11.0/maven-dependency-plugin-3.11.0.jar
27. Git blame and history search: https://git-scm.com/docs/git-blame and https://git-scm.com/docs/git-log
28. GitHub dependency review and Maven submission: https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/about-dependency-review ; https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/submit-dependencies-automatically
29. Historical dependency freshness and release dates: https://github.com/corgibytes/freshli and https://github.com/corgibytes/dependency-history-maven
30. OWASP Dependency-Check: https://jeremylong.github.io/DependencyCheck/ and https://jeremylong.github.io/DependencyCheck/general/internals.html
31. SBOM generation and monitoring: https://cyclonedx.github.io/cyclonedx-maven-plugin/ and https://docs.dependencytrack.org/
32. OSV support, source-analysis limits, and JAR/MCP changelog: https://google.github.io/osv-scanner/supported-languages-and-lockfiles/ ; https://google.github.io/osv-scanner/usage/scan-source ; https://github.com/google/osv-scanner/blob/main/CHANGELOG.md ; https://github.com/google/osv-scanner/pull/2113 ; https://github.com/google/osv-scanner/pull/2256
33. Endor Labs Java, reachability, MCP, and agent workflows: https://docs.endorlabs.com/scan/sca/java ; https://docs.endorlabs.com/scan/sca/reachability-analysis ; https://docs.endorlabs.com/setup-deployment/mcp ; https://docs.endorlabs.com/secure-ai-coding/agent-kit ; https://github.com/endorlabs/endor-labs-agent-kit
34. Eclipse Steady capabilities and knowledge-base disclaimer: https://github.com/eclipse-steady/steady
35. Snyk agent integration and reachability: https://snyk.io/articles/snyk-mcp-cheat-sheet/ ; https://docs.snyk.io/scan-fix-and-prevent/fix/prioritize-issues-for-fixing/reachability-analysis
36. Sonatype Guide and roadmap labeling: https://www.sonatype.com/products/sonatype-guide
37. Socket MCP: https://github.com/SocketDev/socket-mcp
38. SafeDep scanner, experimental Java usage analysis, and MCP-specific ecosystem list: https://github.com/safedep/vet ; https://docs.safedep.io/vet/guides/dependency-usage-identification ; https://docs.safedep.io/governance/vet/code-analysis ; https://github.com/safedep/vet/blob/main/docs/mcp.md
39. Maven version-lookup MCP server: https://github.com/Bigsy/maven-mcp-server
40. IntelliJ IDEA Maven dependency tools: https://www.jetbrains.com/help/idea/work-with-maven-dependencies.html
41. deps.dev documentation and API: https://docs.deps.dev/ and https://docs.deps.dev/api/v3/

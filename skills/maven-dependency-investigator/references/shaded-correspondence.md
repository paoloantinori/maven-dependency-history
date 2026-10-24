# Shaded archive correspondence

Use `collect --reference-jar` to compare an already selected packaged copy with explicitly supplied local reference JARs. This is an offline evidence comparison, not a bytecode decompiler or a Maven Shade execution. It does not identify metadata-free rewritten bytecode automatically.

## Workflow

Discover and select the packaged candidate first. Use the actual returned run directory, current revision and selection. A declaration-only selection has no packaged copy to compare; the shaded stage records `not_applicable` instead of searching arbitrary output for an assumed identity.

```text
python3 scripts/investigate.py collect \
  --run-dir <RUN_DIRECTORY> \
  --expect-revision <CURRENT_REVISION> \
  --reference-jar <LOCAL_REFERENCE_JAR>
```

Invoke the entry point from the skill directory, or use its absolute path. Repeat `--reference-jar` for additional references. The CLI accepts at most eight references and eight selected packaged copies. Explicit references may be outside the checkout. It never downloads reference artifacts or treats a filename as a verified coordinate or version.

The CLI recaptures both sides using the private file-backed archive pipeline. The selected copy's indexed nested-entry chain and digest must still match discovery. Changed copies remain unresolved; they are not silently reassociated. One 120-second deadline covers the shaded stage's archive work and comparisons, with the parent IO limitations described in the [archive reference](cli.md#archive-coverage-and-limits).

## Interpret the comparison

The matcher compares one selected container with the outer archive of each reference. It preserves duplicate entry multiplicity. It reserves exact path, digest and size matches before considering nonempty identical content under different paths. It does not pool classes from unrelated nested archives.

`complete-entry-content-correspondence` means that the observed reference entries correspond within the selected container. It is not whole-JAR byte identity: ZIP layout, compression and surplus entries can differ. A renamed path with identical bytes is only a correspondence candidate, not proof that a class was correctly relocated. Rewritten class bytes generally differ and are not recognized by byte equality.

Missing or conflicting metadata remains explicit. Coordinates from archive metadata are candidate identities, not an independently established component version. Partial retention, duplicate pairing ambiguity, incomplete inspection and stale artifacts limit the result. No match is not evidence that the dependency is absent elsewhere or safe to remove.

## Packaging and inclusion leads

For each selected module, the CLI captures its current POM and records namespace-aware Shade plugin configuration observations. Profile and execution placement, relocations and filters are raw recorded configuration. Literal indexed path pairs can support candidate configuration links; the collector does not evaluate interpolation, profile activation, plugin-version semantics or whether that configuration actually ran.

Already imported dependency trees and effective POMs can supply candidate inclusion and model links for the matching module and available realm context. These links do not authenticate which checkout produced the packaged archive. Missing or malformed module POMs leave packaging attribution unresolved without discarding otherwise useful archive comparisons.

Cite the comparison and its recorded source evidence, and preserve all required qualifications. Keep observed bytes, candidate component identity, configured relocation, resolved inclusion and authenticated build provenance separate. Recognition of rewritten bytecode without metadata is a declared boundary of this interface: it is not supported today and is not presented as completing a broader capability; a future capability task tracks it.

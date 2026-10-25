package mdi;

import java.io.BufferedOutputStream;
import java.io.IOException;
import java.net.URL;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Enumeration;
import java.util.jar.JarOutputStream;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;
import java.util.zip.ZipException;

import org.apache.maven.shared.dependency.analyzer.ClassesPatterns;
import org.apache.maven.shared.dependency.analyzer.DefaultClassAnalyzer;
import org.apache.maven.shared.dependency.analyzer.DependencyUsage;
import org.apache.maven.shared.dependency.analyzer.asm.ASMDependencyAnalyzer;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;

/**
 * Bounded TASK-21 slice: raw class-usage report for one root input (jar or
 * exploded directory) plus upstream dependency jars, built on Apache
 * maven-dependency-analyzer 1.17.2 ({@link DefaultClassAnalyzer} for class
 * indexes, {@link ASMDependencyAnalyzer} for usage edges). No own bytecode
 * parsing, no ASM traversal of our own, and analyzed classes are never
 * loaded or executed.
 *
 * <p>Contract and safety model:
 *
 * <ul>
 *   <li>Inputs are prevalidated staged files (regular {@code *.jar} files or
 *   plain directories) that are trusted as immutable for the duration of the
 *   run. The Python caller stages copies under a controlled root before
 *   invoking this helper; arbitrary paths are not accepted silently. The
 *   analyzer libraries read files without their own bounds, so every
 *   input-count, size, entry, and class budget below is enforced BEFORE any
 *   analyzer call, and the TOTAL class budget is enforced on the sum of
 *   preflight .class counts across all inputs (narrowable, never
 *   widenable, for tests via {@code -Dmdi.maxTotalClasses=N}). The one
 *   exception is MAX_REFS: it is
 *   an OUTPUT cap only, applied after {@code analyzeUsages} has already
 *   materialized one input's full usage set in analyzer memory; no
 *   pre-analysis reference bound exists without writing our own parser.
 *   Callers must therefore stage trusted immutable inputs and cap JVM heap,
 *   wall-clock time, and captured output size themselves.</li>
 *   <li>Symlinks are rejected: the input path itself, and any entry found
 *   while walking a directory input (walking never follows links).</li>
 *   <li>Multi-release jars ARE analyzed, through derived views: a baseline
 *   jar without {@code META-INF/versions/} plus one jar per declared version
 *   with the version prefix stripped, so every class keeps its real name.
 *   The input report lists the declared versions and the classes each
 *   version contributes ({@code multi_release_versions},
 *   {@code versioned_classes}); identical edges collapse to one. Directory
 *   inputs carrying any {@code META-INF/versions/} path, and jars whose
 *   version directories are malformed or whose derived views exceed twice
 *   the input size, are NOT analyzed: they are reported as an explicit
 *   unsupported issue with the input info preserved.</li>
 *   <li>Ownership is never resolved by first wins: every referenced target
 *   lists ALL input paths whose class index contains it. An empty candidate
 *   list means unowned or external, and the ref is still retained.</li>
 *   <li>No Maven coordinates are identified and no reachability is inferred;
 *   refs stay raw and keep their exact origin input (no merging of ids).</li>
 * </ul>
 *
 * <p>CLI: {@code mdi.UsageHelper --root PATH --scope production|test
 * [--dependency PATH]...}. Success: report JSON on stdout, exit 0.
 * Validation or usage failure: one message on stderr, exit 2, nothing on
 * stdout. Unexpected failure: exit 1.
 */
public final class UsageHelper {

    public static final String SCHEMA = "mdi.usage-helper/1";

    static final int MAX_INPUTS = 64;
    static final int MAX_ENTRIES_PER_INPUT = 32_768;
    static final int MAX_CLASSES_PER_INPUT = 8_192;
    static final int MAX_TOTAL_CLASSES = 65_536;
    static final int MAX_REFS = 262_144;
    static final long MAX_INPUT_BYTES = 64L * 1024 * 1024;

    private UsageHelper() {
    }

    private static final class ValidationException extends RuntimeException {
        ValidationException(String message) {
            super(message);
        }
    }

    private static final class Options {
        final String root;
        final String scope;
        final List<String> deps;

        Options(String root, String scope, List<String> deps) {
            this.root = root;
            this.scope = scope;
            this.deps = deps;
        }
    }

    private static final class Input {
        final Path canonical;
        final String role;
        final String kind;
        boolean analyzed = true;
        long sizeBytes;
        Set<String> classes = new TreeSet<>();
        boolean multiRelease;
        List<Integer> mrVersions = new ArrayList<>();
        List<Path> derivedJars = new ArrayList<>();
        List<Integer> derivedVersions = new ArrayList<>();
        Map<Integer, Set<String>> versionedClasses = new TreeMap<>();

        Input(Path canonical, String role, String kind) {
            this.canonical = canonical;
            this.role = role;
            this.kind = kind;
        }
    }

    private static final class Preflight {
        int entries;
        int classFiles;
        int versionedEntries;
        boolean manifestMultiRelease;
        long classBytes;
    }

    public static void main(String[] args) {
        try {
            System.out.println(report(args));
        } catch (ValidationException e) {
            System.err.println("usage-helper: " + e.getMessage());
            System.exit(2);
        } catch (IOException | RuntimeException e) {
            System.err.println("usage-helper: unexpected failure: " + e);
            System.exit(1);
        }
    }

    private static String report(String[] args) throws IOException {
        Options opts = parse(args);
        List<Input> inputs = new ArrayList<>();
        inputs.add(prepare(opts.root, opts.scope));
        for (String dep : opts.deps) {
            inputs.add(prepare(dep, "upstream"));
        }
        Set<Path> seen = new LinkedHashSet<>();
        for (Input in : inputs) {
            if (!seen.add(in.canonical)) {
                throw new ValidationException(
                        "duplicate input path supplied more than once: " + in.canonical);
            }
        }

        List<Map<String, Object>> issues = new ArrayList<>();
        int totalPreflightClasses = 0;
        int totalLimit = maxTotalClasses();
        for (Input in : inputs) {
            Preflight pf = in.kind.equals("jar") ? preflightJar(in) : preflightDirectory(in);
            totalPreflightClasses += pf.classFiles;
            if (totalPreflightClasses > totalLimit) {
                throw budget("total preflight class count", totalPreflightClasses, totalLimit);
            }
            if (pf.versionedEntries > 0 || pf.manifestMultiRelease) {
                in.multiRelease = true;
                boolean jar = in.kind.equals("jar");
                if (!jar) {
                    // Staged directory roots never legitimately carry versioned
                    // layouts; refusing stays honest for them.
                    in.analyzed = false;
                    issues.add(issue(
                            "unsupported-multi-release-directory",
                            "multi-release directory layout not analyzed: "
                                    + pf.versionedEntries + " META-INF/versions/"
                                    + " entries would be reported as misleadingly"
                                    + " prefixed class names",
                            in.canonical));
                } else if (pf.versionedEntries > 0) {
                    // A jar with versioned entries is analyzed through derived
                    // jars: the baseline view and one stripped view per version,
                    // so every class keeps its real name and each versioned
                    // variant is enumerated, never guessed.
                    try {
                        deriveVersionedViews(in);
                    } catch (ValidationException e) {
                        in.analyzed = false;
                        in.derivedJars.clear();
                        in.derivedVersions.clear();
                        in.mrVersions.clear();
                        issues.add(issue(
                                "unsupported-multi-release-jar",
                                "multi-release jar not analyzed: " + e.getMessage(),
                                in.canonical));
                    }
                }
                // A manifest-only Multi-Release declaration with zero versioned
                // entries needs no derived view: the baseline is the whole jar.
            }
        }

        List<Map<String, String>> refs = analyze(inputs);

        for (Input in : inputs) {
            for (Path derived : in.derivedJars) {
                try {
                    Files.deleteIfExists(derived);
                } catch (IOException ignored) {
                    // best effort only: surviving derived jars surface as
                    // staging leftovers through the pipeline's own accounting
                }
            }
        }

        Map<String, List<String>> owners = new TreeMap<>();
        for (Input in : inputs) {
            for (String cls : in.classes) {
                owners.computeIfAbsent(cls, k -> new ArrayList<>()).add(in.canonical.toString());
            }
        }
        Set<String> targets = new TreeSet<>();
        for (Map<String, String> ref : refs) {
            targets.add(ref.get("to_class"));
        }
        List<Map<String, Object>> candidates = new ArrayList<>();
        for (String target : targets) {
            Map<String, Object> cand = new LinkedHashMap<>();
            cand.put("to_class", target);
            cand.put("owner_candidates", owners.getOrDefault(target, new ArrayList<>()));
            candidates.add(cand);
        }

        List<Map<String, Object>> inputMaps = new ArrayList<>();
        for (Input in : inputs) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("path", in.canonical.toString());
            m.put("role", in.role);
            m.put("kind", in.kind);
            m.put("size_bytes", in.sizeBytes);
            m.put("class_count", in.classes.size());
            m.put("classes", new ArrayList<>(in.classes));
            m.put("analyzed", in.analyzed);
            if (in.multiRelease) {
                m.put("multi_release", true);
                m.put("multi_release_versions", new ArrayList<>(in.mrVersions));
                Map<String, List<String>> versioned = new LinkedHashMap<>();
                for (Map.Entry<Integer, Set<String>> e : in.versionedClasses.entrySet()) {
                    versioned.put(String.valueOf(e.getKey()), new ArrayList<>(e.getValue()));
                }
                m.put("versioned_classes", versioned);
            }
            inputMaps.add(m);
        }

        Map<String, Object> report = new LinkedHashMap<>();
        report.put("schema", SCHEMA);
        report.put("scope", opts.scope);
        report.put("root", inputs.get(0).canonical.toString());
        report.put("inputs", inputMaps);
        report.put("refs", refs);
        report.put("target_owner_candidates", candidates);
        report.put("issues", issues);
        return new GsonBuilder().setPrettyPrinting().disableHtmlEscaping().create()
                .toJson(report);
    }

    // -- argument parsing --------------------------------------------------

    private static Options parse(String[] args) {
        String root = null;
        String scope = null;
        List<String> deps = new ArrayList<>();
        for (int i = 0; i < args.length; i++) {
            String flag = args[i];
            String value = i + 1 < args.length ? args[i + 1] : null;
            switch (flag) {
                case "--root":
                    if (root != null) {
                        throw new ValidationException("--root given more than once");
                    }
                    root = valueOf(flag, value);
                    i++;
                    break;
                case "--scope":
                    if (scope != null) {
                        throw new ValidationException("--scope given more than once");
                    }
                    scope = valueOf(flag, value);
                    i++;
                    break;
                case "--dependency":
                    deps.add(valueOf(flag, value));
                    i++;
                    break;
                default:
                    throw new ValidationException("unknown argument: " + flag
                            + " (usage: --root PATH --scope production|test"
                            + " [--dependency PATH]...)");
            }
        }
        if (root == null) {
            throw new ValidationException("--root is required");
        }
        if (scope == null) {
            throw new ValidationException("--scope is required (production|test)");
        }
        if (!"production".equals(scope) && !"test".equals(scope)) {
            throw new ValidationException(
                    "invalid --scope value: " + scope + " (expected production|test)");
        }
        if (1 + deps.size() > MAX_INPUTS) {
            throw budget("input count", 1 + deps.size(), MAX_INPUTS);
        }
        return new Options(root, scope, deps);
    }

    private static String valueOf(String flag, String value) {
        if (value == null || value.isEmpty() || value.startsWith("--")) {
            throw new ValidationException(flag + " requires a value");
        }
        return value;
    }

    // -- validation and preflight -------------------------------------------

    private static Input prepare(String rawPath, String role) throws IOException {
        Path raw = Paths.get(rawPath);
        if (!Files.exists(raw)) {
            throw new ValidationException("no such input path: " + rawPath);
        }
        if (Files.isSymbolicLink(raw)) {
            throw new ValidationException("input path is a symlink: " + rawPath);
        }
        String name = raw.getFileName() == null ? "" : raw.getFileName().toString();
        String kind;
        if (name.isEmpty()) {
            throw new ValidationException(
                    "filesystem root cannot be an input: " + rawPath);
        }
        if (Files.isDirectory(raw)) {
            if (name.endsWith(".jar")) {
                throw new ValidationException(
                        "directory input must not carry a .jar name: " + rawPath);
            }
            kind = "directory";
        } else if (Files.isRegularFile(raw) && name.endsWith(".jar")) {
            kind = "jar";
        } else {
            throw new ValidationException(
                    "input must be a .jar regular file or a plain directory: " + rawPath);
        }
        return new Input(raw.toRealPath(), role, kind);
    }

    private static Preflight preflightJar(Input in) throws IOException {
        in.sizeBytes = Files.size(in.canonical);
        if (in.sizeBytes > MAX_INPUT_BYTES) {
            throw budget("input size in bytes", in.sizeBytes, MAX_INPUT_BYTES);
        }
        Preflight pf = new Preflight();
        try (JarFile jar = new JarFile(in.canonical.toFile(), false, JarFile.OPEN_READ)) {
            Enumeration<JarEntry> all = jar.entries();
            while (all.hasMoreElements()) {
                JarEntry entry = all.nextElement();
                if (++pf.entries > MAX_ENTRIES_PER_INPUT) {
                    throw budget("jar entry count", pf.entries, MAX_ENTRIES_PER_INPUT);
                }
                String name = entry.getName();
                if (name.startsWith("META-INF/versions/")) {
                    pf.versionedEntries++;
                }
                if (name.endsWith(".class")) {
                    pf.classFiles++;
                }
            }
            if (jar.isMultiRelease()) {
                pf.manifestMultiRelease = true;
            }
            if (pf.classFiles > MAX_CLASSES_PER_INPUT) {
                throw budget("class count", pf.classFiles, MAX_CLASSES_PER_INPUT);
            }
        } catch (ZipException e) {
            throw new ValidationException("not a valid jar file: " + in.canonical);
        }
        return pf;
    }

    private static Preflight preflightDirectory(final Input in) throws IOException {
        final Preflight pf = new Preflight();
        Files.walkFileTree(in.canonical, new SimpleFileVisitor<Path>() {
            @Override
            public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
                if (attrs.isSymbolicLink()) {
                    throw new ValidationException("symlink inside directory input: " + dir);
                }
                if (++pf.entries > MAX_ENTRIES_PER_INPUT) {
                    throw budget("directory entry count", pf.entries, MAX_ENTRIES_PER_INPUT);
                }
                if (isVersionedPath(in.canonical.relativize(dir))) {
                    pf.versionedEntries++;
                }
                return FileVisitResult.CONTINUE;
            }

            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) {
                if (attrs.isSymbolicLink()) {
                    throw new ValidationException("symlink inside directory input: " + file);
                }
                if (++pf.entries > MAX_ENTRIES_PER_INPUT) {
                    throw budget("directory entry count", pf.entries, MAX_ENTRIES_PER_INPUT);
                }
                if (isVersionedPath(in.canonical.relativize(file))) {
                    pf.versionedEntries++;
                }
                if (file.getFileName().toString().endsWith(".class")) {
                    pf.classFiles++;
                    pf.classBytes += attrs.size();
                    if (pf.classFiles > MAX_CLASSES_PER_INPUT) {
                        throw budget("class count", pf.classFiles, MAX_CLASSES_PER_INPUT);
                    }
                    if (pf.classBytes > MAX_INPUT_BYTES) {
                        throw budget("class bytes", pf.classBytes, MAX_INPUT_BYTES);
                    }
                }
                return FileVisitResult.CONTINUE;
            }
        });
        in.sizeBytes = pf.classBytes;
        return pf;
    }

    // -- analysis -------------------------------------------------------------

    private static List<Map<String, String>> analyze(List<Input> inputs) throws IOException {
        ClassesPatterns noPatterns = new ClassesPatterns();
        DefaultClassAnalyzer classIndexAnalyzer = new DefaultClassAnalyzer();
        ASMDependencyAnalyzer usageAnalyzer = new ASMDependencyAnalyzer();
        List<Map<String, String>> refs = new ArrayList<>();
        int totalClasses = 0;
        int totalLimit = maxTotalClasses();
        for (Input in : inputs) {
            if (!in.analyzed) {
                continue;
            }
            // A multi-release jar is analyzed through its derived views: the
            // baseline first, then one stripped view per declared version.
            // Every class keeps its real name; versioned variants are recorded
            // per version, and identical edges collapse into one.
            List<Path> analysisTargets = in.derivedJars.isEmpty()
                    ? java.util.Collections.singletonList(in.canonical)
                    : in.derivedJars;
            Set<DependencyUsage> edges = new TreeSet<>(
                    Comparator.comparing(DependencyUsage::getUsedBy)
                            .thenComparing(DependencyUsage::getDependencyClass));
            for (int t = 0; t < analysisTargets.size(); t++) {
                URL url = analysisTargets.get(t).toUri().toURL();
                int version = in.derivedJars.isEmpty() ? -1 : in.derivedVersions.get(t);
                Set<String> classes = new TreeSet<>(classIndexAnalyzer.analyze(url, noPatterns));
                if (classes.size() > MAX_CLASSES_PER_INPUT) {
                    throw budget("class count", classes.size(), MAX_CLASSES_PER_INPUT);
                }
                in.classes.addAll(classes);
                if (version >= 0) {
                    in.versionedClasses.computeIfAbsent(version, k -> new TreeSet<>())
                            .addAll(classes);
                }
                for (DependencyUsage usage : usageAnalyzer.analyzeUsages(url, noPatterns)) {
                    String from = usage.getUsedBy();
                    String to = usage.getDependencyClass();
                    if (from == null || to == null || from.isEmpty() || to.isEmpty()) {
                        continue;
                    }
                    edges.add(usage);
                }
            }
            totalClasses += in.classes.size();
            if (totalClasses > totalLimit) {
                throw budget("total class count (post-analysis defense)",
                        totalClasses, totalLimit);
            }
            for (DependencyUsage edge : edges) {
                if (refs.size() + 1 > MAX_REFS) {
                    throw budget("reference count", refs.size() + 1, MAX_REFS);
                }
                Map<String, String> ref = new LinkedHashMap<>();
                ref.put("from_class", edge.getUsedBy());
                ref.put("to_class", edge.getDependencyClass());
                ref.put("origin_input", in.canonical.toString());
                refs.add(ref);
            }
        }
        return refs;
    }

    // -- small helpers ----------------------------------------------------------

    /**
     * Build the derived analysis views of a multi-release jar: one baseline
     * jar without any {@code META-INF/versions/} entries, then one jar per
     * declared version with that prefix stripped, so class names are real.
     * The derived jars are sibling files of the staged input (never inside a
     * directory root the analyzer walks) and are best-effort deleted after
     * analysis; survivors surface as staging leftovers.
     */
    private static void deriveVersionedViews(Input in) throws IOException {
        Path parent = in.canonical.getParent();
        String base = in.canonical.getFileName().toString();
        Path baseline = parent.resolve(base + ".mdi-baseline.jar");
        Set<Integer> versions = new TreeSet<>();
        try (JarFile src = new JarFile(in.canonical.toFile(), false, JarFile.OPEN_READ)) {
            Enumeration<JarEntry> all = src.entries();
            while (all.hasMoreElements()) {
                String name = all.nextElement().getName();
                if (!name.startsWith("META-INF/versions/") || name.endsWith("/")) {
                    continue;
                }
                String rest = name.substring("META-INF/versions/".length());
                if (rest.isEmpty()) {
                    continue;
                }
                int slash = rest.indexOf('/');
                String segment = slash < 0 ? rest : rest.substring(0, slash);
                int version;
                try {
                    version = Integer.parseInt(segment);
                } catch (NumberFormatException e) {
                    throw new ValidationException(
                            "non-numeric version directory " + segment
                                    + " under META-INF/versions");
                }
                if (version < 6) {
                    throw new ValidationException(
                            "invalid version directory " + segment
                                    + " under META-INF/versions");
                }
                versions.add(version);
            }
            writeDerivedJar(src, baseline, -1);
            in.derivedJars.add(baseline);
            in.derivedVersions.add(-1);
            for (int version : versions) {
                Path view = parent.resolve(base + ".mdi-v" + version + ".jar");
                writeDerivedJar(src, view, version);
                in.derivedJars.add(view);
                in.derivedVersions.add(version);
                in.mrVersions.add(version);
            }
        }
        long derivedBytes = 0;
        for (Path p : in.derivedJars) {
            derivedBytes += Files.size(p);
        }
        if (derivedBytes > 2L * in.sizeBytes + 65_536L) {
            throw new ValidationException(
                    "derived view bytes (" + derivedBytes + ") exceed the bound of"
                            + " twice the input size; refusing to analyze");
        }
    }

    /** Copy the source jar entries into {@code target}: baseline (version -1)
     * keeps everything except versioned entries; a version view keeps only
     * that version's entries with the version prefix stripped. */
    private static void writeDerivedJar(JarFile src, Path target, int version)
            throws IOException {
        try (JarOutputStream out = new JarOutputStream(
                new BufferedOutputStream(Files.newOutputStream(target)))) {
            Enumeration<JarEntry> all = src.entries();
            while (all.hasMoreElements()) {
                JarEntry entry = all.nextElement();
                String name = entry.getName();
                if (version < 0) {
                    if (name.startsWith("META-INF/versions/")) {
                        continue;
                    }
                    copyEntry(src, entry, name, out);
                } else {
                    String prefix = "META-INF/versions/" + version + "/";
                    if (!name.startsWith(prefix)) {
                        continue;
                    }
                    String stripped = name.substring(prefix.length());
                    if (stripped.isEmpty() || stripped.startsWith("META-INF/")) {
                        continue;
                    }
                    copyEntry(src, entry, stripped, out);
                }
            }
        }
    }

    private static void copyEntry(JarFile src, JarEntry entry, String name,
                                  JarOutputStream out) throws IOException {
        JarEntry copy = new JarEntry(name);
        copy.setTime(entry.getTime());
        out.putNextEntry(copy);
        try (java.io.InputStream in = src.getInputStream(entry)) {
            byte[] buffer = new byte[32_768];
            int read;
            while ((read = in.read(buffer)) >= 0) {
                out.write(buffer, 0, read);
            }
        }
        out.closeEntry();
    }

    private static Map<String, Object> issue(String code, String message, Path input) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("code", code);
        m.put("message", message);
        m.put("input", input.toString());
        return m;
    }

    static boolean isVersionedPath(Path relative) {
        String name = relative.toString().replace(java.io.File.separatorChar, '/');
        return name.equals("META-INF/versions") || name.startsWith("META-INF/versions/");
    }

    /**
     * Total class budget, enforced on the preflight sum before any analyzer
     * call. Narrowable for tests via {@code -Dmdi.maxTotalClasses=N}; the
     * override can only narrow, never widen, so any non-integer, zero,
     * negative, above-maximum, or out-of-int-range value is a clean
     * validation failure, never a silent clamp or fallback.
     */
    static int maxTotalClasses() {
        String raw = System.getProperty("mdi.maxTotalClasses");
        if (raw == null) {
            return MAX_TOTAL_CLASSES;
        }
        int value = 0;
        boolean valid;
        try {
            value = Integer.parseInt(raw.trim());
            valid = value >= 1 && value <= MAX_TOTAL_CLASSES;
        } catch (NumberFormatException e) {
            valid = false;
        }
        if (!valid) {
            throw new ValidationException("invalid mdi.maxTotalClasses value: " + raw
                    + " (expected an integer between 1 and " + MAX_TOTAL_CLASSES + ")");
        }
        return value;
    }

    private static ValidationException budget(String what, long value, long limit) {
        return new ValidationException(
                what + " (" + value + ") exceeds budget " + limit
                        + "; refusing to invoke the unbounded analyzer");
    }
}

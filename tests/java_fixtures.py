"""Shared Java source fixtures for the usage test slices (TASK-21.8).

Shared fixtures used by the usage helper, runner and pipeline suites:
the A/B/C/Dup class sources, the multi-release Base/V sources, the sentinel
file name and the file digest helper. Each
suite still compiles its own fixture set into its own private temp dir
with its own class lifetime; this module carries source text and nothing
else. Importing it performs no filesystem access, launches no JVM and
resolves no dependencies (tests/java_support.py owns prerequisite
verification; nothing is ever downloaded).

A_SRC keeps doubled braces: every suite formats it with the absolute path
of its own sentinel file.
"""

import hashlib

SENTINEL_NAME = "A-static-initializer-ran.txt"

A_SRC = """package com.example;
public class A {{
    static {{
        try {{
            java.nio.file.Files.write(
                java.nio.file.Paths.get("{sentinel}"), new byte[] {{ 1 }});
        }} catch (Throwable t) {{
            // never executed: proves analysis never loads classes
        }}
    }}
    public String go() {{ return new B().label() + com.example.dup.Dup.NAME; }}
}}"""
B_SRC = """package com.example;
public class B {
    public String label() { return new C().name(); }
}"""
C_SRC = """package com.example;
public class C {
    public String name() { return "c"; }
}"""
DUP_SRC = """package com.example.dup;
public class Dup {
    public static final String NAME = "dup";
}"""
MR_BASE_SRC = """package com.example.mr;
public class Base {
    public String id() { return "base"; }
}"""
MR_V_SRC = """package com.example.mr;
public class V {
    public String id() { return "v17"; }
}"""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

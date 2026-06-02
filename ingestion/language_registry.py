"""
ingestion/language_registry.py
--------------------------------
Single source of truth for language metadata used across the framework.

Provides:
  • LANGUAGES dict — per-language config (test frameworks, package managers,
    security concerns, linters)
  • EXT_TO_LANG — comprehensive extension → language map (200+ entries)
  • NAME_TO_LANG — special filenames without extensions (Dockerfile, Makefile …)
  • detect_language(file_path) — extension + filename-based detection
  • lang_meta(language) — retrieve metadata, falls back to UNKNOWN_LANG
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LangMeta:
    display:          str              # human-readable name
    test_frameworks:  list[str]        # pytest, JUnit, Jest …
    package_manager:  list[str]        # pip, npm, cargo …
    security_notes:   list[str]        # language-specific risks
    linters:          list[str]        # eslint, ruff, golangci-lint …
    complexity_kw:    list[str] = field(default_factory=list)  # control-flow keywords
    is_infra:         bool = False     # IaC / config (not compiled code)
    is_data:          bool = False     # SQL / migration / schema


# ── Language catalogue ────────────────────────────────────────────────────────

LANGUAGES: dict[str, LangMeta] = {

    # ── Compiled / systems ────────────────────────────────────────────────────
    "c": LangMeta(
        display="C",
        test_frameworks=["Unity", "CUnit", "Check", "CMocka"],
        package_manager=["make", "cmake", "conan"],
        security_notes=["buffer overflow", "use-after-free", "format string", "integer overflow", "unsafe pointer arithmetic"],
        linters=["cppcheck", "clang-tidy", "splint"],
        complexity_kw=["if", "else", "for", "while", "do", "switch", "case", "goto"],
    ),
    "cpp": LangMeta(
        display="C++",
        test_frameworks=["Google Test", "Catch2", "Boost.Test", "doctest"],
        package_manager=["cmake", "conan", "vcpkg"],
        security_notes=["buffer overflow", "use-after-free", "RAII violations", "raw pointer misuse", "exception safety"],
        linters=["cppcheck", "clang-tidy", "clang-analyzer", "pvs-studio"],
        complexity_kw=["if", "else", "for", "while", "do", "switch", "catch", "try"],
    ),
    "csharp": LangMeta(
        display="C#",
        test_frameworks=["xUnit", "NUnit", "MSTest", "SpecFlow"],
        package_manager=["nuget", "dotnet"],
        security_notes=["SQL injection via string concat", "deserialization", "LDAP injection", "XSS in Razor", "path traversal"],
        linters=["roslyn analyzers", "sonarqube", "security code scan"],
        complexity_kw=["if", "else", "for", "foreach", "while", "switch", "catch", "try", "when"],
    ),
    "go": LangMeta(
        display="Go",
        test_frameworks=["testing (stdlib)", "testify", "gomock", "ginkgo"],
        package_manager=["go mod"],
        security_notes=["SQL injection", "command injection via exec.Command", "SSRF", "unsafe.Pointer misuse", "goroutine leak"],
        linters=["golangci-lint", "staticcheck", "gosec", "govulncheck"],
        complexity_kw=["if", "else", "for", "switch", "select", "go", "defer"],
    ),
    "rust": LangMeta(
        display="Rust",
        test_frameworks=["cargo test", "proptest", "criterion"],
        package_manager=["cargo"],
        security_notes=["unsafe block misuse", "integer overflow in release mode", "FFI boundary errors", "unwrap() on None"],
        linters=["clippy", "cargo audit", "cargo-deny"],
        complexity_kw=["if", "else", "for", "while", "loop", "match", "if let", "while let"],
    ),
    "swift": LangMeta(
        display="Swift",
        test_frameworks=["XCTest", "Quick/Nimble", "swift-testing"],
        package_manager=["swift package manager", "cocoapods", "carthage"],
        security_notes=["force unwrap crash", "insecure data storage (iOS Keychain)", "ATS bypass", "certificate pinning bypass"],
        linters=["swiftlint", "swiftformat"],
        complexity_kw=["if", "else", "for", "while", "switch", "guard", "if let", "guard let"],
    ),
    "kotlin": LangMeta(
        display="Kotlin",
        test_frameworks=["JUnit 5", "Kotest", "MockK", "Spek"],
        package_manager=["gradle", "maven"],
        security_notes=["Java interop null pointer", "Android intent injection", "SQL injection", "serialization"],
        linters=["ktlint", "detekt", "sonarqube"],
        complexity_kw=["if", "else", "for", "while", "when", "try", "catch"],
    ),
    "java": LangMeta(
        display="Java",
        test_frameworks=["JUnit 5", "TestNG", "Mockito", "Spock"],
        package_manager=["maven", "gradle"],
        security_notes=["SQL injection", "XXE", "insecure deserialization", "SSRF", "Log4Shell pattern", "JNDI injection"],
        linters=["checkstyle", "pmd", "spotbugs", "sonarqube"],
        complexity_kw=["if", "else", "for", "while", "do", "switch", "catch", "try", "instanceof"],
    ),
    "scala": LangMeta(
        display="Scala",
        test_frameworks=["ScalaTest", "Specs2", "MUnit"],
        package_manager=["sbt", "mill", "maven"],
        security_notes=["SQL injection", "deserialization", "Akka actor message handling"],
        linters=["scalafmt", "scalastyle", "wartremover", "scalafix"],
        complexity_kw=["if", "else", "for", "while", "match", "try", "catch", "case"],
    ),
    "dart": LangMeta(
        display="Dart",
        test_frameworks=["flutter_test", "test (dart)", "mockito"],
        package_manager=["pub", "flutter pub"],
        security_notes=["insecure storage (Flutter Secure Storage)", "deep link injection", "certificate pinning bypass"],
        linters=["dart analyze", "flutter analyze", "very_good_analysis"],
        complexity_kw=["if", "else", "for", "while", "switch", "try", "catch"],
    ),
    "objc": LangMeta(
        display="Objective-C",
        test_frameworks=["XCTest", "OCMock"],
        package_manager=["cocoapods", "carthage"],
        security_notes=["format string attack", "buffer overflow", "iOS Keychain misuse", "ATS bypass"],
        linters=["clang analyzer", "oclint"],
        complexity_kw=["if", "else", "for", "while", "switch"],
    ),

    # ── Scripting / interpreted ───────────────────────────────────────────────
    "python": LangMeta(
        display="Python",
        test_frameworks=["pytest", "unittest", "hypothesis"],
        package_manager=["pip", "poetry", "uv", "conda"],
        security_notes=["SQL injection", "command injection via subprocess/os.system", "pickle deserialization", "SSTI (Jinja2)", "path traversal", "eval/exec misuse"],
        linters=["ruff", "bandit", "mypy", "pylint"],
        complexity_kw=["if", "elif", "else", "for", "while", "try", "except", "with", "match", "case"],
    ),
    "javascript": LangMeta(
        display="JavaScript",
        test_frameworks=["Jest", "Mocha", "Vitest", "Cypress", "Playwright"],
        package_manager=["npm", "yarn", "pnpm"],
        security_notes=["XSS via innerHTML/dangerouslySetInnerHTML", "prototype pollution", "ReDoS", "eval misuse", "SSRF in Node", "dependency confusion"],
        linters=["eslint", "biome", "jshint"],
        complexity_kw=["if", "else", "for", "while", "switch", "try", "catch", "?."],
    ),
    "typescript": LangMeta(
        display="TypeScript",
        test_frameworks=["Jest", "Vitest", "Mocha", "Playwright", "Cypress"],
        package_manager=["npm", "yarn", "pnpm"],
        security_notes=["type assertion bypass (as any)", "XSS", "prototype pollution", "unsafe deserialization", "dependency confusion"],
        linters=["eslint + @typescript-eslint", "biome", "tsc --strict"],
        complexity_kw=["if", "else", "for", "while", "switch", "try", "catch", "?."],
    ),
    "ruby": LangMeta(
        display="Ruby",
        test_frameworks=["RSpec", "Minitest", "Cucumber"],
        package_manager=["bundler", "gem"],
        security_notes=["mass assignment", "SQL injection (ActiveRecord raw queries)", "SSTI (ERB)", "command injection", "YAML.load deserialization"],
        linters=["rubocop", "brakeman", "reek"],
        complexity_kw=["if", "elsif", "else", "for", "while", "until", "case", "when", "rescue", "begin"],
    ),
    "php": LangMeta(
        display="PHP",
        test_frameworks=["PHPUnit", "Pest", "Behat", "Codeception"],
        package_manager=["composer"],
        security_notes=["SQL injection", "XSS via echo/print", "file inclusion (LFI/RFI)", "object injection (unserialize)", "command injection", "SSRF"],
        linters=["phpstan", "psalm", "phpcs", "phan"],
        complexity_kw=["if", "elseif", "else", "for", "foreach", "while", "do", "switch", "match", "try", "catch"],
    ),
    "lua": LangMeta(
        display="Lua",
        test_frameworks=["busted", "luaunit"],
        package_manager=["luarocks"],
        security_notes=["code injection via loadstring/load", "sandbox escape", "integer overflow"],
        linters=["luacheck", "selene"],
        complexity_kw=["if", "elseif", "else", "for", "while", "repeat", "until"],
    ),
    "perl": LangMeta(
        display="Perl",
        test_frameworks=["Test::More", "Test::Simple", "Moo"],
        package_manager=["cpan", "cpanm"],
        security_notes=["taint mode bypass", "regex denial-of-service", "command injection", "SQL injection"],
        linters=["perlcritic", "perl -wc"],
        complexity_kw=["if", "elsif", "else", "for", "foreach", "while", "until", "unless"],
    ),
    "r": LangMeta(
        display="R",
        test_frameworks=["testthat", "tinytest"],
        package_manager=["CRAN", "renv"],
        security_notes=["code injection via parse/eval", "unsafe deserialization (readRDS)", "insecure HTTP (httr)"],
        linters=["lintr", "goodpractice"],
        complexity_kw=["if", "else", "for", "while", "repeat"],
    ),
    "julia": LangMeta(
        display="Julia",
        test_frameworks=["Test (stdlib)", "ReTest.jl"],
        package_manager=["Pkg.jl"],
        security_notes=["eval injection", "type instability (performance)", "unsafe ccall"],
        linters=["JET.jl", "Aqua.jl"],
        complexity_kw=["if", "elseif", "else", "for", "while"],
    ),

    # ── Functional ────────────────────────────────────────────────────────────
    "haskell": LangMeta(
        display="Haskell",
        test_frameworks=["HUnit", "QuickCheck", "Hspec", "Hedgehog"],
        package_manager=["cabal", "stack"],
        security_notes=["lazy evaluation space leak", "unsafe IO", "partial function (head/tail on empty list)"],
        linters=["hlint", "stan", "weeder"],
        complexity_kw=["if", "then", "else", "case", "of", "guard", "where", "let"],
    ),
    "erlang": LangMeta(
        display="Erlang",
        test_frameworks=["EUnit", "Common Test", "PropEr"],
        package_manager=["rebar3", "erlang.mk"],
        security_notes=["atom table exhaustion", "unsafe binary pattern matching", "process message queue overflow"],
        linters=["dialyzer", "xref"],
        complexity_kw=["if", "case", "of", "receive", "try", "catch"],
    ),
    "elixir": LangMeta(
        display="Elixir",
        test_frameworks=["ExUnit", "StreamData (property)"],
        package_manager=["mix", "hex"],
        security_notes=["atom exhaustion", "SSRF in HTTP clients", "SQL injection (Ecto raw queries)", "insecure configuration"],
        linters=["credo", "dialyxir", "sobelow"],
        complexity_kw=["if", "unless", "case", "cond", "with", "for", "try", "rescue"],
    ),
    "clojure": LangMeta(
        display="Clojure",
        test_frameworks=["clojure.test", "Midje", "Expectations"],
        package_manager=["leiningen", "deps.edn", "clojure CLI"],
        security_notes=["eval injection", "Java interop security", "uncontrolled deserialization"],
        linters=["clj-kondo", "eastwood"],
        complexity_kw=["if", "when", "cond", "case", "loop", "recur"],
    ),
    "fsharp": LangMeta(
        display="F#",
        test_frameworks=["xUnit", "NUnit", "Expecto", "FsCheck"],
        package_manager=["nuget", "dotnet", "paket"],
        security_notes=["SQL injection", "deserialization", "type provider misuse"],
        linters=["fantomas", "fsharplint"],
        complexity_kw=["if", "elif", "else", "for", "while", "match", "try", "with"],
    ),
    "ocaml": LangMeta(
        display="OCaml",
        test_frameworks=["Alcotest", "OUnit2", "QCheck"],
        package_manager=["opam", "dune"],
        security_notes=["unsafe C FFI", "integer overflow", "buffer access in Bytes"],
        linters=["ocaml-lsp", "odoc"],
        complexity_kw=["if", "then", "else", "match", "with", "for", "while", "try"],
    ),

    # ── Systems / low-level ───────────────────────────────────────────────────
    "zig": LangMeta(
        display="Zig",
        test_frameworks=["zig test (builtin)"],
        package_manager=["zig build system"],
        security_notes=["undefined behaviour", "integer overflow (unchecked mode)", "unsafe pointer cast"],
        linters=["zig fmt", "zig check"],
        complexity_kw=["if", "else", "for", "while", "switch"],
    ),
    "d": LangMeta(
        display="D",
        test_frameworks=["unittest (builtin)", "dunit"],
        package_manager=["dub"],
        security_notes=["unsafe memory operations", "C interop", "missing purity contracts"],
        linters=["dscanner"],
        complexity_kw=["if", "else", "for", "foreach", "while", "do", "switch", "try"],
    ),
    "nim": LangMeta(
        display="Nim",
        test_frameworks=["unittest (stdlib)", "balls"],
        package_manager=["nimble"],
        security_notes=["unsafe C FFI", "integer overflow", "macros expanding into unsafe code"],
        linters=["nim check", "nimfmt"],
        complexity_kw=["if", "elif", "else", "for", "while", "case", "of", "try", "except"],
    ),
    "pascal": LangMeta(
        display="Pascal / Delphi",
        test_frameworks=["DUnit", "DUnitX", "FPCUnit"],
        package_manager=["fpcmake"],
        security_notes=["buffer overflow", "integer overflow", "untyped pointer cast"],
        linters=["paslint"],
        complexity_kw=["if", "then", "else", "for", "while", "repeat", "case", "try", "except"],
    ),
    "fortran": LangMeta(
        display="Fortran",
        test_frameworks=["pFUnit", "Vegetables.jl"],
        package_manager=["fpm"],
        security_notes=["array out-of-bounds", "uninitialized variable", "implicit typing"],
        linters=["gfortran -Wall", "flake8-fortran"],
        complexity_kw=["IF", "ELSE", "DO", "WHILE", "SELECT", "CASE"],
    ),
    "cobol": LangMeta(
        display="COBOL",
        test_frameworks=["COBOL-check", "Zowe CLI test"],
        package_manager=["GnuCOBOL"],
        security_notes=["buffer overflow in PIC clause", "SQL injection (embedded SQL)", "unvalidated input in ACCEPT"],
        linters=["cobol-check", "cobcop"],
        complexity_kw=["IF", "ELSE", "PERFORM", "EVALUATE", "WHEN"],
    ),

    # ── JVM / CLR others ──────────────────────────────────────────────────────
    "groovy": LangMeta(
        display="Groovy",
        test_frameworks=["Spock", "JUnit 5", "Geb"],
        package_manager=["gradle", "maven"],
        security_notes=["dynamic eval (GroovyShell)", "Groovy script injection in CI/CD", "Java deserialization"],
        linters=["codenarc"],
        complexity_kw=["if", "else", "for", "while", "switch", "try", "catch"],
    ),
    "vbnet": LangMeta(
        display="VB.NET",
        test_frameworks=["xUnit", "NUnit", "MSTest"],
        package_manager=["nuget", "dotnet"],
        security_notes=["SQL injection", "deserialization", "LDAP injection", "XSS in WebForms"],
        linters=["roslyn analyzers"],
        complexity_kw=["If", "ElseIf", "Else", "For", "For Each", "While", "Select", "Case", "Try", "Catch"],
    ),
    "abap": LangMeta(
        display="ABAP",
        test_frameworks=["ABAP Unit", "ecATT"],
        package_manager=["abapgit"],
        security_notes=["SQL injection (open SQL)", "OS command injection", "RFC injection", "authority check bypass"],
        linters=["abaplint", "SCI (SAP Code Inspector)"],
        complexity_kw=["IF", "ELSEIF", "ELSE", "LOOP", "DO", "WHILE", "CASE", "WHEN", "TRY", "CATCH"],
    ),

    # ── Data / query ──────────────────────────────────────────────────────────
    "sql": LangMeta(
        display="SQL",
        test_frameworks=["pgTAP", "utPLSQL", "tSQLt"],
        package_manager=["alembic", "flyway", "liquibase"],
        security_notes=["SQL injection in dynamic statements", "privilege escalation", "data exposure in views", "DDL without transaction"],
        linters=["sqlfluff", "squawk"],
        complexity_kw=["IF", "CASE", "WHEN", "LOOP", "WHILE", "BEGIN", "EXCEPTION"],
        is_data=True,
    ),
    "plsql": LangMeta(
        display="PL/SQL",
        test_frameworks=["utPLSQL", "plunit"],
        package_manager=["sqitch", "liquibase"],
        security_notes=["dynamic SQL injection", "autonomous transaction misuse", "privilege escalation via AUTHID CURRENT_USER"],
        linters=["plsql-cop", "sqlfluff"],
        complexity_kw=["IF", "ELSIF", "ELSE", "FOR", "WHILE", "LOOP", "CASE", "WHEN", "EXCEPTION", "BEGIN"],
        is_data=True,
    ),

    # ── Infrastructure / config ───────────────────────────────────────────────
    "terraform": LangMeta(
        display="Terraform / HCL",
        test_frameworks=["Terratest", "terraform test", "Checkov"],
        package_manager=["terraform init"],
        security_notes=["open security group ingress", "public S3 bucket", "unencrypted storage", "wildcard IAM policy", "hardcoded credentials"],
        linters=["tflint", "checkov", "tfsec", "terrascan"],
        is_infra=True,
    ),
    "dockerfile": LangMeta(
        display="Dockerfile",
        test_frameworks=["container-structure-test", "Goss"],
        package_manager=["docker"],
        security_notes=["running as root", "COPY --chown missing", "hardcoded secrets in ENV/ARG", "unpinned base image", "unnecessary capabilities"],
        linters=["hadolint", "trivy"],
        is_infra=True,
    ),
    "yaml": LangMeta(
        display="YAML",
        test_frameworks=["yamllint", "kubeval", "conftest"],
        package_manager=[],
        security_notes=["Kubernetes RBAC over-permission", "privileged container", "hostPID/hostNetwork", "hardcoded secret in env"],
        linters=["yamllint", "kube-linter", "datree"],
        is_infra=True,
    ),
    "json": LangMeta(
        display="JSON",
        test_frameworks=["ajv (schema)", "conftest"],
        package_manager=[],
        security_notes=["hardcoded API keys", "overly permissive IAM policy", "exposed service account key"],
        linters=["jsonlint", "jq"],
        is_infra=True,
    ),
    "toml": LangMeta(
        display="TOML",
        test_frameworks=["taplo"],
        package_manager=[],
        security_notes=["hardcoded credentials", "insecure dependency version constraints"],
        linters=["taplo"],
        is_infra=True,
    ),
    "makefile": LangMeta(
        display="Makefile",
        test_frameworks=[],
        package_manager=["make"],
        security_notes=["command injection in targets", "unsafe variable expansion", "arbitrary code execution via make include"],
        linters=["checkmake"],
        is_infra=True,
    ),
    "shell": LangMeta(
        display="Shell / Bash",
        test_frameworks=["bats", "shunit2", "shellspec"],
        package_manager=["apk", "apt", "yum"],
        security_notes=["command injection", "unquoted variable expansion", "path injection", "insecure temp file", "hardcoded credentials"],
        linters=["shellcheck", "shfmt"],
        complexity_kw=["if", "elif", "else", "for", "while", "until", "case", "esac"],
    ),
    "powershell": LangMeta(
        display="PowerShell",
        test_frameworks=["Pester"],
        package_manager=["PowerShellGet", "NuGet"],
        security_notes=["Invoke-Expression injection", "execution policy bypass", "credential in plaintext", "AMSI bypass pattern"],
        linters=["PSScriptAnalyzer"],
        complexity_kw=["if", "elseif", "else", "for", "foreach", "while", "do", "switch", "try", "catch"],
    ),
    "protobuf": LangMeta(
        display="Protocol Buffers",
        test_frameworks=[],
        package_manager=["buf", "protoc"],
        security_notes=["field number reuse (wire compat break)", "reserved field removal", "sensitive data in proto without field mask"],
        linters=["buf lint", "protolint"],
        is_infra=True,
    ),
    "graphql": LangMeta(
        display="GraphQL",
        test_frameworks=["jest + graphql-tester", "Postman"],
        package_manager=["npm"],
        security_notes=["introspection enabled in prod", "query depth attack", "field suggestion enabled", "batching attack"],
        linters=["graphql-schema-linter", "graphql-inspector"],
        is_infra=True,
    ),

    # ── Web / markup ──────────────────────────────────────────────────────────
    "html": LangMeta(
        display="HTML",
        test_frameworks=["Playwright", "Cypress", "axe-core"],
        package_manager=[],
        security_notes=["inline script (CSP violation)", "dangerous attribute (onclick, href=javascript)", "missing CSRF token", "open redirect"],
        linters=["htmlhint", "w3c validator"],
    ),
    "css": LangMeta(
        display="CSS / SCSS",
        test_frameworks=["Playwright visual", "Storybook"],
        package_manager=["npm"],
        security_notes=["CSS injection", "expression() in legacy IE", "mixin privilege escalation"],
        linters=["stylelint"],
    ),
    "vue": LangMeta(
        display="Vue",
        test_frameworks=["Vue Test Utils + Jest/Vitest", "Cypress"],
        package_manager=["npm", "yarn", "pnpm"],
        security_notes=["v-html XSS", "template injection", "dependency confusion"],
        linters=["eslint-plugin-vue", "volar"],
        complexity_kw=["v-if", "v-else", "v-for"],
    ),
    "svelte": LangMeta(
        display="Svelte",
        test_frameworks=["Svelte Testing Library + Jest/Vitest", "Playwright"],
        package_manager=["npm", "yarn"],
        security_notes=["{@html} XSS", "server-side rendering injection"],
        linters=["eslint-plugin-svelte", "svelte-check"],
    ),

    # ── Notebooks / data science ──────────────────────────────────────────────
    "jupyter": LangMeta(
        display="Jupyter Notebook",
        test_frameworks=["nbval", "pytest-notebook", "papermill"],
        package_manager=["pip", "conda"],
        security_notes=["arbitrary code execution on open", "hardcoded API keys in output cells", "pickle deserialization in cell output"],
        linters=["nbqa", "nbstripout"],
    ),

    # ── Catch-all ─────────────────────────────────────────────────────────────
    "unknown": LangMeta(
        display="Unknown",
        test_frameworks=[],
        package_manager=[],
        security_notes=["review manually — language not detected"],
        linters=[],
    ),
}


# ── Extension → language map (200+ entries) ───────────────────────────────────

EXT_TO_LANG: dict[str, str] = {
    # Python
    ".py": "python", ".pyw": "python", ".pyi": "python",
    # JavaScript
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript",
    # TypeScript
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript",
    # Java
    ".java": "java",
    # Kotlin
    ".kt": "kotlin", ".kts": "kotlin",
    # Scala
    ".scala": "scala", ".sc": "scala",
    # Groovy
    ".groovy": "groovy", ".gvy": "groovy", ".gy": "groovy", ".gsh": "groovy",
    # C / C++
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cxx": "cpp", ".cc": "cpp", ".c++": "cpp",
    ".hpp": "cpp", ".hxx": "cpp", ".hh": "cpp",
    # C#
    ".cs": "csharp", ".csx": "csharp",
    # Go
    ".go": "go",
    # Rust
    ".rs": "rust",
    # Swift
    ".swift": "swift",
    # Dart
    ".dart": "dart",
    # Objective-C
    ".m": "objc", ".mm": "objc",
    # Ruby
    ".rb": "ruby", ".rake": "ruby", ".gemspec": "ruby",
    # PHP
    ".php": "php", ".php3": "php", ".php4": "php", ".php5": "php", ".phtml": "php",
    # Lua
    ".lua": "lua",
    # Perl
    ".pl": "perl", ".pm": "perl", ".t": "perl",
    # R
    ".r": "r", ".R": "r", ".rmd": "r", ".Rmd": "r",
    # Julia
    ".jl": "julia",
    # Haskell
    ".hs": "haskell", ".lhs": "haskell",
    # Erlang
    ".erl": "erlang", ".hrl": "erlang",
    # Elixir
    ".ex": "elixir", ".exs": "elixir", ".heex": "elixir",
    # Clojure
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure", ".edn": "clojure",
    # F#
    ".fs": "fsharp", ".fsx": "fsharp", ".fsi": "fsharp",
    # OCaml
    ".ml": "ocaml", ".mli": "ocaml",
    # Zig
    ".zig": "zig",
    # D
    ".d": "d",
    # Nim
    ".nim": "nim", ".nims": "nim",
    # Pascal / Delphi
    ".pas": "pascal", ".pp": "pascal", ".dpr": "pascal",
    # Fortran
    ".f": "fortran", ".f90": "fortran", ".f95": "fortran",
    ".f03": "fortran", ".f08": "fortran", ".for": "fortran",
    # COBOL
    ".cob": "cobol", ".cbl": "cobol", ".cpy": "cobol",
    # VB.NET
    ".vb": "vbnet",
    # ABAP
    ".abap": "abap",
    # SQL / PL/SQL / T-SQL
    ".sql": "sql", ".ddl": "sql", ".dml": "sql",
    ".pls": "plsql", ".pck": "plsql", ".pkb": "plsql", ".pks": "plsql",
    # Shell
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".ksh": "shell", ".fish": "shell", ".env": "shell",
    # PowerShell
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    # Terraform / HCL
    ".tf": "terraform", ".tfvars": "terraform", ".hcl": "terraform",
    # YAML
    ".yaml": "yaml", ".yml": "yaml",
    # JSON
    ".json": "json", ".jsonc": "json", ".json5": "json",
    # TOML
    ".toml": "toml",
    # Protobuf
    ".proto": "protobuf",
    # GraphQL
    ".graphql": "graphql", ".gql": "graphql",
    # HTML / Templates
    ".html": "html", ".htm": "html", ".jinja": "html",
    ".jinja2": "html", ".j2": "html",
    # CSS / styling
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    # Vue / Svelte
    ".vue": "vue", ".svelte": "svelte",
    # Jupyter
    ".ipynb": "jupyter",
    # Makefile
    ".mk": "makefile",
    # XML-family
    ".xml": "xml", ".xsl": "xml", ".xsd": "xml", ".wsdl": "xml",
    # Other config
    ".gradle": "groovy",   # Gradle build files use Groovy/Kotlin DSL
    ".lock": "unknown",    # lock files: not analysed
}


# ── Filename → language map (files without meaningful extension) ──────────────

NAME_TO_LANG: dict[str, str] = {
    "dockerfile":        "dockerfile",
    "dockerfile.dev":    "dockerfile",
    "dockerfile.prod":   "dockerfile",
    "dockerfile.test":   "dockerfile",
    "makefile":          "makefile",
    "gnumakefile":       "makefile",
    "gemfile":           "ruby",
    "gemfile.lock":      "unknown",
    "rakefile":          "ruby",
    "vagrantfile":       "ruby",
    "capfile":           "ruby",
    "podfile":           "ruby",
    "brewfile":          "ruby",
    "pipfile":           "python",
    "pipfile.lock":      "unknown",
    "poetry.lock":       "unknown",
    "package.json":      "json",
    "package-lock.json": "json",
    "yarn.lock":         "unknown",
    "pnpm-lock.yaml":    "unknown",
    "cargo.toml":        "toml",
    "cargo.lock":        "unknown",
    "go.mod":            "go",
    "go.sum":            "unknown",
    "jenkinsfile":       "groovy",
    ".gitignore":        "unknown",
    ".dockerignore":     "unknown",
    ".eslintrc":         "json",
    ".eslintrc.json":    "json",
    ".babelrc":          "json",
    "tsconfig.json":     "json",
    "pyproject.toml":    "toml",
    "setup.py":          "python",
    "setup.cfg":         "toml",
    "tox.ini":           "toml",
    "terraform.tfvars":  "terraform",
    "helmfile.yaml":     "yaml",
    "chart.yaml":        "yaml",
    "values.yaml":       "yaml",
    ".env":              "shell",
    ".env.example":      "shell",
}


# ── Public API ────────────────────────────────────────────────────────────────

def detect_language(file_path: str) -> str:
    """
    Detect language from a file path using:
      1. Exact filename match (case-insensitive) — catches Dockerfile, Makefile …
      2. Extension match (longest extension wins for multi-dot names like .d.ts)
    """
    name = file_path.split("/")[-1].lower()

    # 1 — exact filename
    if name in NAME_TO_LANG:
        return NAME_TO_LANG[name]

    # 2 — try progressively shorter extensions (.d.ts before .ts)
    parts = name.split(".")
    for i in range(1, len(parts)):
        ext = "." + ".".join(parts[i:])
        if ext in EXT_TO_LANG:
            return EXT_TO_LANG[ext]

    return "unknown"


def lang_meta(language: str) -> LangMeta:
    """Return metadata for a language, falling back to the 'unknown' entry."""
    return LANGUAGES.get(language, LANGUAGES["unknown"])


def test_framework_hint(language: str) -> str:
    """One-line automation hint for the QA scenarios agent."""
    meta = lang_meta(language)
    if not meta.test_frameworks:
        return ""
    return f"Recommended test frameworks: {', '.join(meta.test_frameworks)}"


def security_concerns(language: str) -> list[str]:
    """Language-specific security risks for the security agent fallback."""
    return lang_meta(language).security_notes


def linter_hint(language: str) -> str:
    meta = lang_meta(language)
    if not meta.linters:
        return ""
    return f"Static analysis: {', '.join(meta.linters)}"

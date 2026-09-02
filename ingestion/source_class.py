"""
ingestion/source_class.py
-------------------------
One shared answer to "is this changed file real, first-party source code that
a code-quality reviewer should nitpick?" — used by the maintainability agent,
the test-coverage agent, the remediation fallback, and the "similar past PRs"
fingerprint so they all agree.

`False` for:
  - test files (FooTest.java, test_x.py, src/test/... — ingestion.test_detect)
  - dependency / build manifests (pom.xml, build.gradle, package.json, *.lock …)
  - markup / config / data / IaC (xml, yaml, json, properties, sql, Dockerfile …)
  - anything the language registry does not recognise as source (logs, .md, .txt)

The reasoning about the orphan ``xml`` language id (mapped by EXT_TO_LANG but
with no LangMeta entry, so ``lang_meta()`` silently degrades it) mirrors
``agents/qa_scenarios_agent._is_scenario_worthy``.
"""
from __future__ import annotations

from ingestion.language_registry import detect_language, lang_meta, LANGUAGES
from ingestion.test_detect import is_test_file

# Dependency / build manifests — matched by basename (case-insensitive).
_MANIFEST_NAMES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "package.json", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "go.mod", "go.sum", "cargo.toml", "cargo.lock",
    "gemfile", "gemfile.lock", "requirements.txt", "requirements-dev.txt",
    "pipfile", "pipfile.lock", "poetry.lock", "composer.json", "composer.lock",
}

# Extra basename suffixes that are always manifests / lockfiles.
_MANIFEST_SUFFIXES = (".csproj", ".vbproj", ".lock")


def _is_manifest(path: str) -> bool:
    name = (path or "").replace("\\", "/").split("/")[-1].lower()
    if name in _MANIFEST_NAMES:
        return True
    if name.startswith("requirements") and name.endswith(".txt"):
        return True
    return name.endswith(_MANIFEST_SUFFIXES)


def is_reviewable_code(path: str) -> bool:
    """True only for real, first-party source a code-quality check should scan."""
    if not path:
        return False
    if is_test_file(path):
        return False
    if _is_manifest(path):
        return False
    lang = detect_language(path)
    if lang == "unknown" or lang not in LANGUAGES:
        return False
    meta = lang_meta(lang)
    if meta.is_infra or meta.is_data:
        return False
    return True

"""
ingestion/test_detect.py
-------------------------
Robust, language-aware detection of unit/integration test files.

Replaces fragile substring checks (which matched "latest_config.py",
"attestation.java", "contestants.js"…) with anchored, convention-based patterns
per ecosystem:

  Python  : tests/ dir, test_*.py, *_test.py, conftest.py
  Java/Kt : src/test/ dir, *Test.java, *Tests.java, *IT.java, *Spec.(groovy|scala)
  JS/TS   : __tests__/ dir, *.test.(js|ts|jsx|tsx), *.spec.(…)
  Go      : *_test.go
  Ruby    : *_spec.rb, *_test.rb
  C#      : *Test(s).cs
  Generic : a /test/ /tests/ /spec/ /specs/ path segment
"""
from __future__ import annotations
import re

_PATTERNS = [
    r"(^|/)tests?/",                                   # /test/ or /tests/ dir
    r"(^|/)specs?/",                                   # /spec/ or /specs/ dir
    r"(^|/)__tests__/",                                # JS __tests__/
    r"(^|/)test_[A-Za-z0-9][\w]*\.py$",                # python test_x.py
    r"_test\.py$",                                     # python x_test.py
    r"(^|/)conftest\.py$",                             # pytest fixtures
    r"_test\.go$",                                     # go
    r"_(spec|test)\.rb$",                              # ruby
    r"[A-Z][A-Za-z0-9]*Tests?\.(java|kt|cs)$",         # FooTest.java / FooTests.cs
    r"[A-Z][A-Za-z0-9]*IT\.java$",                     # integration test
    r"[A-Z][A-Za-z0-9]*Spec\.(groovy|scala)$",         # spock/scalatest
    r"\.(test|spec)\.(jsx?|tsx?|mjs|cjs)$",            # jest/vitest/jasmine
]
_RE = re.compile("|".join(_PATTERNS))


def is_test_file(path: str) -> bool:
    """True if the path is (by convention) a test file."""
    return bool(_RE.search((path or "").replace("\\", "/")))

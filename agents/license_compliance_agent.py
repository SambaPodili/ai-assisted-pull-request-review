"""
agents/license_compliance_agent.py
------------------------------------
License compliance analysis — STATIC ONLY (zero LLM tokens).

Scans changed dependency manifests for newly added packages and flags
those with copyleft, weak-copyleft, or unknown licenses.

Manifest files detected:
  package.json, requirements.txt, pom.xml, go.mod, Cargo.toml, Gemfile

Risk levels:
  CRITICAL  — AGPL / GPL (viral; incompatible with proprietary banking code)
  MEDIUM    — LGPL / MPL / EPL (weak copyleft; allowed with care)
  LOW       — Package not in known-safe list (review required)

Known-safe list (MIT / Apache / BSD / ISC assumed safe; no finding):
  react, lodash, express, flask, django, numpy, pandas, spring, junit,
  pytest, requests, axios, typescript, eslint, jest, and Go stdlib packages.

Banking context: IP and legal exposure; software composition analysis (SCA)
is a SOC-2 and MAS TRM expectation.
"""
from __future__ import annotations

import re
import time
import logging

from core.models import (
    AgentName, AnalysisRequest, AgentResultBase,
    LicenseFinding, LicenseComplianceResult,
)
from agents.base_agent import BaseAgent

log = logging.getLogger(__name__)


# ── Known license keyword maps ────────────────────────────────────────────────

# Order matters: check most specific first
_COPYLEFT_KEYWORDS = ["agpl", "affero"]          # viral — CRITICAL
_GPL_KEYWORDS      = ["gpl"]                      # but NOT lgpl — CRITICAL
_WEAK_COPYLEFT     = ["lgpl", "mpl", "epl"]       # MEDIUM

# Packages definitely safe (MIT/Apache/BSD/ISC)
_SAFE_PACKAGES: frozenset[str] = frozenset({
    "react", "react-dom", "lodash", "underscore", "express", "koa", "fastify",
    "flask", "django", "fastapi", "starlette", "uvicorn", "gunicorn",
    "numpy", "pandas", "scipy", "matplotlib", "scikit-learn", "tensorflow",
    "spring", "spring-boot", "spring-core", "spring-context",
    "junit", "junit-jupiter", "mockito", "hamcrest",
    "pytest", "pytest-cov", "coverage", "unittest2",
    "requests", "httpx", "urllib3", "certifi", "charset-normalizer",
    "axios", "node-fetch", "cross-fetch", "got",
    "typescript", "eslint", "prettier", "babel",
    "jest", "mocha", "chai", "sinon", "jasmine",
    # Go stdlib is handled separately via prefix check
})

# Go standard library packages start with these prefixes
_GO_STDLIB_PREFIXES = (
    "fmt", "os", "io", "net", "http", "strings", "strconv",
    "math", "sort", "sync", "time", "context", "errors",
    "encoding", "crypto", "log", "bufio", "bytes", "path",
    "reflect", "runtime", "testing", "unicode", "regexp",
)

# ── Manifest file patterns ────────────────────────────────────────────────────

_MANIFEST_NAMES = frozenset({
    "package.json", "requirements.txt", "pom.xml",
    "go.mod", "cargo.toml", "gemfile",
})


def _is_manifest(file_path: str) -> bool:
    return file_path.lower().split("/")[-1] in _MANIFEST_NAMES


# ── Package extraction per manifest type ──────────────────────────────────────

# package.json: `"package": "version"` in dependencies block
_PKG_JSON_DEP = re.compile(r'"([a-zA-Z0-9@/_\-\.]+)"\s*:\s*"[^"]+"')

# requirements.txt: package[extras]>=version  or  package==version
_REQUIREMENTS  = re.compile(r'^([A-Za-z0-9_\-\.]+)\s*(?:[>=<!~\[]|$)')

# pom.xml: <artifactId>name</artifactId>
_POM_ARTIFACT  = re.compile(r'<artifactId>\s*([^<]+)\s*</artifactId>')

# go.mod: require package vX.Y.Z  or  package vX.Y.Z  (inside require block)
_GO_MOD        = re.compile(r'^\s*([a-zA-Z0-9\.\-_/]+)\s+v[\d]')

# Cargo.toml: name = "version"  or  name = { version = "..." }
_CARGO         = re.compile(r'^([a-zA-Z0-9_\-]+)\s*=\s*(?:"[^"]+"|{)')

# Gemfile: gem 'name'  or  gem "name"
_GEMFILE       = re.compile(r"gem\s+['\"]([A-Za-z0-9_\-]+)['\"]")


def _extract_package_name(line_content: str, file_path: str) -> str | None:
    """Extract the package name from a manifest line."""
    fp = file_path.lower().split("/")[-1]
    if fp == "package.json":
        m = _PKG_JSON_DEP.search(line_content)
        return m.group(1) if m else None
    if fp == "requirements.txt":
        m = _REQUIREMENTS.match(line_content.strip())
        return m.group(1).lower() if m else None
    if fp == "pom.xml":
        m = _POM_ARTIFACT.search(line_content)
        return m.group(1).strip().lower() if m else None
    if fp == "go.mod":
        m = _GO_MOD.match(line_content)
        return m.group(1) if m else None
    if fp == "cargo.toml":
        m = _CARGO.match(line_content.strip())
        return m.group(1).lower() if m else None
    if fp == "gemfile":
        m = _GEMFILE.search(line_content)
        return m.group(1).lower() if m else None
    return None


def _is_go_stdlib(pkg: str) -> bool:
    top = pkg.split("/")[0].split(".")[0]
    return top in _GO_STDLIB_PREFIXES


def _classify_package(pkg: str) -> tuple[str, str]:
    """Return (risk_level, detected_license) for a package name."""
    name = pkg.lower()

    # Check copyleft keywords in package name (heuristic — real SCA uses SPDX)
    for kw in _COPYLEFT_KEYWORDS:
        if kw in name:
            return "critical", "AGPL"

    for kw in _GPL_KEYWORDS:
        if kw in name and "lgpl" not in name:
            return "critical", "GPL"

    for kw in _WEAK_COPYLEFT:
        if kw in name:
            return "medium", kw.upper()

    if name in _SAFE_PACKAGES:
        return "safe", "MIT/Apache/BSD"

    if _is_go_stdlib(pkg):
        return "safe", "Go stdlib"

    return "low", "unknown"


# ── Main scanner ──────────────────────────────────────────────────────────────

def _scan(request: AnalysisRequest) -> LicenseComplianceResult:
    findings:  list[LicenseFinding] = []
    packages_reviewed = 0

    # Collect all manifest file paths seen in diff for conflict detection
    manifest_files_seen: set[str] = set()
    has_permissive = False   # MIT/Apache seen
    has_copyleft   = False   # GPL/AGPL seen

    for hunk in request.hunks:
        if not _is_manifest(hunk.file_path):
            continue

        manifest_files_seen.add(hunk.file_path)
        lines = hunk.content.splitlines()

        for line in lines:
            if not line.startswith("+") or line.startswith("+++"):
                continue
            content = line[1:].strip()
            if not content:
                continue

            pkg = _extract_package_name(content, hunk.file_path)
            if not pkg:
                continue

            packages_reviewed += 1
            risk, license_name = _classify_package(pkg)

            if risk == "safe":
                has_permissive = True
                continue

            if risk == "critical":
                has_copyleft = True
                findings.append(LicenseFinding(
                    package=pkg,
                    detected_license=license_name,
                    risk_level="critical",
                    description=(
                        f"Package `{pkg}` appears to use a {license_name} license, "
                        "which is viral/copyleft and incompatible with proprietary banking software."
                    ),
                    file_path=hunk.file_path,
                ))
            elif risk == "medium":
                findings.append(LicenseFinding(
                    package=pkg,
                    detected_license=license_name,
                    risk_level="medium",
                    description=(
                        f"Package `{pkg}` uses {license_name} (weak copyleft). "
                        "Allowed with care — verify LGPL/MPL/EPL obligations are met."
                    ),
                    file_path=hunk.file_path,
                ))
            else:  # low / unknown
                findings.append(LicenseFinding(
                    package=pkg,
                    detected_license="unknown",
                    risk_level="low",
                    description=(
                        f"Package `{pkg}` has an unrecognised license. "
                        "Manual review required before merging."
                    ),
                    file_path=hunk.file_path,
                ))

    # ── Conflict detection ────────────────────────────────────────────────────
    # If GPL added and permissive project — flag conflict
    has_conflict = has_copyleft and has_permissive

    # ── Overall risk ──────────────────────────────────────────────────────────
    if any(f.risk_level == "critical" for f in findings):
        overall = "critical"
    elif any(f.risk_level == "medium" for f in findings):
        overall = "medium"
    elif findings:
        overall = "low"
    else:
        overall = "low"

    summary = (
        f"{len(findings)} license finding(s) across {packages_reviewed} package(s) reviewed. "
        f"Overall risk: {overall}."
        if findings else
        f"No license compliance issues detected ({packages_reviewed} package(s) reviewed)."
    )

    return LicenseComplianceResult(
        findings=findings,
        has_copyleft=has_copyleft,
        has_license_conflict=has_conflict,
        packages_reviewed=packages_reviewed,
        overall_risk=overall,
        summary=summary,
    )


# ── Agent ─────────────────────────────────────────────────────────────────────

class LicenseComplianceAgent(BaseAgent[LicenseComplianceResult]):

    agent_name   = AgentName.LICENSE_COMPLIANCE
    output_model = LicenseComplianceResult

    # Static-only agent: system_prompt is never sent, but BaseAgent requires the
    # attribute to be defined on the class.
    system_prompt = ""

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> LicenseComplianceResult:
        """Static-only scan — no LLM invoked."""
        from core.progress import get_progress_store
        progress = get_progress_store().get_or_create(request.request_id)
        agent_key = self.agent_name.value
        progress.agent_started(agent_key)
        t0     = time.monotonic()
        result = _scan(request)
        duration = round(time.monotonic() - t0, 2)
        result.duration_s    = duration
        result.fallback_used = False
        progress.agent_done(agent_key, 0, duration, "static", False)
        self.log_done(request, result, mode="static", note=f"{len(result.findings)} finding(s)")
        return result

    def build_user_prompt(self, request: AnalysisRequest, context: dict) -> str:
        # Never called (run() is overridden), but required by ABC.
        return ""

    def fallback_result(self, request: AnalysisRequest) -> LicenseComplianceResult:
        return LicenseComplianceResult(
            summary="License compliance scan unavailable.",
            fallback_used=True,
        )

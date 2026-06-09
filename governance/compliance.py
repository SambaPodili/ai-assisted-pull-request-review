"""
governance/compliance.py
-------------------------
Maps the analysis findings to industry compliance standards and produces a
pass/fail compliance report:

  • OWASP Top 10 (2021)
  • PCI-DSS 4.0 (relevant requirements)
  • CWE Top 25 (most dangerous weaknesses)

Deterministic — derived from the findings already produced by the agents
(security CWEs, taint flows, secrets, dependency CVEs, IaC, data-privacy,
observability). An item FAILs when there is evidence against it; otherwise it
PASSes (checked, nothing found). Returns a plain dict for easy JSON/UI/export.
"""
from __future__ import annotations
import re


# ── CWE → OWASP Top-10 (2021) mapping ──────────────────────────────────────────
_OWASP = [
    ("A01", "Broken Access Control",            {22, 23, 35, 59, 200, 201, 219, 264, 275, 276, 284, 285, 352, 359, 377, 402, 425, 441, 497, 538, 540, 548, 552, 566, 601, 639, 651, 668, 706, 862, 863, 913, 922, 1275}),
    ("A02", "Cryptographic Failures",           {261, 296, 310, 319, 321, 322, 323, 324, 325, 326, 327, 328, 329, 330, 331, 335, 336, 337, 338, 340, 347, 523, 720, 757, 759, 760, 780, 818, 916}),
    ("A03", "Injection",                        {20, 74, 75, 77, 78, 79, 80, 83, 87, 88, 89, 90, 91, 93, 94, 95, 96, 97, 98, 99, 100, 113, 116, 138, 184, 470, 471, 564, 610, 643, 644, 652, 917}),
    ("A04", "Insecure Design",                  {73, 183, 209, 213, 235, 256, 257, 266, 269, 280, 311, 312, 313, 316, 419, 430, 434, 444, 451, 472, 501, 522, 525, 539, 579, 598, 602, 642, 646, 650, 653, 656, 657, 799, 807, 840, 841, 927, 1021, 1173}),
    ("A05", "Security Misconfiguration",        {2, 11, 13, 15, 16, 260, 315, 520, 526, 537, 541, 547, 611, 614, 756, 776, 942, 1004, 1032, 1174}),
    ("A06", "Vulnerable & Outdated Components",  {937, 1035, 1104}),
    ("A07", "Identification & Auth Failures",   {255, 259, 287, 288, 290, 294, 295, 297, 300, 302, 304, 306, 307, 346, 384, 521, 613, 620, 640, 798, 940, 1216}),
    ("A08", "Software & Data Integrity Failures", {345, 353, 426, 494, 502, 565, 784, 829, 830, 915}),
    ("A09", "Security Logging & Monitoring Failures", {117, 223, 532, 778}),
    ("A10", "Server-Side Request Forgery (SSRF)", {918}),
]

# ── CWE Top 25 (2023) ──────────────────────────────────────────────────────────
_CWE_TOP25 = {
    787: "Out-of-bounds Write", 79: "Cross-site Scripting (XSS)", 89: "SQL Injection",
    416: "Use After Free", 78: "OS Command Injection", 20: "Improper Input Validation",
    125: "Out-of-bounds Read", 22: "Path Traversal", 352: "CSRF", 434: "Unrestricted File Upload",
    862: "Missing Authorization", 476: "NULL Pointer Dereference", 287: "Improper Authentication",
    190: "Integer Overflow", 502: "Deserialization of Untrusted Data", 77: "Command Injection",
    119: "Improper Memory Buffer Ops", 798: "Hard-coded Credentials", 918: "SSRF",
    306: "Missing Authentication", 362: "Race Condition", 269: "Improper Privilege Mgmt",
    94: "Code Injection", 863: "Incorrect Authorization", 276: "Incorrect Default Permissions",
}


def _cwe_num(cwe: str) -> int | None:
    m = re.search(r"(\d+)", cwe or "")
    return int(m.group(1)) if m else None


def _security_cwes(report) -> list[int]:
    out = []
    sec = report.security
    for f in (getattr(sec, "findings", None) or []):
        if getattr(f, "unverified", False):
            continue
        n = _cwe_num(getattr(f, "cwe_id", "") or getattr(f, "cwe", ""))
        if n:
            out.append(n)
    return out


def assess(report) -> dict:
    cwes = set(_security_cwes(report))
    taint = report.taint_analysis
    iac = report.iac_analysis
    dep = report.dependency
    dp = report.data_privacy
    obs = report.observability
    sec = report.security
    se = report.secrets_entropy

    secrets = bool(getattr(sec, "secrets_detected", False)) or bool(
        getattr(se, "findings", None))
    cve_hits = list(getattr(dep, "cve_hits", None) or [])
    has_injection = bool(getattr(taint, "has_injection", False)) or bool(cwes & {89, 77, 78, 94, 79, 90, 91})
    has_ssrf = bool(getattr(taint, "has_ssrf", False)) or (918 in cwes)
    pii = int(getattr(dp, "unencrypted_pii_count", 0) or 0)
    iac_findings = list(getattr(iac, "findings", None) or [])
    logs_removed = bool(getattr(obs, "findings", None))   # observability flags removed logs

    # ── Evidence (file:line + label) so reviewers can see the backing code ──
    def _ev(file, line, label):
        return {"file": file or "", "line": str(line or ""), "label": (label or "").strip(" :")}

    sec_ev = []   # (cwe_num, evidence)
    for f in (getattr(sec, "findings", None) or []):
        if getattr(f, "unverified", False):
            continue
        n = _cwe_num(getattr(f, "cwe_id", "") or getattr(f, "cwe", ""))
        sec_ev.append((n, _ev(getattr(f, "file_path", ""), getattr(f, "line_range", ""),
                              f"{getattr(f,'cwe_id','')} {getattr(f,'description','')}")))
    taint_ev = [_ev(p.sink.file_path, p.sink.line, p.description or f"{p.source.source} → {p.sink.sink}")
                for p in (getattr(taint, "taint_paths", None) or []) if getattr(p, "sink", None)]
    obs_ev   = [_ev(f.file_path, f.line, f"{f.kind}: {f.description}") for f in (getattr(obs, "findings", None) or [])]
    priv_ev  = [_ev(f.file_path, f.line, f"{f.pii_type}: {f.description}") for f in (getattr(dp, "pii_findings", None) or [])]
    iac_ev   = [_ev(f.file_path, f.line, f"{f.kind}: {f.description}") for f in iac_findings]
    secret_ev = [_ev(getattr(f, "file_path", ""), getattr(f, "line", ""),
                     f"{getattr(f,'kind','secret')} {getattr(f,'variable','')}")
                 for f in (getattr(se, "findings", None) or [])]
    cve_ev   = [_ev("", "", c) for c in cve_hits]

    def item(_id, title, failed, detail="", evidence=None):
        return {"id": _id, "title": title, "status": "fail" if failed else "pass",
                "detail": detail, "evidence": (evidence or [])[:8] if failed else []}

    # ── OWASP Top 10 ──
    owasp_items = []
    for code, title, cwe_set in _OWASP:
        hit = cwes & cwe_set
        failed = bool(hit)
        if code == "A06" and cve_hits:
            failed = True
        if code == "A10" and has_ssrf:
            failed = True
        if code == "A09" and logs_removed:
            failed = True
        if code == "A02" and secrets:
            failed = True
        detail = (f"{len(hit)} finding(s): CWE-{', CWE-'.join(map(str, sorted(hit)))}" if hit else
                  ("known CVEs in dependencies" if code == "A06" and cve_hits else
                   ("SSRF data-flow detected" if code == "A10" and has_ssrf else
                    ("logging/monitoring reduced" if code == "A09" and logs_removed else
                     ("secret/credential exposure" if code == "A02" and secrets else "no issues found")))))
        ev = [e for (n, e) in sec_ev if n in cwe_set]
        if code == "A02": ev += secret_ev
        if code == "A03": ev += taint_ev
        if code == "A06": ev += cve_ev
        if code == "A09": ev += obs_ev
        if code == "A10": ev += taint_ev
        owasp_items.append(item(code, title, failed, detail, ev))

    # ── PCI-DSS 4.0 (relevant subset) ──
    inj_ev = [e for (n, e) in sec_ev if n in {89, 77, 78, 94, 79, 90, 91}] + taint_ev
    pci_items = [
        item("3.4", "PAN/PII rendered unreadable (no clear-text storage)", pii > 0,
             f"{pii} unencrypted PII field(s)" if pii else "no clear-text PII detected", priv_ev),
        item("6.2.4", "Protect against injection attacks", has_injection,
             "injection finding/data-flow present" if has_injection else "none", inj_ev),
        item("6.3.3", "Patch known vulnerabilities (components)", bool(cve_hits),
             f"{len(cve_hits)} CVE(s) in changed deps" if cve_hits else "no known CVEs", cve_ev),
        item("8.3.1", "No hard-coded credentials/secrets", secrets,
             "secret/credential detected" if secrets else "none detected", secret_ev),
        item("10.2", "Audit logging present (not reduced)", logs_removed,
             "logging/monitoring reduced" if logs_removed else "logging intact", obs_ev),
        item("2.2", "Secure infrastructure configuration", bool(iac_findings),
             f"{len(iac_findings)} IaC misconfiguration(s)" if iac_findings else "no IaC issues", iac_ev),
    ]

    # ── CWE Top 25 ──
    hit25 = sorted((cwes | ({918} if has_ssrf else set())) & set(_CWE_TOP25))
    cwe_items = [{"id": f"CWE-{n}", "title": _CWE_TOP25[n], "status": "fail",
                  "detail": "detected in this change",
                  "evidence": [e for (cn, e) in sec_ev if cn == n][:8]} for n in hit25]

    standards = [
        {"name": "OWASP Top 10 (2021)", "items": owasp_items},
        {"name": "PCI-DSS 4.0", "items": pci_items},
        {"name": "CWE Top 25", "items": cwe_items or
            [{"id": "—", "title": "No CWE-Top-25 weaknesses detected", "status": "pass", "detail": ""}]},
    ]

    fails = sum(1 for s in standards for it in s["items"] if it["status"] == "fail")
    passes = sum(1 for s in standards for it in s["items"] if it["status"] == "pass")
    return {
        "overall": {"pass": passes, "fail": fails, "status": "FAIL" if fails else "PASS"},
        "standards": standards,
    }

# CIAA — Executive Summary

**An AI-powered pull-request reviewer that catches production issues *before* code is merged.**

Every code change is automatically analysed by **20 specialised expert agents** and reduced to a
single, defensible decision: **Approve, Hold, or Block** — with the reasons spelled out.

---

## Why it matters

| Without it | With CIAA |
|---|---|
| Issues found *in production* (incidents, hotfixes, breaches) | Issues found *at review time*, before merge |
| Inconsistent, manual reviews that miss things under time pressure | Consistent, 20-point expert review on every PR in minutes |
| "Looks fine to me" approvals | A gate decision with named, auditable reasons |
| Compliance checked late (or never) | OWASP / PCI-DSS / CWE mapped on every change |

---

## What it checks (in plain terms)

- **Security** — injection, leaked secrets/keys, SSRF, insecure infrastructure
- **Breaking changes** — API/contract changes that would take down dependent services
- **Data risk** — unsafe database migrations, unencrypted personal data (GDPR/PCI/PDPA)
- **Supply chain** — known vulnerabilities (CVEs) in dependencies
- **Performance** — slow queries and hot-path regressions
- **Testing** — whether the change is actually, meaningfully tested
- **Compliance** — OWASP Top 10 · PCI-DSS · CWE Top 25 pass/fail
- **Business impact** — which capabilities & teams a change affects, and its blast radius

---

## How it decides (the gate)

The 20 agents produce findings; a **deterministic policy** turns them into the gate —
**most-restrictive wins**, so the AI can never weaken a hard rule. A critical security flaw,
a leaked secret, a known CVE, or a destructive migration will **Block**; breaking APIs,
untested security changes, or unencrypted PII will **Hold**.

Built-in safeguards keep it trustworthy: results are **reproducible** (same input → same
verdict), hallucinated findings are **excluded from the gate**, and reviewer feedback
**reduces noise over time** — while serious findings are never auto-hidden.

---

## Audience value

- **Developers** — "here's exactly what to fix, and a ready-to-run test for the gap."
- **Reviewers** — "here's the gate decision, why, and the 3 things to scrutinise."
- **Managers** — an Insights dashboard: how many PRs are blocked, risk trends, top risk areas,
  and detection accuracy over time.

---

## Bottom line

CIAA shifts defect discovery **left** — from production incidents to pre-merge review — with an
expert, consistent, auditable second opinion on **every** pull request. Fewer escaped defects,
faster reviews, and continuous compliance evidence.

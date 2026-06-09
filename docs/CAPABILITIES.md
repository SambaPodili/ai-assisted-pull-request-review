# CIAA — Code Impact & PR Review Framework: Full Capabilities

**Purpose:** an expert, production-grade tool for **pull-request review** and **code-impact
analysis** that identifies production issues *before* a change is merged.

Every analysis runs **20 specialised agents** across **4 phases**. Each agent focuses on one
concern and emits structured findings. A deterministic **gate policy** then turns those
findings into one decision — **APPROVE / HOLD / BLOCK** (most-restrictive wins, so the AI can
never weaken a hard rule).

```
diff ─▶ Phase 1 (core) ─▶ Phase 1b (deep scan, parallel) ─▶ Phase 2 (integration) ─▶ Phase 3 (synthesis)
        │                                                                              │
        └────────────── findings ──────────────▶ Gate Policy ──▶ APPROVE / HOLD / BLOCK
```

**Engine legend:** `LLM` = always uses the model · `Static` = deterministic, zero-token (fast,
reproducible) · `Hybrid` = static first, LLM enhancement when budget allows.

---

## Phase 1 — Core

### 1. Code Analysis  `LLM`
- **Responsibility:** classify the change (feature / refactor / bugfix / config) and surface
  code-quality issues and complexity changes.
- **Catches:** risky refactors, complexity spikes, unclear change intent.
- **Gate:** informational.

### 2. Security  `LLM`
- **Responsibility:** scan for vulnerabilities mapped to CWE / OWASP (injection, auth, crypto,
  access control, deserialization…).
- **Catches:** the classic exploitable bugs that cause breaches.
- **Gate:** **critical → BLOCK**, **high → HOLD**. (Unverified/hallucinated findings excluded.)

---

## Phase 1b — Deep scan (run in parallel)

### 3. AST Analysis  `Hybrid`
- **Responsibility:** parse changed code into a syntax tree; reason about structure, nesting,
  and complexity rather than raw text.
- **Catches:** deeply nested logic, structural smells a text scan misses.

### 4. Entropy / Secrets  `Static`
- **Responsibility:** detect hardcoded secrets via Shannon entropy + known key prefixes
  (skips ordinary code identifiers to avoid false positives).
- **Catches:** API keys, tokens, passwords, private keys committed by accident.
- **Gate:** secret detected → **BLOCK** (PCI-DSS Req 8).

### 5. Taint Analysis  `Hybrid`
- **Responsibility:** track user-controlled data from **source → sink**.
- **Catches:** data-flow-proven **SQL/command injection, SSRF, path traversal** that single-line
  scanners miss.
- **Gate:** injection / SSRF / path-traversal → **BLOCK**.

### 6. IaC Scan  `Static`
- **Responsibility:** Terraform / Kubernetes / Docker misconfigurations.
- **Catches:** public S3 buckets, wildcard IAM, privileged containers, open security groups.
- **Gate:** **critical → BLOCK**, **high → HOLD**.

### 7. Temporal Risk  `Static`
- **Responsibility:** look across PR history (not just this PR) for risky patterns.
- **Catches:** repeatedly-touched "hot" files, escalating change risk, security erosion over time.

### 8. Schema Changes  `Static`
- **Responsibility:** detect DB / migration risk (Flyway, Liquibase, `.sql`).
- **Catches:** destructive or irreversible migrations, missing rollback.
- **Gate:** destructive **AND** irreversible → **BLOCK**; any migration → **HOLD** (verify rollback).

### 9. QA Scenarios  `LLM`
- **Responsibility:** generate the test scenarios that should be covered, with **preconditions,
  acceptance criteria, test data, and runnable test skeletons** (JUnit/pytest/Jest/Go).
  Becomes **requirement-aware** when functional documents are uploaded.
- **Catches:** untested behaviours, missing negative/edge/security cases, requirement gaps.

### 10. Reference Impact  `Static`
- **Responsibility:** find where changed symbols are used across the codebase (the **call graph**
  and blast radius), shown as concentric rings by call distance.
- **Catches:** changes that silently break callers elsewhere; high-fan-in functions.

### 11. Performance Impact  `Hybrid`
- **Responsibility:** static + LLM detection of performance regressions.
- **Catches:** N+1 queries, `SELECT *`, missing pagination, nested loops on hot paths.

### 12. Data Privacy  `Hybrid`
- **Responsibility:** detect PII handling against GDPR / PCI-DSS / PDPA.
- **Catches:** unencrypted PII, clear-text storage/transit of sensitive fields.
- **Gate:** unencrypted PII → **HOLD**.

### 13. Maintainability  `Hybrid`
- **Responsibility:** long functions, duplication, complexity smells.
- **Catches:** code that will be expensive/error-prone to change later.

### 14. License Compliance  `Static`
- **Responsibility:** check newly-added dependencies' licences.
- **Catches:** copyleft (GPL/AGPL) / unknown licences introduced into proprietary code.
- **Gate:** copyleft introduced → **BLOCK** (legal/IP exposure).

### 15. Observability  `Hybrid`
- **Responsibility:** detect removed logs/metrics and missing instrumentation on new paths.
- **Catches:** changes that would be undiagnosable in production (blind spots).

---

## Phase 2 — Integration (needs Phase-1 results)

### 16. Dependency Mapping  `Hybrid`
- **Responsibility:** compute **blast radius** across the service dependency graph; check changed
  dependencies for **known CVEs** (OSV). Includes the **Maven `pom.xml` SCA** (direct-dependency
  CVE scan, any branch, no lockfile/CI needed).
- **Catches:** vulnerable dependencies, changes that ripple across many services.
- **Gate:** known CVE → **BLOCK**; very large blast radius → **HOLD** (stage behind a flag/canary).

### 17. Test Coverage  `LLM`
- **Responsibility:** find test gaps and validate **per-method unit-test scenario coverage**
  (happy path, invalid input, null/empty, boundary, error path, state, security, concurrency,
  data, backward-compat, regression). Flags **hollow tests** (added with no assertions).
- **Catches:** changed code with no/weak tests → regressions in production.
- **Gate:** untested **security-relevant** method → **HOLD**; assertion-free tests → **HOLD**;
  large measured coverage drop → **HOLD/BLOCK**.

### 18. Interface / API  `Hybrid`
- **Responsibility:** detect contract-breaking changes (REST / gRPC / AsyncAPI / MQ) and trace
  **downstream consumer impact** (who breaks and how).
- **Catches:** breaking API changes that take down dependent services.
- **Gate:** breaking change → **HOLD** (confirm consumer migration).

---

## Phase 3 — Synthesis (sequential, last)

### 19. Risk Assessment  `LLM`
- **Responsibility:** synthesise **all** findings into a composite risk score and a *proposed*
  gate. (The deterministic policy then enforces the final gate — most-restrictive of AI + rules.)

### 20. Remediation  `LLM`
- **Responsibility:** produce concrete **fix diffs**, a **deployment strategy** (standard/canary/
  blue-green/phased/feature-flag), and an **executive summary**.
- **Catches/helps:** turns findings into actionable fixes + a safe rollout plan.

---

## The decision & governance layer

| Component | What it does |
|---|---|
| **Gate Policy** | Deterministic rules over the findings; final gate = most-restrictive of (AI proposal, policy). Every blocking reason is named and auditable. |
| **Evidence guard** | A security finding citing a file **not in the diff** is flagged *"location unverified"*, kept visible but **excluded from the gate** (no hallucinated blocks; nothing hidden). |
| **False-positive suppression** | Reviewers mark findings `false positive`; a CWE/category dismissed ≥3× (and never "valid") is auto-suppressed on future runs — **but high/critical security findings are never auto-deleted.** |
| **Compliance mapping** | Findings → **OWASP Top 10 / PCI-DSS / CWE Top 25** pass/fail report (exportable). |
| **Business-capability mapping** | Changed file paths → affected business capabilities + owning team + criticality. |
| **Consumer-impact tracing** | Which downstream call-sites a breaking change will affect, and the failure mode. |

---

## Supporting capabilities

- **Functional documents** — upload requirement specs (`.docx`/`.pdf`/text); QA scenarios become
  requirement-traceable.
- **Maven SCA** — upload `pom.xml` → real CVEs on declared dependencies (any branch, no CI).
- **LLM Judge Panel** — N independent judges score analysis quality (completeness / precision /
  severity accuracy / specificity); configurable models.
- **Reviewer feedback loop** — ⚐ false-positive / ✓ valid feeds suppression + an **accuracy**
  dashboard per agent over time.
- **Insights** — review queue, risk trends, change heatmap, API cost, detection accuracy, and a
  manager **executive summary**.
- **Personas** — Developer view ("what to fix") and Reviewer view ("what to scrutinise / gate"),
  each with a one-line headline; managers live in Insights.
- **Determinism** — temperature 0 → the same diff yields the same findings & gate (auditable).
- **Reliability** — per-call timeouts, fast-fail on unreachable models, admission control
  (concurrency cap + queue), graceful fallback so no agent can crash a run.

---

## How a result becomes a decision

1. 20 agents emit findings.
2. **Evidence guard** flags unsubstantiated findings; **suppression** removes confirmed noise.
3. **Gate policy** applies hard rules → BLOCK / HOLD / APPROVE (most-restrictive wins).
4. **Risk** + **Remediation** summarise; **Compliance** + **Capability** + **Consumer-impact**
   add governance context.
5. Reviewer sees the gate, the named reasons, and per-persona guidance — and can post the result
   to the PR.

**Where to look in the UI:** Results → *Summary* (gate + headline), *Security / Advanced*,
*Dependency* (+ Maven SCA), *Interface*, *Schema*, *QA Scenarios* (+ unit-test coverage),
*Checklist*, *Compliance*, *Remediation*, *Timings* (engine per agent) · Sidebar → *Analysis
agents* (this catalog) and *Insights*.

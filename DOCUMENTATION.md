# Code Impact & Analysis Framework (CIAA)
### Technical Documentation — v2.0

---

## Table of Contents

1. [Overview & Problem Statement](#1-overview--problem-statement)
2. [Solution Overview](#2-solution-overview)
3. [Architecture & Design](#3-architecture--design)
4. [Agent Framework](#4-agent-framework)
5. [Features & Capabilities](#5-features--capabilities)
6. [Integrations](#6-integrations)
7. [Dashboard & UI](#7-dashboard--ui)
8. [Roadmap](#8-roadmap)
9. [Comparison with SonarQube & Veracode](#9-comparison-with-sonarqube--veracode)

---

## 1. Overview & Problem Statement

### The Problem

Software engineering teams in regulated industries — particularly banking, fintech, and financial services — face a critical gap between **code change velocity** and **change risk awareness**.

When a developer raises a pull request, the questions that matter most are rarely answered before merge:

| Question | Typical Answer Today |
|---|---|
| What else in the codebase does this change break? | Unknown until production |
| Does this introduce a security vulnerability? | Caught by Checkmarx/Veracode hours/days later |
| Does this add a GPL dependency to proprietary code? | Discovered in a quarterly SCA audit |
| What is the performance impact of this change? | Found in post-release load testing |
| Does this touch PII fields — does it need a privacy review? | Raised in manual code review, inconsistently |
| Which downstream services are affected? | Tribal knowledge in the team |

The result is **late feedback**, expensive post-merge remediation, compliance gaps, and release delays.

### Why Existing Tools Fall Short

- **SonarQube / Checkmarx** scan for known patterns but have no semantic understanding of *impact* — they tell you a function is complex, not what breaks if you change it
- **Veracode / Snyk** catch security issues but run as isolated gates, not as intelligent advisors that explain the blast radius
- **Jira / Confluence** hold process knowledge but have no code awareness
- These tools operate in **silos** — no single view connects code quality, security, dependency risk, test coverage, and deployment recommendation together

### Regulatory Context

MAS TRM, SOC-2, and PCI-DSS all require demonstrable change management controls, software composition analysis (SCA), and data lineage visibility. Manual processes for these controls are costly and error-prone. CIAA automates these controls at the PR level, generating an audit trail for every change.

---

## 2. Solution Overview

CIAA is a **multi-agent AI analysis framework** that activates on every pull request and delivers a comprehensive impact report before code is merged.

### What it Does

```
Developer opens PR
        │
        ▼
CIAA webhook triggers (GitHub / Bitbucket)
        │
        ▼
20 specialised AI agents analyse in parallel
        │
        ▼
Unified impact report delivered in < 10 minutes
        │
        ▼
Developer, reviewer, and risk team see:
  • What breaks, where, and at what depth
  • Security vulnerabilities introduced
  • Performance, privacy, and compliance risks
  • Deployment strategy recommendation
  • Test scenarios to validate the change
  • Remediation steps with code examples
```

### Key Differentiators

- **Real-time** — results before code review begins, not hours later
- **Contextual** — understands the purpose of the change, not just the syntax
- **Hierarchical** — traces call graphs 2–3 levels deep across the codebase
- **Banking-aware** — built-in checks for MAS TRM, PCI-DSS, GDPR/PDPA data fields
- **Zero lock-in** — works with any LLM provider (Anthropic, OpenAI, Azure, Ollama, custom)
- **Graceful degradation** — static analysis runs even when the LLM API is unavailable

---

## 3. Architecture & Design

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      Git Providers                       │
│              GitHub        Bitbucket                     │
└──────────────────┬──────────────────────────────────────┘
                   │  Webhook
                   ▼
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Backend                        │
│  /api/v1/analyse  /api/v1/progress  /api/v1/report       │
│  Webhooks · Admin · Gate-override · Quality · Evaluate   │
└──────────────────┬──────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────┐
│                  Ingestion Layer                          │
│  diff_parser · git_client · webhook_parser               │
│  symbol_extractor · reference_finder · osv_client        │
│  service_graph_builder · language_registry               │
└──────────────────┬──────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────┐
│                  Orchestrator (core)                      │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  Phase 1a   │  │  Phase 1b    │  │   Phase 2/3   │  │
│  │  (parallel) │  │  (parallel)  │  │  (sequential) │  │
│  │             │  │  13 agents   │  │               │  │
│  │ code_analysis│  │  security    │  │  risk         │  │
│  │             │  │  ast_analysis│  │  remediation  │  │
│  └─────────────┘  │  ...         │  └───────────────┘  │
│                   └──────────────┘                       │
│         LangGraph DAG  ·  ThreadPoolExecutor             │
└──────────────────┬──────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────┐    ┌──────────────────┐
│  LLM Client  │    │  Storage Layer   │
│  Anthropic   │    │  Redis / Memory  │
│  OpenAI      │    │  ChromaDB        │
│  Azure OAI   │    │  Neo4j           │
│  Ollama      │    └──────────────────┘
│  Custom/Org  │
└──────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│              Single-Page Frontend (index.html)           │
│  Real-time progress · Call graph · 10 analysis tabs      │
│  D3.js force-directed graph · Tab groups · Live polling  │
└─────────────────────────────────────────────────────────┘
```

### Execution Pipeline

The orchestrator supports two execution strategies:

**Threaded Pipeline** (default for Phase ≤ 2)
```
Phase 1a  →  Phase 1b (13 agents, 13 threads)  →  Phase 2  →  Phase 3
```

**LangGraph DAG** (advanced mode, Phase 3)
```
node_code_analysis
node_security         ─┐
node_ast_analysis      │
node_secrets_entropy   │  →  node_dependency  →  node_risk  →  node_remediation
node_taint_analysis    │
node_iac_analysis      │
...13 more nodes      ─┘
```

### Token Budget Management

Each agent is allocated a configurable token budget (e.g., Security: 5,000 tokens, Code Analysis: 4,000 tokens). A central `TokenBudgetManager` tracks spend per agent per run and enforces limits, preventing runaway API costs on large PRs.

### Graceful Degradation

Every LLM-backed agent has a `fallback_result()` method. If the LLM call fails (429, 529, timeout), the agent returns a static-analysis-based result and sets `fallback_used: true` in the report. The analysis always completes.

### Retry Strategy

- HTTP 529 (capacity overload): 3 attempts, 2s/4s/8s backoff — max ~24s per agent
- Other transient errors (rate limit, connection, timeout): 5 attempts, 5s/10s/20s… backoff
- Implemented via Tenacity with a conditional `stop_base` subclass

---

## 4. Agent Framework

### Agent Inventory (20 agents)

#### Phase 1a — Foundation

| Agent | Type | What it does |
|---|---|---|
| `code_analysis` | LLM | Understands intent and quality of the change. Summarises what the PR does, complexity introduced, and readability concerns |

#### Phase 1b — Parallel Deep Analysis (13 agents, concurrent)

| Agent | Type | What it does |
|---|---|---|
| `security` | LLM | OWASP Top 10, injection patterns, auth bypasses, cryptographic weaknesses |
| `ast_analysis` | Static + LLM | Structural code analysis — cyclomatic complexity, coupling, dead code |
| `secrets_entropy` | Static | Shannon entropy scan for hardcoded credentials, API keys, tokens in the diff |
| `taint_analysis` | LLM | Tracks untrusted data flows from entry points to sinks (SQL, file, network) |
| `iac_analysis` | LLM | Terraform / CloudFormation / Kubernetes YAML misconfigurations |
| `temporal_risk` | Static | Friday deploys, holiday windows, change frequency risk, stale branch warnings |
| `schema_change` | LLM | Database schema migration safety — column drops, type changes, index removals |
| `qa_scenarios` | LLM | Generates specific test cases: happy path, edge cases, regression, integration |
| `reference_impact` | Static + LLM | Finds every caller of changed functions — 2–3 level call graph via auto-clone |
| `performance_impact` | LLM | O(n²) loops, N+1 queries, synchronous I/O in hot paths, memory leaks |
| `data_privacy` | Static + LLM | PII/PCI field detection, GDPR/PDPA/MAS compliance, encryption checks |
| `maintainability` | LLM | Technical debt, naming clarity, duplication, SOLID principles |
| `observability` | LLM | Missing logs, metrics, traces, alerting gaps in changed code |

#### Phase 2 — Synthesis

| Agent | Type | What it does |
|---|---|---|
| `dependency` | LLM | CVE lookup via OSV.dev, license risk, transitive dependency analysis |
| `test_coverage` | LLM | Maps changed functions to existing tests; identifies untested paths |
| `interface` | LLM | API contract changes — REST, GraphQL, gRPC — breaking vs. non-breaking |
| `license_compliance` | Static | Copyleft/GPL/LGPL/MPL detection in manifests — zero tokens, always runs |

#### Phase 3 — Action

| Agent | Type | What it does |
|---|---|---|
| `risk` | LLM | Synthesises all phase results into a deployment risk score (1–10) with deployment strategy recommendation |
| `remediation` | LLM | Generates prioritised, actionable fix steps with code examples |

### Base Agent Architecture

All agents extend `BaseAgent[T]` which provides:
- Automatic progress reporting (`agent_started` / `agent_done`)
- Token budget enforcement
- LLM client instantiation (provider-agnostic)
- `fallback_result()` on failure
- Duration and token tracking in the report

```python
class BaseAgent(ABC, Generic[T]):
    agent_name:   AgentName
    output_model: type[T]
    system_prompt: str

    def run(request, budget, context) -> T
    def build_user_prompt(request, context) -> str
    def fallback_result(request) -> T
```

---

## 5. Features & Capabilities

### 5.1 Multi-Level Call Graph

The reference impact agent performs a BFS traversal of the codebase:

- **Level 1** — files that directly call the changed functions
- **Level 2** — files that call the Level-1 callers' own functions
- **Level 3** — configurable via `REF_MAX_DEPTH`

**Reference search backends** (tried in priority order):

| Backend | Trigger | Capability |
|---|---|---|
| `local_grep` | `REPO_LOCAL_PATH` set | Full repo, all depths, fastest |
| `auto_clone` | `GITHUB_TOKEN` set | Auto shallow-clones repo to temp dir, full grep, all depths |
| `github_api` | Git binary unavailable | Default branch only, L1 only |
| `diff_scan` | Always (fallback) | Within PR diff only, L1 only |

The call graph is rendered as an interactive D3.js force-directed graph with colour-coded depth levels (orange = changed, blue = L1, teal = L2, purple = L3).

### 5.2 Security Analysis

- **OWASP Top 10** pattern detection (injection, XSS, IDOR, broken auth)
- **Taint analysis** — untrusted input → sensitive sink tracing
- **Secrets entropy scan** — Shannon entropy + pattern matching for credentials
- **IaC security** — Terraform, Kubernetes, CloudFormation misconfigurations
- **CVE lookup** — real-time OSV.dev API query for changed dependencies

### 5.3 Banking & Compliance Controls

| Control | Agent | Standard |
|---|---|---|
| PII/PCI field detection | `data_privacy` | GDPR, PDPA, PCI-DSS |
| Copyleft license check | `license_compliance` | SOC-2, MAS TRM |
| Schema migration safety | `schema_change` | Internal change management |
| Temporal risk (Friday deploys) | `temporal_risk` | MAS TRM release controls |
| Secrets in code | `secrets_entropy` | PCI-DSS Req 6 |

### 5.4 Large PR Support

- Symbol ranking by blast radius (multi-hunk symbols ranked higher)
- Maximum 30 symbols per analysis — long-tail filtered automatically
- `MAX_DIFF_LINES` configurable to handle PRs with hundreds of file changes
- Parallel agent execution — 13 agents run concurrently, not serially

### 5.5 Deployment Recommendation

The risk agent outputs a structured deployment recommendation:

- **Risk score** 1–10 with justification
- **Strategy** — Standard / Canary / Blue-Green / Phased / Feature Flag
- **Rollback plan** — specific steps given the nature of the change
- **Go/No-Go** recommendation with blocking issues listed

### 5.6 QA Scenario Generation

The `qa_scenarios` agent generates test cases tailored to the change:
- Happy path scenarios for each changed function
- Edge cases derived from the diff context
- Integration test suggestions for downstream services
- Regression test recommendations for modified shared libraries

### 5.7 Token Economy

Every analysis run tracks total token spend per agent. The Timings tab in the dashboard shows:
- Tokens consumed per agent
- LLM call duration per agent
- Which agents fell back to static analysis
- Total analysis duration

---

## 6. Integrations

### 6.1 Git Providers

| Provider | Webhook | PR Diff API | Status |
|---|---|---|---|
| GitHub | ✓ `pull_request` events | ✓ GitHub REST API | Production |
| Bitbucket Cloud | ✓ `pullrequest:created/updated` | ✓ Bitbucket API 2.0 | Production |
| Bitbucket Server | Configurable | Configurable | Configurable |

### 6.2 LLM Providers

All providers share a single `UnifiedLLMClient` interface:

| Provider | Model examples | Auth |
|---|---|---|
| Anthropic | `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| OpenAI | `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo` | `OPENAI_API_KEY` |
| Azure OpenAI | `gpt-4o` (deployment name) | `OPENAI_API_KEY` + `LLM_BASE_URL` |
| Ollama (local) | `llama3.2`, `codellama`, `qwen2.5-coder` | No key — local only |
| Custom / Org | Any OpenAI-compatible endpoint | `LLM_API_KEY` + `LLM_BASE_URL` |

Per-request LLM override: API callers can pass `llm_config` in the request body to use a different model for a specific analysis without changing the server configuration.

### 6.3 Storage

| Component | Backend options | Purpose |
|---|---|---|
| Report store | Redis (persistent) / In-memory (dev) | Stores analysis reports, queryable by request_id |
| Vector store | ChromaDB / In-memory keyword fallback | Semantic similarity search for historical patterns |
| Graph database | Neo4j / NetworkX in-process | Service dependency graph, call relationship storage |

### 6.4 Notifications

| Channel | Config key | Trigger |
|---|---|---|
| Slack | `SLACK_WEBHOOK_URL` | Analysis complete, risk score ≥ threshold |
| Microsoft Teams | `TEAMS_WEBHOOK_URL` | Analysis complete, risk score ≥ threshold |

### 6.5 Observability

| Component | Technology | What it tracks |
|---|---|---|
| Distributed tracing | OpenTelemetry → Jaeger (OTLP/gRPC) | Per-request spans, agent durations, LLM call latency |
| Metrics | Prometheus `/metrics` endpoint | Request count, analysis duration, token spend, agent error rate |
| Structured logging | Python `logging` with request_id correlation | Per-agent log lines tied to analysis run |

### 6.6 CI/CD Gate

A quality gate endpoint (`/api/v1/quality/gate/{request_id}`) returns PASS/FAIL/WARNING based on configurable thresholds:
- Minimum security score
- Maximum risk level
- Required agents completed
- Can be polled from GitHub Actions, Jenkins, or Bitbucket Pipelines to block merge on critical findings

### 6.7 Webhook Security

All inbound webhooks are validated with HMAC-SHA256 signatures (`GITHUB_WEBHOOK_SECRET`, `BITBUCKET_WEBHOOK_SECRET`) and deduplicated with a 300-second TTL window to prevent double-processing of retried events.

---

## 7. Dashboard & UI

The dashboard is a single-page application (`frontend/index.html`) with no build step required — served statically from the FastAPI backend.

### Navigation

```
Analysis
  ├── Configure          LLM provider, model, API key settings
  ├── Repositories       Connect GitHub/Bitbucket repos, set primary
  ├── Analysis target    Select PR / branch, trigger analysis
  └── Results            Full report view (tabbed)

History
  └── Past analyses      Searchable list of previous reports

Validation
  └── Quality metrics    Token spend, agent performance trends

Settings
  └── Backend config     Backend URL, auth token
```

### Results Tabs (grouped)

**Overview**
- Summary — executive risk score, deployment strategy, key findings at a glance

**Security**
- Security — OWASP findings, severity ratings, vulnerable code snippets
- Advanced — AST analysis, secrets/entropy, taint flows, IaC issues, temporal risk, schema changes

**Impact**
- References — Interactive D3 call graph with L1/L2/L3 hierarchy, high-impact file list
- Dependency — CVE findings, license compliance, transitive risk
- Interface — API contract changes, breaking vs. additive
- Schema — Database migration risk, column/type/index changes

**Quality**
- QA Scenarios — Generated test cases by type (functional, edge case, integration, regression)
- Performance — Algorithmic complexity issues, N+1 queries, memory concerns
- Privacy — PII/PCI fields touched, encryption gaps, data handling obligations
- Quality — Technical debt, maintainability score, SOLID violations, observability gaps

**Actions**
- Remediation — Prioritised fix steps with code examples
- Timings — Per-agent token spend, duration, fallback status

### Real-Time Progress Panel

While analysis runs, a live progress panel shows all 20 agents with status indicators:
- `pending` — waiting to start
- `running` — LLM call in progress
- `done` — completed with token count and duration
- `fallback` — completed using static analysis (LLM unavailable)

Phase 1b (13 agents) is shown in a compact 2-column grid. Phase labels indicate which pipeline phase is active.

### Call Graph

The reference impact call graph is an interactive SVG rendered with D3.js:
- **Force-directed layout** — nodes repel, links attract
- **Colour coding** — orange (changed symbol), blue (L1 caller), teal (L2), purple (L3), amber (shared lib)
- **Edge style** — solid line = direct call, dashed = indirect (deeper BFS level)
- **Interactions** — drag nodes, scroll to zoom, click to highlight subtree
- **Reset button** — restores initial layout

---

## 8. Roadmap

### Near-Term (Q3 2026)

| Feature | Description |
|---|---|
| **IDE plugin** | VS Code extension that shows CIAA analysis inline as you write code, not just at PR time |
| **GitLab support** | Webhook handler and merge request API integration |
| **Auto-remediation PRs** | For low-risk mechanical fixes (unused imports, formatting), CIAA opens a remediation PR automatically |
| **Multi-repo blast radius** | When a shared library changes, analyse impact across all downstream repos that depend on it |
| **Azure DevOps support** | Pull request events and pipeline gate integration |

### Medium-Term (Q4 2026)

| Feature | Description |
|---|---|
| **Historical trend dashboard** | Risk score trend per repo/team over time; identify which teams and files are consistently high-risk |
| **LLM fine-tuning** | Fine-tune a smaller model on your org's codebase and past PR reviews for faster, lower-cost analysis |
| **Slack bot** | Interactive Slack bot: `/ciaa analyse PR#123` returns summary inline; `/ciaa risk` shows org-wide risk posture |
| **JIRA integration** | Auto-create JIRA tickets for critical security findings; link analysis reports to existing tickets |
| **Policy as code** | Define custom rules in YAML (e.g., "flag any change to `auth/` files for mandatory security review") |

### Long-Term (2027)

| Feature | Description |
|---|---|
| **Predictive risk model** | ML model trained on historical incidents to predict which changes are most likely to cause production issues |
| **Knowledge graph** | Persistent graph database of all code relationships, updated on every merge — enables "what changed since last release" queries |
| **Voice interface** | Natural language query over past analyses: "show me all PRs that touched payment processing in the last 90 days" |
| **Compliance report generation** | Auto-generate MAS TRM / SOC-2 change management evidence packages from the analysis history |

---

## 9. Comparison with SonarQube & Veracode

### Feature Matrix

| Capability | CIAA | SonarQube | Veracode |
|---|---|---|---|
| **Runs at PR time** | ✅ Webhook-triggered, results before review | ✅ PR decoration | ✅ Pipeline scan (slow) |
| **Understands change intent** | ✅ LLM reads the diff and explains what changed | ❌ Pattern-only | ❌ Pattern-only |
| **Multi-level call graph** | ✅ BFS L1/L2/L3 with interactive graph | ❌ | ❌ |
| **Downstream service impact** | ✅ Service dependency graph | ❌ | ❌ |
| **Security vulnerability detection** | ✅ OWASP Top 10 + taint analysis | ✅ SAST rules | ✅ SAST (very deep) |
| **CVE / dependency scanning** | ✅ OSV.dev real-time | ✅ (paid tier) | ✅ SCA module |
| **License compliance** | ✅ Built-in (MIT/GPL/LGPL/MPL) | ⚠️ Plugin | ✅ (paid) |
| **PII / privacy detection** | ✅ GDPR/PDPA/PCI field detection | ❌ | ⚠️ Limited |
| **Performance analysis** | ✅ Algorithmic + N+1 query detection | ⚠️ Code smells only | ❌ |
| **Test scenario generation** | ✅ LLM-generated QA scenarios | ❌ | ❌ |
| **Deployment strategy recommendation** | ✅ Risk score + Canary/Blue-Green guidance | ❌ | ❌ |
| **Remediation with code examples** | ✅ Agent generates fix code | ⚠️ Generic advice | ⚠️ Generic advice |
| **Schema migration safety** | ✅ Column drop / type change detection | ❌ | ❌ |
| **IaC security** | ✅ Terraform / K8s / CloudFormation | ✅ (plugin) | ⚠️ Limited |
| **Secrets / credential detection** | ✅ Shannon entropy + patterns | ✅ | ✅ |
| **Banking / MAS TRM context** | ✅ Built-in regulatory awareness | ❌ | ❌ |
| **Custom LLM / on-premise AI** | ✅ Ollama, any OpenAI-compatible API | ❌ | ❌ |
| **Real-time progress tracking** | ✅ Per-agent live status panel | ❌ | ❌ |
| **Self-hosted** | ✅ Docker, any infra | ✅ | ✅ (complex) |
| **Open source** | ✅ | ✅ Community edition | ❌ Commercial only |
| **Distributed tracing** | ✅ OpenTelemetry → Jaeger | ❌ | ❌ |

### Positioning

```
                        Static Analysis                 AI-Assisted Analysis
                              │                                │
Deep Security Focus ─── Veracode ────────────────────────────┤
                              │                               │
Code Quality Focus ──── SonarQube ───────────────────────────┤
                              │                               │
Full Impact + Context ──────────────────────────────── CIAA  ◄──
                              │                               │
```

### When to Use What

**SonarQube** — Best for continuous code quality metrics, technical debt tracking, and broad language coverage across an existing codebase. Excellent dashboards for engineering managers. Lacks change-impact awareness.

**Veracode** — Best for deep binary-level SAST, compliance reporting for regulated industries, and formal security certifications. Runs on compiled artefacts, not just source diff. Expensive; slow feedback loop.

**CIAA** — Best when you need to understand **what this specific change breaks**, not just whether the code has issues. Particularly strong for:
- Large teams where reviewers cannot know every downstream dependency
- Regulated environments where MAS TRM / SOC-2 controls must be demonstrable
- Orgs that want to enforce data privacy checks at PR time without manual review
- Teams that need actionable remediation steps, not just a list of findings

### Complementary Use

CIAA is **not a replacement** for Veracode in environments that require formal SAST certification. The recommended stack is:

```
CIAA (at PR time, developer feedback loop)
  +
SonarQube (continuous quality baseline)
  +
Veracode (quarterly / release certification scans)
```

This gives immediate feedback where it is cheapest to fix (at PR), baseline quality enforcement, and formal compliance evidence where required.

---

*Document generated: May 2026 | CIAA v2.0 | 20 agents | 5 analysis phases*

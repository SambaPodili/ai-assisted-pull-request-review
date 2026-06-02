# Code Impact & Analysis Framework
## Executive Brief

**Audience:** Technology Leadership, Risk, Compliance, Product  
**Classification:** Internal

---

## The Problem We Are Solving

Every day, developers across our engineering teams make hundreds of changes to production code. Before any change is merged, a fundamental question must be answered:

> **"What is the full impact of this change — and is it safe to deploy?"**

Today, that question is answered too late, too slowly, and too inconsistently.

---

### What Goes Wrong Without This

| Scenario | What Happens Today | Cost |
|---|---|---|
| A developer changes a shared payment function | Nobody knows which of the 40 downstream services will break until they break | Production incident, P1 call, emergency rollback |
| A new open-source library is added with a GPL licence | Discovered in a quarterly audit, months after it entered the codebase | Legal exposure, emergency removal sprint |
| PII data fields are added without encryption | Caught in an annual PDPA review | Regulatory finding, potential fine, remediation cost |
| A database column is dropped | Schema migration fails silently in UAT | Delayed release, weekend war room |
| A Friday deployment introduces a security flaw | Security team finds it in the next Veracode scan — a week later | Breach window open for days |

These are not hypothetical scenarios. They are the leading causes of production incidents, release delays, and regulatory findings in software organisations.

---

## What CIAA Does

**CIAA (Code Impact & Analysis Framework)** is an AI-powered system that automatically analyses every code change the moment a developer raises a pull request — before any reviewer has seen it, before it is merged, and before it reaches any environment.

Within minutes of a code change being submitted, CIAA delivers a complete impact report covering:

- What the change does and what risk it carries
- Every part of the codebase that could be affected
- Security vulnerabilities introduced
- Privacy and compliance obligations triggered
- Performance and reliability concerns
- A recommended deployment approach (standard, staged, or feature-flagged)
- Specific steps to fix any issues found

**The developer gets the right information at the right time** — when it is cheapest and fastest to act on it.

---

## How It Works — In Plain Terms

Think of CIAA as **20 specialist reviewers working simultaneously**, each expert in a different domain, examining every code change the instant it arrives.

```
Developer submits a code change
            │
            ▼
    20 AI specialists analyse it in parallel
    ┌─────────────────────────────────────┐
    │ Security expert                     │
    │ Privacy & compliance expert         │
    │ Performance expert                  │
    │ Code quality expert                 │
    │ Dependency & licence expert         │
    │ Database migration expert           │
    │ Test coverage expert                │
    │ Deployment risk expert              │
    │ ... and 12 more                     │
    └─────────────────────────────────────┘
            │
            ▼
    Single unified report delivered
    to developer, reviewer, and risk team
    in under 10 minutes
```

This process runs automatically on every pull request — no manual trigger, no human bottleneck.

---

## Business Value

### 1. Shift the Cost of Defects Left

The industry benchmark is well established: fixing a bug in production costs **100× more** than fixing it during code review. CIAA moves the discovery of issues from production incidents to the developer's screen — before a single line of bad code is merged.

### 2. Reduce Compliance Risk at Source

Rather than discovering PII exposure, GPL licence violations, or missing encryption in periodic audits, CIAA flags these automatically at the point of change. Every analysis is logged, creating an auditable trail that directly supports MAS TRM, SOC-2, and PCI-DSS change management requirements.

### 3. Protect Shared Services From Unintended Breakage

CIAA maps the full call chain of a change — tracing which functions call which, two and three levels deep across the entire codebase. A change to a shared payments library no longer silently propagates to downstream services; CIAA shows exactly which services are affected and at what severity.

### 4. Reduce Reviewer Cognitive Load

Senior engineers and architects spend significant time in code review trying to understand the downstream implications of a change. CIAA provides this context automatically. Reviewers focus on judgment calls; CIAA handles the mechanical impact analysis.

### 5. Accelerate Safe Delivery

Teams slow down before major releases not because they lack capability, but because they lack confidence. CIAA provides the evidence — risk score, affected services, deployment recommendation — that allows technology leaders to make faster, better-informed go/no-go decisions.

---

## What CIAA Checks — Business Language

| Domain | What It Looks For | Why It Matters |
|---|---|---|
| **Security** | Injection attacks, broken authentication, exposed credentials, insecure data handling | Prevents breaches; satisfies PCI-DSS Requirement 6 |
| **Data Privacy** | Changes that touch customer name, NRIC, account number, transaction data | PDPA/GDPR obligation; MAS data management expectation |
| **Licence Compliance** | Open-source components with viral GPL licences mixed into proprietary code | IP ownership risk; SOC-2 SCA requirement |
| **Change Impact** | Which services, APIs, and teams are affected by the change | Prevents uncoordinated releases breaking downstream consumers |
| **Database Safety** | Destructive schema changes — column removals, type changes, missing rollback | Prevents data loss, failed migrations, silent production errors |
| **Deployment Risk** | Is this change safe to deploy right now? Friday? At end of quarter? | Release governance; MAS TRM change management |
| **Code Quality** | Technical debt, duplicated logic, excessive complexity, missing tests | Long-term maintainability; onboarding cost |
| **Performance** | Inefficient algorithms, database query patterns that degrade under load | Prevents performance incidents; reduces infrastructure cost |
| **Third-Party Vulnerabilities** | Known CVEs in newly added or updated dependencies | Prevents supply chain attacks; patch management evidence |
| **Observability** | Are there sufficient logs and alerts for the new code to be monitored? | Operational confidence; faster incident response |

---

## Comparison With Existing Tools

Your teams likely already use tools like SonarQube or Veracode. CIAA does not replace them — it fills the gap they leave.

| | SonarQube | Veracode | **CIAA** |
|---|---|---|---|
| **When it runs** | After code is committed | After a build is produced | The moment a PR is raised |
| **Feedback speed** | Minutes to hours | Hours to days | Under 10 minutes |
| **Understands change intent** | No — scans entire codebase | No — scans compiled binary | Yes — analyses what changed and why |
| **Downstream impact tracing** | No | No | Yes — 2–3 level call graph |
| **Deployment recommendation** | No | No | Yes — risk score + strategy |
| **Generates fix instructions** | Generic hints | Generic hints | Specific, actionable steps |
| **Privacy / PII awareness** | Limited | Limited | Built-in (GDPR/PDPA/PCI) |
| **Regulatory context** | No | No | Built-in (MAS TRM, SOC-2) |
| **Works on-premise / private cloud** | Yes | Complex | Yes |

**Recommended approach:** Run CIAA at PR time for immediate developer feedback. Keep SonarQube for quality trend tracking. Keep Veracode for formal security certification scans. Each tool does what it does best.

---

## Risk Without CIAA

| Risk Category | Without CIAA | With CIAA |
|---|---|---|
| Security vulnerabilities merged | Discovered in next Veracode scan or in production | Flagged before merge |
| GPL licence introduced | Found in annual SCA audit | Blocked at PR |
| Shared service broken by change | Production incident | Impact map generated before deploy |
| PII field added without controls | Compliance finding | Flagged with specific obligation at PR time |
| Developer deploys on a high-risk day | Possible | Temporal risk warning issued |
| Bad database migration deployed | UAT failure or production data loss | Schema risk assessed before merge |

---

## Deployment & Commercial Model

**Deployment options:**
- On-premise (Docker, existing Kubernetes cluster)
- Private cloud (AWS, Azure, GCP)
- No data leaves your environment — code diffs are processed on your infrastructure

**AI provider flexibility:**
- Works with Anthropic Claude, OpenAI GPT-4, Microsoft Azure OpenAI
- Can run entirely on-premise using local AI models (Llama via Ollama) — no external API calls
- No single-vendor dependency

**What you need to operate it:**
- A server to run the backend (existing Kubernetes or VM)
- A GitHub or Bitbucket API token (already used by most teams)
- An AI API key — or a local GPU for fully private operation

---

## Summary

CIAA gives technology and risk leadership confidence that every code change has been comprehensively reviewed before it reaches production — not by adding process overhead, but by automating the analysis that currently depends on the knowledge, availability, and consistency of individual engineers.

The outcome is faster releases with less risk, a stronger compliance posture, and a developer experience where problems are caught early rather than escalated late.

---

*For technical architecture detail, see the companion Technical Documentation.*  
*For a live demonstration, contact the Engineering Platform team.*

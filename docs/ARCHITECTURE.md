# CIAA — Pipeline & Gate Flow

How a pull request flows through the 20 agents and becomes an Approve / Hold / Block decision.

## Analysis pipeline

```mermaid
flowchart TD
    PR[Pull Request / diff] --> P1

    subgraph P1["Phase 1 · Core"]
        CA[Code Analysis]
        SEC[Security]
    end

    P1 --> P1B

    subgraph P1B["Phase 1b · Deep scan (parallel)"]
        direction LR
        AST[AST] ; ENT[Entropy/Secrets] ; TAINT[Taint] ; IAC[IaC]
        TEMP[Temporal] ; SCH[Schema] ; QA[QA Scenarios] ; REF[Reference Impact]
        PERF[Performance] ; PRIV[Data Privacy] ; MAINT[Maintainability]
        LIC[License] ; OBS[Observability]
    end

    P1B --> P2

    subgraph P2["Phase 2 · Integration"]
        DEP[Dependency + CVE/SCA]
        TEST[Test Coverage]
        IFACE[Interface / API]
    end

    P2 --> P3

    subgraph P3["Phase 3 · Synthesis"]
        RISK[Risk Assessment]
        REM[Remediation]
    end

    P3 --> GATE{{Gate Policy\nmost-restrictive wins}}
    GATE -->|critical sec / secret / CVE / destructive migration / copyleft| BLOCK[BLOCK]
    GATE -->|breaking API / PII / untested security / migration| HOLD[HOLD]
    GATE -->|no blocking evidence| APPROVE[APPROVE]
```

## From finding to decision

```mermaid
flowchart LR
    F[Agent findings] --> EV[Evidence guard\nunverified → excluded from gate]
    EV --> SUP[Suppression\nconfirmed false positives removed]
    SUP --> GP[Gate Policy\nhard rules + AI proposal]
    GP --> D{Decision}
    D --> A[APPROVE]
    D --> H[HOLD]
    D --> B[BLOCK]
    GP --> GOV[Governance context]
    GOV --> C[Compliance: OWASP / PCI-DSS / CWE]
    GOV --> CAP[Business capabilities + teams]
    GOV --> CI[Downstream consumer impact]
```

## ASCII fallback (if Mermaid doesn't render)

```
PR/diff
   │
   ▼
Phase 1   : Code Analysis · Security
   │
   ▼
Phase 1b  : AST · Entropy/Secrets · Taint · IaC · Temporal · Schema · QA ·
   │        Reference · Performance · Privacy · Maintainability · License · Observability   (parallel)
   ▼
Phase 2   : Dependency(+CVE/SCA) · Test Coverage · Interface/API
   │
   ▼
Phase 3   : Risk Assessment · Remediation
   │
   ▼
Evidence guard ─▶ Suppression ─▶ GATE POLICY (most-restrictive wins)
   │
   ├─ BLOCK   ← critical security · secret · CVE · destructive+irreversible migration · copyleft
   ├─ HOLD    ← breaking API · unencrypted PII · untested security method · migration · big blast radius
   └─ APPROVE ← no blocking evidence
                   │
                   └─▶ Governance: Compliance (OWASP/PCI/CWE) · Capabilities/teams · Consumer impact
```

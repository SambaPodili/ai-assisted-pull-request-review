"""
core/models.py
--------------
Single source of truth for all data contracts.
Every agent input/output is a Pydantic model defined here.
"""
from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


# ═════════════════════════════════════════════════════════════════════════════
#  Enumerations
# ═════════════════════════════════════════════════════════════════════════════

class RiskLevel(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

    def as_score(self) -> int:
        return {"low": 10, "medium": 40, "high": 70, "critical": 95}[self.value]

    def __gt__(self, other: "RiskLevel") -> bool:
        order = [self.LOW, self.MEDIUM, self.HIGH, self.CRITICAL]
        return order.index(self) > order.index(other)


class ChangeType(str, Enum):
    PR       = "pull_request"
    BRANCH   = "branch_diff"
    COMMIT   = "commit_diff"
    DEPLOY   = "pre_deploy"


class GateDecision(str, Enum):
    APPROVE = "APPROVE"
    HOLD    = "HOLD"
    BLOCK   = "BLOCK"


class AgentName(str, Enum):
    ORCHESTRATOR    = "orchestrator"
    CODE_ANALYSIS   = "code_analysis"
    SECURITY        = "security"
    AST_ANALYSIS    = "ast_analysis"
    SECRETS_ENTROPY = "secrets_entropy"
    TAINT_ANALYSIS  = "taint_analysis"
    IAC_ANALYSIS    = "iac_analysis"
    TEMPORAL_RISK   = "temporal_risk"
    SCHEMA_CHANGE   = "schema_change"
    QA_SCENARIOS    = "qa_scenarios"
    DEPENDENCY      = "dependency"
    TEST_COVERAGE   = "test_coverage"
    INTERFACE       = "interface"
    RISK            = "risk"
    REMEDIATION     = "remediation"
    REFERENCE_IMPACT      = "reference_impact"
    PERFORMANCE_IMPACT    = "performance_impact"
    DATA_PRIVACY          = "data_privacy"
    MAINTAINABILITY       = "maintainability"
    LICENSE_COMPLIANCE    = "license_compliance"
    OBSERVABILITY         = "observability"


class DeploymentStrategy(str, Enum):
    STANDARD     = "standard"
    CANARY       = "canary"
    BLUE_GREEN   = "blue_green"
    PHASED       = "phased"
    FEATURE_FLAG = "feature_flag"


# ═════════════════════════════════════════════════════════════════════════════
#  Input Contracts
# ═════════════════════════════════════════════════════════════════════════════

class DiffHunk(BaseModel):
    """A single file's contribution to a unified diff."""
    file_path:  str
    language:   str = "unknown"
    additions:  int = 0
    deletions:  int = 0
    content:    str     # raw unified diff text

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


class PRMetadata(BaseModel):
    """Optional pull-request metadata supplied by the webhook or API caller."""
    pr_number:  int    = 0
    pr_title:   str    = ""
    author:     str    = ""
    labels:     list[str] = []
    base_sha:   str    = ""
    head_sha:   str    = ""


class AnalysisRequest(BaseModel):
    """Canonical input to the orchestrator."""
    model_config = ConfigDict(protected_namespaces=())

    request_id:   str
    change_type:  ChangeType
    repo_url:     str
    source_ref:   str
    target_ref:   str
    hunks:        list[DiffHunk] = []
    metadata:     dict[str, Any] = {}
    pr:           PRMetadata     = Field(default_factory=PRMetadata)
    model_config_: dict[str, Any] = {}   # per-request LLM provider override
    created_at:   datetime = Field(default_factory=datetime.utcnow)

    @property
    def total_churn(self) -> int:
        return sum(h.churn for h in self.hunks)

    @property
    def changed_files(self) -> list[str]:
        return [h.file_path for h in self.hunks]


# ═════════════════════════════════════════════════════════════════════════════
#  Agent Result Base
# ═════════════════════════════════════════════════════════════════════════════

class AgentResultBase(BaseModel):
    """Base fields present on every agent result."""
    model_config = ConfigDict(protected_namespaces=())

    token_usage:   int   = 0
    model_used:    str   = ""
    fallback_used: bool  = False
    duration_s:    float = 0.0   # wall-clock seconds for this agent's run


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 1 — Code Analysis & Security
# ═════════════════════════════════════════════════════════════════════════════

class CodeFinding(BaseModel):
    file_path:   str
    line_range:  str
    severity:    RiskLevel
    category:    str       # complexity | smell | logic | dead_code | anti_pattern
    description: str
    suggestion:  str = ""


class CodeAnalysisResult(AgentResultBase):
    summary:          str
    change_type:      str   # refactor | feature | bugfix | config | mixed
    complexity_delta: int = 0
    findings:         list[CodeFinding] = []


class SecurityFinding(BaseModel):
    file_path:   str
    line_range:  str
    severity:    RiskLevel
    cwe_id:      str = ""
    owasp_cat:   str = ""
    mas_trm_ref: str = ""
    description: str
    remediation: str


class SecurityResult(AgentResultBase):
    findings:          list[SecurityFinding] = []
    secrets_detected:  bool = False
    compliance_flags:  list[str] = []
    overall_severity:  RiskLevel = RiskLevel.LOW


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 2 — Dependency, Test Coverage, Interface
# ═════════════════════════════════════════════════════════════════════════════

class DependencyNode(BaseModel):
    name:     str
    version:  str  = ""
    team:     str  = ""
    critical: bool = False   # used by 3+ downstream services


class DependencyResult(AgentResultBase):
    affected_services:   list[str] = []
    blast_radius_score:  int = 0     # 0-100
    dependency_nodes:    list[DependencyNode] = []
    cve_hits:            list[str] = []
    changed_packages:    list[str] = []


class TestGap(BaseModel):
    file_path:       str
    uncovered_path:  str
    suggested_test:  str


class TestCoverageResult(AgentResultBase):
    coverage_delta:   float = 0.0
    uncovered_paths:  list[TestGap] = []
    regression_risk:  RiskLevel = RiskLevel.LOW
    generated_stubs:  list[str] = []


class ContractBreak(BaseModel):
    interface_type: str    # REST | gRPC | AsyncAPI | MQ
    path:           str
    break_type:     str    # removed | renamed | type_change | required_added
    consumers:      list[str] = []
    severity:       RiskLevel = RiskLevel.HIGH


class InterfaceResult(AgentResultBase):
    breaking_changes:   list[ContractBreak] = []
    schema_diffs:       list[str] = []
    affected_consumers: list[str] = []


# ═════════════════════════════════════════════════════════════════════════════
#  Phase 3 — Risk & Remediation
# ═════════════════════════════════════════════════════════════════════════════

class RiskResult(AgentResultBase):
    overall_risk:          RiskLevel
    risk_score:            int = 0       # 0-100
    deployment_guidance:   str = ""
    rollback_feasibility:  str = ""      # easy | moderate | complex | infeasible
    gate_decision:         GateDecision = GateDecision.HOLD
    rationale:             str = ""


class RemediationResult(AgentResultBase):
    fix_suggestions:      list[str] = []
    validation_checklist: list[str] = []
    deployment_strategy:  DeploymentStrategy = DeploymentStrategy.STANDARD
    executive_summary:    str = ""


# ═════════════════════════════════════════════════════════════════════════════
#  Advanced Detection Results
# ═════════════════════════════════════════════════════════════════════════════

class EntropyFinding(BaseModel):
    file_path:   str
    line:        int
    value:       str        # redacted — first 6 chars only
    entropy:     float      # Shannon bits/char
    kind:        str        # high_entropy | known_prefix | base64_blob | hex_key
    severity:    RiskLevel
    variable:    str = ""   # variable name it was assigned to

class SecretsEntropyResult(AgentResultBase):
    findings:           list[EntropyFinding] = []
    high_entropy_count: int = 0
    known_prefix_count: int = 0
    overall_severity:   RiskLevel = RiskLevel.LOW


class ASTFinding(BaseModel):
    file_path:    str
    line:         int
    function:     str
    kind:         str       # dead_code | null_risk | type_confusion | complexity_spike | unreachable
    severity:     RiskLevel
    description:  str
    suggestion:   str = ""

class FunctionProfile(BaseModel):
    name:           str
    file_path:      str
    line:           int
    callers:        list[str] = []   # functions that call this one
    callees:        list[str] = []   # functions this one calls
    cyclomatic:     int = 1
    param_count:    int = 0
    has_null_check: bool = True

class ASTAnalysisResult(AgentResultBase):
    findings:         list[ASTFinding] = []
    function_profiles: list[FunctionProfile] = []
    avg_complexity:   float = 0.0
    max_complexity:   int = 0
    hot_functions:    list[str] = []   # functions changed + many callers


class TaintSource(BaseModel):
    file_path: str
    line:      int
    variable:  str
    source:    str   # request_param | env_var | file_read | db_read | cli_arg

class TaintSink(BaseModel):
    file_path: str
    line:      int
    variable:  str
    sink:      str   # sql_query | exec | file_write | http_response | log

class TaintPath(BaseModel):
    source:     TaintSource
    sink:       TaintSink
    steps:      list[str] = []   # intermediate variable assignments
    cwe:        str = ""
    severity:   RiskLevel = RiskLevel.HIGH
    description: str = ""

class TaintAnalysisResult(AgentResultBase):
    taint_paths:    list[TaintPath] = []
    sources_found:  int = 0
    sinks_found:    int = 0
    has_injection:  bool = False
    has_path_traversal: bool = False
    has_ssrf:       bool = False


class IaCFinding(BaseModel):
    file_path:  str
    line:       int
    resource:   str      # e.g. "aws_security_group.web", "Deployment/payments"
    kind:       str      # open_ingress | privileged | root_container | missing_encryption | public_bucket | wildcard_iam
    severity:   RiskLevel
    description: str
    cis_ref:    str = "" # CIS benchmark reference
    fix:        str = ""

class IaCAnalysisResult(AgentResultBase):
    findings:          list[IaCFinding] = []
    terraform_issues:  int = 0
    kubernetes_issues: int = 0
    docker_issues:     int = 0
    overall_severity:  RiskLevel = RiskLevel.LOW


class FileChangePattern(BaseModel):
    file_path:      str
    change_count:   int     # times changed in last 30 days
    last_changed:   str     # ISO date
    avg_risk_score: float
    incident_correlated: bool = False  # changed before known incidents

class TemporalRiskResult(AgentResultBase):
    hot_files:          list[FileChangePattern] = []
    escalating_pattern: bool = False    # risk increasing over time
    security_erosion:   bool = False    # security controls being removed incrementally
    change_fatigue:     list[str] = []  # files changed too frequently
    risk_trend:         str = "stable"  # improving | stable | degrading | critical
    window_days:        int = 30


class SchemaChange(BaseModel):
    file_path:    str
    change_type:  str        # add_column | drop_column | add_table | drop_table | alter_column | add_index | drop_index | rename
    table_name:   str        = ""
    column_name:  str        = ""
    severity:     RiskLevel  = RiskLevel.MEDIUM
    reversible:   bool       = True
    description:  str        = ""
    rollback_sql: str        = ""

class SchemaChangeResult(AgentResultBase):
    changes:           list[SchemaChange] = []
    has_destructive:   bool      = False   # DROP TABLE / DROP COLUMN
    has_irreversible:  bool      = False   # changes with no clean rollback
    migration_files:   list[str] = []
    rollback_risk:     RiskLevel = RiskLevel.LOW
    gate_contribution: str       = "HOLD"
    summary:           str       = ""


class QAScenarioType(str, Enum):
    FUNCTIONAL   = "functional"
    SECURITY     = "security"
    REGRESSION   = "regression"
    EDGE_CASE    = "edge_case"
    INTEGRATION  = "integration"
    PERFORMANCE  = "performance"
    API          = "api"
    DATA         = "data"


class QAScenario(BaseModel):
    id:               str                       # e.g. "QA-001"
    title:            str
    type:             QAScenarioType
    priority:         RiskLevel
    description:      str
    steps:            list[str]                 = []
    expected_result:  str                       = ""
    affected_files:   list[str]                 = []
    automation_hint:  str                       = ""


class QAScenariosResult(AgentResultBase):
    scenarios:       list[QAScenario]  = []
    total_scenarios: int               = 0
    critical_count:  int               = 0
    high_count:      int               = 0
    coverage_areas:  list[str]         = []
    summary:         str               = ""


class SymbolReference(BaseModel):
    """A call-site or usage of a changed symbol found in the codebase."""
    symbol:    str            # name of the changed function/class/method
    file_path: str            # file containing the reference
    line:      int  = 0
    context:   str  = ""      # surrounding line of code (snippet)
    repo:      str  = ""      # empty = same repo; populated for cross-repo hits
    depth:     int  = 1       # 1 = direct caller, 2 = caller of caller, …
    from_file: str  = ""     # for depth≥2: the L(d-1) file whose symbol was searched


class ReferenceImpactResult(AgentResultBase):
    """Intra- and cross-project reference impact for changed symbols."""
    changed_symbols:    list[str]             = []
    references:         list[SymbolReference] = []
    total_references:   int                   = 0
    high_impact_files:  list[str]             = []  # files with ≥3 references
    shared_lib_breaks:  list[str]             = []  # shared/common paths changed
    intra_project_risk: RiskLevel             = RiskLevel.LOW
    search_backend:     str                   = ""  # "local_grep" | "github_api" | "none"
    summary:            str                   = ""


# ── Performance Impact ────────────────────────────────────────────────────────

class PerformanceFinding(BaseModel):
    category:    str  = ""   # n_plus_1 | select_star | nested_loop | sync_in_async | no_pagination | memory_risk | other
    severity:    str  = "low"
    description: str  = ""
    file_path:   str  = ""
    line:        int  = 0
    suggestion:  str  = ""

class PerformanceImpactResult(AgentResultBase):
    findings:                  list[PerformanceFinding] = []
    has_db_risk:               bool = False
    has_complexity_regression: bool = False
    overall_severity:          str  = "low"
    summary:                   str  = ""


# ── Data Privacy ──────────────────────────────────────────────────────────────

class PIIFinding(BaseModel):
    pii_type:     str  = ""   # email | phone | ssn | credit_card | name | dob | address | ip | passport | generic_pii
    description:  str  = ""
    file_path:    str  = ""
    line:         int  = 0
    context:      str  = ""
    is_logged:    bool = False
    is_encrypted: bool = False
    risk_level:   str  = "medium"

class DataPrivacyResult(AgentResultBase):
    pii_findings:        list[PIIFinding] = []
    logging_violations:  list[str]        = []
    gdpr_risk:           str  = "low"
    data_exposure_risk:  str  = "low"
    unencrypted_pii_count: int = 0
    summary:             str  = ""


# ── Maintainability ───────────────────────────────────────────────────────────

class MaintainabilityIssue(BaseModel):
    kind:        str  = ""   # long_function | deep_nesting | bare_except | swallowed_exception | magic_number | dead_code | missing_type_hint | todo_left_in | other
    severity:    str  = "low"
    description: str  = ""
    file_path:   str  = ""
    line:        int  = 0
    suggestion:  str  = ""

class MaintainabilityResult(AgentResultBase):
    issues:               list[MaintainabilityIssue] = []
    maintainability_score: int = 100
    long_function_count:  int  = 0
    error_handling_gaps:  int  = 0
    overall_severity:     str  = "low"
    summary:              str  = ""


# ── License Compliance ────────────────────────────────────────────────────────

class LicenseFinding(BaseModel):
    package:          str = ""
    detected_license: str = "unknown"
    risk_level:       str = "low"
    description:      str = ""
    file_path:        str = ""

class LicenseComplianceResult(AgentResultBase):
    findings:             list[LicenseFinding] = []
    has_copyleft:         bool = False
    has_license_conflict: bool = False
    packages_reviewed:    int  = 0
    overall_risk:         str  = "low"
    summary:              str  = ""


# ── Observability ─────────────────────────────────────────────────────────────

class ObservabilityFinding(BaseModel):
    kind:        str  = ""   # log_removed | unobserved_branch | unobserved_error | metric_removed | missing_tracing | missing_correlation_id
    severity:    str  = "low"
    description: str  = ""
    file_path:   str  = ""
    line:        int  = 0
    suggestion:  str  = ""

class ObservabilityResult(AgentResultBase):
    findings:             list[ObservabilityFinding] = []
    logs_removed:         int = 0
    metrics_removed:      int = 0
    unobserved_branches:  int = 0
    overall_severity:     str = "low"
    summary:              str = ""


class AgentTokenUsage(BaseModel):
    agent:       AgentName
    tokens_used: int
    model:       str
    duration_s:  float = 0.0


class AnalysisReport(BaseModel):
    request_id:   str
    change_type:  ChangeType
    repo_url:     str
    source_ref:   str
    target_ref:   str
    pr:           PRMetadata = Field(default_factory=PRMetadata)
    phase_run:    int = 1
    completed_at: datetime = Field(default_factory=datetime.utcnow)
    duration_s:   float = 0.0

    # Phase 1
    code_analysis: CodeAnalysisResult | None = None
    security:      SecurityResult     | None = None

    # Phase 2
    dependency:    DependencyResult   | None = None
    test_coverage: TestCoverageResult | None = None
    interface:     InterfaceResult    | None = None

    # Advanced detection agents
    ast_analysis:     ASTAnalysisResult     | None = None
    secrets_entropy:  SecretsEntropyResult  | None = None
    taint_analysis:   TaintAnalysisResult   | None = None
    iac_analysis:     IaCAnalysisResult     | None = None
    temporal_risk:    TemporalRiskResult    | None = None
    schema_change:    SchemaChangeResult    | None = None
    qa_scenarios:     QAScenariosResult     | None = None
    reference_impact:   ReferenceImpactResult   | None = None
    performance_impact: PerformanceImpactResult | None = None
    data_privacy:       DataPrivacyResult       | None = None
    maintainability:    MaintainabilityResult   | None = None
    license_compliance: LicenseComplianceResult | None = None
    observability:      ObservabilityResult     | None = None

    # Phase 3
    risk:          RiskResult         | None = None
    remediation:   RemediationResult  | None = None

    token_budget:  int = 0
    token_usage:   list[AgentTokenUsage] = []
    errors:        list[str] = []

    # Stored gate/risk — set by orchestrator at finalization so the values
    # are stable in the stored report and don't require re-deriving at read time
    _gate_decision: GateDecision | None = None
    _final_risk:    RiskLevel    | None = None

    @property
    def total_tokens(self) -> int:
        return sum(t.tokens_used for t in self.token_usage)

    @property
    def gate_decision(self) -> GateDecision:
        if self._gate_decision is not None:
            return self._gate_decision
        if self.risk:
            return self.risk.gate_decision
        if self.security and self.security.overall_severity == RiskLevel.CRITICAL:
            return GateDecision.BLOCK
        return GateDecision.HOLD

    @property
    def final_risk(self) -> RiskLevel:
        if self._final_risk is not None:
            return self._final_risk
        return self.risk.overall_risk if self.risk else RiskLevel.LOW

    def freeze_gate(self) -> None:
        """Snapshot the computed gate/risk into stored fields so they persist."""
        object.__setattr__(self, "_gate_decision", self.gate_decision)
        object.__setattr__(self, "_final_risk",    self.final_risk)

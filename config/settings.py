"""
config/settings.py
------------------
Single source of truth for all configuration.
Loaded from environment variables / .env file via pydantic-settings.
Use get_settings() everywhere — it is cached after first call.
"""
from __future__ import annotations
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Anthropic ──────────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # ── LLM Provider ─────────────────────────────────────────────────────────
    # Override the default Anthropic provider with any supported provider
    llm_provider:     str = Field(default="anthropic",           alias="LLM_PROVIDER")
    llm_model:        str = Field(default="claude-sonnet-4-6",   alias="LLM_MODEL")
    llm_base_url:     str = Field(default="",                    alias="LLM_BASE_URL")
    llm_api_version:  str = Field(default="2024-08-01-preview",  alias="LLM_API_VERSION")

    # OpenAI
    openai_api_key:   str = Field(default="", alias="OPENAI_API_KEY")

    # Ollama (local)
    ollama_url:       str = Field(default="http://localhost:11434", alias="OLLAMA_URL")

    # ── Analysis ──────────────────────────────────────────────────────────────
    analysis_phase: int = Field(default=2, alias="ANALYSIS_PHASE")

    # Token budgets (input+output combined, per agent per run)
    budget_code_analysis:  int = Field(default=4000,  alias="BUDGET_CODE_ANALYSIS")
    budget_security:       int = Field(default=5000,  alias="BUDGET_SECURITY")
    budget_dependency:     int = Field(default=2000,  alias="BUDGET_DEPENDENCY")
    budget_test_coverage:  int = Field(default=3000,  alias="BUDGET_TEST_COVERAGE")
    budget_interface:      int = Field(default=4000,  alias="BUDGET_INTERFACE")
    budget_risk:           int = Field(default=3000,  alias="BUDGET_RISK")
    budget_remediation:    int = Field(default=4000,  alias="BUDGET_REMEDIATION")
    budget_reserve:            int = Field(default=3000,  alias="BUDGET_RESERVE")
    budget_performance_impact: int = Field(default=3000,  alias="BUDGET_PERFORMANCE_IMPACT")
    budget_data_privacy:       int = Field(default=3000,  alias="BUDGET_DATA_PRIVACY")
    budget_maintainability:    int = Field(default=2000,  alias="BUDGET_MAINTAINABILITY")
    budget_license_compliance: int = Field(default=0,     alias="BUDGET_LICENSE_COMPLIANCE")  # static-only
    budget_observability:      int = Field(default=2000,  alias="BUDGET_OBSERVABILITY")

    # ── Git providers ─────────────────────────────────────────────────────────
    git_provider:              str = Field(default="github", alias="GIT_PROVIDER")

    bitbucket_api_url:         str = Field(default="https://api.bitbucket.org/2.0", alias="BITBUCKET_API_URL")
    bitbucket_token:           str = Field(default="", alias="BITBUCKET_TOKEN")
    bitbucket_workspace:       str = Field(default="", alias="BITBUCKET_WORKSPACE")
    bitbucket_webhook_secret:  str = Field(default="", alias="BITBUCKET_WEBHOOK_SECRET")

    github_api_url:            str = Field(default="https://api.github.com", alias="GITHUB_API_URL")
    github_token:              str = Field(default="", alias="GITHUB_TOKEN")
    github_webhook_secret:     str = Field(default="", alias="GITHUB_WEBHOOK_SECRET")

    # ── Storage ───────────────────────────────────────────────────────────────
    chroma_host: str = Field(default="localhost", alias="CHROMA_HOST")
    chroma_port: int = Field(default=8000,        alias="CHROMA_PORT")

    redis_url:   str = Field(default="",          alias="REDIS_URL")

    neo4j_url:   str = Field(default="",          alias="NEO4J_URL")
    neo4j_user:  str = Field(default="neo4j",     alias="NEO4J_USER")
    neo4j_pass:  str = Field(default="",          alias="NEO4J_PASSWORD")

    # ── API auth ──────────────────────────────────────────────────────────────
    # api_keys accepts plain strings (default role: developer) OR rich objects:
    #   {"key": "abc", "roles": ["reviewer"], "name": "Alice", "team": "Payments"}
    # For many users prefer API_KEYS_FILE pointing to a JSON file — easier to
    # manage, diff in git, and reload without restarting the server.
    # Roles: admin | analyst | reviewer | developer | auditor | ci_system
    api_keys:       list      = Field(default_factory=list, alias="API_KEYS")
    api_keys_file:  str       = Field(default="",           alias="API_KEYS_FILE")
    skip_auth:      bool      = Field(default=False,        alias="SKIP_AUTH")

    # ── Output integrations ────────────────────────────────────────────────────
    slack_webhook_url:   str = Field(default="", alias="SLACK_WEBHOOK_URL")
    teams_webhook_url:   str = Field(default="", alias="TEAMS_WEBHOOK_URL")
    post_pr_comments:    bool = Field(default=True,  alias="POST_PR_COMMENTS")

    # ── Email digest (SMTP) ─────────────────────────────────────────────────────
    smtp_host:        str = Field(default="", alias="SMTP_HOST")
    smtp_port:        int = Field(default=587, alias="SMTP_PORT")
    smtp_user:        str = Field(default="", alias="SMTP_USER")
    smtp_password:    str = Field(default="", alias="SMTP_PASSWORD")
    smtp_use_tls:     bool = Field(default=True, alias="SMTP_USE_TLS")
    smtp_from:        str = Field(default="", alias="SMTP_FROM")          # sender address
    digest_recipients: str = Field(default="", alias="DIGEST_RECIPIENTS") # comma-separated
    digest_enabled:   bool = Field(default=False, alias="DIGEST_ENABLED")
    digest_send_hour: int  = Field(default=8, alias="DIGEST_SEND_HOUR")    # 0-23 UTC

    # ── Phase 4: Ticket creation ───────────────────────────────────────────────
    jira_url:         str = Field(default="", alias="JIRA_URL")
    jira_user:        str = Field(default="", alias="JIRA_USER")
    jira_token:       str = Field(default="", alias="JIRA_TOKEN")
    jira_project_key: str = Field(default="IMPACT", alias="JIRA_PROJECT_KEY")

    snow_url:      str = Field(default="", alias="SNOW_URL")
    snow_user:     str = Field(default="", alias="SNOW_USER")
    snow_password: str = Field(default="", alias="SNOW_PASSWORD")

    # ── Phase 4: Observability ─────────────────────────────────────────────────
    otlp_endpoint: str = Field(default="", alias="OTLP_ENDPOINT")

    # ── Governance ────────────────────────────────────────────────────────────
    audit_log_path:         str = Field(default="logs/audit.jsonl", alias="AUDIT_LOG_PATH")
    compliance_frameworks:  list[str] = Field(
        default_factory=lambda: ["MAS TRM", "PCI-DSS 4.0", "OWASP ASVS L2"],
        alias="COMPLIANCE_FRAMEWORKS",
    )

    # ── Deterministic gate policy thresholds ───────────────────────────────────
    gate_coverage_hold_pct:  float = Field(default=-5.0,  alias="GATE_COVERAGE_HOLD_PCT")   # ≤ this → HOLD
    gate_coverage_block_pct: float = Field(default=-15.0, alias="GATE_COVERAGE_BLOCK_PCT")  # ≤ this → BLOCK
    gate_blast_radius_block: int   = Field(default=70,    alias="GATE_BLAST_RADIUS_BLOCK")  # > this → HOLD
    capability_map_path:     str   = Field(default="config/capability_map.json", alias="CAPABILITY_MAP_PATH")
    feedback_db_path:        str   = Field(default="data/feedback.db", alias="FEEDBACK_DB_PATH")

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level:        str  = Field(default="INFO",  alias="LOG_LEVEL")
    log_format:       str  = Field(default="text",  alias="LOG_FORMAT")   # "text" | "json"

    # ── API security ──────────────────────────────────────────────────────────
    cors_origins:     list[str] = Field(
        default_factory=lambda: ["*"],
        alias="CORS_ORIGINS",
    )
    max_diff_bytes:   int  = Field(default=5_000_000, alias="MAX_DIFF_BYTES")   # 5 MB

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_rpm:   int  = Field(default=60, alias="RATE_LIMIT_RPM")   # requests per minute per key

    # ── Analysis reliability ──────────────────────────────────────────────────
    analysis_timeout_s: int = Field(default=600, alias="ANALYSIS_TIMEOUT_S")   # 10 min hard cap (20 agents, potential 529 retries)
    # Pipeline engine. The threaded pipeline runs the ~20 agents in parallel
    # (ThreadPoolExecutor) and is the fast default. LangGraph runs fan-out nodes
    # sequentially, which is much slower — opt in only if you need its tracing.
    use_langgraph: bool = Field(default=False, alias="USE_LANGGRAPH")

    # ── Admission control (concurrency cap + bounded queue) ───────────────────
    # Caps how many analyses run at once and how many may wait. Protects the
    # backend + LLM provider when many users submit together. Defaults are high
    # so single-user / light load behaves exactly as before. Only affects
    # SCHEDULING — never how an individual analysis runs (results are identical).
    max_concurrent_analyses: int = Field(default=8,  alias="MAX_CONCURRENT_ANALYSES")
    max_queued_analyses:     int = Field(default=50, alias="MAX_QUEUED_ANALYSES")

    # ── Deep scan (full coverage for large PRs) ───────────────────────────────
    # When a request opts into deep_scan, the per-file agents (security, code)
    # run over ALL changed files in batches instead of a prioritised sample, so
    # nothing is omitted. Costs more tokens/time — opt-in per analysis.
    deep_scan_batch_chars: int = Field(default=12000, alias="DEEP_SCAN_BATCH_CHARS")
    deep_scan_max_batches: int = Field(default=10,    alias="DEEP_SCAN_MAX_BATCHES")
    deep_scan_min_files:   int = Field(default=8,     alias="DEEP_SCAN_MIN_FILES")

    # ── LLM retry (tenacity) ──────────────────────────────────────────────────
    llm_retry_attempts:    int = Field(default=5,  alias="LLM_RETRY_ATTEMPTS")
    llm_retry_max_wait_s:  int = Field(default=90, alias="LLM_RETRY_MAX_WAIT_S")
    # Per-call request timeout (seconds). A stuck/unreachable model endpoint
    # fails fast instead of hanging and blowing the overall analysis timeout.
    # Kept well under analysis_timeout_s so the worst-case across the agent DAG
    # (a few sequential layers) still fits inside the overall budget.
    llm_request_timeout_s: int = Field(default=45, alias="LLM_REQUEST_TIMEOUT_S")
    # Sampling temperature. 0.0 = deterministic — the same diff yields the same
    # findings and gate on every run, essential for reviewer trust and
    # reproducible audits. Raise only if you deliberately want varied output.
    llm_temperature: float = Field(default=0.0, alias="LLM_TEMPERATURE")

    # ── Webhook deduplication ─────────────────────────────────────────────────
    webhook_dedup_ttl_s: int = Field(default=300, alias="WEBHOOK_DEDUP_TTL_S")

    # ── Quality alerting ──────────────────────────────────────────────────────
    # Set > 0.0 to emit a WARNING log (and optional Slack alert) when recall drops below threshold
    quality_recall_alert_threshold: float = Field(default=0.0, alias="QUALITY_RECALL_ALERT_THRESHOLD")

    # ── Reference impact & codebase search ───────────────────────────────────
    repo_local_path:  str  = Field(default="", alias="REPO_LOCAL_PATH")
    # Path to a local clone of the repo being analysed.
    # When set, the ReferenceImpactAgent uses ripgrep/grep to find all
    # call-sites of changed symbols across the full codebase.

    github_token:     str  = Field(default="", alias="GITHUB_TOKEN")
    # GitHub PAT (fine-grained, read:code scope).
    # Used as fallback when REPO_LOCAL_PATH is not set and the repo is on GitHub.

    # ── Service dependency graph ──────────────────────────────────────────────
    service_map_path: str  = Field(default="", alias="SERVICE_MAP_PATH")
    # Path to a JSON file {"service-a": ["lib-x", "lib-y"], ...}
    # Loaded at startup; enables transitive blast-radius calculation.

    repos_root:       str  = Field(default="", alias="REPOS_ROOT")
    # Path to a directory containing clones of all microservice repos.
    # Scanned automatically to build the service graph when SERVICE_MAP_PATH
    # is not set.

    # ── CVE lookup ────────────────────────────────────────────────────────────
    osv_enabled:      bool = Field(default=True, alias="OSV_ENABLED")

    # ── Reference graph depth ─────────────────────────────────────────────────
    ref_max_depth:    int  = Field(default=2, alias="REF_MAX_DEPTH")
    # 1 = direct callers only (fast)
    # 2 = callers-of-callers (default; requires REPO_LOCAL_PATH for level 2)
    # 3 = one more level (slow on large repos; use with care)
    # Query OSV.dev for known vulnerabilities in changed packages.
    # Disable if the analyser runs in an air-gapped environment.

    # ── Storage ───────────────────────────────────────────────────────────────
    sqlite_path:      str  = Field(default="", alias="SQLITE_PATH")
    # When empty, make_report_store uses data/reports.db as the default.

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def agent_budgets(self) -> dict[str, int]:
        return {
            "code_analysis": self.budget_code_analysis,
            "security":      self.budget_security,
            "dependency":    self.budget_dependency,
            "test_coverage": self.budget_test_coverage,
            "interface":     self.budget_interface,
            "risk":          self.budget_risk,
            "remediation":        self.budget_remediation,
            "_reserve":           self.budget_reserve,
            "performance_impact": self.budget_performance_impact,
            "data_privacy":       self.budget_data_privacy,
            "maintainability":    self.budget_maintainability,
            "license_compliance": self.budget_license_compliance,
            "observability":      self.budget_observability,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

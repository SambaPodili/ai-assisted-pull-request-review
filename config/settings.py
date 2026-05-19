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
    budget_reserve:        int = Field(default=3000,  alias="BUDGET_RESERVE")

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
    api_keys:       list[str] = Field(default_factory=list, alias="API_KEYS")
    skip_auth:      bool      = Field(default=False, alias="SKIP_AUTH")

    # ── Output integrations ────────────────────────────────────────────────────
    slack_webhook_url:   str = Field(default="", alias="SLACK_WEBHOOK_URL")
    teams_webhook_url:   str = Field(default="", alias="TEAMS_WEBHOOK_URL")
    post_pr_comments:    bool = Field(default=True,  alias="POST_PR_COMMENTS")

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

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level:        str  = Field(default="INFO",  alias="LOG_LEVEL")
    log_format:       str  = Field(default="text",  alias="LOG_FORMAT")   # "text" | "json"

    # ── API security ──────────────────────────────────────────────────────────
    cors_origins:     list[str] = Field(
        default_factory=lambda: ["*"],
        alias="CORS_ORIGINS",
    )
    max_diff_bytes:   int  = Field(default=1_000_000, alias="MAX_DIFF_BYTES")   # 1 MB

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_rpm:   int  = Field(default=60, alias="RATE_LIMIT_RPM")   # requests per minute per key

    # ── Analysis reliability ──────────────────────────────────────────────────
    analysis_timeout_s: int = Field(default=300, alias="ANALYSIS_TIMEOUT_S")   # 5 min hard cap

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
            "remediation":   self.budget_remediation,
            "_reserve":      self.budget_reserve,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

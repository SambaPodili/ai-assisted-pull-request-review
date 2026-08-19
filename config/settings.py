"""
config/settings.py
------------------
Single source of truth for all configuration.
Loaded from environment variables / .env file via pydantic-settings.
Use get_settings() everywhere — it is cached after first call.
"""
from __future__ import annotations
import json
from functools import lru_cache
from typing import Annotated
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, NoDecode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
        # Accept both the Python field name and the UPPER_CASE env alias when
        # constructing Settings(...) directly. Without this, Settings(cors_origins=[…])
        # is silently ignored (only CORS_ORIGINS=… works) — a config footgun.
        populate_by_name=True,
    )

    # List-valued settings come from env vars as raw strings. pydantic-settings
    # would otherwise json.loads() them and crash on a plain value (e.g.
    # CORS_ORIGINS=* or API_KEYS=mykey) with "JSONDecodeError: Expecting value".
    # NoDecode (on the fields) skips that, and this validator accepts all of:
    #   JSON  ->  ["a","b"]  /  [{"key":"..."}]
    #   CSV   ->  a, b, c
    #   bare  ->  mykey   (single element)
    @field_validator("api_keys", "compliance_frameworks", "cors_origins", mode="before")
    @classmethod
    def _coerce_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s[0] in "[{":
                try:
                    return json.loads(s)
                except (ValueError, TypeError):
                    pass   # not JSON after all — fall through to CSV split
            return [part.strip() for part in s.split(",") if part.strip()]
        return v

    # ── Anthropic ──────────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # ── LLM Provider ─────────────────────────────────────────────────────────
    # Override the default Anthropic provider with any supported provider
    llm_provider:     str = Field(default="anthropic",           alias="LLM_PROVIDER")
    llm_model:        str = Field(default="claude-sonnet-4-6",   alias="LLM_MODEL")
    llm_base_url:     str = Field(default="",                    alias="LLM_BASE_URL")
    llm_api_version:  str = Field(default="2024-08-01-preview",  alias="LLM_API_VERSION")
    # Shared key for a self-hosted / custom OpenAI-compatible endpoint (e.g. one
    # gateway serving several models like Llama AND Qwen on the same URL + key —
    # only the model name differs). Sent as an Authorization: Bearer header, NEVER
    # in the URL. Lets the UI omit the key entirely (prefilled from this env).
    llm_api_key:      str = Field(default="",                    alias="LLM_API_KEY")

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
    budget_functional_validation: int = Field(default=3500, alias="BUDGET_FUNCTIONAL_VALIDATION")
    # QA generates many rich scenarios (up to 4000 output tokens) and its prompt can
    # include uploaded functional docs, so it needs more headroom than the shared
    # _reserve pool. Without a dedicated slot it fell back every run. Raise
    # BUDGET_QA_SCENARIOS if you upload large requirement docs.
    budget_qa_scenarios:       int = Field(default=6000, alias="BUDGET_QA_SCENARIOS")
    # These agents are primarily LLM-driven (their core output IS the model's),
    # so — like QA — they need their own slot instead of the shared _reserve, or
    # they fall back every run. (ast/taint/secrets/iac/temporal stay on _reserve:
    # they have a deterministic core and only use the LLM opportunistically.)
    budget_schema_change:      int = Field(default=4000, alias="BUDGET_SCHEMA_CHANGE")
    budget_cross_repo_impact:  int = Field(default=5000, alias="BUDGET_CROSS_REPO_IMPACT")
    budget_reference_impact:   int = Field(default=5000, alias="BUDGET_REFERENCE_IMPACT")

    # ── GenAI usage telemetry → ELK (developer portal) ────────────────────────
    # Emits per-run lifecycle documents (started, analysis success, security
    # review, gate, report) to an Elasticsearch index. Off by default. user_id =
    # repo slug; app_code = last 3 chars of the project key; domain = logged
    # user's domain (from an SSO/gateway header, fallback ELK_DEFAULT_DOMAIN).
    elk_usage_enabled:    bool  = Field(default=False, alias="ELK_USAGE_ENABLED")
    elk_usage_url:        str   = Field(default="https://developerportal.com/elasticp/genai_usage/_doc/", alias="ELK_USAGE_URL")
    elk_tool_id:          str   = Field(default="G040", alias="ELK_TOOL_ID")
    elk_tool_name:        str   = Field(default="GTO Pull Request Review Framework", alias="ELK_TOOL_NAME")
    elk_tool_version:     str   = Field(default="1.0.0", alias="ELK_TOOL_VERSION")
    elk_app_code_default: str   = Field(default="CLR", alias="ELK_APP_CODE_DEFAULT")
    elk_integration_id:   str   = Field(default="ownpccoelkint", alias="ELK_INTEGRATION_ID")
    elk_environment:      str   = Field(default="SIT", alias="ELK_ENVIRONMENT")
    elk_default_domain:   str   = Field(default="", alias="ELK_DEFAULT_DOMAIN")
    elk_auth_header:      str   = Field(default="", alias="ELK_AUTH_HEADER")   # e.g. "ApiKey xxx" / "Bearer xxx"
    # Content negotiation. Plain application/json works against a _doc/ POST; set
    # these to "application/vnd.elasticsearch+json; compatible-with=8" if your
    # cluster (ES8 compatibility mode) requires the vendor media type.
    elk_accept:           str   = Field(default="application/json", alias="ELK_ACCEPT")
    elk_content_type:     str   = Field(default="application/json", alias="ELK_CONTENT_TYPE")
    elk_user_header:      str   = Field(default="X-User-Id", alias="ELK_USER_HEADER")
    elk_domain_header:    str   = Field(default="X-User-Domain", alias="ELK_DOMAIN_HEADER")
    elk_verify_ssl:       bool  = Field(default=True, alias="ELK_VERIFY_SSL")
    elk_timeout_s:        float = Field(default=5.0, alias="ELK_TIMEOUT_S")

    # ── Git providers ─────────────────────────────────────────────────────────
    git_provider:              str = Field(default="github", alias="GIT_PROVIDER")
    # Disable TLS cert verification for git clone/fetch ONLY. For corporate
    # Bitbucket/GitHub Enterprise behind a self-signed or internal-CA cert that
    # git cannot verify (clone fails rc=128 "SSL certificate problem"). INSECURE:
    # prefer trusting the corporate CA. Off by default; opt in per deployment.
    git_ssl_no_verify:         bool = Field(default=False, alias="GIT_SSL_NO_VERIFY")

    # SCA / dependency TLS + endpoints. Declared so .env values are honoured (the
    # SSL helpers read these via the Settings object, not just os.getenv — a value
    # in .env is NOT pushed to os.environ by pydantic-settings).
    osv_verify_ssl:            bool = Field(default=True,  alias="OSV_VERIFY_SSL")    # false → INSECURE skip
    osv_ca_bundle:             str  = Field(default="",    alias="OSV_CA_BUNDLE")     # corporate CA path
    osv_base_url:              str  = Field(default="",    alias="OSV_BASE_URL")      # internal OSV mirror
    osv_proxy_url:             str  = Field(default="",    alias="OSV_PROXY_URL")     # corporate forward proxy for OSV only
    # Directory of pre-downloaded OSV snapshot zips (Maven.zip / NuGet.zip …) —
    # the LAST-RESORT lookup when every live source is down (air-gapped).
    osv_offline_dir:           str  = Field(default="",    alias="OSV_OFFLINE_DIR")
    maven_repo_url:            str  = Field(default="",    alias="MAVEN_REPO_URL")
    maven_repo_auth:           str  = Field(default="",    alias="MAVEN_REPO_AUTH")
    maven_scan_transitive:     bool = Field(default=True,  alias="MAVEN_SCAN_TRANSITIVE")
    # Vulnerability source for SCA scans: "osv" (public DB) or "xray" (JFrog Xray
    # on your Artifactory — fully in-house). UI selection overrides per request.
    vuln_source:               str  = Field(default="osv", alias="VULN_SOURCE")
    # When the primary source is down (after retries), try this one: osv | xray |
    # none. OPT-IN (default none) — e.g. security must approve dependency names
    # leaving the bank to public OSV as a fallback side effect.
    vuln_fallback_source:      str  = Field(default="none", alias="VULN_FALLBACK_SOURCE")
    xray_base_url:             str  = Field(default="",    alias="XRAY_BASE_URL")   # e.g. https://artifactory.uobnet.com/xray
    xray_auth:                 str  = Field(default="",    alias="XRAY_AUTH")       # Bearer <token> (or raw token)

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
    api_keys:       Annotated[list, NoDecode] = Field(default_factory=list, alias="API_KEYS")
    # Defaults to the conventional config/keys.json so editing that file "just
    # works". Point it at an ABSOLUTE path outside the code for production so a
    # re-deploy doesn't overwrite your keys.
    api_keys_file:  str       = Field(default="config/keys.json", alias="API_KEYS_FILE")
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
    audit_log_path:         str = Field(default="", alias="AUDIT_LOG_PATH")   # → <DATA_DIR>/audit.jsonl
    compliance_frameworks:  Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["MAS TRM", "PCI-DSS 4.0", "OWASP ASVS L2"],
        alias="COMPLIANCE_FRAMEWORKS",
    )

    # ── Deterministic gate policy thresholds ───────────────────────────────────
    gate_coverage_hold_pct:  float = Field(default=-5.0,  alias="GATE_COVERAGE_HOLD_PCT")   # ≤ this → HOLD
    gate_coverage_block_pct: float = Field(default=-15.0, alias="GATE_COVERAGE_BLOCK_PCT")  # ≤ this → BLOCK
    gate_blast_radius_block: int   = Field(default=70,    alias="GATE_BLAST_RADIUS_BLOCK")  # > this → HOLD
    # When true, the "high-severity security finding" HOLD only fires for
    # CONFIRMED findings — location verified AND high confidence (deterministic
    # rule or corroborated by ≥2 agents per the correlation engine). Single-source
    # uncorroborated LLM highs still appear in the report but don't hold the merge.
    # Default OFF: conservative banking posture (any verified high holds).
    gate_require_confirmed_highs: bool = Field(default=False, alias="GATE_REQUIRE_CONFIRMED_HIGHS")
    capability_map_path:     str   = Field(default="config/capability_map.json", alias="CAPABILITY_MAP_PATH")
    feedback_db_path:        str   = Field(default="", alias="FEEDBACK_DB_PATH")   # → <DATA_DIR>/feedback.db

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level:        str  = Field(default="INFO",  alias="LOG_LEVEL")
    log_format:       str  = Field(default="text",  alias="LOG_FORMAT")   # "text" | "json"

    # ── API security ──────────────────────────────────────────────────────────
    cors_origins:     Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"],
        alias="CORS_ORIGINS",
    )
    max_diff_bytes:   int  = Field(default=5_000_000, alias="MAX_DIFF_BYTES")   # 5 MB

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_rpm:   int  = Field(default=240, alias="RATE_LIMIT_RPM")   # requests per minute per key (UI polls a lot)

    # ── Analysis reliability ──────────────────────────────────────────────────
    analysis_timeout_s: int = Field(default=900, alias="ANALYSIS_TIMEOUT_S")   # 15 min hard cap (20 agents; higher for slow self-hosted models)
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
    # Auto-promote to deep-scan when the prioritised single-prompt pass would have
    # to SKIP files (large PR, or a few very large files that blow the token
    # budget) — so code/security LLM review covers every changed file without the
    # user having to tick the box. Off → only an explicit deep_scan request does it.
    deep_scan_auto:        bool = Field(default=True,  alias="DEEP_SCAN_AUTO")

    # ── LLM retry (tenacity) ──────────────────────────────────────────────────
    llm_retry_attempts:    int = Field(default=5,  alias="LLM_RETRY_ATTEMPTS")
    llm_retry_max_wait_s:  int = Field(default=90, alias="LLM_RETRY_MAX_WAIT_S")
    # SEPARATE, smaller retry cap for timeout/connection errors on the OpenAI-
    # compatible path. A timeout won't recover by re-issuing the same heavy
    # request — retrying it 5× just hammers an already-overloaded self-hosted
    # endpoint and makes every other agent time out too. Fail fast (1 = no retry).
    llm_timeout_retry_attempts: int = Field(default=1, alias="LLM_TIMEOUT_RETRY_ATTEMPTS")
    # Per-call request timeout (seconds). With streaming ON (llm_stream, the
    # default for OpenAI-compatible endpoints) this is a PER-CHUNK read timeout —
    # a slow model succeeds as long as it keeps emitting tokens within this
    # window, so it mainly needs to cover prefill / time-to-first-token, not the
    # whole generation. With streaming OFF it must cover the ENTIRE generation,
    # which for the heavy agents (QA scenarios, interface/reference impact) on a
    # self-hosted GPU can be minutes — raise it well up (180–300) in that case.
    llm_request_timeout_s: int = Field(default=120, alias="LLM_REQUEST_TIMEOUT_S")
    # Stream OpenAI-compatible completions so the read timeout applies per chunk
    # instead of to the whole (possibly multi-minute) generation. This is the
    # main defence against APITimeoutError on slow on-prem models. Set false only
    # if a server rejects streaming.
    llm_stream: bool = Field(default=True, alias="LLM_STREAM")
    # Max LLM requests in flight at once across the whole process. The pipeline
    # fans out ~13 agents in parallel; cloud providers (Anthropic/OpenAI) handle
    # that easily, but a self-hosted / custom OpenAI-compatible server (vLLM,
    # LM Studio, an org gateway with per-client connection limits) often refuses
    # or resets connections under that load — surfacing as "failed to connect".
    # Lower this (e.g. 2–4) for a small/local custom endpoint. 0 = unlimited.
    llm_max_concurrency:   int = Field(default=8, alias="LLM_MAX_CONCURRENCY")
    # Reasoning models (Qwen/QwQ/DeepSeek-R1) burn output tokens on a <think> phase
    # before the JSON, so the default 4000-token cap returns an EMPTY answer for
    # the bigger agents. Raise the OUTPUT cap (0 = use each agent's own cap) and
    # multiply the per-agent budgets so reasoning + the JSON answer both fit.
    # Example for a Qwen reasoning endpoint: LLM_MAX_OUTPUT_TOKENS=8000, LLM_BUDGET_MULTIPLIER=2.5
    llm_max_output_tokens: int   = Field(default=0,   alias="LLM_MAX_OUTPUT_TOKENS")
    llm_budget_multiplier: float = Field(default=1.0, alias="LLM_BUDGET_MULTIPLIER")
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
    # ONE base dir for everything the app writes (reports/users/feedback/audit…).
    # Set DATA_DIR to an ABSOLUTE path OUTSIDE the code tree so re-deploying the
    # code never overwrites runtime state. Per-store overrides below still win.
    data_dir:         str  = Field(default="data", alias="DATA_DIR")
    sqlite_path:      str  = Field(default="", alias="SQLITE_PATH")               # → <DATA_DIR>/reports.db
    review_session_db_path: str = Field(default="", alias="REVIEW_SESSION_DB_PATH")  # → <DATA_DIR>/review_sessions.db
    user_db_path:     str  = Field(default="", alias="USER_DB_PATH")             # → <DATA_DIR>/users.db
    temporal_db_path: str  = Field(default="", alias="TEMPORAL_DB_PATH")         # → <DATA_DIR>/temporal.db

    # ── Derived helpers ───────────────────────────────────────────────────────
    def data_path(self, name: str) -> str:
        """Absolute/relative path under DATA_DIR for a runtime file, ensuring the
        directory exists. All stores default through here so a single DATA_DIR
        keeps every piece of state together and outside the deployable code."""
        import os
        os.makedirs(self.data_dir, exist_ok=True)
        return os.path.join(self.data_dir, name)

    @property
    def agent_budgets(self) -> dict[str, int]:
        base = {
            "code_analysis": self.budget_code_analysis,
            "security":      self.budget_security,
            "dependency":    self.budget_dependency,
            "test_coverage": self.budget_test_coverage,
            "interface":     self.budget_interface,
            "risk":          self.budget_risk,
            "remediation":        self.budget_remediation,
            "qa_scenarios":       self.budget_qa_scenarios,
            "schema_change":      self.budget_schema_change,
            "cross_repo_impact":  self.budget_cross_repo_impact,
            "reference_impact":   self.budget_reference_impact,
            "_reserve":           self.budget_reserve,
            "performance_impact": self.budget_performance_impact,
            "data_privacy":       self.budget_data_privacy,
            "maintainability":    self.budget_maintainability,
            "license_compliance": self.budget_license_compliance,
            "observability":      self.budget_observability,
            "functional_validation": self.budget_functional_validation,
        }
        # Scale every per-agent budget for token-hungry (e.g. reasoning) endpoints.
        mult = getattr(self, "llm_budget_multiplier", 1.0) or 1.0
        if mult and mult != 1.0:
            base = {k: int(v * mult) for k, v in base.items()}
        return base


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

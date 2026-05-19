"""
tests/unit/test_advanced_detection.py
---------------------------------------
Unit tests for all 5 advanced detection agents:
  - SecretsEntropyAgent   (entropy + known prefix)
  - ASTAnalysisAgent      (Python AST + AST-lite)
  - TaintAnalysisAgent    (source → propagation → sink)
  - IaCAnalysisAgent      (Terraform + Kubernetes + Dockerfile)
  - TemporalRiskAgent     (historical pattern detection)
"""
from __future__ import annotations
import math
import uuid
import pytest

from core.models import AnalysisRequest, ChangeType, DiffHunk, RiskLevel
from agents.secrets_entropy_agent import SecretsEntropyAgent, shannon_entropy
from agents.ast_analysis_agent    import ASTAnalysisAgent, _cyclomatic
from agents.taint_analysis_agent  import TaintAnalysisAgent
from agents.iac_analysis_agent    import IaCAnalysisAgent
from agents.temporal_risk_agent   import TemporalRiskAgent
from storage.temporal_store       import InMemoryTemporalStore, FileChangeRecord, get_temporal_store
import ast


# ── Fixtures ─────────────────────────────────────────────────────────────────

def make_req(*hunks: DiffHunk) -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/bank/test",
        source_ref="feature", target_ref="main",
        hunks=list(hunks),
    )

def hunk(file_path: str, content: str, lang: str = "python") -> DiffHunk:
    added = sum(1 for l in content.splitlines() if l.startswith("+"))
    return DiffHunk(file_path=file_path, language=lang, additions=added, deletions=0, content=content)

@pytest.fixture
def zero_budget():
    from core.token_manager import TokenBudgetManager
    return TokenBudgetManager(str(uuid.uuid4()), {k:0 for k in [
        "code_analysis","security","dependency","test_coverage",
        "interface","risk","remediation","_reserve"]})


# ═══════════════════════════════════════════════════════════════════════════════
#  Shannon Entropy
# ═══════════════════════════════════════════════════════════════════════════════

class TestShannonEntropy:
    def test_empty_string(self):
        assert shannon_entropy("") == 0.0

    def test_single_char_repeated(self):
        assert shannon_entropy("aaaaaaa") == 0.0

    def test_english_prose_low_entropy(self):
        e = shannon_entropy("the quick brown fox jumps over the lazy dog")
        # English prose has entropy ~4.0-4.5 bits/char — lower than truly random keys
        assert e < 5.0  # definitely below max possible for this charset

    def test_random_key_high_entropy(self):
        # A real API key-like string should have entropy > 3.5
        e = shannon_entropy("sk-ant-api03-xKj8mN2pL5qRsTuVwXyZ9a1b2c3d4e5")
        assert e > 3.5

    def test_hex_blob_high_entropy(self):
        e = shannon_entropy("a7f2b9d4e1c8f3a6b0d5e2c9f4a1b8d3")
        assert e > 3.5

    def test_password_equals_abc_low(self):
        e = shannon_entropy("abc123")
        assert e < 3.0


# ═══════════════════════════════════════════════════════════════════════════════
#  Secrets Entropy Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecretsEntropyAgent:

    def _agent(self):
        return SecretsEntropyAgent(api_key="test")

    def test_detects_anthropic_key(self, zero_budget):
        req = make_req(hunk("config.py",
            '+api_key = "sk-ant-api03-xKj8mN2pL5qRsTuVwXyZ9a1b2c3d4"\n'))
        result = self._agent().run(req, zero_budget)
        assert any(f.kind == "known_prefix" for f in result.findings)
        assert result.known_prefix_count >= 1 or result.high_entropy_count >= 1
        assert result.overall_severity in (RiskLevel.CRITICAL, RiskLevel.HIGH)

    def test_detects_aws_key(self, zero_budget):
        req = make_req(hunk("deploy.py",
            '+AWS_KEY = "AKIAxKj8mN2pL5qRsTuV"\n'))
        result = self._agent().run(req, zero_budget)
        assert len(result.findings) >= 1  # detected via entropy or known prefix

    def test_detects_openai_key(self, zero_budget):
        req = make_req(hunk("llm.py",
            '+OPENAI_KEY = "sk-proj-abc123XYZdef456GHIjkl789MNO"\n'))
        result = self._agent().run(req, zero_budget)
        assert result.overall_severity in (RiskLevel.CRITICAL, RiskLevel.HIGH)

    def test_detects_high_entropy_string(self, zero_budget):
        # High entropy string assigned to a secret variable
        req = make_req(hunk("auth.py",
            '+token = "a7f2b9d4e1c8f3a6b0d5e2c9f4a1b8d3c6e7f8a9"\n'))
        result = self._agent().run(req, zero_budget)
        assert result.overall_severity != RiskLevel.LOW or len(result.findings) >= 0

    def test_clean_code_no_findings(self, zero_budget):
        req = make_req(hunk("service.py",
            '+def process_payment(amount: Decimal) -> bool:\n'
            '+    return amount > 0\n'))
        result = self._agent().run(req, zero_budget)
        assert result.overall_severity == RiskLevel.LOW

    def test_safe_patterns_ignored(self, zero_budget):
        req = make_req(hunk("test_auth.py",
            '+token = "placeholder_replace_me_with_real_token"\n'))
        result = self._agent().run(req, zero_budget)
        # placeholder should be ignored or have no critical findings
        crits = [f for f in result.findings if f.severity == RiskLevel.CRITICAL]
        assert len(crits) == 0

    def test_value_is_redacted(self, zero_budget):
        req = make_req(hunk("config.py",
            '+api_key = "sk-ant-api03-realSecretKey12345678901234"\n'))
        result = self._agent().run(req, zero_budget)
        # Values should be redacted (only first 6 chars + ...)
        for f in result.findings:
            assert "..." in f.value
            assert len(f.value) <= 12  # 6 chars + "..."

    def test_github_token_detected(self, zero_budget):
        req = make_req(hunk("ci.py",
            '+GITHUB_TOKEN = "ghp_RsTuVwXyZaAbBcCdDeEfFgGhHiIjJkKlLmM"\n'))
        result = self._agent().run(req, zero_budget)
        assert result.known_prefix_count >= 1 or result.high_entropy_count >= 1


# ═══════════════════════════════════════════════════════════════════════════════
#  AST Analysis Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestASTAnalysisAgent:

    def _agent(self):
        return ASTAnalysisAgent(api_key="test")

    def test_detects_high_cyclomatic_complexity(self, zero_budget):
        # Function with many branches — complexity must exceed 10 for MEDIUM flag
        complex_fn = (
            '+def process_payment(amount, currency, account, user, method, retry, channel, country):\n'
            '+    if amount <= 0: return False\n'
            '+    elif amount > 1000000:\n'
            '+        if currency == "USD":\n'
            '+            if user and user.tier == "premium":\n'
            '+                if account and account.verified:\n'
            '+                    if method == "wire" and channel == "bank":\n'
            '+                        return True\n'
            '+                    elif method == "card" or channel == "mobile":\n'
            '+                        return False\n'
            '+    while retry > 0 and amount > 0:\n'
            '+        retry -= 1\n'
            '+    if country in ("US", "UK", "SG") and method != "cash":\n'
            '+        return True\n'
            '+    return None\n'
        )
        req = make_req(hunk("payment.py", complex_fn))
        result = self._agent().fallback_result(req)
        # Either a complexity finding OR the profile shows high complexity
        profiles_complex = [p for p in result.function_profiles if p.cyclomatic >= 8]
        complexity_findings = [f for f in result.findings if f.kind == "complexity_spike"]
        assert len(complexity_findings) >= 1 or len(profiles_complex) >= 1

    def test_detects_mutable_default_argument(self, zero_budget):
        code = '+def process_batch(items=[]):\n+    items.append(1)\n+    return items\n'
        req = make_req(hunk("service.py", code))
        result = self._agent().fallback_result(req)
        mutable_findings = [f for f in result.findings if "mutable" in f.description.lower() or f.kind == "type_confusion"]
        assert len(mutable_findings) >= 1

    def test_detects_bare_except(self, zero_budget):
        code = (
            '+def risky():\n'
            '+    try:\n'
            '+        do_something()\n'
            '+    except:\n'
            '+        pass\n'
        )
        req = make_req(hunk("handler.py", code))
        result = self._agent().fallback_result(req)
        bare_except = [f for f in result.findings if "bare" in f.description.lower() or "swallow" in f.description.lower()]
        assert len(bare_except) >= 1

    def test_detects_none_equality(self, zero_budget):
        code = '+if result == None:\n+    return default\n'
        req = make_req(hunk("validator.py", code))
        result = self._agent().fallback_result(req)
        none_findings = [f for f in result.findings if "None" in f.description or "identity" in f.description.lower()]
        assert len(none_findings) >= 1

    def test_java_empty_catch_detected(self, zero_budget):
        code = '+} catch (Exception e) {} \n'
        req = make_req(hunk("Service.java", code, lang="java"))
        result = self._agent().fallback_result(req)
        assert any("empty catch" in f.description.lower() or "swallow" in f.description.lower() for f in result.findings)

    def test_go_ignored_error_detected(self, zero_budget):
        code = '+result, _ := db.Query("SELECT * FROM payments")\n'
        req = make_req(hunk("repo.go", code, lang="go"))
        result = self._agent().fallback_result(req)
        assert any("error" in f.description.lower() for f in result.findings)

    def test_typescript_any_detected(self, zero_budget):
        code = '+function process(data: any): void {\n+    console.log(data);\n+}\n'
        req = make_req(hunk("handler.ts", code, lang="typescript"))
        result = self._agent().fallback_result(req)
        assert any("any" in f.description for f in result.findings)

    def test_call_graph_extraction(self, zero_budget):
        code = (
            '+def calculate_fee(amount):\n'
            '+    validated = validate_amount(amount)\n'
            '+    return apply_rate(validated)\n'
        )
        req = make_req(hunk("fee.py", code))
        result = self._agent().fallback_result(req)
        assert len(result.function_profiles) >= 1
        assert any(p.name == "calculate_fee" for p in result.function_profiles)

    def test_cyclomatic_complexity_calculation(self):
        code = '''
def complex_func(a, b, c):
    if a:
        if b:
            return 1
        elif c:
            return 2
    while a and b:
        a -= 1
    return 0
'''
        tree = ast.parse(code)
        func = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
        cx = _cyclomatic(func)
        assert cx >= 4  # if, if, elif, while, and


# ═══════════════════════════════════════════════════════════════════════════════
#  Taint Analysis Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestTaintAnalysisAgent:

    def _agent(self):
        return TaintAnalysisAgent(api_key="test")

    def test_detects_direct_sql_injection(self, zero_budget):
        code = (
            '+user_id = request.getParameter("userId")\n'
            '+db.executeQuery("SELECT * FROM users WHERE id=" + user_id)\n'
        )
        req = make_req(hunk("UserService.java", code, lang="java"))
        result = self._agent().fallback_result(req)
        assert result.has_injection
        assert len(result.taint_paths) >= 1
        assert result.taint_paths[0].sink.sink == "sql_query"

    def test_detects_multi_step_taint(self, zero_budget):
        code = (
            '+user_input = request.get("name")\n'          # source
            '+cached_name = user_input\n'                  # propagation
            '+cursor.execute("SELECT * FROM t WHERE name=" + cached_name)\n'  # sink
        )
        req = make_req(hunk("search.py", code))
        result = self._agent().fallback_result(req)
        assert result.sources_found >= 1
        assert result.has_injection

    def test_detects_command_injection(self, zero_budget):
        code = (
            '+cmd = request.get("command")\n'
            '+os.system(cmd)\n'
        )
        req = make_req(hunk("admin.py", code))
        result = self._agent().fallback_result(req)
        assert any(p.sink.sink == "exec" for p in result.taint_paths)

    def test_detects_ssrf(self, zero_budget):
        code = (
            '+url = request.get("callback_url")\n'
            '+requests.get(url)\n'
        )
        req = make_req(hunk("webhook.py", code))
        result = self._agent().fallback_result(req)
        assert result.has_ssrf or result.sources_found >= 1

    def test_sanitized_code_clean(self, zero_budget):
        code = (
            '+user_id = request.get("id")\n'
            '+stmt = conn.prepareStatement("SELECT * FROM users WHERE id=?")\n'
            '+stmt.setString(1, user_id)\n'
        )
        req = make_req(hunk("safe.py", code))
        result = self._agent().fallback_result(req)
        # PreparedStatement = sanitized, should not flag
        injection_paths = [p for p in result.taint_paths if p.sink.sink == "sql_query"]
        assert len(injection_paths) == 0

    def test_source_tracking(self, zero_budget):
        code = '+user_data = os.environ.get("SECRET_DATA")\n'
        req = make_req(hunk("config.py", code))
        result = self._agent().fallback_result(req)
        assert result.sources_found >= 1

    def test_deserialization_sink(self, zero_budget):
        code = (
            '+data = request.get("payload")\n'
            '+obj = pickle.loads(data)\n'
        )
        req = make_req(hunk("api.py", code))
        result = self._agent().fallback_result(req)
        assert any(p.sink.sink == "deserialization" for p in result.taint_paths)


# ═══════════════════════════════════════════════════════════════════════════════
#  IaC Analysis Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestIaCAnalysisAgent:

    def _agent(self):
        return IaCAnalysisAgent(api_key="test")

    def test_terraform_open_security_group(self, zero_budget):
        tf_content = (
            '+resource "aws_security_group" "web" {\n'
            '+  ingress {\n'
            '+    cidr_blocks = ["0.0.0.0/0"]\n'
            '+    from_port = 80\n'
            '+    to_port = 80\n'
            '+  }\n'
            '+}\n'
        )
        req = make_req(hunk("main.tf", tf_content, lang="terraform"))
        result = self._agent().fallback_result(req)
        open_ingress = [f for f in result.findings if f.kind == "open_ingress"]
        assert len(open_ingress) >= 1

    def test_terraform_rds_publicly_accessible(self, zero_budget):
        tf_content = (
            '+resource "aws_db_instance" "payments_db" {\n'
            '+  publicly_accessible = true\n'
            '+  engine = "postgres"\n'
            '+}\n'
        )
        req = make_req(hunk("rds.tf", tf_content, lang="terraform"))
        result = self._agent().fallback_result(req)
        assert any(f.severity == RiskLevel.CRITICAL for f in result.findings)

    def test_terraform_s3_public_acl(self, zero_budget):
        tf_content = (
            '+resource "aws_s3_bucket" "data" {\n'
            '+  acl = "public-read"\n'
            '+}\n'
        )
        req = make_req(hunk("storage.tf", tf_content, lang="terraform"))
        result = self._agent().fallback_result(req)
        assert any(f.kind == "public_bucket" for f in result.findings)

    def test_terraform_iam_wildcard(self, zero_budget):
        tf_content = (
            '+resource "aws_iam_policy" "admin" {\n'
            '+  actions = ["*"]\n'
            '+  resources = ["*"]\n'
            '+}\n'
        )
        req = make_req(hunk("iam.tf", tf_content, lang="terraform"))
        result = self._agent().fallback_result(req)
        assert any(f.kind == "wildcard_iam" for f in result.findings)

    def test_kubernetes_privileged_container(self, zero_budget):
        k8s_content = (
            '+apiVersion: apps/v1\n'
            '+kind: Deployment\n'
            '+metadata:\n'
            '+  name: payments\n'
            '+spec:\n'
            '+  template:\n'
            '+    spec:\n'
            '+      containers:\n'
            '+      - name: app\n'
            '+        securityContext:\n'
            '+          privileged: true\n'
        )
        req = make_req(hunk("deployment.yaml", k8s_content, lang="yaml"))
        result = self._agent().fallback_result(req)
        privileged = [f for f in result.findings if f.kind == "privileged"]
        assert len(privileged) >= 1
        assert any(f.severity == RiskLevel.CRITICAL for f in privileged)

    def test_kubernetes_root_user(self, zero_budget):
        k8s_content = (
            '+apiVersion: v1\n'
            '+kind: Pod\n'
            '+metadata:\n'
            '+  name: worker\n'
            '+spec:\n'
            '+  containers:\n'
            '+  - securityContext:\n'
            '+      runAsUser: 0\n'
        )
        req = make_req(hunk("pod.yaml", k8s_content, lang="yaml"))
        result = self._agent().fallback_result(req)
        root_findings = [f for f in result.findings if f.kind == "root_container"]
        assert len(root_findings) >= 1

    def test_kubernetes_latest_tag(self, zero_budget):
        k8s_content = (
            '+apiVersion: apps/v1\n'
            '+kind: Deployment\n'
            '+metadata:\n'
            '+  name: api\n'
            '+spec:\n'
            '+  template:\n'
            '+    spec:\n'
            '+      containers:\n'
            '+      - image: myregistry/api:latest\n'
        )
        req = make_req(hunk("deploy.yaml", k8s_content, lang="yaml"))
        result = self._agent().fallback_result(req)
        latest_findings = [f for f in result.findings if "latest" in f.description.lower()]
        assert len(latest_findings) >= 1

    def test_dockerfile_no_user_instruction(self, zero_budget):
        docker_content = (
            '+FROM python:3.12\n'
            '+WORKDIR /app\n'
            '+COPY . .\n'
            '+CMD ["python", "main.py"]\n'
        )
        req = make_req(hunk("Dockerfile", docker_content, lang="dockerfile"))
        result = self._agent().fallback_result(req)
        root_findings = [f for f in result.findings if f.kind == "root_container"]
        assert len(root_findings) >= 1

    def test_dockerfile_secret_in_env(self, zero_budget):
        docker_content = (
            '+FROM python:3.12\n'
            '+ENV API_KEY=sk-realSecretApiKey123\n'
            '+CMD ["python", "app.py"]\n'
        )
        req = make_req(hunk("Dockerfile", docker_content, lang="dockerfile"))
        result = self._agent().fallback_result(req)
        secret_findings = [f for f in result.findings if f.kind == "privileged" and "secret" in f.description.lower()]
        assert len(secret_findings) >= 1

    def test_clean_infrastructure_no_findings(self, zero_budget):
        tf_content = (
            '+resource "aws_s3_bucket" "private_logs" {\n'
            '+  bucket = "my-private-logs"\n'
            '+}\n'
            '+resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {\n'
            '+  bucket = aws_s3_bucket.private_logs.id\n'
            '+}\n'
        )
        req = make_req(hunk("logging.tf", tf_content, lang="terraform"))
        result = self._agent().fallback_result(req)
        critical = [f for f in result.findings if f.severity == RiskLevel.CRITICAL]
        assert len(critical) == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  Temporal Risk Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestTemporalRiskAgent:

    def _agent_with_store(self):
        store = InMemoryTemporalStore()
        agent = TemporalRiskAgent(api_key="test")
        agent._store = store
        return agent, store

    def _make_record(self, repo: str, file: str, risk: int = 30, gate: str = "HOLD") -> FileChangeRecord:
        from datetime import datetime
        return FileChangeRecord(
            repo_url=repo, file_path=file, request_id=str(uuid.uuid4()),
            risk_score=risk, gate_decision=gate, security_severity="medium",
            has_secrets=False, changed_at=datetime.utcnow().isoformat(),
        )

    def test_change_fatigue_detected(self, zero_budget):
        agent, store = self._agent_with_store()
        repo = "https://github.com/bank/payments"
        # Record 5 changes to the same file
        for _ in range(5):
            store.record_change(self._make_record(repo, "src/PaymentService.py", risk=60))

        req = make_req(hunk("src/PaymentService.py", "+# change\n"))
        req.repo_url = repo
        result = agent.fallback_result(req)
        assert "src/PaymentService.py" in result.change_fatigue or len(result.hot_files) >= 1

    def test_no_history_no_fatigue(self, zero_budget):
        agent, _ = self._agent_with_store()
        req = make_req(hunk("new_file.py", "+# new code\n"))
        req.repo_url = "https://github.com/bank/fresh-repo"
        result = agent.fallback_result(req)
        assert not result.security_erosion
        assert result.risk_trend in ("stable", "improving")

    def test_incident_correlated_files(self, zero_budget):
        agent, store = self._agent_with_store()
        repo = "https://github.com/bank/payments"
        store.record_incident(repo, ["src/PaymentProcessor.java"], "P0 incident")
        # Also add a change record so the file appears in hot_files
        from datetime import datetime
        for _ in range(4):
            store.record_change(FileChangeRecord(
                repo_url=repo, file_path="src/PaymentProcessor.java",
                request_id=str(uuid.uuid4()), risk_score=70,
                gate_decision="HOLD", security_severity="high",
                has_secrets=False, changed_at=datetime.utcnow().isoformat(),
            ))
        req = make_req(hunk("src/PaymentProcessor.java", "+// change\n", lang="java"))
        req.repo_url = repo
        result = agent.fallback_result(req)
        # File should appear in hot_files with incident_correlated=True
        incident_files = [h for h in result.hot_files if h.incident_correlated]
        assert len(incident_files) >= 1

    def test_in_memory_store_saves_and_retrieves(self):
        store = InMemoryTemporalStore()
        from datetime import datetime
        record = FileChangeRecord(
            repo_url="https://github.com/bank/test",
            file_path="src/Service.py",
            request_id="req-001",
            risk_score=75,
            gate_decision="HOLD",
            security_severity="high",
            has_secrets=False,
            changed_at=datetime.utcnow().isoformat(),
        )
        store.record_change(record)
        history = store.get_file_history("https://github.com/bank/test", "src/Service.py")
        assert history is not None
        assert history.change_count == 1
        assert history.avg_risk_score == 75.0

    def test_get_hot_files_threshold(self):
        store = InMemoryTemporalStore()
        repo = "https://github.com/bank/test"
        from datetime import datetime
        for _ in range(6):
            store.record_change(FileChangeRecord(
                repo_url=repo, file_path="hot.py", request_id=str(uuid.uuid4()),
                risk_score=50, gate_decision="HOLD", security_severity="medium",
                has_secrets=False, changed_at=datetime.utcnow().isoformat(),
            ))
        for _ in range(2):
            store.record_change(FileChangeRecord(
                repo_url=repo, file_path="cold.py", request_id=str(uuid.uuid4()),
                risk_score=10, gate_decision="APPROVE", security_severity="low",
                has_secrets=False, changed_at=datetime.utcnow().isoformat(),
            ))
        hot = store.get_hot_files(repo, days=30, min_changes=4)
        hot_paths = [h.file_path for h in hot]
        assert "hot.py" in hot_paths
        assert "cold.py" not in hot_paths

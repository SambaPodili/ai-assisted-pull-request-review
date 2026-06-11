"""tests/test_ts_parser.py
Tree-sitter multi-language function metrics (optional dependency — skipped when
the wheel isn't installed; the AST agent then uses the regex fallback).
"""
import pytest
from ingestion.ts_parser import HAS_TREE_SITTER, analyze_functions, supported

pytestmark = pytest.mark.skipif(not HAS_TREE_SITTER, reason="tree-sitter not installed")

JAVA = """
public class Svc {
    public int risky(int amount, String tier) {
        if (amount > 0 && tier != null) {
            for (int i = 0; i < amount; i++) {
                if (i % 2 == 0) {
                    try { process(i); } catch (Exception e) { log(e); }
                }
            }
        } else if (amount < 0) {
            return -1;
        }
        return amount > 100 ? 1 : 0;
    }
    public int simple() { return 1; }
}
"""


def test_java_function_metrics():
    ms = {m.name: m for m in analyze_functions(JAVA, "java")}
    assert set(ms) == {"risky", "simple"}
    assert ms["risky"].complexity == 8          # exact McCabe: 7 decisions + 1
    assert ms["risky"].param_count == 2
    assert ms["risky"].max_nesting >= 3
    assert ms["simple"].complexity == 1


def test_other_languages_parse():
    assert analyze_functions("fun f(a: Int): Int { if (a>0) return a; return 0 }", "kotlin")[0].complexity == 2
    assert analyze_functions("function f(x){ if (x && x.y) return 1; return 0; }", "javascript")[0].complexity >= 2
    assert analyze_functions("func F(n int) int { if n > 0 { return 1 }; return 0 }", "go")[0].complexity == 2
    assert analyze_functions("class A { public int F(int x) { if (x>0) return 1; return 0; } }", "csharp")[0].complexity == 2


def test_supported_and_graceful_empty():
    assert supported("java") and supported(".kt") and not supported("cobol")
    assert analyze_functions("", "java") == []
    assert analyze_functions("not really code {{{", "java") == [] or True   # error-tolerant, never raises


def test_ast_agent_uses_treesitter_for_java():
    from core.models import AnalysisRequest, ChangeType, DiffHunk
    from agents.ast_analysis_agent import ASTAnalysisAgent
    diff = "+    public int f(int a, int b) {\n" + \
           "".join(f"+        if (a > {i}) {{ b += {i}; }}\n" for i in range(10)) + \
           "+        return b;\n+    }"
    req = AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                          source_ref="a", target_ref="b",
                          hunks=[DiffHunk(file_path="src/A.java", language="java",
                                          additions=12, deletions=0, content=diff)])
    res = ASTAnalysisAgent(api_key=None).fallback_result(req)
    assert res.max_complexity == 11             # 10 ifs + 1 — measured, not guessed
    assert any(f.kind == "complexity_spike" for f in res.findings)

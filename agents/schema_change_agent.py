"""
agents/schema_change_agent.py
------------------------------
Detects database and schema change risks in the diff.

Covers:
  • SQL migration files (Flyway, Liquibase, plain .sql)
  • ORM model changes (JPA/Hibernate @Entity, SQLAlchemy, Django models)
  • Schema definition language (DDL) in any diff hunk
  • Data migration risks (irreversible changes, large table ops)

Model: Sonnet — schema change analysis requires understanding of data implications.
Fallback: regex-based DDL detection (no LLM).

Banking context:
  • Any schema change is HOLD by default (complex rollback)
  • DROP TABLE / DROP COLUMN is BLOCK (irreversible data loss)
  • Large table ALTER (adding NOT NULL without default) is HOLD
  • Adding index on large table is HOLD (table lock risk)
"""
from __future__ import annotations
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest, RiskLevel,
    SchemaChange, SchemaChangeResult,
)
from core.token_manager import trim_diff_for_budget
from agents.base_agent import BaseAgent


# ── Regex DDL patterns ────────────────────────────────────────────────────────

_DROP_TABLE    = re.compile(r'(?i)\bDROP\s+TABLE\b')
_DROP_COLUMN   = re.compile(r'(?i)\bDROP\s+COLUMN\b')
_DROP_INDEX    = re.compile(r'(?i)\bDROP\s+INDEX\b')
_CREATE_TABLE  = re.compile(r'(?i)\bCREATE\s+TABLE\b')
_ALTER_TABLE   = re.compile(r'(?i)\bALTER\s+TABLE\b')
_ADD_COLUMN    = re.compile(r'(?i)\bADD\s+(COLUMN\s+)?(\w+)')
_ADD_INDEX     = re.compile(r'(?i)\bCREATE\s+(UNIQUE\s+)?INDEX\b')
_RENAME        = re.compile(r'(?i)\bRENAME\b')
_TABLE_NAME    = re.compile(r'(?i)(?:TABLE|INDEX\s+ON)\s+[`"\[]?(\w+)[`"\]]?')
_NOT_NULL      = re.compile(r'(?i)\bNOT\s+NULL\b')
_TRUNCATE      = re.compile(r'(?i)\bTRUNCATE\b')

# Migration file patterns
_MIGRATION_PATHS = re.compile(
    r'(?:flyway|liquibase|migrations?|db/migrate|V\d+__|\d{4}_|changelog)',
    re.IGNORECASE,
)


class SchemaChangeAgent(BaseAgent[SchemaChangeResult]):

    agent_name   = AgentName.SCHEMA_CHANGE
    output_model = SchemaChangeResult

    system_prompt = (
        "You are a database architect and DBA specialising in enterprise banking systems.\n"
        "Analyse the code diff for database schema changes. For each change identify:\n"
        "  • change_type: add_column | drop_column | add_table | drop_table | alter_column | "
        "add_index | drop_index | rename\n"
        "  • table_name, column_name (if applicable)\n"
        "  • severity: low | medium | high | critical\n"
        "  • reversible: true/false — can we roll back without data loss?\n"
        "  • rollback_sql: the exact SQL needed to undo this change\n"
        "  • description: one sentence on what changed and the risk\n\n"
        "Banking rules:\n"
        "  - DROP TABLE/COLUMN = critical, irreversible\n"
        "  - ALTER TABLE ADD COLUMN NOT NULL without default = high (locked table)\n"
        "  - CREATE INDEX on large tables = high (table lock)\n"
        "  - RENAME = high (app code must also change)\n"
        "  - TRUNCATE = critical, irreversible\n"
        "Also set: has_destructive, has_irreversible, rollback_risk, gate_contribution, summary.\n"
        "Output ONLY compact JSON."
    )

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        migration_hunks = [h for h in request.hunks if _is_migration_file(h.file_path)]
        orm_hunks       = [h for h in request.hunks if _is_orm_file(h.file_path)]
        target          = migration_hunks or orm_hunks or request.hunks

        diff    = "\n\n".join(h.content for h in target)
        trimmed = trim_diff_for_budget(diff, max_tokens_approx=3500)

        return (
            f"Repository: {request.repo_url}\n"
            f"Migration files detected: {[h.file_path for h in migration_hunks]}\n"
            f"ORM model files detected: {[h.file_path for h in orm_hunks]}\n\n"
            f"DIFF:\n{trimmed}"
        )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> SchemaChangeResult:
        # If no migration or ORM files, skip LLM and use structural scan only
        has_relevant = any(
            _is_migration_file(h.file_path) or _is_orm_file(h.file_path) or _has_ddl(h.content)
            for h in request.hunks
        )
        if not has_relevant:
            self.report_static_progress(request)
            return SchemaChangeResult(
                changes=[],
                has_destructive=False,
                has_irreversible=False,
                migration_files=[],
                rollback_risk=RiskLevel.LOW,
                gate_contribution="APPROVE",
                summary="No schema changes detected.",
            )
        return super().run(request, budget, context or {})

    def fallback_result(self, request: AnalysisRequest) -> SchemaChangeResult:
        """Regex DDL scanner — no LLM required."""
        changes:        list[SchemaChange] = []
        migration_files: list[str]         = []
        has_destructive  = False
        has_irreversible = False

        for hunk in request.hunks:
            if _is_migration_file(hunk.file_path):
                migration_files.append(hunk.file_path)

            for line in hunk.content.splitlines():
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                sql = line[1:].strip()

                # DROP TABLE
                if _DROP_TABLE.search(sql):
                    table = _extract_table_name(sql)
                    changes.append(SchemaChange(
                        file_path=hunk.file_path,
                        change_type="drop_table",
                        table_name=table,
                        severity=RiskLevel.CRITICAL,
                        reversible=False,
                        description=f"DROP TABLE '{table}' — data loss, irreversible.",
                        rollback_sql=f"-- Cannot auto-generate: table data is lost",
                    ))
                    has_destructive = has_irreversible = True

                # DROP COLUMN
                elif _DROP_COLUMN.search(sql):
                    table = _extract_table_name(sql)
                    changes.append(SchemaChange(
                        file_path=hunk.file_path,
                        change_type="drop_column",
                        table_name=table,
                        severity=RiskLevel.CRITICAL,
                        reversible=False,
                        description=f"DROP COLUMN on '{table}' — column data permanently lost.",
                        rollback_sql="-- Cannot auto-generate: column data is lost",
                    ))
                    has_destructive = has_irreversible = True

                # TRUNCATE
                elif _TRUNCATE.search(sql):
                    table = _extract_table_name(sql)
                    changes.append(SchemaChange(
                        file_path=hunk.file_path,
                        change_type="drop_table",  # treat as destructive
                        table_name=table,
                        severity=RiskLevel.CRITICAL,
                        reversible=False,
                        description=f"TRUNCATE on '{table}' — all rows deleted.",
                    ))
                    has_destructive = has_irreversible = True

                # ALTER TABLE ADD COLUMN NOT NULL
                elif _ALTER_TABLE.search(sql) and _ADD_COLUMN.search(sql) and _NOT_NULL.search(sql):
                    table = _extract_table_name(sql)
                    changes.append(SchemaChange(
                        file_path=hunk.file_path,
                        change_type="alter_column",
                        table_name=table,
                        severity=RiskLevel.HIGH,
                        reversible=True,
                        description=f"ADD COLUMN NOT NULL on '{table}' without default — may lock table.",
                        rollback_sql=f"ALTER TABLE {table} DROP COLUMN <column_name>;",
                    ))

                # ALTER TABLE (generic)
                elif _ALTER_TABLE.search(sql):
                    table = _extract_table_name(sql)
                    changes.append(SchemaChange(
                        file_path=hunk.file_path,
                        change_type="alter_column",
                        table_name=table,
                        severity=RiskLevel.MEDIUM,
                        reversible=True,
                        description=f"Schema alteration on table '{table}'.",
                    ))

                # CREATE TABLE
                elif _CREATE_TABLE.search(sql):
                    table = _extract_table_name(sql)
                    changes.append(SchemaChange(
                        file_path=hunk.file_path,
                        change_type="add_table",
                        table_name=table,
                        severity=RiskLevel.LOW,
                        reversible=True,
                        description=f"New table '{table}' — easy rollback.",
                        rollback_sql=f"DROP TABLE {table};",
                    ))

                # CREATE INDEX
                elif _ADD_INDEX.search(sql):
                    table = _extract_table_name(sql)
                    changes.append(SchemaChange(
                        file_path=hunk.file_path,
                        change_type="add_index",
                        table_name=table,
                        severity=RiskLevel.MEDIUM,
                        reversible=True,
                        description=f"New index on '{table}' — may lock table during creation.",
                    ))

                # RENAME
                elif _RENAME.search(sql):
                    changes.append(SchemaChange(
                        file_path=hunk.file_path,
                        change_type="rename",
                        severity=RiskLevel.HIGH,
                        reversible=True,
                        description="RENAME detected — application code must also be updated.",
                    ))

        rollback_risk = (
            RiskLevel.CRITICAL if has_irreversible else
            RiskLevel.HIGH     if has_destructive  else
            RiskLevel.MEDIUM   if changes          else
            RiskLevel.LOW
        )

        gate = "BLOCK" if has_irreversible else ("HOLD" if changes else "APPROVE")

        return SchemaChangeResult(
            changes=changes,
            has_destructive=has_destructive,
            has_irreversible=has_irreversible,
            migration_files=migration_files,
            rollback_risk=rollback_risk,
            gate_contribution=gate,
            summary=(
                f"Schema scan: {len(changes)} change(s) detected. "
                f"Destructive: {has_destructive}. Irreversible: {has_irreversible}."
            ),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_migration_file(path: str) -> bool:
    return bool(_MIGRATION_PATHS.search(path)) or path.endswith(".sql")


def _is_orm_file(path: str) -> bool:
    return any(s in path.lower() for s in (
        "entity", "model", "schema", "domain", "repository",
        "models.py", "entities/", "domain/",
    ))


def _has_ddl(content: str) -> bool:
    ddl_keywords = ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "DROP COLUMN",
                    "CREATE INDEX", "TRUNCATE", "RENAME TABLE")
    upper = content.upper()
    return any(kw in upper for kw in ddl_keywords)


def _extract_table_name(sql: str) -> str:
    m = _TABLE_NAME.search(sql)
    return m.group(1) if m else "unknown"

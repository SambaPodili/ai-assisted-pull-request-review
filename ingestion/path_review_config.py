"""
ingestion/path_review_config.py
---------------------------------
Loads a repo's own .gto.yaml — per-path review rules (e.g. "skip the
performance agent under scripts/", "steer stricter review for payments/**").

Trust boundary: for a webhook-triggered PR analysis, this MUST be read from
the PR's target/base branch, never the PR's own head — otherwise a PR could
ship a .gto.yaml that silently disables scrutiny of itself, the same class
of risk governance/prompt_guard.py's gate_manipulation category already
treats as adversarial. Callers (api/routes/webhooks.py) are responsible for
passing the target ref, not the source ref, into the loader below.

Malformed or missing config is never an error — it just means "no
path-scoped rules for this run," same failure mode as any other optional
per-repo file.

Glob semantics: plain fnmatch.fnmatch — a pattern with a '/' is anchored to
the repo root (fnmatch requires a full-string match); '*' is NOT slash-aware
in fnmatch, so it already matches across directory boundaries (no separate
'**' handling needed — 'payments/*' and 'payments/**' behave identically).
"""
from __future__ import annotations
import fnmatch
import logging

import yaml

from core.models import PathReviewConfig, PathReviewRule

log = logging.getLogger(__name__)

MAX_CONFIG_BYTES = 64_000   # defensive cap — this is a small hand-written config file
CONFIG_FILENAME = ".gto.yaml"


def parse_path_review_config(raw: str | None) -> PathReviewConfig | None:
    """Parse .gto.yaml content already fetched from disk/API. Never raises —
    malformed YAML or a schema mismatch just means no path-scoped rules."""
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        return None
    try:
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            return None
        return PathReviewConfig.model_validate(data)
    except Exception:
        log.warning("Malformed .gto.yaml — ignoring, proceeding without path-scoped rules", exc_info=True)
        return None


def load_from_git_client(git_client, repo_slug: str, ref: str) -> PathReviewConfig | None:
    """Webhook-triggered path: fetch .gto.yaml via the same provider API
    already used for diffs (ingestion/git_client.py::GitClient), resolved
    from `ref` — callers MUST pass the PR's target/base branch, see module
    docstring."""
    try:
        raw = git_client.get_file_content(repo_slug, ref, CONFIG_FILENAME)
    except Exception:
        log.debug("Could not fetch %s for %s@%s", CONFIG_FILENAME, repo_slug, ref, exc_info=True)
        return None
    return parse_path_review_config(raw)


def match_path(rule_match: str, file_path: str) -> bool:
    """True if `file_path` matches the glob `rule_match` — see module
    docstring for semantics."""
    return fnmatch.fnmatch(file_path, rule_match)


def rule_excludes_agent(rule: PathReviewRule, agent_key: str) -> bool:
    """True if `rule` excludes `agent_key` from hunks it matches.
    agents=None -> inherit (not excluded by this rule alone); skip=True or
    agents=[] -> excludes every agent; a non-empty agents=[...] is an
    allow-list (only those agents run for matching hunks)."""
    if rule.skip:
        return True
    if rule.agents is None:
        return False
    return agent_key not in rule.agents


def agent_fully_excluded(path_cfg: PathReviewConfig | None, agent_key: str, hunks: list) -> bool:
    """True if EVERY hunk is excluded for `agent_key` by some matching rule —
    used to skip dispatching the agent entirely (real cost savings), not just
    hide specific files from a still-running agent. A diff mixing excluded
    and non-excluded paths for the same agent still runs that agent (v1
    doesn't hide individual files from an agent that also has in-scope
    work — only a full-diff exclusion skips dispatch)."""
    if not hunks or not path_cfg or not path_cfg.paths:
        return False
    for hunk in hunks:
        excluded = any(
            match_path(rule.match, hunk.file_path) and rule_excludes_agent(rule, agent_key)
            for rule in path_cfg.paths
        )
        if not excluded:
            return False
    return True


def collect_path_scoped_instructions(path_cfg: PathReviewConfig | None, hunks: list) -> str:
    """Steering text from every rule that matches at least one hunk in the
    diff, deduped, newline-joined. Each piece is scanned by
    governance.prompt_guard before being trusted — .gto.yaml is repo-owned
    config, but still free text, and still gets the same defense-in-depth
    treatment as any other prioritization text (a bad phrase is dropped, not
    fatal — it never aborts the analysis)."""
    if not path_cfg or not path_cfg.paths or not hunks:
        return ""
    from governance.prompt_guard import scan as _scan
    file_paths = {h.file_path for h in hunks}
    seen: set[str] = set()
    pieces: list[str] = []
    for rule in path_cfg.paths:
        text = (rule.user_instructions or "").strip()
        if not text or text in seen:
            continue
        if not any(match_path(rule.match, fp) for fp in file_paths):
            continue
        if _scan(text):
            log.warning("Dropping .gto.yaml user_instructions for rule %r — blocked phrase detected", rule.match)
            continue
        seen.add(text)
        pieces.append(text)
    return "\n".join(pieces)

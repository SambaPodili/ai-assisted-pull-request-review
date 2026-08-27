"""
tests/unit/test_path_review_merge.py
--------------------------------------
Unit tests for merge_path_review_configs (team-scale config templates) —
asserts the merge is always at least as restrictive as either input alone,
since a team default must never widen what a repo's own .gto.yaml already
restricts (core.models.PathReviewRule's narrows-never-widens invariant).
"""
from __future__ import annotations

from core.models import PathReviewConfig, PathReviewRule
from ingestion.path_review_config import (
    merge_path_review_configs,
    agent_fully_excluded,
)


class Hunk:
    def __init__(self, file_path: str):
        self.file_path = file_path


def test_merge_both_none():
    assert merge_path_review_configs(None, None) is None


def test_merge_repo_only():
    repo_cfg = PathReviewConfig(paths=[PathReviewRule(match="scripts/**", skip=True)])
    merged = merge_path_review_configs(repo_cfg, None)
    assert merged is repo_cfg


def test_merge_team_only():
    team_cfg = PathReviewConfig(paths=[PathReviewRule(match="**/*.generated.*", skip=True)])
    merged = merge_path_review_configs(None, team_cfg)
    assert merged is team_cfg


def test_merge_disjoint_globs_unions_paths():
    repo_cfg = PathReviewConfig(paths=[PathReviewRule(match="scripts/**", skip=True)])
    team_cfg = PathReviewConfig(paths=[PathReviewRule(match="vendor/**", skip=True)])
    merged = merge_path_review_configs(repo_cfg, team_cfg)
    assert len(merged.paths) == 2

    hunks_scripts = [Hunk("scripts/deploy.sh")]
    hunks_vendor = [Hunk("vendor/lib.js")]
    assert agent_fully_excluded(merged, "security", hunks_scripts)
    assert agent_fully_excluded(merged, "security", hunks_vendor)


def test_merge_never_re_includes_what_repo_config_excluded():
    """A team file saying nothing about payments/** must not affect the
    repo's own exclusion of it — the merge only ever adds restriction."""
    repo_cfg = PathReviewConfig(paths=[PathReviewRule(match="payments/**", agents=[])])
    team_cfg = PathReviewConfig(paths=[PathReviewRule(match="docs/**", skip=True)])
    merged = merge_path_review_configs(repo_cfg, team_cfg)

    hunks = [Hunk("payments/charge.py")]
    assert agent_fully_excluded(merged, "security", hunks)


def test_merge_is_at_least_as_restrictive_as_either_alone():
    repo_cfg = PathReviewConfig(paths=[PathReviewRule(match="a/**", agents=["security"])])
    team_cfg = PathReviewConfig(paths=[PathReviewRule(match="a/**", agents=["code_analysis"])])
    merged = merge_path_review_configs(repo_cfg, team_cfg)

    hunks = [Hunk("a/x.py")]
    # repo_cfg alone allow-lists only security -> code_analysis excluded
    assert agent_fully_excluded(repo_cfg, "code_analysis", hunks)
    # merged still excludes code_analysis (repo's own rule still applies,
    # even though team_cfg's separate rule allow-lists it)
    assert agent_fully_excluded(merged, "code_analysis", hunks)

#!/usr/bin/env python3
"""
scripts/sync_mirrors.py
------------------------
Keep the cross-repo reference mirror (REPOS_ROOT) warm. Run from cron / CI so
analysis runs grep dependent repos instantly instead of cloning per request.

Credentials come from env (never the request pipeline):
  GIT_PROVIDER   github | github_enterprise | bitbucket_cloud | bitbucket_server
  GIT_BASE_URL   e.g. https://bitbucket.mycorp.com   (server/enterprise)
  GIT_USERNAME   (bitbucket_server / basic auth)
  GIT_TOKEN      personal/http access token  (or GIT_PASSWORD)
  GIT_WORKSPACE  (bitbucket_cloud)
  REPOS_ROOT     mirror directory (also read from config/settings)

Repos to sync: CLI args, or the MIRROR_REPOS env var (comma/space separated),
each as "PROJECT/repo" (server) / "owner/repo" (github) / "ws/repo" (cloud).
Pin a specific release branch/tag per repo with "slug@ref"
(e.g. "SCV/billing@release/2.1"); unpinned repos use MIRROR_REF, else default.

Examples:
  REPOS_ROOT=/var/ciaa/mirror GIT_PROVIDER=bitbucket_server GIT_BASE_URL=https://bb.corp \\
    GIT_USERNAME=svc GIT_TOKEN=xxxx python scripts/sync_mirrors.py \\
    SCV/billing@release/2.1 SCV/ledger@release/3.0 SCV/core
  # cron (every 15 min):  */15 * * * * cd /app && python scripts/sync_mirrors.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.routes.git_proxy import GitConfig, _clone_url, _split_slug_refs   # noqa: E402
from ingestion.repo_mirror import sync_repo, mirror_root  # noqa: E402


def _repos_from_args() -> list[str]:
    if len(sys.argv) > 1:
        return [a for a in sys.argv[1:] if a.strip()]
    raw = os.environ.get("MIRROR_REPOS", "")
    return [r for r in raw.replace(",", " ").split() if r.strip()]


def main() -> int:
    root = mirror_root()
    if not root:
        print("ERROR: REPOS_ROOT is not configured (env or config/settings).", file=sys.stderr)
        return 2
    repos = _repos_from_args()
    if not repos:
        print("No repos given. Pass slugs as args or set MIRROR_REPOS.", file=sys.stderr)
        return 2

    cfg = GitConfig(
        provider=os.environ.get("GIT_PROVIDER", "bitbucket_server"),
        base_url=os.environ.get("GIT_BASE_URL", ""),
        auth_mode="token" if os.environ.get("GIT_TOKEN") else "basic",
        token=os.environ.get("GIT_TOKEN", ""),
        username=os.environ.get("GIT_USERNAME", ""),
        password=os.environ.get("GIT_PASSWORD", ""),
        workspace=os.environ.get("GIT_WORKSPACE", ""),
    )
    # Per-repo release branch: "SCV/billing@release/2.1" or a MIRROR_REF default.
    slugs, ref_map = _split_slug_refs(repos, default_ref=os.environ.get("MIRROR_REF", ""))

    print(f"Syncing {len(slugs)} repo(s) into {root}")
    ok = 0
    for slug in slugs:
        url, secret = _clone_url(cfg, slug)
        if not url:
            print(f"  - {slug}: SKIP (unsupported provider)")
            continue
        ref = ref_map.get(slug, "")
        res = sync_repo(url, slug, ref=ref, secret=secret, root=root)
        ok += 1 if res.get("ok") else 0
        status = res.get("action", "?")
        err = f" — {res['error']}" if res.get("error") else ""
        print(f"  - {slug}@{ref or '(default)'}: {'OK' if res.get('ok') else 'FAIL'} ({status}){err}")
    print(f"Done: {ok}/{len(repos)} synced.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

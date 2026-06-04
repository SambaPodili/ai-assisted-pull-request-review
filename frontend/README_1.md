# AI Impact Analyzer — Frontend UI

A single-file web app that connects to both your Git provider and the backend API.

## How to open

Just double-click `frontend/index.html` or open it in any browser.
No build step. No npm. No install.

## What it does

**Step 1 — Configure provider**
- Choose GitHub or Bitbucket
- GitHub: personal access token OR username/password
- Bitbucket: username + app password (with workspace auto-detection)
- Verifies credentials against the real API and shows your username

**Step 2 — Repositories**
- Fetches all your real repositories (up to 200, paginated)
- Search by name
- Click to set the **primary repo** (green)
- Click-to-toggle **connected apps** (blue) — these are included in
  blast radius and interface analysis

**Step 3 — Analysis target**
Three tabs:
- **Pull request** — lists all open PRs (real data from GitHub/Bitbucket)
- **Branch diff** — two dropdowns from your real branches
- **Commit** — recent commits listed, or type a SHA directly

**Step 4 — Results**
- Submits the analysis to your backend if configured in Settings
- Falls back to AI simulation if the backend is offline
- Six result tabs: Summary, Security, Dependency, Interface, Schema, Remediation
- Gate banner (APPROVE / HOLD / BLOCK) with risk score
- History persisted in localStorage

## Backend setup (Settings tab in the UI)

1. Start the backend:
   ```bash
   cd impact-analyzer
   cp .env.example .env      # set ANTHROPIC_API_KEY, SKIP_AUTH=true
   pip install -r requirements.txt
   python main.py
   ```

2. Open the UI → click **Settings** → set Backend URL to `http://localhost:8080`

3. Click **Test connection** — should show `✓ Connected — Phase 2`

## GitHub token scopes needed

- `repo` (read access to private repos)
- `read:org` (to list org repos)

Create at: https://github.com/settings/tokens/new

## Bitbucket app password permissions needed

- Repositories: Read
- Pull requests: Read

Create at: https://bitbucket.org/account/settings/app-passwords/new

# GTO Pull Request Review — Web App UI Reference

UI reference for the standalone web app (`frontend/`, package name `impact-analyzer`) —
branded in-app as **GTO Pull Request Review Framework**. Same backend, same 20+
review agents as the VS Code extension and IntelliJ plugin, but the primary,
full-featured way to run and review an analysis: this is where the review
workflow (developer triage → reviewer validation → post to PR), team insights,
and admin/user management all live.

**Version:** 1.0.0 (frontend build stamp) · **Vendor:** UOB · **Platform:** React 19 + Vite, runs in any modern browser · **App id:** `impact-analyzer`

---

## Contents

1. [Overview](#overview)
2. [Shared chrome](#shared-chrome)
3. [Global keyboard shortcuts](#global-keyboard-shortcuts)
4. [Launcher (home page)](#launcher-home-page)
5. [Configure provider](#configure-provider)
6. [Repositories](#repositories)
7. [Analysis target](#analysis-target)
8. [Results](#results)
9. [Past analyses (History)](#past-analyses-history)
10. [Quality metrics](#quality-metrics)
11. [Team insights](#team-insights)
12. [Analysis agents (reference)](#analysis-agents-reference)
13. [Backend settings](#backend-settings)
14. [User management (Admin)](#user-management-admin)
15. [Cross-cutting behavior](#cross-cutting-behavior)

---

## Overview

The app has two layers:

- **Launcher** — a home page showing a grid of "framework" cards. Today only
  one framework is registered (GTO); the launcher exists so more frameworks
  can be added later without a rewrite.
- **App shell** — opened by clicking the GTO card. A left **Sidebar** for
  navigation, a **Topbar** with page title and status, and a content pane that
  swaps between 10 views. Four of them (**Configure → Repositories → Analysis
  target → Results**) are a linear wizard with Back/Continue footer buttons;
  the other six (**History, Quality, Insights, Agents, Settings, Admin**) are
  standalone pages reached directly from the sidebar.

---

## Shared chrome

Chrome shown on every view inside the app shell (not on the Launcher, which has its own minimal topbar).

| Element | Description |
|---|---|
| **Sidebar** | UOB logo/brand (click → back to Launcher), then nav sections: **Analysis** (Configure, Repositories, Analysis target, Results — each with a live status dot: connected/repo-selected/gate-color/running-pulse), **History** (Past analyses, with a run-count badge), **Validation** (Quality metrics, Insights), **Reference** (Analysis agents), **Settings** (Backend config, plus **User management** — shown only to users with the `user:manage` permission). Bottom of the sidebar shows the connected git identity (avatar initials, name, role chip, team, a green/grey connection dot). |
| **Topbar** | Page title + one-line subtitle for the active view, a **build-version badge** (see Build badge below), the current user's name + role chip (role always shown, defaulting to "Developer" if no role is resolved), a **Backend URL pill** (click → Settings), a dark/light mode toggle, and a keyboard-shortcuts (`?`) button that opens a fixed overlay panel. |
| **Footer bar** | Shown only on the 4-step wizard. **Back** (disabled on the first step) and **Continue** / **Run Analysis** (the label and action change on the last wizard step — Analysis target). The Run button is disabled with an explanatory tooltip until a valid target is selected or the priorities text is un-blocked. |
| **Toast** | Bottom-of-screen notifications (success / error / info / warn), auto-dismiss after 3.5s with a shrinking progress bar, dismissible by click, stacks up to 5. |
| **Build badge** | A small pill in the Topbar showing the frontend build version. Polls the backend `/live` endpoint every 30s; turns amber with a mismatch warning if the backend's reported version differs from the frontend's, and grey with a "plug-off" icon if the backend is unreachable. |
| **Error boundary** | Full-page fallback if a React render throws: an apology message, a collapsible raw error detail block, and **Reload app** / **Try to recover** buttons. Settings and history are unaffected (they live in `localStorage`). |

---

## Global keyboard shortcuts

Active anywhere except while typing in an input/textarea/select.

| Key | Action |
|---|---|
| `1`–`9` | Jump directly to the Nth sidebar nav item |
| `←` / `→` | Previous / next results sub-tab (Results view only) |
| `P` | Open the PR description generator (Results view only) |
| `N` | Start a new analysis (clears the current report/target and returns to Configure) |
| `D` | Toggle dark / light mode |
| `?` | Open the keyboard-shortcuts help overlay |
| `Esc` | Close the help overlay |

---

## Launcher (home page)

**Purpose:** entry point shown before any framework is opened; lets a workspace host more than one tool without a separate app per tool.

**Layout:** a minimal topbar (brand mark, "Frameworks" title, dark-mode toggle), a hero heading ("Choose a framework"), then a responsive card grid.

**Key elements**

| Element | Type | Behavior |
|---|---|---|
| Framework card (GTO) | Clickable card | Shows version badge, icon, title, tagline, description, and tags (Code review / Risk gate / Security / Banking-aligned); click opens the GTO app shell. |
| "More frameworks coming" tile | Static placeholder | Points to `frameworks.js` as the place to register a new framework entry. |

**Notable states:** a framework can be `active` (opens in-app), `external` (opens a link in a new tab), or `soon` (greyed out, non-clickable "Coming soon" badge). Only `active`/`external` frameworks are clickable.

---

## Configure provider

**Purpose:** connect a Git provider and an AI model provider, and optionally attach functional/requirement documents — step 1 of the wizard.

**Layout:** a two-column grid — **Git Provider** card on the left, **AI Model** card on the right — plus a full-width **Functional documents** card below.

**Key elements**

| Field / Control | Type | Description |
|---|---|---|
| Provider tiles | Selectable tiles | GitHub Cloud, GitHub Enterprise, Bitbucket Cloud, Bitbucket Server — switching resets the connection. |
| Enterprise base URL | URL input | Shown only for the Enterprise/Server variants. |
| Auth mode | Toggle (2 tiles) | Access Token vs Username+Password (label text adapts per provider). |
| Token / Password fields | Password input (show/hide) | Contextual hint text with a direct "create token" link where applicable. |
| Workspace slug / Project key | Text input | Bitbucket-specific; Project Key is optional and upper-cased automatically. |
| Connect & verify | Button | Calls the backend to verify credentials; shows a spinner while in flight. |
| Connection status banner | Info banner | Green "Connected as {name} [role]" or red "Connection failed: {message}" with a reachability hint. |
| Model provider tiles | Selectable tiles | Anthropic Claude, OpenAI, Azure OpenAI, Ollama (Local), Custom/Org. |
| Model / API key / Base URL | Select or text, password, URL | Fields shown depend on the chosen provider (e.g. Ollama needs a URL not a key; Custom is frozen — configured server-side). |
| Upload documents | File input (multi) | Accepts `.txt .md .json .csv` (read client-side) and `.docx .pdf` (extracted server-side); each added doc shows name, size, word count, and a remove button. |

**Notable states:** connecting spinner, success/failure banners, per-file toast warnings if a document fails to extract (e.g. binary format needs a backend and none is configured), and a "no text could be extracted" toast if every file in a batch fails.

---

## Repositories

**Purpose:** pick the primary repository to analyze and, optionally, additional "connected" repos for multi-repo blast-radius analysis — wizard step 2.

**Layout:** two stacked cards: **Primary repository** (search, optional Bitbucket Server project-key filter, repo tile grid) and **Connected applications** (chips of selected repos + the same tile grid to add more).

**Key elements**

| Element | Type | Description |
|---|---|---|
| Search box | Text input | Filters the repo tile grid by name/project. |
| Project key + Load | Text input + button | Bitbucket Server only — avoids fetching every repo across every project by default. |
| Repo tile | Clickable tile | Shows short name, language dot, and a "primary" or "+" badge when selected; click sets primary or toggles connected. |
| Connected repo chip | Chip with × | Removes that repo from the connected set. |

**Notable states:** "connect your provider first" error if no git session, loading spinner, load-error banner with the raw error message, grouped-by-project rendering automatically kicks in for Bitbucket Server once more than one project is present in the results.

---

## Analysis target

**Purpose:** choose what to diff (a PR, a branch pair, or a commit), and configure scan depth, agent scope, and review priorities — wizard step 3, the last step before running.

**Layout:** a target-type tab strip (Pull request / Branch diff / Commit) inside a card, followed by standalone cards: Deep scan checkbox, Analysis scope, Analysis priorities, and (conditionally) a Connected-repos-in-scope summary.

**Key elements**

| Field / Control | Type | Description |
|---|---|---|
| Target tabs | Tab strip | Pull request / Branch diff / Commit — switching loads that target type's data on first visit. |
| PR list | Scrollable list | Title, `#id`, head → base, author; click to select. |
| Source / Target branch | Two dropdowns | Populated from the repo's branch list; an info line confirms the pair once both are chosen. |
| Commit SHA | Text input + scrollable commit list | Type manually or click a commit row to fill it. |
| Deep scan | Checkbox | Runs security + code analysis over **every** changed file in batches instead of a prioritized sample — for large/critical PRs. |
| Analysis scope | Preset buttons (Fast / Standard / Thorough) + "Advanced: customize agents" checklist grouped by phase | Thorough is the default full-depth set (`selected_agents: null` server-side); toggling any agent switches to a custom selection; enabling Remediation force-enables Risk (it depends on Risk's output). |
| Analysis priorities | Textarea, 1500-char max | Free-text steering guidance ("focus on security in the payment module"). Debounced (~300ms) client-side prompt-guard scan mirrors the backend's rules; flags text that reads as an instruction override and **blocks submission** until reworded. |
| Connected repos in scope | Chip list | Read-only summary of the repos chosen in the previous step. |

**Notable states:** "select a primary repository first" error if step 2 was skipped, per-target-type loading/empty/error states (each error state has a **Retry** button), and the priorities textarea shows an amber border + warning line the moment blocked phrasing is detected.

---

## Results

**Purpose:** run the analysis, watch live agent progress, then read, triage, and act on the report — wizard step 4, and also reachable directly from History/Insights to reopen a past report.

### Before a run

If no report exists and no run has been requested: an empty state ("No analysis running") with a **Run Analysis** button (if a target is already selected) and a **Go to Analysis target** button.

### Running state

| Element | Description |
|---|---|
| Header | Spinner icon, "Running GTO Pull Request Review Framework", live status text, elapsed-seconds counter. |
| Progress bar | Percentage + a rotating status label (e.g. "Running: Security Review, Code Analysis…"). |
| Diff-fetched / cross-repo info lines | One-line confirmations once the diff is fetched and once dependent-repo call-sites are traced. |
| Live pipeline | Agent rows grouped by phase (Phase 1 → 1b → 2 → 3), each showing a status dot, icon, name, a running/done/fallback badge, an engine badge (LLM / Hybrid / Static), elapsed or final duration, token count, and a fill bar sized by token share. |
| Simulation notice | Amber banner shown only when no backend is configured — the app falls back to a locally-simulated mock report so the UI can still be demoed offline. |

**Notable states:** a dedicated "No diff found" / "Diff fetch failed" screen (with a **Choose a different target** button) short-circuits the run entirely rather than showing an empty report; a long-running backend queue shows "Queued — position N"; the run is guarded against React StrictMode double-invocation and against a stale in-flight run when a second analysis is started.

### Completed report

| Element | Description |
|---|---|
| Role badge bar | Shows the current user's resolved role and a note that reviewer actions are hidden if the role can't comment. |
| Notices | Conditional banners: heuristic-mode (agents fell back to rule-based checks), analysis errors, and "served from cache" (with a **Re-analyse fresh** button to bypass the cache). |
| Human review panel | Three decision tiles — **Approve merge**, **Request changes**, **Block merge** — each opening a modal that requires a reason before submitting; the recorded decision then shows as a persistent banner. |
| Gate banner | The single authoritative **APPROVE / HOLD / BLOCK** decision, risk score (0–100), rationale, an optional "POLICY-ENFORCED" badge (when the deterministic policy overrode the AI's proposed gate), and the active model badge. |
| Metrics grid | 9 stat tiles: security findings, secrets detected, blast radius, breaking changes, test gaps, QA scenarios, schema changes, total tokens, run time. |
| Section / sub-tab navigation | Two-level tabs — 5 sections (**Summary, Security & Compliance, Impact, Quality, Testing & Spec**), each expanding to its own row of sub-tabs (19 tabs total, listed below). |
| Findings search | A search box appears above finding-heavy tabs (Security, Secrets, Taint, IaC, Dependency, Interface, Schema, Performance, Privacy, Quality) to filter that tab's list. |
| Action bar | **Post to PR** (reviewers only) or **Review summary** toggle, a **Judge panel** toggle, a **More** menu (PR description generator, plus a locked "Post to PR" entry with an explanatory tooltip for non-reviewers), and **New analysis**. |

#### Summary section

| Tab | Contents |
|---|---|
| **Summary** | Developer/Reviewer persona toggle; auto-suppressed-findings notice; LLM coverage banner (full / batched deep-scan / partial-with-budget-cap); collapsible Files changed list; **Review plan** (files bucketed into Must fix / Needs a human / Auto-approvable, plus a "read first" shortlist) — reviewer view only shows a Domain status rollup above this; **Top issues to review** — the cross-agent-deduplicated, ranked triage list (see below); then a persona-specific view (Developer: blockers + fix suggestions + QA scenario count; Reviewer: a decision-oriented findings rollup). |
| **Timings** | Per-agent timing breakdown for the run. |

**Top issues to review** is the app's central triage workflow: each row shows severity, title, file:line (with an expandable inline code snippet), a location-confidence dot, which agent(s) flagged it (with a "✓ N agents agree" badge on cross-agent matches), and — when a review session exists — a **developer/reviewer triage workflow** with three stages: ① *Developer triage* (mark each issue ✓ Valid or ⚐ False positive, with an optional comment, then **Submit for review**), ② *Reviewer validation* (Confirm / Reject each, pick a final gate, then **Approve & post to PR**, or send it back to the developer), ③ *Done*. Only the user's own role's actions are active in each stage.

#### Security & Compliance section

| Tab | Contents |
|---|---|
| **Findings** (security) | CWE-tagged findings list with severity, file/line, inline code snippet. |
| **Secrets** | Hardcoded-secret detections with "what to do" remediation guidance (remove, rotate, or mark false positive). |
| **Taint** | Source-to-sink taint-tracking findings (injection / SSRF / path traversal). |
| **IaC** | Terraform/Kubernetes/Docker misconfiguration findings. |
| **Privacy** | PII-handling findings (GDPR/PCI-DSS/PDPA). |
| **Compliance** | Compliance-framework mapping per finding category, with a **Copy as Markdown** export. |

#### Impact section

| Tab | Contents |
|---|---|
| **Blast radius & deps** | Dependency blast-radius score, affected services, changed packages, CVE hits; includes an embedded **SCA scanner** (upload a manifest to scan ad hoc) and a **dependency auto-update** helper. |
| **References** | An interactive D3-rendered call graph, a "High-Impact Files" list (≥3 references), and a paginated full reference table. |
| **Cross-Repo** | Per-declared-dependent-repo call-site analysis: for each call site of a changed symbol, whether it breaks and the suggested fix. |
| **Interface** | Contract-breaking changes (REST/gRPC/AsyncAPI/MQ) and downstream consumer impact. |
| **Schema** | Database/migration risk (destructive or irreversible changes flagged). |

#### Quality section

| Tab | Contents |
|---|---|
| **Code Quality** | Maintainability findings — long functions, duplication, complexity smells. |
| **Complexity** (AST) | Structural/complexity analysis from the parsed syntax tree. |
| **Performance** | N+1 queries, complexity regressions, hot-path risk findings. |
| **History** (temporal) | Cross-PR-history risk signals — repeatedly-touched hot files, escalating risk. |

#### Testing & Spec section

| Tab | Contents |
|---|---|
| **QA Scenarios** | Generated test scenarios, filterable by priority and type; each scenario has a **Copy as Gherkin** button and, where a runnable skeleton was generated, a **Copy skeleton** button. |
| **FSD / Spec** | Validates the change against uploaded functional/requirement documents and reports business-function impact. |
| **Remediation** | Deterministic + AI-suggested fix diffs (each with a **Copy** button), a unit-test-coverage table for changed methods, and deployment guidance. |

#### Modals and side panels

| Panel | Opened from | Purpose |
|---|---|---|
| **PR description modal** | `More` menu, or the `P` shortcut | Generates a PR description from the report and offers a **Copy to clipboard** button. |
| **Review summary panel** | `Review summary` button | A reviewer-ready rollup grouped by severity, with its own **Copy as Markdown** export. |
| **Judge panel** | `Judge panel` button | Configure a panel of LLM judges (provider + model each, add/remove rows, or "use my model") and run an independent quality evaluation of the analysis. |
| **Human review decision modal** | Clicking a Human review panel tile | Requires a written reason before recording an Approve / Request changes / Block decision. |

---

## Past analyses (History)

**Purpose:** browse previously run analyses (stored client-side) and reopen any of them.

**Layout:** a 4-tile stats row, a risk-trend sparkline, a search + gate-filter bar, then a date-grouped list (Today / Yesterday / This week / This month / Month Year).

**Key elements**

| Element | Type | Description |
|---|---|---|
| Stats tiles | Read-only | Total runs, Approved, Blocked, Avg risk. |
| Risk trend sparkline | Inline SVG chart | Last 10 runs' risk scores, with a trend arrow. |
| Search box | Text input | Filters by repo, target, or gate text. |
| Gate filter | Segmented control | All / APPROVE / HOLD / BLOCK. |
| History row | Clickable list item | Gate icon, repo, target, timestamp, risk badge + bar + numeric score. |
| Clear history | Button + confirm step | Wipes all locally stored history after an inline "Yes, clear" / "Cancel" confirmation. |

**Notable states:** empty state ("No analyses run yet") with a **Go to Analysis target** button; "No matches" when a filter/search yields nothing. History is capped at 50 entries and lives entirely in `localStorage` — never on the backend.

---

## Quality metrics

**Purpose:** measure the static detection layer's own recall/precision against a golden-diff test suite — an internal QA tool for the review agents themselves, not for a specific PR.

**Layout:** a control card (Run / Load buttons, an alert-threshold input) followed by two result tables once data exists.

**Key elements**

| Field / Control | Type | Description |
|---|---|---|
| Run quality check | Button | Triggers a fresh golden-diff run on the backend. |
| Load last run | Button | Fetches the most recent stored summary without re-running. |
| Alert threshold | Number input (0–100%) | Persisted locally; the by-agent table highlights whether overall recall is above or below it. |
| By-agent table | Table | Recall / Precision / F1 / TP / FP / FN per agent, with colored progress bars. |
| By golden case table | Table | Recall / Precision per individual test case. |

**Notable states:** "Backend required" info message when no backend URL is set; "No history yet" before any run has been made; a loading spinner while a check is running.

---

## Team insights

**Purpose:** a manager/lead-facing rollup across all analyses — review queue triage, risk trends, a change-frequency heatmap, API cost, and detection-quality feedback.

**Layout:** a shared filter bar (day-range chips, repo dropdown, auto-refresh checkbox, CSV export) and an executive-summary banner above 5 tabs.

| Tab | Contents |
|---|---|
| **Review queue** | Every analysis ranked BLOCK-first then by risk score, with top findings previewed inline; **View** reopens the full report in the Results view. |
| **Risk trend** | Per-repo risk sparkline over the last 12 weeks with an improving/degrading/stable badge, plus a bar chart of top recurring issue categories. |
| **Change heatmap** | A D3 tile grid — tile size = change frequency, color = max risk — plus a highest-risk-files table with security/privacy flags. |
| **API cost** | Summary tiles (total cost, tokens, fallback count, weeks covered), a weekly-spend bar chart, and cost-by-agent / cost-by-model tables. |
| **Feedback** | Detection accuracy computed from reviewer valid/false-positive verdicts (overall %, least-accurate agents), gate-override behavior (did reviewers loosen or tighten the AI's proposed gate more often), and a noisiest-checks table ranked by false-positive rate. |

**Key elements**

| Element | Type | Description |
|---|---|---|
| Day-range chips | Segmented control | 7 / 30 / 90 days / All time. |
| Repo dropdown | Select | Filters every tab to one repository. |
| Auto-refresh | Checkbox | Re-polls the active tab every 30s. |
| CSV export | Button | Downloads the active tab's data (not available for the Heatmap tab). |
| Executive summary banner | Gradient banner | One-sentence rollup: total analyses, % needing attention, blocked count, and the top-risk repo. |

**Notable states:** "Backend required" empty state if no backend is configured; per-tab loading spinners and error banners; "not enough history yet" on the trend tab with too little data.

---

## Analysis agents (reference)

**Purpose:** a read-only reference explaining what each of the ~21 analysis agents does and how it runs — no interactive controls beyond navigation.

**Layout:** an overview card (agent count + engine-type legend), then one card per phase (Phase 1 — Core, Phase 1b — Deep Scan (parallel), Phase 2 — Integration, Phase 3 — Synthesis), each listing its agents in a card grid.

**Key elements**

| Element | Type | Description |
|---|---|---|
| Engine legend | Static | Three badges — **LLM** (always uses the model), **Hybrid** (static scan first, LLM enhancement when budget allows), **Static** (deterministic, zero tokens) — explaining why some agents finish "instant". |
| Agent card | Static | Icon, label, engine badge, one-line description of what that agent checks. |

---

## Backend settings

**Purpose:** configure the backend connection, dependency-scanning (SCA) sources, and — for super admins — email digests and stored-report purging.

**Layout:** a stack of independent cards.

| Card | Key fields | Notes |
|---|---|---|
| **Backend API connection** | Backend URL, API key | The URL is editable only by a super admin or before first connecting (then it locks); the API key is always editable — it's each user's own login credential and determines their role. **Save** / **Test connection** buttons. |
| **Maven repository (SCA)** | Maven repo URL, auth token | Used to resolve parent/BOM versions (e.g. Spring Boot) when scanning a `pom.xml`; falls back to the backend's own default (Maven Central) if blank. |
| **Vulnerability database (SCA)** | OSV vs JFrog Xray radio, Xray URL/token, fallback-source dropdown | OSV is the public CVE database; Xray is an in-house alternative for air-gapped/bank-network use. The fallback dropdown controls what's tried if the primary source is unreachable. |
| **Config export / import** | Export / Import buttons | Downloads or restores the full local config (provider, auth, model, backend URL) as a JSON file. **Includes secrets** — the UI warns to keep the file secure. |
| **Email daily digest** *(super admin only)* | Preview / Send test now | SMTP is configured via backend env vars (shown in a reference table); this card only previews or triggers a test send. |
| **Purge stored reports** *(super admin only)* | One-click "clean demo data", or a repo-substring + age-in-days filter | Always offers a **Preview** (dry run) before an irreversible **Delete**. |
| **Reliability & alerting env vars** | Read-only reference table | Documents `LLM_RETRY_ATTEMPTS`, `LLM_RETRY_MAX_WAIT_S`, `WEBHOOK_DEDUP_TTL_S`, `QUALITY_RECALL_ALERT_THRESHOLD`. |

**Notable states:** a lock badge ("Super admin only") visually disables fields the current user's role can't edit rather than hiding them; every destructive purge action requires an explicit confirm dialog.

---

## User management (Admin)

**Purpose:** create and manage backend API keys / roles for other users, without hand-editing the backend's key file. Only visible in the sidebar to a user whose role includes the `user:manage` permission.

**Layout:** a one-time key-reveal banner (conditional), an "Add a user" card, a users table, and an audit trail list.

**Key elements**

| Element | Type | Description |
|---|---|---|
| Bitbucket/git username lookup | Text input + button | Auto-fills the Name and User ID fields from the connected git provider's user directory. |
| Add-user form | Name, User ID, Team, Role fields + Create button | Role choices are limited to whatever the current user is permitted to grant (`creatable_roles`). |
| New API key banner | One-time reveal | Shown immediately after creating a user; the key is never retrievable again — a **Copy** button and an explicit "Done" dismissal. |
| Users table | Table | Name, User ID, Team, Role (editable dropdown for backend-managed users), key prefix, source (`managed` vs `file`), created-by, and a **Revoke** action (with a confirm dialog) for managed users — file-based keys must be edited in the key file directly. |
| Audit trail | List | Every create/revoke/role-change event, permanently logged, with actor and timestamp. |

**Notable states:** "Configure a backend URL in Settings first" error if unset; empty "No users yet" / "No actions recorded yet" states.

---

## Cross-cutting behavior

- **Role-based visibility is default-deny.** A user whose role hasn't resolved yet (no key, or the backend hasn't responded) is always treated as a plain Developer — never shown Reviewer, Admin, or Super Admin controls. The backend re-enforces every permission independently; the UI's role gating is a convenience, not the security boundary.
- **Two persistence layers.** Most Configure/Settings fields (provider, auth, model choice, backend URL, SCA config) persist to `localStorage` and survive a reload. A few fields are deliberately **not** persisted — Deep scan and Analysis priorities reset on reload, since they're per-run choices, not standing preferences.
- **Analysis history is local-only.** The last 50 runs are cached in the browser's `localStorage` for the History view and are never sent to or read from the backend as a data source (the backend has its own separate store, surfaced instead via the Insights views).
- **Offline/no-backend simulation.** If no Backend URL is configured, running an analysis falls back to a client-side simulated report (clearly labeled "AI Simulation mode") so the UI remains demoable without a live backend.
- **Client-side prompt-guard is UX-only.** The real-time "this looks like an override attempt" warning on the Analysis priorities textarea is a convenience mirror of the backend's authoritative guardrail (`governance/prompt_guard.py`) — the backend re-validates regardless, and priorities text can never suppress a security/secrets finding or change the gate decision.

---

*Source: `frontend/src/` in the repository. Reflects the `impact-analyzer` frontend as of this writing; no screenshots included — this is a structural/behavioral reference intended for pasting into Confluence.*

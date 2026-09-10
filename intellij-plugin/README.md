# GTO Pull Request Review Framework — IntelliJ Plugin

IntelliJ counterpart to `vscode-extension/` — same backend, same review
agents, same results rendering (see `shared/report-view`). Built with the
IntelliJ Platform Gradle Plugin (Kotlin).

## Status: feature parity with the VS Code extension

Every VS Code extension command and interaction has an IntelliJ
counterpart, verified building (`compileKotlin`, `buildPlugin`, and
`verifyPluginProjectConfiguration` all pass clean) against IntelliJ
Platform Community `2023.3.6`.

| VS Code | IntelliJ | File |
|---|---|---|
| Analyze Changes / Analyze Branch... | ✅ same flow: preset + priorities prompts (prompt-guard-validated), path-scoped `.gto.yaml`, review-context metadata, model-override resolution, run-dedup, suppression filtering, delta badges | `actions/AnalyzeChangesAction.kt`, `AnalyzeBranchAction.kt`, `AnalysisRunner.kt` |
| Results panel (issues, fix diffs, QA skeletons, diagrams, similar PRs) | ✅ same rendering (shared/report-view), full interactivity: open finding, apply fix / apply all, suppress / unsuppress, mark false positive, explain finding, post to PR, approve PR, create test file, copy code/markdown, similar past PRs | `toolwindow/GtoResultsPanel.kt` |
| Diagnostics (squiggles) | ✅ | `inspection/GtoFindingInspection.kt` |
| Quick Fix (💡 lightbulb) | ✅ same staleness check as the results panel's Apply button | `inspection/GtoFindingInspection.kt` (`GtoApplyFixQuickFix`), `GtoCodeFixApplier.kt` |
| CodeLens ("⚠ N GTO issue(s)") | ✅ via IntelliJ's Code Vision (the platform's own CodeLens equivalent) | `inspection/GtoCodeVisionProvider.kt` |
| Status bar item | ✅ | `GtoStatusBarWidget.kt` |
| Auto-analyze-on-save | ✅ 1.5s-debounced, non-interactive, never overlaps a run in flight | `GtoStartupActivity.kt` |
| Local suppression (`.gto-ignore.json`) + "New" delta badge | ✅ same fingerprint formula as shared/report-view and the web app | `GtoReportState.kt` |
| Git hook install/uninstall | ✅ reuses the exact bash script byte-for-byte (plain shell, IDE-agnostic) | `actions/GitHookActions.kt` |
| Set API Key / Model API Key / Git Provider Token | ✅ via PasswordSafe (OS keychain) | `actions/CredentialActions.kt`, `settings/GtoCredentials.kt` |
| Select Model (server presets) | ✅ real quick-pick, current selection marked | `actions/CredentialActions.kt` |
| Settings (all `gto.*` equivalents) | ✅ one Settings > Tools > GTO Review page | `settings/GtoSettingsState.kt`, `GtoSettingsConfigurable.kt` |
| Review-context gathering (existing tests / cross-repo refs / functional docs) | ✅ — actually cheaper here than in VS Code (see `ReviewContext.kt`'s header) | `ReviewContext.kt` |
| Prompt-injection guard (client-side mirror) | ✅ same ~15 rules | `GtoPromptGuard.kt` |

**Known, deliberate non-1:1 differences** (not gaps — see each file's own
comment for the reasoning):
- Git access shells out to the `git` CLI directly rather than depending on
  a bundled VCS plugin's internal API — same choice `gitDiff.ts` makes and
  states its own rationale for.
- `AnalysisReportView` keeps the report as raw JSON rather than a ~40-field
  typed model, since rendering happens entirely in the shared JS bundle.

## Build

```bash
cd intellij-plugin
gradle compileKotlin   # or: gradle buildPlugin, gradle runIde
```

No Gradle wrapper is committed yet — `gradle wrapper` in this sandbox
couldn't reach `services.gradle.org` to validate the distribution URL (a
sandbox network restriction, not a project issue). Run
`gradle wrapper --gradle-version 9.7.1` yourself once on a machine with
normal internet access to generate and commit `gradlew`/`gradlew.bat`/
`gradle/wrapper/*` — Gradle 9.7.1 + `org.jetbrains.intellij.platform` 2.18.1
is the combination verified working in this session.

**JDK**: needs a real JDK 17 for the Gradle daemon itself, separate from
whatever toolchain auto-provisioning resolves for the compile target. If
`gradle.properties`' `org.gradle.java.home`/`org.gradle.java.installations.paths`
point at a path that doesn't exist on your machine, remove those two lines —
they exist only because this sandbox's only readily-available JDK 17 was a
keg-only Homebrew install not registered with `/usr/libexec/java_home`. A
normal JDK 17 install should need none of this.

## Installing (manual ZIP, matching how the VS Code `.vsix` is shared today)

1. `gradle buildPlugin` → produces `build/distributions/gto-pr-review-intellij-<version>.zip`.
2. Share that ZIP the same way the VS Code `.vsix` is already shared (network drive, wiki, Slack — whatever's already in use).
3. Each developer: **Settings → Plugins → ⚙️ → Install Plugin from Disk...** → pick the ZIP → restart when prompted.
4. First run: **Settings → Tools → GTO Review** — set Backend URL + API key.

Updates are manual (re-share the new ZIP, re-install) — ask if/when this
grows past a handful of developers and a proper internal plugin repository
(auto-update notifications) becomes worth the extra hosting setup.

## Architecture notes

- **No VCS4Idea dependency.** Git access shells out to the `git` CLI
  directly (`GitDiffProvider.kt`), mirroring `vscode-extension/src/
  gitDiff.ts`'s own stated rationale: fewer moving parts, no dependency on
  a bundled plugin's internal API surface.
- **No typed `AnalysisReport` model.** `ApiClient.kt` deliberately keeps the
  report as a raw `JsonObject` (`AnalysisReportView`) rather than a ~40-field
  Kotlin data class — the report is rendered entirely by the shared JS
  bundle in a JCEF browser, which parses the JSON itself; Kotlin only reads
  a handful of top-level fields directly (gate, request id, top_issues for
  the inspection/Code Vision).
- **Credentials never touch the persisted XML state** — `GtoCredentials.kt`
  uses PasswordSafe (OS keychain), matching `vscode-extension/src/
  settings.ts`'s SecretStorage separation exactly.
- **One shared JS bridge, not ten.** `GtoResultsPanel.kt` uses a single
  `JBCefJSQuery` dispatching on a `{command, ...}` payload — the IntelliJ
  equivalent of `acquireVsCodeApi().postMessage`, mirroring
  `resultsPanel.ts`'s own single-channel design rather than one query
  object per button.

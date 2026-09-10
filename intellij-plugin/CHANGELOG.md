# Changelog

## 0.2.1

Lowered the minimum supported IntelliJ Platform version so developers on
an older IDE install aren't blocked from installing the plugin.

- **Compatibility floor lowered from 2023.3 (build 233) to 2022.3 (build
  223).** `sinceBuild` (`build.gradle.kts`) and `platformVersion`
  (`gradle.properties`) both moved down, and the plugin was recompiled
  directly against a real 2022.3.3 SDK — not just declared compatible —
  to catch any real API difference at build time instead of at runtime on
  someone's machine.
- Replaced the auto-analyze-on-save implementation
  (`GtoStartupActivity.kt` → `GtoFileSaveListener.kt`): the old one used
  `com.intellij.openapi.startup.ProjectActivity` and
  `FileDocumentManagerListener.TOPIC`, neither of which exists in
  2022.3.3. Now registers a `FileDocumentManagerListener` directly via
  its extension point (present in every version this plugin targets),
  resolving the owning project per save via `ProjectLocator` since that
  registration is application-scoped rather than project-scoped.
  Behavior is unchanged: 1.5s debounce, non-interactive, never overlaps a
  run already in flight, warns once (not on every save) if no API key is
  set.
- Disabled the `buildSearchableOptions` Gradle task. It's unrelated to
  plugin compatibility — that task's own implementation (IntelliJ
  Platform Gradle Plugin 2.18.1) hard-refuses to run against an IDE
  older than build 233, regardless of what the plugin's code supports.
  Only effect: the Settings dialog's own search box won't index this
  plugin's Settings page fields. The Settings page itself
  (**Settings → Tools → GTO Review**) is unaffected.

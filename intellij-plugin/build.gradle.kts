import org.jetbrains.kotlin.gradle.tasks.KotlinCompile

plugins {
    id("java")
    id("org.jetbrains.kotlin.jvm") version "1.9.22"
    id("org.jetbrains.intellij.platform") version "2.18.1"
}

group = providers.gradleProperty("pluginGroup").get()
version = providers.gradleProperty("pluginVersion").get()

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        create(providers.gradleProperty("platformType"), providers.gradleProperty("platformVersion"))
        // Bundled plugins we build against — none required for v1 (no VCS4Idea
        // dependency: this plugin shells out to the `git` CLI directly, same
        // as vscode-extension/src/gitDiff.ts, rather than depending on a
        // bundled VCS plugin's internal API surface).
        bundledPlugin("com.intellij.modules.json")
    }

    // kotlinx-coroutines-core is deliberately NOT declared here — IntelliJ
    // Platform bundles its own copy, and adding a project-level one conflicts
    // at runtime (flagged by `gradle verifyPluginProjectConfiguration`). See
    // https://jb.gg/intellij-platform-kotlin-coroutines — `delay`/`runBlocking`
    // (ApiClient.kt, AnalysisRunner.kt) resolve against the platform's copy.
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")
    // .gto.yaml parsing (PathReviewConfig.kt) — mirrors vscode-extension's
    // js-yaml dependency for the same file/schema.
    implementation("org.yaml:snakeyaml:2.2")

    testImplementation("org.junit.jupiter:junit-jupiter:5.10.2")
}

kotlin {
    jvmToolchain(17)
}

intellijPlatform {
    pluginConfiguration {
        id.set("com.uob.gto.pr-review")
        name.set("GTO Pull Request Review Framework")
        version.set(providers.gradleProperty("pluginVersion"))

        ideaVersion {
            sinceBuild.set("223")
            untilBuild.set(provider { null }) // no upper bound — stay compatible forward until proven otherwise
        }
    }

    // pluginVerification (marketplace binary-compatibility checking across IDE
    // versions) is deliberately not configured yet — hit a Gradle 9.x
    // incompatibility in the plugin-verifier DSL's `ides { recommended() }`
    // helper during initial scaffolding; revisit once actually preparing a
    // release build. Doesn't affect compiling/running the plugin.
}

// Copies the shared report-rendering bundle in from ../shared/report-view
// (see that package's README "Consumers" section) on every build, so
// src/main/resources/webview/report-view.iife.js can never silently go
// stale relative to the checked-in copy — it's regenerated from source
// every time instead of being a manually-maintained artifact. Requires
// `npm run build` to have been run in shared/report-view at least once
// (its dist/ output is itself gitignored, same as any other build output).
val copySharedReportView = tasks.register<Copy>("copySharedReportView") {
    from("../shared/report-view/dist/report-view.iife.js")
    into("src/main/resources/webview")
    doFirst {
        val src = file("../shared/report-view/dist/report-view.iife.js")
        if (!src.exists()) {
            throw GradleException(
                "../shared/report-view/dist/report-view.iife.js is missing — run " +
                    "'npm run build' in shared/report-view first (see that package's README)."
            )
        }
    }
}

tasks {
    named("processResources") {
        dependsOn(copySharedReportView)
    }
    // buildSearchableOptions (indexes Settings-page fields for the Settings
    // dialog's own search box) launches a headless IDE using the SAME
    // platformVersion this module compiles against, and the Gradle plugin's
    // OWN implementation of that task hard-refuses below IDE 2023.3 — a
    // build-tooling limit, unrelated to whether the plugin's actual code is
    // compatible with an older IDE (verified separately: compileKotlin
    // passes clean against 2022.3.3). Disabling it costs only the Settings-
    // search-box indexing for our one Settings page; the page itself still
    // works and is still reachable normally via Settings > Tools > GTO Review.
    named("buildSearchableOptions") {
        enabled = false
    }
    withType<KotlinCompile> {
        compilerOptions {
            jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
        }
    }
    test {
        useJUnitPlatform()
    }
}

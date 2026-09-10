package com.uob.gto

import com.intellij.openapi.components.Service
import com.uob.gto.api.AnalysisReportView

/** Holds the most recent report (+ the state it was rendered with) so "GTO:
 * Show Last Result" can reopen it without re-running analysis, and so a
 * repeat "Analyze Changes" on an unchanged diff can short-circuit — mirrors
 * extension.ts's module-level `lastReport`/`lastRunKey`. */
@Service(Service.Level.APP)
class LastReportHolder {
    var report: AnalysisReportView? = null
        private set
    var repoRoot: String? = null
        private set
    var suppressed: List<GtoReportState.SuppressedEntry> = emptyList()
        private set
    var newFingerprints: Set<String> = emptySet()
        private set
    var runKey: String? = null
        private set

    fun set(report: AnalysisReportView, repoRoot: String, suppressed: List<GtoReportState.SuppressedEntry>, newFingerprints: Set<String>, runKey: String) {
        this.report = report
        this.repoRoot = repoRoot
        this.suppressed = suppressed
        this.newFingerprints = newFingerprints
        this.runKey = runKey
    }

    companion object {
        fun getInstance(): LastReportHolder =
            com.intellij.openapi.application.ApplicationManager.getApplication().getService(LastReportHolder::class.java)
    }
}

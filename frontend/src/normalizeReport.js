// Normalize backend report to UI shape
export function normalizeReport(r) {
  const isFull = r.code_analysis !== undefined || r.security !== undefined || r.errors !== undefined;

  let gate_decision, overall_risk, risk_score, rationale, riskObj, remObj;

  if (isFull) {
    const rr = r.risk || {};
    gate_decision = rr.gate_decision || 'HOLD';
    overall_risk  = rr.overall_risk  || 'medium';
    risk_score    = rr.risk_score    || 0;
    rationale     = rr.rationale     || (r.errors && r.errors[0]) || '';
    riskObj = {
      deployment_guidance: rr.deployment_guidance || '',
      rollback_feasibility: rr.rollback_feasibility || '?',
      deployment_strategy: r.remediation?.deployment_strategy || 'standard',
    };
    remObj = r.remediation || null;
  } else {
    gate_decision = r.gate || r.gate_decision || 'HOLD';
    overall_risk  = r.risk || r.overall_risk  || 'medium';
    risk_score    = r.risk_score || 0;
    rationale     = r.rationale  || '';
    riskObj = { deployment_strategy: 'standard', rollback_feasibility: '?', deployment_guidance: '' };
    remObj  = null;
  }

  const sec = r.security || null;
  const secNorm = sec ? {
    overall_severity: sec.overall_severity || 'low',
    secrets_detected: sec.secrets_detected || false,
    findings: (sec.findings || []).map(f => ({
      cwe: f.cwe_id || f.cwe || '',
      severity: f.severity || 'low',
      description: f.description || '',
      file: f.file_path || f.file || '',
      line_range: f.line_range || '',
      unverified: f.unverified || false,
    })).filter(f => f.description.trim() || f.cwe.trim() || f.file.trim()),  // drop phantom/empty findings
  } : null;

  const ca = r.code_analysis || null;
  const caNorm = ca ? {
    summary: ca.summary || '',
    change_type: ca.change_type || 'unknown',
    complexity_delta: ca.complexity_delta || 0,
    findings: (ca.findings || []).map(f => ({
      severity: f.severity || 'low',
      description: f.description || '',
      file: f.file_path || f.file || '',
      line_range: f.line_range || '',
    })),
  } : null;

  const dep = r.dependency || null;
  const depNorm = dep ? {
    blast_radius_score: dep.blast_radius_score || 0,
    affected_services: dep.affected_services || [],
    changed_packages: dep.changed_packages || [],
    cve_hits: dep.cve_hits || [],
  } : null;

  const tc = r.test_coverage || null;
  const tcNorm = tc ? {
    coverage_delta: tc.coverage_delta || 0,
    regression_risk: tc.regression_risk || 'low',
    untested_functions: tc.untested_functions || [],
    uncovered_paths: (tc.uncovered_paths || []).map(u => ({
      file: u.file_path || u.file || '',
      description: u.suggested_test || u.description || u.uncovered_path || '',
    })),
    scenario_summary: tc.scenario_summary || '',
    hollow_tests: tc.hollow_tests || [],
    method_coverage: (tc.method_coverage || []).map(m => ({
      method: m.method || '',
      file: m.file_path || '',
      is_new: m.is_new || false,
      has_test: m.has_test || false,
      required: m.required_scenarios || [],
      covered: m.covered_scenarios || [],
      missing: m.missing_scenarios || [],
    })),
  } : null;

  const iface = r.interface || null;
  const ifaceNorm = iface ? {
    breaking_changes: (iface.breaking_changes || []).map(b => ({
      type: b.interface_type || b.type || 'REST',
      path: b.path || '',
      break_type: b.break_type || 'changed',
      severity: b.severity || 'high',
    })),
  } : null;

  const sc = r.schema_change || null;
  const scNorm = sc ? {
    has_destructive: sc.has_destructive || false,
    has_irreversible: sc.has_irreversible || false,
    changes: (sc.changes || []).map(c => ({
      change_type: c.change_type || 'alter',
      table: c.table_name || c.table || '',
      severity: c.severity || 'medium',
      reversible: c.reversible !== false,
    })),
  } : null;

  const remNorm = remObj ? {
    fix_suggestions: remObj.fix_suggestions || [],
    code_fixes: (remObj.code_fixes || []).map(f => ({
      title: f.title || '', file: f.file_path || '', category: f.category || '',
      severity: (f.severity || 'medium').toLowerCase(), before: f.before || '',
      after: f.after || '', diff: f.diff || '', explanation: f.explanation || '',
      confidence: f.confidence || 'medium',
    })),
    validation_checklist: remObj.validation_checklist || [],
    deployment_strategy: remObj.deployment_strategy || 'standard',
    executive_summary: remObj.executive_summary || '',
  } : null;

  const se = r.secrets_entropy || null;
  const seNorm = se ? {
    findings: (se.findings||[]).map(f=>({ variable:f.variable||'', value:f.value||'', kind:f.kind||'', entropy:f.entropy||0, severity:(f.severity||'low').toLowerCase(), line:f.line||0, file:f.file_path||'' })),
    high_entropy_count: se.high_entropy_count||0,
    known_prefix_count: se.known_prefix_count||0,
    overall_severity: (se.overall_severity||'low').toLowerCase(),
  } : null;

  const ast = r.ast_analysis || null;
  const astNorm = ast ? {
    findings: (ast.findings||[]).map(f=>({ function:f.function||'', kind:f.kind||'', severity:(f.severity||'low').toLowerCase(), description:f.description||'', suggestion:f.suggestion||'', line:f.line||0, file:f.file_path||'' })),
    avg_complexity: ast.avg_complexity||0,
    max_complexity: ast.max_complexity||0,
  } : null;

  const ta = r.taint_analysis || null;
  const taNorm = ta ? {
    taint_paths: (ta.taint_paths||[]).map(p=>({
      source_var:p.source?.variable||'', source_kind:p.source?.source||'', source_file:p.source?.file_path||'', source_line:p.source?.line||0,
      sink_var:p.sink?.variable||'', sink_kind:p.sink?.sink||'', sink_file:p.sink?.file_path||'', sink_line:p.sink?.line||0,
      cwe:p.cwe||'', severity:(p.severity||'high').toLowerCase(), description:p.description||''
    })),
    sources_found: ta.sources_found||0, sinks_found: ta.sinks_found||0, has_injection: ta.has_injection||false,
  } : null;

  const iac = r.iac_analysis || null;
  const iacNorm = iac ? {
    findings: (iac.findings||[]).map(f=>({ resource:f.resource||'', kind:f.kind||'', severity:(f.severity||'medium').toLowerCase(), description:f.description||'', cis_ref:f.cis_ref||'', file:f.file_path||'', line:f.line||0 })),
    overall_severity: (iac.overall_severity||'low').toLowerCase(),
  } : null;

  const tr = r.temporal_risk || null;
  const trNorm = tr ? {
    hot_files: (tr.hot_files||[]).map(f=>({ file_path:f.file_path||'', change_count:f.change_count||0, avg_risk_score:f.avg_risk_score||0 })),
    risk_trend: tr.risk_trend||'stable', escalating_pattern: tr.escalating_pattern||false, security_erosion: tr.security_erosion||false,
  } : null;

  const ri = r.reference_impact || null;
  const riNorm = ri ? {
    changed_symbols: ri.changed_symbols || [],
    references: (ri.references||[]).map(ref=>({ symbol:ref.symbol||'', file_path:ref.file_path||'', line:ref.line||0, context:ref.context||'', repo:ref.repo||'', depth:ref.depth||1, from_file:ref.from_file||'' })),
    total_references: ri.total_references || (ri.references||[]).length,
    high_impact_files: ri.high_impact_files || [],
    shared_lib_breaks: ri.shared_lib_breaks || [],
    intra_project_risk: ri.intra_project_risk || 'LOW',
    search_backend: ri.search_backend || 'none',
    summary: ri.summary || '',
  } : null;

  const perf = r.performance_impact || null;
  const perfNorm = perf ? {
    summary: perf.summary || '',
    overall_severity: perf.overall_severity || 'low',
    regression_risk: perf.has_complexity_regression || perf.regression_risk || false,
    has_db_risk: perf.has_db_risk || false,
    findings: (perf.findings||[]).map(f=>({ kind:f.category||f.kind||'', severity:(f.severity||'low').toLowerCase(), description:f.description||'', file:f.file_path||f.file||'', line:f.line||0, suggestion:f.suggestion||'' })),
  } : null;

  const priv = r.data_privacy || null;
  const privNorm = priv ? {
    summary: priv.summary || '',
    pii_detected: (priv.pii_findings||priv.findings||[]).length > 0 || (priv.unencrypted_pii_count||0) > 0,
    compliance_violations: priv.logging_violations || priv.compliance_violations || [],
    unencrypted_pii_count: priv.unencrypted_pii_count || 0,
    findings: (priv.pii_findings||priv.findings||[]).map(f=>({ pii_type:f.pii_type||f.kind||'', severity:(f.risk_level||f.severity||'medium').toLowerCase(), description:f.description||'', file:f.file_path||f.file||'', line:f.line||0, recommendation:f.recommendation||'' })),
  } : null;

  const maint = r.maintainability || null;
  const maintNorm = maint ? {
    score: maint.maintainability_score ?? maint.score ?? 100,
    summary: maint.summary || '',
    issues: (maint.issues||[]).map(i=>({ kind:i.kind||'', severity:(i.severity||'low').toLowerCase(), description:i.description||'', file:i.file_path||i.file||'', line:i.line||0, suggestion:i.suggestion||'' })),
  } : null;

  const lic = r.license_compliance || null;
  const licNorm = lic ? {
    has_copyleft: lic.has_copyleft || false,
    has_unknown: lic.has_license_conflict || lic.has_unknown || false,
    summary: lic.summary || '',
    findings: (lic.findings||[]).map(f=>({ package:f.package||'', license:f.detected_license||f.license||'unknown', severity:(f.risk_level||f.severity||'low').toLowerCase(), file:f.file_path||f.file||'', reason:f.description||f.reason||'', recommendation:f.recommendation||'' })),
  } : null;

  const obs = r.observability || null;
  const obsNorm = obs ? {
    observability_score: obs.observability_score ?? Math.max(0, 100 - (obs.logs_removed||0)*8 - (obs.metrics_removed||0)*8 - (obs.unobserved_branches||0)*4),
    logs_removed: obs.logs_removed || 0,
    metrics_removed: obs.metrics_removed || 0,
    summary: obs.summary || '',
    findings: (obs.findings||[]).map(f=>({ kind:f.kind||'', severity:(f.severity||'low').toLowerCase(), description:f.description||'', file:f.file_path||f.file||'', line:f.line||0, suggestion:f.suggestion||'' })),
  } : null;

  const qar = r.qa_scenarios || null;
  const qaNorm = qar ? {
    scenarios: (qar.scenarios||[]).map(s=>({ id:s.id||'', title:s.title||'', type:s.type||'functional', priority:s.priority||'medium', description:s.description||'', steps:s.steps||[], expected_result:s.expected_result||'', affected_files:s.affected_files||[], automation_hint:s.automation_hint||'', preconditions:s.preconditions||[], acceptance_criteria:s.acceptance_criteria||[], test_skeleton:s.test_skeleton||'' })),
    total_scenarios: qar.total_scenarios || (qar.scenarios||[]).length,
    summary: qar.summary || '',
  } : null;

  return {
    gate_decision, overall_risk, risk_score, rationale,
    code_analysis: caNorm, security: secNorm,
    secrets_entropy: seNorm, ast_analysis: astNorm, taint_analysis: taNorm,
    iac_analysis: iacNorm, temporal_risk: trNorm,
    dependency: depNorm, test_coverage: tcNorm, interface: ifaceNorm,
    schema_change: scNorm, qa_scenarios: qaNorm,
    reference_impact: riNorm, performance_impact: perfNorm,
    data_privacy: privNorm, maintainability: maintNorm,
    license_compliance: licNorm, observability: obsNorm,
    risk: riskObj, remediation: remNorm,
    token_usage: r.total_tokens || (Array.isArray(r.token_usage) ? r.token_usage.reduce((s,u)=>s+(u.tokens_used||0),0) : r.token_usage) || 0,
    duration_s: r.duration_s || 0,
    agent_timings: (Array.isArray(r.agent_timings) && r.agent_timings.length)
      ? r.agent_timings
      : (Array.isArray(r.token_usage) ? r.token_usage.map(u=>({ agent:(u.agent&&u.agent.value)||u.agent||'', tokens:u.tokens_used||0, model:u.model||'', duration_s:u.duration_s||0 })) : []),
    errors: r.errors || [],
    request_id: r.request_id || '',
    // Deterministic gate policy + business-capability mapping
    gate_policy_reasons:       r.gate_policy_reasons || [],
    gate_overridden_by_policy: r.gate_overridden_by_policy || false,
    ai_proposed_gate:          r.ai_proposed_gate || '',
    capabilities_affected:     r.capabilities_affected || [],
    consumer_impacts:          r.consumer_impacts || [],
    suppressed_count:          r.suppressed_count || 0,
    suppressed_notes:          r.suppressed_notes || [],
  };
}

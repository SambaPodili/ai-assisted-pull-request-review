import { AGENT_META } from '../state'

// How each agent runs by design — explains why some always finish "instant".
const ENGINE = {
  code_analysis:'llm', security:'llm', risk:'llm', remediation:'llm', qa_scenarios:'llm', test_coverage:'llm',
  ast_analysis:'hybrid', taint_analysis:'hybrid', performance_impact:'hybrid', data_privacy:'hybrid',
  maintainability:'hybrid', observability:'hybrid', interface:'hybrid', dependency:'hybrid',
  secrets_entropy:'static', iac_analysis:'static', temporal_risk:'static', schema_change:'static',
  license_compliance:'static', reference_impact:'static',
}
const ENGINE_STYLE = {
  llm:    { label:'LLM',    color:'#7c3aed', bg:'#f5f0ff', border:'#e2d6fb', tip:'Always uses the LLM — should show a model + time, not "instant".' },
  hybrid: { label:'Hybrid', color:'#0369a1', bg:'#eff8ff', border:'#cde4f7', tip:'Static scan first, LLM enhancement when token budget allows — "instant" if budget is low.' },
  static: { label:'Static', color:'#1a6cf6', bg:'#eef4ff', border:'#cfe0ff', tip:'Deterministic, zero-token — "instant" is expected and correct.' },
}
const keyFor = m => Object.keys(AGENT_META).find(k => AGENT_META[k] === m)

const PHASES = ['Phase 1', 'Phase 1b', 'Phase 2', 'Phase 3']
const PHASE_DESC = {
  'Phase 1':  'Core — runs first',
  'Phase 1b': 'Deep scan — runs in parallel',
  'Phase 2':  'Integration — needs phase-1 results',
  'Phase 3':  'Synthesis — runs last',
}

export default function AgentsView() {
  const byPhase = {}
  Object.values(AGENT_META).forEach(m => { (byPhase[m.phase] = byPhase[m.phase] || []).push(m) })
  const total = Object.keys(AGENT_META).length

  return (
    <div style={{ maxWidth: 920 }}>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title"><i className="ti ti-robot" />Analysis agents</div>
        <div style={{ fontSize: 13, color: '#7a8494', lineHeight: 1.6 }}>
          Every analysis runs <strong>{total} specialised agents</strong> across four phases. Each focuses on one
          concern; their findings feed a deterministic <strong>gate policy</strong> (most-restrictive wins) that
          decides Approve / Hold / Block.
        </div>
        <div style={{ display:'flex', gap:14, flexWrap:'wrap', marginTop:12, fontSize:11.5, color:'#7a8494' }}>
          {Object.values(ENGINE_STYLE).map(s=>(
            <span key={s.label} style={{ display:'inline-flex', alignItems:'center', gap:6 }} title={s.tip}>
              <span style={{ fontSize:10, fontWeight:700, padding:'1px 7px', borderRadius:10, background:s.bg, color:s.color, border:`1px solid ${s.border}` }}>{s.label}</span>
              {s.tip}
            </span>
          ))}
        </div>
      </div>

      {PHASES.filter(p => byPhase[p]).map(p => (
        <div key={p} className="card" style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 12 }}>
            <span style={{ fontSize: 13, fontWeight: 800, color: '#0d1117' }}>{p}</span>
            <span style={{ fontSize: 12, color: '#9fadbf' }}>{PHASE_DESC[p]}</span>
            <span style={{ marginLeft: 'auto', fontSize: 11, color: '#9fadbf' }}>{byPhase[p].length} agent{byPhase[p].length !== 1 ? 's' : ''}</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(380px, 1fr))', gap: 12 }}>
            {byPhase[p].map((m, i) => (
              <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'flex-start', padding: '10px 12px', border: '1px solid #eef0f3', borderRadius: 10 }}>
                <div style={{ width: 34, height: 34, borderRadius: 9, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: `${m.color}18`, color: m.color }}>
                  <i className={`ti ${m.icon}`} style={{ fontSize: 18 }} />
                </div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 700, color: '#0d1117', display:'flex', alignItems:'center', gap:7 }}>
                    {m.label}
                    {(() => { const e=ENGINE_STYLE[ENGINE[keyFor(m)]]; return e?<span title={e.tip} style={{ fontSize:9.5, fontWeight:700, padding:'1px 6px', borderRadius:10, background:e.bg, color:e.color, border:`1px solid ${e.border}` }}>{e.label}</span>:null })()}
                  </div>
                  <div style={{ fontSize: 12, color: '#7a8494', lineHeight: 1.5, marginTop: 2 }}>{m.desc}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

import { AGENT_META, AGENT_ORDER, fmtDuration, agentEngine } from '../state'

const PHASES = ['Phase 1', 'Phase 1b', 'Phase 2', 'Phase 3']

const PHASE_LABELS = {
  'Phase 1': 'Phase 1 — Core',
  'Phase 1b': 'Phase 1b — Deep Scan (parallel)',
  'Phase 2': 'Phase 2 — Integration',
  'Phase 3': 'Phase 3 — Synthesis',
}

export default function LivePipeline({ agentMap = {} }) {
  const maxTok = Math.max(1, ...Object.values(agentMap).map(e => e.tokens || 0))

  const byPhase = {}
  PHASES.forEach(p => { byPhase[p] = [] })
  AGENT_ORDER.forEach(key => {
    const meta = AGENT_META[key]
    if (!meta) return
    const entry = agentMap[key] || { status: 'pending', tokens: 0, duration_s: 0, elapsed_s: 0 }
    ;(byPhase[meta.phase] || byPhase['Phase 2']).push({ key, meta, entry })
  })

  return (
    <div className="pipeline-scroll">
      {PHASES.map(phase => {
        const rows = byPhase[phase]
        if (!rows || !rows.length) return null
        return (
          <div key={phase}>
            <div className="pipeline-phase">{PHASE_LABELS[phase] || phase}</div>
            {phase === 'Phase 1b' ? (
              <div className="pipeline-grid">
                {rows.map(r => <AgentRow key={r.key} {...r} maxTok={maxTok} compact />)}
              </div>
            ) : (
              rows.map(r => <AgentRow key={r.key} {...r} maxTok={maxTok} />)
            )}
          </div>
        )
      })}
    </div>
  )
}

function AgentRow({ key: agentKey, meta, entry, maxTok, compact }) {
  const st = entry.status || 'pending'
  const isRunning = st === 'running'
  const isDone = st === 'done'
  const isFallback = st === 'fallback'
  const isPending = st === 'pending'

  const timeVal = isRunning
    ? <span className="agent-stat" style={{ color: '#1a6cf6' }}>{(entry.elapsed_s || 0).toFixed(1)}s</span>
    : (isDone || isFallback)
      ? <span className="agent-stat" title={(entry.model || '').toLowerCase() === 'static' ? 'Deterministic static scan' : ''}>{fmtDuration(entry.duration_s, entry.model)}</span>
      : <span className="agent-stat dim">—</span>

  const tokVal = isDone && entry.tokens
    ? <span className="agent-tok">{entry.tokens.toLocaleString()} tok</span>
    : isFallback
      ? <span className="agent-tok" style={{ color: '#f59e0b' }}>fallback</span>
      : <span className="agent-tok" style={{ color: '#e8eaed' }}>—</span>

  const tokPct = isDone ? Math.round((entry.tokens / maxTok) * 100) : 0

  return (
    <div className={`agent-row${isPending ? '' : ' ' + st}`} style={compact ? { padding: '6px 10px' } : {}}
      title={meta.desc || meta.label}>
      <div className={`agent-dot ${st}`} />
      <i
        className={`ti ${meta.icon}`}
        style={{
          fontSize: 13, flexShrink: 0,
          color: isDone ? meta.color : isFallback ? '#f59e0b' : isRunning ? '#3b82f6' : '#d1d5db'
        }}
      />
      <span className={`agent-name${isPending ? ' pending' : ''}`} style={{ fontSize: compact ? 11.5 : 13 }}>
        {meta.label}
      </span>
      {isRunning && <span className="agent-badge running">running</span>}
      {isDone && <span className="agent-badge done">done</span>}
      {isFallback && <span className="agent-badge fallback">fallback</span>}
      {(isDone || isFallback) && (() => {
        const eng = agentEngine(entry.model, { fallback: isFallback, completed: true })
        return eng ? <span title={eng.title} style={{ fontSize: 9, fontWeight: 700, padding: '1px 6px', borderRadius: 4, background: eng.bg, color: eng.color, border: `1px solid ${eng.border}`, flexShrink: 0 }}>{eng.label}</span> : null
      })()}
      {timeVal}
      {!compact && tokVal}
      {!compact && (
        <div className="agent-bar">
          {isDone && <div className="agent-bar-fill" style={{ width: `${tokPct}%`, background: meta.color }} />}
          {isRunning && <div className="agent-bar-fill" style={{ width: '30%', background: '#bfdbfe', animation: 'pulse-dot 1.2s infinite' }} />}
        </div>
      )}
    </div>
  )
}

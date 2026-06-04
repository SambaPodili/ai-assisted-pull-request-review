import { useState, useMemo } from 'react'
import { useApp } from '../AppContext'

const GATE_META = {
  APPROVE: { icon: 'ti-circle-check', color: '#3fb950', bg: '#edfaf3', border: '#b5e8cf' },
  HOLD:    { icon: 'ti-alert-triangle', color: '#d29922', bg: '#fff8ec', border: '#fad98a' },
  BLOCK:   { icon: 'ti-ban',           color: '#f85149', bg: '#fff1f1', border: '#f8c0c0' },
}
const gate_m = g => GATE_META[g] || { icon: 'ti-help-circle', color: '#7a8494', bg: '#f7f8fa', border: '#e8eaed' }

function Sparkline({ scores }) {
  if (!scores || scores.length < 2) return null
  const W = 80, H = 24, pad = 2
  const max = Math.max(...scores, 1)
  const pts = scores.map((v, i) => {
    const x = pad + (i / (scores.length - 1)) * (W - pad * 2)
    const y = H - pad - ((v / max) * (H - pad * 2))
    return `${x},${y}`
  }).join(' ')
  const last = scores[scores.length - 1]
  const trend = scores.length >= 2 ? (scores[scores.length - 1] - scores[scores.length - 2]) : 0
  const color = last >= 70 ? '#f85149' : last >= 40 ? '#d29922' : '#3fb950'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <svg width={W} height={H} style={{ overflow: 'visible' }}>
        <polyline points={pts} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
        {(() => {
          const [lx, ly] = pts.split(' ').pop().split(',')
          return <circle cx={lx} cy={ly} r={2.5} fill={color} />
        })()}
      </svg>
      <span style={{ fontSize: 10, color: trend > 0 ? '#f85149' : trend < 0 ? '#3fb950' : '#7a8494' }}>
        {trend > 0 ? '↑' : trend < 0 ? '↓' : '→'} {Math.abs(trend).toFixed(0)}
      </span>
    </div>
  )
}

function RiskBar({ score }) {
  const color = score >= 70 ? '#f85149' : score >= 40 ? '#d29922' : '#3fb950'
  return (
    <div style={{ width: 60, height: 4, background: '#e8eaed', borderRadius: 2, overflow: 'hidden' }}>
      <div style={{ width: `${score}%`, height: '100%', background: color, borderRadius: 2, transition: 'width .4s' }} />
    </div>
  )
}

function groupByDate(items) {
  const groups = {}
  const now = new Date()
  items.forEach(h => {
    const d   = new Date(h.ts)
    const diff = Math.floor((now - d) / 86400000)
    const key = diff === 0 ? 'Today'
              : diff === 1 ? 'Yesterday'
              : diff <= 7  ? 'This week'
              : diff <= 30 ? 'This month'
              : d.toLocaleString('default', { month: 'long', year: 'numeric' })
    if (!groups[key]) groups[key] = []
    groups[key].push(h)
  })
  return groups
}

export default function HistoryView({ showView, showToast }) {
  const { state, update } = useApp()
  const [search, setSearch]   = useState('')
  const [gateFilter, setGate] = useState('all')
  const [confirmClear, setClear] = useState(false)

  const filtered = useMemo(() => {
    let items = state.history || []
    if (gateFilter !== 'all') items = items.filter(h => h.gate === gateFilter)
    if (search.trim()) {
      const q = search.toLowerCase()
      items = items.filter(h =>
        h.repo?.toLowerCase().includes(q) ||
        h.target?.toLowerCase().includes(q) ||
        h.gate?.toLowerCase().includes(q)
      )
    }
    return items
  }, [state.history, search, gateFilter])

  const grouped = useMemo(() => groupByDate(filtered), [filtered])

  // Risk trend over last 10 runs
  const recentScores = (state.history || []).slice(0, 10).map(h => h.score || 0).reverse()

  // Stats
  const total    = (state.history || []).length
  const blocked  = (state.history || []).filter(h => h.gate === 'BLOCK').length
  const approved = (state.history || []).filter(h => h.gate === 'APPROVE').length
  const avgScore = total > 0
    ? Math.round((state.history || []).reduce((s, h) => s + (h.score || 0), 0) / total)
    : 0

  function doClear() {
    update({ history: [] })
    localStorage.removeItem('analysisHistory')
    setClear(false)
    showToast('History cleared', 'info')
  }

  if (total === 0) {
    return (
      <div style={{ maxWidth: 700 }}>
        <div className="card">
          <div className="empty-state">
            <i className="ti ti-history" />
            No analyses run yet<br />
            <span style={{ fontSize: 12, color: '#9fadbf', marginTop: 8, display: 'block' }}>
              Run an analysis from the Analysis target view to see results here.
            </span>
            <button className="btn btn-primary" style={{ marginTop: 16 }} onClick={() => showView('target')}>
              <i className="ti ti-target" /> Go to Analysis target
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div style={{ maxWidth: 780 }}>

      {/* ── Stats row ── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10, marginBottom: 16 }}>
        {[
          ['Total runs', total, '#1a6cf6'],
          ['Approved',   approved, '#3fb950'],
          ['Blocked',    blocked,  '#f85149'],
          ['Avg risk',   avgScore, avgScore >= 70 ? '#f85149' : avgScore >= 40 ? '#d29922' : '#3fb950'],
        ].map(([label, val, color]) => (
          <div key={label} className="metric">
            <div className="metric-label">{label}</div>
            <div className="metric-value" style={{ color, fontSize: 20 }}>{val}</div>
          </div>
        ))}
      </div>

      {/* ── Risk trend sparkline ── */}
      {recentScores.length >= 2 && (
        <div className="card" style={{ padding: '12px 16px', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 16 }}>
          <span style={{ fontSize: 12, color: '#7a8494', whiteSpace: 'nowrap' }}>Risk trend (last {recentScores.length})</span>
          <Sparkline scores={recentScores} />
          <span style={{ fontSize: 11, color: '#9fadbf', marginLeft: 'auto' }}>Latest: <strong>{recentScores[recentScores.length - 1]}</strong>/100</span>
        </div>
      )}

      {/* ── Search + filter bar ── */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 14, flexWrap: 'wrap' }}>
        <div className="search-wrap" style={{ flex: 1, minWidth: 180, marginBottom: 0 }}>
          <i className="ti ti-search" />
          <input type="text" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search repo, target, gate…" />
        </div>
        <div style={{ display: 'flex', gap: 4, background: '#f0f2f5', borderRadius: 8, padding: 3 }}>
          {['all', 'APPROVE', 'HOLD', 'BLOCK'].map(g => (
            <button key={g} onClick={() => setGate(g)} style={{
              border: 'none', cursor: 'pointer', borderRadius: 6, padding: '5px 10px',
              fontSize: 11, fontWeight: gateFilter === g ? 700 : 400,
              background: gateFilter === g ? '#fff' : 'transparent',
              color: gateFilter === g
                ? (g === 'APPROVE' ? '#3fb950' : g === 'BLOCK' ? '#f85149' : g === 'HOLD' ? '#d29922' : '#0d1117')
                : '#7a8494',
              boxShadow: gateFilter === g ? '0 1px 2px rgba(0,0,0,.1)' : 'none',
            }}>
              {g === 'all' ? 'All' : g}
            </button>
          ))}
        </div>
        <span style={{ fontSize: 11, color: '#9fadbf' }}>{filtered.length} of {total}</span>
      </div>

      {filtered.length === 0 && (
        <div className="card"><div className="empty-state"><i className="ti ti-search" />No matches</div></div>
      )}

      {/* ── Grouped items ── */}
      {Object.entries(grouped).map(([group, items]) => (
        <div key={group}>
          <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em', color: '#9fadbf', marginBottom: 6, marginTop: 6 }}>
            {group}
          </div>
          {items.map(h => {
            const m = gate_m(h.gate)
            return (
              <div key={h.id} className="history-item" style={{ gap: 12 }}>
                <div style={{
                  width: 32, height: 32, borderRadius: 8, background: m.bg, border: `1px solid ${m.border}`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                }}>
                  <i className={`ti ${m.icon}`} style={{ fontSize: 16, color: m.color }} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: '#0d1117', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {h.repo}
                  </div>
                  <div style={{ fontSize: 11, color: '#7a8494', marginTop: 2, display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span>{h.target}</span>
                    <span>·</span>
                    <span>{new Date(h.ts).toLocaleString()}</span>
                    {h.risk && <span style={{ textTransform: 'uppercase', fontSize: 10, fontWeight: 600, color: m.color }}>{h.risk}</span>}
                  </div>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4, flexShrink: 0 }}>
                  <span className={`badge badge-${h.gate === 'APPROVE' ? 'green' : h.gate === 'BLOCK' ? 'red' : 'amber'}`}>{h.gate}</span>
                  <RiskBar score={h.score || 0} />
                  <span style={{ fontSize: 10, color: '#9fadbf', fontFamily: 'var(--mono)' }}>{h.score || 0}/100</span>
                </div>
              </div>
            )
          })}
        </div>
      ))}

      {/* ── Footer ── */}
      <div style={{ marginTop: 16, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontSize: 12, color: '#9fadbf' }}>{total} analysis run{total !== 1 ? 's' : ''} stored locally</span>
        {confirmClear ? (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span style={{ fontSize: 12, color: '#b81c1c' }}>Delete all {total} records?</span>
            <button className="btn btn-danger btn-sm" onClick={doClear}><i className="ti ti-trash" />Yes, clear</button>
            <button className="btn btn-sm" onClick={() => setClear(false)}>Cancel</button>
          </div>
        ) : (
          <button className="btn btn-sm" onClick={() => setClear(true)}>
            <i className="ti ti-trash" />Clear history
          </button>
        )}
      </div>
    </div>
  )
}

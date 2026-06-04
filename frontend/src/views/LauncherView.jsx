import { FRAMEWORKS } from '../frameworks'
import Icon from '../components/Icon'

function glow(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || '')
  if (!m) return 'rgba(59,130,246,.38)'
  const [r, g, b] = [1, 2, 3].map(i => parseInt(m[i], 16))
  return `rgba(${r},${g},${b},.38)`
}
function shade(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || '')
  if (!m) return '#6366f1'
  let [r, g, b] = [1, 2, 3].map(i => parseInt(m[i], 16))
  r = Math.round(r * 0.78); g = Math.round(g * 0.74); b = Math.min(255, Math.round(b * 0.92 + 28))
  return `rgb(${r},${g},${b})`
}
const accentVars = (a) => ({ '--fw-accent': a, '--fw-glow': glow(a) })
const iconGrad = (a) => `linear-gradient(135deg, ${a}, ${shade(a)})`

function openFw(fw, onOpen) {
  if (fw.status === 'active') onOpen?.(fw)
  else if (fw.status === 'external' && fw.href) window.open(fw.href, '_blank', 'noopener')
}

/**
 * Home / launcher page — a polished grid of framework cards.
 * `onOpen(framework)` is called when an active framework card is clicked.
 */
export default function LauncherView({ onOpen, dark, setDark }) {
  return (
    <div className="launcher">
      {/* Top bar */}
      <header className="launcher-topbar">
        <div className="launcher-brandmark"><Icon name="stack" size={19} strokeWidth={2} /></div>
        <div className="launcher-topbar-title">Frameworks</div>
        <button
          onClick={() => setDark?.(v => !v)}
          title={`${dark ? 'Light' : 'Dark'} mode`}
          className="launcher-iconbtn"
        >
          <Icon name={dark ? 'sun' : 'moon'} size={17} />
        </button>
      </header>

      {/* Hero */}
      <div className="launcher-hero">
        <div className="launcher-eyebrow"><Icon name="sparkles" size={14} /> AI engineering frameworks</div>
        <h1>Choose a framework</h1>
        <p>Pick a framework to get started. New frameworks appear here automatically as they’re added.</p>
      </div>

      {/* Card grid */}
      <div className="fw-grid">
        {FRAMEWORKS.map(fw => <FrameworkCard key={fw.id} fw={fw} onOpen={onOpen} />)}

        {/* "Add a framework" placeholder */}
        <div className="fw-card fw-card-add">
          <div className="fw-add-icon"><Icon name="plus" size={24} /></div>
          <div className="fw-add-title">More frameworks coming</div>
          <div className="fw-add-sub">
            Register new frameworks in <code>frameworks.js</code> to link them here.
          </div>
        </div>
      </div>
    </div>
  )
}

function FrameworkCard({ fw, onOpen }) {
  const accent = fw.accent || '#3b82f6'
  const soon = fw.status === 'soon'
  const external = fw.status === 'external'
  const clickable = !soon

  return (
    <div
      className={`fw-card${clickable ? ' clickable' : ''}`}
      style={accentVars(accent)}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? () => openFw(fw, onOpen) : undefined}
      onKeyDown={clickable ? (e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openFw(fw, onOpen) } }) : undefined}
    >
      <span className="fw-badge" style={{
        background: soon ? 'rgba(127,127,127,.15)' : `${accent}1a`,
        color: soon ? '#7a8494' : accent,
        border: `1px solid ${soon ? 'rgba(127,127,127,.3)' : accent + '55'}`,
      }}>
        {soon ? 'Coming soon' : external ? 'External ↗' : `v${fw.version || '—'}`}
      </span>

      <div className="fw-iconwrap" style={{ background: iconGrad(accent), boxShadow: `0 8px 22px ${glow(accent)}` }}>
        <Icon name={fw.icon} size={28} strokeWidth={1.7} />
      </div>

      <div>
        <div className="fw-title">{fw.title}</div>
        <div className="fw-tagline" style={{ color: accent }}>{fw.tagline}</div>
      </div>

      <p className="fw-desc">{fw.description}</p>

      {fw.tags?.length > 0 && (
        <div className="fw-tags">
          {fw.tags.map(t => <span key={t} className="fw-tag">{t}</span>)}
        </div>
      )}

      {clickable && (
        <span className="fw-cta">
          {external ? 'Open' : 'Launch'} <Icon name="arrow-right" size={16} strokeWidth={2.2} />
        </span>
      )}
    </div>
  )
}

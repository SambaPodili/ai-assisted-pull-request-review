import { useApp } from '../AppContext'
import { canManageUsers } from '../state'

export default function Sidebar({ view, showView, state, dark, setDark, goHome }) {
  const connected = !!(state.userInfo)
  const name = state.userInfo
    ? (state.userInfo.display_name || state.userInfo.name || state.userInfo.login || state.userInfo.username || state.username || 'Connected')
    : 'Not connected'

  // Gate color for Results badge
  const gate = state.report?.gate_decision
  const gateDot = gate
    ? { APPROVE: '#3fb950', HOLD: '#d29922', BLOCK: '#f85149' }[gate]
    : null

  // Running analysis indicator
  const analysisRunning = state.analysisRequested && !state.report

  // History count
  const histCount = state.history?.length || 0

  return (
    <aside className="sidebar">
      <div
        className="brand"
        onClick={goHome}
        title="Back to all frameworks"
        style={{ cursor: goHome ? 'pointer' : undefined }}
      >
        {/* UOB logo lockup (inline SVG — no external asset, works air-gapped).
            Fixed small size so the sidebar layout is unaffected. textLength forces
            "UOB" to fit fully inside the card so the B never clips. */}
        <svg width="50" height="28" viewBox="0 0 150 84" fill="none" aria-label="UOB" style={{ flexShrink: 0 }}>
          <rect x="4" y="10" width="142" height="64" rx="12" fill="#1B3A8B" />
          <g fill="#E4002B">
            <rect x="18" y="31" width="4" height="26" rx="2" />
            <rect x="26" y="28" width="4" height="29" rx="2" />
            <rect x="34" y="31" width="4" height="26" rx="2" />
            <rect x="42" y="29" width="4" height="28" rx="2" />
            <rect x="15" y="37" width="33" height="4" rx="2" />
          </g>
          <text x="58" y="59" textLength="82" lengthAdjust="spacingAndGlyphs"
                fontFamily="Arial, Helvetica, sans-serif" fontWeight="700" fontSize="34" fill="#ffffff">UOB</text>
        </svg>
        <span className="brand-text">Code Analysis &amp; Review</span>
        {goHome && <i className="ti ti-layout-grid" style={{ marginLeft: 'auto', fontSize: 14, opacity: 0.55 }} title="All frameworks" />}
      </div>

      <div className="sidebar-nav">
      <div className="nav-section">Analysis</div>
      <NavItem id="configure" label="Configure"       icon="ti-plug-connected"  view={view} showView={showView} shortcut="1"
        badge={connected ? <span style={dotStyle('#3fb950')} title="Connected" /> : null} />
      <NavItem id="repos"     label="Repositories"    icon="ti-folder"           view={view} showView={showView} shortcut="2"
        badge={state.primaryRepo ? <span style={dotStyle('#3fb950')} title="Repo selected" /> : null} />
      <NavItem id="target"    label="Analysis target" icon="ti-target"           view={view} showView={showView} shortcut="3" />
      <NavItem id="results"   label="Results"         icon="ti-chart-bar"        view={view} showView={showView} shortcut="4"
        badge={
          analysisRunning
            ? <span style={{...dotStyle('#1a6cf6'), animation: 'pulse-dot 1.2s infinite'}} title="Analysis running…" />
            : gateDot ? <span style={dotStyle(gateDot)} title={`Gate: ${gate}`} /> : null
        }
      />

      <div className="nav-section">History</div>
      <NavItem id="history"  label="Past analyses"  icon="ti-history"      view={view} showView={showView} shortcut="5"
        badge={histCount > 0 ? <CountBadge n={histCount} /> : null} />

      <div className="nav-section">Validation</div>
      <NavItem id="quality"  label="Quality metrics" icon="ti-target-arrow"  view={view} showView={showView} shortcut="6" />
      <NavItem id="insights" label="Insights"        icon="ti-chart-dots-3"  view={view} showView={showView} shortcut="7" />

      <div className="nav-section">Reference</div>
      <NavItem id="agents"   label="Analysis agents" icon="ti-robot"         view={view} showView={showView} shortcut="8" />

      <div className="nav-section">Settings</div>
      <NavItem id="settings" label="Backend config"  icon="ti-settings"      view={view} showView={showView} shortcut="9" />
      {canManageUsers(state) && <NavItem id="admin" label="User management" icon="ti-users-group" view={view} showView={showView} />}
      </div>{/* /sidebar-nav */}

      <div className="sidebar-foot">
      {/* Dark mode lives in the top-right toolbar (Topbar) — no duplicate here. */}
      <UserIdentity state={state} gitName={name} connected={connected} />
      </div>{/* /sidebar-foot */}
    </aside>
  )
}

function UserIdentity({ state, gitName, connected }) {
  const ciaa = state.ciaaPerms
  const loggedIn = !!(ciaa || state.userInfo)
  const displayName = (ciaa && ciaa.name) || gitName || 'Not connected'
  const roleColor = (ciaa && ciaa.role_color) || '#1a56db'
  const roleLabel = (ciaa && (ciaa.role_label || ciaa.primary_role)) || (loggedIn ? null : null)
  const team = ciaa && ciaa.team
  const initials = String(displayName).split(/[\s/._-]+/).filter(Boolean).slice(0, 2)
    .map(s => s[0]?.toUpperCase()).join('') || '?'

  return (
    <div className="user-card" title={ciaa ? `${displayName}${team ? ' · ' + team : ''} — ${ciaa.description || roleLabel || ''}` : 'Not connected'}>
      <div className="user-avatar" style={{ background: loggedIn ? roleColor : '#9fadbf' }}>
        {loggedIn ? initials : <i className="ti ti-user" />}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="user-name">{displayName}</div>
        <div className="user-meta">
          {roleLabel
            ? <><span className="user-role" style={{ background: `${roleColor}18`, color: roleColor, border: `1px solid ${roleColor}44` }}>{roleLabel}</span>{team ? <span className="user-team">{team}</span> : null}</>
            : <span className="user-team">{loggedIn ? 'Connected' : 'Not connected'}</span>}
        </div>
      </div>
      <span className={`conn-dot${connected ? ' ok' : ''}`} style={{ flexShrink: 0 }} />
    </div>
  )
}

function dotStyle(color) {
  return { width: 7, height: 7, borderRadius: '50%', background: color, flexShrink: 0, display: 'inline-block' }
}

function CountBadge({ n }) {
  return (
    <span style={{
      marginLeft: 'auto', fontSize: 10, fontWeight: 700,
      background: '#1a6cf6', color: '#fff',
      borderRadius: 8, padding: '1px 6px', minWidth: 18, textAlign: 'center',
    }}>{n > 99 ? '99+' : n}</span>
  )
}

function NavItem({ id, label, icon, view, showView, badge, shortcut }) {
  return (
    <div
      className={`nav-item${view === id ? ' active' : ''}`}
      onClick={() => showView(id)}
      title={shortcut ? `${label} (${shortcut})` : label}
      style={{ position: 'relative' }}
    >
      <i className={`ti ${icon}`} />
      <span style={{ flex: 1 }}>{label}</span>
      {badge}
    </div>
  )
}

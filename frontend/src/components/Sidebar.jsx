import { useApp } from '../AppContext'

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
        <i className="ti ti-microscope" />
        <span className="brand-text">Impact Analyzer</span>
        {goHome && <i className="ti ti-layout-grid" style={{ marginLeft: 'auto', fontSize: 14, opacity: 0.55 }} title="All frameworks" />}
      </div>

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

      {/* Dark mode toggle */}
      <div style={{ padding: '8px 20px 0' }}>
        <button
          onClick={() => setDark?.(v => !v)}
          title={`Switch to ${dark ? 'light' : 'dark'} mode (D)`}
          style={{
            width: '100%', display: 'flex', alignItems: 'center', gap: 8,
            padding: '7px 0', background: 'none', border: 'none', cursor: 'pointer',
            fontSize: 12, color: '#7a8494', borderRadius: 6,
          }}
        >
          <i className={`ti ${dark ? 'ti-sun' : 'ti-moon'}`} style={{ fontSize: 15 }} />
          {dark ? 'Light mode' : 'Dark mode'}
          <kbd style={{ marginLeft: 'auto', fontSize: 9, background: 'currentColor', color: dark ? '#161b22' : '#fff', borderRadius: 3, padding: '1px 4px', opacity: 0.45 }}>D</kbd>
        </button>
      </div>

      <UserIdentity state={state} gitName={name} connected={connected} />
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

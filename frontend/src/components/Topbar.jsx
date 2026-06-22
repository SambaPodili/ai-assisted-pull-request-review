import BuildBadge from './BuildBadge'

const TITLES = {
  configure: 'Configure provider',
  repos:     'Select repositories',
  target:    'Analysis target',
  results:   'Analysis results',
  history:   'Past analyses',
  settings:  'Backend settings',
  quality:   'Quality metrics',
  insights:  'Team insights',
  agents:    'Analysis agents',
}
const SUBS = {
  configure: 'Connect GitHub or Bitbucket',
  repos:     'Choose primary repo and connected apps',
  target:    'Pick PR, branch diff, or commit',
  results:   'Review gate decision and findings',
  history:   'Previous analysis runs',
  settings:  'Configure backend API connection',
  quality:   'Recall, precision & F1 across all detection agents',
  insights:  'Review queue, risk trends, change heatmap & API cost',
  agents:    'What each of the 20 analysis agents does',
}

export default function Topbar({ view, state, showView, dark, setDark }) {
  return (
    <div className="topbar">
      <div>
        <div className="topbar-title">{TITLES[view] || view}</div>
        <div className="topbar-sub">{SUBS[view] || ''}</div>
      </div>

      <div style={{ marginLeft:'auto', display:'flex', alignItems:'center', gap:8 }}>
        {/* Build version badge (turns amber on FE/BE version mismatch) */}
        <BuildBadge state={state} />

        {/* Logged-in user + role — role is ALWAYS shown (falls back to the default
            role when no CIAA key / skip-auth), so "what role am I?" is answerable
            at a glance on every screen. Name appears when known. */}
        {(() => {
          const perms = state.ciaaPerms
          const roleColor = perms?.role_color || '#1a56db'
          const roleLabel = perms?.role_label || state.ciaaRole || 'Developer'
          const name = perms?.name
          const tip = perms
            ? `${name||''}${perms.team?' · '+perms.team:''} — ${perms.description||roleLabel}`
            : `Role: ${roleLabel} (no CIAA key configured — using the default role)`
          return (
            <span style={{ display:'flex', alignItems:'center', gap:7 }} title={tip}>
              {name && (
                <span style={{ fontSize:12, color:'#4b5563', fontWeight:600, maxWidth:160, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
                  <i className="ti ti-user" style={{ fontSize:12, marginRight:4, color:'#9fadbf' }}/>{name}
                </span>
              )}
              <span style={{
                background: `${roleColor}1a`, color: roleColor, border: `1px solid ${roleColor}44`,
                borderRadius: 5, padding:'2px 8px', fontSize:11, fontWeight:700, whiteSpace:'nowrap',
              }} title={`Your role: ${roleLabel}`}>
                <i className="ti ti-shield-lock" style={{ fontSize:11, marginRight:4 }}/>{roleLabel}
              </span>
            </span>
          )
        })()}

        {/* Backend URL pill */}
        {state.backendUrl && (
          <button
            onClick={() => showView('settings')}
            title="Backend settings"
            style={{
              background:'none', border:'1px solid #e8eaed', borderRadius:6,
              padding:'3px 9px', fontSize:11, color:'#7a8494', cursor:'pointer',
              display:'flex', alignItems:'center', gap:4,
            }}
          >
            <span style={{ width:6, height:6, borderRadius:'50%', background: state.userInfo ? '#3fb950' : '#e8eaed', flexShrink:0 }} />
            {state.backendUrl.replace(/^https?:\/\//, '')}
          </button>
        )}

        {/* Dark mode toggle */}
        <button
          onClick={() => setDark?.(v => !v)}
          title={`${dark ? 'Light' : 'Dark'} mode (D)`}
          style={{ background:'none', border:'1px solid #e8eaed', borderRadius:6, width:30, height:30, display:'flex', alignItems:'center', justifyContent:'center', cursor:'pointer', color:'#7a8494', fontSize:15 }}
        >
          <i className={`ti ${dark ? 'ti-sun' : 'ti-moon'}`} />
        </button>

        {/* Keyboard shortcuts hint */}
        <button
          onClick={() => { const el=document.getElementById('kb-hint'); if(el) el.style.display=el.style.display==='flex'?'none':'flex' }}
          title="Keyboard shortcuts (?)"
          style={{ background:'none', border:'1px solid #e8eaed', borderRadius:6, width:30, height:30, display:'flex', alignItems:'center', justifyContent:'center', cursor:'pointer', color:'#7a8494', fontSize:13 }}
        >
          <i className="ti ti-keyboard" />
        </button>
      </div>

      {/* Keyboard shortcuts overlay */}
      <div id="kb-hint" style={{
        display:'none', position:'fixed', bottom:20, right:20,
        background:'#1a2332', color:'#c9d4e8', borderRadius:12,
        padding:'16px 20px', fontSize:12, zIndex:8000, lineHeight:2,
        boxShadow:'0 8px 32px rgba(0,0,0,.35)', minWidth:240,
        flexDirection:'column',
      }}>
        <div style={{ fontWeight:700, marginBottom:4, fontSize:13, color:'#e8f0ff' }}>⌨️ Keyboard shortcuts</div>
        {[
          ['1–8', 'Jump to view'],
          ['← →', 'Previous / next results tab'],
          ['P',   'Generate PR description'],
          ['C',   'Open reviewer checklist'],
          ['N',   'New analysis'],
          ['D',   'Toggle dark mode'],
          ['?',   'This help'],
          ['Esc', 'Close'],
        ].map(([k, v]) => (
          <div key={k} style={{ display:'flex', gap:8 }}>
            <kbd style={{ background:'#2d3f55', padding:'0 5px', borderRadius:3, minWidth:28, textAlign:'center', fontSize:11 }}>{k}</kbd>
            <span style={{ color:'#9fadbf' }}>{v}</span>
          </div>
        ))}
        <button onClick={() => { document.getElementById('kb-hint').style.display='none' }}
          style={{ marginTop:10, background:'#2d3f55', border:'none', color:'#c9d4e8', borderRadius:6, padding:'4px 12px', cursor:'pointer', fontSize:11, alignSelf:'flex-end' }}>
          Close
        </button>
      </div>
    </div>
  )
}

import { useState } from 'react'
import { useApp } from '../AppContext'
import { isSuperAdmin } from '../state'

// Small "super admin only" lock badge for gated settings cards.
function Lock({ on }) {
  return on ? null : (
    <span style={{ marginLeft: 8, fontSize: 11, fontWeight: 600, color: '#92400e', background: '#fffbeb', border: '1px solid #fcd34d', borderRadius: 10, padding: '1px 8px' }}>
      <i className="ti ti-lock" style={{ fontSize: 11, marginRight: 3 }} />Super admin only
    </span>
  )
}

function PasswordInput({ value, onChange, placeholder }) {
  const [show, setShow] = useState(false)
  return (
    <div style={{ position: 'relative' }}>
      <input type={show ? 'text' : 'password'} value={value} onChange={onChange}
        placeholder={placeholder} style={{ paddingRight: 36 }} />
      <button type="button" onClick={() => setShow(v => !v)} tabIndex={-1}
        style={{ position:'absolute', right:8, top:'50%', transform:'translateY(-50%)', background:'none', border:'none', cursor:'pointer', color:'#7a8494', fontSize:15, padding:0, lineHeight:1 }}>
        <i className={`ti ${show ? 'ti-eye-off' : 'ti-eye'}`} />
      </button>
    </div>
  )
}

function CopyCode({ code }) {
  const [copied, setCopied] = useState(false)
  function copy() {
    navigator.clipboard.writeText(code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1800) })
  }
  return (
    <div style={{ display:'flex', alignItems:'center', gap:6, background:'#f7f8fa', border:'1px solid #e8eaed', borderRadius:6, padding:'6px 10px 6px 12px', marginBottom:4 }}>
      <code style={{ flex:1, fontSize:12, background:'none', border:'none', padding:0, color:'#1a6cf6' }}>{code}</code>
      <button onClick={copy} title="Copy" style={{ background:'none', border:'none', cursor:'pointer', color:'#7a8494', fontSize:13, padding:'2px 4px', borderRadius:4, flexShrink:0 }}>
        <i className={`ti ${copied ? 'ti-check' : 'ti-copy'}`} style={{ color: copied ? '#3fb950' : undefined }} />
      </button>
    </div>
  )
}

export default function SettingsView({ showToast }) {
  const { state, update } = useApp()
  const [url, setUrl]         = useState(state.backendUrl)
  const [key, setKey]         = useState(state.backendKey)
  const [mvnUrl, setMvnUrl]   = useState(state.mavenRepoUrl || '')
  const [mvnAuth, setMvnAuth] = useState(state.mavenRepoAuth || '')
  const [mvnMsg, setMvnMsg]   = useState('')
  const [settingsMsg, setMsg] = useState('')
  const [digestMsg, setDMsg]  = useState('')
  const [purgeRepo, setPurgeRepo] = useState('')
  const [purgeDays, setPurgeDays] = useState('')
  const [purgeMsg, setPMsg]   = useState('')
  const superAdmin = isSuperAdmin(state)
  // The backend URL is editable for super admins OR while NOT yet connected (so a
  // first-time super admin can point the UI at their backend before logging in);
  // once connected as a non-super user it locks.
  const canEditUrl = superAdmin || !state.ciaaPerms

  function saveSettings() {
    const newUrl = url.replace(/\/$/, '')
    update({ backendUrl: newUrl, backendKey: key })
    setMsg('✓ Saved')
    setTimeout(() => setMsg(''), 2000)
  }

  async function testBackend() {
    setMsg('Testing…')
    try {
      const r = await fetch(url.replace(/\/$/, '') + '/health', { signal: AbortSignal.timeout(5000) })
      if (r.ok) {
        const d  = await r.json()
        const llm = d.components?.llm
        // A blank backend env key is fine when a key is supplied in the UI
        // (Configure → AI Model), which is sent per-request. Only warn if BOTH
        // the backend env AND the UI model key are empty.
        const uiKeySet = !!state.modelApiKey
        const llmStr = llm
          ? (llm.key_set
              ? ` · LLM: ${llm.provider} (env key)`
              : uiKeySet
                ? ` · LLM: ${state.modelProvider} (UI key)`
                : ' · ⚠ no LLM key (set it in Configure → AI Model, or in .env)')
          : ''
        setMsg((d.status === 'ok' ? '✓ Connected' : `⚠ ${d.status}`) + llmStr)
        showToast('Backend connected', 'success')
      } else throw new Error('HTTP ' + r.status)
    } catch(e) { setMsg('✗ ' + e.message); showToast(e.message, 'error') }
  }

  async function previewDigest() {
    if (!state.backendUrl) { setDMsg('Configure backend URL first'); return }
    try {
      const h = {}; if (state.backendKey) h['X-API-Key'] = state.backendKey
      const r = await fetch(`${state.backendUrl}/admin/digest/preview?days=30`, { headers: h })
      if (r.status === 403) { setDMsg('Admin key required'); return }
      const html = await r.text()
      const w = window.open('', '_blank'); w.document.write(html); w.document.close()
      setDMsg('Preview opened in new tab')
    } catch(e) { setDMsg('✗ ' + e.message) }
  }

  async function sendDigestNow() {
    if (!state.backendUrl) { setDMsg('Configure backend URL first'); return }
    setDMsg('Sending…')
    try {
      const h = { 'Content-Type': 'application/json' }; if (state.backendKey) h['X-API-Key'] = state.backendKey
      const r = await fetch(`${state.backendUrl}/admin/digest/send?days=1`, { method:'POST', headers:h })
      const d = await r.json().catch(() => ({}))
      if (r.status === 403) { setDMsg('Admin key required'); return }
      if (r.status === 400) { setDMsg('⚠ ' + (d.detail || 'Check SMTP config')); return }
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status))
      setDMsg(`✓ Sent to ${(d.recipients || []).length} recipient(s)`)
    } catch(e) { setDMsg('✗ ' + e.message) }
  }

  async function purge(dryRun, opts = {}) {
    if (!state.backendUrl) { setPMsg('Configure backend URL first'); return }
    const demoOnly = !!opts.demo_only
    if (!demoOnly && !purgeRepo.trim() && !purgeDays) { setPMsg('Enter a repo substring or age (days)'); return }
    if (!dryRun && !window.confirm(demoOnly
        ? 'Delete all demo/test reports (non-UUID ids)? Your real analyses are kept. This cannot be undone.'
        : 'Permanently delete the matching reports? This cannot be undone.')) return
    setPMsg(dryRun ? 'Previewing…' : 'Deleting…')
    try {
      const h = { 'Content-Type': 'application/json' }; if (state.backendKey) h['X-API-Key'] = state.backendKey
      const r = await fetch(`${state.backendUrl}/admin/reports/purge`, {
        method: 'POST', headers: h,
        body: JSON.stringify({
          repo_contains: demoOnly ? '' : purgeRepo.trim(),
          older_than_days: demoOnly ? 0 : (Number(purgeDays) || 0),
          demo_only: demoOnly,
          dry_run: dryRun,
        }),
      })
      const d = await r.json().catch(() => ({}))
      if (r.status === 403) { setPMsg('Admin key required'); return }
      if (r.status === 400) { setPMsg('⚠ ' + (d.detail || 'Provide a filter')); return }
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status))
      setPMsg(dryRun ? `Preview: ${d.would_delete} report(s) match` : `✓ Deleted ${d.deleted} report(s)`)
    } catch(e) { setPMsg('✗ ' + e.message) }
  }

  // ── Export / Import config ─────────────────────────────────────────────────
  function exportConfig() {
    const cfg = {}
    const keys = ['provider','authMode','token','username','workspace','projectKey','baseUrl',
      'backendUrl','backendKey','modelProvider','modelName','modelApiKey','modelBaseUrl','modelApiVer']
    keys.forEach(k => { if (state[k]) cfg[k] = state[k] })
    const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' })
    const a    = document.createElement('a')
    a.href     = URL.createObjectURL(blob)
    a.download = 'ciaa-config.json'
    a.click()
    URL.revokeObjectURL(a.href)
    showToast('Config exported', 'success')
  }

  function importConfig() {
    const input = document.createElement('input')
    input.type  = 'file'
    input.accept = '.json,application/json'
    input.onchange = e => {
      const file = e.target.files[0]
      if (!file) return
      const reader = new FileReader()
      reader.onload = ev => {
        try {
          const cfg = JSON.parse(ev.target.result)
          update(cfg)
          setUrl(cfg.backendUrl || url)
          setKey(cfg.backendKey || key)
          showToast('Config imported — settings updated', 'success')
        } catch { showToast('Invalid JSON file', 'error') }
      }
      reader.readAsText(file)
    }
    input.click()
  }

  return (
    <div style={{ maxWidth: 860 }}>

      {/* ── Backend connection — the URL is a super-admin-only infra setting;
            the API key is each user's own login credential (always editable),
            so non-super users can still authenticate without a lockout. ── */}
      <div className="card">
        <div className="card-title"><i className="ti ti-server" />Backend API connection</div>
        <div className="field">
          <label>Backend URL <Lock on={canEditUrl} /></label>
          <fieldset disabled={!canEditUrl} style={{ border:'none', padding:0, margin:0, minWidth:0, opacity: canEditUrl ? 1 : 0.6 }}>
            <input type="url" value={url} onChange={e => setUrl(e.target.value)} placeholder="http://localhost:8080" />
          </fieldset>
          <div className="field-hint">{canEditUrl
            ? 'The URL where your backend is running.'
            : 'Set by your super admin — ask them to change the backend URL.'}</div>
        </div>
        <div className="field">
          <label>API Key</label>
          <PasswordInput value={key} onChange={e => setKey(e.target.value)} placeholder="Your CIAA API key (from keys.json)" />
          <div className="field-hint">Your personal key — it determines your role (developer / reviewer / super admin).</div>
        </div>
        <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap' }}>
          <button className="btn btn-primary" onClick={saveSettings}><i className="ti ti-device-floppy" />Save</button>
          <button className="btn" onClick={testBackend}><i className="ti ti-plug-connected" />Test connection</button>
          {settingsMsg && <span style={{ fontSize:12, color: settingsMsg.startsWith('✓') ? '#3fb950' : '#b81c1c' }}>{settingsMsg}</span>}
        </div>
      </div>

      {/* ── Maven / Artifactory (for SCA parent-POM resolution) ── */}
      <div className="card">
        <div className="card-title"><i className="ti ti-package" />Maven repository (SCA)</div>
        <div className="field">
          <label>Maven repository URL</label>
          <input type="url" value={mvnUrl} onChange={e => setMvnUrl(e.target.value)} placeholder="https://artifactory.company.com/artifactory/maven-virtual" />
          <div className="field-hint">Your internal Artifactory/Nexus. Used to resolve parent/BOM versions (e.g. Spring Boot) when scanning a <code>pom.xml</code>. Leave blank to use the backend <code>MAVEN_REPO_URL</code> (default Maven Central).</div>
        </div>
        <div className="field">
          <label>Auth token <span style={{ color:'#7a8494', fontWeight:400 }}>(optional)</span></label>
          <PasswordInput value={mvnAuth} onChange={e => setMvnAuth(e.target.value)} placeholder="Bearer xxxxx   or   Basic xxxxx" />
          <div className="field-hint">Sent as the <code>Authorization</code> header to the Maven repo. Include the scheme (<code>Bearer</code> / <code>Basic</code>). Leave blank if the repo is anonymous.</div>
        </div>
        <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap' }}>
          <button className="btn btn-primary" onClick={() => { update({ mavenRepoUrl: mvnUrl.replace(/\/$/, ''), mavenRepoAuth: mvnAuth }); setMvnMsg('✓ Saved'); setTimeout(() => setMvnMsg(''), 2000) }}><i className="ti ti-device-floppy" />Save</button>
          {mvnMsg && <span style={{ fontSize:12, color:'#3fb950' }}>{mvnMsg}</span>}
        </div>
      </div>

      {/* ── Export / Import ── */}
      <div className="card">
        <div className="card-title"><i className="ti ti-package-export" />Config export / import</div>
        <p style={{ fontSize:13, color:'#7a8494', lineHeight:1.6, marginBottom:12 }}>
          Export your entire configuration (Git provider, auth, model settings, backend URL) as a JSON file to share with teammates or back up. Sensitive tokens are included — keep the file secure.
        </p>
        <div style={{ display:'flex', gap:8 }}>
          <button className="btn" onClick={exportConfig}><i className="ti ti-download" />Export config</button>
          <button className="btn" onClick={importConfig}><i className="ti ti-upload" />Import config</button>
        </div>
      </div>

      {/* ── Email digest (super admin only) ── */}
      <div className="card">
        <div className="card-title"><i className="ti ti-mail" />Email daily digest<Lock on={superAdmin} /></div>
        <fieldset disabled={!superAdmin} style={{ border:'none', padding:0, margin:0, minWidth:0, opacity: superAdmin ? 1 : 0.6 }}>
        <div style={{ fontSize:13, color:'#7a8494', lineHeight:1.7, marginBottom:12 }}>
          Sends a daily summary (PRs needing review, blocked count, API spend) to your team.
          Configure SMTP in the backend <code>.env</code>, then test or preview here.
        </div>
        <div style={{ fontSize:12, lineHeight:1.9, color:'#7a8494', background:'#f7f8fa', borderRadius:8, padding:'12px 14px', marginBottom:12 }}>
          <code>SMTP_HOST</code>, <code>SMTP_PORT</code>, <code>SMTP_USER</code>, <code>SMTP_PASSWORD</code><br />
          <code>SMTP_FROM</code>, <code>DIGEST_RECIPIENTS</code> (comma-separated)<br />
          <code>DIGEST_ENABLED=true</code> · <code>DIGEST_SEND_HOUR=8</code> (UTC)
        </div>
        <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap' }}>
          <button className="btn" onClick={previewDigest}><i className="ti ti-eye" />Preview</button>
          <button className="btn btn-primary" onClick={sendDigestNow}><i className="ti ti-send" />Send test now</button>
          {digestMsg && <span style={{ fontSize:12, color:'#7a8494' }}>{digestMsg}</span>}
        </div>
        </fieldset>
      </div>

      {/* ── Purge stored reports (super admin only) ── */}
      <div className="card">
        <div className="card-title"><i className="ti ti-trash" />Purge stored reports<Lock on={superAdmin} /></div>
        <fieldset disabled={!superAdmin} style={{ border:'none', padding:0, margin:0, minWidth:0, opacity: superAdmin ? 1 : 0.6 }}>
        <div style={{ fontSize:13, color:'#7a8494', lineHeight:1.7, marginBottom:12 }}>
          Remove demo/test analyses so Insights reflects only real data. Filter by a repo
          substring and/or age. <strong>Preview</strong> first — delete is permanent. Admin key required.
        </div>

        {/* One-click demo/test cleanup */}
        <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap', marginBottom:12,
            padding:'10px 12px', background:'#f3f7ff', border:'1px solid #dbe7ff', borderRadius:8 }}>
          <i className="ti ti-sparkles" style={{ color:'#1a6cf6' }} />
          <span style={{ fontSize:12.5, color:'#334155', flex:1, minWidth:160 }}>
            <strong>Remove demo / test rows</strong> — clears seed entries (non-UUID ids like <code>test-*</code>, <code>ins0</code>, <code>purge-*</code>); your real analyses are kept.
          </span>
          <button className="btn btn-sm" onClick={()=>purge(true, { demo_only:true })}><i className="ti ti-eye" />Preview</button>
          <button className="btn btn-sm" onClick={()=>purge(false, { demo_only:true })} style={{ borderColor:'#fca5a5', color:'#b91c1c' }}><i className="ti ti-trash" />Clean demo data</button>
        </div>

        <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap', marginBottom:10 }}>
          <input type="text" value={purgeRepo} onChange={e=>setPurgeRepo(e.target.value)}
            placeholder="repo contains… (e.g. test/demo)"
            style={{ flex:1, minWidth:180, border:'1.5px solid #e8eaed', borderRadius:8, padding:'6px 10px', fontSize:12 }} />
          <input type="number" value={purgeDays} onChange={e=>setPurgeDays(e.target.value)}
            placeholder="older than (days)" min="0"
            style={{ width:150, border:'1.5px solid #e8eaed', borderRadius:8, padding:'6px 10px', fontSize:12 }} />
        </div>
        <div style={{ display:'flex', gap:8, alignItems:'center', flexWrap:'wrap' }}>
          <button className="btn" onClick={()=>purge(true)}><i className="ti ti-eye" />Preview</button>
          <button className="btn" onClick={()=>purge(false)} style={{ borderColor:'#fca5a5', color:'#b91c1c' }}><i className="ti ti-trash" />Delete matching</button>
          {purgeMsg && <span style={{ fontSize:12, color:purgeMsg.startsWith('✓')?'#0c7c4b':'#7a8494' }}>{purgeMsg}</span>}
        </div>
        </fieldset>
      </div>

      {/* ── Env vars table ── */}
      <div className="card">
        <div className="card-title"><i className="ti ti-shield-check" />Reliability &amp; alerting env vars</div>
        <div style={{ fontSize:12, lineHeight:2, color:'#7a8494', overflowX:'auto' }}>
          <table style={{ borderCollapse:'collapse', width:'100%' }}>
            <thead>
              <tr style={{ fontSize:11, textTransform:'uppercase', letterSpacing:.06, color:'#9fadbf', borderBottom:'1px solid #e8eaed' }}>
                <th style={{ padding:'4px 8px', textAlign:'left', fontWeight:500 }}>Variable</th>
                <th style={{ padding:'4px 8px', textAlign:'left', fontWeight:500 }}>Default</th>
                <th style={{ padding:'4px 8px', textAlign:'left', fontWeight:500 }}>Description</th>
              </tr>
            </thead>
            <tbody>
              {[
                ['LLM_RETRY_ATTEMPTS',          '3',   'Max retries on rate-limit / transient LLM errors'],
                ['LLM_RETRY_MAX_WAIT_S',         '60',  'Maximum wait (seconds) between retry attempts'],
                ['WEBHOOK_DEDUP_TTL_S',           '300', 'How long (seconds) to suppress webhook duplicates'],
                ['QUALITY_RECALL_ALERT_THRESHOLD','0.0', 'Emit WARNING log when recall drops below this value'],
              ].map(([v, d, desc]) => (
                <tr key={v} style={{ borderBottom:'1px solid #f0f2f5' }}>
                  <td style={{ padding:'5px 8px' }}><code>{v}</code></td>
                  <td style={{ padding:'5px 8px', fontFamily:'JetBrains Mono,monospace' }}>{d}</td>
                  <td style={{ padding:'5px 8px' }}>{desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

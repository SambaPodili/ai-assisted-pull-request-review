import { useState } from 'react'
import { useApp } from '../AppContext'
import { GIT_PROVIDERS, MODEL_PROVIDERS } from '../state'
import { backendBase, fetchCiaaRole } from '../api'

// Reusable password field with show/hide toggle
function PasswordInput({ value, onChange, placeholder, id }) {
  const [show, setShow] = useState(false)
  return (
    <div style={{ position: 'relative' }}>
      <input
        id={id}
        type={show ? 'text' : 'password'}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        style={{ paddingRight: 36 }}
      />
      <button
        type="button"
        onClick={() => setShow(v => !v)}
        tabIndex={-1}
        title={show ? 'Hide' : 'Show'}
        style={{
          position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)',
          background: 'none', border: 'none', cursor: 'pointer',
          color: '#7a8494', fontSize: 15, padding: 0, lineHeight: 1,
        }}
      >
        <i className={`ti ${show ? 'ti-eye-off' : 'ti-eye'}`} />
      </button>
    </div>
  )
}

export default function ConfigureView({ showView, showToast }) {
  const { state, update } = useApp()
  const gp = GIT_PROVIDERS[state.provider] || GIT_PROVIDERS.github
  const isBB  = gp.bb
  const isEnt = gp.enterprise
  const mp    = MODEL_PROVIDERS[state.modelProvider] || MODEL_PROVIDERS.anthropic

  const [connecting, setConnecting] = useState(false)
  const [authStatus, setAuthStatus] = useState(null)  // { ok: bool, msg: string, name?: string }

  async function doConnect() {
    setConnecting(true)
    setAuthStatus(null)
    try {
      const h = { 'Content-Type': 'application/json' }
      if (state.backendKey) h['X-API-Key'] = state.backendKey
      const body = {
        provider:  state.provider,
        base_url:  state.baseUrl || '',
        token:     state.authMode === 'token' ? state.token : '',
        username:  state.authMode === 'password' ? state.username : '',
        password:  state.authMode === 'password' ? (state.password || '') : '',
        workspace: state.workspace || '',
        path:      '',
      }
      const r = await fetch(backendBase(state) + '/api/v1/git/verify', {
        method: 'POST', headers: h, body: JSON.stringify(body),
        signal: AbortSignal.timeout(15000),
      })
      if (r.status === 401) throw new Error('Invalid credentials — check your token or password')
      if (!r.ok) { const t = await r.text(); throw new Error(`${r.status}: ${t.slice(0, 200)}`) }

      const userInfo = await r.json()
      const name = userInfo.login || userInfo.display_name || userInfo.username || state.username
      const ws   = (!state.workspace && userInfo.username) ? userInfo.username : state.workspace

      const roleData = await fetchCiaaRole({ ...state, workspace: ws })
      const patch    = { userInfo, workspace: ws }
      if (roleData) { patch.ciaaRole = roleData.primary_role || 'developer'; patch.ciaaPerms = roleData }
      update(patch)

      const connLabel = GIT_PROVIDERS[state.provider]?.label || state.provider
      const roleLabel = roleData?.role_label || ''
      setAuthStatus({ ok: true, name, connLabel, roleLabel, roleColor: roleData?.role_color })
      showToast(`Connected as ${name} on ${connLabel}`, 'success')
      setTimeout(() => showView('repos'), 700)
    } catch (e) {
      setAuthStatus({ ok: false, msg: e.message })
    }
    setConnecting(false)
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, maxWidth: 960 }}>

      {/* ── LEFT: Git provider ── */}
      <div>
        <div className="card">
          <div className="card-title"><i className="ti ti-plug-connected" />Git Provider</div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 18 }}>
            {Object.entries(GIT_PROVIDERS).map(([k, v]) => (
              <div
                key={k}
                onClick={() => update({ provider: k, authMode: 'token', userInfo: null, repos: [], primaryRepo: null })}
                style={{
                  border: `1.5px solid ${state.provider === k ? '#1a6cf6' : '#e8eaed'}`,
                  background: state.provider === k ? '#eff5ff' : undefined,
                  borderRadius: 8, padding: '11px 12px', cursor: 'pointer',
                  transition: 'all .15s', display: 'flex', alignItems: 'center', gap: 8,
                }}
              >
                <i className={`ti ${v.icon}`} style={{ fontSize: 17, color: state.provider === k ? '#1a6cf6' : '#7a8494', flexShrink: 0 }} />
                <span style={{ fontSize: 12, fontWeight: 600, color: state.provider === k ? '#1a6cf6' : undefined }}>{v.label}</span>
                {state.provider === k && <i className="ti ti-check" style={{ marginLeft: 'auto', fontSize: 13, color: '#1a6cf6' }} />}
              </div>
            ))}
          </div>

          {isEnt && (
            <div className="field">
              <label>{isBB ? 'Bitbucket Server base URL' : 'GitHub Enterprise URL'}</label>
              <input type="url" value={state.baseUrl}
                onChange={e => update({ baseUrl: e.target.value })}
                placeholder={isBB ? 'https://bitbucket.mycompany.com' : 'https://github.mycompany.com'} />
            </div>
          )}

          <div className="card-title" style={{ marginTop: 4 }}><i className="ti ti-lock" />Authentication</div>

          <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
            {['token', 'password'].map(mode => (
              <div key={mode} onClick={() => update({ authMode: mode })} style={{
                flex: 1, padding: 10,
                border: `1.5px solid ${state.authMode === mode ? '#1a6cf6' : '#e8eaed'}`,
                background: state.authMode === mode ? '#eff5ff' : undefined,
                borderRadius: 8, cursor: 'pointer', textAlign: 'center', transition: 'all .15s',
              }}>
                <i className={`ti ${mode === 'token' ? 'ti-key' : 'ti-user'}`}
                  style={{ fontSize: 20, display: 'block', marginBottom: 4, color: state.authMode === mode ? '#1a6cf6' : '#7a8494' }} />
                <span style={{ fontSize: 12, fontWeight: 600, color: state.authMode === mode ? '#1a6cf6' : undefined }}>
                  {mode === 'token' ? (isBB ? 'HTTP Access Token' : 'Access Token') : (isBB ? 'Username + Password' : 'Password')}
                </span>
              </div>
            ))}
          </div>

          {state.authMode === 'token' ? (
            <div className="field">
              <label>{isBB ? 'HTTP Access Token / Personal Access Token' : 'Personal access token'}</label>
              <PasswordInput
                value={state.token}
                onChange={e => update({ token: e.target.value })}
                placeholder={isBB ? (isEnt ? 'HTTP access token from your profile' : 'App password from bitbucket.org') : 'ghp_… or github_pat_…'}
              />
              <div className="field-hint" dangerouslySetInnerHTML={{ __html:
                isBB && isEnt
                  ? '🔑 Bitbucket Server: <strong>Your avatar → Profile → HTTP Access Tokens → Create token</strong><br>Needs: Repository Read, Project Read'
                  : isBB
                    ? '🔑 <a href="https://bitbucket.org/account/settings/app-passwords/new" target="_blank">Create App Password ↗</a> — needs Repositories: Read, Pull requests: Read'
                    : isEnt
                      ? `🔑 Create at <strong>${state.baseUrl || 'your-server'}/settings/tokens/new</strong> — needs <code>repo</code> scope`
                      : '🔑 <a href="https://github.com/settings/tokens/new" target="_blank">Create Personal Access Token ↗</a> — needs <code>repo</code>, <code>read:org</code>'
              }} />
            </div>
          ) : (
            <div className="two-col">
              <div className="field">
                <label>Username</label>
                <input type="text" value={state.username} onChange={e => update({ username: e.target.value })} placeholder="username" />
              </div>
              <div className="field">
                <label>{isBB && isEnt ? 'Password' : isBB ? 'App password' : 'Password'}</label>
                <PasswordInput value={state.password || ''} onChange={e => update({ password: e.target.value })} placeholder="••••••••" />
              </div>
            </div>
          )}

          {isBB && !isEnt && (
            <div className="field">
              <label>Workspace slug</label>
              <input type="text" value={state.workspace} onChange={e => update({ workspace: e.target.value })} placeholder="my-workspace" />
              <div className="field-hint">Your workspace slug from bitbucket.org/<strong>workspace</strong>/</div>
            </div>
          )}
          {isBB && isEnt && (
            <div className="field">
              <label>Project Key <span style={{ fontWeight: 400, color: '#7a8494' }}>(optional)</span></label>
              <input type="text" value={state.projectKey}
                onChange={e => update({ projectKey: e.target.value.toUpperCase() })}
                placeholder="e.g. BANK — leave blank for all" />
            </div>
          )}

          {/* Status messages — pure React state, no DOM mutation */}
          {authStatus && (
            authStatus.ok ? (
              <div style={{ display:'flex', alignItems:'center', gap:8, padding:'10px 12px', background:'#edfaf3', border:'1px solid #b5e8cf', borderRadius:8, marginBottom:12, fontSize:13, color:'#0c7c4b' }}>
                <i className="ti ti-circle-check" />
                Connected as <strong>{authStatus.name}</strong>
                {authStatus.roleLabel && (
                  <span style={{ background:`${authStatus.roleColor||'#1a56db'}1a`, color:authStatus.roleColor||'#1a56db', border:`1px solid ${authStatus.roleColor||'#1a56db'}44`, borderRadius:4, padding:'1px 7px', fontSize:11, fontWeight:700, marginLeft:4 }}>
                    {authStatus.roleLabel}
                  </span>
                )}
                <span style={{ color:'#7a8494', marginLeft:4 }}>on {authStatus.connLabel}</span>
              </div>
            ) : (
              <div className="err-msg" style={{ marginBottom: 12 }}>
                <i className="ti ti-alert-circle" style={{ flexShrink:0, marginTop:1 }} />
                <div>
                  <strong>Connection failed</strong><br />{authStatus.msg}<br />
                  <span style={{ fontSize:11, opacity:.8 }}>Make sure your backend is running at <strong>{state.backendUrl || 'http://localhost:8080'}</strong></span>
                </div>
              </div>
            )
          )}

          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <button className="btn btn-primary" onClick={doConnect} disabled={connecting}>
              {connecting
                ? <><span className="spinner" style={{ width: 14, height: 14, borderWidth: 2 }} /> Verifying…</>
                : <><i className="ti ti-plug-connected" /> Connect &amp; verify</>}
            </button>
            <div style={{ fontSize: 11, color: '#7a8494', display: 'flex', alignItems: 'center', gap: 5 }}>
              <i className="ti ti-shield-lock" style={{ color: '#0c7c4b', fontSize: 14 }} />
              All API calls proxied through backend
            </div>
          </div>
        </div>
      </div>

      {/* ── RIGHT: AI Model ── */}
      <div>
        <div className="card">
          <div className="card-title"><i className="ti ti-cpu" />AI Model</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 14 }}>
            {Object.entries(MODEL_PROVIDERS).map(([k, v]) => (
              <div key={k}
                onClick={() => update({ modelProvider: k, modelName: v.models[0] || '', modelBaseUrl: k === 'ollama' ? 'http://localhost:11434' : '' })}
                style={{
                  border: `1.5px solid ${state.modelProvider === k ? '#1a6cf6' : '#e8eaed'}`,
                  background: state.modelProvider === k ? '#eff5ff' : undefined,
                  borderRadius: 8, padding: 10, cursor: 'pointer', transition: 'all .15s', textAlign: 'center',
                }}>
                <div style={{ fontSize: 18, marginBottom: 3 }}>{v.icon}</div>
                <div style={{ fontSize: 11, fontWeight: 600, color: state.modelProvider === k ? '#1a6cf6' : undefined }}>{v.label}</div>
              </div>
            ))}
          </div>
          <div style={{ background: '#f7f8fa', border: '1px solid #e8eaed', borderRadius: 8, padding: 14 }}>
            {mp.models.length > 0 ? (
              <div className="field" style={{ marginBottom: 10 }}>
                <label>Model</label>
                <select value={state.modelName} onChange={e => update({ modelName: e.target.value })}>
                  {mp.models.map(m => <option key={m} value={m}>{m}</option>)}
                </select>
              </div>
            ) : (
              <div className="field" style={{ marginBottom: 10 }}>
                <label>Model name</label>
                <input type="text" value={state.modelName} onChange={e => update({ modelName: e.target.value })} />
              </div>
            )}
            {mp.needsKey && (
              <div className="field" style={{ marginBottom: 10 }}>
                <label>API key</label>
                <PasswordInput value={state.modelApiKey} onChange={e => update({ modelApiKey: e.target.value })} placeholder={mp.keyPlaceholder} />
              </div>
            )}
            {mp.needsUrl && (
              <div className="field" style={{ marginBottom: 0 }}>
                <label>{state.modelProvider === 'azure_openai' ? 'Azure endpoint' : 'Base URL'}</label>
                <input type="url" value={state.modelBaseUrl} onChange={e => update({ modelBaseUrl: e.target.value })} placeholder={mp.urlPlaceholder || ''} />
              </div>
            )}
            <div className="field-hint" style={{ marginTop: 8 }} dangerouslySetInnerHTML={{ __html: mp.hint || '' }} />
          </div>
        </div>
      </div>
    </div>
  )
}

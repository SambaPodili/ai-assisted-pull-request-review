import { useState, useEffect } from 'react'
import { useApp } from '../AppContext'
import { backendBase, backendHeaders, backendGet, backendPost, gitCfg } from '../api'

const ROLE_LABEL = { super_admin:'Super Admin', admin:'Admin', analyst:'Analyst', reviewer:'Reviewer', developer:'Developer', auditor:'Auditor', ci_system:'CI System' }
const ROLE_COLOR = { super_admin:'#4c1d95', admin:'#7c3aed', analyst:'#0ea5e9', reviewer:'#059669', developer:'#1a56db', auditor:'#9fadbf', ci_system:'#f59e0b' }

function RoleChip({ role }) {
  const c = ROLE_COLOR[role] || '#7a8494'
  return <span style={{fontSize:11,fontWeight:700,padding:'2px 8px',borderRadius:9,background:`${c}1a`,color:c,border:`1px solid ${c}55`}}>{ROLE_LABEL[role]||role}</span>
}

export default function AdminView({ active, showToast }) {
  const { state } = useApp()
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const [form, setForm] = useState({ name:'', team:'', role:'developer', user_id:'' })
  const [bbUser, setBbUser] = useState('')
  const [looking, setLooking] = useState(false)
  const [newKey, setNewKey] = useState(null)   // { api_key, user } — shown ONCE
  const [copied, setCopied] = useState(false)
  const [audit, setAudit] = useState([])

  async function load() {
    if (!state.backendUrl) { setErr('Configure a backend URL in Settings first.'); return }
    setLoading(true); setErr('')
    try {
      setData(await backendGet(state, '/admin/users'))
      try { setAudit((await backendGet(state, '/admin/users/audit?limit=50')).events || []) } catch (_) {}
    }
    catch (e) { setErr(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { if (active) load() }, [active, state.backendUrl, state.backendKey])

  const creatable = (data?.creatable_roles && data.creatable_roles.length) ? data.creatable_roles : ['developer','reviewer']
  useEffect(() => { if (!creatable.includes(form.role)) setForm(f => ({ ...f, role: creatable[0] })) }, [data])

  async function lookupBitbucket() {
    const u = bbUser.trim()
    if (!u) return
    setLooking(true)
    try {
      const d = await backendPost(state, '/api/v1/git/user', { cfg: gitCfg(state), username: u })
      if (d.found) {
        setForm(f => ({ ...f, name: d.display_name || u, user_id: d.slug || u }))
        showToast(`Mapped: ${d.display_name} (${d.slug})`, 'success')
      } else {
        showToast(`No git user found for "${u}"`, 'error')
      }
    } catch (e) { showToast(e.message, 'error') } finally { setLooking(false) }
  }

  async function create() {
    if (!form.name.trim()) { showToast('Name is required', 'error'); return }
    try {
      const d = await backendPost(state, '/admin/users', {
        name: form.name.trim(), team: form.team.trim(), roles: [form.role], user_id: form.user_id.trim() })
      setNewKey(d); setCopied(false)
      setForm({ name:'', team:'', role: creatable[0] || 'developer', user_id:'' }); setBbUser('')
      load()
    } catch (e) { showToast(e.message, 'error') }
  }

  async function method(path, opts) {
    const r = await fetch(backendBase(state) + path, { headers: backendHeaders(state), ...opts })
    if (!r.ok) { const j = await r.json().catch(()=>({})); throw new Error(j.detail || `HTTP ${r.status}`) }
    return r.json()
  }
  async function revoke(u) {
    if (!window.confirm(`Revoke ${u.name || u.key_prefix}? Their API key stops working immediately.`)) return
    try { await method(`/admin/users/${u.id}`, { method: 'DELETE' }); showToast('User revoked', 'success'); load() }
    catch (e) { showToast(e.message, 'error') }
  }
  async function changeRole(u, role) {
    try { await method(`/admin/users/${u.id}`, { method: 'PATCH', body: JSON.stringify({ roles: [role] }) }); showToast('Role updated', 'success'); load() }
    catch (e) { showToast(e.message, 'error') }
  }

  const users = data?.users || []

  return (
    <div className="view-wrap" style={{maxWidth:1000,margin:'0 auto'}}>
      <div style={{marginBottom:18}}>
        <h2 style={{margin:0,fontSize:20,fontWeight:700}}>User management</h2>
        <p style={{margin:'4px 0 0',fontSize:13,color:'#7a8494'}}>Add users and assign roles instead of hand-editing the key file. You can create: {creatable.map(r=><RoleChip key={r} role={r}/>).reduce((a,c)=>[a,' ',c])}.</p>
      </div>

      {err && <div className="err-msg" style={{marginBottom:14}}><i className="ti ti-alert-circle"/> {err}</div>}

      {/* One-time API key reveal */}
      {newKey && (
        <div className="card" style={{marginBottom:16,border:'1.5px solid #16a34a',background:'#f0fdf4'}}>
          <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:8}}>
            <i className="ti ti-key" style={{color:'#166534'}}/>
            <strong style={{color:'#166534'}}>API key for {newKey.user?.name || 'new user'} — copy it now</strong>
          </div>
          <div style={{fontSize:12,color:'#3a4452',marginBottom:8}}>This key is shown <strong>only once</strong> and cannot be recovered. Share it with the user securely.</div>
          <div style={{display:'flex',gap:8,alignItems:'center'}}>
            <code style={{flex:1,fontSize:13,background:'#0d1117',color:'#7ee787',padding:'8px 12px',borderRadius:6,overflowX:'auto'}}>{newKey.api_key}</code>
            <button className="btn" onClick={()=>{navigator.clipboard?.writeText(newKey.api_key);setCopied(true)}}><i className="ti ti-copy"/> {copied?'Copied':'Copy'}</button>
            <button className="btn" onClick={()=>setNewKey(null)}>Done</button>
          </div>
        </div>
      )}

      {/* Add user */}
      <div className="card" style={{marginBottom:16}}>
        <div className="section-heading"><i className="ti ti-user-plus"/>Add a user</div>
        {/* Map from a Bitbucket / git user: name ← displayName, user id ← slug */}
        <div style={{display:'flex',gap:8,alignItems:'flex-end',marginBottom:12,paddingBottom:12,borderBottom:'1px solid #eef0f3'}}>
          <label style={{display:'flex',flexDirection:'column',gap:4,fontSize:12,color:'#5b6675'}}>Bitbucket / git username
            <input value={bbUser} onChange={e=>setBbUser(e.target.value)} onKeyDown={e=>e.key==='Enter'&&lookupBitbucket()} placeholder="unc" style={inp}/></label>
          <button className="btn" onClick={lookupBitbucket} disabled={looking||!bbUser.trim()} style={{height:34}}>
            <i className="ti ti-search"/> {looking?'Looking up…':'Look up & map'}</button>
          <span style={{fontSize:11,color:'#9fadbf',marginBottom:9}}>maps <strong>displayName → name</strong> and <strong>slug → user id</strong> from the connected git provider</span>
        </div>
        <div style={{display:'flex',gap:10,flexWrap:'wrap',alignItems:'flex-end'}}>
          <label style={{display:'flex',flexDirection:'column',gap:4,fontSize:12,color:'#5b6675'}}>Name (displayName)
            <input value={form.name} onChange={e=>setForm(f=>({...f,name:e.target.value}))} placeholder="Samba" style={inp}/></label>
          <label style={{display:'flex',flexDirection:'column',gap:4,fontSize:12,color:'#5b6675'}}>User ID (slug)
            <input value={form.user_id} onChange={e=>setForm(f=>({...f,user_id:e.target.value}))} placeholder="unc" style={inp}/></label>
          <label style={{display:'flex',flexDirection:'column',gap:4,fontSize:12,color:'#5b6675'}}>Team
            <input value={form.team} onChange={e=>setForm(f=>({...f,team:e.target.value}))} placeholder="Payments" style={inp}/></label>
          <label style={{display:'flex',flexDirection:'column',gap:4,fontSize:12,color:'#5b6675'}}>Role
            <select value={form.role} onChange={e=>setForm(f=>({...f,role:e.target.value}))} style={inp}>
              {creatable.map(r=><option key={r} value={r}>{ROLE_LABEL[r]||r}</option>)}
            </select></label>
          <button className="btn btn-primary" onClick={create} style={{height:34}}><i className="ti ti-plus"/> Create user</button>
        </div>
      </div>

      {/* Users table */}
      <div className="card">
        <div style={{display:'flex',alignItems:'center',gap:10}}>
          <div className="section-heading" style={{marginBottom:0}}><i className="ti ti-users"/>Users ({users.length})</div>
          <button className="btn" style={{marginLeft:'auto',fontSize:12}} onClick={load} disabled={loading}>{loading?'…':'Refresh'}</button>
        </div>
        <table style={{width:'100%',borderCollapse:'collapse',fontSize:13,marginTop:10}}>
          <thead><tr style={{color:'#9fadbf',fontSize:10,textTransform:'uppercase',textAlign:'left'}}>
            <th style={th}>Name</th><th style={th}>User ID</th><th style={th}>Team</th><th style={th}>Role(s)</th><th style={th}>Key</th><th style={th}>Source</th><th style={th}>Created by</th><th style={th}></th>
          </tr></thead>
          <tbody>
            {users.length===0 && <tr><td colSpan={8} style={{padding:14,color:'#9fadbf'}}>No users yet.</td></tr>}
            {users.map((u,i)=>{
              const managed = u.source==='managed'
              return (
                <tr key={u.id||i} style={{borderTop:'1px solid #edeef1'}}>
                  <td style={td}>{u.name||'—'}</td>
                  <td style={{...td,fontFamily:'var(--mono)',fontSize:12,color:'#5b6675'}}>{u.user_id||'—'}</td>
                  <td style={td}>{u.team||'—'}</td>
                  <td style={td}>
                    {managed && creatable.length
                      ? <select value={(u.roles||[])[0]} onChange={e=>changeRole(u,e.target.value)} style={{...inp,padding:'3px 6px',fontSize:12}}>
                          {[...new Set([...(creatable||[]), ...(u.roles||[])])].map(r=><option key={r} value={r}>{ROLE_LABEL[r]||r}</option>)}
                        </select>
                      : <span style={{display:'flex',gap:5,flexWrap:'wrap'}}>{(u.roles||[]).map(r=><RoleChip key={r} role={r}/>)}</span>}
                  </td>
                  <td style={{...td,fontFamily:'var(--mono)',fontSize:11,color:'#7a8494'}}>{u.key_prefix||'—'}</td>
                  <td style={td}><span style={{fontSize:10,fontWeight:700,padding:'1px 7px',borderRadius:8,background:managed?'#eef4ff':'#f3f4f6',color:managed?'#1e40af':'#7a8494'}}>{managed?'managed':'file'}</span></td>
                  <td style={{...td,fontSize:11,color:'#7a8494'}}>{u.created_by||'—'}</td>
                  <td style={td}>{managed
                    ? <button className="btn" style={{fontSize:11,padding:'3px 9px',color:'#b91c1c'}} onClick={()=>revoke(u)}>Revoke</button>
                    : <span style={{fontSize:10,color:'#c2c8d0'}} title="From keys.json — edit the file to change">file key</span>}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Audit trail */}
      <div className="card" style={{marginTop:16}}>
        <div className="section-heading"><i className="ti ti-history"/>Audit trail</div>
        <div style={{fontSize:11,color:'#9fadbf',marginBottom:8}}>Every user-management action is permanently logged.</div>
        {audit.length===0
          ? <div style={{fontSize:12,color:'#9fadbf'}}>No actions recorded yet.</div>
          : <div style={{display:'flex',flexDirection:'column',gap:6}}>
              {audit.map((e,i)=>{
                const c = { created:['#166534','#f0fdf4','＋ created'], revoked:['#991b1b','#fff1f2','✕ revoked'], updated:['#92400e','#fffbeb','✎ updated'] }[e.action] || ['#7a8494','#f3f4f6',e.action]
                return (
                  <div key={i} style={{display:'flex',alignItems:'center',gap:8,fontSize:12,padding:'6px 10px',border:'1px solid #f0f2f5',borderRadius:7,flexWrap:'wrap'}}>
                    <span style={{fontSize:10,fontWeight:700,padding:'1px 8px',borderRadius:8,background:c[1],color:c[0]}}>{c[2]}</span>
                    <span style={{color:'#3a4452'}}><strong>{e.actor||'?'}</strong> {e.action} <strong>{e.target||e.target_id}</strong>{(e.roles||[]).length?` as ${(e.roles||[]).map(r=>ROLE_LABEL[r]||r).join(', ')}`:''}</span>
                    <span style={{marginLeft:'auto',fontSize:11,color:'#9fadbf',fontFamily:'var(--mono)'}}>{new Date(e.ts).toLocaleString()}</span>
                  </div>
                )
              })}
            </div>}
      </div>
    </div>
  )
}

const inp = { fontSize:13, padding:'7px 9px', border:'1px solid #e3e7ee', borderRadius:6, minWidth:130 }
const th  = { padding:'4px 6px' }
const td  = { padding:'7px 6px', verticalAlign:'middle' }

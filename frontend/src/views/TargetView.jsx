import { useState, useEffect } from 'react'
import { useApp } from '../AppContext'
import StepsRow from '../components/StepsRow'
import { repoName, shortName, prNum, prTitle, prHead, prBase, prAuthor, branchName, commitSha, commitMsg, commitAuthor,
         AGENT_META, AGENT_ORDER, AGENT_PRESETS, AGENT_PRESET_META } from '../state'
import { backendPost, gitCfg } from '../api'
import { scanUserInstructions } from '../promptGuard'

const USER_INSTRUCTIONS_MAX_CHARS = 1500   // mirrors config.settings.USER_INSTRUCTIONS_MAX_CHARS

export default function TargetView({ active, showToast }) {
  const { state, update } = useApp()
  const [loadState, setLoadState] = useState({ pr: null, branch: null, commit: null })

  useEffect(() => {
    if (active && state.primaryRepo && loadState[state.targetType] === null) {
      loadTargetData(state.targetType)
    }
  }, [active, state.targetType, state.primaryRepo])

  async function loadTargetData(t) {
    setLoadState(prev => ({ ...prev, [t]: true }))
    try {
      const slug = repoName(state.primaryRepo)
      if (t === 'pr') {
        const d = await backendPost(state, `/api/v1/git/prs/${encodeURIComponent(slug)}`, gitCfg(state))
        update({ prs: d.prs || [] })
      } else if (t === 'branch') {
        const d = await backendPost(state, `/api/v1/git/branches/${encodeURIComponent(slug)}`, gitCfg(state))
        const branches = d.branches || []
        update({ branches })
        if (!state.targetBranch) {
          const names = branches.map(b => branchName(b))
          update({ targetBranch: names.find(n=>n==='main')||names.find(n=>n==='master')||names[0]||'' })
        }
      } else {
        const d = await backendPost(state, `/api/v1/git/commits/${encodeURIComponent(slug)}`, gitCfg(state))
        update({ commits: d.commits || [] })
      }
      setLoadState(prev => ({ ...prev, [t]: false }))
    } catch (e) {
      setLoadState(prev => ({ ...prev, [t]: e.message || 'Unknown error' }))
    }
  }

  function setTargetType(t) {
    update({ targetType: t, selectedPR: null })
    if (loadState[t] === null) loadTargetData(t)
  }

  if (!state.primaryRepo) {
    return (
      <div><StepsRow active={2}/>
        <div className="err-msg"><i className="ti ti-alert-circle"/>Select a primary repository first.</div>
      </div>
    )
  }

  return (
    <div>
      <StepsRow active={2}/>
      <div className="card">
        <div className="card-title"><i className="ti ti-target"/>Target — {repoName(state.primaryRepo)}</div>
        <div style={{display:'flex',gap:0,borderBottom:'1px solid #e8eaed',marginBottom:18}}>
          {[['pr','ti-git-pull-request','Pull request'],['branch','ti-git-branch','Branch diff'],['commit','ti-git-commit','Commit']].map(([t,icon,label])=>(
            <button key={t} className={`tab ${state.targetType===t?'active':''}`} onClick={()=>setTargetType(t)}>
              <i className={`ti ${icon}`}/> {label}
            </button>
          ))}
        </div>
        <TargetBody t={state.targetType} loadState={loadState[state.targetType]} state={state} update={update}
          onRetry={()=>{setLoadState(prev=>({...prev,[state.targetType]:null}));loadTargetData(state.targetType)}}/>
      </div>

      {/* Scan depth */}
      <div className="card">
        <label style={{display:'flex',alignItems:'flex-start',gap:10,cursor:'pointer'}}>
          <input type="checkbox" checked={!!state.deepScan} onChange={e=>update({deepScan:e.target.checked})}
            style={{marginTop:3,cursor:'pointer',width:16,height:16,flexShrink:0}}/>
          <div>
            <div style={{fontSize:13,fontWeight:700,color:'#0d1117',display:'flex',alignItems:'center',gap:6}}>
              <i className="ti ti-microscope" style={{color:'#1a6cf6'}}/> Deep scan — analyse every changed file
            </div>
            <div style={{fontSize:12,color:'#7a8494',marginTop:3,lineHeight:1.5}}>
              For large or critical PRs (50–100+ files). Runs security & code analysis over <strong>all</strong> files in
              batches so nothing is sampled out — slower and uses more tokens. Leave off for fast, prioritised review.
            </div>
          </div>
        </label>
      </div>

      <AnalysisScopeCard state={state} update={update}/>

      <PriorityPromptCard state={state} update={update}/>

      {state.connectedRepos.length > 0 && (
        <div className="card">
          <div className="card-title"><i className="ti ti-topology-star-3"/>Connected repos in scope</div>
          <div className="chips">{state.connectedRepos.map(r=><div key={repoName(r)} className="chip"><i className="ti ti-git-branch" style={{fontSize:12}}/>{shortName(r)}</div>)}</div>
          <div style={{fontSize:12,color:'#7a8494'}}>These repos will be included in dependency blast radius and interface contract analysis.</div>
        </div>
      )}
    </div>
  )
}

const PRESET_ORDER = ['fast', 'standard', 'thorough']

function AnalysisScopeCard({ state, update }) {
  const preset  = state.agentPreset || 'thorough'
  const active  = state.customAgents ?? AGENT_PRESETS[preset]
  const isCustom = state.customAgents !== null

  function choosePreset(key) {
    update({ agentPreset: key, customAgents: null })
  }

  function toggleAgent(key) {
    const base = state.customAgents ?? AGENT_PRESETS[preset]
    let next = base.includes(key) ? base.filter(k => k !== key) : [...base, key]
    // remediation needs risk's assessment — force it along whenever remediation is on.
    if (next.includes('remediation') && !next.includes('risk')) next = [...next, 'risk']
    update({ customAgents: next })
  }

  const byPhase = {}
  AGENT_ORDER.forEach(k => { const m = AGENT_META[k]; (byPhase[m.phase] = byPhase[m.phase] || []).push(k) })

  return (
    <div className="card">
      <div className="card-title"><i className="ti ti-adjustments"/>Analysis scope</div>
      <div style={{fontSize:12,color:'#7a8494',marginBottom:12,lineHeight:1.5}}>
        Choose how many agents run. Fewer agents means a faster, cheaper review; Thorough is today's full-depth default.
      </div>
      <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
        {PRESET_ORDER.map(key => {
          const m = AGENT_PRESET_META[key]
          const n = AGENT_PRESETS[key].length
          const on = !isCustom && preset === key
          return (
            <button key={key} type="button" onClick={()=>choosePreset(key)}
              className={`btn btn-sm ${on ? 'btn-primary' : ''}`}
              style={{flexDirection:'column',alignItems:'flex-start',gap:2,padding:'8px 14px',height:'auto',textAlign:'left'}}>
              <span style={{fontWeight:700,fontSize:12.5}}>{m.label} <span style={{fontWeight:500,opacity:.7}}>({n})</span></span>
              <span style={{fontSize:11,opacity:.8,fontWeight:400,whiteSpace:'normal'}}>{m.desc}</span>
            </button>
          )
        })}
      </div>

      {isCustom && (
        <div style={{fontSize:11.5,color:'#1a6cf6',marginTop:10,display:'flex',alignItems:'center',gap:6}}>
          <i className="ti ti-adjustments-alt"/> Custom selection — {active.length} agent{active.length!==1?'s':''}
          <button type="button" className="btn btn-sm" style={{marginLeft:'auto'}} onClick={()=>update({customAgents:null})}>Reset to preset</button>
        </div>
      )}

      {!active.includes('risk') && (
        <div style={{fontSize:11.5,color:'#b45309',marginTop:10,display:'flex',gap:6,alignItems:'flex-start'}}>
          <i className="ti ti-alert-triangle" style={{marginTop:1,flexShrink:0}}/>
          Gate decision will rely only on deterministic policy rules — no AI risk synthesis or rationale.
        </div>
      )}

      <details style={{marginTop:12}}>
        <summary style={{fontSize:12,fontWeight:600,color:'#1a6cf6',cursor:'pointer',userSelect:'none'}}>Advanced: customize agents</summary>
        <div style={{marginTop:10,display:'flex',flexDirection:'column',gap:12}}>
          {Object.entries(byPhase).map(([phase, keys]) => (
            <div key={phase}>
              <div style={{fontSize:10.5,fontWeight:700,color:'#9fadbf',textTransform:'uppercase',letterSpacing:'.04em',marginBottom:6}}>{phase}</div>
              <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill, minmax(220px, 1fr))',gap:6}}>
                {keys.map(key => {
                  const m = AGENT_META[key]
                  const checked = active.includes(key)
                  const forcedByRemediation = key === 'risk' && active.includes('remediation')
                  return (
                    <label key={key} style={{display:'flex',alignItems:'center',gap:7,fontSize:12,cursor:forcedByRemediation?'default':'pointer',opacity:forcedByRemediation?.7:1}}
                      title={forcedByRemediation ? 'Required by Remediation' : m.desc}>
                      <input type="checkbox" checked={checked} disabled={forcedByRemediation}
                        onChange={()=>toggleAgent(key)} style={{cursor:forcedByRemediation?'default':'pointer'}}/>
                      <i className={`ti ${m.icon}`} style={{color:m.color,fontSize:14,flexShrink:0}}/>
                      <span style={{color:'#0d1117'}}>{m.label}</span>
                    </label>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </details>
    </div>
  )
}

function PriorityPromptCard({ state, update }) {
  const text = state.userInstructions || ''
  const [scan, setScan] = useState({ blocked: false, matches: [] })

  // Debounced (~300ms) client-side guard — real-time feedback before submit.
  // The server re-checks with the authoritative rule set regardless.
  useEffect(() => {
    const t = setTimeout(() => setScan(scanUserInstructions(text)), 300)
    return () => clearTimeout(t)
  }, [text])

  return (
    <div className="card">
      <div className="card-title"><i className="ti ti-target-arrow"/>Analysis priorities <span style={{fontWeight:400,color:'#9fadbf',fontSize:12}}>(optional)</span></div>
      <div style={{fontSize:12,color:'#7a8494',marginBottom:10,lineHeight:1.5}}>
        Tell the agents what to emphasize — e.g. "focus on security in the payment module" or
        "deprioritize style nitpicks, prioritize DB migration safety". This only shapes which
        findings get highlighted and how fixes are ordered — it can never suppress a security,
        secrets, or critical finding, or change the gate decision.
      </div>
      <textarea
        value={text}
        maxLength={USER_INSTRUCTIONS_MAX_CHARS}
        onChange={e=>update({userInstructions:e.target.value})}
        placeholder="e.g. Focus on security in the payment module. Deprioritize style nitpicks."
        rows={3}
        style={{width:'100%',fontSize:13,padding:'8px 10px',border:`1px solid ${scan.blocked?'#fca5a5':'#e3e7ee'}`,borderRadius:8,resize:'vertical',fontFamily:'inherit'}}
      />
      <div style={{display:'flex',justifyContent:'flex-end',fontSize:11,color:'#9fadbf',marginTop:4}}>
        {text.length}/{USER_INSTRUCTIONS_MAX_CHARS}
      </div>
      {scan.blocked && (
        <div style={{fontSize:11.5,color:'#b45309',marginTop:6,display:'flex',gap:6,alignItems:'flex-start'}}>
          <i className="ti ti-alert-triangle" style={{marginTop:1,flexShrink:0}}/>
          <span>
            This looks like it's trying to override review rules ("{scan.matches[0].phrase}") rather than
            prioritize — rephrase it as guidance about what to focus on. Submission is blocked until this
            is resolved.
          </span>
        </div>
      )}
    </div>
  )
}

function TargetBody({ t, loadState, state, update, onRetry }) {
  if (loadState === true) {
    const label = t==='pr'?'pull requests':t==='branch'?'branches':'commits'
    return <div className="empty-state"><span className="spinner" style={{width:24,height:24}}/><div style={{marginTop:12}}>Loading {label}...</div></div>
  }
  if (typeof loadState === 'string') {
    return (
      <div className="err-msg" style={{margin:0}}>
        <i className="ti ti-alert-circle" style={{flexShrink:0}}/>
        <div><strong>Failed to load data</strong><br/>{loadState}<br/>
          <button className="btn btn-sm" style={{marginTop:8}} onClick={onRetry}><i className="ti ti-refresh"/>Retry</button>
        </div>
      </div>
    )
  }
  if (t==='pr') {
    if (!state.prs.length) return (
      <div className="empty-state">
        <i className="ti ti-git-pull-request"/><div style={{marginBottom:12}}>No open pull requests found.</div>
        <div style={{fontSize:12,color:'#7a8494',marginBottom:12}}>Switch to <strong>Branch diff</strong> or <strong>Commit</strong> to analyse without a PR.</div>
        <button className="btn btn-sm" onClick={onRetry}><i className="ti ti-refresh"/>Refresh</button>
      </div>
    )
    return (
      <div className="list-scroll">
        {state.prs.map(p=>{
          const id=prNum(p); const sel=state.selectedPR&&prNum(state.selectedPR)===id
          return (
            <div key={id} className={`list-item ${sel?'selected':''}`} onClick={()=>update({selectedPR:p})}>
              <i className="ti ti-git-pull-request" style={{fontSize:18,color:sel?'#1a6cf6':'#7a8494',flexShrink:0}}/>
              <div className="li-main">
                <div className="li-title">{prTitle(p)}</div>
                <div className="li-sub">{id} · {prHead(p)} → {prBase(p)} · by {prAuthor(p)}</div>
              </div>
              <i className={`ti ${sel?'ti-circle-check':'ti-circle'}`} style={{color:sel?'#1a6cf6':'#e8eaed',fontSize:20,flexShrink:0}}/>
            </div>
          )
        })}
      </div>
    )
  }
  if (t==='branch') {
    if (!state.branches.length) return <div className="empty-state"><i className="ti ti-git-branch"/><div style={{marginBottom:12}}>No branches found.</div><button className="btn btn-sm" onClick={onRetry}><i className="ti ti-refresh"/>Retry</button></div>
    const opts = state.branches.map(b=><option key={branchName(b)} value={branchName(b)}>{branchName(b)}</option>)
    return (
      <div>
        <div className="two-col">
          <div className="field">
            <label>Source branch (your feature branch)</label>
            <select value={state.sourceBranch} onChange={e=>update({sourceBranch:e.target.value})}>
              <option value="">— select source —</option>{opts}
            </select>
          </div>
          <div className="field">
            <label>Target branch (merge into)</label>
            <select value={state.targetBranch} onChange={e=>update({targetBranch:e.target.value})}>
              <option value="">— select target —</option>{opts}
            </select>
          </div>
        </div>
        {state.sourceBranch&&state.targetBranch&&(
          <div className="info-msg"><i className="ti ti-arrow-right-bar"/><span>Analysing: <strong>{state.sourceBranch}</strong> → <strong>{state.targetBranch}</strong></span></div>
        )}
      </div>
    )
  }
  if (t==='commit') {
    return (
      <div>
        <div className="field">
          <label>Commit SHA (type manually or click from list below)</label>
          <input type="text" placeholder="e.g. a1b2c3d" value={state.commitSha} onChange={e=>update({commitSha:e.target.value})} style={{fontFamily:'var(--mono)',fontSize:13}}/>
        </div>
        <div className="list-scroll">
          {!state.commits.length ? (
            <div className="empty-state"><i className="ti ti-git-commit"/>No commits loaded.<br/><button className="btn btn-sm" style={{marginTop:8}} onClick={onRetry}><i className="ti ti-refresh"/>Retry</button></div>
          ) : state.commits.map(c=>{
            const sha=commitSha(c); const sel=state.commitSha===sha
            return (
              <div key={sha} className={`list-item ${sel?'selected':''}`} onClick={()=>update({commitSha:sha})}>
                <code style={{fontSize:11,flexShrink:0,width:56,textAlign:'center',color:'#1a6cf6'}}>{sha}</code>
                <div className="li-main"><div className="li-title">{(commitMsg(c)||'').slice(0,80)}</div><div className="li-sub">{commitAuthor(c)}</div></div>
                {sel&&<i className="ti ti-circle-check" style={{color:'#1a6cf6',fontSize:18,flexShrink:0}}/>}
              </div>
            )
          })}
        </div>
      </div>
    )
  }
  return null
}

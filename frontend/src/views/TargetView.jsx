import { useState, useEffect } from 'react'
import { useApp } from '../AppContext'
import StepsRow from '../components/StepsRow'
import { repoName, shortName, prNum, prTitle, prHead, prBase, prAuthor, branchName, commitSha, commitMsg, commitAuthor } from '../state'
import { backendPost, gitCfg } from '../api'

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

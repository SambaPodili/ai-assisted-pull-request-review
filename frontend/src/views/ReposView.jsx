import { useState, useEffect } from 'react'
import { useApp } from '../AppContext'
import StepsRow from '../components/StepsRow'
import { repoName, shortName, repoLang, LANG_COLORS } from '../state'
import { backendPost, gitCfg } from '../api'

export default function ReposView({ active }) {
  const { state, update } = useApp()
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    if (!active || !state.userInfo || state.repos.length || loading) return
    // Bitbucket Server: do NOT auto-fan-out across every project (that's what
    // produced "random project repos"). Wait until the user supplies a project
    // key (or explicitly clicks Load to browse all).
    if (state.provider === 'bitbucket_server' && !state.projectKey) return
    loadRepos()
  }, [active])

  async function loadRepos(overrideKey) {
    setLoading(true); setErr('')
    try {
      // Build the config from the latest state, allowing an explicit key
      // override so the Load button never sends a stale value.
      const cfg = gitCfg(state)
      if (overrideKey !== undefined) cfg.project_key = overrideKey
      const d = await backendPost(state, '/api/v1/git/repos', cfg)
      update({ repos: d.repos || [] })
    } catch (e) { setErr(e.message) }
    setLoading(false)
  }

  function selectRepo(name, mode) {
    const r = state.repos.find(x=>repoName(x)===name)
    if (!r) return
    if (mode==='primary') {
      if (!state.primaryRepo||repoName(state.primaryRepo)!==name) {
        update({ primaryRepo:r, connectedRepos:state.connectedRepos.filter(x=>repoName(x)!==name), prs:[], branches:[], commits:[], selectedPR:null, sourceBranch:'', targetBranch:'', commitSha:'' })
      } else update({ primaryRepo:r, connectedRepos:state.connectedRepos.filter(x=>repoName(x)!==name) })
    } else {
      if (state.primaryRepo&&repoName(state.primaryRepo)===name) { update({primaryRepo:r}); return }
      const idx = state.connectedRepos.findIndex(x=>repoName(x)===name)
      if (idx>=0) { const next=[...state.connectedRepos]; next.splice(idx,1); update({connectedRepos:next}) }
      else update({connectedRepos:[...state.connectedRepos, r]})
    }
  }

  const isBBSrv = state.provider==='bitbucket_server'
  const q = search.toLowerCase()
  const filtered = q ? state.repos.filter(r=>repoName(r).toLowerCase().includes(q)||(r.project||'').toLowerCase().includes(q)) : state.repos

  return (
    <div>
      <StepsRow active={1}/>
      {!state.userInfo && <div className="err-msg"><i className="ti ti-alert-circle"/>Please connect your provider first.</div>}
      <div className="card">
        <div className="card-title">
          <i className="ti ti-git-branch"/>Primary repository
          <span style={{marginLeft:'auto'}}>{state.primaryRepo&&<span className="badge badge-green"><i className="ti ti-check"/>{shortName(state.primaryRepo)}</span>}</span>
        </div>
        <div className="info-msg"><i className="ti ti-info-circle"/>Click a repository to set it as the primary impact analysis target.</div>
        {isBBSrv ? (
          <>
          <div style={{display:'flex',gap:8,alignItems:'center',marginBottom:10,flexWrap:'wrap'}}>
            <div className="search-wrap" style={{flex:1,minWidth:160,marginBottom:0}}>
              <i className="ti ti-search"/><input type="text" placeholder="Search repositories..." value={search} onChange={e=>setSearch(e.target.value)}/>
            </div>
            <input type="text" value={state.projectKey}
              onChange={e=>update({projectKey:e.target.value.toUpperCase()})}
              onKeyDown={e=>{ if(e.key==='Enter'){ update({repos:[]}); loadRepos(state.projectKey) } }}
              placeholder="Project key (e.g. BANK)" style={{width:160,border:'1.5px solid #e8eaed',borderRadius:8,padding:'6px 10px',fontSize:12}}/>
            <button onClick={()=>{ update({repos:[]}); loadRepos(state.projectKey) }} style={{padding:'6px 14px',border:'1.5px solid #c5d8fb',background:'#eff5ff',color:'#1a56db',borderRadius:8,cursor:'pointer',fontSize:12,fontWeight:600}}>
              <i className="ti ti-reload"/> Load
            </button>
          </div>
          <div style={{fontSize:11,color:'#7a8494',marginBottom:10,display:'flex',alignItems:'center',gap:6}}>
            <i className="ti ti-folder"/>
            {state.projectKey
              ? <>Showing repositories in project <strong style={{color:'#1a6cf6'}}>{state.projectKey}</strong>. Clear the key and Load to browse all projects.</>
              : <>Enter a <strong>project key</strong> and click Load to see that project's repositories, or click Load with the key blank to browse all projects.</>}
          </div>
          </>
        ) : (
          <div className="search-wrap"><i className="ti ti-search"/><input type="text" placeholder="Search repositories..." value={search} onChange={e=>setSearch(e.target.value)}/></div>
        )}
        {loading && <div className="empty-state"><span className="spinner" style={{width:24,height:24}}/><div style={{marginTop:12}}>Loading repositories...</div></div>}
        {err && <div className="err-msg" style={{margin:0}}><i className="ti ti-alert-circle" style={{flexShrink:0}}/> Failed to load repos: {err}</div>}
        {!loading && !err && <RepoGridGrouped repos={filtered} mode="primary" state={state} selectRepo={selectRepo}/>}
      </div>
      <div className="card">
        <div className="card-title"><i className="ti ti-topology-star-3"/>Connected applications <span style={{fontSize:11,fontWeight:400,color:'#7a8494',marginLeft:4}}>multi-repo blast radius</span></div>
        <div className="info-msg"><i className="ti ti-info-circle"/>Select additional repos that call or depend on the primary.</div>
        <div className="chips">
          {state.connectedRepos.map(r=>(
            <div key={repoName(r)} className="chip">
              <i className="ti ti-git-branch" style={{fontSize:12}}/>{shortName(r)}
              <button onClick={()=>update({connectedRepos:state.connectedRepos.filter(x=>repoName(x)!==repoName(r))})}><i className="ti ti-x"/></button>
            </div>
          ))}
        </div>
        <RepoGridGrouped repos={filtered.filter(r=>!state.primaryRepo||repoName(r)!==repoName(state.primaryRepo))} mode="connected" state={state} selectRepo={selectRepo}/>
      </div>
    </div>
  )
}

function RepoGridGrouped({repos, mode, state, selectRepo}) {
  const isBBSrv = state.provider==='bitbucket_server'
  if (!isBBSrv) return <RepoGrid repos={repos} mode={mode} state={state} selectRepo={selectRepo}/>
  const groups = {}
  repos.slice(0,200).forEach(r=>{ const proj=r.project||r.full_name?.split('/')[0]||'OTHER'; if(!groups[proj])groups[proj]=[]; groups[proj].push(r) })
  const keys = Object.keys(groups).sort()
  if (keys.length<=1) return <RepoGrid repos={repos} mode={mode} state={state} selectRepo={selectRepo}/>
  return (
    <div>{keys.map(proj=>(
      <div key={proj} style={{marginBottom:14}}>
        <div style={{display:'flex',alignItems:'center',gap:6,marginBottom:6}}>
          <span>📁</span><span style={{fontWeight:700,fontSize:12,color:'#1a2332'}}>{proj}</span>
          <span style={{fontSize:10,color:'#9fadbf'}}>{groups[proj].length} repo{groups[proj].length!==1?'s':''}</span>
        </div>
        <RepoGrid repos={groups[proj]} mode={mode} state={state} selectRepo={selectRepo}/>
      </div>
    ))}</div>
  )
}

function RepoGrid({repos, mode, state, selectRepo}) {
  return (
    <div className="repo-grid">
      {repos.slice(0,80).map(r=>{
        const n=repoName(r); const isPrimary=state.primaryRepo&&repoName(state.primaryRepo)===n; const isConn=state.connectedRepos.some(x=>repoName(x)===n)
        const cls=isPrimary?'primary':isConn?'connected':''; const lang=repoLang(r)
        return (
          <div key={n} className={`repo-tile ${cls}`} onClick={()=>selectRepo(n,mode)}>
            <div className="repo-tile-name" title={n}>{shortName(r)}</div>
            <div className="repo-tile-meta">
              {lang&&<><span className="lang-dot" style={{background:LANG_COLORS[lang]||'#4a5b7a'}}/><span>{lang}</span></>}
              {isPrimary&&<span className="badge badge-green" style={{fontSize:10,padding:'1px 5px'}}>primary</span>}
              {isConn&&<span className="badge badge-blue" style={{fontSize:10,padding:'1px 5px'}}>+</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}

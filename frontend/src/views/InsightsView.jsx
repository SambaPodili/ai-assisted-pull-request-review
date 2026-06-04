import { useState, useEffect, useRef, useCallback } from 'react'
import { useApp } from '../AppContext'
import { normalizeReport } from '../normalizeReport'
import * as d3 from 'd3'

const TABS = [
  { id: 'queue', label: '🎯 Review queue' },
  { id: 'trend', label: '📈 Risk trend' },
  { id: 'heatmap', label: '🔥 Change heatmap' },
  { id: 'cost', label: '💰 API cost' },
  { id: 'feedback', label: '🧠 Feedback' },
]

export default function InsightsView({ active, showView, showToast }) {
  const { state, update } = useApp()
  const [activeTab, setActiveTab] = useState('queue')
  const [filter, setFilter] = useState({ days: 30, repo: '' })
  const [repos, setRepos] = useState([])
  const [autoRefresh, setAutoRefresh] = useState(false)
  const autoRefreshTimer = useRef(null)

  useEffect(() => {
    if (active) switchTab(activeTab)
  }, [active, filter])

  useEffect(() => {
    if (autoRefreshTimer.current) clearInterval(autoRefreshTimer.current)
    if (autoRefresh) {
      autoRefreshTimer.current = setInterval(() => { if (active) switchTab(activeTab) }, 30000)
    }
    return () => clearInterval(autoRefreshTimer.current)
  }, [autoRefresh, activeTab, active])

  function filterQS(kind) {
    const parts = []
    if (kind==='days' && filter.days>0) parts.push('days='+filter.days)
    if (kind==='weeks') parts.push('weeks='+(filter.days>0?Math.max(1,Math.ceil(filter.days/7)):52))
    if (filter.repo) parts.push('repo='+encodeURIComponent(filter.repo))
    return parts.length?'?'+parts.join('&'):''
  }

  async function insightFetch(path) {
    const h={}; if(state.backendKey)h['X-API-Key']=state.backendKey
    const r = await fetch(`${state.backendUrl}${path}`,{headers:h,signal:AbortSignal.timeout(30000)})
    if(!r.ok){const e=await r.json().catch(()=>({detail:r.statusText}));throw new Error(e.detail||`HTTP ${r.status}`)}
    return r.json()
  }

  function switchTab(tab) {
    setActiveTab(tab)
    // Tab content will re-render via props
  }

  if (!state.backendUrl) {
    return (
      <div style={{maxWidth:860}}>
        <div className="card"><div className="empty-state"><i className="ti ti-plug-x"/>Backend required<br/><span style={{fontSize:12,color:'#9fadbf'}}>Configure a backend URL in Settings to see team insights.</span></div></div>
      </div>
    )
  }

  const dayOpts = [[7,'7 days'],[30,'30 days'],[90,'90 days'],[0,'All time']]

  async function exportInsight() {
    let path, qs
    if(activeTab==='queue'){path='/api/v1/insights/export/queue.csv';qs=filterQS('days')}
    else if(activeTab==='cost'){path='/api/v1/insights/export/cost.csv';qs=filterQS('weeks')}
    else if(activeTab==='trend'){path='/api/v1/insights/export/trend.csv';qs=filterQS('weeks')}
    else{showToast('CSV export not available for the heatmap tab — use PDF.','info');return}
    try {
      const h={}; if(state.backendKey)h['X-API-Key']=state.backendKey
      const r=await fetch(`${state.backendUrl}${path}${qs}`,{headers:h})
      if(!r.ok)throw new Error('HTTP '+r.status)
      const blob=await r.blob(); const url=URL.createObjectURL(blob)
      const a=document.createElement('a')
      const fn=(r.headers.get('content-disposition')||'').match(/filename="([^"]+)"/)
      a.href=url; a.download=fn?fn[1]:'ciaa_export.csv'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url)
      showToast('CSV downloaded','success')
    } catch(e){showToast('Export failed: '+e.message,'error')}
  }

  return (
    <div style={{maxWidth:1000}}>
      <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:14,flexWrap:'wrap'}}>
        <div style={{display:'flex',gap:4,background:'#f0f2f5',borderRadius:8,padding:3}}>
          {dayOpts.map(([d,lbl])=>(
            <button key={d} onClick={()=>setFilter(f=>({...f,days:d}))} style={{border:'none',background:filter.days===d?'#fff':'transparent',boxShadow:filter.days===d?'0 1px 2px rgba(0,0,0,.1)':'none',borderRadius:6,padding:'5px 12px',fontSize:12,fontWeight:filter.days===d?600:400,color:filter.days===d?'#0d1117':'#7a8494',cursor:'pointer'}}>{lbl}</button>
          ))}
        </div>
        <select value={filter.repo} onChange={e=>setFilter(f=>({...f,repo:e.target.value}))} style={{border:'1.5px solid #e8eaed',borderRadius:8,padding:'6px 10px',fontSize:12,maxWidth:220}}>
          <option value="">All repositories</option>
          {repos.map(r=><option key={r} value={r}>{r}</option>)}
        </select>
        <label style={{display:'flex',alignItems:'center',gap:6,fontSize:12,color:'#7a8494',cursor:'pointer',marginLeft:'auto'}}>
          <input type="checkbox" checked={autoRefresh} onChange={e=>setAutoRefresh(e.target.checked)} style={{cursor:'pointer'}}/>
          <i className="ti ti-refresh" style={{fontSize:13}}/> Auto-refresh (30s)
        </label>
        <button className="btn" style={{fontSize:12,padding:'6px 12px'}} onClick={exportInsight} title="Download current tab as CSV"><i className="ti ti-download"/> CSV</button>
      </div>

      <div style={{marginBottom:18,display:'flex',gap:4,flexWrap:'wrap'}}>
        {TABS.map(t=>(
          <button key={t.id} className={`tab ${activeTab===t.id?'active':''}`} onClick={()=>setActiveTab(t.id)}>{t.label}</button>
        ))}
      </div>

      {activeTab==='queue' && <QueueTab state={state} insightFetch={insightFetch} filterQS={filterQS} setRepos={setRepos} repos={repos} showView={showView} update={update}/>}
      {activeTab==='trend' && <TrendTab state={state} insightFetch={insightFetch} filterQS={filterQS}/>}
      {activeTab==='heatmap' && <HeatmapTab state={state} insightFetch={insightFetch} filter={filter}/>}
      {activeTab==='cost' && <CostTab state={state} insightFetch={insightFetch} filterQS={filterQS}/>}
      {activeTab==='feedback' && <FeedbackTab state={state} insightFetch={insightFetch} filter={filter}/>}
    </div>
  )
}

function QueueTab({ state, insightFetch, filterQS, setRepos, repos, showView, update }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    setLoading(true); setErr('')
    insightFetch('/api/v1/insights/queue'+filterQS('days'))
      .then(d => {
        if (d.repos?.length) setRepos(d.repos)
        setData(d); setLoading(false)
      })
      .catch(e => { setErr(e.message); setLoading(false) })
  }, [filterQS('days')])

  if (loading) return <div className="empty-state"><span className="spinner" style={{width:22,height:22}}/><div style={{marginTop:10}}>Loading review queue…</div></div>
  if (err) return <div className="card"><div className="err-msg" style={{margin:0}}><i className="ti ti-alert-circle"/>{err}</div></div>
  if (!data?.queue?.length) return <div className="card"><div className="empty-state"><i className="ti ti-inbox"/>No analyses yet.</div></div>

  const gateBadge = g => {
    const m={BLOCK:['#fff1f2','#991b1b','#fca5a5','🚫'],HOLD:['#fffbeb','#92400e','#fcd34d','⚠️'],APPROVE:['#f0fdf4','#166534','#86efac','✅']}[g]||['#f7f8fa','#7a8494','#e8eaed','•']
    return <span style={{background:m[0],color:m[1],border:`1px solid ${m[2]}`,borderRadius:5,padding:'2px 8px',fontSize:11,fontWeight:700,whiteSpace:'nowrap'}}>{m[3]} {g}</span>
  }
  const riskColor = s => s>=7?'#dc2626':s>=4?'#f59e0b':'#10b981'

  async function openReport(rid) {
    try {
      const h={}; if(state.backendKey)h['X-API-Key']=state.backendKey
      const r=await fetch(`${state.backendUrl}/api/v1/report/${rid}?fmt=full`,{headers:h})
      const d=await r.json()
      update({report:normalizeReport(d),lastRequestId:rid})
      showView('results')
    } catch(e){alert('Could not load report: '+e.message)}
  }

  return (
    <div className="card">
      <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:14}}>
        <div className="section-heading" style={{margin:0}}><i className="ti ti-list-numbers"/>Review queue — {data.total} PR(s)</div>
        <span style={{fontSize:11,color:'#9fadbf',marginLeft:'auto'}}>Ranked: BLOCK first, then by risk score</span>
        <button className="btn" style={{fontSize:12,padding:'5px 12px'}} onClick={()=>window.location.reload()}><i className="ti ti-refresh"/></button>
      </div>
      <div style={{display:'flex',flexDirection:'column',gap:8}}>
        {data.queue.map((item,i)=>(
          <div key={i} style={{border:'1px solid #e8eaed',borderRadius:10,padding:'12px 14px',display:'flex',alignItems:'flex-start',gap:12,transition:'border-color .15s'}} onMouseOver={e=>e.currentTarget.style.borderColor='#1a6cf6'} onMouseOut={e=>e.currentTarget.style.borderColor='#e8eaed'}>
            <div style={{textAlign:'center',flexShrink:0,width:48}}>
              <div style={{fontSize:22,fontWeight:800,fontFamily:'JetBrains Mono,monospace',color:riskColor(item.risk_score)}}>{item.risk_score.toFixed(1)}</div>
              <div style={{fontSize:9,color:'#9fadbf',textTransform:'uppercase',letterSpacing:.05}}>risk</div>
            </div>
            <div style={{flex:1,minWidth:0}}>
              <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:3,flexWrap:'wrap'}}>
                {gateBadge(item.gate)}
                <span style={{fontWeight:600,fontSize:13,color:'#0d1117'}}>{item.pr_title||item.source_ref||'(no title)'}</span>
                {item.pr_number&&<span style={{fontSize:11,color:'#7a8494'}}>#{item.pr_number}</span>}
              </div>
              <div style={{fontSize:11,color:'#7a8494',marginBottom:6}}>
                <i className="ti ti-git-branch" style={{fontSize:11}}/> {item.repo}
                {item.author&&` · ${item.author}`}
                {` · ${item.elapsed} · ${(item.total_tokens||0).toLocaleString()} tokens`}
              </div>
              {item.top_findings?.length>0&&<div style={{display:'flex',flexDirection:'column',gap:2}}>{item.top_findings.map((f,j)=><div key={j} style={{fontSize:11,color:'#5a6a7e'}}><span className={`sev sev-${f.severity}`} style={{fontSize:9,padding:'0 5px'}}>{f.severity}</span> {f.title}</div>)}</div>}
            </div>
            <button className="btn" style={{fontSize:11,padding:'5px 12px',flexShrink:0}} onClick={()=>openReport(item.request_id)}><i className="ti ti-eye"/> View</button>
          </div>
        ))}
      </div>
    </div>
  )
}

function TrendTab({ state, insightFetch, filterQS }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    setLoading(true); setErr('')
    insightFetch('/api/v1/insights/trend'+filterQS('weeks'))
      .then(d=>{setData(d);setLoading(false)})
      .catch(e=>{setErr(e.message);setLoading(false)})
  }, [filterQS('weeks')])

  if (loading) return <div className="empty-state"><span className="spinner" style={{width:22,height:22}}/><div style={{marginTop:10}}>Computing risk trends…</div></div>
  if (err) return <div className="card"><div className="err-msg" style={{margin:0}}><i className="ti ti-alert-circle"/>{err}</div></div>
  if (!data?.series?.length) return <div className="card"><div className="empty-state"><i className="ti ti-chart-line"/>Not enough history yet.</div></div>

  const trendBadge = t => {
    const m={improving:['#f0fdf4','#166534','↓ improving'],degrading:['#fff1f2','#991b1b','↑ degrading'],stable:['#f7f8fa','#7a8494','→ stable']}[t]||['#f7f8fa','#7a8494',t]
    return <span style={{background:m[0],color:m[1],borderRadius:5,padding:'2px 8px',fontSize:11,fontWeight:700}}>{m[2]}</span>
  }

  function Sparkline({scores}) {
    const W=100,H=32,max=10
    const pts=scores.map((s,i)=>{const x=(i/Math.max(scores.length-1,1))*W,y=H-(s/max)*H;return `${x.toFixed(1)},${y.toFixed(1)}`}).join(' ')
    const dots=scores.map((s,i)=>{if(!s)return null;const x=(i/Math.max(scores.length-1,1))*W,y=H-(s/max)*H,col=s>=7?'#dc2626':s>=4?'#f59e0b':'#10b981';return <circle key={i} cx={x.toFixed(1)} cy={y.toFixed(1)} r="2" fill={col}/>})
    return <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{width:'100%',height:48,background:'#fafbfc',border:'1px solid #f0f2f5',borderRadius:6}}><polyline points={pts} fill="none" stroke="#1a6cf6" strokeWidth="1" vectorEffect="non-scaling-stroke"/>{dots}</svg>
  }

  return (
    <div>
      <div className="card">
        <div className="section-heading"><i className="ti ti-chart-line"/>Risk score trend — last 12 weeks</div>
        {data.series.map((s,i)=>(
          <div key={i} style={{marginBottom:18}}>
            <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:8}}>
              <span style={{fontWeight:600,fontSize:13}}>{s.repo}</span>
              {trendBadge(s.trend)}
              <span style={{fontSize:11,color:'#7a8494',marginLeft:'auto'}}>avg {s.avg}</span>
            </div>
            <Sparkline scores={s.scores}/>
          </div>
        ))}
      </div>
      {data.top_issues?.length>0&&(
        <div className="card">
          <div className="section-heading"><i className="ti ti-flame"/>Top recurring issue categories</div>
          <div style={{display:'flex',flexDirection:'column',gap:8}}>
            {data.top_issues.map((it,i)=>{
              const max=data.top_issues[0].count||1
              return (
                <div key={i} style={{display:'flex',alignItems:'center',gap:10}}>
                  <span style={{width:120,fontSize:12,textTransform:'capitalize'}}>{it.category}</span>
                  <div style={{flex:1,height:18,background:'#f0f2f5',borderRadius:4,overflow:'hidden'}}>
                    <div style={{height:'100%',width:`${(it.count/max*100).toFixed(0)}%`,background:'linear-gradient(to right,#f59e0b,#dc2626)',borderRadius:4}}/>
                  </div>
                  <span style={{fontSize:12,fontWeight:600,fontFamily:'JetBrains Mono,monospace',width:40,textAlign:'right'}}>{it.count}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function HeatmapTab({ state, insightFetch, filter }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    setLoading(true); setErr('')
    const hp = []
    if (filter.days > 0) hp.push('days='+filter.days)
    if (filter.repo) hp.push('repo='+encodeURIComponent(filter.repo))
    hp.push('limit=60')
    insightFetch('/api/v1/insights/heatmap?'+hp.join('&'))
      .then(d=>{setData(d);setLoading(false)})
      .catch(e=>{setErr(e.message);setLoading(false)})
  }, [filter.days, filter.repo])

  if (loading) return <div className="empty-state"><span className="spinner" style={{width:22,height:22}}/><div style={{marginTop:10}}>Building change heatmap…</div></div>
  if (err) return <div className="card"><div className="err-msg" style={{margin:0}}><i className="ti ti-alert-circle"/>{err}</div></div>
  if (!data?.cells?.length) return <div className="card"><div className="empty-state"><i className="ti ti-grid-dots"/>No file-level data yet.</div></div>

  const maxChanges=Math.max(...data.cells.map(x=>x.changes),1)
  return (
    <div className="card">
      <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:6}}>
        <div className="section-heading" style={{margin:0}}><i className="ti ti-grid-dots"/>Change impact heatmap — last {data.days} days</div>
        <span style={{fontSize:11,color:'#9fadbf',marginLeft:'auto'}}>{data.total} file(s) · tile size = change frequency · colour = max risk</span>
      </div>
      <div style={{display:'flex',gap:14,fontSize:11,color:'#7a8494',marginBottom:14}}>
        {[['#dc2626','critical (≥7)'],['#f59e0b','high (≥5)'],['#3b82f6','medium (≥3)'],['#10b981','low']].map(([c,l])=>(
          <span key={l}><span style={{display:'inline-block',width:10,height:10,background:c,borderRadius:2,verticalAlign:'middle',marginRight:4}}/>{l}</span>
        ))}
      </div>
      <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
        {data.cells.map((cell,i)=>{
          const scale=0.6+(cell.changes/maxChanges)*0.8,sz=Math.round(46*scale)
          return (
            <div key={i} title={`${cell.file}\n${cell.changes} change(s) · max risk ${cell.max_risk.toFixed(1)}${cell.has_security?' · security':''}${cell.has_privacy?' · privacy':''}`}
              style={{width:sz,height:sz,background:cell.colour,borderRadius:6,display:'flex',alignItems:'center',justifyContent:'center',cursor:'default',position:'relative',overflow:'hidden',boxShadow:'0 1px 2px rgba(0,0,0,.08)'}}>
              <span style={{fontSize:8,color:'#fff',fontWeight:700,textAlign:'center',padding:2,lineHeight:1.1,textShadow:'0 1px 2px rgba(0,0,0,.3)',overflow:'hidden'}}>{cell.label.slice(0,8)}</span>
              {cell.changes>1&&<span style={{position:'absolute',top:1,right:2,fontSize:8,color:'#fff',fontWeight:700,background:'rgba(0,0,0,.3)',borderRadius:3,padding:'0 3px'}}>{cell.changes}</span>}
            </div>
          )
        })}
      </div>
      <div style={{marginTop:16,borderTop:'1px solid #f0f2f5',paddingTop:12}}>
        <div style={{fontSize:12,fontWeight:600,color:'#5a6a7e',marginBottom:8}}>Highest-risk files</div>
        <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
          <thead><tr style={{color:'#9fadbf',fontSize:10,textTransform:'uppercase',letterSpacing:.05}}>
            <th style={{textAlign:'left',padding:'6px 8px'}}>File</th>
            <th style={{textAlign:'right',padding:'6px 8px'}}>Changes</th>
            <th style={{textAlign:'right',padding:'6px 8px'}}>Max risk</th>
            <th style={{textAlign:'left',padding:'6px 8px'}}>Flags</th>
          </tr></thead>
          <tbody>
            {data.cells.slice(0,10).map((cell,i)=>(
              <tr key={i} style={{borderTop:'1px solid #f5f6f8'}}>
                <td style={{padding:'6px 8px',fontFamily:'JetBrains Mono,monospace',fontSize:11}} title={cell.file}>{cell.file.length>48?'…'+cell.file.slice(-46):cell.file}</td>
                <td style={{padding:'6px 8px',textAlign:'right',fontWeight:600}}>{cell.changes}</td>
                <td style={{padding:'6px 8px',textAlign:'right',fontWeight:700,color:cell.colour}}>{cell.max_risk.toFixed(1)}</td>
                <td style={{padding:'6px 8px'}}>
                  {cell.has_security&&<span className="badge badge-red" style={{fontSize:9,padding:'1px 5px',marginRight:4}}>sec</span>}
                  {cell.has_privacy&&<span className="badge badge-amber" style={{fontSize:9,padding:'1px 5px'}}>pii</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function CostTab({ state, insightFetch, filterQS }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    setLoading(true); setErr('')
    insightFetch('/api/v1/insights/cost'+filterQS('weeks'))
      .then(d=>{setData(d);setLoading(false)})
      .catch(e=>{setErr(e.message);setLoading(false)})
  }, [filterQS('weeks')])

  if (loading) return <div className="empty-state"><span className="spinner" style={{width:22,height:22}}/><div style={{marginTop:10}}>Calculating API spend…</div></div>
  if (err) return <div className="card"><div className="err-msg" style={{margin:0}}><i className="ti ti-alert-circle"/>{err}</div></div>
  if (!data) return null

  const s=data.summary
  const maxWeekCost=Math.max(...data.weekly.map(w=>w.cost_usd),0.0001)
  const costCard=(icon,label,value,color)=>(
    <div className="card" style={{flex:1,minWidth:140,margin:0,padding:'14px 16px'}}>
      <div style={{fontSize:11,color:'#9fadbf',marginBottom:4}}>{icon} {label}</div>
      <div style={{fontSize:22,fontWeight:800,color,fontFamily:'JetBrains Mono,monospace'}}>{value}</div>
    </div>
  )

  return (
    <div>
      <div style={{display:'flex',gap:12,marginBottom:14,flexWrap:'wrap'}}>
        {costCard('💰','Total cost','$'+s.total_cost_usd.toFixed(2),'#059669')}
        {costCard('🔤','Total tokens',s.total_tokens.toLocaleString(),'#1a56db')}
        {costCard('⚠️','Fallbacks',s.fallback_count,s.fallback_count>0?'#f59e0b':'#7a8494')}
        {costCard('📅','Window',s.weeks_covered+' weeks','#7c3aed')}
      </div>
      <div className="card">
        <div className="section-heading"><i className="ti ti-chart-bar"/>Weekly spend</div>
        <div style={{display:'flex',alignItems:'flex-end',gap:4,height:120,padding:'8px 0'}}>
          {data.weekly.map((w,i)=>{
            const h=Math.max((w.cost_usd/maxWeekCost*100),2)
            return (
              <div key={i} style={{flex:1,display:'flex',flexDirection:'column',alignItems:'center',gap:3}} title={`${w.week}: $${w.cost_usd.toFixed(4)} · ${w.tokens.toLocaleString()} tokens · ${w.analyses} analyses`}>
                <div style={{width:'100%',height:`${h}%`,background:'linear-gradient(to top,#1a6cf6,#10b981)',borderRadius:'3px 3px 0 0',minHeight:2}}/>
                <span style={{fontSize:8,color:'#9fadbf',transform:'rotate(-45deg)',whiteSpace:'nowrap',marginTop:4}}>{w.week.split('-W')[1]||w.week}</span>
              </div>
            )
          })}
        </div>
      </div>
      <div style={{display:'flex',gap:12,flexWrap:'wrap'}}>
        <div className="card" style={{flex:1,minWidth:300}}>
          <div className="section-heading"><i className="ti ti-robot"/>Cost by agent</div>
          <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
            <thead><tr style={{color:'#9fadbf',fontSize:10,textTransform:'uppercase'}}><th style={{textAlign:'left',padding:'5px 6px'}}>Agent</th><th style={{textAlign:'right',padding:'5px 6px'}}>Tokens</th><th style={{textAlign:'right',padding:'5px 6px'}}>Cost</th></tr></thead>
            <tbody>{data.by_agent.slice(0,12).map((a,i)=><tr key={i} style={{borderTop:'1px solid #f5f6f8'}}><td style={{padding:'5px 6px'}}>{a.agent}</td><td style={{padding:'5px 6px',textAlign:'right',fontFamily:'JetBrains Mono,monospace'}}>{a.tokens.toLocaleString()}</td><td style={{padding:'5px 6px',textAlign:'right',fontWeight:600,color:'#059669'}}>${a.cost_usd.toFixed(3)}</td></tr>)}</tbody>
          </table>
        </div>
        <div className="card" style={{flex:1,minWidth:280}}>
          <div className="section-heading"><i className="ti ti-cpu"/>Cost by model</div>
          <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
            <thead><tr style={{color:'#9fadbf',fontSize:10,textTransform:'uppercase'}}><th style={{textAlign:'left',padding:'5px 6px'}}>Model</th><th style={{textAlign:'right',padding:'5px 6px'}}>Calls</th><th style={{textAlign:'right',padding:'5px 6px'}}>Cost</th></tr></thead>
            <tbody>{data.by_model.map((m,i)=><tr key={i} style={{borderTop:'1px solid #f5f6f8'}}><td style={{padding:'5px 6px',fontSize:11}}>{m.model}</td><td style={{padding:'5px 6px',textAlign:'right'}}>{m.calls}</td><td style={{padding:'5px 6px',textAlign:'right',fontWeight:600,color:'#059669'}}>${m.cost_usd.toFixed(3)}</td></tr>)}</tbody>
          </table>
        </div>
      </div>
      <div style={{fontSize:11,color:'#9fadbf',textAlign:'center',marginTop:10}}><i className="ti ti-info-circle"/> {data.pricing_note}</div>
    </div>
  )
}

function FeedbackTab({ state, insightFetch, filter }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let on = true
    const qs = filter?.repo ? `?repo=${encodeURIComponent(filter.repo)}` : ''
    insightFetch(`/api/v1/feedback/stats${qs}`)
      .then(d => { if (on) setData(d) })
      .catch(e => { if (on) setErr(e.message) })
    return () => { on = false }
  }, [filter?.repo])

  if (err) return <div className="card"><div className="err-msg" style={{margin:0}}><i className="ti ti-alert-circle"/> {err}</div></div>
  if (!data) return <div className="card"><div className="empty-state"><span className="spinner" style={{width:22,height:22}}/><div style={{marginTop:10}}>Loading feedback…</div></div></div>

  const noisy = data.noisy_checks || []
  const gs = data.gate_stats || {}
  const total = gs.total_overrides || 0

  return (
    <div>
      <div className="card">
        <div className="section-heading"><i className="ti ti-gavel"/>Gate override behaviour</div>
        {total === 0
          ? <div style={{fontSize:13,color:'#7a8494'}}>No human gate overrides recorded yet. As reviewers approve/block, this shows whether the system tends to be too strict or too lenient for this repo.</div>
          : (
            <div style={{display:'flex',gap:20,flexWrap:'wrap',fontSize:13}}>
              <Stat n={total} label="overrides" color="#1a56db"/>
              <Stat n={gs.looser||0} label="went looser (system too strict)" color={(gs.looser||0)>(gs.stricter||0)?'#92400e':'#7a8494'}/>
              <Stat n={gs.stricter||0} label="went stricter (system too lenient)" color={(gs.stricter||0)>(gs.looser||0)?'#b91c1c':'#7a8494'}/>
              <Stat n={gs.same||0} label="agreed (reason logged)" color="#166534"/>
            </div>
          )}
        {total>0 && (gs.looser||0) > (gs.stricter||0) && (
          <div style={{marginTop:12,padding:'10px 14px',background:'#fffbeb',border:'1px solid #fcd34d',borderRadius:8,fontSize:12,color:'#92400e'}}>
            <i className="ti ti-bulb"/> Reviewers more often relax the gate than tighten it — consider loosening the deterministic policy thresholds (e.g. coverage / blast-radius) for this repo.
          </div>
        )}
      </div>

      <div className="card">
        <div className="section-heading"><i className="ti ti-flag"/>Noisiest checks (false-positive rate)</div>
        {noisy.length === 0
          ? <div style={{fontSize:13,color:'#7a8494'}}>No finding feedback yet. Use the <strong>⚐ false positive / ✓ valid</strong> controls under findings to train this — repeated false positives surface here so you can tune or suppress those checks.</div>
          : (
            <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
              <thead><tr style={{color:'#9fadbf',fontSize:10,textTransform:'uppercase'}}>
                <th style={{textAlign:'left',padding:'5px 6px'}}>Agent</th>
                <th style={{textAlign:'left',padding:'5px 6px'}}>Category</th>
                <th style={{textAlign:'right',padding:'5px 6px'}}>Marks</th>
                <th style={{textAlign:'right',padding:'5px 6px'}}>False+</th>
                <th style={{textAlign:'right',padding:'5px 6px'}}>FP rate</th>
              </tr></thead>
              <tbody>{noisy.map((c,i)=>(
                <tr key={i} style={{borderTop:'1px solid #f5f6f8'}}>
                  <td style={{padding:'5px 6px'}}>{c.agent}</td>
                  <td style={{padding:'5px 6px',fontFamily:'var(--mono)',fontSize:11}}>{c.category||'—'}</td>
                  <td style={{padding:'5px 6px',textAlign:'right'}}>{c.total}</td>
                  <td style={{padding:'5px 6px',textAlign:'right',fontWeight:700,color:c.false_positives>0?'#b91c1c':'#7a8494'}}>{c.false_positives}</td>
                  <td style={{padding:'5px 6px',textAlign:'right',fontWeight:700,color:c.fp_rate>0.5?'#b91c1c':c.fp_rate>0.2?'#92400e':'#166534'}}>{(c.fp_rate*100).toFixed(0)}%</td>
                </tr>
              ))}</tbody>
            </table>
          )}
      </div>
    </div>
  )
}

function Stat({ n, label, color }) {
  return (
    <div>
      <div style={{fontSize:24,fontWeight:800,fontFamily:'var(--mono)',color}}>{n}</div>
      <div style={{fontSize:11,color:'#9fadbf'}}>{label}</div>
    </div>
  )
}

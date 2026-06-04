import { useState, useEffect } from 'react'
import { useApp } from '../AppContext'

export default function QualityView({ active, showToast }) {
  const { state } = useApp()
  const [results, setResults] = useState(null)
  const [loading, setLoading] = useState(false)
  const [threshold, setThreshold] = useState(parseFloat(localStorage.getItem('qualityRecallThreshold')||'0'))

  useEffect(() => {
    if (active && state.backendUrl) loadSummary()
  }, [active])

  async function loadSummary() {
    setLoading(true)
    try {
      const h = {}; if (state.backendKey) h['X-API-Key'] = state.backendKey
      const r = await fetch(state.backendUrl + '/api/v1/quality/summary', { headers: h })
      if (!r.ok) throw new Error('HTTP ' + r.status)
      const data = await r.json()
      if (data.runs === 0) setResults(null)
      else setResults(data)
    } catch (e) { setResults(null) }
    setLoading(false)
  }

  async function runCheck() {
    setLoading(true)
    try {
      const h = { 'Content-Type': 'application/json' }; if (state.backendKey) h['X-API-Key'] = state.backendKey
      const r = await fetch(state.backendUrl + '/api/v1/quality/run', { method: 'POST', headers: h })
      if (!r.ok) throw new Error('HTTP ' + r.status)
      setResults(await r.json())
      showToast('Quality check complete', 'success')
    } catch (e) { showToast('Quality run failed: ' + e.message, 'error') }
    setLoading(false)
  }

  function saveThreshold(val) {
    const pct = Math.max(0, Math.min(100, parseInt(val)||0))
    const frac = (pct/100).toFixed(2)
    localStorage.setItem('qualityRecallThreshold', frac)
    setThreshold(parseFloat(frac))
  }

  const pct = v => `${Math.round((v||0)*100)}%`
  const scoreColor = v => v>=0.9?'#0c7c4b':v>=0.7?'#8a5200':'#b81c1c'
  const scoreBar = v => <div style={{height:5,background:'#f0f2f5',borderRadius:3,overflow:'hidden',marginTop:4}}><div style={{height:'100%',width:`${Math.round((v||0)*100)}%`,background:scoreColor(v),borderRadius:3}}/></div>

  return (
    <div style={{maxWidth:860}}>
      <div className="card">
        <div className="card-title"><i className="ti ti-target-arrow"/>Detection quality — recall &amp; precision</div>
        <p style={{fontSize:13,color:'#7a8494',marginBottom:16,lineHeight:1.6}}>Runs all golden-diff test cases through the static detection layer and measures how many known issues each agent finds (<strong>recall</strong>) and how many false alarms it raises (<strong>precision</strong>).</p>
        <div style={{display:'flex',alignItems:'center',gap:10,flexWrap:'wrap',marginBottom:12}}>
          {state.backendUrl ? (
            <>
              <button className="btn btn-primary" onClick={runCheck} disabled={loading}><i className="ti ti-player-play"/>{loading?'Running…':'Run quality check'}</button>
              <button className="btn" onClick={loadSummary} disabled={loading}><i className="ti ti-refresh"/>Load last run</button>
            </>
          ) : <div className="info-msg"><i className="ti ti-info-circle"/>Backend required — configure it in Settings.</div>}
        </div>
        <div style={{display:'flex',alignItems:'center',gap:8,paddingTop:10,borderTop:'1px solid #f0f2f5'}}>
          <i className="ti ti-bell" style={{color:'#9fadbf',fontSize:14}}/>
          <label style={{fontSize:12,color:'#7a8494',whiteSpace:'nowrap',margin:0}}>Alert threshold (recall %)</label>
          <input type="number" min="0" max="100" step="1" value={Math.round(threshold*100)} onChange={e=>saveThreshold(e.target.value)}
            style={{width:70,padding:'4px 8px',border:'1px solid #d1d5db',borderRadius:6,fontSize:12}}/>
          <span style={{fontSize:11,color:'#9fadbf'}}>0 = disabled</span>
        </div>
      </div>

      {loading && <div className="card"><div className="empty-state"><span className="spinner" style={{width:22,height:22}}/><div style={{marginTop:10}}>Running quality check…</div></div></div>}

      {!loading && !results && state.backendUrl && (
        <div className="info-msg"><i className="ti ti-info-circle"/>No history yet — run a quality check first.</div>
      )}

      {results && !loading && (
        <>
          <div className="card">
            <div style={{display:'flex',alignItems:'flex-start',justifyContent:'space-between',marginBottom:18,flexWrap:'wrap',gap:10}}>
              <div className="card-title" style={{marginBottom:0}}><i className="ti ti-chart-bar"/>By agent</div>
              <div style={{textAlign:'right'}}>
                {results.latest_timestamp&&<div style={{fontSize:11,color:'#9fadbf',marginBottom:4}}>Last run: {new Date(results.latest_timestamp).toLocaleString()}{results.runs>1?` · ${results.runs} runs logged`:''}</div>}
                <div style={{display:'flex',alignItems:'center',gap:12,flexWrap:'wrap',justifyContent:'flex-end'}}>
                  <div style={{display:'flex',gap:16,fontSize:12}}>
                    <span>Recall: <strong style={{fontFamily:'JetBrains Mono,monospace',color:scoreColor(results.overall_recall||0)}}>{pct(results.overall_recall||0)}</strong></span>
                    <span>Precision: <strong style={{fontFamily:'JetBrains Mono,monospace',color:scoreColor(results.overall_precision||0)}}>{pct(results.overall_precision||0)}</strong></span>
                  </div>
                  {threshold>0&&(results.overall_recall||0)>=threshold&&<span style={{display:'inline-flex',alignItems:'center',gap:4,padding:'2px 8px',background:'#e6f4ee',color:'#0c7c4b',borderRadius:10,fontSize:11,fontWeight:600}}><i className="ti ti-check"/>Above threshold ({Math.round(threshold*100)}%)</span>}
                  {threshold>0&&(results.overall_recall||0)<threshold&&<span style={{display:'inline-flex',alignItems:'center',gap:4,padding:'2px 8px',background:'#fde8e8',color:'#b81c1c',borderRadius:10,fontSize:11,fontWeight:600}}><i className="ti ti-alert-triangle"/>Below threshold ({Math.round(threshold*100)}%)</span>}
                </div>
              </div>
            </div>
            <div style={{overflowX:'auto'}}>
              <table style={{width:'100%',borderCollapse:'collapse'}}>
                <thead><tr style={{fontSize:11,textTransform:'uppercase',letterSpacing:.06,color:'#9fadbf',borderBottom:'2px solid #e8eaed'}}>
                  <th style={{padding:'8px 12px',textAlign:'left',fontWeight:500}}>Agent</th>
                  <th style={{padding:'8px 16px',textAlign:'right',fontWeight:500}}>Recall</th>
                  <th style={{padding:'8px 16px',textAlign:'right',fontWeight:500}}>Precision</th>
                  <th style={{padding:'8px 16px',textAlign:'right',fontWeight:500}}>F1</th>
                  <th style={{padding:'8px 12px',textAlign:'center',fontWeight:500,color:'#0c7c4b'}}>TP</th>
                  <th style={{padding:'8px 12px',textAlign:'center',fontWeight:500,color:'#b81c1c'}}>FP</th>
                  <th style={{padding:'8px 12px',textAlign:'center',fontWeight:500,color:'#8a5200'}}>FN</th>
                </tr></thead>
                <tbody>
                  {Object.entries(results.by_agent||{}).map(([name, m])=>(
                    <tr key={name} style={{borderBottom:'1px solid #e8eaed'}}>
                      <td style={{padding:'10px 12px',fontSize:13,fontWeight:500,whiteSpace:'nowrap'}}>{name.replace(/_/g,' ')}</td>
                      <td style={{padding:'10px 16px',textAlign:'right',minWidth:90}}><div style={{fontSize:13,fontWeight:700,fontFamily:'JetBrains Mono,monospace',color:scoreColor(m.recall)}}>{pct(m.recall)}</div>{scoreBar(m.recall)}</td>
                      <td style={{padding:'10px 16px',textAlign:'right',minWidth:90}}><div style={{fontSize:13,fontWeight:700,fontFamily:'JetBrains Mono,monospace',color:scoreColor(m.precision)}}>{pct(m.precision)}</div>{scoreBar(m.precision)}</td>
                      <td style={{padding:'10px 16px',textAlign:'right',minWidth:90}}><div style={{fontSize:13,fontWeight:700,fontFamily:'JetBrains Mono,monospace',color:scoreColor(m.f1)}}>{pct(m.f1)}</div>{scoreBar(m.f1)}</td>
                      <td style={{padding:'10px 12px',textAlign:'center',fontSize:12,fontFamily:'JetBrains Mono,monospace',color:'#0c7c4b',fontWeight:600}}>{m.tp}</td>
                      <td style={{padding:'10px 12px',textAlign:'center',fontSize:12,fontFamily:'JetBrains Mono,monospace',color:'#b81c1c',fontWeight:600}}>{m.fp}</td>
                      <td style={{padding:'10px 12px',textAlign:'center',fontSize:12,fontFamily:'JetBrains Mono,monospace',color:'#8a5200',fontWeight:600}}>{m.fn}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {(results.by_case||[]).length>0&&(
            <div className="card">
              <div className="card-title" style={{marginBottom:14}}><i className="ti ti-list-check"/>By golden case</div>
              <table style={{width:'100%',borderCollapse:'collapse'}}>
                <thead><tr style={{fontSize:11,textTransform:'uppercase',letterSpacing:.06,color:'#9fadbf',borderBottom:'2px solid #e8eaed'}}>
                  <th style={{padding:'8px 12px',textAlign:'left',fontWeight:500}}>Case</th>
                  <th style={{padding:'8px 12px',textAlign:'right',fontWeight:500}}>Recall</th>
                  <th style={{padding:'8px 12px',textAlign:'right',fontWeight:500}}>Precision</th>
                </tr></thead>
                <tbody>
                  {(results.by_case||[]).map((c,i)=>(
                    <tr key={i} style={{borderBottom:'1px solid #e8eaed'}}>
                      <td style={{padding:'8px 12px',fontSize:12,fontFamily:'JetBrains Mono,monospace',color:'#1a6cf6'}}>{c.case_id}</td>
                      <td style={{padding:'8px 12px',textAlign:'right',fontSize:12,fontWeight:600,color:scoreColor(c.recall)}}>{pct(c.recall)}</td>
                      <td style={{padding:'8px 12px',textAlign:'right',fontSize:12,fontWeight:600,color:scoreColor(c.precision)}}>{pct(c.precision)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}

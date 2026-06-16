import { useState, useEffect, useRef, useCallback } from 'react'
import * as d3 from 'd3'
import { useApp } from '../AppContext'
import { AGENT_META, AGENT_ORDER, MODEL_PROVIDERS, repoName, shortName, prNum, prHead, prBase, prTitle, fmtDuration, canPostToGit, canOverrideGate, agentEngine } from '../state'
import { normalizeReport } from '../normalizeReport'
import { backendBase, backendHeaders, gitCfg, backendPost } from '../api'
import LivePipeline from '../components/LivePipeline'

// ── Helpers ────────────────────────────────────────────────────────────────────

function buildAnalysisTarget(state) {
  if (state.targetType==='pr'&&state.selectedPR) return { source:prHead(state.selectedPR), target:prBase(state.selectedPR), changeType:'pull_request', meta:{pr_id:prNum(state.selectedPR),pr_title:prTitle(state.selectedPR)} }
  if (state.targetType==='branch') return { source:state.sourceBranch, target:state.targetBranch, changeType:'branch_diff', meta:{} }
  return { source:state.commitSha, target:state.commitSha+'~1', changeType:'commit_diff', meta:{sha:state.commitSha} }
}

async function fetchRealDiff(state, target) {
  try {
    const slug = repoName(state.primaryRepo)
    const body = { cfg: gitCfg(state), change_type: target.changeType, repo_slug: slug, source: target.source||'', target: target.target||'', pr_id: target.meta?.pr_id||'' }
    const d = await backendPost(state, '/api/v1/git/diff', body)
    return d.diff || ''
  } catch (e) { console.warn('Diff fetch failed:', e.message); return '' }
}

// Candidate paths of the test file(s) that would cover a given source file.
function testCandidatesFor(path) {
  const p = (path||'').replace(/\\/g,'/')
  const dir = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : ''
  const file = p.slice(p.lastIndexOf('/')+1)
  const base = file.replace(/\.[^.]+$/,'')
  const ext = (file.match(/\.([^.]+)$/)||[])[1] || ''
  const out = []
  if (ext==='java' || ext==='kt') {
    const td = dir.replace('/main/','/test/')
    out.push(`${td}/${base}Test.${ext}`, `${td}/${base}Tests.${ext}`, `${td}/${base}IT.java`, `${dir}/${base}Test.${ext}`)
  } else if (ext==='py') {
    out.push(`${dir}/test_${file}`, `${dir}/${base}_test.py`, `${dir.replace(/[^/]*$/,'tests')}/test_${file}`, `tests/test_${file}`)
  } else if (['ts','tsx','js','jsx'].includes(ext)) {
    out.push(`${dir}/${base}.test.${ext}`, `${dir}/${base}.spec.${ext}`, `${dir}/__tests__/${base}.test.${ext}`)
  } else if (ext==='go') {
    out.push(`${dir}/${base}_test.go`)
  }
  return [...new Set(out.filter(Boolean))]
}

// Fetch existing test files from the repo (paired with each changed source file).
// Best-effort + bounded: skips silently if git creds/endpoint aren't available.
async function fetchExistingTests(state, tgt, diffText, headers) {
  const isTest = f => /(^|\/)(tests?|specs?|__tests__)\//i.test(f) || /(_test\.|\.test\.|\.spec\.|Test\.|Tests\.|Spec\.|_spec\.)/.test(f)
  const changed = Object.keys(parseDiffToSnippets(diffText)).filter(f=>!isTest(f)).slice(0,15)
  if (!changed.length) return []
  const cfg = gitCfg(state)
  const repo_slug = repoName(state.primaryRepo)
  const ref = tgt.source
  const found = []
  let calls = 0
  for (const src of changed) {
    for (const cand of testCandidatesFor(src)) {
      if (calls++ >= 40) return found    // hard cap on requests
      try {
        const r = await fetch(state.backendUrl+'/api/v1/git/file', {
          method:'POST', headers, body:JSON.stringify({ cfg, repo_slug, path:cand, ref }),
          signal: AbortSignal.timeout(8000),
        })
        if (r.ok) { const d = await r.json(); if (d.found) { found.push({ path:d.path, text:(d.content||'').slice(0,60000) }); break } }
      } catch(_) {}
    }
  }
  return found
}

// Cross-repo call-site search: ask the backend proxy to run the git provider's
// code-search API over the reviewer-declared dependent repos. Results are folded
// into reference impact + consumer impact server-side. Resilient — returns [] on
// any failure so analysis proceeds with the manual checklist fallback.
async function fetchCrossRepoRefs(state, tgt, diffText, headers) {
  const empty = { refs: [], backend: 'none', repos: [] }
  if (!state.connectedRepos?.length || !diffText || !state.backendUrl) return empty
  const cfg = gitCfg(state)
  const repo_slugs = state.connectedRepos.map(repoName)
  try {
    const r = await fetch(state.backendUrl+'/api/v1/git/xref', {
      method:'POST', headers,
      // 70s: the clone fallback (search-disabled Bitbucket Server) needs headroom.
      body: JSON.stringify({ cfg, repo_slugs, diff_text: diffText.slice(0, 400000), ref: tgt.source }),
      signal: AbortSignal.timeout(70000),
    })
    if (r.ok) {
      const d = await r.json()
      return { refs: d.references || [], backend: d.backend || 'none', repos: d.searched_repos || repo_slugs }
    }
  } catch(_) {}
  return { ...empty, repos: repo_slugs }
}

function parseDiffToSnippets(diffText) {
  const result = {}; if (!diffText) return result
  let currentFile=null, newLine=0, oldLine=0
  for (const raw of diffText.split('\n')) {
    if (raw.startsWith('+++ b/')) { currentFile=raw.slice(6).trim(); result[currentFile]=[]; continue }
    if (raw.startsWith('--- ')||raw.startsWith('+++ ')) continue
    const hm=raw.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/)
    if (hm) { oldLine=parseInt(hm[1]); newLine=parseInt(hm[2]); continue }
    if (!currentFile) continue
    if (raw.startsWith('+')) result[currentFile].push({newLine:newLine++,oldLine:null,content:raw.slice(1),type:'add'})
    else if (raw.startsWith('-')) result[currentFile].push({newLine:null,oldLine:oldLine++,content:raw.slice(1),type:'del'})
    else result[currentFile].push({newLine:newLine++,oldLine:oldLine++,content:raw.length?raw.slice(1):raw,type:'ctx'})
  }
  return result
}

function snippetNote(text) {
  return <div style={{fontSize:11,color:'#9fadbf',fontStyle:'italic',margin:'4px 0 2px',display:'flex',alignItems:'center',gap:5}}>
    <i className="ti ti-code-off" style={{fontSize:12}}/>{text}</div>
}

function getCodeSnippetJSX(file, lineRef, snipCache, ctx=3) {
  if (!file || !lineRef) return null
  // No diff available (e.g. a stored report opened from Insights/History).
  if (!snipCache || !Object.keys(snipCache).length)
    return snippetNote('Inline code shows on a fresh run (the diff isn’t stored with the report).')
  let lines = snipCache[file]
  if (!lines) {
    const norm = String(file).replace(/\\/g,'/')
    const base = norm.split('/').pop()
    const key = Object.keys(snipCache).find(k=>{
      const kn=k.replace(/\\/g,'/'); return kn===norm||kn.endsWith('/'+norm)||norm.endsWith('/'+kn)||kn.split('/').pop()===base
    })
    lines = key ? snipCache[key] : null
  }
  if (!lines||!lines.length) return snippetNote(`No diff lines for ${String(file).split(/[\\/]/).pop()} in this change.`)
  const m=String(lineRef).match(/^(\d+)(?:[,-](\d+))?$/)
  if (!m) return null
  const s=parseInt(m[1]), e=m[2]?parseInt(m[2]):s
  let relevant=lines.filter(l=>{const ln=l.newLine??l.oldLine; return ln!=null&&ln>=s-ctx&&ln<=e+ctx})
  // Line is outside the changed window → show the nearest changed lines instead
  // of nothing, with a note, so the reviewer still gets context.
  let note=null
  if (!relevant.length) {
    relevant = lines.filter(l=>l.type==='add').slice(0,6)
    if (!relevant.length) relevant = lines.slice(0,6)
    if (!relevant.length) return snippetNote(`Line ${lineRef} not in the changed hunks.`)
    note = snippetNote(`Line ${lineRef} is outside the changed lines — showing nearby changes:`)
  }
  return (<>
    {note}
    <div className="code-snippet">
      {relevant.map((l,i)=>(
        <div key={i} className={`code-snippet-line ${l.type}`}>
          <span className="code-snippet-ln">{l.newLine??l.oldLine??''}</span>
          <span className="code-snippet-content">{l.type==='add'?'+':l.type==='del'?'-':' '}{l.content}</span>
        </div>
      ))}
    </div>
  </>)
}

function saveHistory(report, target, state) {
  const entry = { id:Date.now(), ts:new Date().toISOString(), repo:repoName(state.primaryRepo), target:target.changeType+' '+target.source, gate:report.gate_decision, risk:report.overall_risk, score:report.risk_score }
  const history = [entry, ...state.history].slice(0,50)
  localStorage.setItem('analysisHistory', JSON.stringify(history))
  return history
}

// ── Running state UI ────────────────────────────────────────────────────────────

function RunningView({ state, update, showToast }) {
  const [agentMap, setAgentMap] = useState({})
  const [elapsed, setElapsed] = useState(0)
  const [progress, setProgress] = useState(0)
  const [progressLabel, setProgressLabel] = useState('Waiting for agents…')
  const [agentStatus, setAgentStatus] = useState('Initialising agents…')
  const [diffInfo, setDiffInfo] = useState('')
  const [xrefInfo, setXrefInfo] = useState('')
  const [simHint, setSimHint] = useState('')
  const target = useRef(buildAnalysisTarget(state))
  const startTime = useRef(Date.now())
  const aborted = useRef(false)
  const started = useRef(false)   // guard against double-run (StrictMode / remount)

  useEffect(() => {
    // Reset the abort flag on every (re)mount BEFORE the started-guard. Under
    // React 18 StrictMode the effect runs mount → cleanup → remount; the cleanup
    // sets aborted=true, so without resetting here the (still in-flight) run would
    // see aborted=true and bail immediately — flashing a false "timed out" error.
    aborted.current = false
    const clockTimer = setInterval(() => setElapsed(Math.round((Date.now()-startTime.current)/1000)), 500)
    if (!started.current) {       // kick the analysis off exactly once
      started.current = true
      runAnalysis()
    }
    return () => { aborted.current = true; clearInterval(clockTimer) }
  }, [])

  async function runAnalysis() {
    const tgt = target.current
    const headers = backendHeaders(state)

    setAgentStatus('Fetching diff…')
    setProgress(5)
    const diffText = await fetchRealDiff(state, tgt)
    update({ diffText: diffText||'' })
    const diffLines = diffText ? diffText.split('\n').length : 0
    const diffKb = diffText ? Math.round(diffText.length/1024) : 0
    setDiffInfo(diffText ? `Diff fetched — ${diffLines.toLocaleString()} lines · ${diffKb} KB` : '⚠ No diff — check token permissions')

    // Repo-aware coverage: fetch existing test files (paired with changed source)
    // so methods already tested in the repo aren't flagged as untested.
    let existingTests = []
    try {
      if (diffText && state.backendUrl && state.primaryRepo) {
        setAgentStatus('Locating existing tests in repo…')
        existingTests = await fetchExistingTests(state, tgt, diffText, headers)
      }
    } catch(_) {}

    // Cross-repo impact: search reviewer-declared dependent repos for call-sites
    // of the changed symbols (provider code-search API → clone+grep fallback).
    let externalRefs = []
    try {
      if (diffText && state.backendUrl && state.connectedRepos.length) {
        setAgentStatus(`Searching ${state.connectedRepos.length} dependent repo(s) for call-sites…`)
        const xref = await fetchCrossRepoRefs(state, tgt, diffText, headers)
        externalRefs = xref.refs
        const labelMap = { local_mirror:'warm local mirror', bitbucket_server_search:'Bitbucket Server search',
                      github_search:'GitHub code search', bitbucket_cloud_search:'Bitbucket code search',
                      clone_grep:'shallow clone + grep', none:'no backend', unsupported:'unsupported provider' }
        const via = String(xref.backend||'').split('+').map(b=>labelMap[b]||b).join(' + ')
        const nRepos = (xref.repos||[]).length
        setXrefInfo(externalRefs.length
          ? `Traced ${externalRefs.length} cross-repo call-site${externalRefs.length===1?'':'s'} across ${nRepos} dependent repo${nRepos===1?'':'s'} via ${via}.`
          : `No cross-repo call-sites found in ${nRepos} dependent repo${nRepos===1?'':'s'}${xref.backend==='none'?'':` (via ${via})`}.`)
      }
    } catch(_) {}

    if (state.backendUrl) {
      try {
        const payload = {
          repo_url: state.provider==='github'?`https://github.com/${repoName(state.primaryRepo)}`:repoName(state.primaryRepo),
          source_ref: tgt.source, target_ref: tgt.target, change_type: tgt.changeType,
          diff_text: diffText,
          deep_scan: !!state.deepScan,
          llm_config: { provider:state.modelProvider, model:state.modelName, api_key:state.modelApiKey, base_url:state.modelBaseUrl, api_version:state.modelApiVer },
          metadata: { provider:state.provider, connected_repos:state.connectedRepos.map(repoName), diff_lines:diffLines,
            functional_docs:(state.functionalDocs||[]).map(d=>({name:d.name,text:(d.text||'').slice(0,40000)})).slice(0,10),
            existing_tests: existingTests, external_references: externalRefs, ...tgt.meta }
        }
        setAgentStatus('Submitting to backend…'); setProgress(10)
        const submitResp = await fetch(state.backendUrl+'/api/v1/analyse', { method:'POST', headers, body:JSON.stringify(payload) })
        if (submitResp.status===401) throw new Error('Backend 401 — set SKIP_AUTH=true in .env')
        if (!submitResp.ok) throw new Error(`Backend error ${submitResp.status}`)
        const { request_id: rid } = await submitResp.json()
        update({ lastRequestId: rid })
        setAgentStatus('Agents running…'); setProgress(15)

        let lastProg = {}
        const progTimer = setInterval(async () => {
          try {
            const pr = await fetch(state.backendUrl+'/api/v1/progress/'+rid, {headers})
            if (!pr.ok) return
            const { agents } = await pr.json()
            agents.forEach(a=>{ lastProg[a.agent]=a })
            const done = Object.values(lastProg).filter(a=>a.status==='done'||a.status==='fallback').length
            const running = Object.values(lastProg).filter(a=>a.status==='running').length
            const total = AGENT_ORDER.length
            const pct = Math.round(15+(done/total)*75)
            setProgress(pct)
            setProgressLabel(running ? `Running: ${agents.filter(a=>a.status==='running').map(a=>AGENT_META[a.agent]?.label||a.agent).join(', ')}` : done ? `${done}/${total} agents complete` : 'Agents initialising…')
            setAgentStatus(`${done} of ${total} agents complete`)
            if (!aborted.current) setAgentMap({...lastProg})
          } catch(_) {}
        }, 1500)

        let report = null
        let netFails = 0
        let runningSince = null          // ms when the backend actually started running
        const QUEUE_MAX_MS = 15*60*1000  // tolerate up to 15 min waiting in the queue
        const RUN_MAX_MS   = 12*60*1000  // up to 12 min of actual processing
        const queueStart   = Date.now()
        while (!aborted.current) {
          await new Promise(r=>setTimeout(r,2000))
          if (aborted.current) break
          try {
            // Check admission status first so a QUEUED wait isn't mistaken for a hang.
            const st = await fetch(state.backendUrl+'/api/v1/status/'+rid, {headers})
              .then(r=>r.ok?r.json():null).catch(()=>null)
            const s = (st && st.status) || ''
            if (s === 'queued') {
              const pos = st.queue_position || 0
              setAgentStatus(pos ? `Queued — position ${pos}…` : 'Queued — waiting for a free slot…')
              setProgressLabel(pos ? `Waiting in queue (position ${pos} of ${st.queue_total||pos})` : 'Waiting for a free analysis slot…')
              if (Date.now()-queueStart > QUEUE_MAX_MS) throw new Error('Still queued after 15 min — the server is very busy. Please try again later.')
              continue   // do NOT count queue time against the processing timeout
            }
            if (s.startsWith('error')) throw new Error('Backend: '+s.replace(/^error:\s*/,''))
            if (runningSince === null) runningSince = Date.now()

            const poll = await fetch(state.backendUrl+'/api/v1/report/'+rid+'?fmt=full', {headers})
            netFails = 0
            if (poll.status===200) {
              const json = await poll.json()
              if (Array.isArray(json.errors) && json.errors.length && !json.risk && !json.security) {
                throw new Error('Backend analysis failed — '+json.errors[0])
              }
              report = json; break
            }
            // 202 = running, 404 = brief save race → keep polling until the run budget elapses.
            if (runningSince && Date.now()-runningSince > RUN_MAX_MS) {
              throw new Error('Timed out while the backend was processing. Check the model API key (Configure → AI Model) and that the backend isn’t rate-limited.')
            }
          } catch(e) {
            if (e.message && (e.message.startsWith('Backend') || e.message.includes('analysis failed')
                || e.message.includes('queued') || e.message.includes('Timed out'))) throw e
            if (++netFails >= 5) throw new Error('Backend became unreachable while waiting for the report')
          }
        }
        clearInterval(progTimer)
        if (report && !aborted.current) {
          setProgress(100)
          const normalized = normalizeReport(report)
          const history = saveHistory(normalized, tgt, state)
          // Clear analysisRequested so the completed report is sticky — navigating
          // away and back can never silently re-trigger a fresh run that overwrites it.
          update({ report: normalized, history, analysisRequested: false })
          return
        }
        throw new Error('Timed out waiting for the backend to finish. Check that a valid model API key is set (Configure → AI Model) and the backend isn’t rate-limited.')
      } catch(e) {
        const isNetwork = e.message==='Failed to fetch'||e.message.includes('NetworkError')||e.message.includes('CORS')
        if (isNetwork) setSimHint('Backend unreachable — switching to AI simulation')
        else setSimHint(`Error: ${e.message} — falling back to simulation`)
      }
    }

    // ── Simulation fallback ─────────────────────────────────────────────────────
    if (!state.backendUrl) setSimHint('No backend configured. Running AI simulation mode.')
    const simPhases = [
      ['code_analysis','security'],
      ['ast_analysis','secrets_entropy','taint_analysis','iac_analysis','temporal_risk','schema_change'],
      ['qa_scenarios','reference_impact','performance_impact','data_privacy','maintainability','license_compliance','observability','functional_validation'],
      ['dependency','test_coverage','interface','risk'],
      ['remediation'],
    ]
    let simTok = 0
    const simAgentMap = {}
    for (const phase of simPhases) {
      if (aborted.current) return
      phase.forEach(k=>{ simAgentMap[k]={status:'running',tokens:0,duration_s:0,elapsed_s:0} })
      setAgentMap({...simAgentMap})
      setAgentStatus(`Running: ${phase.map(k=>AGENT_META[k]?.label||k).join(', ')}…`)
      const phaseDelay = 600+Math.random()*600
      await new Promise(r=>setTimeout(r,phaseDelay))
      if (aborted.current) return
      phase.forEach(k=>{
        const tok = k.includes('analysis')||k==='security'||k==='risk'?800+Math.floor(Math.random()*600):0
        simTok+=tok
        simAgentMap[k]={ status:tok?'done':'fallback', tokens:tok, duration_s:parseFloat((phaseDelay/1000*(0.7+Math.random()*0.5)).toFixed(2)), elapsed_s:0, model:tok?'anthropic/claude-sonnet-4-6':'' }
      })
      setAgentMap({...simAgentMap})
      const done=Object.values(simAgentMap).filter(a=>a.status==='done'||a.status==='fallback').length
      setProgress(Math.round(10+(done/AGENT_ORDER.length)*80))
    }
    setProgress(92)

    // Build a mock report
    const mockReport = buildMockReport(tgt, repoName(state.primaryRepo), diffText, simTok)
    setProgress(100)
    const history = saveHistory(mockReport, tgt, state)
    update({ report: mockReport, history, analysisRequested: false })
  }

  return (
    <div style={{maxWidth:700,margin:'0 auto',padding:'20px 0'}}>
      <div style={{display:'flex',alignItems:'center',gap:16,marginBottom:22,paddingBottom:18,borderBottom:'1px solid #e8eaed'}}>
        <div style={{width:44,height:44,background:'#0d1117',borderRadius:10,display:'flex',alignItems:'center',justifyContent:'center',flexShrink:0}}>
          <span className="spinner" style={{width:22,height:22,borderColor:'rgba(255,255,255,.2)',borderTopColor:'#fff'}}/>
        </div>
        <div style={{flex:1,minWidth:0}}>
          <div style={{fontSize:18,fontWeight:600,color:'#0d1117',fontFamily:'Instrument Serif,serif',letterSpacing:'-.01em'}}>Running Impact Analysis</div>
          <div style={{fontSize:13,color:'#7a8494',marginTop:2}}>{agentStatus}</div>
        </div>
        <div style={{textAlign:'right',flexShrink:0}}>
          <div style={{fontSize:22,fontWeight:700,fontFamily:'JetBrains Mono,monospace',color:'#0d1117',lineHeight:1}}>{elapsed}s</div>
          <div style={{fontSize:10,color:'#9fadbf',marginTop:2}}>elapsed</div>
        </div>
      </div>
      <div style={{marginBottom:18}}>
        <div style={{display:'flex',justifyContent:'space-between',fontSize:11,color:'#9fadbf',marginBottom:5}}>
          <span>{progressLabel}</span><span>{progress}%</span>
        </div>
        <div style={{height:6,background:'#e8eaed',borderRadius:3,overflow:'hidden'}}>
          <div style={{height:'100%',background:'linear-gradient(to right,#1a6cf6,#10b981)',borderRadius:3,transition:'width .6s ease',width:`${progress}%`}}/>
        </div>
      </div>
      {diffInfo && (
        <div style={{marginBottom:14,padding:'8px 12px',background:'#f7f8fa',border:'1px solid #e8eaed',borderRadius:7,fontSize:12,color:'#7a8494',display:'flex',alignItems:'center',gap:7}}>
          <i className="ti ti-file-code" style={{fontSize:14}}/><span>{diffInfo}</span>
        </div>
      )}
      {xrefInfo && (
        <div style={{marginBottom:14,padding:'8px 12px',background:'#f3f6fb',border:'1px solid #d8e0ec',borderRadius:7,fontSize:12,color:'#3a4452',display:'flex',alignItems:'center',gap:7}}>
          <i className="ti ti-affiliate" style={{fontSize:14}}/><span>{xrefInfo}</span>
        </div>
      )}
      <div className="pipeline-scroll"><LivePipeline agentMap={agentMap}/></div>
      {simHint && (
        <div style={{marginTop:14,textAlign:'center',fontSize:11,color:'#c0c9d8'}}>
          <span style={{background:'#fff8ec',border:'1px solid #f59e0b',borderRadius:7,padding:'6px 12px',color:'#92400e',display:'inline-flex',alignItems:'center',gap:6}}>
            <i className="ti ti-info-circle"/><strong>AI Simulation mode</strong> — {simHint}
          </span>
        </div>
      )}
    </div>
  )
}

function buildMockReport(target, repoSlug, diffText, tokenUsage) {
  const hasSecurityFiles = diffText && (diffText.includes('password')||diffText.includes('secret')||diffText.includes('token')||diffText.includes('auth'))
  const hasDbFiles = diffText && (diffText.includes('.sql')||diffText.includes('migration')||diffText.includes('schema'))
  const diffLines = diffText ? diffText.split('\n').length : 0
  const riskScore = Math.min(100, Math.round(20 + (hasSecurityFiles?30:0) + (hasDbFiles?15:0) + Math.min(diffLines/20,20)))
  const gate = riskScore >= 70 ? 'BLOCK' : riskScore >= 40 ? 'HOLD' : 'APPROVE'
  return {
    gate_decision: gate, overall_risk: riskScore>=70?'high':riskScore>=40?'medium':'low', risk_score: riskScore,
    rationale: `Analysis of ${repoSlug} — ${diffLines} diff lines. ${hasSecurityFiles?'Security-related code detected. ':''}${hasDbFiles?'Database changes detected. ':''}Risk score ${riskScore}/100.`,
    code_analysis: { summary:`This change modifies ${repoSlug}. Analysed ${diffLines} lines of diff.`, change_type: target.changeType==='pull_request'?'feature':'patch', complexity_delta: Math.round(Math.random()*3), findings:[] },
    security: { overall_severity: hasSecurityFiles?'high':'low', secrets_detected: false, findings: hasSecurityFiles?[{cwe:'CWE-200',severity:'medium',description:'Sensitive data may be exposed in this code path',file:'',line_range:''}]:[] },
    dependency: { blast_radius_score: Math.round(riskScore*0.6), affected_services:[], changed_packages:[], cve_hits:[] },
    test_coverage: { coverage_delta: -2, regression_risk:'low', uncovered_paths:[] },
    interface: { breaking_changes:[] },
    schema_change: { has_destructive:hasDbFiles, has_irreversible:false, changes: hasDbFiles?[{change_type:'alter_table',table:'unknown',severity:'medium',reversible:true}]:[] },
    qa_scenarios: { scenarios:[], total_scenarios:0, summary:'', critical_count:0, high_count:0, coverage_areas:[] },
    reference_impact: null, performance_impact: null, data_privacy: null, maintainability: null,
    license_compliance: null, observability: null, secrets_entropy: null, ast_analysis: null,
    taint_analysis: null, iac_analysis: null, temporal_risk: null,
    risk: { deployment_strategy:'standard', rollback_feasibility:'easy', deployment_guidance:'Deploy during low-traffic period. Monitor error rates.' },
    remediation: { fix_suggestions:['Review all changed code for security implications','Add or update unit tests for changed functions','Verify no secrets or credentials are hardcoded'], validation_checklist:['Run full test suite','Security scan completed','Code reviewed by senior developer'], deployment_strategy:'standard', executive_summary:`This change modifies ${repoSlug} with a risk score of ${riskScore}/100. ${gate === 'APPROVE' ? 'The change appears safe to deploy.' : gate === 'HOLD' ? 'Review is recommended before deploying.' : 'This change should be blocked until issues are resolved.'}` },
    token_usage: tokenUsage||0, duration_s: 3.5, agent_timings:[], errors:[],
  }
}

// ── Tab content renderers ──────────────────────────────────────────────────────

function AllFindings(r) {
  const out=[]
  const push=(sev,cat,msg,file,line)=>out.push({severity:(sev||'low').toLowerCase(),category:cat,message:msg||'',file:file||'',line:line||''})
  ;(r.security?.findings||[]).forEach(f=>push(f.severity,'Security',`${f.cwe?f.cwe+' — ':''}${f.description}`,f.file,f.line_range))
  if(r.security?.secrets_detected)push('critical','Security','Hardcoded secret detected — rotate immediately','','')
  ;(r.performance_impact?.findings||[]).forEach(f=>push(f.severity,'Performance',f.description,f.file_path||f.file,f.line))
  ;(r.data_privacy?.pii_findings||r.data_privacy?.findings||[]).forEach(f=>push(f.risk_level||f.severity,'Privacy',`${(f.pii_type||'PII').toUpperCase()} — ${f.description}`,f.file_path||f.file,f.line))
  ;(r.maintainability?.issues||[]).forEach(f=>push(f.severity,'Quality',f.description,f.file_path||f.file,f.line))
  ;(r.observability?.findings||[]).forEach(f=>push(f.severity,'Observability',f.description,f.file_path||f.file,f.line))
  ;(r.schema_change?.changes||[]).filter(c=>['high','critical'].includes((c.severity||'').toLowerCase())).forEach(c=>push(c.severity,'Schema',`${c.change_type} on ${c.table||'table'}${c.reversible===false?' (irreversible)':''}`,  '',''))
  ;(r.interface?.breaking_changes||[]).forEach(b=>push(b.severity||'high','API',`${b.break_type||'breaking'} change: ${b.path||''}`, '', ''))
  const order={critical:0,high:1,medium:2,low:3}
  out.sort((a,b)=>(order[a.severity]??9)-(order[b.severity]??9))
  return out
}

function SevChip({sev}) {
  const m={critical:['#fff1f2','#991b1b','🚨'],high:['#fff1f2','#b91c1c','🔴'],medium:['#fffbeb','#92400e','🟡'],low:['#eff6ff','#1e40af','🔵']}[sev]||['#f0f2f5','#7a8494','•']
  return <span style={{background:m[0],color:m[1],borderRadius:4,padding:'1px 7px',fontSize:10,fontWeight:700,whiteSpace:'nowrap'}}>{m[2]} {sev}</span>
}

// "Location unverified" badge — the generalised evidence guard flagged this
// finding because its cited file isn't in the diff (likely hallucinated location).
function UnvBadge({ f }) {
  if (!f?.unverified) return null
  return <span title="The cited file isn't in this diff — shown for awareness; verify before acting."
    style={{marginLeft:6,fontSize:10,fontWeight:700,padding:'1px 7px',borderRadius:10,background:'#fff7ed',color:'#9a3412',border:'1px solid #fed7aa',whiteSpace:'nowrap'}}>
    <i className="ti ti-map-pin-off" style={{fontSize:11,marginRight:3}}/>location unverified</span>
}

// Reviewer feedback control — mark a finding as false positive / valid.
// Feeds the feedback loop so noisy checks surface in Insights over time.
// Rendered as two clearly-visible pill buttons so reviewers can triage at a glance.
function FindingFeedback({ r, agent, category='', file='' }) {
  const { state } = useApp()
  const [sent, setSent] = useState('')
  const [busy, setBusy] = useState(false)
  async function send(verdict) {
    if (busy) return
    if (!state.backendUrl || !r.request_id) { setSent('need-backend'); return }
    setBusy(true)
    try {
      await backendPost(state, `/api/v1/report/${r.request_id}/feedback`,
        { agent, category, file_path: file, verdict })
      setSent(verdict)
    } catch { setSent('error') } finally { setBusy(false) }
  }
  const pill = (extra={}) => ({
    display:'inline-flex',alignItems:'center',gap:5,fontSize:11,fontWeight:600,
    padding:'4px 11px',borderRadius:20,cursor:busy?'default':'pointer',
    lineHeight:1,transition:'all .12s',userSelect:'none',...extra,
  })
  if (sent === 'false_positive')
    return <span style={pill({background:'#eef1f5',color:'#5b6675',border:'1px solid #d4dae2',cursor:'default'})}>⚐ Marked false positive</span>
  if (sent === 'valid')
    return <span style={pill({background:'#e9f8ef',color:'#166534',border:'1px solid #b5e8cf',cursor:'default'})}>✓ Confirmed valid</span>
  if (sent === 'need-backend')
    return <span style={{fontSize:11,color:'#b91c1c'}}>Connect a backend to record reviewer feedback.</span>
  if (sent === 'error')
    return <span style={{fontSize:11,color:'#b91c1c'}}>Couldn’t save — retry.</span>
  return (
    <span style={{display:'inline-flex',gap:8,alignItems:'center',flexWrap:'wrap'}}>
      <span style={{fontSize:10,color:'#9fadbf',textTransform:'uppercase',letterSpacing:.4,marginRight:1}}>Triage:</span>
      <button type="button" disabled={busy} onClick={()=>send('valid')}
        title="Confirm this is a real issue worth acting on"
        style={pill({background:'#fff',color:'#166534',border:'1px solid #9bd9b8'})}>✓ Valid</button>
      <button type="button" disabled={busy} onClick={()=>send('false_positive')}
        title="This finding is a false positive — teach CIAA to stop flagging it for this repo"
        style={pill({background:'#fff',color:'#9a3412',border:'1px solid #e6c4a6'})}>⚐ False positive</button>
    </span>
  )
}

function GateHero({r}) {
  const gate=(r.gate_decision||r.gate||'HOLD').toUpperCase()
  const risk=r.risk_score||0
  const m={APPROVE:['#f0fdf4','#166534','#86efac','✅','Ready to merge'],HOLD:['#fffbeb','#92400e','#fcd34d','⚠️','Needs attention before merge'],BLOCK:['#fff1f2','#991b1b','#fca5a5','🚫','Blocked — must fix first']}[gate]||['#f7f8fa','#7a8494','#e8eaed','•','']
  const reasons = r.gate_policy_reasons || []
  const overrode = r.gate_overridden_by_policy
  return (
    <div style={{marginBottom:16}}>
      <div style={{display:'flex',alignItems:'center',gap:16,padding:'18px 20px',background:m[0],border:`1.5px solid ${m[2]}`,borderRadius:reasons.length?'12px 12px 0 0':12}}>
        <div style={{fontSize:34}}>{m[3]}</div>
        <div style={{flex:1}}>
          <div style={{fontSize:20,fontWeight:800,color:m[1]}}>{gate}
            {overrode && <span style={{fontSize:10,fontWeight:700,background:'#1a2332',color:'#fff',borderRadius:4,padding:'2px 7px',marginLeft:8,verticalAlign:'middle'}} title={`AI proposed ${r.ai_proposed_gate||'?'}; policy enforced ${gate}`}>POLICY-ENFORCED</span>}
          </div>
          <div style={{fontSize:13,color:'#5a6a7e',marginTop:2}}>{m[4]} · {(r.overall_risk||'').toUpperCase()} risk</div>
        </div>
        <div style={{textAlign:'right'}}>
          <div style={{fontSize:28,fontWeight:800,fontFamily:'JetBrains Mono,monospace',color:m[1]}}>{risk}</div>
          <div style={{fontSize:10,color:'#9fadbf'}}>risk /100</div>
        </div>
      </div>
      {reasons.length>0 && (
        <div style={{border:`1.5px solid ${m[2]}`,borderTop:'none',borderRadius:'0 0 12px 12px',background:'#fff',padding:'12px 18px'}}>
          <div style={{fontSize:11,fontWeight:700,textTransform:'uppercase',letterSpacing:'.06em',color:'#7a8494',marginBottom:6}}>
            Why this gate (deterministic policy)
          </div>
          <ul style={{margin:0,paddingLeft:18,fontSize:12.5,color:'#3d4652',lineHeight:1.7}}>
            {reasons.map((reason,i)=><li key={i}>{reason}</li>)}
          </ul>
        </div>
      )}
    </div>
  )
}

function SummaryTab({r, snipCache}) {
  const [persona, setPersona] = useState(null)
  const effectivePersona = persona || (canOverrideGate({ciaaPerms:r._ciaaPerms}) ? 'reviewer' : 'developer')
  const findings = AllFindings(r)
  const blockers = findings.filter(f=>['critical','high'].includes(f.severity))
  const fixes = r.remediation?.fix_suggestions||[]
  const scenarios = r.qa_scenarios?.scenarios||[]
  const covDelta = parseFloat(r.test_coverage?.coverage_delta||0)

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:16,flexWrap:'wrap'}}>
        <div style={{display:'flex',gap:4,background:'#f0f2f5',borderRadius:8,padding:3}}>
          {['developer','reviewer'].map(p=>(
            <button key={p} onClick={()=>setPersona(p)} style={{border:'none',background:effectivePersona===p?'#fff':'transparent',boxShadow:effectivePersona===p?'0 1px 2px rgba(0,0,0,.1)':'none',borderRadius:6,padding:'6px 16px',fontSize:12,fontWeight:effectivePersona===p?700:500,color:effectivePersona===p?'#0d1117':'#7a8494',cursor:'pointer'}}>
              {p==='developer'?'👨‍💻 Developer view':'🔍 Reviewer view'}
            </button>
          ))}
        </div>
        <span style={{fontSize:11,color:'#9fadbf'}}>{effectivePersona==='developer'?'What to fix before requesting review':'Decision view — what to scrutinise and approve'}</span>
      </div>
      {r.suppressed_count>0 && (
        <div style={{display:'flex',alignItems:'flex-start',gap:8,background:'#f3f7ff',border:'1px solid #dbe7ff',borderRadius:8,padding:'9px 12px',marginBottom:14,fontSize:12.5,color:'#334155'}}
          title={(r.suppressed_notes||[]).join('\n')}>
          <i className="ti ti-filter-off" style={{color:'#1a6cf6',marginTop:1}}/>
          <span><strong>{r.suppressed_count} finding(s) auto-suppressed</strong> — repeatedly marked false positive by reviewers for this repo. Hover for details.</span>
        </div>
      )}
      <ReviewPlanCard r={r}/>
      <TopIssues r={r}/>
      {effectivePersona==='developer' ? <DeveloperView r={r} findings={findings} blockers={blockers} fixes={fixes} scenarios={scenarios} covDelta={covDelta}/> : <ReviewerView r={r} findings={findings}/>}
    </div>
  )
}

// Reviewer triage: every changed file bucketed into must-fix / needs-review /
// auto-approvable, with read-first ordering and an effort estimate. The "where
// do I spend my attention" view.
function ReviewPlanCard({ r }) {
  const rp = r.review_plan
  if (!rp) return null
  const buckets = [
    { key:'must_fix',        files:rp.must_fix||[],        c:'#991b1b', bg:'#fef2f2', bd:'#fca5a5', icon:'🚨', label:'Must fix' },
    { key:'needs_review',    files:rp.needs_review||[],    c:'#92400e', bg:'#fffbeb', bd:'#fcd34d', icon:'⚠️', label:'Needs a human' },
    { key:'auto_approvable', files:rp.auto_approvable||[], c:'#166534', bg:'#f0fdf4', bd:'#86efac', icon:'✅', label:'Auto-approvable' },
  ]
  const sevC = {critical:'#991b1b',high:'#b91c1c',medium:'#92400e',low:'#1e40af'}
  return (
    <div className="card" style={{marginBottom:14}}>
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:8,marginBottom:4}}>
        <div className="section-heading" style={{marginBottom:0}}><i className="ti ti-list-check"/>Review plan</div>
        <div style={{fontSize:13,fontWeight:700,color:'#1a2332'}}>{rp.headline}</div>
      </div>
      <div style={{fontSize:12,color:'#7a8494',marginBottom:12}}>{rp.summary}</div>
      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(220px,1fr))',gap:10}}>
        {buckets.map(b=>(
          <div key={b.key} style={{border:`1px solid ${b.bd}`,background:b.bg,borderRadius:8,padding:'10px 12px'}}>
            <div style={{display:'flex',alignItems:'center',gap:6,fontSize:12,fontWeight:700,color:b.c,marginBottom:8}}>
              <span>{b.icon}</span><span>{b.label}</span>
              <span style={{marginLeft:'auto',background:'#fff',border:`1px solid ${b.bd}`,borderRadius:10,padding:'0 8px',fontSize:11}}>{b.files.length}</span>
            </div>
            {b.files.length===0
              ? <div style={{fontSize:11.5,color:'#9fadbf'}}>None</div>
              : b.files.slice(0,8).map((f,i)=>(
                <div key={i} style={{marginBottom:b.key==='auto_approvable'?2:7}}>
                  <div style={{display:'flex',alignItems:'center',gap:6}}>
                    {b.key!=='auto_approvable' && <span style={{width:7,height:7,borderRadius:'50%',background:sevC[f.top_severity]||'#9fadbf',flexShrink:0}}/>}
                    <code style={{fontSize:11.5,color:'#1a2332',wordBreak:'break-all'}}>{f.file.split('/').slice(-2).join('/')}</code>
                  </div>
                  {b.key!=='auto_approvable' && (f.reasons||[]).length>0 &&
                    <div style={{fontSize:11,color:b.c,marginLeft:13,marginTop:1}}>{f.reasons.join(' · ')}</div>}
                </div>
              ))}
            {b.files.length>8 && <div style={{fontSize:11,color:'#9fadbf',marginTop:4}}>+{b.files.length-8} more</div>}
          </div>
        ))}
      </div>
      {(rp.read_first||[]).length>0 && (
        <div style={{marginTop:10,fontSize:11.5,color:'#5b6675'}}>
          <strong>Read first:</strong> {rp.read_first.slice(0,5).map((p,i)=><code key={i} style={{fontSize:11,marginLeft:i?6:4}}>{p.split('/').slice(-1)[0]}</code>)}
        </div>
      )}
    </div>
  )
}

// Ranked, cross-agent-deduplicated "what must I look at" list. One row per real
// issue: findings from several agents on the same location are merged, agreement
// raises confidence, unverified locations are penalised in rank.
function TopIssues({ r }) {
  const issues = r.top_issues || []
  if (!issues.length) return null
  const sevC  = {critical:'#991b1b',high:'#b91c1c',medium:'#92400e',low:'#1e40af'}
  const confC = {high:'#16a34a',medium:'#d97706',low:'#9ca3af'}
  const agentLabel = a => (AGENT_META[a]?.label) || a.replace(/_/g,' ')
  return (
    <div className="card" style={{marginBottom:14}}>
      <div className="section-heading"><i className="ti ti-target-arrow"/>Top issues to review ({issues.length})</div>
      <div style={{fontSize:11.5,color:'#9fadbf',marginBottom:10}}>
        Deduplicated across all agents and ranked by severity, cross-agent agreement and location confidence — start here.
      </div>
      {issues.map((it,i)=>(
        <div key={i} className="finding" style={{alignItems:'flex-start',gap:10}}>
          <span style={{fontFamily:'var(--mono)',fontSize:13,fontWeight:700,color:'#9fadbf',minWidth:22,textAlign:'right',marginTop:2}}>{i+1}</span>
          <span style={{fontSize:10,fontWeight:700,padding:'2px 8px',borderRadius:10,textTransform:'uppercase',whiteSpace:'nowrap',flexShrink:0,marginTop:2,
            background:`${sevC[it.severity]||'#7a8494'}1a`,color:sevC[it.severity]||'#7a8494',border:`1px solid ${sevC[it.severity]||'#7a8494'}55`}}>{it.severity}</span>
          <div className="finding-body">
            <div className="finding-desc">{it.title}{it.unverified && <UnvBadge f={it}/>}</div>
            <div className="finding-file" style={{display:'flex',alignItems:'center',gap:8,flexWrap:'wrap',marginTop:3}}>
              {it.file_path && <span style={{fontFamily:'var(--mono)'}}>{it.file_path}{it.line?`:${it.line}`:''}</span>}
              <span title={`Location confidence: ${it.confidence}`} style={{display:'inline-flex',alignItems:'center',gap:4,fontSize:10,color:confC[it.confidence]||'#9ca3af',fontWeight:700}}>
                <span style={{width:7,height:7,borderRadius:4,background:confC[it.confidence]||'#9ca3af',display:'inline-block'}}/>{it.confidence} confidence
              </span>
              {(it.agents||[]).map(a=>(
                <span key={a} style={{fontSize:10,fontWeight:600,padding:'1px 7px',borderRadius:9,background:'#f0f4fa',color:'#3a4452',border:'1px solid #dde5f0'}}>{agentLabel(a)}</span>
              ))}
              {(it.agents||[]).length>1 && <span style={{fontSize:10,color:'#16a34a',fontWeight:700}}>✓ {(it.agents||[]).length} agents agree</span>}
            </div>
            {(it.descriptions||[]).length>1 && (
              <div style={{fontSize:11,color:'#7a8494',marginTop:4,lineHeight:1.45}}>
                {(it.descriptions||[]).slice(1,3).map((d,j)=><div key={j}>· {d}</div>)}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

// One-line "bottom line" shown at the top of each persona view.
function Headline({ tone, icon, text }) {
  const c = {
    bad:  ['#fff1f2', '#991b1b', '#fecaca'],
    warn: ['#fffbeb', '#92400e', '#fde68a'],
    good: ['#f0fdf4', '#166534', '#bbf7d0'],
  }[tone] || ['#f3f7ff', '#1e40af', '#dbe7ff']
  return (
    <div style={{
      display:'flex', alignItems:'center', gap:10, marginBottom:14,
      background:c[0], color:c[1], border:`1px solid ${c[2]}`, borderRadius:10,
      padding:'12px 16px', fontSize:14, fontWeight:600, lineHeight:1.4,
    }}>
      <i className={`ti ${icon}`} style={{ fontSize:20, flexShrink:0 }}/>
      <span>{text}</span>
    </div>
  )
}

function topFinding(findings) {
  const f = findings.find(x=>x.severity==='critical') || findings.find(x=>x.severity==='high') || findings[0]
  if (!f) return ''
  const loc = f.file ? ` in ${f.file}${f.line?':'+f.line:''}` : ''
  return `${f.severity} ${f.category||'issue'}${loc}`
}

function DeveloperView({r, findings, blockers, fixes, scenarios, covDelta}) {
  // Headline: how many blockers + the top one, or all-clear.
  const dh = blockers.length
    ? { tone:'bad', icon:'ti-alert-triangle',
        text:`${blockers.length} issue${blockers.length>1?'s':''} to fix before requesting review — top: ${topFinding(blockers)}.` }
    : findings.length
      ? { tone:'warn', icon:'ti-info-circle',
          text:`No critical/high blockers — ${findings.length} lower-severity item${findings.length>1?'s':''} to review in the domain tabs.` }
      : { tone:'good', icon:'ti-circle-check',
          text:'No issues found — looks good to submit for review.' }
  return (
    <div>
      <Headline {...dh}/>
      <GateHero r={r}/>
      <div className="card">
        <div className="section-heading"><i className="ti ti-code"/>What you changed</div>
        <p style={{fontSize:13,marginBottom:8}}>{r.code_analysis?.summary||'No summary available.'}</p>
        <div style={{display:'flex',gap:6,flexWrap:'wrap'}}>
          <span className="badge badge-dim">{r.code_analysis?.change_type||'unknown'}</span>
          <span className={`badge ${(r.code_analysis?.complexity_delta||0)>0?'badge-amber':'badge-dim'}`}>complexity {(r.code_analysis?.complexity_delta||0)>0?'+':''}{r.code_analysis?.complexity_delta||0}</span>
          <span className={`badge ${covDelta<0?'badge-amber':'badge-dim'}`}>coverage {covDelta>0?'+':''}{covDelta.toFixed(1)}%</span>
        </div>
      </div>
      <div className="card">
        <div className="section-heading" style={{color:blockers.length?'#b91c1c':'#166534'}}>
          <i className={`ti ${blockers.length?'ti-alert-triangle':'ti-circle-check'}`}/>
          {blockers.length?`Fix before requesting review (${blockers.length})`:'No blockers — looks good to submit'}
        </div>
        {blockers.length ? (
          <div style={{display:'flex',flexDirection:'column',gap:8}}>
            {blockers.slice(0,12).map((f,i)=>(
              <div key={i} style={{display:'flex',alignItems:'flex-start',gap:10,padding:'9px 12px',border:'1px solid #f0f2f5',borderRadius:8}}>
                <SevChip sev={f.severity}/>
                <div style={{flex:1,minWidth:0}}>
                  <div style={{fontSize:13,color:'#1a2332'}}>{f.message}</div>
                  <div style={{fontSize:11,color:'#9fadbf'}}>{f.category}{f.file?` · ${f.file}${f.line?':'+f.line:''}`:''}</div>
                </div>
              </div>
            ))}
          </div>
        ) : <div style={{fontSize:13,color:'#7a8494'}}>No critical or high-severity findings. Address any medium/low items in the domain tabs if relevant.</div>}
      </div>
      {fixes.length>0&&<div className="card"><div className="section-heading"><i className="ti ti-tool"/>Suggested fixes</div><ol style={{margin:'0 0 0 18px',fontSize:13,lineHeight:1.9,color:'#3d4652'}}>{fixes.slice(0,6).map((f,i)=><li key={i}>{f}</li>)}</ol></div>}
      {scenarios.length>0&&<div className="card"><div className="section-heading"><i className="ti ti-test-pipe"/>Tests to add / run ({scenarios.length})</div><div style={{display:'flex',flexDirection:'column',gap:6}}>{scenarios.slice(0,6).map((s,i)=><div key={i} style={{fontSize:13,color:'#3d4652'}}>☐ {s.title||s.description||''} {s.priority&&<span style={{fontSize:10,color:'#9fadbf'}}>[{s.priority}]</span>}</div>)}</div></div>}
      <SimilarPRs r={r}/>
    </div>
  )
}

function ReviewerView({r, findings}) {
  const crit=findings.filter(f=>f.severity==='critical').length
  const high=findings.filter(f=>f.severity==='high').length
  const domainStatus=[
    ['Security',(r.security?.findings||[]).some(f=>['critical','high'].includes((f.severity||'').toLowerCase()))||r.security?.secrets_detected?'fail':'pass'],
    ['Privacy',(r.data_privacy?.unencrypted_pii_count||0)>0?'fail':'pass'],
    ['Performance',r.performance_impact?.has_db_risk||r.performance_impact?.has_complexity_regression?'warn':'pass'],
    ['API',(r.interface?.breaking_changes||[]).length?'fail':'pass'],
    ['Schema',r.schema_change?.has_destructive||r.schema_change?.has_irreversible?'fail':'pass'],
    ['Tests',parseFloat(r.test_coverage?.coverage_delta||0)<-5?'warn':'pass'],
    ['Licence',r.license_compliance?.has_copyleft?'fail':'pass'],
  ]
  const stColor={pass:['#f0fdf4','#166534','✅'],warn:['#fffbeb','#92400e','⚠️'],fail:['#fff1f2','#991b1b','❌']}
  const affected=r.dependency?.affected_services||[]
  const blast=r.dependency?.blast_radius_score||0
  const refs=r.reference_impact?.total_references||0

  // Headline: gate decision + why + what to scrutinise (capabilities / breaking changes).
  const gate=(r.gate_decision||'HOLD').toUpperCase()
  const breaking=(r.interface?.breaking_changes||[]).length
  const critCaps=(r.capabilities_affected||[]).filter(c=>['critical','high'].includes(c.criticality)).map(c=>c.name)
  const failDomains=domainStatus.filter(([,st])=>st==='fail').map(([d])=>d)
  const reasons=[]
  if(crit+high) reasons.push(`${crit+high} critical/high finding${crit+high>1?'s':''}`)
  if(breaking) reasons.push(`${breaking} breaking API change${breaking>1?'s':''}`)
  if(failDomains.length) reasons.push(failDomains.join(', ').toLowerCase()+' risk')
  const why = reasons.length?` — ${reasons.slice(0,3).join('; ')}`:''
  const capNote = critCaps.length?` Affects ${critCaps.slice(0,2).join(', ')}.`:''
  const rh = {
    APPROVE:{tone:'good',icon:'ti-circle-check',text:`Gate: APPROVE — no blocking issues found.${capNote}`},
    HOLD:{tone:'warn',icon:'ti-alert-triangle',text:`Gate: HOLD${why}. Resolve before merge.${capNote}`},
    BLOCK:{tone:'bad',icon:'ti-ban',text:`Gate: BLOCK${why}. Must be fixed before merge.${capNote}`},
  }[gate]||{tone:'warn',icon:'ti-alert-triangle',text:`Gate: ${gate}${why}.${capNote}`}
  return (
    <div>
      <Headline {...rh}/>
      <GateHero r={r}/>
      <div className="card">
        <div className="section-heading"><i className="ti ti-layout-grid"/>Domain status at a glance</div>
        <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
          {domainStatus.map(([d,st])=>{
            const c=stColor[st]
            return <div key={d} style={{background:c[0],color:c[1],border:`1px solid ${c[1]}33`,borderRadius:8,padding:'6px 12px',fontSize:12,fontWeight:600}}>{c[2]} {d}</div>
          })}
        </div>
      </div>
      {(r.capabilities_affected||[]).length>0 && (
        <div className="card">
          <div className="section-heading"><i className="ti ti-affiliate"/>Business capabilities affected</div>
          <div style={{display:'flex',flexDirection:'column',gap:8}}>
            {(r.capabilities_affected||[]).map((c,i)=>{
              const cc={critical:['#fff1f2','#991b1b'],high:['#fffbeb','#92400e'],medium:['#eff6ff','#1e40af'],low:['#f0fdf4','#166534']}[c.criticality]||['#f7f8fa','#7a8494']
              return (
                <div key={i} style={{display:'flex',alignItems:'flex-start',gap:10,padding:'10px 12px',border:'1px solid #f0f2f5',borderRadius:8}}>
                  <span style={{background:cc[0],color:cc[1],border:`1px solid ${cc[1]}33`,borderRadius:5,padding:'2px 8px',fontSize:10,fontWeight:700,whiteSpace:'nowrap'}}>{c.criticality}</span>
                  <div style={{flex:1,minWidth:0}}>
                    <div style={{fontSize:13,fontWeight:600,color:'#1a2332'}}>{c.name}</div>
                    <div style={{fontSize:11,color:'#7a8494'}}>{c.team}{(c.owners||[]).length?` · ${c.owners.join(', ')}`:''} · {c.file_count} file{c.file_count!==1?'s':''}</div>
                  </div>
                </div>
              )
            })}
          </div>
          <div style={{fontSize:11,color:'#9fadbf',marginTop:8}}><i className="ti ti-info-circle"/> Mapped from changed file paths. Loop in the owning team for capabilities marked critical/high.</div>
        </div>
      )}
      <div className="card">
        <div className="section-heading" style={{color:crit||high?'#b91c1c':'#166534'}}><i className="ti ti-eye-exclamation"/>What to scrutinise {crit+high?`(${crit} critical, ${high} high)`:''}</div>
        {crit+high>0 ? (
          <div style={{display:'flex',flexDirection:'column',gap:8}}>
            {findings.filter(f=>['critical','high'].includes(f.severity)).slice(0,10).map((f,i)=>(
              <div key={i} style={{display:'flex',alignItems:'flex-start',gap:10,padding:'9px 12px',border:'1px solid #f0f2f5',borderRadius:8}}>
                <SevChip sev={f.severity}/>
                <div style={{flex:1,minWidth:0}}><div style={{fontSize:13,color:'#1a2332'}}>{f.message}</div><div style={{fontSize:11,color:'#9fadbf'}}>{f.category}{f.file?` · ${f.file}${f.line?':'+f.line:''}`:''}</div></div>
              </div>
            ))}
          </div>
        ) : <div style={{fontSize:13,color:'#7a8494'}}>No critical or high-severity findings.</div>}
      </div>
      <div className="card">
        <div className="section-heading"><i className="ti ti-topology-star-3"/>Blast radius</div>
        <div style={{display:'flex',gap:20,flexWrap:'wrap',fontSize:13}}>
          <div><div style={{fontSize:22,fontWeight:800,color:blast>70?'#b91c1c':blast>40?'#92400e':'#166534',fontFamily:'JetBrains Mono,monospace'}}>{blast}<span style={{fontSize:12,color:'#9fadbf'}}>/100</span></div><div style={{fontSize:11,color:'#9fadbf'}}>blast radius</div></div>
          <div><div style={{fontSize:22,fontWeight:800,fontFamily:'JetBrains Mono,monospace'}}>{refs}</div><div style={{fontSize:11,color:'#9fadbf'}}>call-site references</div></div>
          <div><div style={{fontSize:22,fontWeight:800,fontFamily:'JetBrains Mono,monospace'}}>{affected.length}</div><div style={{fontSize:11,color:'#9fadbf'}}>downstream services</div></div>
        </div>
        {affected.length>0&&<div style={{marginTop:10,display:'flex',flexWrap:'wrap',gap:6}}>{affected.slice(0,8).map(s=><span key={s} className="badge badge-amber">{s}</span>)}</div>}
      </div>
    </div>
  )
}

function SecurityTab({r, snipCache, search=''}) {
  const q = search.trim().toLowerCase()
  const findings = (r.security?.findings||[]).filter(f =>
    !q || f.description?.toLowerCase().includes(q) || f.cwe?.toLowerCase().includes(q) || f.file?.toLowerCase().includes(q)
  )
  return (
    <div className="card">
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:12}}>
        <div className="section-heading" style={{marginBottom:0}}><i className="ti ti-shield-lock"/>Security findings</div>
        <div style={{display:'flex',alignItems:'center',gap:8}}>
          {q && <span style={{fontSize:11,color:'#7a8494'}}>{findings.length} match{findings.length!==1?'es':''}</span>}
          <span className={`badge badge-${r.security?.overall_severity==='critical'||r.security?.overall_severity==='high'?'red':r.security?.overall_severity==='medium'?'amber':'dim'}`}>{r.security?.overall_severity} overall</span>
        </div>
      </div>
      {r.security?.secrets_detected&&<div className="err-msg" style={{marginBottom:12}}><i className="ti ti-key"/><strong>Secrets detected</strong> — immediate rotation required</div>}
      {!findings.length ? <div className="empty-state"><i className="ti ti-shield-check"/>{q ? 'No matching findings' : 'No security findings'}</div>
        : findings.map((f,i)=>(
          <div key={i} className="finding" style={f.unverified?{opacity:.72}:undefined}>
            <span className={`sev sev-${f.severity}`}>{f.severity}</span>
            <div className="finding-body">
              <div className="finding-desc">
                <code>{f.cwe||''}</code> {f.description}
                {f.unverified && <span title="The cited file isn't in this diff — shown for awareness but excluded from the gate decision." style={{marginLeft:6,fontSize:10,fontWeight:700,padding:'1px 7px',borderRadius:10,background:'#fff7ed',color:'#9a3412',border:'1px solid #fed7aa',whiteSpace:'nowrap'}}><i className="ti ti-map-pin-off" style={{fontSize:11,marginRight:3}}/>location unverified</span>}
              </div>
              <div className="finding-file">{f.file||''}{f.line_range?` · line ${f.line_range}`:''}</div>
              {getCodeSnippetJSX(f.file,f.line_range,snipCache)}
              <div style={{marginTop:6}}><FindingFeedback r={r} agent="security" category={f.cwe||''} file={f.file||''}/></div>
            </div>
          </div>
        ))}
    </div>
  )
}

function AdvancedTab({r, snipCache}) {
  const se=r.secrets_entropy, ast=r.ast_analysis, ta=r.taint_analysis, iac=r.iac_analysis, tr=r.temporal_risk
  const emptyRow=msg=><div style={{fontSize:12,color:'#9fadbf',padding:'8px 0'}}><i className="ti ti-circle-check" style={{marginRight:5}}/>{ msg}</div>
  const agentSection=(icon,color,title,body)=>(
    <div key={title} style={{marginBottom:20,paddingBottom:20,borderBottom:'1px solid #e8eaed'}}>
      <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:12}}><i className={`ti ${icon}`} style={{fontSize:16,color}}/><span style={{fontSize:13,fontWeight:600,color:'#0d1117'}}>{title}</span></div>
      {body}
    </div>
  )
  return (
    <div className="card">
      {agentSection('ti-key','#ec4899','Entropy / Secrets', !se?emptyRow('Agent did not run'):(se.findings||[]).length===0?emptyRow('No secrets found'):
        <div>
          <div style={{fontSize:11.5,color:'#7a8494',background:'#fdf2f8',border:'1px solid #fbcfe8',borderRadius:7,padding:'8px 11px',marginBottom:10,lineHeight:1.5}}>
            <i className="ti ti-info-circle" style={{color:'#ec4899',marginRight:4}}/>
            <strong>What this checks:</strong> hardcoded secrets accidentally committed — API keys, tokens, passwords, private keys.
            "Entropy" measures randomness; real secrets look random (≈4.5+), normal code ≈4.0.
            <strong> How to act:</strong> open the line, confirm whether it's a real credential. If yes → remove it, move it to a secret store, and <strong>rotate the key</strong>. If it's just config/code, mark it <code>⚐ false positive</code> so it stops flagging.
          </div>
          {(se.findings||[]).map((f,i)=><div key={i} className="finding"><span className={`sev sev-${f.severity}`}>{f.severity}</span><div className="finding-body"><div className="finding-desc"><code>{f.variable||f.kind||''}</code> — {f.kind||''} (entropy: {(f.entropy||0).toFixed(2)})</div><div className="finding-file" title="Value is redacted — see the highlighted line below for full context">value: <code>{f.value||''}</code> {f.file?`· ${f.file.split(/[\\/]/).pop()}`:''} · line {f.line||'?'}</div>{getCodeSnippetJSX(f.file,f.line,snipCache,2)}<div style={{marginTop:6}}><FindingFeedback r={r} agent="secrets_entropy" category={f.kind||''} file={f.file||''}/></div></div></div>)}
        </div>
      )}
      {agentSection('ti-binary-tree','#8b5cf6','AST Analysis', !ast?emptyRow('Agent did not run'):(ast.findings||[]).length===0?emptyRow(`No AST issues — max complexity: ${ast.max_complexity||0}`):
        <div>{(ast.findings||[]).map((f,i)=><div key={i} className="finding"><span className={`sev sev-${f.severity}`}>{f.severity}</span><div className="finding-body"><div className="finding-desc"><code>{f.kind||''}</code> in <code>{f.function||''}</code><UnvBadge f={f}/></div><div className="finding-file">{f.description||''}{f.suggestion?` — ${f.suggestion}`:''}{f.line?` · line ${f.line}`:''}</div>{getCodeSnippetJSX(f.file,f.line,snipCache,3)}</div></div>)}</div>
      )}
      {agentSection('ti-arrows-diff','#f97316','Taint Analysis', !ta?emptyRow('Agent did not run'):(ta.taint_paths||[]).length===0?emptyRow(`No taint paths — ${ta.sources_found||0} sources, ${ta.sinks_found||0} sinks scanned`):
        <div>{(ta.taint_paths||[]).map((p,i)=><div key={i} className="finding"><span className={`sev sev-${p.severity}`}>{p.severity}</span><div className="finding-body"><div className="finding-desc">{p.cwe&&<code>{p.cwe} </code>}{p.description||`${p.source_var||'input'} → ${p.sink_kind||'sink'}`}</div><div className="finding-file">source: <code>{p.source_var||'?'} ({p.source_kind||'?'})</code> → sink: <code>{p.sink_var||'?'} ({p.sink_kind||'?'})</code></div></div></div>)}</div>
      )}
      {agentSection('ti-server','#14b8a6','IaC Security', !iac?emptyRow('Agent did not run'):(iac.findings||[]).length===0?emptyRow('No IaC issues found'):
        <div>{(iac.findings||[]).map((f,i)=><div key={i} className="finding"><span className={`sev sev-${f.severity}`}>{f.severity}</span><div className="finding-body"><div className="finding-desc"><code>{f.kind||''}</code> — {f.description||''}<UnvBadge f={f}/></div><div className="finding-file">resource: <code>{f.resource||''}</code>{f.cis_ref?` · CIS ${f.cis_ref}`:''}{ f.line?` · line ${f.line}`:''}</div>{getCodeSnippetJSX(f.file,f.line,snipCache,3)}</div></div>)}</div>
      )}
      {agentSection('ti-clock-record','#a855f7','Temporal Risk', !tr?emptyRow('Agent did not run'):
        <div>
          <div style={{display:'flex',gap:10,marginBottom:10}}>
            <span className="badge" style={{color:tr.risk_trend==='degrading'?'#b81c1c':tr.risk_trend==='improving'?'#0c7c4b':'#7a8494',borderColor:tr.risk_trend==='degrading'?'#b81c1c':tr.risk_trend==='improving'?'#0c7c4b':'#e8eaed'}}>trend: {tr.risk_trend||'stable'}</span>
            {tr.escalating_pattern&&<span className="badge badge-amber">Escalating pattern</span>}
            {tr.security_erosion&&<span className="badge badge-red">Security erosion</span>}
          </div>
          {(tr.hot_files||[]).length===0?emptyRow('No hot files'):(tr.hot_files||[]).map((f,i)=>(
            <div key={i} className="finding"><span className="sev sev-medium">hot</span><div className="finding-body"><div className="finding-desc"><code>{f.file_path||''}</code></div><div className="finding-file">changed {f.change_count||0}× · avg risk {(f.avg_risk_score||0).toFixed(0)}</div></div></div>
          ))}
        </div>
      )}
    </div>
  )
}

// Bands give the number meaning at a glance. Kept in sync with the gate weighting.
function blastBand(score) {
  if (score >= 76) return { label:'CRITICAL', color:'#991b1b', note:'change can ripple across many components — review downstream carefully' }
  if (score >= 51) return { label:'HIGH',     color:'#b91c1c', note:'meaningful downstream reach — verify dependents and contracts' }
  if (score >= 21) return { label:'MODERATE', color:'#8a5200', note:'limited reach — a handful of callers/impacts' }
  return { label:'LOW', color:'#0c7c4b', note:'little to no downstream reach detected' }
}

function DependencyTab({r}) {
  const blast=r.dependency?.blast_radius_score||0
  const band = blastBand(blast)
  // Reconstruct the contributing signals (mirrors governance/blast_radius.py) so
  // the score is explainable rather than a mystery number.
  const refs     = r.reference_impact?.total_references || 0
  const hiFiles  = (r.reference_impact?.high_impact_files||[]).length
  const shared   = (r.reference_impact?.shared_lib_breaks||[]).length
  const breaking = (r.interface?.breaking_changes||[]).length
  const services = (r.dependency?.affected_services||[]).length
  const factors = [
    { label:'References to changed code (in-repo + dependents)', n:refs,     w:2,  hint:'call-sites that use a symbol you changed' },
    { label:'High-impact files (≥3 references)',                 n:hiFiles,  w:8,  hint:'files that lean heavily on the changed code' },
    { label:'Breaking API / contract changes',                  n:breaking, w:18, hint:'removed/renamed/retyped public surface' },
    { label:'Shared / common library changes',                  n:shared,   w:15, hint:'edits under shared/common/core paths' },
    { label:'Declared dependent repos',                         n:services, w:12, hint:'repos you flagged as consumers' },
  ].filter(f=>f.n>0)
  const signalSum = Math.min(100, factors.reduce((a,f)=>a+f.n*f.w,0))
  const graphExtra = blast - signalSum   // >0 ⇒ a service-graph traversal added reach
  return (
    <div>
      <div className="card">
        <div className="section-heading"><i className="ti ti-topology-star-3"/>Blast radius analysis</div>
        <div style={{marginBottom:12}}>
          <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:6,flexWrap:'wrap'}}>
            <span style={{fontSize:13,color:'#3a4452'}}>Blast radius score: <strong>{blast}/100</strong></span>
            <span style={{fontSize:11,fontWeight:700,padding:'2px 9px',borderRadius:10,background:`${band.color}1a`,color:band.color,border:`1px solid ${band.color}55`}}>{band.label}</span>
          </div>
          <div className="score-bar"><div className="score-fill" style={{width:`${blast}%`,background:band.color}}/></div>
          <div style={{fontSize:11.5,color:'#7a8494',marginTop:6,lineHeight:1.5}}>
            <strong>What this means:</strong> how far a problem in this change could spread across the codebase and services — {band.note}.
          </div>
        </div>

        {/* Score breakdown — makes the number explainable to any reviewer */}
        <div style={{background:'#f7f8fa',border:'1px solid #e8eaed',borderRadius:8,padding:'10px 12px',marginBottom:12}}>
          <div style={{fontSize:11,fontWeight:700,color:'#5b6675',textTransform:'uppercase',letterSpacing:.4,marginBottom:7}}>How this score is calculated</div>
          {factors.length===0 && graphExtra<=0
            ? <div style={{fontSize:12,color:'#7a8494'}}>No downstream-reach signals were found, so the score is 0. Select dependent repos on the Repos screen, or configure <code>SERVICE_MAP_PATH</code> / <code>REPOS_ROOT</code>, to include cross-service reach.</div>
            : <>
                <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
                  <thead><tr style={{color:'#9fadbf',fontSize:10,textTransform:'uppercase'}}>
                    <th style={{textAlign:'left',padding:'3px 4px'}}>Signal</th>
                    <th style={{textAlign:'right',padding:'3px 4px'}}>Count</th>
                    <th style={{textAlign:'right',padding:'3px 4px'}}>Weight</th>
                    <th style={{textAlign:'right',padding:'3px 4px'}}>Points</th>
                  </tr></thead>
                  <tbody>
                    {factors.map((f,i)=>(
                      <tr key={i} style={{borderTop:'1px solid #edeef1'}} title={f.hint}>
                        <td style={{padding:'4px 4px',color:'#3a4452'}}>{f.label}</td>
                        <td style={{padding:'4px 4px',textAlign:'right',fontFamily:'var(--mono)'}}>{f.n}</td>
                        <td style={{padding:'4px 4px',textAlign:'right',color:'#9fadbf'}}>×{f.w}</td>
                        <td style={{padding:'4px 4px',textAlign:'right',fontFamily:'var(--mono)',fontWeight:600}}>{f.n*f.w}</td>
                      </tr>
                    ))}
                    {graphExtra>0 && (
                      <tr style={{borderTop:'1px solid #edeef1'}} title="Transitive reach from the configured service dependency graph (NetworkX/Neo4j).">
                        <td style={{padding:'4px 4px',color:'#3a4452'}}>Service-graph transitive reach</td>
                        <td style={{padding:'4px 4px',textAlign:'right',color:'#9fadbf'}}>—</td>
                        <td style={{padding:'4px 4px',textAlign:'right',color:'#9fadbf'}}>—</td>
                        <td style={{padding:'4px 4px',textAlign:'right',fontFamily:'var(--mono)',fontWeight:600}}>{graphExtra}</td>
                      </tr>
                    )}
                  </tbody>
                  <tfoot><tr style={{borderTop:'2px solid #e0e2e6'}}>
                    <td colSpan={3} style={{padding:'5px 4px',fontWeight:700,color:'#3a4452'}}>Total (capped at 100)</td>
                    <td style={{padding:'5px 4px',textAlign:'right',fontFamily:'var(--mono)',fontWeight:700}}>{blast}</td>
                  </tr></tfoot>
                </table>
              </>}
          <div style={{fontSize:11,color:'#9fadbf',marginTop:8,lineHeight:1.5}}>
            Bands: <span style={{color:'#0c7c4b'}}>0–20 Low</span> · <span style={{color:'#8a5200'}}>21–50 Moderate</span> · <span style={{color:'#b91c1c'}}>51–75 High</span> · <span style={{color:'#991b1b'}}>76–100 Critical</span>.
            A score with no dependent repos reflects reach <em>within this repository</em>; add dependents or a service graph to include cross-service reach.
          </div>
        </div>
        {(r.dependency?.affected_services||[]).length>0&&<><div className="section-heading"><i className="ti ti-server"/>Affected downstream services ({(r.dependency?.affected_services||[]).length})</div><div style={{fontSize:11,color:'#9fadbf',marginBottom:6}}>Declared dependent repos + any traced from the service graph.</div><div style={{display:'flex',flexWrap:'wrap',gap:6,marginBottom:12}}>{(r.dependency?.affected_services||[]).map(s=><span key={s} className="badge badge-amber">{s}</span>)}</div></>}
        {(r.dependency?.changed_packages||[]).length>0&&<><div className="section-heading"><i className="ti ti-package"/>Changed packages</div><div style={{display:'flex',flexWrap:'wrap',gap:6,marginBottom:12}}>{(r.dependency?.changed_packages||[]).map(p=><code key={p}>{p}</code>)}</div></>}
        {(r.dependency?.cve_hits||[]).length>0&&<><div className="section-heading" style={{color:'#b91c1c'}}><i className="ti ti-bug"/>Known CVEs</div><div style={{display:'flex',flexWrap:'wrap',gap:6,marginBottom:12}}>{(r.dependency?.cve_hits||[]).map(c=><span key={c} className="badge badge-red" style={{fontFamily:'var(--mono)'}}>{c}</span>)}</div></>}
        {(r.dependency?.notes||[]).length>0&&(r.dependency?.cve_hits||[]).length===0&&(r.dependency?.affected_services||[]).length===0&&(
          <div style={{padding:'9px 12px',background:'#f3f6fb',border:'1px solid #d8e0ec',borderRadius:8,fontSize:12,color:'#3a4452',lineHeight:1.55,display:'flex',gap:8}}>
            <i className="ti ti-info-circle" style={{flexShrink:0,marginTop:2,color:'#1a6cf6'}}/>
            <div>{(r.dependency?.notes||[]).map((n,i)=><div key={i}>{n}</div>)}</div>
          </div>
        )}
      </div>
      <MavenSca report={r}/>
      <DepAutoUpdate r={r}/>
    </div>
  )
}

function MavenSca({ report }) {
  const { state } = useApp()
  const [data,setData]=useState(null); const [err,setErr]=useState(''); const [loading,setLoading]=useState(false)
  async function scan(text){
    if(!state.backendUrl){setErr('Configure a Backend URL in Settings to run the scan.');return}
    setLoading(true);setErr('');setData(null)
    try{
      const h={'Content-Type':'application/xml'}; if(state.backendKey)h['X-API-Key']=state.backendKey
      const r=await fetch(`${state.backendUrl}/api/v1/sca/pom`,{method:'POST',headers:h,body:text})
      const d=await r.json().catch(()=>({}))
      if(!r.ok) throw new Error(d.detail||('HTTP '+r.status))
      setData(d)
    }catch(e){setErr(e.message)}finally{setLoading(false)}
  }
  function onFile(e){const f=e.target.files[0]; if(!f)return; const rd=new FileReader(); rd.onload=()=>scan(String(rd.result||'')); rd.readAsText(f); e.target.value=''}
  const sevColor=s=>({CRITICAL:'#b91c1c',HIGH:'#9a3412',MEDIUM:'#8a5200',LOW:'#6b7280'})[s]||'#6b7280'
  return (
    <div className="card">
      <div className="section-heading"><i className="ti ti-shield-bolt"/>Maven dependency scan (SCA)</div>
      <div style={{fontSize:12.5,color:'#7a8494',lineHeight:1.6,marginBottom:10}}>
        Upload this repo’s <code>pom.xml</code> to check declared dependencies against OSV for known CVEs.
        Works on any branch — no lockfile or CI needed. <strong>Direct dependencies only</strong> (Maven has no lockfile for transitive resolution).
      </div>
      <label className="btn btn-sm" style={{cursor:'pointer'}}><i className="ti ti-upload"/> Upload pom.xml
        <input type="file" accept=".xml,application/xml,text/xml" onChange={onFile} style={{display:'none'}}/></label>
      {loading&&<div style={{marginTop:10,fontSize:13,color:'#7a8494',display:'flex',alignItems:'center',gap:6}}><span className="spinner" style={{width:14,height:14}}/>Scanning against OSV…</div>}
      {err&&<div className="info-msg" style={{marginTop:10}}><i className="ti ti-alert-circle"/>{err}</div>}
      {data&&(
        <div style={{marginTop:12}}>
          <div style={{fontSize:12.5,fontWeight:700,marginBottom:8,color:data.vulnerabilities.length?'#b91c1c':'#166534'}}>{data.summary}</div>
          {data.vulnerabilities.map((v,i)=>(
            <div key={i} className="finding">
              <span style={{fontSize:11,fontWeight:700,padding:'2px 8px',borderRadius:10,background:`${sevColor(v.severity)}1a`,color:sevColor(v.severity),border:`1px solid ${sevColor(v.severity)}55`,whiteSpace:'nowrap',flexShrink:0}}>{v.severity}</span>
              <div className="finding-body">
                <div className="finding-desc"><code>{v.cve}</code> {v.summary}</div>
                <div className="finding-file"><code>{v.package}@{v.version}</code> · scope: {v.scope} · {v.depth}</div>
                {report && <div style={{marginTop:6}}><FindingFeedback r={report} agent="dependency" category={v.cve||''} file={v.package||''}/></div>}
              </div>
            </div>
          ))}
          {(data.unresolved||[]).length>0&&<div style={{marginTop:8,fontSize:11,color:'#9fadbf'}}><i className="ti ti-info-circle" style={{marginRight:4}}/>{data.unresolved.length} version(s) managed by a parent POM/BOM — not resolvable from this pom alone: {data.unresolved.slice(0,5).join(', ')}{data.unresolved.length>5?'…':''}</div>}
        </div>
      )}
    </div>
  )
}

function InterfaceTab({r}) {
  const impacts = r.consumer_impacts || []
  const breaking = r.interface?.breaking_changes || []
  const additive = r.interface?.additive_changes || []
  // Downstream repos the reviewer flagged as dependents (DependencyResult.affected_services,
  // populated from the connected-repos selection). CIAA can't see their source, so when a
  // breaking change exists these must be verified manually.
  const declaredDeps = r.dependency?.affected_services || []
  return (
    <div>
      <div className="card">
        <div className="section-heading"><i className="ti ti-api"/>Contract breaking changes</div>
        {!(r.interface?.breaking_changes||[]).length?<div className="empty-state"><i className="ti ti-circle-check"/>No breaking interface changes</div>
          :(r.interface?.breaking_changes||[]).map((b,i)=>(
            <div key={i} className="finding" style={{flexDirection:'column',alignItems:'stretch',gap:7}}>
              <div style={{display:'flex',gap:10,alignItems:'flex-start'}}>
                <span className={`sev sev-${b.severity}`}>{b.severity}</span>
                <div className="finding-body"><div className="finding-desc"><span className="badge badge-dim" style={{marginRight:6}}>{b.type}</span>{(b.break_type||'').replace(/_/g,' ')} change</div><div className="finding-file">{b.path||''}</div></div>
              </div>
              <div><FindingFeedback r={r} agent="interface" category={b.break_type||b.type||''} file={b.path||''}/></div>
            </div>
          ))}
      </div>
      {additive.length>0 && (
        <div className="card">
          <div className="section-heading" style={{color:'#1e40af'}}><i className="ti ti-plus"/>Data-model / contract additions ({additive.length})</div>
          <div style={{fontSize:12,color:'#7a8494',marginBottom:10,lineHeight:1.55}}>
            Fields added to serializable data classes. <strong>Not breaking</strong>, but they appear in JSON/API output — confirm consumers and deserializers tolerate unknown fields, and update API docs / schemas.
          </div>
          {additive.map((a,i)=>(
            <div key={i} className="finding" style={{alignItems:'flex-start',gap:8}}>
              <span style={{fontSize:10,fontWeight:700,padding:'2px 8px',borderRadius:8,background:'#eff5ff',color:'#1e40af',border:'1px solid #c5d8fb',whiteSpace:'nowrap',flexShrink:0}}>ADDITIVE</span>
              <div className="finding-body"><div className="finding-desc">{a}</div></div>
            </div>
          ))}
        </div>
      )}
      {impacts.length>0 && (
        <div className="card">
          <div className="section-heading" style={{color:'#b91c1c'}}><i className="ti ti-affiliate"/>Downstream consumers that will break ({impacts.length})</div>
          <div style={{fontSize:12,color:'#7a8494',marginBottom:10}}>Exact call-sites affected by these breaking changes, with the likely runtime failure. <strong>Cross-repo</strong> rows were traced in dependent repos via the provider’s code search.</div>
          <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
            <thead><tr style={{color:'#9fadbf',fontSize:10,textTransform:'uppercase'}}>
              <th style={{textAlign:'left',padding:'5px 6px'}}>Change</th>
              <th style={{textAlign:'left',padding:'5px 6px'}}>Repo</th>
              <th style={{textAlign:'left',padding:'5px 6px'}}>Caller (file:line)</th>
              <th style={{textAlign:'left',padding:'5px 6px'}}>Failure mode</th>
            </tr></thead>
            <tbody>{impacts.map((ci,i)=>(
              <tr key={i} style={{borderTop:'1px solid #f5f6f8'}}>
                <td style={{padding:'5px 6px'}}><code>{ci.change}</code> <span style={{fontSize:10,color:'#9fadbf'}}>({(ci.change_type||'').replace(/_/g,' ')})</span></td>
                <td style={{padding:'5px 6px'}}>{ci.repo
                  ? <span style={{fontSize:10,fontWeight:600,padding:'2px 7px',borderRadius:8,background:'#fff1f1',color:'#b91c1c',border:'1px solid #f8c0c0'}}>{ci.repo}</span>
                  : <span style={{fontSize:10,color:'#9fadbf'}}>this repo</span>}</td>
                <td style={{padding:'5px 6px',fontFamily:'var(--mono)',fontSize:11}}>{ci.file_path?`${ci.file_path}${ci.line?':'+ci.line:''}`:<span style={{color:'#9fadbf'}}>no caller found — verify external consumers</span>}</td>
                <td style={{padding:'5px 6px',color:'#b91c1c'}}>{ci.failure_mode}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      {declaredDeps.length>0 && (
        <div className="card">
          <div className="section-heading" style={{color:breaking.length?'#b91c1c':'#5b6675'}}>
            <i className="ti ti-affiliate"/>Declared downstream repos ({declaredDeps.length})
          </div>
          <div style={{fontSize:12,color:'#7a8494',marginBottom:10,lineHeight:1.55}}>
            {breaking.length
              ? <>This PR has <strong>{breaking.length} breaking change(s)</strong> and you flagged these repos as dependents. CIAA can’t read their source code, so <strong>verify each one</strong> still compiles/calls correctly against the new contract.</>
              : <>You flagged these repos as dependents. No breaking interface changes were detected, so they’re likely unaffected — but confirm if this PR changes shared behaviour.</>}
          </div>
          <div style={{display:'flex',flexWrap:'wrap',gap:8}}>
            {declaredDeps.map(s=>(
              <span key={s} style={{display:'inline-flex',alignItems:'center',gap:6,fontSize:12,padding:'5px 11px',borderRadius:8,
                background:breaking.length?'#fff1f1':'#f3f6fb',color:breaking.length?'#b91c1c':'#3a4452',
                border:`1px solid ${breaking.length?'#f8c0c0':'#d8e0ec'}`}}>
                <i className="ti ti-git-branch" style={{fontSize:12}}/>{s}
              </span>
            ))}
          </div>
          <div style={{fontSize:11,color:'#9fadbf',marginTop:10,lineHeight:1.5}}>
            <i className="ti ti-info-circle" style={{marginRight:4}}/>
            To trace exact call-sites inside these repos automatically, run CIAA with <code>REPO_LOCAL_PATH</code> pointing at their clones (or a <code>SERVICE_MAP_PATH</code> graph). Otherwise this is a manual checklist.
          </div>
        </div>
      )}
    </div>
  )
}

// Plain-English explanation of what a schema change does + what to verify,
// so the Schema tab is actionable even when the agent gives no description.
const SCHEMA_EXPLAIN = {
  add_table:    ['Creates a new table.', 'Confirm the migration is additive only and the app handles the table not existing on older nodes during rollout.'],
  drop_table:   ['Deletes a table and all its rows — irreversible data loss.', 'Require a verified backup + DBA sign-off; confirm no service still reads this table.'],
  add_column:   ['Adds a column. Safe if nullable / has a default; a NOT NULL column with no default can lock a large table on write.', 'Check the column is nullable or back-filled before any NOT NULL constraint.'],
  drop_column:  ['Removes a column — data in it is lost and any code/SQL referencing it breaks.', 'Grep the codebase for the column; deploy code that stops reading it first (expand/contract).'],
  alter_column: ['Changes a column’s type/constraint. Type narrowing can truncate or reject existing rows.', 'Validate existing values fit the new type; do it in two steps for large tables.'],
  rename:       ['Renames a table/column — breaks every query still using the old name.', 'Use a view/alias or expand-contract so old and new names coexist during rollout.'],
  add_index:    ['Adds an index. On large tables a blocking CREATE INDEX can hold locks; prefer CONCURRENTLY / online DDL.', 'Confirm it’s built online and won’t block writes during deploy.'],
  drop_index:   ['Removes an index — queries that relied on it may do full scans and slow down.', 'Check no hot query plan depends on this index before dropping.'],
}
function SchemaTab({r}) {
  const sc = r.schema_change
  const changes = sc?.changes || []
  const sevC = s => ({critical:'#991b1b',high:'#b91c1c',medium:'#92400e',low:'#1e40af'})[(s||'').toLowerCase()]||'#7a8494'
  return (
    <div className="card">
      <div className="section-heading"><i className="ti ti-database"/>Database schema changes</div>
      {sc?.summary && <div style={{fontSize:13,color:'#4a5568',lineHeight:1.55,marginBottom:12}}>{sc.summary}</div>}
      {sc?.has_irreversible&&<div className="err-msg" style={{marginBottom:12}}><i className="ti ti-alert-triangle"/>Irreversible changes detected — verified backup + DBA sign-off required before merge.</div>}
      {sc?.has_destructive&&!sc?.has_irreversible&&<div style={{padding:'8px 12px',background:'#fff8ec',border:'1px solid #8a5200',borderRadius:'var(--r)',fontSize:12,color:'#8a5200',display:'flex',alignItems:'center',gap:7,marginBottom:12}}><i className="ti ti-alert-triangle"/>Destructive changes — verify the rollback plan works on a copy of prod.</div>}
      {!changes.length?<div className="empty-state"><i className="ti ti-database"/>No schema changes detected in this PR.</div>
        :changes.map((c,i)=>{
          const ct = (c.change_type||'').toLowerCase()
          let [what, verify] = SCHEMA_EXPLAIN[ct] || [c.description||'Schema change.', 'Review the migration and its rollback path.']
          // Index/PK/FK created together with a brand-new table can't lock it (empty table) —
          // replace the generic "prefer CONCURRENTLY" warning with an accurate note.
          if (c.on_new_table && (ct==='add_index'||ct==='add_column')) {
            what = 'Created together with the new table, so it applies to an empty table — no CREATE INDEX lock / online-DDL concern.'
            verify = 'No action needed for locking; just confirm the index/constraint definition is correct.'
          }
          const target = [c.table, c.column && `· column ${c.column}`].filter(Boolean).join(' ')
          return (
          <div key={i} className="finding" style={{flexDirection:'column',alignItems:'stretch',gap:8,borderLeft:`3px solid ${sevC(c.severity)}`}}>
            <div style={{display:'flex',alignItems:'center',gap:10,flexWrap:'wrap'}}>
              <span style={{fontSize:11,fontWeight:700,padding:'2px 9px',borderRadius:10,background:`${sevC(c.severity)}1a`,color:sevC(c.severity),border:`1px solid ${sevC(c.severity)}55`,textTransform:'uppercase'}}>{c.severity||'medium'}</span>
              <code style={{fontSize:13}}>{ct.replace(/_/g,' ')}</code>
              {c.table && <span style={{fontSize:13,color:'#4a5568'}}>on <code>{target}</code></span>}
              <span style={{marginLeft:'auto',fontSize:11,fontWeight:600,color:c.reversible?'#166534':'#b91c1c'}}>{c.reversible?'↩ Reversible':'⚠ Not reversible'}</span>
            </div>
            {c.description && <div style={{fontSize:12.5,color:'#374151'}}>{c.description}</div>}
            <div style={{fontSize:12,color:'#5b6675',lineHeight:1.55}}><strong>What it does:</strong> {what}</div>
            <div style={{fontSize:12,color:'#5b6675',lineHeight:1.55}}><strong>Before merge:</strong> {verify}</div>
            {c.file && <div className="finding-file" style={{fontFamily:'var(--mono)'}}>{c.file}</div>}
            {c.rollback_sql && <div><div style={{fontSize:10,color:'#9fadbf',textTransform:'uppercase',letterSpacing:.4,margin:'2px 0 3px'}}>Rollback SQL</div><pre style={{margin:0,padding:'8px 10px',background:'#0d1117',color:'#e6edf3',borderRadius:6,fontSize:11.5,overflowX:'auto',fontFamily:'var(--mono)'}}>{c.rollback_sql}</pre></div>}
            <div style={{marginTop:2}}><FindingFeedback r={r} agent="schema_change" category={ct} file={c.file||''}/></div>
          </div>
        )})}
    </div>
  )
}

function CodeFixCard({fx}) {
  const [copied, setCopied] = useState(false)
  const sevC = {critical:'#991b1b',high:'#b91c1c',medium:'#92400e',low:'#1e40af'}[fx.severity]||'#7a8494'
  const confC = fx.confidence==='high'?['#f0fdf4','#166534']:fx.confidence==='low'?['#fff1f2','#991b1b']:['#fffbeb','#92400e']
  function copy() {
    navigator.clipboard?.writeText(fx.diff || `${fx.before}\n→\n${fx.after}`).then(()=>{ setCopied(true); setTimeout(()=>setCopied(false),1500) })
  }
  return (
    <div style={{border:'1px solid #e8eaed',borderRadius:8,overflow:'hidden'}}>
      <div style={{display:'flex',alignItems:'center',gap:8,padding:'9px 12px',background:'#f7f8fa',borderBottom:'1px solid #e8eaed'}}>
        <span style={{background:`${sevC}18`,color:sevC,borderRadius:4,padding:'1px 7px',fontSize:10,fontWeight:700}}>{fx.severity}</span>
        <span style={{fontWeight:600,fontSize:13,color:'#1a2332',flex:1,minWidth:0}}>{fx.title}</span>
        <span style={{background:confC[0],color:confC[1],borderRadius:4,padding:'1px 7px',fontSize:10,fontWeight:700}}>{fx.confidence} confidence</span>
        <button onClick={copy} className="btn" style={{fontSize:11,padding:'3px 9px'}}><i className="ti ti-copy"/> {copied?'Copied':'Copy'}</button>
      </div>
      <div style={{padding:'10px 12px'}}>
        {fx.file && <div style={{fontSize:11,color:'#7a8494',fontFamily:'var(--mono)',marginBottom:6}}>{fx.file}</div>}
        <pre style={{margin:0,fontSize:12,fontFamily:'var(--mono)',lineHeight:1.6,overflowX:'auto'}}>
          <div style={{background:'#fff1f2',color:'#991b1b',padding:'2px 6px',borderRadius:'4px 4px 0 0'}}>- {fx.before}</div>
          <div style={{background:'#f0fdf4',color:'#166534',padding:'2px 6px',borderRadius:'0 0 4px 4px'}}>+ {fx.after}</div>
        </pre>
        {fx.explanation && <div style={{fontSize:12,color:'#5a6a7e',marginTop:8,lineHeight:1.5}}>{fx.explanation}</div>}
      </div>
    </div>
  )
}

function RemediationTab({r}) {
  return (
    <div>
      <div className="card">
        <div className="section-heading"><i className="ti ti-speakerphone"/>Executive summary</div>
        <p style={{fontSize:13,lineHeight:1.7,marginBottom:12}}>{r.remediation?.executive_summary||''}</p>
        <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
          <span className="badge badge-blue"><i className="ti ti-rocket" style={{fontSize:12}}/>{r.risk?.deployment_strategy||'standard'}</span>
          <span className="badge badge-dim"><i className="ti ti-history" style={{fontSize:12}}/>Rollback: {r.risk?.rollback_feasibility||'?'}</span>
        </div>
        <p style={{fontSize:12,color:'#7a8494',marginTop:10}}>{r.risk?.deployment_guidance||''}</p>
      </div>
      {(r.remediation?.code_fixes||[]).length>0 && (
        <div className="card">
          <div className="section-heading"><i className="ti ti-git-pull-request"/>Suggested code fixes ({r.remediation.code_fixes.length})</div>
          <div style={{fontSize:12,color:'#7a8494',marginBottom:12}}>Concrete before/after patches for high-confidence issues — review and apply.</div>
          <div style={{display:'flex',flexDirection:'column',gap:14}}>
            {r.remediation.code_fixes.map((fx,i)=>(<CodeFixCard key={i} fx={fx}/>))}
          </div>
        </div>
      )}
      <div className="card">
        <div className="section-heading"><i className="ti ti-tool"/>Fix suggestions</div>
        {(r.remediation?.fix_suggestions||[]).map((f,i)=><div key={i} style={{display:'flex',gap:8,padding:'7px 0',borderBottom:'1px solid #e8eaed',fontSize:13}}><span style={{color:'#7a8494',flexShrink:0,width:20}}>{i+1}.</span>{f}</div>)}
      </div>
      <div className="card">
        <div className="section-heading"><i className="ti ti-checklist"/>Validation checklist</div>
        {(r.remediation?.validation_checklist||[]).map((c,i)=><div key={i} className="checklist-item"><i className="ti ti-square"/>{ c}</div>)}
      </div>
    </div>
  )
}

const SCENARIO_HINT = {
  'happy path': m => `Call \`${m}()\` with typical valid inputs and assert the exact expected return value / output.`,
  'invalid input': m => `Call \`${m}()\` with malformed, out-of-range, or wrong-type arguments; assert it is rejected with a clear validation error (not a generic crash).`,
  'null / empty': m => `Pass \`null\`/empty/blank for each argument of \`${m}()\`; assert safe handling — no NullPointer/KeyError, sensible default or explicit error.`,
  'boundary / edge': m => `Exercise \`${m}()\` at min, max, zero, empty collection, single element, and off-by-one boundaries.`,
  'error / exception': m => `Force \`${m}()\` down its failure path; assert the exact exception type AND message/error code, and that state is left consistent.`,
  'state / side-effects': m => `After \`${m}()\`, verify the persisted/mutated state is correct, and that calling it twice is idempotent (no double-charge / duplicate write).`,
  'security (authz / injection)': m => `Assert \`${m}()\` denies unauthorized/forbidden callers (401/403) and is safe against injection (SQL/command/XSS) and malicious input.`,
  'concurrency / thread-safety': m => `Invoke \`${m}()\` from several threads concurrently (latch/executor); assert no race condition, lost update, or corrupted shared state.`,
  'data integrity / serialization': m => `Round-trip serialize → deserialize through \`${m}()\`; assert no data loss/precision change, and verify migration rollback works.`,
  'backward compatibility': m => `Call \`${m}()\` with the PREVIOUS request/contract shape; assert existing consumers (v1 clients) still get a valid response.`,
  'regression (the fix)': m => `Add a test that reproduces the ORIGINAL bug this change fixes; confirm it failed before and passes now (guards against re-introduction).`,
}

function UnitTestCoverage({ tc }) {
  const methods = tc?.method_coverage || []
  const hollow = tc?.hollow_tests || []
  if (!methods.length && !hollow.length) return null
  const totalMissing = methods.reduce((n,m)=>n+(m.missing?.length||0),0)
  return (
    <div className="card" style={{marginBottom:12}}>
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:8,marginBottom:8}}>
        <div className="card-title" style={{marginBottom:0}}><i className="ti ti-list-check"/>Unit test coverage — changed methods</div>
        <span style={{fontSize:12,fontWeight:700,color:totalMissing?'#b45309':'#166534'}}>
          {totalMissing? `${totalMissing} scenario(s) not covered` : 'all recommended scenarios covered'}
        </span>
      </div>
      <div style={{fontSize:11.5,color:'#7a8494',marginBottom:8}}>{tc.scenario_summary} · ✓ = evidenced in this PR’s tests, ✗ = not found in this PR.</div>
      <div style={{fontSize:11,color:'#9a6a00',background:'#fffbeb',border:'1px solid #fde68a',borderRadius:7,padding:'7px 10px',marginBottom:12,display:'flex',alignItems:'flex-start',gap:6}}>
        <i className="ti ti-info-circle" style={{marginTop:1}}/>
        <span>Only tests <strong>included in this PR</strong> are scanned. A method may already be covered by <strong>existing tests in the repo</strong> that aren’t part of this change — so treat ✗ as "verify", not "definitely untested". <strong>Newly-added methods</strong> with no test are the real gaps.</span>
      </div>
      {methods.map((m,i)=>(
        <div key={i} style={{padding:'10px 0',borderTop:i?'1px solid #f0f2f5':'none'}}>
          <div style={{display:'flex',alignItems:'center',gap:8,flexWrap:'wrap',marginBottom:6}}>
            <code style={{fontSize:12.5,fontWeight:700,color:'#0d1117'}}>{m.method}()</code>
            {m.is_new && <span className="badge badge-blue" style={{fontSize:10}}>new</span>}
            {m.has_test && m.test_source==='repo' && <span className="badge badge-green" style={{fontSize:10}} title="Covered by an existing test file in the repo (not part of this PR)"><i className="ti ti-circle-check" style={{fontSize:10,marginRight:2}}/>existing repo test</span>}
            {m.has_test && m.test_source==='pr' && <span className="badge badge-green" style={{fontSize:10}}>tested in PR</span>}
            {!m.has_test && (m.is_new
              ? <span className="badge badge-red" style={{fontSize:10}}><i className="ti ti-alert-triangle" style={{fontSize:10,marginRight:2}}/>new · no test</span>
              : <span className="badge badge-amber" style={{fontSize:10}} title="No test found in this PR or the paired repo test file">no test found</span>)}
            <span style={{fontSize:11,color:'#9fadbf',fontFamily:'JetBrains Mono,monospace'}}>{(m.file||'').split(/[\\/]/).pop()}</span>
          </div>
          <div style={{display:'flex',gap:6,flexWrap:'wrap'}}>
            {m.required.map(s=>{
              const ok = m.covered.includes(s)
              return <span key={s} style={{fontSize:11,fontWeight:600,padding:'2px 9px',borderRadius:12,
                background:ok?'#f0fdf4':'#fff7ed', color:ok?'#166534':'#9a3412', border:`1px solid ${ok?'#bbf7d0':'#fed7aa'}`}}>
                {ok?'✓':'✗'} {s}</span>
            })}
          </div>
          {m.missing.length>0 && (
            <details style={{marginTop:7}}>
              <summary style={{fontSize:11.5,fontWeight:600,color:'#9a3412',cursor:'pointer',userSelect:'none'}}>
                ▸ How to cover the {m.missing.length} missing test{m.missing.length>1?'s':''}
              </summary>
              <ul style={{margin:'6px 0 2px',paddingLeft:18,fontSize:12,color:'#4b5563',lineHeight:1.6}}>
                {m.missing.map(s=>(
                  <li key={s} style={{marginBottom:4}}>
                    <strong style={{color:'#9a3412'}}>{s}:</strong>{' '}
                    {(SCENARIO_HINT[s] ? SCENARIO_HINT[s](m.method) : `Add a ${s} test for ${m.method}().`)
                      .split('`').map((part,k)=> k%2 ? <code key={k}>{part}</code> : part)}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      ))}
      {hollow.length>0 && (
        <div style={{marginTop:12,background:'#fff7ed',border:'1px solid #fed7aa',borderRadius:8,padding:'9px 12px'}}>
          <div style={{fontSize:12.5,fontWeight:700,color:'#9a3412',marginBottom:4}}>
            <i className="ti ti-alert-triangle" style={{marginRight:5}}/>{hollow.length} test(s) added with no assertions
          </div>
          <div style={{fontSize:11.5,color:'#9a3412',lineHeight:1.5}}>
            These pass trivially and give false confidence — add real assertions: <code>{hollow.slice(0,6).join(', ')}</code>{hollow.length>6?` +${hollow.length-6} more`:''}
          </div>
        </div>
      )}
      <div style={{fontSize:11,color:'#9fadbf',marginTop:10,display:'flex',alignItems:'flex-start',gap:5}}>
        <i className="ti ti-info-circle" style={{marginTop:1}}/>
        <span>Heuristic scan of the tests in this PR. Add the missing scenarios (or mark not-applicable in review). Detailed step-by-step cases are in the scenarios below.</span>
      </div>
    </div>
  )
}

const QA_TESTDATA = {
  functional: "Representative valid values + a few invalid ones; cover the main branches.",
  security:   "Unauthorized/expired token, forged identity, injection payloads (' OR 1=1 --, <script>), oversized & malformed input.",
  regression: "The exact input that triggered the original defect (and a near-miss variant).",
  edge_case:  "null, empty, 0, negative, max int, very long strings, unicode, off-by-one.",
  integration:"Mock the dependency for: success, timeout, 5xx error, and malformed response.",
  performance:"Large/high-volume inputs (1k / 10k / 100k); measure latency & memory vs baseline.",
  api:        "Valid + invalid bodies, missing required fields, wrong content-type, and the previous (v1) contract shape.",
  data:       "Round-trip records, nulls, precision-boundary values, and schema-mismatch rows.",
}

function scenarioToGherkin(s) {
  const steps = (s.steps||[])
  const when = steps.length ? steps : ['the change under test is exercised']
  const pre = (s.preconditions||[])
  const acc = (s.acceptance_criteria||[]).length ? s.acceptance_criteria
              : [s.expected_result || 'the expected outcome occurs and no error is raised']
  const lines = [
    `Scenario: ${s.title||s.id}`,
    `  # priority: ${(s.priority||'medium').toUpperCase()} · type: ${(s.type||'functional')}`,
    `  Given ${pre[0] || 'the system is in a valid, known state'}`,
    ...pre.slice(1).map(p=>`  And ${p}`),
    `  When ${when[0]}`,
    ...when.slice(1).map(st=>`  And ${st}`),
    `  Then ${acc[0]}`,
    ...acc.slice(1).map(a=>`  And ${a}`),
  ]
  return lines.join('\n')
}

// FSD validation: requirements from the uploaded spec checked against the diff,
// plus business-function impact across the declared dependent repos.
function CrossRepoTab({r}) {
  const cri = r.cross_repo_impact
  const sevC = s => ({critical:'#991b1b',high:'#b91c1c',medium:'#92400e',low:'#1e40af'})[(s||'').toLowerCase()]||'#7a8494'
  const impactMeta = {
    breaks:   ['#7f1d1d','#fee2e2','#fca5a5','✗ Breaks'],
    likely:   ['#b81c1c','#fff1f1','#f8c0c0','⚠ Likely breaks'],
    possible: ['#8a5200','#fff8ec','#fad98a','◐ Possible'],
    unlikely: ['#0c7c4b','#edfaf3','#b5e8cf','✓ Unlikely'],
    verify:   ['#7a8494','#f7f8fa','#e8eaed','— Verify'],
  }
  if (!cri || !cri.analysed) {
    return <div className="card"><div className="empty-state"><i className="ti ti-affiliate"/>
      No downstream repos analysed. Declare dependent repos in the Analysis Target (Connected repos) so call-sites of the changed symbols are pulled in and assessed here.
    </div></div>
  }
  const impacts = cri.impacts||[]
  // group by downstream repo
  const byRepo = {}
  impacts.forEach(i=>{ (byRepo[i.repo]=byRepo[i.repo]||[]).push(i) })
  const order = {breaks:0,likely:1,possible:2,unlikely:3,verify:4}
  return (
    <div>
      <div className="card" style={{marginBottom:12}}>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:10}}>
          <div className="section-heading" style={{marginBottom:0}}><i className="ti ti-affiliate"/>Downstream repo impact</div>
          <div style={{display:'flex',gap:14,alignItems:'center',fontSize:11.5,color:'#7a8494'}}>
            <span><strong style={{color:'#1a2332',fontSize:15}}>{cri.total_call_sites}</strong> call-site(s)</span>
            <span><strong style={{color:cri.breaking_count?'#b91c1c':'#0c7c4b',fontSize:15}}>{cri.breaking_count}</strong> likely to break</span>
            <span><strong style={{color:'#1a2332',fontSize:15}}>{(cri.repos_analysed||[]).length}</strong> repo(s)</span>
            {cri.fallback_used && <span title="Heuristic (signature-diff) analysis — the LLM did not run for this agent." style={{fontSize:10,fontWeight:700,padding:'1px 7px',borderRadius:10,background:'#fff7ed',color:'#9a3412',border:'1px solid #fed7aa'}}>heuristic</span>}
          </div>
        </div>
        <div style={{fontSize:12,color:'#7a8494',marginTop:8}}>{cri.summary||''}</div>
      </div>
      {Object.entries(byRepo).map(([repo, list])=>(
        <div key={repo} className="card" style={{marginBottom:12}}>
          <div className="section-heading" style={{marginBottom:8}}><i className="ti ti-git-fork"/><code>{repo}</code></div>
          {list.sort((a,b)=>(order[a.impact]??9)-(order[b.impact]??9)).map((i,idx)=>{
            const [c,bg,bd,lbl] = impactMeta[i.impact]||impactMeta.verify
            return (
              <div key={idx} className="finding" style={{flexDirection:'column',alignItems:'stretch',gap:7,borderLeft:`3px solid ${sevC(i.severity)}`}}>
                <div style={{display:'flex',alignItems:'center',gap:9,flexWrap:'wrap'}}>
                  <span style={{fontSize:10.5,fontWeight:700,padding:'2px 9px',borderRadius:10,background:bg,color:c,border:`1px solid ${bd}`}}>{lbl}</span>
                  <span style={{fontSize:11,fontWeight:700,color:sevC(i.severity),textTransform:'uppercase'}}>{i.severity}</span>
                  <code style={{fontSize:12.5}}>{i.symbol}</code>
                  <span style={{fontSize:11,color:'#7a8494'}}>({(i.change_kind||'').replace(/_/g,' ')})</span>
                  <span style={{marginLeft:'auto',fontFamily:'var(--mono)',fontSize:11,color:'#7a8494'}}>{i.file}{i.line?`:${i.line}`:''}</span>
                </div>
                {i.reason && <div style={{fontSize:12.5,color:'#374151'}}>{i.reason}</div>}
                {i.suggested_fix && <div style={{fontSize:12,color:'#0c7c4b'}}><i className="ti ti-bulb" style={{marginRight:3}}/>{i.suggested_fix}</div>}
                {i.caller_context && <pre style={{margin:0,padding:'8px 10px',background:'#0d1117',color:'#e6edf3',borderRadius:6,fontSize:11,overflowX:'auto',fontFamily:'var(--mono)',maxHeight:200}}>{i.caller_context}</pre>}
                <div style={{marginTop:2}}><FindingFeedback r={r} agent="cross_repo_impact" category={i.change_kind||''} file={`${i.repo}/${i.file}`}/></div>
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}

function FunctionalTab({r}) {
  const fv = r.functional_validation
  const statusMeta = {
    implemented:  ['#0c7c4b','#edfaf3','#b5e8cf','✓ Implemented'],
    partial:      ['#8a5200','#fff8ec','#fad98a','◐ Partial'],
    missing:      ['#b81c1c','#fff1f1','#f8c0c0','✗ Missing'],
    contradicted: ['#7f1d1d','#fee2e2','#fca5a5','⚠ Contradicted'],
    not_addressed:['#7a8494','#f7f8fa','#e8eaed','— Not in scope'],
  }
  const riskC = {high:'#b81c1c',medium:'#8a5200',low:'#0c7c4b'}
  if (!fv) return <div className="card"><div className="empty-state"><i className="ti ti-file-check"/>FSD validation has not run for this report.</div></div>
  const reqs = fv.requirements||[], impacts = fv.impacts||[]
  const inScope = reqs.filter(q=>q.status!=='not_addressed')
  return (
    <div>
      {(fv.notes||[]).length>0 && reqs.length===0 && (
        <div className="card"><div className="empty-state" style={{textAlign:'left'}}>
          <i className="ti ti-file-off"/>
          <div style={{marginTop:8}}>{(fv.notes||[]).map((n,i)=><div key={i} style={{fontSize:13,color:'#5b6675',lineHeight:1.6}}>{n}</div>)}</div>
        </div></div>
      )}
      {reqs.length>0 && (
        <div className="card" style={{marginBottom:12}}>
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:10,marginBottom:6}}>
            <div className="section-heading" style={{marginBottom:0}}><i className="ti ti-file-check"/>Spec requirements vs this change</div>
            <div style={{display:'flex',gap:10,alignItems:'center',fontSize:11.5,color:'#7a8494'}}>
              <span><strong style={{color:'#1a6cf6',fontSize:15}}>{fv.coverage_pct}%</strong> of requirements touched</span>
              {fv.has_contradiction && <span style={{color:'#b81c1c',fontWeight:700}}>⚠ contradiction found</span>}
            </div>
          </div>
          <div style={{fontSize:12,color:'#7a8494',marginBottom:10}}>{fv.summary||''} {fv.docs_analysed?.length?<span>Docs: {fv.docs_analysed.join(', ')}</span>:null}</div>
          {[...inScope, ...reqs.filter(q=>q.status==='not_addressed')].map((q,i)=>{
            const [c,bg,bd,lbl] = statusMeta[q.status]||statusMeta.not_addressed
            return (
              <div key={i} className="finding" style={{alignItems:'flex-start',gap:10,opacity:q.status==='not_addressed'?0.65:1}}>
                <span style={{fontFamily:'var(--mono)',fontSize:11,fontWeight:700,color:'#9fadbf',minWidth:36,marginTop:3}}>{q.req_id}</span>
                <span style={{fontSize:10.5,fontWeight:700,padding:'2px 9px',borderRadius:10,whiteSpace:'nowrap',flexShrink:0,marginTop:1,background:bg,color:c,border:`1px solid ${bd}`}}>{lbl}</span>
                <div className="finding-body">
                  <div className="finding-desc" style={{fontSize:12.5}}>{q.text}</div>
                  <div className="finding-file">
                    {q.evidence && <span style={{fontFamily:'var(--mono)'}}>{q.evidence} · </span>}
                    {q.notes||''}{q.source_doc?` · ${q.source_doc}`:''}
                  </div>
                  {['contradicted','missing'].includes(q.status) && <div style={{marginTop:5}}><FindingFeedback r={r} agent="functional_validation" category={q.status} file={q.evidence||''}/></div>}
                </div>
              </div>
            )
          })}
        </div>
      )}
      {impacts.length>0 && (
        <div className="card">
          <div className="section-heading"><i className="ti ti-sitemap"/>Functional impact ({impacts.length})</div>
          <div style={{fontSize:12,color:'#7a8494',marginBottom:10}}>Business functions this change touches — combined view of the FSD, the diff and your declared dependent repos.</div>
          {impacts.map((m,i)=>(
            <div key={i} className="finding" style={{flexDirection:'column',alignItems:'stretch',gap:6}}>
              <div style={{display:'flex',alignItems:'center',gap:10,flexWrap:'wrap'}}>
                <span style={{fontSize:10.5,fontWeight:700,padding:'2px 9px',borderRadius:10,textTransform:'uppercase',background:`${riskC[m.risk]||'#7a8494'}1a`,color:riskC[m.risk]||'#7a8494',border:`1px solid ${riskC[m.risk]||'#7a8494'}55`}}>{m.risk}</span>
                <strong style={{fontSize:13.5,color:'#1c2530'}}>{m.function}</strong>
              </div>
              <div style={{fontSize:12.5,color:'#4a5568',lineHeight:1.5}}>{m.impact}</div>
              <div style={{display:'flex',gap:8,flexWrap:'wrap',alignItems:'center',fontSize:11.5}}>
                {m.test_focus && <span style={{color:'#0c7c4b'}}><i className="ti ti-target" style={{marginRight:3}}/>Test focus: <code>{m.test_focus}</code></span>}
                {(m.affected_repos||[]).map(rp=><span key={rp} style={{fontSize:10,fontWeight:600,padding:'1px 8px',borderRadius:9,background:'#f0f4fa',color:'#3a4452',border:'1px solid #dde5f0'}}><i className="ti ti-git-branch" style={{fontSize:10,marginRight:3}}/>{rp}</span>)}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function QAScenariosTab({r}) {
  const [filter, setFilter] = useState('all')
  const [copied, setCopied] = useState('')
  const qa = r.qa_scenarios
  const hasUnits = (r.test_coverage?.method_coverage || []).length > 0
  if (!qa?.scenarios?.length && !hasUnits) return <div className="card"><div className="empty-state"><i className="ti ti-checklist"/>No QA scenarios generated yet.</div></div>
  if (!qa?.scenarios?.length) return <div><UnitTestCoverage tc={r.test_coverage}/></div>
  const priColor=p=>({critical:'#b81c1c',high:'#8a5200',medium:'#1a6cf6',low:'#6b7280'})[p?.toLowerCase()]||'#6b7280'
  const typeIcon=t=>({functional:'ti-click',security:'ti-shield-lock',regression:'ti-arrows-right-left',edge_case:'ti-test-pipe',integration:'ti-puzzle',performance:'ti-gauge',api:'ti-api',data:'ti-database'})[t]||'ti-checklist'
  const priorities=['all','critical','high','medium','low']
  const types=['functional','security','regression','edge_case','integration','performance','api','data']
  const filtered = filter==='all'?qa.scenarios:qa.scenarios.filter(s=>s.priority?.toLowerCase()===filter||s.type===filter)
  const critN=qa.scenarios.filter(s=>s.priority?.toLowerCase()==='critical').length
  const highN=qa.scenarios.filter(s=>s.priority?.toLowerCase()==='high').length
  return (
    <div>
      <UnitTestCoverage tc={r.test_coverage}/>
      {qa.fallback_used && (
        <div style={{display:'flex',alignItems:'flex-start',gap:8,background:'#fff8ec',border:'1px solid #fad98a',borderRadius:8,padding:'9px 12px',marginBottom:12,fontSize:12.5,color:'#8a5200'}}>
          <i className="ti ti-alert-triangle" style={{marginTop:1}}/>
          <span><strong>Generic template scenarios</strong> — the LLM did not run for this agent (no model key/budget, or a parse error), so these are static fallback scenarios, not tailored to this change. Configure the AI model to get change-specific QA scenarios.</span>
        </div>
      )}
      <div className="card" style={{marginBottom:12}}>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:10,marginBottom:14}}>
          <div><div className="card-title" style={{marginBottom:4}}><i className="ti ti-checklist"/>QA Test Scenarios</div><div style={{fontSize:12,color:'#7a8494'}}>{qa.summary||''}</div></div>
          <div style={{display:'flex',gap:12,fontSize:12}}>
            {critN>0&&<span style={{color:'#b81c1c',fontWeight:700}}><i className="ti ti-alert-circle"/> {critN} critical</span>}
            {highN>0&&<span style={{color:'#8a5200',fontWeight:700}}><i className="ti ti-alert-triangle"/> {highN} high</span>}
            <span style={{color:'#7a8494'}}>{qa.total_scenarios} total</span>
          </div>
        </div>
        <div style={{display:'flex',gap:6,flexWrap:'wrap'}}>
          {priorities.map(p=><button key={p} className={`btn btn-sm ${filter===p?'btn-primary':''}`} onClick={()=>setFilter(p)}>{p==='all'?'All':p.charAt(0).toUpperCase()+p.slice(1)}</button>)}
          <span style={{width:1,background:'#e8eaed',margin:'0 4px'}}/>
          {types.map(t=><button key={t} className={`btn btn-sm ${filter===t?'btn-primary':''}`} onClick={()=>setFilter(t)}>{t.replace(/_/g,' ')}</button>)}
        </div>
      </div>
      {!filtered.length?<div className="empty-state" style={{padding:20}}><i className="ti ti-filter-off"/>No scenarios match this filter.</div>
        :filtered.map((s,i)=>(
          <div key={i} className="card" style={{marginBottom:12,borderLeft:`3px solid ${priColor(s.priority)}`}}>
            <div style={{display:'flex',alignItems:'flex-start',gap:10,flexWrap:'wrap',marginBottom:10}}>
              <span style={{fontFamily:'JetBrains Mono,monospace',fontSize:11,background:'#f0f2f5',padding:'2px 7px',borderRadius:10,color:'#7a8494'}}>{s.id}</span>
              <span style={{display:'inline-flex',alignItems:'center',gap:3,padding:'2px 8px',background:`${priColor(s.priority)}18`,color:priColor(s.priority),borderRadius:10,fontSize:11,fontWeight:700}}>{(s.priority||'').toUpperCase()}</span>
              <span style={{display:'inline-flex',alignItems:'center',gap:4,padding:'2px 8px',background:'#f0f2f5',color:'#4b5563',borderRadius:10,fontSize:11}}><i className={`ti ${typeIcon(s.type)}`} style={{fontSize:12}}/>{(s.type||'').replace(/_/g,' ')}</span>
              <div style={{flex:1,minWidth:200,fontSize:14,fontWeight:600,color:'#0d1117'}}>{s.title}</div>
              <button className="btn btn-sm" title="Copy this scenario as a Gherkin (Given/When/Then) template"
                onClick={()=>{ navigator.clipboard?.writeText(scenarioToGherkin(s)); setCopied(s.id); setTimeout(()=>setCopied(''),1500) }}>
                <i className="ti ti-copy"/> {copied===s.id?'Copied':'Gherkin'}
              </button>
            </div>
            <p style={{fontSize:13,color:'#4b5563',marginBottom:12,lineHeight:1.6}}>{s.description}</p>

            <div style={{display:'grid',gridTemplateColumns:'1fr',gap:10}}>
              {(s.preconditions||[]).length>0&&<div><div className="qa-sec-label">Preconditions</div><ul style={{margin:0,paddingLeft:20,fontSize:12.5,color:'#374151',lineHeight:1.7}}>{s.preconditions.map((p,j)=><li key={j}>{p}</li>)}</ul></div>}

              {s.steps?.length>0&&<div><div className="qa-sec-label">Test steps</div><ol style={{margin:0,paddingLeft:20,fontSize:13,color:'#374151',lineHeight:1.8}}>{s.steps.map((st,j)=><li key={j}>{st}</li>)}</ol></div>}

              <div><div className="qa-sec-label">Test data &amp; inputs to cover</div>
                <div style={{fontSize:12.5,color:'#4b5563',lineHeight:1.55}}>{QA_TESTDATA[s.type]||QA_TESTDATA.functional}</div>
              </div>

              {(s.acceptance_criteria||[]).length>0&&<div><div className="qa-sec-label">Acceptance criteria (pass/fail)</div><ul style={{margin:0,paddingLeft:20,fontSize:12.5,color:'#166534',lineHeight:1.7}}>{s.acceptance_criteria.map((a,j)=><li key={j}>{a}</li>)}</ul></div>}

              {s.expected_result&&!(s.acceptance_criteria||[]).length&&<div style={{background:'#f0fdf4',border:'1px solid #bbf7d0',borderRadius:6,padding:'8px 12px',fontSize:12.5,color:'#166534'}}><strong>Expected result:</strong> {s.expected_result}</div>}

              {s.test_skeleton&&<details><summary style={{fontSize:11.5,fontWeight:600,color:'#1a6cf6',cursor:'pointer',userSelect:'none'}}>▸ Ready-to-run test skeleton</summary>
                <pre style={{margin:'6px 0 0',background:'#0d1117',color:'#e6edf3',borderRadius:6,padding:'10px 12px',fontSize:12,fontFamily:'JetBrains Mono,monospace',overflowX:'auto',lineHeight:1.5}}>{s.test_skeleton}</pre>
                <button className="btn btn-sm" style={{marginTop:6}} onClick={()=>{navigator.clipboard?.writeText(s.test_skeleton);setCopied(s.id+'-sk');setTimeout(()=>setCopied(''),1500)}}><i className="ti ti-copy"/> {copied===s.id+'-sk'?'Copied':'Copy skeleton'}</button>
              </details>}

              <div style={{display:'flex',flexWrap:'wrap',gap:14,fontSize:11.5,color:'#7a8494',alignItems:'center'}}>
                {s.automation_hint && <span><i className="ti ti-robot" style={{marginRight:4,color:'#8b5cf6'}}/><strong>Automate with:</strong> {s.automation_hint}</span>}
                {(s.affected_files||[]).length>0 && <span><i className="ti ti-file-code" style={{marginRight:4}}/><strong>Covers:</strong> {(s.affected_files||[]).slice(0,3).map(f=>f.split(/[\\/]/).pop()).join(', ')}{s.affected_files.length>3?` +${s.affected_files.length-3}`:''}</span>}
              </div>
            </div>
          </div>
        ))}
    </div>
  )
}

function PerformanceTab({r, snipCache}) {
  const perf=r.performance_impact
  if (!perf) return <div className="card"><div className="empty-state"><i className="ti ti-rocket"/>No performance data available.</div></div>
  const sevColor=s=>({critical:'#b81c1c',high:'#8a5200',medium:'#1a6cf6',low:'#6b7280'})[s?.toLowerCase()]||'#6b7280'
  const findings=perf.findings||[]
  return (
    <div className="card" style={{marginBottom:12}}>
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:10,marginBottom:14}}>
        <div><div className="card-title" style={{marginBottom:4}}><i className="ti ti-rocket"/>Performance Impact Analysis</div><div style={{fontSize:12,color:'#7a8494'}}>{perf.summary||''}</div></div>
        <div style={{display:'flex',gap:12,fontSize:12,alignItems:'center'}}>
          <span style={{color:sevColor(perf.overall_severity),fontWeight:700,fontSize:13}}>{(perf.overall_severity||'low').toUpperCase()} impact</span>
          {perf.regression_risk&&<span className="badge badge-amber"><i className="ti ti-alert-triangle" style={{fontSize:11}}/>Regression risk</span>}
        </div>
      </div>
      {findings.length===0?<div className="empty-state"><i className="ti ti-circle-check"/>No performance issues found</div>
        :findings.map((f,i)=><div key={i} className="finding"><span className={`sev sev-${(f.severity||'low').toLowerCase()}`}>{f.severity||'low'}</span><div className="finding-body"><div className="finding-desc"><code>{f.kind||''}</code> — {f.description||''}<UnvBadge f={f}/></div><div className="finding-file">{f.file||''}{f.line?` · line ${f.line}`:''}</div>{f.suggestion&&<div style={{fontSize:11,color:'#0c7c4b',marginTop:4}}><i className="ti ti-bulb" style={{marginRight:3}}/>{f.suggestion}</div>}{getCodeSnippetJSX(f.file,f.line,snipCache,3)}</div></div>)}
    </div>
  )
}

function PrivacyTab({r, snipCache}) {
  const priv=r.data_privacy
  if (!priv) return <div className="card"><div className="empty-state"><i className="ti ti-lock"/>No data privacy data available.</div></div>
  const findings=priv.findings||[]; const violations=priv.compliance_violations||[]
  return (
    <div className="card" style={{marginBottom:12}}>
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:10,marginBottom:14}}>
        <div><div className="card-title" style={{marginBottom:4}}><i className="ti ti-lock"/>Data Privacy Analysis</div><div style={{fontSize:12,color:'#7a8494'}}>{priv.summary||''}</div></div>
        <div style={{display:'flex',gap:12,fontSize:12,alignItems:'center'}}>
          {priv.pii_detected&&<span className="badge badge-red"><i className="ti ti-alert-circle" style={{fontSize:11}}/>PII Detected</span>}
          {violations.length>0&&<span className="badge badge-amber">{violations.length} violation{violations.length!==1?'s':''}</span>}
        </div>
      </div>
      {violations.length>0&&<div style={{marginBottom:14}}><div style={{fontSize:11,fontWeight:600,textTransform:'uppercase',letterSpacing:.06,color:'#9fadbf',marginBottom:6}}>Compliance frameworks affected</div><div style={{display:'flex',flexWrap:'wrap',gap:6}}>{violations.map(v=><span key={v} className="badge badge-red">{v}</span>)}</div></div>}
      {findings.length===0?<div className="empty-state"><i className="ti ti-shield-check"/>No PII or privacy violations found</div>
        :findings.map((f,i)=><div key={i} className="finding"><span className={`sev sev-${(f.severity||'medium').toLowerCase()}`}>{f.severity||'medium'}</span><div className="finding-body"><div className="finding-desc"><code>{f.pii_type||f.kind||''}</code> — {f.description||''}<UnvBadge f={f}/></div><div className="finding-file">{f.file||''}{f.line?` · line ${f.line}`:''}</div>{f.recommendation&&<div style={{fontSize:11,color:'#0c7c4b',marginTop:4}}><i className="ti ti-bulb" style={{marginRight:3}}/>{f.recommendation}</div>}{getCodeSnippetJSX(f.file,f.line,snipCache,3)}</div></div>)}
    </div>
  )
}

function QualityTab({r, snipCache}) {
  const maint=r.maintainability, license=r.license_compliance, obs=r.observability
  const emptyRow=msg=><div style={{fontSize:12,color:'#9fadbf',padding:'8px 0'}}><i className="ti ti-circle-check" style={{marginRight:5}}/>{msg}</div>
  const agentSection=(icon,color,title,body)=>(<div key={title} style={{marginBottom:20,paddingBottom:20,borderBottom:'1px solid #e8eaed'}}><div style={{display:'flex',alignItems:'center',gap:8,marginBottom:12}}><i className={`ti ${icon}`} style={{fontSize:16,color}}/><span style={{fontSize:13,fontWeight:600,color:'#0d1117'}}>{title}</span></div>{body}</div>)
  const maintBody=!maint?emptyRow('Agent did not run'):(() => {
    const issues=maint.issues||[]; const score=maint.score??100; const scoreColor=score>=80?'#0c7c4b':score>=60?'#8a5200':'#b81c1c'
    return <div><div style={{display:'flex',gap:16,alignItems:'center',marginBottom:12}}><div style={{textAlign:'center'}}><div style={{fontSize:28,fontWeight:700,color:scoreColor}}>{score}</div><div style={{fontSize:10,color:'#9fadbf',textTransform:'uppercase',letterSpacing:.06}}>score /100</div></div><div style={{flex:1}}><div className="score-bar"><div className="score-fill" style={{width:`${score}%`,background:scoreColor}}/></div></div></div>{issues.length===0?emptyRow('No maintainability issues'):issues.map((i2,idx)=><div key={idx} className="finding"><span className={`sev sev-${(i2.severity||'low').toLowerCase()}`}>{i2.severity||'low'}</span><div className="finding-body"><div className="finding-desc"><code>{i2.kind||''}</code> — {i2.description||''}<UnvBadge f={i2}/></div><div className="finding-file">{i2.file||''}{i2.line?` · line ${i2.line}`:''}</div>{i2.suggestion&&<div style={{fontSize:11,color:'#0c7c4b',marginTop:4}}><i className="ti ti-bulb" style={{marginRight:3}}/>{i2.suggestion}</div>}</div></div>)}</div>
  })()
  const licenseBody=!license?emptyRow('Agent did not run'):(() => {
    const findings=license.findings||[]
    return findings.length===0?emptyRow('No license issues'):(<div>{(license.has_copyleft||license.has_unknown)&&<div style={{display:'flex',gap:10,flexWrap:'wrap',marginBottom:10}}>{license.has_copyleft&&<span className="badge badge-red"><i className="ti ti-alert-circle" style={{fontSize:11}}/>Copyleft detected</span>}{license.has_unknown&&<span className="badge badge-amber">Unknown licenses</span>}</div>}{findings.map((f,i)=><div key={i} className="finding"><span className={`sev sev-${(f.severity||'low').toLowerCase()}`}>{f.severity||'low'}</span><div className="finding-body"><div className="finding-desc"><code>{f.package||''}</code> — <code>{f.license||'unknown'}</code></div><div className="finding-file">{f.file||''}{f.reason?` — ${f.reason}`:''}</div></div></div>)}</div>)
  })()
  const obsBody=!obs?emptyRow('Agent did not run'):(() => {
    const findings=obs.findings||[]; const score=obs.observability_score??100; const scoreColor=score>=80?'#0c7c4b':score>=60?'#8a5200':'#b81c1c'
    return <div><div style={{display:'flex',gap:16,alignItems:'center',marginBottom:12}}><div style={{textAlign:'center'}}><div style={{fontSize:28,fontWeight:700,color:scoreColor}}>{score}</div><div style={{fontSize:10,color:'#9fadbf',textTransform:'uppercase',letterSpacing:.06}}>obs score /100</div></div><div style={{flex:1}}><div className="score-bar"><div className="score-fill" style={{width:`${score}%`,background:scoreColor}}/></div></div></div>{findings.length===0?emptyRow('No observability gaps'):findings.map((f,i)=><div key={i} className="finding"><span className={`sev sev-${(f.severity||'low').toLowerCase()}`}>{f.severity||'low'}</span><div className="finding-body"><div className="finding-desc"><code>{f.kind||''}</code> — {f.description||''}<UnvBadge f={f}/></div><div className="finding-file">{f.file||''}{f.line?` · line ${f.line}`:''}</div>{f.suggestion&&<div style={{fontSize:11,color:'#0c7c4b',marginTop:4}}><i className="ti ti-bulb" style={{marginRight:3}}/>{f.suggestion}</div>}{getCodeSnippetJSX(f.file,f.line,snipCache,3)}</div></div>)}</div>
  })()
  return <div className="card">{agentSection('ti-tool','#6366f1','Maintainability',maintBody)}{agentSection('ti-license','#10b981','License Compliance',licenseBody)}{agentSection('ti-eye','#0ea5e9','Observability',obsBody)}</div>
}

function ChecklistTab({r, canOverride}) {
  function buildChecklist(r) {
    const item=(domain,label,status,detail='')=>({domain,label,status,detail})
    const items=[]
    if(r.security){const crit=(r.security.findings||[]).filter(f=>['critical','high'].includes((f.severity||'').toLowerCase()));const detail=crit.length?`${crit.length} finding(s): ${crit.slice(0,2).map(f=>[f.cwe,(f.description||'').slice(0,50)].filter(Boolean).join(' ')).join('; ')}`:'';items.push(item('security','No critical/high security vulnerabilities',crit.length?'fail':'pass',detail));items.push(item('security','No hardcoded secrets or credentials',r.security.secrets_detected?'fail':'pass',r.security.secrets_detected?'Secrets detected — rotate immediately':''))}else items.push(item('security','Security analysis','skip','Agent did not run'))
    if(r.data_privacy){items.push(item('privacy','PII fields encrypted/hashed',(r.data_privacy.unencrypted_pii_count||0)>0?'fail':'pass',(r.data_privacy.unencrypted_pii_count||0)>0?`${r.data_privacy.unencrypted_pii_count} unencrypted`:''));items.push(item('privacy','No PII logged or printed',(r.data_privacy.logging_violations||[]).length>0?'fail':'pass',(r.data_privacy.logging_violations||[]).length>0?`${(r.data_privacy.logging_violations||[]).length} violation(s)`:''))}else items.push(item('privacy','Data privacy review','skip','Agent did not run'))
    if(r.performance_impact){items.push(item('performance','No N+1 queries or unbounded DB calls',r.performance_impact.has_db_risk?'warn':'pass',r.performance_impact.has_db_risk?'DB query risk detected':''));items.push(item('performance','No algorithmic complexity regression',r.performance_impact.has_complexity_regression?'warn':'pass',r.performance_impact.has_complexity_regression?'Nested loop / O(n²) detected':''))}else items.push(item('performance','Performance review','skip','Agent did not run'))
    if(r.test_coverage){const delta=parseFloat(r.test_coverage.coverage_delta||0);items.push(item('testing','Test coverage not reduced',delta<-5?'warn':'pass',delta<-5?`Coverage delta: ${delta.toFixed(1)}%`:''));const unt=r.test_coverage.untested_functions||[];items.push(item('testing','All changed functions have tests',unt.length?'warn':'pass',unt.length?`Untested: ${unt.slice(0,3).join(', ')}`:'' ))}else items.push(item('testing','Test coverage review','skip','Agent did not run'))
    if(r.interface){const br=r.interface.breaking_changes||[];items.push(item('interface','No breaking API changes',br.length?'fail':'pass',br.length?`${br.length} breaking change(s)`:''))}else items.push(item('interface','API contract review','skip','Agent did not run'))
    if(r.schema_change){const risky=(r.schema_change.changes||[]).filter(f=>['high','critical'].includes((f.severity||'').toLowerCase()));items.push(item('schema','Database migration is safe',risky.length?'fail':'pass',risky.length?`${risky.length} risky change(s)`:''))}else items.push(item('schema','Schema migration review','skip','Agent did not run'))
    if(r.license_compliance){items.push(item('license','No copyleft (GPL/AGPL) dependencies',r.license_compliance.has_copyleft?'fail':'pass',r.license_compliance.has_copyleft?'Copyleft licence detected':''))}else items.push(item('license','Licence compliance','skip','Agent did not run'))
    if(r.risk){const score=r.risk.risk_score||0;items.push(item('deployment','Deployment risk acceptable',score>=7?'fail':score>=4?'warn':'pass',`Risk score ${score}/10 · ${r.remediation?.deployment_strategy||'standard'}`))}else items.push(item('deployment','Deployment risk review','skip','Agent did not run'))
    return items
  }
  const items=buildChecklist(r)
  const pass=items.filter(i=>i.status==='pass').length,fail=items.filter(i=>i.status==='fail').length,warn=items.filter(i=>i.status==='warn').length
  const realGate=(r.gate_decision||r.gate||'HOLD').toUpperCase()
  const gateColor=realGate==='BLOCK'?'#b91c1c':realGate==='HOLD'?'#92400e':'#166534'
  const gateLabel=realGate==='BLOCK'?'🚫 BLOCK':realGate==='HOLD'?'⚠️ HOLD':'✅ APPROVE'
  // The header shows the ACTUAL gate, not one derived from these binary checks —
  // explain when they disagree (e.g. all checks pass but gate is HOLD on risk score).
  const checksImply = fail>0?'BLOCK':warn>0?'HOLD':'APPROVE'
  const gateMismatch = checksImply !== realGate
  const byDomain={}; items.forEach(i=>{if(!byDomain[i.domain])byDomain[i.domain]=[];byDomain[i.domain].push(i)})
  const statusIcon={pass:'✅',warn:'⚠️',fail:'❌',skip:'⬜'}
  const statusColor={pass:'#166534',warn:'#92400e',fail:'#991b1b',skip:'#9fadbf'}
  const statusBg={pass:'#f0fdf4',warn:'#fffbeb',fail:'#fff1f2',skip:'#f9fafb'}
  const domainLabels={security:{icon:'🔒',label:'Security'},privacy:{icon:'🔐',label:'Data Privacy'},performance:{icon:'🚀',label:'Performance'},testing:{icon:'🧪',label:'Test Coverage'},interface:{icon:'🔌',label:'API Contract'},schema:{icon:'🗄',label:'Database Schema'},license:{icon:'⚖️',label:'Licence Compliance'},deployment:{icon:'🚀',label:'Deployment Risk'}}
  return (
    <div className="card">
      <div style={{display:'flex',alignItems:'center',gap:12,marginBottom:18,flexWrap:'wrap'}}>
        <div style={{fontSize:22,fontWeight:800,color:gateColor}}>{gateLabel}</div>
        <div style={{display:'flex',gap:8,marginLeft:'auto',flexWrap:'wrap'}}>
          <span style={{background:'#f0fdf4',color:'#166534',border:'1px solid #86efac',borderRadius:6,padding:'4px 12px',fontSize:12,fontWeight:700}}>✅ {pass} passed</span>
          {warn>0&&<span style={{background:'#fffbeb',color:'#92400e',border:'1px solid #fcd34d',borderRadius:6,padding:'4px 12px',fontSize:12,fontWeight:700}}>⚠️ {warn} warnings</span>}
          {fail>0&&<span style={{background:'#fff1f2',color:'#991b1b',border:'1px solid #fca5a5',borderRadius:6,padding:'4px 12px',fontSize:12,fontWeight:700}}>❌ {fail} failed</span>}
        </div>
      </div>
      {gateMismatch&&(
        <div style={{padding:'10px 14px',background:'#fffbeb',border:'1px solid #fcd34d',borderRadius:8,marginBottom:16,fontSize:12,color:'#92400e',display:'flex',alignItems:'flex-start',gap:8}}>
          <i className="ti ti-info-circle" style={{fontSize:14,marginTop:1}}/>
          <span>Individual checks imply <strong>{checksImply}</strong>, but the overall gate is <strong>{realGate}</strong>. The gate reflects the AI risk assessment (risk score {r.risk_score||0}/100{r.rationale?` — ${r.rationale}`:''}), which weighs factors beyond these binary checks (e.g. coverage drop, change complexity).</span>
        </div>
      )}
      <div style={{padding:'10px 14px',background:'#f7f8fa',border:'1px solid #e8eaed',borderRadius:8,marginBottom:16,fontSize:12,color:'#7a8494',display:'flex',alignItems:'center',gap:8}}>
        <i className="ti ti-arrow-up" style={{fontSize:14}}/>
        <span>{canOverride?<>Use the <strong>Human Review</strong> panel at the top of this page to approve, request changes, or block this merge.</>:'View-only access — Reviewer actions require reviewer role.'}</span>
      </div>
      <div style={{display:'flex',flexDirection:'column',gap:14}}>
        {Object.entries(byDomain).map(([domain, domItems])=>{
          const info=domainLabels[domain]||{icon:'📋',label:domain}
          const domFail=domItems.some(i=>i.status==='fail'),domWarn=domItems.some(i=>i.status==='warn')
          const domColor=domFail?'#fff1f2':domWarn?'#fffbeb':'#f0fdf4',domBorder=domFail?'#fca5a5':domWarn?'#fcd34d':'#86efac'
          return (
            <div key={domain} style={{border:`1.5px solid ${domBorder}`,borderRadius:10,background:domColor,overflow:'hidden'}}>
              <div style={{padding:'10px 14px',fontWeight:700,fontSize:13,borderBottom:`1px solid ${domBorder}`,display:'flex',alignItems:'center',gap:8}}>{info.icon} {info.label}</div>
              {domItems.map((item,i)=>(
                <div key={i} style={{padding:'10px 14px',borderBottom:`1px solid ${domBorder}26`,display:'flex',alignItems:'flex-start',gap:10,background:statusBg[item.status]}}>
                  <span style={{fontSize:16,flexShrink:0,marginTop:1}}>{statusIcon[item.status]}</span>
                  <div style={{flex:1}}><div style={{fontSize:13,fontWeight:600,color:statusColor[item.status]}}>{item.label}</div>{item.detail&&<div style={{fontSize:11,color:'#7a8494',marginTop:2}}>{item.detail}</div>}</div>
                </div>
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function ComplianceTab({r, snipCache}) {
  const c = r.compliance
  if (!c || !c.standards) return <div className="card"><div className="empty-state"><i className="ti ti-shield-off"/>No compliance data — run analysis with a connected backend.</div></div>
  const fail = (c.overall||{}).fail || 0
  const pass = (c.overall||{}).pass || 0
  function exportMd() {
    const lines = [`# Compliance Report`, ``, `**Overall: ${c.overall.status}** — ${fail} failed, ${pass} passed`, ``]
    c.standards.forEach(s=>{ lines.push(`## ${s.name}`); s.items.forEach(it=>lines.push(`- [${it.status==='fail'?'x':' '}] **${it.id}** ${it.title}${it.detail?` — ${it.detail}`:''}`)); lines.push('') })
    const blob = new Blob([lines.join('\n')], {type:'text/markdown'})
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='compliance-report.md'; a.click(); URL.revokeObjectURL(a.href)
  }
  return (
    <div>
      <div className="card" style={{marginBottom:12}}>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:10}}>
          <div className="card-title" style={{marginBottom:0}}><i className="ti ti-shield-check"/>Compliance mapping</div>
          <div style={{display:'flex',alignItems:'center',gap:10}}>
            <span style={{fontSize:13,fontWeight:700,padding:'3px 12px',borderRadius:14,
              background:fail?'#fff1f2':'#f0fdf4', color:fail?'#991b1b':'#166534', border:`1px solid ${fail?'#fecaca':'#bbf7d0'}`}}>
              {c.overall.status} · {fail} failed / {pass} passed
            </span>
            <button className="btn btn-sm" onClick={exportMd}><i className="ti ti-download"/> Export</button>
          </div>
        </div>
        <div style={{fontSize:12,color:'#7a8494',marginTop:8}}>Derived from this change’s findings. <strong>Fail</strong> = evidence against the control in this PR; <strong>Pass</strong> = checked, none found.</div>
      </div>
      {c.standards.map((s,i)=>{
        const fails=s.items.filter(it=>it.status==='fail')
        return (
          <div key={i} className="card" style={{marginBottom:12}}>
            <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:10}}>
              <div className="card-title" style={{marginBottom:0}}>{s.name}</div>
              <span style={{marginLeft:'auto',fontSize:11,fontWeight:700,color:fails.length?'#991b1b':'#166534'}}>
                {fails.length?`${fails.length} failing`:'all clear'}
              </span>
            </div>
            <div style={{display:'flex',flexDirection:'column',gap:6}}>
              {s.items.map((it,j)=>{
                const f=it.status==='fail'
                return (
                  <div key={j} style={{display:'flex',alignItems:'flex-start',gap:9,padding:'7px 10px',borderRadius:7,
                    background:f?'#fff7f7':'#fafdfb', border:`1px solid ${f?'#fbdcdc':'#e3f3e9'}`}}>
                    <span style={{fontSize:13,flexShrink:0,color:f?'#b91c1c':'#16a34a'}}>{f?'✗':'✓'}</span>
                    <div style={{minWidth:0,flex:1}}>
                      <div style={{fontSize:12.5,color:'#1a2332'}}><strong>{it.id}</strong> {it.title}</div>
                      {it.detail&&<div style={{fontSize:11,color:f?'#9a3412':'#7a8494'}}>{it.detail}</div>}
                      {(it.evidence||[]).map((ev,k)=>(
                        <div key={k} style={{marginTop:6}}>
                          <div style={{fontSize:11,color:'#7a2020'}}>
                            {ev.label}{ev.file?<span style={{color:'#9fadbf'}}> · <code>{(ev.file||'').split(/[\\/]/).pop()}</code>{ev.line?`:${ev.line}`:''}</span>:null}
                          </div>
                          {ev.file&&ev.line&&getCodeSnippetJSX(ev.file,ev.line,snipCache,2)}
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function TimingsTab({r}) {
  const timings=r.agent_timings||[]; const totalTokens=r.token_usage||0; const totalTime=r.duration_s||0
  if (!timings.length) return <div className="card"><div className="section-heading"><i className="ti ti-clock"/>Agent timings</div><div className="empty-state"><i className="ti ti-clock-off"/>No timing data — run analysis with a connected backend</div></div>
  const maxTime=Math.max(1,...timings.map(t=>t.duration_s||0)); const maxTokens=Math.max(1,...timings.map(t=>t.tokens||0))
  const AGENT_COLORS={code_analysis:'#1a6cf6',security:'#ef4444',test_coverage:'#10b981',dependency:'#f59e0b',interface:'#8b5cf6',risk:'#0ea5e9',remediation:'#f97316',schema_change:'#06b6d4'}
  const agentColor=name=>AGENT_COLORS[name]||'#7a8494'
  const sName=name=>name.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase())
  return (
    <div className="card">
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:18}}>
        <div className="section-heading" style={{marginBottom:0}}><i className="ti ti-clock-bolt"/>Agent timings &amp; token usage</div>
        <div style={{display:'flex',gap:16,fontSize:12,color:'#7a8494'}}>
          <span>Total: <strong style={{color:'#0d1117',fontFamily:'JetBrains Mono,monospace'}}>{totalTime.toFixed(1)}s</strong></span>
          <span>Tokens: <strong style={{color:'#0d1117',fontFamily:'JetBrains Mono,monospace'}}>{totalTokens.toLocaleString()}</strong></span>
        </div>
      </div>
      <div style={{marginBottom:20}}>
        {timings.map((t,i)=>{
          const timePct=Math.round(((t.duration_s||0)/maxTime)*100),tokenPct=Math.round(((t.tokens||0)/maxTokens)*100),col=agentColor(t.agent)
          return (
            <div key={i} style={{marginBottom:14}}>
              <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:5}}>
                <div style={{display:'flex',alignItems:'center',gap:7}}>
                  <div style={{width:10,height:10,borderRadius:2,background:col,flexShrink:0}}/>
                  <span style={{fontSize:13,fontWeight:500,color:'#0d1117'}}>{sName(t.agent)}</span>
                  {(() => { const eng=agentEngine(t.model,{completed:true}); return eng?<span title={eng.title} style={{fontSize:10,fontWeight:700,background:eng.bg,border:`1px solid ${eng.border}`,borderRadius:10,padding:'1px 8px',color:eng.color}}>{eng.label}</span>:null })()}
                </div>
                <div style={{display:'flex',gap:14,fontSize:12,fontFamily:'JetBrains Mono,monospace'}}>
                  <span><strong>{(t.duration_s||0).toFixed(2)}s</strong></span>
                  <span><strong>{(t.tokens||0).toLocaleString()}</strong> tok</span>
                </div>
              </div>
              <div style={{display:'flex',flexDirection:'column',gap:3}}>
                <div style={{display:'flex',alignItems:'center',gap:8}}><div style={{width:38,fontSize:10,color:'#9fadbf',textAlign:'right',flexShrink:0}}>time</div><div style={{flex:1,height:8,background:'#f0f2f5',borderRadius:4,overflow:'hidden'}}><div style={{height:'100%',width:`${timePct}%`,background:col,borderRadius:4}}/></div></div>
                <div style={{display:'flex',alignItems:'center',gap:8}}><div style={{width:38,fontSize:10,color:'#9fadbf',textAlign:'right',flexShrink:0}}>tokens</div><div style={{flex:1,height:8,background:'#f0f2f5',borderRadius:4,overflow:'hidden'}}><div style={{height:'100%',width:`${tokenPct}%`,background:col,opacity:.45,borderRadius:4}}/></div></div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Reference Graph (layered DAG / force, folder grouping, focus highlight) ─────
// Force layouts turn into an unreadable "hairball" once there are many links.
// Three things fix that, all available here: a deterministic LAYERED layout
// (columns by call-distance, links flow one way), FOLDER GROUPING (collapses
// files in a directory into one module node — the biggest link reduction), and
// FOCUS-ON-HOVER (dim everything except the hovered node's direct connections).
function ReferenceGraph({ ref: refData }) {
  const containerRef = useRef(null)
  const simRef       = useRef(null)
  const totalFiles = new Set((refData?.references || []).map(r => r.file_path)).size
  const [mode, setMode]   = useState('layered')        // 'layered' | 'force'
  const [group, setGroup] = useState(totalFiles > 18)  // auto-group busy graphs

  useEffect(() => {
    const el = containerRef.current
    if (!el || !refData) return
    renderGraph(d3, el, refData, mode, group)
    return () => { if (simRef.current) { simRef.current.stop(); simRef.current = null } }
  }, [refData, mode, group])

  function renderGraph(d3, el, ref, mode, group) {
    el.innerHTML = ''
    if (simRef.current) { simRef.current.stop(); simRef.current = null }

    const W = el.clientWidth || 860
    const H = 540
    const symbols    = (ref.changed_symbols  || []).slice(0, 12)
    const sharedLibs = (ref.shared_lib_breaks || []).slice(0, 6)

    const depthColor = d => (['#3b82f6','#0d9488','#7c3aed','#6b7280'])[Math.min(d,4)-1] || '#6b7280'
    const depthBg    = d => (['#eff6ff','#f0fdfa','#f5f3ff','#f9fafb'])[Math.min(d,4)-1] || '#f9fafb'
    const depthLabel = d => (['Direct callers','Callers of callers','3rd level','4th level+'])[Math.min(d,4)-1] || 'Deep'

    // ── Per-file metadata ──
    const fileMeta = {}
    ;(ref.references || []).forEach(r => {
      const fp = r.file_path
      if (!fileMeta[fp]) fileMeta[fp] = { count:0, depth:r.depth||1, symbols:new Set() }
      fileMeta[fp].count++
      fileMeta[fp].depth = Math.min(fileMeta[fp].depth, r.depth||1)
      fileMeta[fp].symbols.add(r.symbol)
      if (r.from_file && !fileMeta[r.from_file])
        fileMeta[r.from_file] = { count:0, depth:(r.depth||2)-1, symbols:new Set() }
    })

    // Folder grouping → one node per directory (collapses many files → few modules)
    const dirOf = fp => { const p = fp.replace(/\\/g,'/').split('/'); return p.slice(0,-1).join('/') || '(root)' }
    const keyOf = fp => group ? dirOf(fp) : fp
    const aggMeta = {}
    Object.entries(fileMeta).forEach(([fp, m]) => {
      const k = keyOf(fp)
      if (!aggMeta[k]) aggMeta[k] = { count:0, depth:m.depth, files:new Set() }
      aggMeta[k].count += m.count
      aggMeta[k].depth  = Math.min(aggMeta[k].depth, m.depth)
      aggMeta[k].files.add(fp)
    })

    const totalUnits = Object.keys(aggMeta).length
    const MAX_NODES  = group ? 40 : 26
    const topUnits = Object.entries(aggMeta)
      .sort(([,a],[,b]) => b.count - a.count).slice(0, MAX_NODES)
      .map(([k, m]) => {
        const parts = k.replace(/\\/g,'/').split('/')
        return { id:'node:'+k, key:k, label: group ? (parts.slice(-2).join('/')||k) : parts.slice(-1)[0],
                 fullPath:k, count:m.count, depth:m.depth, files:[...m.files],
                 type: group ? 'module' : 'file', r: Math.min(7 + m.count * 1.4, 18) }
      })

    const symNodes = symbols.map(s => ({
      id:'sym:'+s, label:s.length>20?s.slice(0,18)+'…':s, fullLabel:s, type:'symbol', depth:0, r:14,
    }))
    const libNodes = sharedLibs.map(p => {
      const parts = p.replace(/\\/g,'/').split('/')
      return { id:'lib:'+p, label:parts.slice(-2).join('/'), fullPath:p, type:'shared_lib', depth:5, r:11 }
    })

    const nodes   = [...symNodes, ...topUnits, ...libNodes]
    const nodeIds = new Set(nodes.map(n => n.id))

    const linkSet = new Set(); const links = []
    ;(ref.references || []).forEach(r => {
      const depth = r.depth || 1
      const tgt   = 'node:' + keyOf(r.file_path)
      const src   = (depth === 1 || !r.from_file) ? 'sym:' + (r.symbol||'') : 'node:' + keyOf(r.from_file)
      if (src === tgt) return
      const key = src + '→' + tgt
      if (!linkSet.has(key) && nodeIds.has(src) && nodeIds.has(tgt)) {
        linkSet.add(key); links.push({ source:src, target:tgt, depth })
      }
    })
    libNodes.forEach(ln => { if (symNodes.length) links.push({ source:symNodes[0].id, target:ln.id, shared:true }) })

    if (!nodes.length) {
      el.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#7a8494;font-size:13px"><i class="ti ti-topology-star-3" style="margin-right:8px"/>No graph data to display</div>`
      return
    }

    const svg = d3.select(el).append('svg').attr('width','100%').attr('height',H)
      .style('border-radius','8px').style('background','#f7f8fa')
    const defs = svg.append('defs')
    defs.append('marker').attr('id','refArrow').attr('viewBox','0 -4 8 8')
      .attr('refX',18).attr('refY',0).attr('markerWidth',6).attr('markerHeight',6).attr('orient','auto')
      .append('path').attr('d','M0,-4L8,0L0,4').attr('fill','#c9d4e8')
    const zoomLayer = svg.append('g')
    svg.call(d3.zoom().scaleExtent([0.3, 3]).on('zoom', e => zoomLayer.attr('transform', e.transform)))

    const nodeById  = new Map(nodes.map(n => [n.id, n]))
    const nodeColor = d => d.type==='symbol'?'#f97316':d.type==='shared_lib'?'#f59e0b':depthColor(d.depth||1)
    const nodeBg    = d => d.type==='symbol'?'#fff7ed':d.type==='shared_lib'?'#fffbeb':depthBg(d.depth||1)
    const lid       = l => [(l.source.id||l.source),(l.target.id||l.target)]
    let linkEl, nodeEl

    if (mode === 'layered') {
      links.forEach(l => { l.source = nodeById.get(l.source) || l.source; l.target = nodeById.get(l.target) || l.target })
      // Columns: 0 = changed symbols, 1–4 = caller depth, 5 = shared libs.
      const colOf = n => n.type==='symbol'?0 : n.type==='shared_lib'?5 : Math.min(n.depth||1,4)
      const colsPresent = [...new Set(nodes.map(colOf))].sort((a,b)=>a-b)
      const colIndex = new Map(colsPresent.map((c,i)=>[c,i]))
      const nCols = colsPresent.length, padX = 84, padY = 28
      const xFor = c => nCols<=1 ? W/2 : padX + (colIndex.get(c)/(nCols-1))*(W-2*padX)
      const byCol = {}
      nodes.forEach(n => { (byCol[colOf(n)] ||= []).push(n) })
      Object.values(byCol).forEach(arr => arr.sort((a,b)=>(b.count||0)-(a.count||0)))
      nodes.forEach(n => {
        const c = colOf(n), arr = byCol[c], i = arr.indexOf(n), k = arr.length
        n.x = xFor(c); n.y = k===1 ? H/2 : padY + i*(H-2*padY)/(k-1)
      })
      colsPresent.forEach(c => {
        zoomLayer.append('text').attr('x',xFor(c)).attr('y',14).attr('text-anchor','middle')
          .attr('font-size','10px').attr('font-weight','700').attr('fill','#9fadbf')
          .attr('font-family',"'JetBrains Mono', monospace")
          .text(c===0?'Changed':c===5?'Shared libs':depthLabel(c))
      })
      linkEl = zoomLayer.append('g').selectAll('path').data(links).join('path')
        .attr('fill','none').attr('stroke', d=>d.shared?'#f59e0b':depthColor(d.depth||1))
        .attr('stroke-width', d=>d.shared?1.4:1.1).attr('stroke-opacity', d=>d.shared?.7:.4)
        .attr('marker-end','url(#refArrow)')
        .attr('d', d=>{ const sx=d.source.x,sy=d.source.y,tx=d.target.x,ty=d.target.y,mx=(sx+tx)/2; return `M${sx},${sy} C${mx},${sy} ${mx},${ty} ${tx},${ty}` })
      nodeEl = zoomLayer.append('g').selectAll('g').data(nodes).join('g')
        .attr('transform', d=>`translate(${d.x},${d.y})`).style('cursor','pointer')
    } else {
      const maxR = Math.min(W, H) / 2 - 40
      const ringR = d => d.type==='symbol'?0 : d.type==='shared_lib'?maxR : Math.min([0.34,0.60,0.82,0.95][Math.min(d.depth,4)-1]||0.95,1)*maxR
      const sim = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(links).id(d=>d.id).distance(d=>d.shared?120:70).strength(0.12))
        .force('charge', d3.forceManyBody().strength(d=>d.type==='symbol'?-700:-260))
        .force('radial', d3.forceRadial(ringR, W/2, H/2).strength(d=>d.type==='symbol'?1:0.55))
        .force('collision', d3.forceCollide().radius(d=>d.r+12).strength(0.9))
      simRef.current = sim
      linkEl = zoomLayer.append('g').selectAll('line').data(links).join('line')
        .attr('stroke', d=>d.shared?'#f59e0b':depthColor(d.depth||1))
        .attr('stroke-width', d=>d.shared?1.5:d.depth===1?1.5:1)
        .attr('stroke-opacity', d=>d.shared?.8:d.depth===1?.6:.35)
        .attr('marker-end','url(#refArrow)')
      nodeEl = zoomLayer.append('g').selectAll('g').data(nodes).join('g').style('cursor','pointer')
        .call(d3.drag()
          .on('start',(e,d)=>{ if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y })
          .on('drag',(e,d)=>{ d.fx=e.x; d.fy=e.y })
          .on('end',(e,d)=>{ if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null }))
      sim.on('tick', () => {
        linkEl.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y)
        nodeEl.attr('transform',d=>`translate(${d.x},${d.y})`)
      })
    }

    nodeEl.append('circle').attr('r',d=>d.r).attr('fill',d=>nodeBg(d))
      .attr('stroke',d=>nodeColor(d)).attr('stroke-width',d=>d.type==='symbol'?2.5:1.5)
    nodeEl.filter(d=>d.type==='symbol').append('text').attr('text-anchor','middle').attr('dominant-baseline','central').attr('font-size','10px').attr('fill','#f97316').text('ƒ')
    nodeEl.filter(d=>d.type==='module').append('text').attr('text-anchor','middle').attr('dominant-baseline','central').attr('font-size','10px').attr('fill','#3b82f6').text('▣')
    nodeEl.filter(d=>d.type==='file').append('text').attr('text-anchor','middle').attr('dominant-baseline','central').attr('font-size','9px').attr('fill','#3b82f6').text('{ }')
    nodeEl.filter(d=>d.type==='shared_lib').append('text').attr('text-anchor','middle').attr('dominant-baseline','central').attr('font-size','9px').attr('fill','#d97706').text('⚠')

    // Layered columns are well separated, so we can label every node; force mode
    // labels only symbols + the heaviest nodes to avoid clutter.
    const labelCount = Math.max(...topUnits.map(f=>f.count||0), 1)
    const showLabel = d => mode==='layered' || d.type==='symbol' || d.type==='shared_lib' || d.count>=3 || d.count>=labelCount
    nodeEl.filter(showLabel).append('text')
      .attr('dy',d=>d.r+12).attr('text-anchor','middle')
      .attr('font-size',d=>d.type==='symbol'?'11px':'9.5px').attr('font-weight',d=>d.type==='symbol'?'700':'500')
      .attr('fill',d=>nodeColor(d)).attr('font-family',"'JetBrains Mono', monospace")
      .style('paint-order','stroke').attr('stroke','#f7f8fa').attr('stroke-width','3px')
      .text(d=> d.label || (d.fullPath||'').split('/').pop())
    nodeEl.filter(d=>(d.type==='file'||d.type==='module')&&d.count>1).append('text')
      .attr('dy',d=>-d.r-3).attr('text-anchor','middle').attr('font-size','9px').attr('fill','#6b7280').text(d=>`×${d.count}`)

    const tooltip = d3.select(el).append('div')
      .style('position','absolute').style('pointer-events','none').style('background','#1c2333').style('color','#e8f0ff')
      .style('padding','8px 12px').style('border-radius','6px').style('font-size','12px').style('max-width','300px')
      .style('line-height','1.5').style('opacity','0').style('transition','opacity .15s').style('z-index','100')
      .style('font-family',"'JetBrains Mono', monospace")

    const restoreLinks = () => linkEl.style('stroke-opacity', d=>d.shared?(mode==='layered'?.7:.8):(mode==='layered'?.4:(d.depth===1?.6:.35)))
    nodeEl
      .on('mouseenter', (event,d) => {
        // Focus: keep the hovered node + its direct neighbours, fade the rest.
        const keep = new Set([d.id])
        links.forEach(l => { const [s,t]=lid(l); if(s===d.id) keep.add(t); if(t===d.id) keep.add(s) })
        nodeEl.style('opacity', n=> keep.has(n.id)?1:0.12)
        linkEl.style('stroke-opacity', l=>{ const [s,t]=lid(l); return (s===d.id||t===d.id)?0.95:0.04 })
        let html = `<div style="font-weight:700;color:${nodeColor(d)}">${d.fullLabel||d.fullPath||d.label}</div>`
        if (d.type==='module')          html += `<div style="color:#9fadbf;font-size:11px">${d.files.length} file(s) · ${d.count} reference(s)</div><div style="color:${depthColor(d.depth||1)};font-size:11px;margin-top:2px">${depthLabel(d.depth||1)}</div>`
        else if (d.type==='file')       html += `<div style="color:#9fadbf;font-size:11px">${d.count} reference${d.count!==1?'s':''}</div><div style="color:${depthColor(d.depth||1)};font-size:11px;margin-top:2px">${depthLabel(d.depth||1)}</div>`
        else if (d.type==='shared_lib') html += `<div style="color:#f59e0b;font-size:11px">⚠ Shared library — cross-project risk</div>`
        else                            html += `<div style="color:#9fadbf;font-size:11px">Changed symbol</div>`
        tooltip.html(html).style('opacity','1').style('left',(event.offsetX+14)+'px').style('top',(event.offsetY-10)+'px')
      })
      .on('mousemove', event => tooltip.style('left',(event.offsetX+14)+'px').style('top',(event.offsetY-10)+'px'))
      .on('mouseleave', () => { tooltip.style('opacity','0'); nodeEl.style('opacity',1); restoreLinks() })

    if (totalUnits > topUnits.length) {
      svg.append('text').attr('x',12).attr('y',H-14).attr('font-size','11px').attr('fill','#9fadbf')
        .attr('font-family',"'JetBrains Mono', monospace")
        .text(`+${totalUnits - topUnits.length} more ${group?'folders':'files'} — see the list below`)
    }
    svg.append('g').attr('transform',`translate(${W-58},${H-36})`).append('foreignObject').attr('width',52).attr('height',26)
      .append('xhtml:button').style('font-size','10px').style('padding','2px 8px').style('background','#fff')
      .style('border','1px solid #e8eaed').style('border-radius','5px').style('cursor','pointer').style('color','#4b5563')
      .text('Reset view').on('click', () => svg.transition().duration(300).call(d3.zoom().transform, d3.zoomIdentity))
  }

  return (
    <div>
      <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:6,flexWrap:'wrap'}}>
        <div style={{display:'inline-flex',border:'1px solid #e8eaed',borderRadius:7,overflow:'hidden'}}>
          {[['layered','⬚ Layered'],['force','✺ Force']].map(([m,lbl])=>(
            <button key={m} onClick={()=>setMode(m)} style={{padding:'4px 11px',fontSize:11,fontWeight:600,border:'none',cursor:'pointer',background:mode===m?'#1a6cf6':'#fff',color:mode===m?'#fff':'#4b5563'}}>{lbl}</button>
          ))}
        </div>
        <label style={{fontSize:11,color:'#4b5563',display:'inline-flex',alignItems:'center',gap:5,cursor:'pointer'}}>
          <input type="checkbox" checked={group} onChange={e=>setGroup(e.target.checked)} style={{cursor:'pointer'}}/> Group files by folder
        </label>
        <span style={{fontSize:11,color:'#9fadbf'}}><i className="ti ti-info-circle" style={{marginRight:3}}/>Hover a node to isolate its links · {mode==='layered'?'columns = call distance':'rings = call distance'} · scroll to zoom{mode==='force'?' · drag to rearrange':''}.</span>
      </div>
      <div ref={containerRef} style={{ position:'relative', width:'100%', height:540, borderRadius:8, overflow:'hidden', border:'1px solid #e8eaed', background:'#f7f8fa' }}>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'center', height:'100%', color:'#9fadbf', fontSize:12 }}>
          <span className="spinner" style={{width:16,height:16,marginRight:8}}/> Rendering graph…
        </div>
      </div>
    </div>
  )
}

// ── References Tab — full rebuild ───────────────────────────────────────────────
function ReferencesTab({r}) {
  const ref = r.reference_impact
  const [search,    setSearch]    = useState('')
  const [sortBy,    setSortBy]    = useState('depth')    // depth | file | symbol
  const [filterDepth, setFDepth]  = useState(0)          // 0 = all
  const [page,      setPage]      = useState(0)
  const PAGE_SIZE = 50

  if (!ref) return (
    <div className="card">
      <div className="empty-state">
        <i className="ti ti-git-branch"/>
        No reference impact data.<br/>
        <span style={{fontSize:12,color:'#9fadbf'}}>Configure REPO_LOCAL_PATH or GITHUB_TOKEN to enable cross-file reference search.</span>
      </div>
    </div>
  )

  const riskColor = v => ({CRITICAL:'#b81c1c',HIGH:'#8a5200',MEDIUM:'#1a6cf6',LOW:'#6b7280'})[v?.toUpperCase()]||'#6b7280'
  const risk      = (ref.intra_project_risk||'LOW').toUpperCase()

  const depthColor = d => (['#3b82f6','#0d9488','#7c3aed','#6b7280'])[Math.min(d,4)-1]||'#6b7280'
  const depthLabel = d => (['L1 — Direct','L2 — Callers of callers','L3 — 3rd level','L4+'])[Math.min(d,4)-1]||'L?'

  const backendLabels = {
    local_grep:'local grep', auto_clone:'auto clone',
    github_api:'GitHub API', diff_scan:'PR diff scan', none:'none',
  }

  // ── Filter + sort the references list ──────────────────────────────────────
  const allRefs    = ref.references || []
  const q          = search.trim().toLowerCase()
  const filtered   = allRefs.filter(r2 => {
    if (filterDepth && (r2.depth||1) !== filterDepth) return false
    if (!q) return true
    return (r2.file_path||'').toLowerCase().includes(q) ||
           (r2.symbol||'').toLowerCase().includes(q)    ||
           (r2.context||'').toLowerCase().includes(q)   ||
           (r2.repo||'').toLowerCase().includes(q)
  })
  const sorted = [...filtered].sort((a,b) => {
    if (sortBy==='depth')  return (a.depth||1)-(b.depth||1)
    if (sortBy==='file')   return (a.file_path||'').localeCompare(b.file_path||'')
    if (sortBy==='symbol') return (a.symbol||'').localeCompare(b.symbol||'')
    return 0
  })
  const paged = sorted.slice(page * PAGE_SIZE, (page+1) * PAGE_SIZE)
  const totalPages = Math.ceil(sorted.length / PAGE_SIZE)

  // Depth distribution for filter pills
  const depthCounts = {}
  allRefs.forEach(r2 => { const d=r2.depth||1; depthCounts[d]=(depthCounts[d]||0)+1 })
  const depths = Object.keys(depthCounts).map(Number).sort()

  return (
    <div>
      {/* ── Summary card ── */}
      <div className="card" style={{marginBottom:12}}>
        <div style={{display:'flex',alignItems:'flex-start',justifyContent:'space-between',flexWrap:'wrap',gap:10,marginBottom:14}}>
          <div>
            <div className="card-title" style={{marginBottom:4}}><i className="ti ti-git-branch"/>Reference Impact Analysis</div>
            <div style={{fontSize:12,color:'#7a8494',maxWidth:560}}>{ref.summary||''}</div>
          </div>
          <div style={{display:'flex',gap:12,flexWrap:'wrap',alignItems:'center',fontSize:12}}>
            <span style={{color:riskColor(risk),fontWeight:700,fontSize:13}}>{risk} intra-project risk</span>
            <span className={`badge badge-${(ref.total_references||0)>50?'red':(ref.total_references||0)>10?'amber':'dim'}`}>
              {ref.total_references||0} references
            </span>
            {ref.search_backend && (
              <span style={{background:ref.search_backend==='diff_scan'?'#fff7ed':'#f0f2f5',border:`1px solid ${ref.search_backend==='diff_scan'?'#fed7aa':'#e8eaed'}`,borderRadius:4,padding:'2px 8px',fontSize:10,fontFamily:'var(--mono)',color:'#7a8494'}}>
                via {backendLabels[ref.search_backend]||ref.search_backend}
                {ref.search_backend==='diff_scan'&&<span style={{color:'#f59e0b',marginLeft:4}}>⚠ set REPO_LOCAL_PATH for full search</span>}
              </span>
            )}
          </div>
        </div>

        {/* Metrics row */}
        <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fill,minmax(110px,1fr))',gap:8,marginBottom:14}}>
          {[
            ['Total refs',     ref.total_references||0,          '#1a6cf6'],
            ['Files affected', Object.keys(
              (ref.references||[]).reduce((acc,r2)=>{acc[r2.file_path]=1;return acc},{})).length, '#7c3aed'],
            ['Changed symbols',(ref.changed_symbols||[]).length,  '#f97316'],
            ['Shared-lib risks',(ref.shared_lib_breaks||[]).length,'#f59e0b'],
          ].map(([l,v,c])=>(
            <div key={l} style={{background:'#f7f8fa',border:'1px solid #e8eaed',borderRadius:8,padding:'8px 12px',textAlign:'center'}}>
              <div style={{fontSize:18,fontWeight:700,fontFamily:'var(--mono)',color:c}}>{v}</div>
              <div style={{fontSize:10,color:'#9fadbf',marginTop:2}}>{l}</div>
            </div>
          ))}
        </div>

        {/* Changed symbols */}
        {(ref.changed_symbols||[]).length>0&&(
          <div style={{marginBottom:14}}>
            <div style={{fontSize:11,fontWeight:600,textTransform:'uppercase',letterSpacing:.06,color:'#9fadbf',marginBottom:6}}>
              Changed symbols ({ref.changed_symbols.length})
            </div>
            <div style={{display:'flex',flexWrap:'wrap',gap:4}}>
              {ref.changed_symbols.slice(0,40).map(s=>(
                <code key={s} style={{background:'#fff7ed',border:'1px solid #fed7aa',padding:'2px 7px',borderRadius:4,fontSize:11,color:'#c2410c'}}>{s}</code>
              ))}
              {ref.changed_symbols.length>40&&<span style={{fontSize:11,color:'#9fadbf',alignSelf:'center'}}>+{ref.changed_symbols.length-40} more</span>}
            </div>
          </div>
        )}

        {/* Shared lib breaks */}
        {(ref.shared_lib_breaks||[]).length>0&&(
          <div style={{background:'#fff7ed',border:'1px solid #fed7aa',borderRadius:8,padding:'10px 14px',marginBottom:14}}>
            <div style={{fontSize:11,fontWeight:700,textTransform:'uppercase',letterSpacing:.06,color:'#e05e00',marginBottom:6}}>
              ⚠ Shared-library paths modified — cross-project risk
            </div>
            <div style={{display:'flex',flexWrap:'wrap',gap:4}}>
              {ref.shared_lib_breaks.map((p,i)=>(
                <code key={i} style={{background:'#fff3e0',padding:'2px 7px',borderRadius:4,fontSize:11,color:'#8a5200'}}>{p}</code>
              ))}
            </div>
          </div>
        )}

        {/* Call graph legend */}
        <div style={{display:'flex',gap:14,fontSize:11,color:'#7a8494',flexWrap:'wrap',alignItems:'center',paddingTop:4}}>
          <span style={{fontWeight:600,color:'#4b5563',fontSize:11}}>Call graph legend:</span>
          {[
            ['#fff7ed','#f97316','Changed symbol'],
            ['#eff6ff','#3b82f6','Direct callers'],
            ['#f0fdfa','#0d9488','Callers of callers'],
            ['#f5f3ff','#7c3aed','3rd level'],
            ...(ref.shared_lib_breaks?.length?[['#fffbeb','#f59e0b','Shared-lib']]:[]),
          ].map(([bg,border,label])=>(
            <span key={label} style={{display:'flex',alignItems:'center',gap:4}}>
              <span style={{display:'inline-block',width:10,height:10,borderRadius:'50%',background:bg,border:`2px solid ${border}`,flexShrink:0}}/>
              {label}
            </span>
          ))}
          <span style={{color:'#9fadbf',marginLeft:'auto',fontSize:10}}>Drag nodes · Scroll to zoom · Click Reset view</span>
        </div>
      </div>

      {/* ── D3 Call graph ── */}
      <div className="card" style={{marginBottom:12,padding:'14px 16px'}}>
        <div className="card-title" style={{marginBottom:10}}><i className="ti ti-topology-star-3"/>Call graph</div>
        <ReferenceGraph ref={ref}/>
      </div>

      {/* ── High-impact files ── */}
      {(ref.high_impact_files||[]).length>0&&(
        <div className="card" style={{marginBottom:12}}>
          <div className="card-title" style={{marginBottom:10}}><i className="ti ti-flame"/>High-Impact Files <span style={{fontSize:11,fontWeight:400,color:'#9fadbf'}}>(≥3 references)</span></div>
          {(ref.high_impact_files||[]).map((f,i)=>(
            <div key={i} style={{display:'flex',alignItems:'center',gap:8,padding:'6px 0',borderBottom:'1px solid #f0f2f5'}}>
              <i className="ti ti-file-code" style={{color:'#1a6cf6',fontSize:13,flexShrink:0}}/>
              <code style={{fontSize:12,wordBreak:'break-all'}}>{f}</code>
            </div>
          ))}
        </div>
      )}

      {/* ── All references table ── */}
      <div className="card">
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:8,marginBottom:12}}>
          <div className="card-title" style={{marginBottom:0}}>
            <i className="ti ti-list-search"/>
            All References
            <span style={{fontWeight:400,color:'#9fadbf',marginLeft:6,fontSize:11}}>
              {sorted.length < allRefs.length ? `${sorted.length} filtered / ${allRefs.length} total` : `${allRefs.length} total`}
            </span>
          </div>
          <div style={{display:'flex',gap:6,alignItems:'center',flexWrap:'wrap'}}>
            {/* Depth filter pills */}
            {depths.length>1&&(
              <div style={{display:'flex',gap:3}}>
                <button onClick={()=>{setFDepth(0);setPage(0)}} style={{border:'none',background:filterDepth===0?'#1a6cf6':'#f0f2f5',color:filterDepth===0?'#fff':'#7a8494',borderRadius:4,padding:'2px 7px',fontSize:10,cursor:'pointer',fontWeight:600}}>All</button>
                {depths.map(d=>(
                  <button key={d} onClick={()=>{setFDepth(d);setPage(0)}} style={{border:'none',background:filterDepth===d?depthColor(d):'#f0f2f5',color:filterDepth===d?'#fff':'#7a8494',borderRadius:4,padding:'2px 7px',fontSize:10,cursor:'pointer',fontWeight:600}}>
                    L{d} <span style={{opacity:.7}}>({depthCounts[d]})</span>
                  </button>
                ))}
              </div>
            )}
            {/* Sort */}
            <select value={sortBy} onChange={e=>{setSortBy(e.target.value);setPage(0)}}
              style={{fontSize:11,padding:'3px 7px',borderRadius:4,border:'1px solid #e8eaed'}}>
              <option value="depth">Sort: depth</option>
              <option value="file">Sort: file</option>
              <option value="symbol">Sort: symbol</option>
            </select>
          </div>
        </div>

        {/* Search */}
        <div className="search-wrap" style={{marginBottom:10}}>
          <i className="ti ti-search"/>
          <input type="text" value={search} onChange={e=>{setSearch(e.target.value);setPage(0)}} placeholder="Search file, symbol, context, repo…"/>
          {search&&<button onClick={()=>{setSearch('');setPage(0)}} style={{position:'absolute',right:10,top:'50%',transform:'translateY(-50%)',background:'none',border:'none',cursor:'pointer',color:'#7a8494',fontSize:14}}>✕</button>}
        </div>

        {/* Table header */}
        <div style={{display:'grid',gridTemplateColumns:'52px 1fr 1fr 2fr auto',gap:'0 10px',padding:'4px 0 6px',borderBottom:'2px solid #e8eaed',fontSize:10,fontWeight:700,textTransform:'uppercase',letterSpacing:.08,color:'#9fadbf'}}>
          <span>Depth</span><span>File</span><span>Symbol</span><span>Context</span><span>Repo</span>
        </div>

        {/* Rows */}
        <div style={{maxHeight:480,overflowY:'auto'}}>
          {paged.length===0 ? (
            <div className="empty-state" style={{padding:'24px 0'}}><i className="ti ti-search"/>No matching references</div>
          ) : paged.map((r2,i)=>{
            const d    = r2.depth||1
            const dc   = depthColor(d)
            const dl   = `L${Math.min(d,4)}${d>4?'+':''}`
            const fp   = r2.file_path||''
            const parts= fp.replace(/\\/g,'/').split('/')
            const fname= parts.slice(-2).join('/')
            const fdir = parts.slice(0,-2).join('/')
            return (
              <div key={i} style={{display:'grid',gridTemplateColumns:'52px 1fr 1fr 2fr auto',gap:'0 10px',alignItems:'center',padding:'7px 0',borderBottom:'1px solid #f0f2f5',fontSize:12}}>
                {/* Depth badge */}
                <span style={{flexShrink:0,padding:'2px 6px',borderRadius:4,fontSize:10,fontWeight:700,background:`${dc}18`,color:dc,border:`1px solid ${dc}40`,textAlign:'center',whiteSpace:'nowrap'}}>
                  {dl}
                </span>
                {/* File path — short name + dir tooltip */}
                <div style={{minWidth:0}} title={fp}>
                  <div style={{fontFamily:'var(--mono)',fontSize:11,color:'#1a6cf6',whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis',fontWeight:500}}>
                    {fname}{r2.line?<span style={{color:'#9fadbf'}}>:{r2.line}</span>:null}
                  </div>
                  {fdir&&<div style={{fontSize:9,color:'#9fadbf',whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis'}}>{fdir}</div>}
                </div>
                {/* Symbol */}
                <code style={{color:'#c2410c',fontSize:11,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',display:'block'}} title={r2.symbol||''}>
                  {r2.symbol||''}
                </code>
                {/* Context */}
                <span style={{color:'#4b5563',fontSize:11,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',display:'block'}} title={r2.context||''}>
                  {r2.context||''}
                </span>
                {/* Repo */}
                {r2.repo ? (
                  <span style={{fontSize:10,color:'#9fadbf',whiteSpace:'nowrap',textAlign:'right'}}>{r2.repo}</span>
                ) : <span/>}
              </div>
            )
          })}
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',paddingTop:10,borderTop:'1px solid #e8eaed',flexWrap:'wrap',gap:8}}>
            <span style={{fontSize:11,color:'#9fadbf'}}>
              Showing {page*PAGE_SIZE+1}–{Math.min((page+1)*PAGE_SIZE, sorted.length)} of {sorted.length}
              {sorted.length<allRefs.length&&` (${allRefs.length} total)`}
            </span>
            <div style={{display:'flex',gap:4}}>
              <button className="btn btn-sm" disabled={page===0} onClick={()=>setPage(p=>p-1)}>
                <i className="ti ti-chevron-left"/>Prev
              </button>
              {Array.from({length:Math.min(totalPages,7)},(_,i)=>{
                const p = totalPages<=7?i:page<4?i:page>totalPages-4?totalPages-7+i:page-3+i
                return (
                  <button key={p} className={`btn btn-sm${page===p?' btn-primary':''}`} onClick={()=>setPage(p)}>
                    {p+1}
                  </button>
                )
              })}
              <button className="btn btn-sm" disabled={page===totalPages-1} onClick={()=>setPage(p=>p+1)}>
                Next<i className="ti ti-chevron-right"/>
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ── PR Description modal ────────────────────────────────────────────────────────

function buildLocalPRDescription(r) {
  const risk=(r.risk?.overall_risk||'unknown').toUpperCase(), gate=r.gate_decision||'HOLD', gateIcon={APPROVE:'✅',HOLD:'⚠️',BLOCK:'🚫'}[gate]||'❓'
  const lines=['## Summary','',r.code_analysis?.summary||'This PR introduces changes to the codebase.','',`**Risk Level:** \`${risk}\` ${gateIcon}  |  **Gate:** \`${gate}\``,'',' ## Changes','']
  if(r.code_analysis?.change_type)lines.push(`- **Type:** ${r.code_analysis.change_type}`)
  const delta=r.code_analysis?.complexity_delta||0
  if(delta)lines.push(`- **Complexity:** ${delta>0?'increased':'decreased'} by ${Math.abs(delta)}`)
  lines.push('')
  const impacts=[]
  if(r.reference_impact?.total_references>0)impacts.push(`- **Callers affected:** ${r.reference_impact.total_references}`)
  if((r.interface?.breaking_changes||[]).length)impacts.push(`- **Breaking API changes:** ${r.interface.breaking_changes.length}`)
  if(impacts.length){lines.push('## Impact','', ...impacts,'')}
  lines.push('## Testing','')
  if((r.qa_scenarios?.scenarios||[]).length)(r.qa_scenarios.scenarios||[]).slice(0,5).forEach(s=>lines.push(`- [ ] ${s.description||s}`))
  else lines.push('- [ ] Unit tests pass','- [ ] Integration tests pass','- [ ] Smoke test completed')
  lines.push('','---',`*Generated by CIAA · Analysis ID: \`${r.request_id||''}\`*`)
  return lines.join('\n')
}

function PRDescModal({r, state, onClose}) {
  const [text, setText] = useState('')
  useEffect(()=>{
    async function load() {
      setText('Generating…')
      if (state.backendUrl && state.lastRequestId) {
        try {
          const h={}; if(state.backendKey)h['X-API-Key']=state.backendKey
          const resp=await fetch(`${state.backendUrl}/api/v1/report/${state.lastRequestId}/pr-description`,{headers:h})
          if(resp.ok){setText((await resp.json()).markdown||'');return}
        } catch(_){}
      }
      setText(buildLocalPRDescription(r))
    }
    load()
  },[])
  return (
    <div style={{display:'flex',position:'fixed',inset:0,background:'rgba(0,0,0,.45)',zIndex:9999,alignItems:'center',justifyContent:'center'}}>
      <div style={{background:'#fff',borderRadius:14,width:'min(720px,95vw)',maxHeight:'80vh',display:'flex',flexDirection:'column',boxShadow:'0 20px 60px rgba(0,0,0,.25)'}}>
        <div style={{padding:'18px 22px',borderBottom:'1px solid #f0f2f5',display:'flex',alignItems:'center',gap:10}}>
          <span style={{fontSize:18}}>📋</span><strong style={{fontSize:15}}>PR Description</strong>
          <span style={{fontSize:11,color:'#7a8494',marginLeft:4}}>Copy and paste into your PR</span>
          <button onClick={onClose} style={{marginLeft:'auto',border:'none',background:'none',fontSize:20,cursor:'pointer',color:'#7a8494'}}>✕</button>
        </div>
        <div style={{flex:1,overflowY:'auto',padding:'18px 22px'}}>
          <textarea value={text} readOnly style={{width:'100%',height:400,fontFamily:'monospace',fontSize:12,border:'1px solid #e8eaed',borderRadius:8,padding:12,resize:'vertical',lineHeight:1.6}}/>
        </div>
        <div style={{padding:'14px 22px',borderTop:'1px solid #f0f2f5',display:'flex',gap:10}}>
          <button className="btn btn-primary" onClick={()=>{navigator.clipboard.writeText(text).then(()=>alert('Copied!'))}}>
            <i className="ti ti-copy"/> Copy to clipboard
          </button>
          <button className="btn" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  )
}

// ── Similar PRs ────────────────────────────────────────────────────────────────

function SimilarPRs() {
  const { state } = useApp()
  const [loading, setLoading] = useState(false)
  const [results, setResults] = useState(null)
  const [error, setError] = useState('')

  async function load() {
    if (!state.backendUrl || !state.lastRequestId) {
      setError('Backend URL and a saved analysis required.'); return
    }
    setLoading(true); setError(''); setResults(null)
    try {
      const h = {'Content-Type':'application/json'}
      if (state.backendKey) h['X-API-Key'] = state.backendKey
      const r = await fetch(`${state.backendUrl}/api/v1/insights/similar/${state.lastRequestId}?top_k=5`, {headers:h})
      if (!r.ok) throw new Error('HTTP '+r.status)
      const d = await r.json()
      setResults(d.similar||[])
    } catch(e) { setError(e.message) } finally { setLoading(false) }
  }

  const gateBadge = g => {
    const m={BLOCK:['#fff1f2','#991b1b','🚫'],HOLD:['#fffbeb','#92400e','⚠️'],APPROVE:['#f0fdf4','#166534','✅']}[g]||['#f7f8fa','#7a8494','•']
    return <span style={{background:m[0],color:m[1],borderRadius:4,padding:'1px 6px',fontSize:10,fontWeight:700}}>{m[2]} {g}</span>
  }

  return (
    <div className="card">
      <div style={{display:'flex',gap:8,flexWrap:'wrap',marginBottom:results||error?10:0}}>
        <button className="btn" id="btn-similar-dev" onClick={load} disabled={loading}>
          {loading?<><span className="spinner" style={{width:12,height:12}}/>Searching…</>:<><i className="ti ti-git-compare"/> Find similar past PRs</>}
        </button>
      </div>
      {error&&<div style={{fontSize:12,color:'#dc2626'}}>{error}</div>}
      {results!==null&&!results.length&&<div style={{fontSize:13,color:'#7a8494'}}>No similar past PRs found. This change pattern looks new.</div>}
      {results?.length>0&&(
        <div style={{display:'flex',flexDirection:'column',gap:8,marginTop:8}}>
          {results.map(s=>(
            <div key={s.request_id} style={{border:'1px solid #e8eaed',borderRadius:8,padding:'10px 12px',cursor:'pointer'}}
              onMouseOver={e=>e.currentTarget.style.borderColor='#1a6cf6'} onMouseOut={e=>e.currentTarget.style.borderColor='#e8eaed'}>
              <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:3}}>
                <span style={{background:'#eff5ff',color:'#1a56db',borderRadius:4,padding:'1px 7px',fontSize:10,fontWeight:700}}>{(s.similarity*100).toFixed(0)}% match</span>
                {gateBadge(s.gate)}
                <span style={{fontWeight:600,fontSize:12,color:'#0d1117'}}>{s.pr_title||s.source_ref||'(untitled)'}</span>
                <span style={{fontSize:10,color:'#9fadbf',marginLeft:'auto'}}>{s.elapsed}</span>
              </div>
              <div style={{fontSize:11,color:'#7a8494'}}>{s.repo} · risk {s.risk_score?.toFixed(1)}{s.shared_files?.length?` · shares ${s.shared_files.length} file(s): ${s.shared_files.slice(0,2).join(', ')}`:''}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Dependency Auto-Update ──────────────────────────────────────────────────────

function DepAutoUpdate() {
  const { state } = useApp()
  const [loading, setLoading] = useState(false)
  const [results, setResults] = useState(null)
  const [error, setError] = useState('')

  async function run() {
    if (!state.backendUrl || !state.lastRequestId) { setError('Backend URL and a saved analysis required.'); return }
    if (!canPostToGit(state)) { setError('Creating PRs requires Reviewer role.'); return }
    setLoading(true); setError(''); setResults(null)
    try {
      const h = {'Content-Type':'application/json'}
      if (state.backendKey) h['X-API-Key'] = state.backendKey
      const body = { request_id:state.lastRequestId, provider:state.provider, token:state.token, base_url:state.baseUrl||'', workspace:state.workspace||'', repo_slug:repoName(state.primaryRepo), target_ref:state.targetBranch||'main' }
      const r = await fetch(`${state.backendUrl}/api/v1/insights/dep-update`, {method:'POST', headers:h, body:JSON.stringify(body)})
      if (r.status===403) { setError('Reviewer role required to create PRs.'); return }
      const d = await r.json()
      setResults(d)
    } catch(e) { setError(e.message) } finally { setLoading(false) }
  }

  if (!state.backendUrl) return null
  return (
    <div className="card">
      <div className="section-heading"><i className="ti ti-git-pull-request"/>Dependency Auto-Fix</div>
      <div style={{fontSize:12,color:'#7a8494',marginBottom:10}}>Scan for known CVEs via OSV.dev and auto-create fix PRs for vulnerable packages.</div>
      <button className="btn" id="btn-dep-update" onClick={run} disabled={loading}>
        {loading?<><span className="spinner" style={{width:12,height:12}}/>Creating fix PRs…</>:<><i className="ti ti-git-pull-request"/> Create fix PRs</>}
      </button>
      {error&&<div style={{fontSize:12,color:'#dc2626',marginTop:8}}>{error}</div>}
      {results&&(
        <div style={{marginTop:10}}>
          {!(results.prs_created||[]).length
            ? <div style={{fontSize:12,color:'#7a8494'}}>{results.message||'No vulnerable dependencies found.'}</div>
            : <div style={{display:'flex',flexDirection:'column',gap:6}}>
                {(results.prs_created||[]).map((p,i)=>{
                  const ic=p.status==='created'?'✅':p.status==='skipped'?'⬜':'❌'
                  return (
                    <div key={i} style={{fontSize:12,padding:'6px 10px',background:'#f7f8fa',borderRadius:6}}>
                      {ic} <code>{p.package}</code>{p.safe_version?<> → <strong>{p.safe_version}</strong></>:null} <span style={{color:'#9fadbf'}}>{p.cve||''}</span>
                      {p.pr_url&&<> <a href={p.pr_url} target="_blank" rel="noreferrer" style={{color:'#1a56db'}}>PR #{p.pr_number} →</a></>}
                      {p.reason&&<span style={{color:'#9fadbf'}}> — {p.reason}</span>}
                    </div>
                  )
                })}
              </div>
          }
        </div>
      )}
    </div>
  )
}

// ── Judge Panel ─────────────────────────────────────────────────────────────────

function JudgePanel({ state, showToast }) {
  const { update } = useApp()
  const [loading, setLoading] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [showCfg, setShowCfg] = useState(false)

  const judges = state.judges && state.judges.length ? state.judges : []
  const nJudges = judges.length || 3

  function setJudges(next) { update({ judges: next }) }
  function updateJudge(i, patch) {
    setJudges(judges.map((j, idx) => idx === i ? { ...j, ...patch } : j))
  }
  function addJudge() {
    setJudges([...judges, { provider: state.modelProvider || 'anthropic', model: state.modelName || '' }])
  }
  function removeJudge(i) { setJudges(judges.filter((_, idx) => idx !== i)) }
  function useMyModel() {
    setJudges(judges.map(j => ({ provider: state.modelProvider, model: state.modelName })))
    showToast?.('Judges set to your analysis model', 'success')
  }

  async function run() {
    if (!state.backendUrl || !state.lastRequestId) {
      setError('Judge panel requires a connected backend. Run analysis via backend first.'); return
    }
    setLoading(true); setError(''); setData(null)
    try {
      const h = {'Content-Type':'application/json'}
      if (state.backendKey) h['X-API-Key'] = state.backendKey
      // Attach the key/base-url from the main model config when the judge uses
      // the same provider; otherwise leave blank so the backend falls back to env.
      // Key precedence per judge:
      //   1. explicit per-judge key entered in the editor
      //   2. if the judge uses the SAME provider as your analysis model → reuse that key
      //   3. otherwise blank → backend falls back to its env key for that provider
      const judgePayload = judges.filter(j => j.model && j.model.trim()).map(j => ({
        provider: j.provider,
        model: j.model,
        api_key: (j.api_key && j.api_key.trim()) ? j.api_key.trim()
                 : (j.provider === state.modelProvider ? (state.modelApiKey || '') : ''),
        base_url: (j.base_url && j.base_url.trim()) ? j.base_url.trim()
                 : (j.provider === state.modelProvider ? (state.modelBaseUrl || '') : ''),
      }))
      const resp = await fetch(state.backendUrl+'/api/v1/evaluate/'+state.lastRequestId, {
        method:'POST', headers:h,
        body:JSON.stringify({ diff_text:state.diffText||'', async_mode:false, judges: judgePayload })
      })
      if (!resp.ok) throw new Error('HTTP '+resp.status)
      setData(await resp.json())
    } catch(e) { setError(e.message) } finally { setLoading(false) }
  }

  const verdictColor  = {PASS:'#0c7c4b', PARTIAL:'#8a5200', FAIL:'#b81c1c'}
  const verdictBg     = {PASS:'#edfaf3', PARTIAL:'#fff8ec', FAIL:'#fff1f1'}
  const verdictBorder = {PASS:'#b5e8cf', PARTIAL:'#fad98a', FAIL:'#f8c0c0'}
  const confColor     = {HIGH:'#0c7c4b', MEDIUM:'#8a5200', LOW:'#b81c1c'}

  return (
    <div className="card" style={{marginTop:14}}>
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:14,flexWrap:'wrap',gap:8}}>
        <div className="card-title" style={{marginBottom:0}}><i className="ti ti-gavel"/>LLM Judge Panel</div>
        {data&&(
          <div style={{display:'flex',alignItems:'center',gap:8}}>
            <span style={{fontSize:11,color:'#7a8494'}}>{data.panel_size||3} judges · {(data.agent_verdicts||[]).length} agents</span>
            <span style={{fontSize:13,fontWeight:700,fontFamily:'var(--mono)'}}>{(data.overall_score||0).toFixed(2)}<span style={{fontSize:10,fontWeight:400,color:'#9fadbf'}}>/5</span></span>
            <span style={{fontSize:12,fontWeight:600,padding:'3px 10px',borderRadius:12,background:verdictBg[data.overall_verdict]||'#f7f8fa',color:verdictColor[data.overall_verdict]||'#7a8494',border:`1px solid ${verdictBorder[data.overall_verdict]||'#e8eaed'}`}}>{data.overall_verdict||'—'}</span>
          </div>
        )}
        {!data&&(
          <div style={{display:'flex',alignItems:'center',gap:8}}>
            <button className="btn btn-sm" onClick={()=>setShowCfg(v=>!v)} title="Choose judge models">
              <i className="ti ti-settings"/> Judges ({nJudges})
            </button>
            <button className="btn" onClick={run} disabled={loading}>
              {loading?<><span className="spinner" style={{width:12,height:12}}/>Running {nJudges} judges…</>:<><i className="ti ti-player-play"/> Run judges</>}
            </button>
          </div>
        )}
      </div>

      {/* Judge model configuration */}
      {showCfg&&!data&&(
        <div style={{border:'1px solid #e8eaed',borderRadius:9,padding:14,marginBottom:12,background:'#fafbfc'}}>
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:10,flexWrap:'wrap',gap:8}}>
            <span style={{fontSize:12,fontWeight:700,color:'#0d1117'}}>Judge panel models</span>
            <div style={{display:'flex',gap:6}}>
              <button className="btn btn-sm" onClick={useMyModel} title="Set all judges to your analysis model">
                <i className="ti ti-copy"/> Use my model
              </button>
              <button className="btn btn-sm" onClick={addJudge}><i className="ti ti-plus"/> Add judge</button>
            </div>
          </div>
          <div style={{display:'flex',flexDirection:'column',gap:10}}>
            {judges.map((j,i)=>{
              const prov = MODEL_PROVIDERS[j.provider] || MODEL_PROVIDERS.anthropic
              const sameAsMain = j.provider === state.modelProvider
              // Show a key field when the provider needs a key and it won't be
              // auto-supplied from your analysis model (different provider).
              const showKey = prov.needsKey && !(sameAsMain && state.modelApiKey)
              const showUrl = prov.needsUrl
              return (
                <div key={i} style={{display:'flex',flexDirection:'column',gap:5}}>
                  <div style={{display:'flex',alignItems:'center',gap:7}}>
                    <span style={{fontSize:11,color:'#9fadbf',width:54,flexShrink:0}}>Judge {i+1}</span>
                    <select value={j.provider} onChange={e=>updateJudge(i,{provider:e.target.value,model:(MODEL_PROVIDERS[e.target.value]?.models[0]||''),api_key:'',base_url:''})}
                      style={{flex:'0 0 150px',fontSize:12,padding:'5px 7px'}}>
                      {Object.entries(MODEL_PROVIDERS).map(([k,v])=><option key={k} value={k}>{v.label}</option>)}
                    </select>
                    {prov.models.length>0
                      ? <select value={j.model} onChange={e=>updateJudge(i,{model:e.target.value})} style={{flex:1,fontSize:12,padding:'5px 7px'}}>
                          {prov.models.map(m=><option key={m} value={m}>{m}</option>)}
                        </select>
                      : <input type="text" value={j.model} onChange={e=>updateJudge(i,{model:e.target.value})} placeholder="model name"
                          style={{flex:1,fontSize:12,padding:'5px 7px'}}/>}
                    <button className="btn btn-sm" onClick={()=>removeJudge(i)} disabled={judges.length<=1} title="Remove judge"
                      style={{flexShrink:0,color:'#b81c1c'}}><i className="ti ti-trash"/></button>
                  </div>
                  {(showKey||showUrl)&&(
                    <div style={{display:'flex',gap:7,paddingLeft:61}}>
                      {showKey&&<input type="password" value={j.api_key||''} onChange={e=>updateJudge(i,{api_key:e.target.value})}
                        placeholder={sameAsMain ? `${prov.keyPlaceholder||'API key'}` : `${prov.keyPlaceholder||'API key'} (blank = backend env key)`}
                        style={{flex:1,fontSize:12,padding:'5px 7px',fontFamily:'var(--mono)'}}/>}
                      {showUrl&&<input type="url" value={j.base_url||''} onChange={e=>updateJudge(i,{base_url:e.target.value})}
                        placeholder={prov.urlPlaceholder||'Base URL'} style={{flex:'0 0 230px',fontSize:12,padding:'5px 7px'}}/>}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
          <div style={{fontSize:11,color:'#9fadbf',marginTop:9,display:'flex',alignItems:'flex-start',gap:5}}>
            <i className="ti ti-info-circle" style={{marginTop:1}}/>
            <span>Odd number of judges avoids ties. A judge using your analysis-model provider reuses its key automatically; otherwise enter a key here, or leave blank to use the backend env key for that provider.</span>
          </div>
        </div>
      )}

      {!data&&!loading&&!error&&<div style={{fontSize:13,color:'#7a8494'}}>Evaluate analysis quality with {nJudges} independent LLM judges running in parallel. Each scores completeness, precision, severity accuracy and specificity.</div>}
      {loading&&<div style={{display:'flex',alignItems:'center',gap:10,color:'#7a8494',fontSize:13}}><span className="spinner"/>Running {nJudges} independent judges in parallel…</div>}
      {error&&<div className="info-msg" style={{marginTop:0}}><i className="ti ti-info-circle"/>{error}</div>}
      {data&&(
        <>
          {(data.critical_gaps||[]).length>0&&(
            <details style={{background:'#fff1f1',border:'1px solid #f8c0c0',borderRadius:7,marginBottom:12}}>
              <summary style={{padding:'9px 12px',cursor:'pointer',fontSize:12,fontWeight:700,color:'#b81c1c',userSelect:'none'}}>
                <i className="ti ti-alert-triangle" style={{marginRight:5}}/>
                {data.critical_gaps.length} critical gap{data.critical_gaps.length>1?'s':''} (consensus missed) — click to expand
              </summary>
              <ul style={{margin:0,padding:'4px 14px 12px 30px',maxHeight:260,overflowY:'auto',fontSize:12,color:'#7a2020',lineHeight:1.55}}>
                {data.critical_gaps.map((g,i)=>{
                  const m = /^\[([^\]]+)\]\s*(.*)$/.exec(g)   // split "[agent] text"
                  return (
                    <li key={i} style={{marginBottom:6}}>
                      {m && <code style={{marginRight:6,fontSize:11,background:'#fde0e0',padding:'1px 5px',borderRadius:4,color:'#b81c1c'}}>{m[1]}</code>}
                      {m ? m[2] : g}
                    </li>
                  )
                })}
              </ul>
            </details>
          )}
          <div style={{display:'flex',flexDirection:'column',gap:6}}>
            {(data.agent_verdicts||[]).map((v,i)=>(
              <div key={i} style={{padding:'10px 14px',border:'1px solid #e8eaed',borderRadius:8,background:'#fff'}}>
                <div style={{display:'flex',alignItems:'center',gap:10,flexWrap:'wrap'}}>
                  <span style={{fontSize:12,fontWeight:600,color:'#0d1117',minWidth:120}}>{(v.agent_name||'').replace(/_/g,' ')}</span>
                  <span style={{fontSize:11,fontWeight:600,padding:'2px 8px',borderRadius:10,background:verdictBg[v.verdict]||'#f7f8fa',color:verdictColor[v.verdict]||'#7a8494',border:`1px solid ${verdictBorder[v.verdict]||'#e8eaed'}`}}>{v.verdict}</span>
                  <span style={{fontSize:11,color:'#7a8494'}}>confidence: <strong style={{color:confColor[v.confidence]||'#7a8494'}}>{v.confidence||'—'}</strong></span>
                  <span style={{fontSize:13,fontWeight:700,fontFamily:'var(--mono)',color:'#0d1117',marginLeft:'auto'}}>{(v.overall_score||0).toFixed(1)}<span style={{fontSize:10,fontWeight:400,color:'#9fadbf'}}>/5</span></span>
                </div>
                {v.summary&&<div style={{fontSize:12,color:'#7a8494',marginTop:6,lineHeight:1.5}}>{v.summary}</div>}
                {(v.consensus_missed||[]).length>0&&<div style={{fontSize:11,color:'#b81c1c',marginTop:5}}><i className="ti ti-alert-triangle" style={{fontSize:12,marginRight:3}}/>Missed: {v.consensus_missed.map((m,j)=><code key={j} style={{marginRight:4}}>{m}</code>)}</div>}
                {(v.disagreements||[]).length>0&&<div style={{fontSize:11,color:'#8a5200',marginTop:4}}><i className="ti ti-arrows-diff" style={{fontSize:12,marginRight:3}}/>{v.disagreements.join(' · ')}</div>}
              </div>
            ))}
          </div>
          <div style={{fontSize:11,color:'#9fadbf',marginTop:10,display:'flex',alignItems:'center',gap:5}}><i className="ti ti-info-circle"/>Scores: completeness · precision · severity accuracy · specificity (each 1–5)</div>
          <button className="btn btn-sm" style={{marginTop:10}} onClick={()=>setData(null)}><i className="ti ti-refresh"/> Re-run judges</button>
        </>
      )}
    </div>
  )
}

// ── Review Summary Panel ────────────────────────────────────────────────────────

function ReviewSummaryPanel({ r, onClose }) {
  const sevBadge = s => {
    const c={critical:'#b81c1c',high:'#8a5200',medium:'#1a6cf6',low:'#6b7280'}[s?.toLowerCase()]||'#6b7280'
    return <span style={{display:'inline-block',padding:'1px 7px',borderRadius:10,fontSize:10,fontWeight:700,textTransform:'uppercase',background:`${c}18`,color:c,marginRight:6,verticalAlign:'middle'}}>{s||'low'}</span>
  }
  const sevRank = {critical:0,high:1,medium:2,low:3}
  const sortBySev = arr => [...arr].sort((a,b)=>(sevRank[a.severity?.toLowerCase()]??9)-(sevRank[b.severity?.toLowerCase()]??9))

  const secFindings   = r.security?.findings||[]
  const codeFindings  = r.code_analysis?.findings||[]
  const astFindings   = r.ast_analysis?.findings||[]
  const taintPaths    = r.taint_analysis?.taint_paths||[]
  const iacFindings   = r.iac_analysis?.findings||[]
  const entropyFinds  = r.secrets_entropy?.findings||[]
  const breakingChgs  = r.interface?.breaking_changes||[]
  const schemaChgs    = r.schema_change?.changes||[]
  const qaScenarios   = r.qa_scenarios?.scenarios||[]
  const fixSuggestions= r.remediation?.fix_suggestions||[]
  const checklist     = r.remediation?.validation_checklist||[]

  const gateColor  = {APPROVE:'#0c7c4b', HOLD:'#8a5200', BLOCK:'#b81c1c'}[r.gate_decision]||'#7a8494'
  const gateIcon   = {APPROVE:'ti-circle-check', HOLD:'ti-alert-triangle', BLOCK:'ti-ban'}[r.gate_decision]||'ti-help-circle'
  const riskColor  = {low:'#0c7c4b', medium:'#8a5200', high:'#b81c1c', critical:'#b81c1c'}[r.overall_risk]||'#7a8494'
  const blastScore = r.dependency?.blast_radius_score||0
  const affectedSvcs = r.dependency?.affected_services||[]
  const qaHighPlus = qaScenarios.filter(s=>['critical','high'].includes(s.priority?.toLowerCase()))

  function FindingRow({sev, text, file}) {
    return (
      <div style={{display:'flex',alignItems:'flex-start',gap:8,padding:'6px 0',borderBottom:'1px solid #f0f2f5'}}>
        {sevBadge(sev)}
        <div style={{flex:1,fontSize:13,color:'#1a1f2e',lineHeight:1.5}}>
          {text}
          {file&&<div style={{fontSize:11,color:'#9fadbf',fontFamily:'var(--mono)',marginTop:2}}>{file}</div>}
        </div>
      </div>
    )
  }

  function Section({icon, title, count, countColor='#7a8494', children}) {
    return (
      <div style={{marginBottom:20}}>
        <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:8,paddingBottom:6,borderBottom:'2px solid #f0f2f5'}}>
          <i className={`ti ${icon}`} style={{fontSize:15,color:countColor}}/>
          <span style={{fontSize:13,fontWeight:600,color:'#0d1117'}}>{title}</span>
          {count!=null&&<span style={{marginLeft:'auto',fontSize:12,fontWeight:700,color:countColor}}>{count}</span>}
        </div>
        {children}
      </div>
    )
  }

  function EmptyRow({msg}) {
    return <div style={{fontSize:12,color:'#9fadbf',padding:'4px 0'}}><i className="ti ti-circle-check" style={{color:'#0c7c4b'}}/> {msg}</div>
  }

  function buildMarkdown() {
    const lines = [
      `## Impact Analysis Review — ${r.gate_decision}`,
      `**Risk:** ${(r.overall_risk||'').toUpperCase()}  |  **Score:** ${r.risk_score||0}/100  |  **Tokens:** ${(r.token_usage||0).toLocaleString()}`,
      '',`> ${r.rationale||''}`, '',
    ]
    // Lead with the ranked, deduplicated Top Issues so reviewers see "what
    // must I look at" first — per-agent sections follow as supporting detail.
    const topIssues = (r.top_issues||[]).slice(0,5)
    if (topIssues.length) {
      const sevIcon = {critical:'🚨',high:'🔴',medium:'🟡',low:'🔵'}
      lines.push('### 🎯 Top issues to review')
      topIssues.forEach((it,i)=>{
        const loc = it.file_path ? ` — \`${it.file_path}${it.line?':'+it.line:''}\`` : ''
        const agree = (it.agents||[]).length>1 ? ` *(✓ ${it.agents.length} agents agree)*` : ''
        const unv = it.unverified ? ' ⚠️ *location unverified*' : ''
        lines.push(`${i+1}. ${sevIcon[it.severity]||'ℹ️'} **${(it.severity||'').toUpperCase()}** — ${it.title}${loc}${agree}${unv}`)
      })
      lines.push('')
    }
    const push = (title, items, fmt) => {
      if (!items.length) return
      lines.push(`### ${title}`)
      items.forEach(f=>lines.push(fmt(f)))
      lines.push('')
    }
    push('🔒 Security & Secrets', sortBySev([...secFindings,...entropyFinds]),
      f=>`- **[${(f.severity||'low').toUpperCase()}]** ${f.cwe?'['+f.cwe+'] ':''}${f.description||f.kind||''}${f.file?' — `'+f.file+'`':''}`)
    push('🧬 Taint / Injection Paths', sortBySev(taintPaths),
      t=>`- **[${(t.severity||'high').toUpperCase()}]** \`${t.source_var}\` → \`${t.sink_var}\`${t.cwe?' ['+t.cwe+']':''} — ${t.description||''}`)
    push('🏗️ IaC Security', sortBySev(iacFindings),
      f=>`- **[${(f.severity||'medium').toUpperCase()}]** ${f.kind} on \`${f.resource}\` — ${f.description}`)
    push('⚙️ Code Quality (AST)', sortBySev(astFindings),
      f=>`- **[${(f.severity||'medium').toUpperCase()}]** ${f.kind}: \`${f.function}\` — ${f.description}`)
    push('🔌 Breaking Changes', sortBySev(breakingChgs),
      b=>`- **[${(b.severity||'high').toUpperCase()}]** ${b.type} — ${(b.break_type||'').replace(/_/g,' ')} \`${b.path}\``)
    push('🗄️ Schema Changes', sortBySev(schemaChgs),
      c=>`- **[${(c.severity||'medium').toUpperCase()}]** ${(c.change_type||'').replace(/_/g,' ')} on \`${c.table}\` ${c.reversible===false?'⚠ NOT reversible':''}`)
    if (blastScore||affectedSvcs.length) {
      lines.push('### 📦 Dependencies')
      lines.push(`- Blast radius: **${blastScore}/100**`)
      if (affectedSvcs.length) lines.push(`- Affected services: ${affectedSvcs.map(s=>'`'+s+'`').join(', ')}`)
      lines.push('')
    }
    if (r.test_coverage?.coverage_delta!==undefined) {
      lines.push('### 🧪 Test Coverage')
      lines.push(`- Coverage delta: **${(r.test_coverage.coverage_delta||0)>0?'+':''}${parseFloat(r.test_coverage.coverage_delta||0).toFixed(1)}%** | Regression risk: **${r.test_coverage.regression_risk||'low'}**`)
      lines.push('')
    }
    if (qaHighPlus.length) {
      lines.push(`### 🧪 QA Scenarios (Critical/High — ${qaHighPlus.length} of ${qaScenarios.length})`)
      qaHighPlus.forEach(s=>lines.push(`- **[${(s.priority||'').toUpperCase()}]** [${s.id}] ${s.title} *(${(s.type||'').replace(/_/g,' ')})*`))
      lines.push('')
    }
    if (r.remediation?.executive_summary) {
      lines.push('### 📋 Recommendation')
      lines.push(r.remediation.executive_summary)
      lines.push('')
    }
    if (fixSuggestions.length) {
      lines.push('#### Fixes Required')
      fixSuggestions.forEach(f=>lines.push(`- [ ] ${typeof f==='string'?f:f.description||''}`))
      lines.push('')
    }
    if (checklist.length) {
      lines.push('#### Pre-release Checklist')
      checklist.forEach(c=>lines.push(`- [ ] ${typeof c==='string'?c:c.item||''}`))
    }
    lines.push('','---',`*Generated by CIAA · Analysis ID: \`${r.request_id||''}\`*`)
    return lines.join('\n')
  }

  function copyMd(btn) {
    navigator.clipboard.writeText(buildMarkdown()).then(()=>{
      const orig=btn.innerHTML; btn.innerHTML='<i class="ti ti-check"/> Copied!'
      setTimeout(()=>btn.innerHTML=orig, 2000)
    })
  }

  const secCriticalHigh=[...secFindings,...entropyFinds,...taintPaths,...iacFindings]
    .filter(f=>['critical','high'].includes((f.severity||'').toLowerCase())).length

  const allSecRows=[
    ...sortBySev(secFindings).map((f,i)=><FindingRow key={'s'+i} sev={f.severity} text={`${f.cwe?'['+f.cwe+'] ':''}${f.description}`} file={f.file}/>),
    ...sortBySev(entropyFinds).map((f,i)=><FindingRow key={'e'+i} sev={f.severity} text={`Secret detected: ${f.kind||'unknown'} — ${f.variable||''}`}/>),
    ...sortBySev(taintPaths).map((t,i)=><FindingRow key={'t'+i} sev={t.severity||'high'} text={`Taint: ${t.source_var||'?'} → ${t.sink_var||'?'}${t.cwe?' ['+t.cwe+']':''} ${t.description||''}`}/>),
    ...sortBySev(iacFindings).map((f,i)=><FindingRow key={'i'+i} sev={f.severity} text={`IaC: ${f.kind||''} on ${f.resource||''} — ${f.description}`}/>),
  ]
  const qualityRows=[
    ...sortBySev(astFindings).map((f,i)=><FindingRow key={'a'+i} sev={f.severity} text={`${f.kind||''}: ${f.function||''} — ${f.description}`}/>),
    ...sortBySev(codeFindings).map((f,i)=><FindingRow key={'c'+i} sev={f.severity} text={f.description} file={f.file}/>),
  ]
  const contractRows=[
    ...sortBySev(breakingChgs).map((b,i)=><FindingRow key={'b'+i} sev={b.severity} text={`${b.type||''} — ${(b.break_type||'').replace(/_/g,' ')} change`} file={b.path}/>),
    ...sortBySev(schemaChgs).map((c,i)=><FindingRow key={'sc'+i} sev={c.severity} text={`${(c.change_type||'').replace(/_/g,' ')} on ${c.table||'?'} ${c.reversible===false?'(NOT reversible)':'(reversible)'}`}/>),
  ]

  return (
    <div className="card" style={{marginTop:16,border:`2px solid ${gateColor}20`}}>
      {/* Gate banner */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:12,padding:'14px 18px',margin:'-20px -20px 20px',background:`${gateColor}0d`,borderRadius:'var(--rl) var(--rl) 0 0',borderBottom:`1px solid ${gateColor}30`}}>
        <div style={{display:'flex',alignItems:'center',gap:12}}>
          <i className={`ti ${gateIcon}`} style={{fontSize:28,color:gateColor}}/>
          <div>
            <div style={{fontSize:18,fontWeight:800,color:gateColor}}>{r.gate_decision}</div>
            <div style={{fontSize:12,color:'#7a8494',marginTop:1}}>Risk: <strong style={{color:riskColor}}>{(r.overall_risk||'').toUpperCase()}</strong> · Score: <strong>{r.risk_score||0}/100</strong> · {(r.token_usage||0).toLocaleString()} tokens</div>
          </div>
        </div>
        <div style={{display:'flex',gap:8}}>
          <button className="btn" onClick={e=>copyMd(e.currentTarget)} style={{fontSize:12}}><i className="ti ti-clipboard"/> Copy as Markdown</button>
          <button className="btn" onClick={onClose} style={{fontSize:12}}><i className="ti ti-x"/> Close</button>
        </div>
      </div>

      {r.rationale&&<p style={{fontSize:13,color:'#374151',lineHeight:1.7,background:'#f8f9fb',borderLeft:`3px solid ${gateColor}`,padding:'10px 14px',borderRadius:'0 6px 6px 0',marginBottom:18}}>{r.rationale}</p>}

      <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(340px,1fr))',gap:20}}>
        <div>
          <Section icon="ti-shield-lock" title="Security & Secrets" count={secCriticalHigh?`${secCriticalHigh} critical/high`:'clean'} countColor={secCriticalHigh?'#b81c1c':'#0c7c4b'}>
            {allSecRows.length?allSecRows:<EmptyRow msg="No security issues detected"/>}
          </Section>
          <Section icon="ti-code" title="Code Quality" count={qualityRows.length||null} countColor={qualityRows.length?'#8a5200':'#0c7c4b'}>
            {qualityRows.length?qualityRows:<EmptyRow msg="No code quality issues"/>}
          </Section>
        </div>
        <div>
          <Section icon="ti-plug-connected" title="Contracts & Schema" count={(breakingChgs.length+schemaChgs.length)||null} countColor={(breakingChgs.length+schemaChgs.length)?'#b81c1c':'#0c7c4b'}>
            {contractRows.length?contractRows:<EmptyRow msg="No breaking changes or schema issues"/>}
          </Section>
          <Section icon="ti-topology-star-3" title="Dependencies & Coverage" count={null}>
            <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:8}}>
              <div style={{flex:1,height:8,background:'#f0f2f5',borderRadius:4,overflow:'hidden'}}>
                <div style={{height:'100%',width:`${blastScore}%`,background:blastScore>70?'#b81c1c':blastScore>40?'#8a5200':'#0c7c4b',borderRadius:4}}/>
              </div>
              <span style={{fontSize:13,fontWeight:700,color:blastScore>70?'#b81c1c':blastScore>40?'#8a5200':'#0c7c4b',minWidth:48,textAlign:'right'}}>{blastScore}/100</span>
            </div>
            {affectedSvcs.length>0&&<div style={{fontSize:12,color:'#4b5563'}}>Affected: {affectedSvcs.map((s,i)=><span key={i} style={{background:'#fff3cd',padding:'1px 6px',borderRadius:8,marginRight:4}}>{s}</span>)}</div>}
            {r.test_coverage?.coverage_delta!==undefined&&<div style={{fontSize:12,color:'#4b5563',marginTop:6}}>Coverage delta: <strong style={{color:(r.test_coverage.coverage_delta||0)>=0?'#0c7c4b':'#b81c1c'}}>{(r.test_coverage.coverage_delta||0)>0?'+':''}{parseFloat(r.test_coverage.coverage_delta||0).toFixed(1)}%</strong> · Regression risk: <strong>{r.test_coverage.regression_risk||'low'}</strong></div>}
          </Section>
          {qaHighPlus.length>0&&(
            <Section icon="ti-checklist" title={`QA Scenarios — High/Critical (${qaHighPlus.length})`} count={null}>
              {qaHighPlus.map((s,i)=>(
                <div key={i} style={{display:'flex',alignItems:'flex-start',gap:8,padding:'6px 0',borderBottom:'1px solid #f0f2f5'}}>
                  {sevBadge(s.priority)}
                  <div style={{flex:1}}>
                    <div style={{fontSize:13,fontWeight:500,color:'#1a1f2e'}}>{s.title}</div>
                    <div style={{fontSize:11,color:'#9fadbf',marginTop:2}}>{(s.type||'').replace(/_/g,' ')} · {s.id}</div>
                  </div>
                </div>
              ))}
              {qaScenarios.length>qaHighPlus.length&&<div style={{fontSize:12,color:'#9fadbf',paddingTop:6}}>+{qaScenarios.length-qaHighPlus.length} medium/low scenarios — see QA Scenarios tab</div>}
            </Section>
          )}
          {(fixSuggestions.length>0||checklist.length>0)&&(
            <Section icon="ti-tool" title="Remediation" count={null}>
              {r.remediation?.executive_summary&&<p style={{fontSize:13,color:'#374151',lineHeight:1.7,marginBottom:12}}>{r.remediation.executive_summary}</p>}
              {fixSuggestions.length>0&&<><div style={{fontSize:11,fontWeight:600,textTransform:'uppercase',letterSpacing:'.06em',color:'#9fadbf',marginBottom:6}}>Fixes required</div>{fixSuggestions.slice(0,5).map((f,i)=><div key={i} style={{fontSize:13,color:'#1a1f2e',padding:'4px 0',borderBottom:'1px solid #f0f2f5'}}>• {typeof f==='string'?f:f.description||JSON.stringify(f)}</div>)}</>}
              {checklist.length>0&&<><div style={{fontSize:11,fontWeight:600,textTransform:'uppercase',letterSpacing:'.06em',color:'#9fadbf',margin:'10px 0 6px'}}>Pre-release checklist</div>{checklist.slice(0,6).map((c,i)=><div key={i} style={{fontSize:13,color:'#374151',padding:'3px 0',display:'flex',gap:6,alignItems:'flex-start'}}><span style={{color:'#9fadbf',flexShrink:0}}>☐</span>{typeof c==='string'?c:c.item||JSON.stringify(c)}</div>)}</>}
            </Section>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Human Review Panel ──────────────────────────────────────────────────────────

function HumanReviewPanel({r, state, showToast}) {
  const [showModal, setShowModal] = useState(false)
  const [pendingDecision, setPendingDecision] = useState('')
  const [reviewDone, setReviewDone] = useState(null)
  const gKey=(r.gate_decision||'HOLD').toUpperCase()
  const canOverride=canOverrideGate(state)
  const reviewNeeded=gKey==='HOLD'||gKey==='BLOCK'
  if (!reviewNeeded && !canOverride) return null
  const panelBorder=gKey==='BLOCK'?'#f8c0c0':gKey==='HOLD'?'#fad98a':'#cfe8d8'
  const panelTint=gKey==='BLOCK'?'#fff1f1':gKey==='HOLD'?'#fff8ec':'#edfaf3'
  const panelIconC=gKey==='BLOCK'?'#b81c1c':gKey==='HOLD'?'#8a5200':'#0c7c4b'
  const panelTitle=reviewNeeded?'Human Review Required':'Reviewer Override (optional)'

  if (reviewDone) {
    const s={APPROVED:{color:'#0c7c4b',bg:'#edfaf3',icon:'ti-circle-check',label:'Approved by reviewer'},CHANGES:{color:'#8a5200',bg:'#fff8ec',icon:'ti-alert-triangle',label:'Changes requested by reviewer'},REJECTED:{color:'#b81c1c',bg:'#fff1f1',icon:'ti-ban',label:'Blocked by reviewer'}}[reviewDone.decision]||{}
    return (
      <div style={{background:s.bg,border:`1.5px solid ${s.color}`,borderRadius:12,padding:'20px 22px',marginBottom:18}}>
        <div style={{display:'flex',alignItems:'center',gap:12}}>
          <div style={{width:40,height:40,borderRadius:10,background:s.color,display:'flex',alignItems:'center',justifyContent:'center',flexShrink:0}}><i className={`ti ${s.icon}`} style={{fontSize:20,color:'#fff'}}/></div>
          <div style={{flex:1}}>
            <div style={{fontSize:15,fontWeight:700,color:s.color,fontFamily:'Instrument Serif,serif'}}>{s.label}</div>
            <div style={{fontSize:12,color:'#7a8494',marginTop:2}}>By <strong>{reviewDone.reviewer}</strong> at {new Date(reviewDone.timestamp).toLocaleString()}</div>
          </div>
        </div>
        <div style={{marginTop:12,padding:'10px 13px',background:'rgba(0,0,0,.03)',borderRadius:8,fontSize:13,color:'#3d4652',lineHeight:1.5}}>
          <strong>Reason:</strong> {reviewDone.reason}
        </div>
      </div>
    )
  }

  return (
    <div style={{background:'#fff',border:`1.5px solid ${panelBorder}`,borderRadius:12,padding:'20px 22px',marginBottom:18}} id="human-review-panel">
      <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:14}}>
        <div style={{width:36,height:36,borderRadius:8,background:panelTint,display:'flex',alignItems:'center',justifyContent:'center',flexShrink:0}}>
          <i className="ti ti-user-check" style={{fontSize:18,color:panelIconC}}/>
        </div>
        <div>
          <div style={{fontSize:14,fontWeight:600,color:'#0d1117'}}>{panelTitle}</div>
          <div style={{fontSize:12,color:'#7a8494',marginTop:1}}>{reviewNeeded?`This ${gKey} gate requires a reviewer to decide.`:'The AI recommends APPROVE. As a reviewer you may override.'}</div>
        </div>
      </div>
      {canOverride ? (
        <div style={{display:'grid',gridTemplateColumns:'1fr 1fr 1fr',gap:10,marginBottom:14}}>
          {[['APPROVED','ti-circle-check','#0c7c4b','Approve merge','Pipeline can proceed'],['CHANGES','ti-alert-triangle','#8a5200','Request changes','Hold for rework'],['REJECTED','ti-ban','#b81c1c','Block merge','Block deployment']].map(([dec,icon,color,label,sub])=>(
            <div key={dec} onClick={()=>{setPendingDecision(dec);setShowModal(true)}} style={{background:'#f7f8fa',border:'2px solid transparent',borderRadius:8,padding:12,textAlign:'center',cursor:'pointer',transition:'all .15s'}}
              onMouseOver={e=>e.currentTarget.style.borderColor=color} onMouseOut={e=>e.currentTarget.style.borderColor='transparent'}>
              <i className={`ti ${icon}`} style={{fontSize:22,color,display:'block',marginBottom:5}}/>
              <div style={{fontSize:13,fontWeight:600,color}}>{label}</div>
              <div style={{fontSize:11,color:'#7a8494',marginTop:2}}>{sub}</div>
            </div>
          ))}
        </div>
      ) : (
        <div style={{background:'#f7f8fa',border:'1px solid #e8eaed',borderRadius:8,padding:'12px 14px',marginBottom:14,fontSize:12,color:'#7a8494',display:'flex',alignItems:'center',gap:8}}>
          <i className="ti ti-lock" style={{fontSize:14}}/>Awaiting reviewer approval — your role does not have gate-override permission.
        </div>
      )}
      <div style={{fontSize:11,color:'#9fadbf',display:'flex',alignItems:'center',gap:5}}>
        <i className="ti ti-shield-lock"/>All review decisions are permanently logged. Request ID: <code style={{fontSize:10}}>{state.lastRequestId||'—'}</code>
      </div>
      {showModal && (
        <ReviewModal decision={pendingDecision} state={state} showToast={showToast}
          onClose={()=>setShowModal(false)}
          onSubmit={(review)=>{setReviewDone(review);setShowModal(false);showToast(`${review.decision==='APPROVED'?'✅':'review.decision'==='CHANGES'?'⚠️':'🚫'} Review submitted`,'success')}}/>
      )}
    </div>
  )
}

function ReviewModal({decision, state, onClose, onSubmit, showToast}) {
  const [name, setName] = useState('')
  const [reason, setReason] = useState('')
  const [notes, setNotes] = useState('')
  const [loading, setLoading] = useState(false)
  const meta={APPROVED:{title:'Approve Merge',color:'#0c7c4b',btnTxt:'Confirm Approval'},CHANGES:{title:'Request Changes',color:'#8a5200',btnTxt:'Confirm Request'},REJECTED:{title:'Block Merge',color:'#b81c1c',btnTxt:'Confirm Block'}}[decision]||{}
  async function submit() {
    if(!name){alert('Reviewer name is required.');return}
    if(!reason||reason.length<10){alert('Reason must be at least 10 characters.');return}
    setLoading(true)
    const record={decision,reviewer:name,reason,notes,timestamp:new Date().toISOString(),request_id:state.lastRequestId||'',gate:decision}
    if(state.backendUrl&&state.lastRequestId){
      try {
        const h={'Content-Type':'application/json'}; if(state.backendKey)h['X-API-Key']=state.backendKey
        const override_to={APPROVED:'APPROVE',CHANGES:'HOLD',REJECTED:'BLOCK'}[decision]||'BLOCK'
        await fetch(`${state.backendUrl}/api/v1/gate/${state.lastRequestId}/override`,{method:'POST',headers:h,body:JSON.stringify({override_to,reason:`[${decision}] ${reason} — ${name}`})})
      } catch(_){}
    }
    setLoading(false)
    onSubmit(record)
  }
  return (
    <div style={{display:'flex',position:'fixed',inset:0,background:'rgba(0,0,0,.4)',zIndex:1000,alignItems:'center',justifyContent:'center'}}>
      <div style={{background:'#fff',borderRadius:14,padding:28,maxWidth:480,width:'90%',boxShadow:'0 20px 60px rgba(0,0,0,.2)'}}>
        <div style={{marginBottom:18}}>
          <div style={{fontSize:18,fontWeight:600,color:meta.color,fontFamily:'Instrument Serif,serif'}}>{meta.title}</div>
          <div style={{fontSize:13,color:'#7a8494',marginTop:3}}>Your decision will be permanently logged in the audit trail.</div>
        </div>
        <div className="field" style={{marginBottom:14}}><label>Your name / reviewer ID <span style={{color:'#b81c1c'}}>*</span></label><input type="text" value={name} onChange={e=>setName(e.target.value)} placeholder="e.g. Jane Smith"/></div>
        <div className="field" style={{marginBottom:14}}><label>Reason / justification <span style={{color:'#b81c1c'}}>*</span></label><textarea value={reason} onChange={e=>setReason(e.target.value)} rows="3" placeholder="Explain your decision…" style={{resize:'vertical'}}/></div>
        <div className="field" style={{marginBottom:18}}><label>Additional notes <span style={{color:'#7a8494',fontWeight:400}}>(optional)</span></label><textarea value={notes} onChange={e=>setNotes(e.target.value)} rows="2" style={{resize:'vertical'}}/></div>
        <div style={{display:'flex',gap:10,justifyContent:'flex-end'}}>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={submit} disabled={loading}>
            {loading?<><span className="spinner" style={{width:14,height:14,borderWidth:2}}/>Submitting…</>:meta.btnTxt}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Main ResultsView ────────────────────────────────────────────────────────────

const RESULT_TABS = [
  {group:'Overview',tabs:[{id:'summary',label:'Summary'}]},
  {group:'Security',tabs:[{id:'security',label:'Security'},{id:'advanced',label:'⚗ Advanced'}]},
  {group:'Impact',tabs:[{id:'references',label:'🔗 References'},{id:'cross_repo',label:'🔀 Cross-Repo'},{id:'dependency',label:'Dependency'},{id:'interface',label:'Interface'},{id:'schema',label:'Schema'}]},
  {group:'Quality',tabs:[{id:'functional',label:'📋 FSD'},{id:'qa',label:'🧪 QA Scenarios'},{id:'performance',label:'🚀 Performance'},{id:'privacy',label:'🔒 Privacy'},{id:'quality',label:'🔧 Quality'}]},
  {group:'Actions',tabs:[{id:'checklist',label:'✅ Checklist'},{id:'compliance',label:'🛡 Compliance'},{id:'remediation',label:'Remediation'},{id:'timings',label:'⏱ Timings'}]},
]
const ALL_TABS = RESULT_TABS.flatMap(g=>g.tabs.map(t=>t.id))

// Suggested review order shown as ①②③④ badges on key tabs (+ ⑤ on Post to PR).
// Guides users start → finish without cluttering every tab.
const STEP_FOR_TAB = { summary: 1, security: 2, dependency: 3, checklist: 4 }
const STEP_TITLE = {
  1: 'Step 1 — start here: gate decision & overview',
  2: 'Step 2 — review security findings',
  3: 'Step 3 — review impact (dependencies & interfaces)',
  4: 'Step 4 — work through the reviewer checklist',
  5: 'Step 5 — finish: post findings to the PR',
}

export default function ResultsView({ active, showView, showToast }) {
  const { state, update } = useApp()
  const [activeTab, setActiveTab] = useState('summary')
  const [showPRDesc, setShowPRDesc] = useState(false)
  const [showReviewSummary, setShowReviewSummary] = useState(false)
  const [showJudgePanel, setShowJudgePanel] = useState(false)
  const [findingsSearch, setFindingsSearch] = useState('')
  const tabContentRef = useRef(null)

  // Switch tab + scroll tab content to top
  function switchTab(id) {
    setActiveTab(id)
    setFindingsSearch('')
    requestAnimationFrame(() => {
      if (tabContentRef.current) tabContentRef.current.scrollTop = 0
    })
  }

  // Listen for keyboard events + left/right arrow tab navigation
  useEffect(() => {
    const prHandler  = () => setShowPRDesc(true)
    const tabHandler = e => { if (e.detail) switchTab(e.detail) }
    const keyHandler = e => {
      if (['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (!active) return
      if (e.key === 'ArrowRight') {
        setActiveTab(cur => { const i = ALL_TABS.indexOf(cur); return ALL_TABS[Math.min(i+1, ALL_TABS.length-1)] })
      }
      if (e.key === 'ArrowLeft') {
        setActiveTab(cur => { const i = ALL_TABS.indexOf(cur); return ALL_TABS[Math.max(i-1, 0)] })
      }
    }
    document.addEventListener('ciaa:showPRDesc', prHandler)
    document.addEventListener('ciaa:switchTab', tabHandler)
    document.addEventListener('keydown', keyHandler)
    return () => {
      document.removeEventListener('ciaa:showPRDesc', prHandler)
      document.removeEventListener('ciaa:switchTab', tabHandler)
      document.removeEventListener('keydown', keyHandler)
    }
  }, [active])

  if (!state.report && !state.analysisRequested) {
    const hasTarget = !!(state.selectedPR || (state.sourceBranch && state.targetBranch) || state.commitSha?.length >= 5)
    return (
      <div style={{maxWidth:560,margin:'60px auto',textAlign:'center',padding:'0 20px'}}>
        <div style={{fontSize:56,marginBottom:16}}>🔬</div>
        <div style={{fontSize:22,fontWeight:700,color:'#0d1117',marginBottom:8}}>No analysis running</div>
        <div style={{fontSize:14,color:'#7a8494',marginBottom:28,lineHeight:1.6}}>
          {hasTarget?'An analysis target is selected. Click Run Analysis to start.':'Select a pull request, branch diff, or commit on the Analysis target screen, then click Run Analysis.'}
        </div>
        <div style={{display:'flex',gap:12,justifyContent:'center',flexWrap:'wrap'}}>
          {hasTarget && <button className="btn btn-primary" style={{fontSize:14,padding:'10px 24px'}} onClick={()=>update({analysisRequested:true})}>
            <i className="ti ti-player-play"/> Run Analysis
          </button>}
          <button className="btn" style={{fontSize:14,padding:'10px 24px'}} onClick={()=>showView('target')}>
            <i className="ti ti-target"/> Go to Analysis target
          </button>
        </div>
      </div>
    )
  }

  if (!state.report && state.analysisRequested) {
    return <RunningView state={state} update={update} showToast={showToast}/>
  }

  const r = state.report
  const gKey=(r.gate_decision||'HOLD').toUpperCase()
  const gCls={APPROVE:'gate-approve',HOLD:'gate-hold',BLOCK:'gate-block'}[gKey]||'gate-hold'
  const gIcon={APPROVE:'ti-circle-check',HOLD:'ti-alert-triangle',BLOCK:'ti-ban'}[gKey]||'ti-alert-triangle'
  const provInfo=MODEL_PROVIDERS[state.modelProvider]||{icon:'✦',label:'AI'}
  const modelBadge=`${provInfo.icon} ${state.modelName||state.modelProvider}`
  const hasAgentData=r.code_analysis?.summary||r.security?.findings?.length||r.code_analysis?.change_type!=='unknown'
  const snipCache = parseDiffToSnippets(state.diffText||'')

  function renderTab(tab, search='') {
    switch(tab) {
      case 'summary': return <SummaryTab r={r} snipCache={snipCache}/>
      case 'security': return <SecurityTab r={r} snipCache={snipCache} search={search}/>
      case 'advanced': return <AdvancedTab r={r} snipCache={snipCache}/>
      case 'dependency': return <DependencyTab r={r}/>
      case 'interface': return <InterfaceTab r={r}/>
      case 'schema': return <SchemaTab r={r}/>
      case 'remediation': return <RemediationTab r={r}/>
      case 'functional': return <FunctionalTab r={r}/>
      case 'cross_repo': return <CrossRepoTab r={r}/>
      case 'qa': return <QAScenariosTab r={r}/>
      case 'performance': return <PerformanceTab r={r} snipCache={snipCache}/>
      case 'privacy': return <PrivacyTab r={r} snipCache={snipCache}/>
      case 'quality': return <QualityTab r={r} snipCache={snipCache}/>
      case 'checklist': return <ChecklistTab r={r} canOverride={canOverrideGate(state)}/>
      case 'compliance': return <ComplianceTab r={r} snipCache={snipCache}/>
      case 'timings': return <TimingsTab r={r}/>
      case 'references': return <ReferencesTab r={r}/>
      default: return null
    }
  }

  async function postPRComments() {
    if(!state.backendUrl||!state.lastRequestId){showToast('Need a backend URL and a completed analysis first.','error');return}
    const pr=state.selectedPR; const prId=pr?(pr.number||pr.id||''):''
    if(!prId){showToast('Select a PR in Analysis Target first.','error');return}
    showToast('Posting comments to PR…','info')
    try {
      const h={'Content-Type':'application/json'}; if(state.backendKey)h['X-API-Key']=state.backendKey
      const body={provider:state.provider,token:state.token,base_url:state.baseUrl||'',workspace:state.workspace||'',repo_slug:repoName(state.primaryRepo),pr_id:String(prId),inline:true}
      const resp=await fetch(`${state.backendUrl}/api/v1/report/${state.lastRequestId}/comment-pr`,{method:'POST',headers:h,body:JSON.stringify(body)})
      const d=await resp.json()
      if(d.ok)showToast(`✅ Posted to PR — ${d.files_commented} file comment(s) + summary`,'success')
      else showToast('Failed to post comments — check server logs','error')
    } catch(e){showToast(`Error: ${e.message}`,'error')}
  }

  return (
    <div>
      {/* Role badge bar */}
      {state.ciaaPerms&&(
        <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:10,padding:'8px 12px',background:`${state.ciaaPerms.role_color||'#1a56db'}08`,border:`1px solid ${state.ciaaPerms.role_color||'#1a56db'}22`,borderRadius:8}}>
          <span style={{fontSize:16}}>{state.ciaaPerms.can_comment?'🔍':'👨‍💻'}</span>
          <div>
            <span style={{fontWeight:700,fontSize:12,color:state.ciaaPerms.role_color||'#1a56db'}}>{state.ciaaPerms.role_label||'Developer'}</span>
            <span style={{fontSize:11,color:'#7a8494',marginLeft:6}}>{state.ciaaPerms.description||''}</span>
          </div>
          {!state.ciaaPerms.can_comment&&<span style={{marginLeft:'auto',fontSize:11,color:'#9fadbf',fontStyle:'italic'}}>Reviewer actions hidden — contact your lead for reviewer access</span>}
        </div>
      )}

      {/* Errors / fallback notices */}
      {!hasAgentData && <div style={{display:'flex',alignItems:'flex-start',gap:8,padding:'10px 14px',background:'#fff8ec',border:'1px solid #fad98a',borderRadius:8,marginBottom:14,fontSize:12,color:'#8a5200'}}><i className="ti ti-info-circle" style={{flexShrink:0,marginTop:1}}/><div><strong>Heuristic mode</strong> — agents used rule-based fallbacks.</div></div>}
      {(r.errors||[]).length>0&&<div style={{display:'flex',alignItems:'flex-start',gap:8,padding:'10px 14px',background:'#fff1f1',border:'1px solid #f8c0c0',borderRadius:8,marginBottom:14,fontSize:12,color:'#b81c1c'}}><i className="ti ti-alert-circle" style={{flexShrink:0,marginTop:1}}/><div><strong>Analysis errors:</strong> {(r.errors||[]).join(', ')}</div></div>}

      {/* Human review panel */}
      <HumanReviewPanel r={r} state={state} showToast={showToast}/>

      {/* Gate banner */}
      <div className={`gate-banner ${gCls}`}>
        <i className={`ti ${gIcon} gate-icon`}/>
        <div style={{flex:1}}>
          <div className="gate-title">{r.gate_decision} — {(r.overall_risk||'').toUpperCase()} RISK</div>
          <div className="gate-sub">{r.rationale||''}</div>
          <div style={{marginTop:6}}><span style={{fontSize:11,background:'#ffffff',border:'1px solid #e8eaed',borderRadius:12,padding:'2px 8px',color:'#7a8494',display:'inline-flex',alignItems:'center',gap:4}}>{modelBadge}</span></div>
        </div>
        <div style={{marginLeft:'auto',textAlign:'right',flexShrink:0}}>
          <div style={{fontSize:28,fontWeight:700,fontFamily:'var(--mono)'}}>{r.risk_score}</div>
          <div style={{fontSize:11,color:'#7a8494'}}>risk score /100</div>
        </div>
      </div>

      {/* Metrics grid */}
      <div className="metrics-grid">
        {[['Security findings',(r.security?.findings?.length||0)],['Secrets',r.security?.secrets_detected?'⚠ YES':'None'],['Blast radius',`${r.dependency?.blast_radius_score||0}/100`],['Breaking changes',(r.interface?.breaking_changes?.length||0)],['Coverage delta',`${((r.test_coverage?.coverage_delta||0)>0?'+':'')+parseFloat(r.test_coverage?.coverage_delta||0).toFixed(1)}%`],['QA scenarios',(r.qa_scenarios?.total_scenarios||0)],['Schema changes',(r.schema_change?.changes?.length||0)],['Total tokens',(r.token_usage||0).toLocaleString()],['Run time',r.duration_s?`${r.duration_s.toFixed(1)}s`:'—']].map(([l,v])=>(
          <div key={l} className="metric"><div className="metric-label">{l}</div><div className="metric-value">{v}</div></div>
        ))}
      </div>

      {/* Notice when a loaded report has no detailed agent data (seed/test data or failed run) */}
      {!hasAgentData && (
        <div className="info-msg" style={{marginBottom:12,display:'flex',alignItems:'center',gap:8}}>
          <i className="ti ti-info-circle"/>
          <span>This report has no detailed analysis data — it’s likely seed/test data or a run that didn’t complete. Run a fresh analysis, or remove demo data via <strong>Settings → Purge stored reports</strong>.</span>
        </div>
      )}

      {/* Suggested review order legend */}
      <div className="flow-legend">
        <i className="ti ti-route"/> Suggested review order:
        <span className="tab-step">1</span> Summary →
        <span className="tab-step">2</span> Security →
        <span className="tab-step">3</span> Impact →
        <span className="tab-step">4</span> Checklist →
        <span className="tab-step">5</span> Post to PR
      </div>

      {/* Result tabs */}
      <div className="tabs">
        {RESULT_TABS.map(g=>(
          <div key={g.group} className="tab-group">
            <span className="tab-group-label">{g.group}</span>
            <div className="tab-group-tabs">
              {g.tabs.map(t=>{
                const step = STEP_FOR_TAB[t.id]
                return (
                  <button key={t.id} className={`tab ${activeTab===t.id?'active':''}`} onClick={()=>switchTab(t.id)}
                    title={step?STEP_TITLE[step]:undefined}>
                    {step && <span className="tab-step">{step}</span>}{t.label}
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      {/* Findings search — shown on finding-heavy tabs */}
      {['security','dependency','interface','schema','performance','privacy','quality'].includes(activeTab) && (
        <div className="search-wrap" style={{marginBottom:12}}>
          <i className="ti ti-search"/>
          <input type="text" value={findingsSearch} onChange={e=>setFindingsSearch(e.target.value)}
            placeholder={`Search findings in ${activeTab}…`} />
          {findingsSearch && <button onClick={()=>setFindingsSearch('')}
            style={{position:'absolute',right:10,top:'50%',transform:'translateY(-50%)',background:'none',border:'none',cursor:'pointer',color:'#7a8494',fontSize:14}}>✕</button>}
        </div>
      )}

      <div ref={tabContentRef}>{renderTab(activeTab, findingsSearch)}</div>

      {/* Action bar */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:10,marginTop:16}}>
        <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
          <button className="btn btn-primary" onClick={()=>setShowReviewSummary(v=>!v)} id="btn-review-summary">
            <i className={`ti ${showReviewSummary?'ti-x':'ti-clipboard-text'}`}/>{showReviewSummary?'Close Summary':'Review Summary'}
          </button>
          <button className="btn" onClick={()=>setShowPRDesc(true)} title="Generate a PR description to paste into GitHub/Bitbucket">
            <i className="ti ti-file-description"/>PR Description
          </button>
          <button className="btn" onClick={()=>setActiveTab('checklist')} title="Open structured reviewer checklist">
            <i className="ti ti-list-check"/>Checklist
          </button>
          {canPostToGit(state) ? (
            <button className="btn" onClick={postPRComments} style={{background:'#f0fdf4',borderColor:'#86efac',color:'#166634'}} title={STEP_TITLE[5]}>
              <span className="tab-step">5</span><i className="ti ti-message-2-code"/>Post to PR
            </button>
          ) : (
            <button className="btn" style={{opacity:.45,cursor:'not-allowed'}} onClick={()=>showToast('Post to PR requires Reviewer role. Ask your tech lead to assign reviewer access.','error')} title="Posting PR comments requires Reviewer role">
              <span className="tab-step">5</span><i className="ti ti-lock"/>Post to PR
            </button>
          )}
        </div>
        <div style={{display:'flex',alignItems:'center',gap:8,flexWrap:'wrap'}}>
          <button className="btn" onClick={()=>{update({report:null,analysisRequested:false,selectedPR:null,sourceBranch:'',commitSha:''});showView('configure')}}><i className="ti ti-plus"/>New analysis</button>
          <button className="btn" onClick={()=>setShowJudgePanel(v=>!v)} id="btn-judge" title="Evaluate analysis quality with multiple LLM judges">
            <i className="ti ti-gavel"/>Judge panel
          </button>
          <button className="btn" onClick={()=>showToast(`Report ready. Backend: ${state.backendUrl||'not configured'}`, 'success')} title="Submit report to backend">
            <i className="ti ti-upload"/>Submit to backend
          </button>
          <button className="btn btn-icon" onClick={()=>document.getElementById('kb-hint')?.style.setProperty('display','flex')} title="Keyboard shortcuts"><i className="ti ti-keyboard"/></button>
        </div>
      </div>

      {/* Review Summary Panel */}
      {showReviewSummary && <ReviewSummaryPanel r={r} onClose={()=>setShowReviewSummary(false)}/>}

      {/* Judge Panel */}
      {showJudgePanel && <JudgePanel state={state} showToast={showToast}/>}

      {/* PR Description Modal */}
      {showPRDesc && <PRDescModal r={r} state={state} onClose={()=>setShowPRDesc(false)}/>}

      {/* Keyboard shortcuts help */}
      <div id="kb-hint" style={{display:'none',position:'fixed',bottom:20,right:20,background:'#1a2332',color:'#fff',borderRadius:10,padding:'14px 18px',fontSize:11,zIndex:8000,lineHeight:1.8,boxShadow:'0 4px 20px rgba(0,0,0,.3)'}}>
        <div style={{fontWeight:700,marginBottom:6}}>⌨️ Keyboard shortcuts</div>
        <div><kbd style={{background:'#2d3f55',padding:'1px 5px',borderRadius:3}}>← →</kbd> Previous / next tab</div>
        <div><kbd style={{background:'#2d3f55',padding:'1px 5px',borderRadius:3}}>P</kbd> Generate PR description</div>
        <div><kbd style={{background:'#2d3f55',padding:'1px 5px',borderRadius:3}}>C</kbd> Load reviewer checklist</div>
        <div><kbd style={{background:'#2d3f55',padding:'1px 5px',borderRadius:3}}>N</kbd> New analysis</div>
        <div><kbd style={{background:'#2d3f55',padding:'1px 5px',borderRadius:3}}>?</kbd> Toggle this help</div>
        <button onClick={()=>document.getElementById('kb-hint').style.display='none'} style={{marginTop:8,width:'100%',border:'1px solid #2d3f55',background:'none',color:'#9fadbf',borderRadius:6,padding:4,cursor:'pointer',fontSize:11}}>Close</button>
      </div>
    </div>
  )
}

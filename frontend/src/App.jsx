import { useState, useCallback, useEffect, useRef } from 'react'
import { AppProvider, useApp } from './AppContext'
import { scanUserInstructions } from './promptGuard'
import './index.css'

import ErrorBoundary from './components/ErrorBoundary'
import Sidebar from './components/Sidebar'
import Topbar from './components/Topbar'
import FooterBar from './components/FooterBar'
import Toast from './components/Toast'

import LauncherView from './views/LauncherView'
import ConfigureView from './views/ConfigureView'
import ReposView from './views/ReposView'
import TargetView from './views/TargetView'
import ResultsView from './views/ResultsView'
import HistoryView from './views/HistoryView'
import SettingsView from './views/SettingsView'
import QualityView from './views/QualityView'
import InsightsView from './views/InsightsView'
import AgentsView from './views/AgentsView'
import AdminView from './views/AdminView'

const STEP_VIEWS = ['configure', 'repos', 'target', 'results']
const ALL_VIEWS   = ['configure', 'repos', 'target', 'results', 'history', 'quality', 'insights', 'agents', 'settings', 'admin']

const PAGE_TITLES = {
  configure: 'Configure provider',
  repos:     'Repositories',
  target:    'Analysis target',
  results:   'Results',
  history:   'Past analyses',
  settings:  'Backend settings',
  quality:   'Quality metrics',
  insights:  'Team insights',
  agents:    'Analysis agents',
}

function useTheme() {
  const [dark, setDark] = useState(() => localStorage.getItem('theme') === 'dark')
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('theme', dark ? 'dark' : 'light')
  }, [dark])
  return [dark, setDark]
}

function AppShell() {
  const { state, update } = useApp()
  const [view, setViewRaw] = useState('configure')
  const [toast, setToast] = useState(null)
  const [dark, setDark] = useTheme()
  const prevView = useRef(null)

  // Which framework is open. null = show the home/launcher page.
  const [activeApp, setActiveApp] = useState(() => sessionStorage.getItem('activeApp') || null)
  const openFramework = useCallback((fw) => {
    setActiveApp(fw.app)
    sessionStorage.setItem('activeApp', fw.app)
  }, [])
  const goHome = useCallback(() => {
    setActiveApp(null)
    sessionStorage.removeItem('activeApp')
  }, [])

  const showToast = useCallback((msg, type = 'success') => {
    setToast({ msg, type, id: Date.now() })
    setTimeout(() => setToast(null), 3500)
  }, [])

  // Page title + content div scroll-to-top on view change
  const showView = useCallback((name) => {
    prevView.current = name
    setViewRaw(name)
    document.title = `${PAGE_TITLES[name] || name} — GTO Pull Request Review Framework`
    // Scroll the active content pane to top
    requestAnimationFrame(() => {
      const el = document.getElementById(`view-content-${name}`)
      if (el) el.scrollTop = 0
    })
  }, [])

  // Initialise page title
  useEffect(() => {
    document.title = `${PAGE_TITLES[view] || view} — GTO Pull Request Review Framework`
  }, [])

  const goNext = useCallback(() => {
    const cur = STEP_VIEWS.indexOf(view)
    if (cur === 2) {
      if (!canProceedFromTarget(state)) {
        showToast(
          scanUserInstructions(state.userInstructions||'').blocked
            ? 'Fix the flagged analysis priorities text before running.'
            : 'Please select a pull request, branch pair, or commit SHA first.',
          'warn'
        )
        return
      }
      // runNonce forces RunningView to remount (fresh refs) so a 2nd run always
      // re-fires runAnalysis — even if the previous run was left mid-flight.
      update({ report: null, analysisRequested: true, runNonce: Date.now() })
      showView('results')
      return
    }
    if (cur >= 0 && cur < 2) showView(STEP_VIEWS[cur + 1])
  }, [view, state, update, showView, showToast])

  const goBack = useCallback(() => {
    const cur = STEP_VIEWS.indexOf(view)
    if (cur > 0) showView(STEP_VIEWS[cur - 1])
  }, [view, showView])

  // Global keyboard shortcuts
  useEffect(() => {
    const handler = (e) => {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      if (e.metaKey || e.ctrlKey) return

      // Number keys 1-9: jump to views
      if (!e.altKey && e.key >= '1' && e.key <= '9') {
        const idx = parseInt(e.key) - 1
        if (ALL_VIEWS[idx]) showView(ALL_VIEWS[idx])
        return
      }

      if (e.key === '?') {
        const el = document.getElementById('kb-hint')
        if (el) el.style.display = el.style.display === 'flex' ? 'none' : 'flex'
      }
      if ((e.key === 'p' || e.key === 'P') && view === 'results')
        document.dispatchEvent(new CustomEvent('ciaa:showPRDesc'))
      if (e.key === 'n' || e.key === 'N') {
        update({ report: null, analysisRequested: false, selectedPR: null, sourceBranch: '', commitSha: '' })
        showView('configure')
      }
      if (e.key === 'd' || e.key === 'D') setDark(v => !v)
      if (e.key === 'Escape') {
        const el = document.getElementById('kb-hint')
        if (el) el.style.display = 'none'
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [view, update, showView, setDark])

  const stepIdx   = STEP_VIEWS.indexOf(view)
  const showFooter = stepIdx >= 0 && stepIdx < 3
  const viewProps  = { showView, showToast }

  // Home / launcher page — shown until a framework is opened.
  if (!activeApp) {
    return <LauncherView onOpen={openFramework} dark={dark} setDark={setDark} />
  }

  return (
    <div className="shell">
      <Sidebar view={view} showView={showView} state={state} dark={dark} setDark={setDark} goHome={goHome} />
      <div className="main">
        <Topbar view={view} state={state} showView={showView} dark={dark} setDark={setDark} />

        {ALL_VIEWS.map(v => (
          <div
            key={v}
            id={`view-content-${v}`}
            className={`content${view === v ? ' view-enter' : ''}`}
            style={{ display: view === v ? 'block' : 'none' }}
          >
            {v === 'configure' && <ConfigureView active={view === 'configure'} {...viewProps} />}
            {v === 'repos'     && <ReposView     active={view === 'repos'}     {...viewProps} />}
            {v === 'target'    && <TargetView    active={view === 'target'}    {...viewProps} goNext={goNext} />}
            {v === 'results'   && <ResultsView   active={view === 'results'}   {...viewProps} />}
            {v === 'history'   && <HistoryView   active={view === 'history'}   {...viewProps} />}
            {v === 'settings'  && <SettingsView  active={view === 'settings'}  {...viewProps} />}
            {v === 'quality'   && <QualityView   active={view === 'quality'}   {...viewProps} />}
            {v === 'insights'  && <InsightsView  active={view === 'insights'}  {...viewProps} />}
            {v === 'agents'    && <AgentsView    active={view === 'agents'}    {...viewProps} />}
            {v === 'admin'     && <AdminView     active={view === 'admin'}     {...viewProps} />}
          </div>
        ))}

        {showFooter && <FooterBar view={view} state={state} goBack={goBack} goNext={goNext} />}
      </div>

      {toast && <Toast key={toast.id} msg={toast.msg} type={toast.type} />}
    </div>
  )
}

function canProceedFromTarget(state) {
  if (scanUserInstructions(state.userInstructions||'').blocked) return false
  if (state.targetType === 'pr')     return !!state.selectedPR
  if (state.targetType === 'branch') return !!(state.sourceBranch && state.targetBranch && state.sourceBranch !== state.targetBranch)
  if (state.targetType === 'commit') return state.commitSha.length >= 5
  return false
}

export default function App() {
  return (
    <ErrorBoundary>
      <AppProvider>
        <AppShell />
      </AppProvider>
    </ErrorBoundary>
  )
}

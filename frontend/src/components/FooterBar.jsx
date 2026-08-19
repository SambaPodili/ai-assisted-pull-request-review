import { useApp } from '../AppContext'
import { scanUserInstructions } from '../promptGuard'

const STEP_VIEWS = ['configure', 'repos', 'target', 'results']

function canProceedFromTarget(state) {
  if (scanUserInstructions(state.userInstructions||'').blocked) return false
  if (state.targetType === 'pr') return !!state.selectedPR
  if (state.targetType === 'branch') return !!(state.sourceBranch && state.targetBranch && state.sourceBranch !== state.targetBranch)
  if (state.targetType === 'commit') return state.commitSha.length >= 5
  return false
}

export default function FooterBar({ view, state, goBack, goNext }) {
  const cur = STEP_VIEWS.indexOf(view)
  const isTarget = cur === 2
  const canProceed = isTarget ? canProceedFromTarget(state) : true
  const priorityBlocked = isTarget && scanUserInstructions(state.userInstructions||'').blocked

  return (
    <div className="footer-bar">
      <button className="btn" onClick={goBack} disabled={cur <= 0}>
        <i className="ti ti-arrow-left" /> Back
      </button>
      <button
        className="btn btn-primary"
        onClick={goNext}
        disabled={isTarget && !canProceed}
        style={isTarget && canProceed ? { background: '#059669', borderColor: '#059669' } : {}}
        title={isTarget ? (canProceed ? 'Fetch diff and run all 20 agents' : priorityBlocked ? 'Fix the flagged analysis priorities text before running' : 'Select a PR, branch pair, or commit SHA first') : ''}
      >
        {isTarget ? <><i className="ti ti-player-play" /> Run Analysis</> : <>Continue <i className="ti ti-arrow-right" /></>}
      </button>
    </div>
  )
}

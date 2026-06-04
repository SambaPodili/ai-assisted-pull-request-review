const STEPS = ['Connect', 'Repositories', 'Target', 'Results']

export default function StepsRow({ active }) {
  return (
    <div className="steps-row">
      {STEPS.map((s, i) => {
        const cls = i < active ? 'done' : i === active ? 'active' : ''
        return (
          <span key={i} style={{ display: 'contents' }}>
            <div className="step-item">
              <div className={`step-circle ${cls}`}>
                {i < active ? <i className="ti ti-check" style={{ fontSize: 12 }} /> : i + 1}
              </div>
              <span className={`step-label ${cls}`}>{s}</span>
            </div>
            {i < STEPS.length - 1 && <div className={`step-line ${i < active ? 'done' : ''}`} />}
          </span>
        )
      })}
    </div>
  )
}

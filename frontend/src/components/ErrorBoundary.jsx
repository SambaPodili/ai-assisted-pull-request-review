import { Component } from 'react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('[CIAA ErrorBoundary]', error, info)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        height: '100vh', padding: 40, background: '#f7f8fa', textAlign: 'center'
      }}>
        <div style={{ fontSize: 48, marginBottom: 16 }}>⚠️</div>
        <h2 style={{ fontFamily: 'Instrument Serif,serif', fontSize: 24, color: '#0d1117', marginBottom: 8 }}>
          Something went wrong
        </h2>
        <p style={{ fontSize: 14, color: '#7a8494', maxWidth: 480, lineHeight: 1.6, marginBottom: 24 }}>
          An unexpected error occurred. Your settings and history are safe in localStorage.
        </p>
        <details style={{ marginBottom: 24, textAlign: 'left', maxWidth: 600, width: '100%' }}>
          <summary style={{ cursor: 'pointer', fontSize: 12, color: '#7a8494', marginBottom: 8 }}>
            Error details
          </summary>
          <pre style={{
            background: '#fff1f1', border: '1px solid #f8c0c0', borderRadius: 8,
            padding: '12px 16px', fontSize: 11, color: '#b81c1c', overflowX: 'auto',
            whiteSpace: 'pre-wrap', wordBreak: 'break-all', fontFamily: 'JetBrains Mono,monospace'
          }}>
            {String(this.state.error)}
          </pre>
        </details>
        <div style={{ display: 'flex', gap: 10 }}>
          <button
            className="btn btn-primary"
            onClick={() => { this.setState({ error: null }); window.location.reload() }}
          >
            <i className="ti ti-refresh" /> Reload app
          </button>
          <button
            className="btn"
            onClick={() => this.setState({ error: null })}
          >
            <i className="ti ti-arrow-back-up" /> Try to recover
          </button>
        </div>
      </div>
    )
  }
}

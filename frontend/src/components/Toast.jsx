import { useState, useEffect, useCallback, createContext, useContext } from 'react'

// ── Toast context (so any component can fire toasts) ──────────────────────────
const ToastCtx = createContext(null)
export function useToast() { return useContext(ToastCtx) }

const ICONS = {
  success: 'ti-circle-check',
  error:   'ti-alert-circle',
  info:    'ti-info-circle',
  warn:    'ti-alert-triangle',
}
const COLORS = {
  success: { bg: '#edfaf3', border: '#b5e8cf', icon: '#0c7c4b', text: '#0c7c4b' },
  error:   { bg: '#fff1f1', border: '#f8c0c0', icon: '#b81c1c', text: '#b81c1c' },
  info:    { bg: '#eff5ff', border: '#c2d6fe', icon: '#1a56db', text: '#1a56db' },
  warn:    { bg: '#fff8ec', border: '#fad98a', icon: '#8a5200', text: '#8a5200' },
}

function ToastItem({ id, msg, type = 'success', onDismiss }) {
  const [visible, setVisible] = useState(false)
  const [leaving, setLeaving] = useState(false)
  const c = COLORS[type] || COLORS.success
  const icon = ICONS[type] || ICONS.success

  useEffect(() => {
    // Slide in
    const t = setTimeout(() => setVisible(true), 10)
    return () => clearTimeout(t)
  }, [])

  function dismiss() {
    setLeaving(true)
    setTimeout(() => onDismiss(id), 280)
  }

  // Auto-dismiss after 3.5s
  useEffect(() => {
    const t = setTimeout(dismiss, 3500)
    return () => clearTimeout(t)
  }, [id])

  return (
    <div
      onClick={dismiss}
      style={{
        display: 'flex', alignItems: 'flex-start', gap: 10,
        padding: '11px 14px 11px 12px',
        background: c.bg, border: `1px solid ${c.border}`,
        borderRadius: 10, cursor: 'pointer', userSelect: 'none',
        boxShadow: '0 4px 16px rgba(0,0,0,.10)',
        maxWidth: 380, width: '100%',
        transition: 'transform 0.28s cubic-bezier(.34,1.56,.64,1), opacity 0.28s ease',
        transform: visible && !leaving ? 'translateX(0) scale(1)' : 'translateX(40px) scale(0.95)',
        opacity: visible && !leaving ? 1 : 0,
        position: 'relative', overflow: 'hidden',
      }}
    >
      <i className={`ti ${icon}`} style={{ fontSize: 16, color: c.icon, flexShrink: 0, marginTop: 1 }} />
      <span style={{ flex: 1, fontSize: 13, fontWeight: 500, color: c.text, lineHeight: 1.4 }}>
        {msg}
      </span>
      <button
        onClick={e => { e.stopPropagation(); dismiss() }}
        style={{ background: 'none', border: 'none', cursor: 'pointer', color: c.icon, fontSize: 14, padding: 0, flexShrink: 0, opacity: 0.6, lineHeight: 1 }}
      >✕</button>
      {/* Progress bar */}
      <div style={{
        position: 'absolute', bottom: 0, left: 0, height: 2,
        background: c.icon, borderRadius: '0 0 10px 10px',
        animation: 'toast-progress 3.5s linear forwards',
      }} />
    </div>
  )
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const showToast = useCallback((msg, type = 'success') => {
    const id = Date.now() + Math.random()
    setToasts(t => [...t.slice(-4), { id, msg, type }]) // max 5 stacked
  }, [])

  const dismiss = useCallback((id) => {
    setToasts(t => t.filter(x => x.id !== id))
  }, [])

  return (
    <ToastCtx.Provider value={showToast}>
      {children}
      <div style={{
        position: 'fixed', bottom: 24, right: 24,
        display: 'flex', flexDirection: 'column', gap: 8,
        zIndex: 99999, pointerEvents: 'none',
      }}>
        {toasts.map(t => (
          <div key={t.id} style={{ pointerEvents: 'auto' }}>
            <ToastItem {...t} onDismiss={dismiss} />
          </div>
        ))}
      </div>
      <style>{`
        @keyframes toast-progress {
          from { width: 100%; }
          to   { width: 0%; }
        }
      `}</style>
    </ToastCtx.Provider>
  )
}

// Legacy compat: standalone <Toast> for existing callers that pass msg/type directly
export default function Toast({ msg, type = 'success' }) {
  const c = COLORS[type] || COLORS.success
  const icon = ICONS[type] || ICONS.success
  return (
    <div style={{
      position: 'fixed', bottom: 24, left: '50%', transform: 'translateX(-50%)',
      display: 'flex', alignItems: 'center', gap: 8,
      padding: '10px 18px', borderRadius: 10, fontSize: 13, fontWeight: 600,
      zIndex: 99999, background: c.bg, color: c.text,
      border: `1px solid ${c.border}`, pointerEvents: 'none',
      boxShadow: '0 4px 16px rgba(0,0,0,.12)', whiteSpace: 'nowrap',
    }}>
      <i className={`ti ${icon}`} style={{ fontSize: 16 }} />
      {msg}
    </div>
  )
}

import { useState, useEffect } from 'react'
import { backendBase } from '../api'

/* global __BUILD_VERSION__, __BUILD_TIME__ */
const FE_VERSION = typeof __BUILD_VERSION__ !== 'undefined' ? __BUILD_VERSION__ : 'dev'
const FE_BUILT   = typeof __BUILD_TIME__ !== 'undefined' ? __BUILD_TIME__ : null

/**
 * Tiny build-stamp pill shown in the top bar.
 *  • Shows the frontend build version (baked in at `vite build`).
 *  • Polls the backend /live endpoint for its version.
 *  • Turns amber with a ⚠ when the backend version differs from the frontend
 *    (i.e. one side is stale / not restarted), so "is it the latest build?"
 *    is answerable at a glance.
 */
export default function BuildBadge({ state }) {
  const [beVersion, setBeVersion] = useState(null)
  const [reachable, setReachable] = useState(null) // null=unknown, true/false

  useEffect(() => {
    let cancelled = false
    async function check() {
      try {
        const r = await fetch(backendBase(state) + '/live', { signal: AbortSignal.timeout(5000) })
        if (!r.ok) throw new Error('bad status')
        const d = await r.json()
        if (cancelled) return
        setBeVersion(d.version || '?')
        setReachable(true)
      } catch {
        if (cancelled) return
        setBeVersion(null)
        setReachable(false)
      }
    }
    check()
    const t = setInterval(check, 30000) // re-check every 30s
    return () => { cancelled = true; clearInterval(t) }
  }, [state.backendUrl])

  const mismatch = reachable && beVersion && beVersion !== FE_VERSION
  const builtLocal = FE_BUILT ? new Date(FE_BUILT).toLocaleString() : 'unknown'

  let bg = '#eef2f7', border = '#e8eaed', color = '#7a8494', icon = 'ti-package'
  let title = `Frontend build v${FE_VERSION} (built ${builtLocal})`

  if (reachable === false) {
    icon = 'ti-plug-off'
    title = `Frontend v${FE_VERSION} • backend unreachable at ${backendBase(state)}`
  } else if (mismatch) {
    bg = '#fff8e6'; border = '#f0c000'; color = '#9a6a00'; icon = 'ti-alert-triangle'
    title = `Version mismatch — restart the stale side.\nFrontend: v${FE_VERSION} (built ${builtLocal})\nBackend:  v${beVersion}`
  } else if (reachable && beVersion) {
    bg = '#e9f7ee'; border = '#3fb95055'; color = '#1a7f3c'; icon = 'ti-circle-check'
    title = `Frontend & backend both on v${FE_VERSION} (latest) • FE built ${builtLocal}`
  }

  return (
    <span
      title={title}
      style={{
        display: 'flex', alignItems: 'center', gap: 4,
        background: bg, border: `1px solid ${border}`, color,
        borderRadius: 6, padding: '3px 8px', fontSize: 11, fontWeight: 700,
        whiteSpace: 'nowrap', cursor: 'default',
      }}
    >
      <i className={`ti ${icon}`} style={{ fontSize: 13 }} />
      v{FE_VERSION}
      {mismatch && <span style={{ fontWeight: 600, opacity: 0.85 }}>≠ be v{beVersion}</span>}
    </span>
  )
}

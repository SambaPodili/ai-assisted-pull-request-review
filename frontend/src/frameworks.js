/* global __BUILD_VERSION__ */
const VERSION = typeof __BUILD_VERSION__ !== 'undefined' ? __BUILD_VERSION__ : 'dev'

/**
 * Registry of frameworks shown on the home/launcher page.
 *
 * To add a NEW framework later, append an object here:
 *   • status: 'active'   → opens inside this app (set `app: '<id>'`)
 *   • status: 'external' → opens `href` in a new tab
 *   • status: 'soon'     → shown greyed-out as "Coming soon"
 *
 * The `app` id is matched in App.jsx to decide which shell to mount. Today
 * only 'ciaa' exists; future frameworks can mount their own shell or link out.
 */
export const FRAMEWORKS = [
  {
    id: 'ciaa',
    app: 'ciaa',
    status: 'active',
    title: 'Code Analysis & Review',
    tagline: 'Multi-agent PR review, risk gating & business-impact mapping',
    description:
      'Analyse a PR, branch diff, or commit across 20 specialised agents — '
      + 'security, breaking changes, schema/data risk, test coverage, dependency '
      + 'CVEs and more — with a deterministic, auditable gate decision.',
    icon: 'microscope',
    accent: '#3b82f6',
    tags: ['Code review', 'Risk gate', 'Security', 'Banking-aligned'],
    version: VERSION,
  },
  // ── Example template for a future framework (kept disabled) ──────────────────
  // {
  //   id: 'data-lineage',
  //   status: 'soon',
  //   title: 'Data Lineage Framework',
  //   tagline: 'Trace data flow & PII across services',
  //   description: 'Map how data moves between systems and where it is transformed.',
  //   icon: 'ti-share',
  //   accent: '#10b981',
  //   tags: ['Lineage', 'PII', 'Governance'],
  // },
]

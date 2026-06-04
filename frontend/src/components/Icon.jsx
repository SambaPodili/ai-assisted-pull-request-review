/**
 * Tiny inline-SVG icon set (Tabler-style, 24×24, currentColor stroke).
 * Used where the Tabler webfont may not load (e.g. the launcher / offline).
 * Add new glyphs to PATHS as needed; unknown names fall back to `box`.
 */
const PATHS = {
  'arrow-right': <><path d="M5 12h14" /><path d="M13 6l6 6l-6 6" /></>,
  plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  sun: <><circle cx="12" cy="12" r="4" /><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4" /></>,
  moon: <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />,
  sparkles: <path d="M12 3l1.8 4.6L18.4 9.4l-4.6 1.8L12 15.8l-1.8-4.6L5.6 9.4l4.6-1.8z M19 14l.7 1.8l1.8.7l-1.8.7L19 19l-.7-1.8L16.5 16.5l1.8-.7z" />,
  stack: <><path d="M12 4l9 5l-9 5l-9-5z" /><path d="M3 14l9 5l9-5" /></>,
  microscope: <><path d="M5 21h14" /><path d="M6 18h6" /><path d="M9 18a6 6 0 0 0 6-6" /><path d="M11 5l3.5 3.5" /><path d="M13.5 2.5a2.1 2.1 0 0 1 3 3l-6 6l-3-3z" /></>,
  shield: <path d="M12 3l7 3v5c0 4.5-3 7.5-7 9c-4-1.5-7-4.5-7-9V6z" />,
  database: <><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6" /><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /></>,
  share: <><circle cx="6" cy="12" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="18" cy="18" r="2.5" /><path d="M8.2 10.8l7.6-3.6M8.2 13.2l7.6 3.6" /></>,
  cpu: <><rect x="6" y="6" width="12" height="12" rx="2" /><rect x="9" y="9" width="6" height="6" /><path d="M9 3v2M15 3v2M9 19v2M15 19v2M3 9h2M3 15h2M19 9h2M19 15h2" /></>,
  box: <><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" /><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5" /></>,
}

export default function Icon({ name, size = 24, strokeWidth = 1.8, style, className }) {
  const path = PATHS[name] || PATHS.box
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={style}
      aria-hidden="true"
    >
      {path}
    </svg>
  )
}

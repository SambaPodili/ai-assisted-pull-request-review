import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Self-hosted Tabler icon webfont (outline + filled) — bundled into the build so
// icons render with NO CDN / network access (air-gapped & corporate-proxy safe).
import '@tabler/icons-webfont/dist/tabler-icons.min.css'
// Self-hosted text fonts (was Google Fonts CDN) — same offline reason. Only the
// weights the UI actually uses are imported.
import '@fontsource/instrument-sans/400.css'
import '@fontsource/instrument-sans/500.css'
import '@fontsource/instrument-sans/600.css'
import '@fontsource/instrument-sans/400-italic.css'
import '@fontsource/instrument-serif/400.css'
import '@fontsource/instrument-serif/400-italic.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import './index.css'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

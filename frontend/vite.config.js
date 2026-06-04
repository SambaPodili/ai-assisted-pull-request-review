import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build stamp — surfaced in the UI so operators can confirm the running
// frontend is on the latest build (and matches the backend). __BUILD_TIME__
// changes on every build, so a stale dev server / cached bundle is obvious.
const BUILD_VERSION = process.env.BUILD_VERSION || '2.4.0'
const BUILD_TIME = new Date().toISOString()

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  define: {
    __BUILD_VERSION__: JSON.stringify(BUILD_VERSION),
    __BUILD_TIME__: JSON.stringify(BUILD_TIME),
  },
})

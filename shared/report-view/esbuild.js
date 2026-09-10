// Builds the browser-ready global-script bundle IntelliJ's JCEF side loads
// (VS Code doesn't need this — it imports src/index.ts directly at bundle
// time via its own esbuild). Produces dist/report-view.iife.js exposing
// `window.GtoReportView`.
const esbuild = require('esbuild');

esbuild
  .build({
    entryPoints: ['src/index.ts'],
    bundle: true,
    outfile: 'dist/report-view.iife.js',
    format: 'iife',
    globalName: 'GtoReportView',
    target: ['es2020'],
    minify: process.argv.includes('--production'),
    sourcemap: !process.argv.includes('--production'),
  })
  .then(() => console.log('[report-view] built dist/report-view.iife.js'))
  .catch(() => process.exit(1));

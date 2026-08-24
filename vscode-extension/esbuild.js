// Bundles src/extension.ts -> dist/extension.js as a CommonJS VS Code extension.
// `vscode` is provided by the extension host at runtime, never bundled.
const esbuild = require('esbuild');

const production = process.argv.includes('--production');
const watch = process.argv.includes('--watch');

// Emits the exact "[watch] build started/finished" markers VS Code's
// background-task problem matcher (.vscode/tasks.json) waits on before it
// considers the pre-launch build task ready and lets F5 proceed.
const watchLogPlugin = {
  name: 'watch-log',
  setup(build) {
    build.onStart(() => console.log('[watch] build started'));
    build.onEnd((result) => {
      result.errors.forEach((e) =>
        console.error(`✘ [ERROR] ${e.text}\n    ${e.location?.file}:${e.location?.line}:${e.location?.column}`)
      );
      console.log('[watch] build finished');
    });
  },
};

async function main() {
  const ctx = await esbuild.context({
    entryPoints: ['src/extension.ts'],
    bundle: true,
    format: 'cjs',
    platform: 'node',
    target: 'node18',
    outfile: 'dist/extension.js',
    external: ['vscode'],
    sourcemap: !production,
    minify: production,
    logLevel: 'silent',
    plugins: [watchLogPlugin],
  });
  if (watch) {
    await ctx.watch();
  } else {
    await ctx.rebuild();
    await ctx.dispose();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});

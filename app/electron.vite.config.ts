import { resolve } from 'node:path'

import { defineConfig } from 'electron-vite'

import { alias, appRoot, assetsInlineLimit, rendererPages, rendererPlugins, rendererRoot } from './config/vite-shared'

export default defineConfig({
  main: {
    resolve: { alias },
    build: {
      outDir: resolve(appRoot, 'out/main'),
      rolldownOptions: { input: { index: resolve(appRoot, 'src/main/index.ts') } },
    },
  },
  preload: {
    resolve: { alias },
    build: {
      outDir: resolve(appRoot, 'out/preload'),
      // Sandboxed preloads cannot load ESM or sibling chunks: one standalone CommonJS file per entry,
      // with every dependency (e.g. @mozilla/readability) bundled in.
      isolatedEntries: true,
      externalizeDeps: false,
      rolldownOptions: {
        input: {
          index: resolve(appRoot, 'src/preload/index.ts'),
          web: resolve(appRoot, 'src/preload/web.ts'),
        },
        output: { format: 'cjs', entryFileNames: '[name].cjs' },
      },
    },
  },
  renderer: {
    root: rendererRoot,
    resolve: { alias },
    plugins: rendererPlugins(),
    server: { port: 5173, strictPort: true, host: '127.0.0.1' },
    build: {
      outDir: resolve(appRoot, 'out/renderer'),
      minify: true,
      assetsInlineLimit,
      rolldownOptions: { input: rendererPages },
    },
  },
})

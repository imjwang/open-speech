/**
 * Serves (and builds) the renderer pages in a plain browser, for UI tests against a real engine:
 *   bun run dev:web  →  http://127.0.0.1:5173/commandbar/index.html?engine=http://127.0.0.1:<port>&token=<t>
 *                       http://127.0.0.1:5173/reader/index.html?engine=…&token=…&doc=<doc_id>
 * The engine must allow this origin: OPEN_SPEECH_ALLOWED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173
 */
import { resolve } from 'node:path'

import { defineConfig } from 'vite'

import { alias, appRoot, assetsInlineLimit, rendererPages, rendererPlugins, rendererRoot } from './config/vite-shared'

export default defineConfig({
  root: rendererRoot,
  resolve: { alias },
  plugins: rendererPlugins(),
  server: { port: 5173, strictPort: true, host: '127.0.0.1' },
  preview: { port: 5173, strictPort: true, host: '127.0.0.1' },
  build: {
    outDir: resolve(appRoot, 'out/web'),
    emptyOutDir: true,
    assetsInlineLimit,
    rolldownOptions: { input: { index: resolve(rendererRoot, 'index.html'), ...rendererPages } },
  },
})

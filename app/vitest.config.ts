import { resolve } from 'node:path'

import babel from '@rolldown/plugin-babel'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

const alias = {
  '@shared': resolve(import.meta.dirname, 'src/shared'),
  '@': resolve(import.meta.dirname, 'src/renderer/shared'),
}

export default defineConfig({
  resolve: { alias },
  test: {
    projects: [
      {
        resolve: { alias },
        test: {
          name: 'node',
          environment: 'node',
          include: ['src/main/**/*.test.ts', 'src/shared/**/*.test.ts', 'src/preload/**/*.test.ts'],
        },
      },
      {
        resolve: { alias },
        plugins: [react(), babel({ presets: [reactCompilerPreset()] })],
        test: {
          name: 'dom',
          environment: 'jsdom',
          include: ['src/renderer/**/*.test.{ts,tsx}'],
          setupFiles: ['test/setup-dom.ts'],
        },
      },
    ],
  },
})

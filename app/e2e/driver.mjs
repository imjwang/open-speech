// UI test driver for the Open Speech shell: drives the real Electron windows over Playwright and takes
// commands as JSON over HTTP, so a test session can click, type, screenshot and inspect step by step.
//
//   node e2e/driver.mjs <path to app/>            # dev build: launches out/main/index.js with Electron
//   PACKAGED_APP=".../Open Speech.app/Contents/MacOS/Open Speech" node e2e/driver.mjs <app/>
//   CDP_URL=http://127.0.0.1:9223 node e2e/driver.mjs <app/>   # attach to an app started with
//                                                              # --remote-debugging-port=9223 (packaged)
// Useful env for the app: OPEN_SPEECH_ENGINE_URL / OPEN_SPEECH_ENGINE_TOKEN (attach to a running engine),
// OPEN_SPEECH_BAR_KEEP_OPEN=1, OPEN_SPEECH_HOME (fresh profile), OPEN_SPEECH_AUDIO_OUTPUT=null (silent).
//
// Commands (POST JSON to http://127.0.0.1:9333):
//   {op:"windows"} · {op:"shot", page:"commandbar"|"reader"|"web", file} · {op:"eval", page, js}
//   {op:"main", js}   main-process JS with electron in scope (not available over CDP)
//   {op:"type", text} · {op:"press", key} · {op:"click", selector} · {op:"choose", selector, file}
//   {op:"aria", selector} · {op:"axe", page} · {op:"dark", on} · {op:"logs"}
//   {op:"fakemic"}    replace getUserMedia with a synthetic tone stream (never touches the real mic)
//   {op:"voicemic"}   a microphone that is silent until {op:"say", file:"x.wav"} plays a recording into it
// Before opening the bar, make windows non-focusable so the bar never takes the user's keystrokes:
//   {op:"main", js:"BrowserWindow.getAllWindows().forEach(w=>w.setFocusable(false)); return 1"}
// The bar exposes a read-only, content-free handle: window.__openSpeechBar.getState() and .log
import http from 'node:http'
import path from 'node:path'
import { createRequire } from 'node:module'

const APP = process.argv[2]
const require = createRequire(path.join(APP, 'package.json'))
const { _electron: electron } = require('playwright-core')

const logs = []
const PACKAGED = process.env.PACKAGED_APP
const CDP = process.env.CDP_URL
let app
if (CDP) {
  const { chromium } = require('playwright-core')
  const browser = await chromium.connectOverCDP(CDP)
  app = {
    windows: () => browser.contexts().flatMap((c) => c.pages()),
    evaluate: async () => { throw new Error('main-process eval unavailable over CDP') },
    on: (ev, fn) => browser.contexts().forEach((c) => c.on(ev === 'window' ? 'page' : ev, fn)),
    process: () => ({ stdout: { on() {} }, stderr: { on() {} } }),
  }
} else app = await electron.launch({
  executablePath: PACKAGED || require('electron'),
  args: PACKAGED ? [] : [path.join(APP, 'out/main/index.js')],
  cwd: APP,
  env: { ...process.env },
})
app.process().stdout.on('data', (d) => logs.push('[main] ' + d.toString().trim()))
app.process().stderr.on('data', (d) => logs.push('[main!] ' + d.toString().trim()))
app.on('window', (p) => {
  p.on('console', (m) => logs.push(`[${p.url().split('/').slice(-2, -1)[0]}:${m.type()}] ${m.text()}`))
  p.on('pageerror', (e) => logs.push(`[pageerror] ${e.message}`))
})

function pageFor(name) {
  const ws = app.windows()
  if (name === 'web') return ws.find((w) => w.url().startsWith('http')) ?? null
  return ws.find((w) => w.url().includes(`/${name}/`)) ?? null
}

async function handle(cmd) {
  switch (cmd.op) {
    case 'windows':
      return app.windows().map((w) => w.url())
    case 'main':
      return await app.evaluate(new Function('electron', `const {app, BrowserWindow, webContents, screen} = electron; return (async () => { ${cmd.js} })()`))
    case 'logs': {
      const out = logs.splice(0)
      return out
    }
  }
  const p = pageFor(cmd.page ?? 'commandbar')
  if (!p) throw new Error(`no window for ${cmd.page}`)
  switch (cmd.op) {
    case 'shot':
      await p.screenshot({ path: cmd.file, animations: 'disabled' })
      return cmd.file
    case 'eval':
      return await p.evaluate(cmd.js)
    case 'press':
      await p.keyboard.press(cmd.key)
      return 'ok'
    case 'type':
      await p.keyboard.type(cmd.text, { delay: cmd.delay ?? 15 })
      return 'ok'
    case 'click':
      await p.click(cmd.selector, { timeout: 5000 })
      return 'ok'
    case 'choose': {
      const [fc] = await Promise.all([p.waitForEvent('filechooser', { timeout: 5000 }), p.click(cmd.selector, { timeout: 5000 })])
      await fc.setFiles(cmd.file)
      return 'ok'
    }
    case 'fakemic': {
      // Replace getUserMedia with a synthetic "speech" stream: 1.2 s tone bursts, 1.2 s silences.
      await p.addInitScript(() => {
        navigator.mediaDevices.getUserMedia = async () => {
          const ctx = new AudioContext()
          const osc = ctx.createOscillator()
          osc.frequency.value = 220
          const gain = ctx.createGain()
          gain.gain.value = 0
          const dest = ctx.createMediaStreamDestination()
          osc.connect(gain).connect(dest)
          osc.start()
          let on = false
          setInterval(() => { on = !on; gain.gain.setValueAtTime(on ? 0.35 : 0, ctx.currentTime) }, 1200)
          return dest.stream
        }
      })
      await p.reload()
      return 'ok'
    }
    case 'voicemic': {
      // A microphone that is silent until {op:"say", file} plays a recording into it: spoken tests that never
      // touch the real microphone or the speakers (make the recording with `say -o file.wav ...`).
      await p.addInitScript(() => {
        let ctx = null
        let dest = null
        const context = () => (ctx ??= new AudioContext())
        navigator.mediaDevices.getUserMedia = async () => {
          // A fresh destination per request: the app stops the tracks of the stream it was given.
          dest = context().createMediaStreamDestination()
          const hush = context().createGain()
          hush.gain.value = 0
          const osc = context().createOscillator()
          osc.connect(hush).connect(dest)
          osc.start()
          return dest.stream
        }
        window.__sayIntoMic = async (b64) => {
          if (!dest) throw new Error('the microphone is not open')
          await context().resume()
          const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
          const buffer = await context().decodeAudioData(bytes.buffer)
          const source = context().createBufferSource()
          source.buffer = buffer
          source.connect(dest)
          source.start()
          return new Promise((resolve) => (source.onended = () => resolve(buffer.duration)))
        }
      })
      await p.reload()
      return 'ok'
    }
    case 'say': {
      const fs = await import('node:fs')
      const b64 = fs.readFileSync(cmd.file).toString('base64')
      return await p.evaluate((b) => window.__sayIntoMic(b), b64)
    }
    case 'dark':
      await p.emulateMedia({ colorScheme: cmd.on === false ? 'light' : 'dark' })
      return 'ok'
    case 'hover':
      await p.hover(cmd.selector, { timeout: 5000 })
      return 'ok'
    case 'aria':
      return await p.locator(cmd.selector ?? 'body').ariaSnapshot()
    case 'axe': {
      const fs = await import('node:fs')
      await p.evaluate(fs.readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8') + ';0')
      return await p.evaluate(async () => {
        // eslint-disable-next-line no-undef
        const r = await axe.run(document, { resultTypes: ['violations'] })
        return r.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 4).map((n) => n.target.join(' ') + ' :: ' + n.failureSummary) }))
      })
    }
  }
  throw new Error('unknown op ' + cmd.op)
}

http
  .createServer(async (req, res) => {
    let body = ''
    for await (const c of req) body += c
    try {
      const out = await handle(JSON.parse(body || '{}'))
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify(out ?? null, null, 1))
    } catch (e) {
      res.writeHead(500)
      res.end(String(e?.stack ?? e))
    }
  })
  .listen(9333, '127.0.0.1', () => console.log('driver ready on 9333'))

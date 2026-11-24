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
//   {op:"voicemic"}   a microphone that is silent until {op:"say", file:"x.wav"} plays a recording into it. Every
//                     getUserMedia call gets its own stream and `say` plays into the latest one, so it feeds the
//                     conversation microphone and, while a voice is being cloned, the separate reference capture
//                     (window.__osMicRequests lists each request's audio constraints: EC/NS/AGC, sample rate)
//   {op:"nomic"}     a microphone that never opens (no audio graph at all)
//   {op:"noisemic", gain} a microphone with quiet, slowly varying room noise (never speech): the orb moves
//   The bar opens the microphone the instant it opens, so install one of these BEFORE the first open. Each op
//   reloads the page; with `live: true` it patches the running page instead (the bar builds its audio graph at
//   load but only asks for the microphone on open, so a live patch is enough and keeps the page's state).
//   DRIVER_MIC=voice|fake|no|noise installs one at launch (the first launch opens the bar by itself): `say` then
//   works without any op. It also launches the app with Chromium's fake capture device (OPEN_SPEECH_FAKE_MIC=1),
//   so an open that happens before the stub reaches the page (the shell can open the bar ~250 ms after launch)
//   still never touches the real microphone. Set OPEN_SPEECH_FAKE_MIC=1 yourself when using the ops instead.
//   DRIVER_UNFOCUSABLE=1 makes every window non-focusable from launch on (the first launch shows the bar by
//   itself, before any op could do it).
// Before opening the bar, make windows non-focusable so the bar never takes the user's keystrokes:
//   {op:"main", js:"BrowserWindow.getAllWindows().forEach(w=>w.setFocusable(false)); return 1"}
// The bar exposes a read-only, content-free handle: window.__openSpeechBar.getState() and .log
//
// Flicker measurement (docs/dev: Verification §6). All timestamps are the main process's performance.now() (ms):
//   {op:"frames", page, ms}            record every frame the page's compositor produces for `ms`, then return them
//   {op:"frames", page, action:"start"|"stop"}   the same, across other commands (stop returns the frames)
//       each frame: {t, dirty:{x,y,width,height}, changed (the rect that differs from the previous frame, device px;
//       null = identical), w, h, hash (downscaled image), band (transparent rows at the bottom)}
//       `dirty: true` records only the repainted region (its rect, and the image/hash of that patch)
//   {op:"bounds", action:"start"|"stop"}  log of the bar window's setBounds/setSize/setOpacity/show/hide calls
//       and resize events: {t, op, height, opacity, visible}
//   {op:"renders", page, reset}       React render counts per component (pages loaded with debug=1, i.e. unpackaged)
//   {op:"tray", action:"start"|"stop"} count Tray setImage/setToolTip/setTitle calls (stop returns the log)
//   {op:"open"}                        open the bar as the tray click does (main process)
//   {op:"init", page, js}             run js before the page's own scripts on every load, then reload
//   {op:"route", page, pattern, json}  answer the page's requests matching `pattern` with `json` · {op:"unroute", page, pattern}
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
  // With a microphone stand-in asked for, the real microphone is out of reach from the first instant.
  env: { ...process.env, ...(process.env.DRIVER_MIC ? { OPEN_SPEECH_FAKE_MIC: '1' } : {}) },
})
app.process().stdout.on('data', (d) => logs.push('[main] ' + d.toString().trim()))
app.process().stderr.on('data', (d) => logs.push('[main!] ' + d.toString().trim()))
app.on('window', (p) => {
  p.on('console', (m) => logs.push(`[${p.url().split('/').slice(-2, -1)[0]}:${m.type()}] ${m.text()}`))
  p.on('pageerror', (e) => logs.push(`[pageerror] ${e.message}`))
})

// ---------------------------------------------------------------- microphone stand-ins (never the real microphone)

const MIC_STUBS = {
  // A synthetic "speech" stream: 1.2 s tone bursts, 1.2 s silences.
  fake: () => {
    if (window.__osMicStub) return
    window.__osMicStub = 'fake'
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
  },
  // Silent until {op:"say", file} plays a recording into it: spoken tests that never touch the real microphone or
  // the speakers (make the recording with `say -o file.wav ...`).
  voice: () => {
    if (window.__osMicStub) return
    window.__osMicStub = 'voice'
    let ctx = null
    let dest = null
    const context = () => (ctx ??= new AudioContext())
    window.__osMicRequests = []
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      // A fresh destination per request: the app stops the tracks of the stream it was given.
      window.__osMicRequests.push(JSON.parse(JSON.stringify(constraints?.audio ?? null)))
      dest = context().createMediaStreamDestination()
      const hush = context().createGain()
      hush.gain.value = 0
      const osc = context().createOscillator()
      osc.connect(hush).connect(dest)
      osc.start()
      window.__osMicOpens = (window.__osMicOpens ?? 0) + 1
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
  },
  // Never opens (getUserMedia stays pending): the bar listens, but no audio graph is built.
  no: () => {
    if (window.__osMicStub) return
    window.__osMicStub = 'no'
    navigator.mediaDevices.getUserMedia = () => new Promise(() => undefined)
  },
  // Quiet room noise whose loudness drifts slowly: the orb's level meter moves, the VAD never hears speech.
  noise: (gain) => {
    if (window.__osMicStub) return
    window.__osMicStub = 'noise'
    navigator.mediaDevices.getUserMedia = async () => {
      const ctx = new AudioContext()
      const buffer = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate)
      const data = buffer.getChannelData(0)
      for (let i = 0; i < data.length; i++) data[i] = (Math.random() * 2 - 1) * gain
      const source = ctx.createBufferSource()
      source.buffer = buffer
      source.loop = true
      const drift = ctx.createGain()
      const lfo = ctx.createOscillator()
      const depth = ctx.createGain()
      lfo.frequency.value = 0.7
      depth.gain.value = 0.5
      drift.gain.value = 1
      lfo.connect(depth).connect(drift.gain)
      const dest = ctx.createMediaStreamDestination()
      source.connect(drift).connect(dest)
      source.start()
      lfo.start()
      return dest.stream
    }
  },
}

if (process.env.DRIVER_UNFOCUSABLE === '1' && !CDP) {
  await app.evaluate(({ app: a, BrowserWindow }) => {
    for (const w of BrowserWindow.getAllWindows()) w.setFocusable(false)
    a.on('browser-window-created', (_event, w) => w.setFocusable(false))
  })
}
console.log('driver: windows are non-focusable')

const EARLY_MIC = process.env.DRIVER_MIC
if (EARLY_MIC) {
  if (!MIC_STUBS[EARLY_MIC]) throw new Error(`DRIVER_MIC must be one of ${Object.keys(MIC_STUBS).join(', ')}`)
  // Every page from now on, from its first script; and the pages already there, patched in place.
  const arg = EARLY_MIC === 'noise' ? 0.01 : undefined
  const ctx = app.context?.()
  if (ctx) await Promise.race([ctx.addInitScript(MIC_STUBS[EARLY_MIC], arg), new Promise((r) => setTimeout(r, 3000))])
  // In place too (never awaited: a page still loading would hold the driver up); the stub ignores a second install.
  const patch = (page) => {
    void page.addInitScript(MIC_STUBS[EARLY_MIC], arg).catch(() => undefined)
    void page.evaluate(MIC_STUBS[EARLY_MIC], arg).catch(() => undefined)
  }
  for (const page of app.windows()) patch(page)
  app.on('window', patch)
  console.log(`driver: ${EARLY_MIC} microphone installed`)
}

// ---------------------------------------------------------------- main-process probes (serialized into app.evaluate)

function startFrames({ BrowserWindow }, { page, dirty: onlyDirty }) {
  const win = BrowserWindow.getAllWindows().find((w) => w.webContents.getURL().includes(`/${page}/`))
  if (!win) throw new Error(`no window for ${page}`)
  const store = (globalThis.__osFrames ??= {})
  if (store[page]) win.webContents.endFrameSubscription()
  const rec = (store[page] = { frames: [], win })
  win.webContents.beginFrameSubscription(onlyDirty, (image, dirty) => {
    const t = performance.now()
    const { width: w, height: h } = image.getSize()
    // A quarter-size copy: its hash identifies the picture (quantised so encoder noise doesn't count), and
    // diffing it with the previous frame gives the rectangle that actually changed on screen (macOS reports
    // every frame as fully dirty, so `dirty` alone can't say what repainted).
    const sw = Math.max(1, Math.round(w / 4))
    const smallImg = image.resize({ width: sw, quality: 'good' })
    const sh = smallImg.getSize().height
    const small = smallImg.toBitmap()
    let hash = 2166136261
    for (let i = 0; i < small.length; i++) {
      hash ^= small[i] >> 3
      hash = Math.imul(hash, 16777619)
    }
    let changed = null
    const prev = rec.prev
    if (!prev || prev.w !== sw || prev.h !== sh) changed = prev ? 'resized' : 'first'
    else {
      let x0 = sw, y0 = sh, x1 = -1, y1 = -1
      for (let y = 0; y < sh; y++) {
        for (let x = 0; x < sw; x++) {
          const i = (y * sw + x) * 4
          if (Math.abs(small[i] - prev.px[i]) > 6 || Math.abs(small[i + 1] - prev.px[i + 1]) > 6 || Math.abs(small[i + 2] - prev.px[i + 2]) > 6 || Math.abs(small[i + 3] - prev.px[i + 3]) > 6) {
            if (x < x0) x0 = x
            if (x > x1) x1 = x
            if (y < y0) y0 = y
            if (y > y1) y1 = y
          }
        }
      }
      const k = w / sw
      changed = x1 < 0 ? null : { x: Math.round(x0 * k), y: Math.round(y0 * k), width: Math.round((x1 - x0 + 1) * k), height: Math.round((y1 - y0 + 1) * k) }
    }
    rec.prev = { w: sw, h: sh, px: small }
    // Rows at the bottom with nothing painted in them: a band of window that isn't panel.
    const full = image.toBitmap()
    let band = 0
    for (let y = h - 1; y >= 0; y--) {
      let painted = false
      for (let x = 0; x < w; x += 2) {
        if (full[(y * w + x) * 4 + 3] > 8) {
          painted = true
          break
        }
      }
      if (painted) break
      band++
    }
    rec.frames.push({ t: Math.round(t * 10) / 10, dirty, changed, w, h, hash: (hash >>> 0).toString(16), band })
  })
  return { started: performance.now() }
}

function stopFrames(_electron, page) {
  const rec = globalThis.__osFrames?.[page]
  if (!rec) return []
  try {
    rec.win.webContents.endFrameSubscription()
  } catch {
    // window gone
  }
  delete globalThis.__osFrames[page]
  return rec.frames
}

function startBounds({ BrowserWindow }) {
  const win = BrowserWindow.getAllWindows().find((w) => w.webContents.getURL().includes('/commandbar/'))
  if (!win) throw new Error('no bar window')
  const log = (globalThis.__osBounds = [])
  const note = (op) => {
    const b = win.getBounds()
    log.push({ t: Math.round(performance.now() * 10) / 10, op, height: b.height, opacity: win.getOpacity(), visible: win.isVisible() })
  }
  for (const m of ['setBounds', 'setSize', 'setOpacity', 'show', 'showInactive', 'hide']) {
    if (win[m].__osOrig) continue
    const orig = win[m]
    const wrapped = function (...args) {
      const r = orig.apply(this, args)
      globalThis.__osBoundsNote?.(m)
      return r
    }
    wrapped.__osOrig = orig
    win[m] = wrapped
  }
  globalThis.__osBoundsNote = note
  if (!globalThis.__osBoundsResize) {
    globalThis.__osBoundsResize = true
    win.on('resize', () => globalThis.__osBoundsNote?.('resize-event'))
  }
  note('start')
  return { started: performance.now() }
}

function stopBounds() {
  const log = globalThis.__osBounds ?? []
  globalThis.__osBoundsNote = null
  globalThis.__osBounds = []
  return log
}

function startTray({ Tray }) {
  globalThis.__osTray = []
  const P = Tray.prototype
  for (const m of ['setImage', 'setToolTip', 'setTitle']) {
    if (P[m].__osOrig) continue
    const orig = P[m]
    const wrapped = function (...args) {
      globalThis.__osTray?.push({ t: Math.round(performance.now()), m, arg: m === 'setImage' ? args[0]?.getSize?.().width : args[0] })
      return orig.apply(this, args)
    }
    wrapped.__osOrig = orig
    P[m] = wrapped
  }
  return { started: performance.now() }
}

function stopTray() {
  const log = globalThis.__osTray ?? []
  globalThis.__osTray = null
  return log
}

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
    case 'open':
      return await app.evaluate(({ app: a }) => {
        a.emit('activate')
        return 'ok'
      })
    case 'frames':
      if (cmd.action === 'start') return await app.evaluate(startFrames, { page: cmd.page ?? 'commandbar', dirty: !!cmd.dirty })
      if (cmd.action === 'stop') return await app.evaluate(stopFrames, cmd.page ?? 'commandbar')
      await app.evaluate(startFrames, { page: cmd.page ?? 'commandbar', dirty: !!cmd.dirty })
      await new Promise((r) => setTimeout(r, cmd.ms ?? 1000))
      return await app.evaluate(stopFrames, cmd.page ?? 'commandbar')
    case 'bounds':
      return await app.evaluate(cmd.action === 'stop' ? stopBounds : startBounds)
    case 'tray':
      return await app.evaluate(cmd.action === 'stop' ? stopTray : startTray)
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
    case 'fakemic':
    case 'voicemic':
    case 'nomic':
    case 'noisemic': {
      const kind = cmd.op.replace('mic', '')
      await p.addInitScript(MIC_STUBS[kind], kind === 'noise' ? (cmd.gain ?? 0.01) : undefined)
      if (cmd.live) await p.evaluate(MIC_STUBS[kind], kind === 'noise' ? (cmd.gain ?? 0.01) : undefined)
      else await p.reload()
      return 'ok'
    }
    case 'init':
      // Run `js` at the start of every load of this page, then reload it (e.g. to watch the first paint).
      await p.addInitScript(cmd.js)
      await p.reload()
      return 'ok'
    case 'route':
      // Answer a REST call from the page with fixed JSON (e.g. a month's spending for screenshots).
      await p.unroute(cmd.pattern).catch(() => undefined)
      await p.route(cmd.pattern, (route) => route.fulfill({ status: cmd.status ?? 200, contentType: 'application/json', body: JSON.stringify(cmd.json) }))
      return 'ok'
    case 'unroute':
      await p.unroute(cmd.pattern)
      return 'ok'
    case 'renders':
      return await p.evaluate((reset) => {
        const counts = window.__openSpeechRenders
        if (!counts) return null
        const out = { ...counts }
        if (reset) for (const k of Object.keys(counts)) counts[k] = 0
        return out
      }, !!cmd.reset)
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

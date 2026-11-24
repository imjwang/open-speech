#!/usr/bin/env node
// Draws the menu-bar icons (09 § Menu bar: idle outline, listening filled + pulse frame, speaking speech
// bubble, playing waveform, attention dot) as macOS template images (black + alpha) at 1x and 2x. No dependencies:
// shapes are rasterised with 4×4 supersampling and encoded as PNG with node:zlib.
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import zlib from 'node:zlib'

const out = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'resources', 'tray')
mkdirSync(out, { recursive: true })

const SS = 4 // supersampling factor

function raster(size, inside) {
  const rgba = Buffer.alloc(size * size * 4)
  const unit = 16 / size // shapes are defined on a 16×16 grid
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let hits = 0
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const gx = (x + (sx + 0.5) / SS) * unit
          const gy = (y + (sy + 0.5) / SS) * unit
          if (inside(gx, gy)) hits++
        }
      }
      const a = Math.round((hits / (SS * SS)) * 255)
      const i = (y * size + x) * 4
      rgba[i] = 0
      rgba[i + 1] = 0
      rgba[i + 2] = 0
      rgba[i + 3] = a
    }
  }
  return rgba
}

function crcChunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const td = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(zlib.crc32(td) >>> 0)
  return Buffer.concat([len, td, crc])
}

function png(size, rgba) {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0)
  ihdr.writeUInt32BE(size, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 6 // RGBA
  ihdr[10] = ihdr[11] = ihdr[12] = 0
  const rows = []
  for (let y = 0; y < size; y++) {
    rows.push(Buffer.from([0]), rgba.subarray(y * size * 4, (y + 1) * size * 4))
  }
  const idat = zlib.deflateSync(Buffer.concat(rows), { level: 9 })
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    crcChunk('IHDR', ihdr),
    crcChunk('IDAT', idat),
    crcChunk('IEND', Buffer.alloc(0)),
  ])
}

const dist = (x, y, cx, cy) => Math.hypot(x - cx, y - cy)
const ring = (cx, cy, r, w) => (x, y) => Math.abs(dist(x, y, cx, cy) - r) <= w / 2
const disc = (cx, cy, r) => (x, y) => dist(x, y, cx, cy) <= r
const roundRect = (x0, y0, x1, y1, r) => (x, y) => {
  if (x < x0 || x > x1 || y < y0 || y > y1) return false
  const cx = Math.min(Math.max(x, x0 + r), x1 - r)
  const cy = Math.min(Math.max(y, y0 + r), y1 - r)
  return dist(x, y, cx, cy) <= r
}
const triangle = (ax, ay, bx, by, cx, cy) => (x, y) => {
  const s1 = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
  const s2 = (cx - bx) * (y - by) - (cy - by) * (x - bx)
  const s3 = (ax - cx) * (y - cy) - (ay - cy) * (x - cx)
  return (s1 >= 0 && s2 >= 0 && s3 >= 0) || (s1 <= 0 && s2 <= 0 && s3 <= 0)
}
const union = (...fs) => (x, y) => fs.some((f) => f(x, y))
const minus = (a, b) => (x, y) => a(x, y) && !b(x, y)

// A small "speech" glyph: three bars inside the circle, so idle/listening read as Open Speech.
const bars = (heights, cx = 8, cy = 8) =>
  union(
    ...heights.map((h, i) => {
      const x = cx - 3.4 + i * 2.6
      return roundRect(x, cy - h / 2, x + 1.3, cy + h / 2, 0.65)
    }),
  )

const icons = {
  // idle: outline circle with a quiet glyph
  idle: union(ring(8, 8, 6.4, 1.3), bars([2.4, 4, 2.4])),
  // listening: filled circle, glyph knocked out
  listening: minus(disc(8, 8, 7), bars([3, 5.4, 3])),
  // listening pulse frame (alternates with `listening` unless reduced motion is on)
  'listening-pulse': union(minus(disc(8, 8, 5.6), bars([2.4, 4.2, 2.4])), ring(8, 8, 7.2, 0.9)),
  // speaking (the assistant is talking): a speech bubble with the glyph inside
  speaking: union(
    minus(roundRect(1, 1.6, 15, 11.6, 3.2), roundRect(2.3, 2.9, 13.7, 10.3, 2)),
    minus(triangle(3.6, 10.8, 3.6, 14.8, 7.6, 10.8), roundRect(2.3, 2.9, 13.7, 10.3, 2)),
    bars([2.4, 4.4, 2.4], 8.45, 6.6),
  ),
  // playing: waveform
  playing: union(
    roundRect(1.4, 6, 3, 10, 0.8),
    roundRect(4.4, 3.2, 6, 12.8, 0.8),
    roundRect(7.4, 1.5, 9, 14.5, 0.8),
    roundRect(10.4, 4.2, 12, 11.8, 0.8),
    roundRect(13.4, 6.4, 15, 9.6, 0.8),
  ),
  // attention: outline with a solid dot (budget warning or error)
  attention: union(minus(ring(7, 9, 5.6, 1.3), disc(13, 3.2, 3.4)), disc(13, 3.2, 2.5), bars([2, 3.4, 2], 7, 9)),
}

for (const [name, shape] of Object.entries(icons)) {
  for (const [scale, suffix] of [
    [1, ''],
    [2, '@2x'],
  ]) {
    const size = 16 * scale
    const file = path.join(out, `${name}Template${suffix}.png`)
    writeFileSync(file, png(size, raster(size, shape)))
    console.log(`wrote ${path.relative(process.cwd(), file)}`)
  }
}

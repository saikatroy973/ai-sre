// Generates a native Excalidraw scene (.excalidraw JSON) from the whiteboard sketch.
import { writeFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'

const FONT = 2 // 2 = Helvetica (renders as Helvetica Neue / system font on macOS)
const STROKE = '#1e1e1e'
const ACCENT = '#1971c2' // arrows
const MUTED = '#868e96'

let seedN = 1
const seed = () => 100000 + (seedN++ * 7919) % 900000

const els = []
const base = (o) => ({
  id: randomUUID(),
  angle: 0,
  strokeColor: STROKE,
  backgroundColor: 'transparent',
  fillStyle: 'solid',
  strokeWidth: 2,
  strokeStyle: 'solid',
  roughness: 1,
  opacity: 100,
  groupIds: [],
  frameId: null,
  roundness: null,
  seed: seed(),
  version: 1,
  versionNonce: seed(),
  isDeleted: false,
  boundElements: null,
  updated: 1,
  link: null,
  locked: false,
  ...o,
})

// rough text metrics for Virgil
const textW = (t, fs) => Math.max(...t.split('\n').map((l) => l.length)) * fs * 0.52
const textH = (t, fs) => t.split('\n').length * fs * 1.25

function boxed({ x, y, w, h, label, fontSize = 28 }) {
  const rect = base({ type: 'rectangle', x, y, width: w, height: h, roundness: { type: 3 } })
  const tw = textW(label, fontSize)
  const th = textH(label, fontSize)
  const text = base({
    type: 'text',
    x: x + (w - tw) / 2,
    y: y + (h - th) / 2,
    width: tw,
    height: th,
    text: label,
    originalText: label,
    fontSize,
    fontFamily: FONT,
    textAlign: 'center',
    verticalAlign: 'middle',
    containerId: rect.id,
    lineHeight: 1.25,
    roundness: null,
  })
  rect.boundElements = [{ type: 'text', id: text.id }]
  els.push(rect, text)
  return rect
}

function arrow({ points, start = null, end = null, color = ACCENT }) {
  // Excalidraw stores arrow vertices relative to the element's own origin.
  const [ox, oy] = points[0]
  const rel = points.map(([x, y]) => [x - ox, y - oy])
  const xs = rel.map((p) => p[0])
  const ys = rel.map((p) => p[1])
  const a = base({
    type: 'arrow',
    x: ox,
    y: oy,
    width: Math.max(...xs) - Math.min(...xs),
    height: Math.max(...ys) - Math.min(...ys),
    points: rel,
    lastCommittedPoint: null,
    startBinding: start ? { elementId: start.id, focus: 0, gap: 6 } : null,
    endBinding: end ? { elementId: end.id, focus: 0, gap: 6 } : null,
    startArrowhead: null,
    endArrowhead: 'arrow',
    strokeColor: color,
    roundness: { type: 2 },
  })
  els.push(a)
  for (const node of [start, end]) {
    if (!node) continue
    node.boundElements = [...(node.boundElements || []), { type: 'arrow', id: a.id }]
  }
  return a
}

function freeText({ x, y, text, fontSize = 30, color = MUTED }) {
  const e = base({
    type: 'text',
    x,
    y,
    width: textW(text, fontSize),
    height: textH(text, fontSize),
    text,
    originalText: text,
    fontSize,
    fontFamily: FONT,
    textAlign: 'center',
    verticalAlign: 'top',
    containerId: null,
    lineHeight: 1.25,
    strokeColor: color,
  })
  els.push(e)
  return e
}

function slash({ x, y, dx, dy }) {
  const e = base({
    type: 'line',
    x,
    y,
    width: Math.abs(dx),
    height: Math.abs(dy),
    points: [
      [0, 0],
      [dx, dy],
    ],
    lastCommittedPoint: null,
    startBinding: null,
    endBinding: null,
    startArrowhead: null,
    endArrowhead: null,
    strokeColor: '#adb5bd',
  })
  els.push(e)
}

function label({ x, y, text, fontSize = 30, color = STROKE, align = 'left' }) {
  const e = base({
    type: 'text',
    x,
    y,
    width: textW(text, fontSize),
    height: textH(text, fontSize),
    text,
    originalText: text,
    fontSize,
    fontFamily: FONT,
    textAlign: align,
    verticalAlign: 'top',
    containerId: null,
    lineHeight: 1.35,
    strokeColor: color,
  })
  els.push(e)
  return e
}

function badge({ x, y, d, label: l }) {
  const c = base({ type: 'ellipse', x, y, width: d, height: d, roundness: null })
  const fs = Math.round(d * 0.52)
  const t = base({
    type: 'text',
    x: x + d / 2 - textW(l, fs) / 2,
    y: y + d / 2 - textH(l, fs) / 2,
    width: textW(l, fs),
    height: textH(l, fs),
    text: l,
    originalText: l,
    fontSize: fs,
    fontFamily: FONT,
    textAlign: 'center',
    verticalAlign: 'middle',
    containerId: c.id,
    lineHeight: 1.25,
  })
  c.boundElements = [{ type: 'text', id: t.id }]
  els.push(c, t)
  return c
}

function segment({ x, y, pts, color = STROKE, style = 'solid', width = 2 }) {
  const rel = pts.map(([px, py]) => [px - x, py - y])
  const e = base({
    type: 'line',
    x,
    y,
    points: rel,
    width: Math.max(...rel.map((p) => Math.abs(p[0]))),
    height: Math.max(...rel.map((p) => Math.abs(p[1]))),
    lastCommittedPoint: null,
    startBinding: null,
    endBinding: null,
    startArrowhead: null,
    endArrowhead: null,
    strokeColor: color,
    strokeStyle: style,
    strokeWidth: width,
    roundness: { type: 2 },
  })
  els.push(e)
  return e
}

// ---- nodes -------------------------------------------------------------
const devops = boxed({ x: 90, y: 96, w: 280, h: 104, label: 'DevOps' })
const k8s = boxed({ x: 522, y: 96, w: 240, h: 104, label: 'K8s' })
const prompt = boxed({ x: 420, y: 248, w: 520, h: 104, label: 'prompt + MCP + RAG', fontSize: 30 })
const agent = boxed({ x: 540, y: 456, w: 280, h: 104, label: 'agent' })
const pod = boxed({ x: 942, y: 414, w: 336, h: 188, label: 'Pod\nDeployment\nLogs', fontSize: 27 })
const proto = boxed({ x: 1400, y: 456, w: 310, h: 104, label: 'Prototype', fontSize: 29 })

// ---- edges -------------------------------------------------------------
arrow({ points: [[382, 148], [516, 148]], start: devops, end: k8s })
arrow({ points: [[200, 202], [200, 300], [408, 300]], start: devops, end: prompt })
arrow({ points: [[680, 356], [680, 450]], start: prompt, end: agent })
arrow({ points: [[824, 508], [936, 508]], start: agent, end: pod })
arrow({ points: [[1282, 508], [1394, 508]], start: pod, end: proto })

// ---- day-1 annotation --------------------------------------------------
freeText({ x: 108, y: 528, text: 'day-1', fontSize: 34 })
slash({ x: 300, y: 618, dx: 36, dy: -166 })
slash({ x: 332, y: 618, dx: 36, dy: -166 })

// ======================================================================
// diagram 2 — "1. Trust / memory"
// ======================================================================
segment({ x: 90, y: 706, pts: [[90, 706], [1712, 706]], color: '#dee2e6', style: 'dashed' })

badge({ x: 90, y: 762, d: 58, label: '1' })
label({ x: 176, y: 768, text: 'Trust', fontSize: 40 })
arrow({ points: [[206, 832], [206, 884], [322, 884]] })

const memory = boxed({ x: 340, y: 836, w: 300, h: 96, label: 'memory', fontSize: 30 })

// T-split below memory
segment({ x: 490, y: 932, pts: [[490, 932], [490, 1040]] })
arrow({ points: [[490, 1040], [440, 1040]], start: memory })
arrow({ points: [[490, 1040], [700, 1040]] })

// left: brace + logs / events / Recent, with the check mark
label({ x: 200, y: 974, text: '{', fontSize: 118, color: '#343a40' })
label({ x: 272, y: 982, text: 'logs\nevents\nRecent', fontSize: 30 })
label({ x: 118, y: 986, text: '✓\n=', fontSize: 36, color: MUTED })

// right: memory x
boxed({ x: 706, y: 992, w: 292, h: 96, label: 'memoryˣ', fontSize: 30 })
label({ x: 706, y: 1120, text: 'Agent evaluation =', fontSize: 30 })

const scene = {
  type: 'excalidraw',
  version: 2,
  source: 'https://excalidraw.com',
  elements: els,
  appState: { gridSize: null, viewBackgroundColor: '#ffffff' },
  files: {},
}

writeFileSync(process.argv[2] || 'diagram.excalidraw', JSON.stringify(scene, null, 2))
console.log('elements:', els.length)

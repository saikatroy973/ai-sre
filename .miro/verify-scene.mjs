import { chromium } from '/Users/saikat/projects/career-ops/node_modules/playwright-core/index.mjs'
const EXEC = '/Users/saikat/Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
const dir = '/Users/saikat/projects/switch_ai_hackathon/.miro'
const sceneUrl = process.argv[2]
const tag = process.argv[3] || 'check'

const browser = await chromium.launch({ executablePath: EXEC, headless: true })
const page = await browser.newPage({ viewport: { width: 1700, height: 1150 }, deviceScaleFactor: 2 })
const bad = []
page.on('response', r => { if (r.status() >= 400) bad.push(r.status() + ' ' + r.url().slice(0, 90)) })

const link = 'https://excalidraw.com/#url=' + encodeURIComponent(sceneUrl)
await page.goto(link, { waitUntil: 'domcontentloaded', timeout: 60000 })
await page.waitForSelector('canvas', { timeout: 30000 })
await page.waitForTimeout(6000)
await page.mouse.click(900, 880)
await page.keyboard.press('Shift+1')
await page.waitForTimeout(1200)

const st = await page.evaluate(() => {
  const raw = localStorage.getItem('excalidraw') || ''
  let els = []
  try { els = JSON.parse(raw).elements || [] } catch {}
  return { url: location.href, n: els.length, types: els.map(e => e.type), badRedDot: document.body.innerText.includes('Error') }
})
console.log(JSON.stringify(st))
console.log('HTTP>=400:', bad.slice(0, 6))
await page.screenshot({ path: `${dir}/${tag}.png` })
await browser.close()

import { chromium } from '/Users/saikat/projects/career-ops/node_modules/playwright-core/index.mjs'
const EXEC = '/Users/saikat/Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
const dir = '/Users/saikat/projects/switch_ai_hackathon/.miro'

const browser = await chromium.launch({ executablePath: EXEC, headless: true })
const page = await browser.newPage({ viewport: { width: 1800, height: 760 }, deviceScaleFactor: 2 })
await page.goto('file://' + dir + '/diagram.svg')
await page.waitForTimeout(500)
await page.screenshot({ path: dir + '/diagram.png', omitBackground: false })
await browser.close()
console.log('rendered')

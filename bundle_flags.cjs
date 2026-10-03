// Build-time SVG rasterization, using Playwright. No runtime browser dependency.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const source = JSON.parse(fs.readFileSync(path.join(__dirname, '.runtime/svg-flags.json'), 'utf8'));
  const browser = await chromium.launch({headless: true, channel: process.env.FLAG_BROWSER_CHANNEL || 'msedge'});
  const page = await browser.newPage();
  const flags = await page.evaluate(async (source) => {
    const output = {};
    for (const [code, svg] of Object.entries(source)) {
      const image = new Image();
      image.src = svg;
      await image.decode();
      const canvas = document.createElement('canvas');
      canvas.width = 32;
      canvas.height = 24;
      canvas.getContext('2d').drawImage(image, 0, 0, 32, 24);
      output[code] = canvas.toDataURL('image/png');
    }
    return output;
  }, source);
  fs.writeFileSync(path.join(__dirname, 'flags.json'), JSON.stringify(flags, null, 2) + '\n');
  await browser.close();
  console.log(`Bundled ${Object.keys(flags).length} tiny, self-contained flags`);
})().catch(error => { console.error(error.message); process.exitCode = 1; });

// Build-time only (docker/stacks/web/Dockerfile): the default headless launch must be the headless
// shell. A page served by `python3 -m http.server` must give no console error and no /favicon.ico request.
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('@playwright/test');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-'));
fs.writeFileSync(path.join(dir, 'index.html'), '<p>ok</p>');
const srv = spawn('python3', ['-m', 'http.server', '8765', '--bind', '127.0.0.1', '--directory', dir], { stdio: 'ignore' });
(async () => {
  await new Promise((r) => setTimeout(r, 1000));
  const b = await chromium.launch();
  const pg = await b.newPage();
  const errs = [];
  const urls = [];
  pg.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  pg.on('request', (r) => urls.push(r.url()));
  await pg.goto('http://127.0.0.1:8765/');
  await pg.waitForTimeout(1000);
  await b.close();
  if (errs.length || urls.some((u) => u.endsWith('/favicon.ico'))) throw new Error(JSON.stringify({ errs, urls }));
})().catch((e) => { console.error(e); process.exitCode = 1; }).finally(() => srv.kill());

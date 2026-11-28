// Build-time only (docker/stacks/web/Dockerfile): lay out the browser folders stock Playwright
// looks for under PLAYWRIGHT_BROWSERS_PATH, as symlinks to Debian's chromium (full) and chromium-headless-shell, so a plain
// `chromium.launch()` works offline with no download. Revisions come from the installed packages,
// never hardcoded. Usage: node link-browsers.js <python browsers.json> <chromium> <headless-shell>
// Fails (exit 1) when the node and python Playwright disagree on a revision.
const fs = require('fs');
const path = require('path');
const root = process.env.PLAYWRIGHT_BROWSERS_PATH;
const [pyJson, fullTarget, shellTarget] = process.argv.slice(2);
const nodeJson = path.join(path.dirname(require.resolve('playwright-core/package.json')), 'browsers.json');
const revs = (file) => Object.fromEntries(
  JSON.parse(fs.readFileSync(file, 'utf8')).browsers.map((b) => [b.name, b.revision]));
const node = revs(nodeJson);
const py = revs(pyJson);
const arm = process.arch === 'arm64';
// Folder and executable names as the Playwright registry resolves them on linux, per CPU.
const kinds = [
  { name: 'chromium-headless-shell', dir: 'chromium_headless_shell',
    sub: arm ? 'chrome-headless-shell-linux-arm64' : 'chrome-headless-shell-linux64', exe: 'chrome-headless-shell', target: shellTarget },
  { name: 'chromium', dir: 'chromium', sub: arm ? 'chrome-linux-arm64' : 'chrome-linux64', exe: 'chrome', target: fullTarget },
];
for (const b of kinds) {
  if (!node[b.name] || node[b.name] !== py[b.name]) {
    console.error(`revision mismatch for ${b.name}: node ${node[b.name]} python ${py[b.name]}`);
    process.exit(1);
  }
  const top = path.join(root, `${b.dir}-${node[b.name]}`);
  fs.mkdirSync(path.join(top, b.sub), { recursive: true });
  fs.symlinkSync(b.target, path.join(top, b.sub, b.exe));
  // Playwright treats a folder as installed only with this marker file.
  fs.writeFileSync(path.join(top, 'INSTALLATION_COMPLETE'), '');
  console.log(`${top}/${b.sub}/${b.exe} -> ${b.target}`);
}

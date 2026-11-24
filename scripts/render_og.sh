#!/usr/bin/env bash
# Renders scripts/og-image.html to src/tarnow_events/web/og-image.png (1200×630) with headless
# Chromium from the official Playwright image. Needs Docker and internet (Google Fonts).
set -euo pipefail
cd "$(dirname "$0")/.."

docker run --rm --network host \
  -v "$PWD/scripts:/in:ro" -v "$PWD/src/tarnow_events/web:/out" -w /tmp \
  --user "$(id -u):$(id -g)" -e HOME=/tmp mcr.microsoft.com/playwright:v1.49.1-noble sh -c '
    npm i -s playwright@1.49.1 >/dev/null 2>&1 && node -e "
      const { chromium } = require(\"playwright\");
      (async () => {
        const b = await chromium.launch();
        const p = await b.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
        await p.goto(\"file:///in/og-image.html\", { waitUntil: \"networkidle\" });
        await p.evaluate(() => document.fonts.ready);
        await p.waitForTimeout(300);
        await p.screenshot({ path: \"/out/og-image.png\" });
        await b.close();
      })();"'
echo "wrote src/tarnow_events/web/og-image.png"

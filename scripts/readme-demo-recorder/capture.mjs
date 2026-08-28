// Render every frame of the composition to out/frames/ by driving it in headless Chromium.
//
//     node capture.mjs
//
// esbuild bundles src/entry.tsx into one file, Playwright loads it once, and each frame is a
// `renderFrame(n)` followed by a screenshot. `deviceScaleFactor: 1` and an explicit viewport keep
// the output at the composition's own pixel size, so no scaling happens before ffmpeg sees it.
import { mkdir, rm, writeFile } from "node:fs/promises";
import { build } from "esbuild";
import { chromium } from "playwright";

const W = 1200;
const H = 640;
const OUT = "out/frames";

await rm(OUT, { recursive: true, force: true });
await mkdir(OUT, { recursive: true });

const bundle = await build({
  entryPoints: ["src/entry.tsx"],
  bundle: true,
  write: false,
  format: "iife",
  jsx: "automatic",
  target: "chrome120",
  define: { "process.env.NODE_ENV": '"production"' },
});
const js = bundle.outputFiles[0].text;

// A bare page with a fixed-size root and no default margin: the composition positions everything
// itself, and any body margin would offset every frame by the same few pixels.
await writeFile(
  "out/frame.html",
  `<!doctype html><meta charset="utf-8">
<style>
  html,body{margin:0;padding:0;background:#0E1424}
  #root{position:relative;width:${W}px;height:${H}px;overflow:hidden}
</style>
<div id="root"></div>
<script>${js}</script>`,
);

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: W, height: H },
  deviceScaleFactor: 1,
});
await page.goto(`file://${process.cwd()}/out/frame.html`);

const { duration, fps } = await page.evaluate(() => window.meta);
process.stdout.write(`capturing ${duration} frames at ${fps}fps\n`);

for (let n = 0; n < duration; n++) {
  await page.evaluate((f) => window.renderFrame(f), n);
  await page.screenshot({
    path: `${OUT}/${String(n).padStart(4, "0")}.png`,
    animations: "disabled",
  });
  if (n % 50 === 0) process.stdout.write(`  ${n}/${duration}\n`);
}

await browser.close();
process.stdout.write(`wrote ${duration} frames to ${OUT}\n`);

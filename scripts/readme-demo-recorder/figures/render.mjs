// Render architecture.html to the two PNGs the README embeds, one per colour scheme.
//
//     npm run figures        (from scripts/readme-demo-recorder; needs the network, for the fonts)
//
// `deviceScaleFactor: 1` keeps the output at the page's own pixel size, 1240 wide; the README
// scales it to its column. The height is whatever the page lays out, so a row added to the server
// panel grows the figure rather than cropping it.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const here = path.dirname(fileURLToPath(import.meta.url));
const page_ = `file://${path.join(here, "architecture.html")}`;
const out = path.resolve(here, "..", "..", "..", "assets", "figures");
const W = 1240;

const browser = await chromium.launch();
for (const [scheme, name] of [
  ["dark", "architecture.png"],
  ["light", "architecture-light.png"],
]) {
  const page = await browser.newPage({ viewport: { width: W, height: 600 }, deviceScaleFactor: 1 });
  await page.emulateMedia({ colorScheme: scheme });
  await page.goto(page_, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  const fonts = await page.evaluate(() =>
    ["500 15px Geist", '12px "Geist Mono"'].filter((f) => !document.fonts.check(f)),
  );
  if (fonts.length) throw new Error(`fonts not loaded: ${fonts.join(", ")}`);
  const height = Math.ceil(await page.evaluate(() => document.body.getBoundingClientRect().height));
  const file = path.join(out, name);
  await page.screenshot({ path: file, clip: { x: 0, y: 0, width: W, height } });
  await page.close();
  process.stdout.write(`${path.relative(process.cwd(), file)}  ${W}x${height}\n`);
}
await browser.close();

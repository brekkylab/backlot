# README demo recorder

Renders the animation the README embeds at [`assets/demo.gif`](../../assets/demo.gif). Build-only —
nothing here ships in the wheel. [← README](../../README.md)

React components draw one frame at a time from a frame number; [esbuild](https://esbuild.github.io)
bundles them, [Playwright](https://playwright.dev) screenshots each frame in headless Chromium, and
ffmpeg and gifsicle encode the GIF. No video framework, and every dependency is permissively
licensed.

```bash
brew install ffmpeg gifsicle
cd scripts/readme-demo-recorder
npm install && npx playwright install chromium
./build.sh
```

`build.sh` captures to `out/frames/` and writes `../../assets/demo.gif`. `npx tsc --noEmit`
typechecks.

| Path | What it is |
|---|---|
| `src/theme.ts` | The palette, and `T` — every beat of the timeline, in frames at 30fps |
| `src/SlackOnboarding.tsx` | The composition |
| `src/parts.tsx` | The pieces it is built from |
| `src/runtime.tsx` | `interpolate`, `Easing`, `AbsoluteFill` — the whole animation runtime |
| `capture.mjs` | Bundle, drive the browser, write the frames |

The comments in those files carry the reasoning: why entrances use a clamped ease rather than a
spring, why transitions are staggered per row, why the GIF is encoded from one global palette. Read
them before changing a number — most of them exist because the obvious choice looked wrong on screen
or cost several hundred KB.

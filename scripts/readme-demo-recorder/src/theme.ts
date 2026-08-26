/**
 * A deliberately small, flat palette.
 *
 * Every colour here is opaque and every surface is a solid fill: no gradients, no shadows, no
 * blur. That is a GIF constraint before it is a taste one. The deliverable is an inline README
 * image, so it is encoded as a 256-colour GIF and compressed by inter-frame delta — a gradient
 * costs hundreds of palette entries and turns a still region into a dithered one that re-encodes
 * every frame. Flat fills keep a held frame byte-identical to the one before it.
 *
 * Two accents carry the whole argument, so they must stay distinguishable at README width and for
 * a red/green colour-blind reader: AMBER is the cost of doing it by hand, MINT is Backlot. They
 * differ in lightness as well as hue, and each is paired with a shape or a word wherever it makes
 * a point on its own.
 */
export const C = {
  bg: "#0E1424",
  panel: "#161D33",
  panelEdge: "#26304C",
  ink: "#EAEEF7",
  inkDim: "#93A0BE",
  inkFaint: "#5C688A",
  amber: "#E9A94E",
  mint: "#3DDC97",
  blocked: "#E4614F",
} as const;

export const F = {
  ui: '-apple-system, "SF Pro Text", "Helvetica Neue", Arial, sans-serif',
  mono: '"SF Mono", ui-monospace, "JetBrains Mono", Menlo, monospace',
} as const;

/** 30fps: the timeline below is written in frames, so keep these in sync with Root.tsx. */
export const FPS = 30;
export const DURATION = 450;

/** Named beats, so the composition reads as a storyboard rather than a pile of magic numbers. */
export const T = {
  titleIn: 0,
  titleOut: 36,
  lanesIn: 36,
  gates: [54, 78, 102, 126, 150, 174],
  steps: [60, 96, 132],
  connected: 162,
  fields: 176,
  chips: 236,
  wall: 228,
  verdict: 312,
  endcard: 366,
} as const;

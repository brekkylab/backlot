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
export const DURATION = 690;

/** Named beats, so the composition reads as a storyboard rather than a pile of magic numbers. */
export const T = {
  // The wordmark opens centred and large on the full brand card, then shrinks into the top-left
  // corner and stays there as the piece's header, with the subject line arriving beside it.
  //
  // The card is held for 1.8s here and 1.8s at the close. The split matters only to a first-time
  // viewer, because the clip loops seamlessly — the last frame IS this one, so across a repeat the
  // card reads as one 3.6s beat. Someone opening the README cold starts at frame 0, though, and
  // giving them 0.8s of the brand before it leaves was too brief to register.
  morphStart: 24,
  // 16 frames, not 24. The travel is short and cubic-out lands it 95% of the way in the first ten,
  // so the extra eight were the wordmark creeping the last pixel — during which it sat in the
  // subject line's space and the two read as crowded. Landing here also lets the lanes start on the
  // same frame, which is what makes the brand look like it finished moving before they begin.
  morphEnd: 40,
  // Lands with the wordmark, not after the lanes. Cubic-out puts the travel 78% of the way by the
  // midpoint, so by here the corner is effectively occupied and the line reads as part of the
  // header rather than as something the lanes brought with them.
  headingIn: 34,

  // Held until the wordmark has all but arrived — cubic-out has it 95% of the way by here. Starting
  // it earlier read as the lanes beginning while the brand was still in flight. What keeps the frame
  // from emptying in between is `headingIn`, not the lanes, so this can wait.
  lanesIn: 40,
  gates: [66, 90, 114, 138, 162, 186],
  steps: [72, 108, 144],
  connected: 174,
  fields: 188,
  wall: 240,
  chips: 248,
  // Chips settle at 297; this lands 9 frames later, and holds until the closing card arrives.
  verdict: 276,
  // The generalising act. The lanes slide left — Slack's column off the edge, Backlot's into its
  // place — and then the surviving column repeats itself for one source after another. Showing it
  // beats asserting it: every card below is a real response this server returned.
  slide: 372,
  // 24 frames each: short enough to fit eight sources, and the frame around them never moves,
  // so the eye only has to track the strings that changed.
  cycle: [392, 416, 440, 464, 488, 512, 536, 560],
  // The closing card waits: the last tick lands at 560 and the checklist is only complete then, so
  // cutting away 30 frames later gave the finished list no time to be read as finished.
  endcard: 636,
} as const;

import React from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { SlackOnboarding } from "./SlackOnboarding";
import { DURATION, FPS } from "./theme";

// One page load, one React root, N paints: the capture script sets a frame and screenshots. Doing
// it per-frame as a fresh navigation would spend the whole render budget on page loads.
const root = createRoot(document.getElementById("root")!);

declare global {
  interface Window {
    renderFrame: (n: number) => Promise<void>;
    meta: { duration: number; fps: number };
  }
}

window.meta = { duration: DURATION, fps: FPS };

// `root.render` alone is not enough to screenshot against, and getting this wrong is silent: React
// commits concurrently, so the capture raced the paint and every other frame came out identical to
// the one before it — 252 of 450 duplicated, scattered through the animated stretches rather than
// the held ones. `flushSync` commits the DOM before returning, and the two nested rAFs give the
// compositor a painted frame to hand over.
window.renderFrame = (n: number) =>
  new Promise<void>((resolve) => {
    flushSync(() => {
      root.render(<SlackOnboarding frame={n} />);
    });
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  });

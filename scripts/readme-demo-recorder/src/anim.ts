import { interpolate, Easing } from "./runtime";

/**
 * Every entrance in this piece is the same shape: fade up over 9 frames and stop.
 *
 * A spring would be wrong here, which is why none is used. A spring settles asymptotically, so a row
 * that has visually arrived is still moving by a subpixel for another twenty frames, and a
 * subpixel move rewrites the row's whole bounding box in the GIF. A clamped ease that actually
 * reaches its end value lets the frame go byte-identical, which is what makes the held stretches
 * of this video nearly free.
 */
export const IN_FRAMES = 9;

export const appear = (frame: number, at: number) =>
  interpolate(frame, [at, at + IN_FRAMES], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });

/** Fade out, same reasoning. */
export const vanish = (frame: number, at: number, over = IN_FRAMES) =>
  interpolate(frame, [at, at + over], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.in(Easing.cubic),
  });

/** Slide distance paired with `appear`, in px. Small: a long travel smears across many frames. */
export const rise = (t: number, px = 10) => (1 - t) * px;

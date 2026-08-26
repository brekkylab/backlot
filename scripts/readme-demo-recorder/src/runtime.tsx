import React from "react";

/**
 * The four things this composition needed from a video framework, written out.
 *
 * It never used a timeline, a sequence, audio, or a spring — only a frame number, a linear
 * interpolation with clamped ends, cubic easing, and a full-bleed div. Depending on a
 * source-available renderer for that was a licence tripwire with no engineering return, so the
 * frames are captured by driving this in headless Chromium instead. See ../README.md.
 *
 * `interpolate` and `Easing` reproduce Remotion's semantics deliberately, because the committed
 * GIF was rendered against them: easing is applied to the normalised progress *before* it is
 * mapped onto the output range, and `Easing.in` runs the curve forward while `Easing.out` mirrors
 * it. Change either and every entrance in the piece moves.
 */

type Extrapolate = "clamp" | "extend";

export const Easing = {
  cubic: (t: number) => t * t * t,
  /** Runs the curve forward — Remotion's `Easing.in` is the identity wrapper. */
  in: (fn: (t: number) => number) => fn,
  /** Mirrors it: fast then settling. */
  out:
    (fn: (t: number) => number) =>
    (t: number): number =>
      1 - fn(1 - t),
} as const;

export function interpolate(
  input: number,
  inputRange: readonly [number, number],
  outputRange: readonly [number, number],
  opts?: {
    extrapolateLeft?: Extrapolate;
    extrapolateRight?: Extrapolate;
    easing?: (t: number) => number;
  },
): number {
  const [i0, i1] = inputRange;
  const [o0, o1] = outputRange;
  let progress = i1 === i0 ? 1 : (input - i0) / (i1 - i0);
  if (progress < 0 && (opts?.extrapolateLeft ?? "extend") === "clamp") progress = 0;
  if (progress > 1 && (opts?.extrapolateRight ?? "extend") === "clamp") progress = 1;
  const eased = opts?.easing ? opts.easing(progress) : progress;
  return o0 + (o1 - o0) * eased;
}

/** Remotion's AbsoluteFill: a full-bleed flex column, overridable by `style`. */
export const AbsoluteFill: React.FC<{
  children?: React.ReactNode;
  style?: React.CSSProperties;
}> = ({ children, style }) => (
  <div
    style={{
      position: "absolute",
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      display: "flex",
      flexDirection: "column",
      ...style,
    }}
  >
    {children}
  </div>
);

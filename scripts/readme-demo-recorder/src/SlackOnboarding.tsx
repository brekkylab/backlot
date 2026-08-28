import React from "react";
import { AbsoluteFill, interpolate, Easing } from "./runtime";
import { C, F, T } from "./theme";
import { appear, vanish, rise } from "./anim";
import {
  LaneHead,
  Step,
  Wall,
  Chips,
  ResponseCard,
  Verdict,
  SourceCycle,
  SourceTally,
  SERVED,
  activeServed,
} from "./parts";

/**
 * "Connecting Slack costs a day of dashboard work, or one changed line."
 *
 * The two lanes run on ONE clock, side by side, and that is the whole device: the right lane is
 * already returning messages while the left is still filling in OAuth scopes. Told in sequence —
 * first the slow way, then the fast way — the reader has to hold the first half in memory to feel
 * the second. Run in parallel, the comparison is just what the frame looks like.
 *
 * Nothing here uses a Slack logo or wordmark styling. The service is named in text, which is
 * nominative use; a redrawn vendor mark in a project's own README is a trademark question we have
 * no reason to open.
 */

const W = 1200;
const GUTTER = 34;
const GATES: { text: string; note?: string }[] = [
  { text: "Create a Slack workspace" },
  { text: "Register an app in the dashboard" },
  { text: "Add a bot user to it" },
  { text: "Pick OAuth scopes", note: "channels:history, users:read, chat:write" },
  { text: "Register a redirect URL" },
  { text: "Install to the workspace" },
];

const STEPS: { text: React.ReactNode; note?: string }[] = [
  { text: "pip install backlot" },
  { text: "backlot serve" },
  {
    // The value in mint, as it is for every source in the second act: the colour marks the one
    // thing that differs, and act 1 leaving it plain broke that before it was established.
    text: (
      <>
        base_url=
        <span style={{ color: C.mint }}>&quot;http://localhost:8000/slack/api/&quot;</span>
      </>
    ),
    note: "the only line that changes",
  },
];

/**
 * The wordmark opens centred and large, then shrinks into the corner and stays as the header.
 *
 * It is one element the whole way, scaled and translated rather than crossfaded between two copies:
 * a crossfade at these sizes reads as two different things, and the point is that the brand *became*
 * the header. `transformOrigin: "left top"` puts the element at its FINAL position and treats the
 * opening as the displacement, which keeps the landing exact — the alternative, animating `left`
 * and `fontSize`, reflows text every frame and lands a pixel or two off.
 */
const BRAND = { left: 60, top: 40, size: 26, cardSize: 64 };
// Where the travelling wordmark starts: exactly on top of the one BrandCard would have drawn.
// Measured off a rendered card — its wordmark inks at x 492..712, y 239..286 — rather than derived,
// because nothing here computes text metrics. Recheck all three if the wordmark, its size, or the
// canvas changes; if they drift the opening will visibly jump on the first morph frame.
const OPEN_DX = 428;
const OPEN_DY = 185;

const Header: React.FC<{ frame: number; subject: string }> = ({ frame, subject }) => {
  // No fade-in. The clip loops forever and it ENDS on this same card, so starting at full opacity
  // makes the loop seam invisible; fading up from nothing put one black frame between the end card
  // and the opening, which is the blink you see once per repeat.
  const m = interpolate(frame, [T.morphStart, T.morphEnd], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  const open = BRAND.cardSize / BRAND.size;
  const scale = open + (1 - open) * m;
  const heading = appear(frame, T.headingIn);
  const travelling = frame >= T.morphStart;
  // The card leaves linearly and in half the morph's span, while the wordmark travels on the eased
  // curve for all of it. Two separate rates on purpose: sharing the eased curve emptied the frame
  // before the lanes arrived, and matching the morph's full span made the tagline linger. Leaving
  // early is only safe because the lanes start at `lanesIn`, part-way through, and rise into it.
  const card =
    1 -
    interpolate(frame, [T.morphStart, T.morphStart + 13], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
  return (
    <>
      <BrandCard opacity={card} wordmark={!travelling} />
      <div
        style={{
          position: "absolute",
          left: BRAND.left,
          top: BRAND.top,
          transformOrigin: "left top",
          transform: `translate(${OPEN_DX * (1 - m)}px, ${OPEN_DY * (1 - m)}px) scale(${scale})`,
          font: `700 ${BRAND.size}px ${F.ui}`,
          color: C.ink,
          letterSpacing: -0.5,
          whiteSpace: "nowrap",
          opacity: travelling ? 1 : 0,
        }}
      >
        Backlot
      </div>
      <div
        style={{
          position: "absolute",
          left: BRAND.left + 118,
          top: BRAND.top + 3,
          font: `500 22px ${F.ui}`,
          color: C.ink,
          whiteSpace: "nowrap",
          opacity: heading,
        }}
      >
        <span style={{ color: C.inkDim, marginRight: 10 }}>·</span>
        Connecting <span style={{ color: C.mint }}>{subject}</span> to your app
      </div>
    </>
  );
};

/**
 * The left lane's running cost. It counts in wall-clock minutes to ~13, then gives up on minutes
 * and says "day 2" — because the admin-approval gate is not measured in minutes, and pretending
 * it is would undersell the honest complaint.
 */
const Clock: React.FC<{ frame: number }> = ({ frame }) => {
  const t = appear(frame, T.lanesIn + 6);
  const stalled = frame >= T.wall;
  const mins = Math.min(13, Math.floor(interpolate(frame, [T.lanesIn, T.wall], [0, 13], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  })));
  return (
    <div
      style={{
        opacity: t,
        font: `600 15px ${F.mono}`,
        color: stalled ? C.blocked : C.amber,
      }}
    >
      {stalled ? "elapsed  day 2" : `elapsed  ${String(mins).padStart(2, "0")}:00`}
    </div>
  );
};

/**
 * A lane is a full-height column whose verdict is pushed to the bottom with `marginTop: auto`.
 *
 * That is what makes the two verdicts share a baseline. Letting them sit after their own content
 * put "a day gone" 46px above "seconds", because the right lane carries a response card the left
 * does not — and two numbers at different heights read as two facts rather than one comparison.
 */
const Lane: React.FC<{
  children: React.ReactNode;
  verdict: React.ReactNode;
  align: "left" | "right";
  style?: React.CSSProperties;
}> = ({ children, verdict, align, style }) => (
  <div
    style={{
      ...style,
      // Half the inner width each, with a symmetric gutter so neither column's text touches the
      // rule at 50%. Asymmetric padding put "seconds" exactly on the divider.
      width: (W - 120) / 2,
      height: "100%",
      display: "flex",
      flexDirection: "column",
      paddingRight: align === "left" ? GUTTER : 0,
      paddingLeft: align === "right" ? GUTTER : 0,
    }}
  >
    {children}
    <div style={{ marginTop: "auto" }}>{verdict}</div>
  </div>
);

/**
 * The brand card, drawn at both ends of the piece: it opens on this, and closes on it.
 *
 * `wordmark` is hidden while the opening plays, because the travelling copy in `Header` stands in
 * for it — the two are positioned to coincide, so hiding one and drawing the other is invisible.
 * Keeping the card as one flex column is the point: the tagline and the command land wherever this
 * layout puts them, and the opening cannot drift from the ending because it is the same layout.
 */
const BrandCard: React.FC<{
  opacity: number;
  wordmark: boolean;
  /**
   * Opaque only where the card has to hide something. The closing card covers the lanes; the
   * opening one has nothing under it, and painting `C.bg` over the root's identical `C.bg` at a
   * partial alpha rounds to ±1 per channel every frame — a whole-frame flicker that 64-colour
   * quantisation turns into two visibly different backgrounds, and that also makes every held
   * frame differ, so none of them dedupe.
   */
  background?: string;
  taglineOpacity?: number;
  taglineRise?: number;
  commandOpacity?: number;
}> = ({
  opacity,
  wordmark,
  background = "transparent",
  taglineOpacity = 1,
  taglineRise = 0,
  commandOpacity = 1,
}) => (
  <AbsoluteFill
    style={{
      alignItems: "center",
      justifyContent: "center",
      background,
      opacity,
    }}
  >
    <div
      style={{
        font: `700 64px ${F.ui}`,
        color: C.ink,
        letterSpacing: -1.5,
        visibility: wordmark ? "visible" : "hidden",
      }}
    >
      Backlot
    </div>
    <div
      style={{
        font: `600 26px ${F.ui}`,
        color: C.mint,
        marginTop: 14,
        transform: `translateY(${taglineRise}px)`,
        opacity: taglineOpacity,
      }}
    >
      Serve your own enterprise playground
    </div>
    <div
      style={{
        opacity: commandOpacity,
        marginTop: 26,
        font: `500 19px ${F.mono}`,
        color: C.inkDim,
        border: `1px solid ${C.panelEdge}`,
        borderRadius: 8,
        padding: "10px 18px",
        background: C.panel,
      }}
    >
      pip install backlot
    </div>
  </AbsoluteFill>
);

const EndCard: React.FC<{ frame: number }> = ({ frame }) => (
  <BrandCard
    // Arrives over 5 frames, not the usual 9. It crossfades against the densest picture in the
    // piece, and halfway through a longer fade neither the lanes nor the card carry enough contrast
    // to read — a wash that looks like a dropout. Its contents still stagger in at the normal rate.
    opacity={interpolate(frame, [T.endcard, T.endcard + 5], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: Easing.out(Easing.cubic),
    })}
    background={C.bg}
    wordmark
    taglineOpacity={appear(frame, T.endcard + 5)}
    taglineRise={rise(appear(frame, T.endcard + 5), 8)}
    commandOpacity={appear(frame, T.endcard + 11)}
  />
);

export const SlackOnboarding: React.FC<{ frame: number }> = ({ frame }) => {

  // No fade-out: the closing card is opaque and covers the lanes as it arrives. Fading them first
  // left a stretch with the lanes gone and the card not yet in, which read as a blink.
  const lanes = appear(frame, T.lanesIn);

  const divider = interpolate(frame, [T.lanesIn, T.lanesIn + 12], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });

  // Slack's column leaves to the left and Backlot's takes its place. One lane's width, so the
  // survivor lands exactly where the other was — the eye follows a column that moved, not a cut.
  const LANE = (W - 120) / 2;
  const slid = interpolate(frame, [T.slide, T.slide + 20], [0, -LANE], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  // Faded as well as moved: at -LANE the departing column still has 26px inside the frame, and a
  // sliver of the thing we just said goodbye to is worse than none of it.
  const leaving = 1 - interpolate(frame, [T.slide, T.slide + 16], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // The lanes hand over on the frame the slide settles. A fade here overlapped the panel's own
  // fade and left one frame with neither fully present, which is the same blink in a new place.
  const lanesOut = frame >= T.slide + 20 ? 0 : 1;
  const i = activeServed(frame, T.cycle);
  const subject = i < 0 ? "Slack" : SERVED[i].subject;

  return (
    <AbsoluteFill style={{ background: C.bg }}>
      <Header frame={frame} subject={subject} />

      <AbsoluteFill style={{ opacity: lanes * lanesOut, padding: "96px 60px 40px" }}>
        <div
          style={{
            display: "flex",
            position: "relative",
            height: "100%",
            transform: `translateX(${slid}px)`,
          }}
        >
          {/* Left: the real Slack API, by hand. Leaves first, and leaves entirely. */}
          <Lane
            style={{ opacity: leaving }}
            align="left"
            verdict={
              <Verdict
                frame={frame}
                at={T.verdict}
                big="a day gone"
                sub="before the first response"
                accent={C.amber}
              />
            }
          >
            <LaneHead
              frame={frame}
              at={T.lanesIn}
              label="Straight at Slack"
              accent={C.amber}
              aside={<Clock frame={frame} />}
            />
            {GATES.map((g, i) => (
              <Step
                key={g.text}
                frame={frame}
                at={T.gates[i]}
                text={g.text}
                note={g.note}
                tone={C.amber}
                count={i + 1}
              />
            ))}
            <Wall frame={frame} at={T.wall} />
          </Lane>

          {/* The rule between them, drawn once. */}
          <div
            style={{
              position: "absolute",
              left: "50%",
              top: 0,
              width: 1,
              height: `${divider * 100}%`,
              background: C.panelEdge,
            }}
          />

          {/* Right: the same job against Backlot. */}
          <Lane
            align="right"
            verdict={
              <Verdict
                frame={frame}
                at={T.verdict}
                big="a few seconds"
                accent={C.mint}
              />
            }
          >
            <LaneHead frame={frame} at={T.lanesIn} label="With Backlot" accent={C.mint} />
            {STEPS.map((s, i) => (
              <Step
                key={i}
                frame={frame}
                at={T.steps[i]}
                text={s.text}
                note={s.note}
                tone={C.mint}
                mono
              />
            ))}
            <ResponseCard frame={frame} at={T.fields} />
            <Chips
              frame={frame}
              at={T.chips}
              items={["same response shapes", "pagination", "per-document ACLs"]}
            />
          </Lane>
        </div>
      </AbsoluteFill>

      {/* The card lands at the x the surviving column slides to; the tally fills the half the
          departing one left, so the frame stays as full as the comparison it replaced. */}
      {/* 736 wide, and the checklist starts at 850: the longest base_url — Confluence's
          /atlassian/wiki/rest/api/ — runs to x=809 at 20px mono, and at the old 606/760 it ran
          straight through the list. Shrinking the type or dropping the scheme from the URL would
          have cost either the hierarchy or the accuracy; the gutter was the cheaper thing to spend. */}
      <SourceCycle frame={frame} cycle={T.cycle} left={94} width={736} />
      <SourceTally frame={frame} cycle={T.cycle} slide={T.slide} left={908} rule={866} />

      <EndCard frame={frame} />
    </AbsoluteFill>
  );
};

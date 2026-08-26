import React from "react";
import { AbsoluteFill, useCurrentFrame, interpolate, Easing } from "remotion";
import { C, F, T } from "./theme";
import { appear, vanish, rise } from "./anim";
import { LaneHead, Step, Wall, Chips, ResponseCard, Verdict } from "./parts";

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

const STEPS: { text: string; note?: string }[] = [
  { text: "pip install backlot" },
  { text: "backlot serve" },
  { text: 'base_url="localhost:8000/slack/api/"', note: "the only line that changes" },
];

/** Title card, gone by the time the lanes arrive. */
const Title: React.FC<{ frame: number }> = ({ frame }) => {
  const o = appear(frame, T.titleIn) * vanish(frame, T.titleOut, 10);
  return (
    <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", opacity: o }}>
      <div style={{ font: `700 52px ${F.ui}`, color: C.ink, letterSpacing: -1 }}>
        Connecting Slack to your app
      </div>
      <div style={{ font: `450 23px ${F.ui}`, color: C.inkDim, marginTop: 12 }}>
        the same job, two ways
      </div>
    </AbsoluteFill>
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
}> = ({ children, verdict, align }) => (
  <div
    style={{
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

const EndCard: React.FC<{ frame: number }> = ({ frame }) => {
  const t = appear(frame, T.endcard);
  return (
    <AbsoluteFill
      style={{
        alignItems: "center",
        justifyContent: "center",
        background: C.bg,
        opacity: t,
      }}
    >
      <div style={{ font: `700 64px ${F.ui}`, color: C.ink, letterSpacing: -1.5 }}>Backlot</div>
      <div
        style={{
          font: `600 26px ${F.ui}`,
          color: C.mint,
          marginTop: 14,
          transform: `translateY(${rise(appear(frame, T.endcard + 5), 8)}px)`,
          opacity: appear(frame, T.endcard + 5),
        }}
      >
        Bring your own enterprise. Serve it like the real thing.
      </div>
      <div
        style={{
          opacity: appear(frame, T.endcard + 11),
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
};

export const SlackOnboarding: React.FC = () => {
  const frame = useCurrentFrame();

  // The lanes fade for the end card rather than cutting, so the last GIF frame is a clean hold.
  const lanes = appear(frame, T.lanesIn) * vanish(frame, T.endcard - 6, 10);

  const divider = interpolate(frame, [T.lanesIn, T.lanesIn + 12], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });

  return (
    <AbsoluteFill style={{ background: C.bg }}>
      <Title frame={frame} />

      <AbsoluteFill style={{ opacity: lanes, padding: "48px 60px 40px" }}>
        <div style={{ display: "flex", position: "relative", height: "100%" }}>
          {/* Left: the real Slack API, by hand. */}
          <Lane
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
                big="seconds"
                sub="and it runs in CI"
                accent={C.mint}
              />
            }
          >
            <LaneHead frame={frame} at={T.lanesIn} label="With Backlot" accent={C.mint} />
            {STEPS.map((s, i) => (
              <Step
                key={s.text}
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

      <EndCard frame={frame} />
    </AbsoluteFill>
  );
};

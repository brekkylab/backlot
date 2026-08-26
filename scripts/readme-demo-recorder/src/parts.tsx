import React from "react";
import { C, F } from "./theme";
import { appear, rise, IN_FRAMES } from "./anim";

/** A lane's heading: a dot in the lane's accent, a label, and an optional aside on the same line. */
export const LaneHead: React.FC<{
  frame: number;
  at: number;
  label: string;
  accent: string;
  aside?: React.ReactNode;
}> = ({ frame, at, label, accent, aside }) => {
  const t = appear(frame, at);
  return (
    <div style={{ opacity: t, marginBottom: 26 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ width: 10, height: 10, borderRadius: 5, background: accent }} />
        <div
          style={{
            font: `600 21px ${F.ui}`,
            color: C.ink,
            letterSpacing: 0.2,
          }}
        >
          {label}
        </div>
        {aside ? <div style={{ marginLeft: 14 }}>{aside}</div> : null}
      </div>
    </div>
  );
};

/**
 * One step in a lane. `tone` decides whether it reads as another chore (amber) or as done (mint).
 *
 * The marker differs per lane on purpose, and it is the argument the video is making. A ticked box
 * says *achieved*; six amber ticks down the left would read as a job going well, which is the
 * opposite of the complaint. So the left counts — 1, 2, 3 … — and a growing numbered list is a
 * backlog on sight. Only the right lane earns ticks, and its tick lands `IN_FRAMES` after the row
 * so the reader sees something *become* done rather than arrive done.
 */
export const Step: React.FC<{
  frame: number;
  at: number;
  text: string;
  tone: string;
  mono?: boolean;
  note?: string;
  count?: number;
}> = ({ frame, at, text, tone, mono, note, count }) => {
  const t = appear(frame, at);
  const ticked = appear(frame, at + IN_FRAMES);
  return (
    <div
      style={{
        opacity: t,
        transform: `translateY(${rise(t)}px)`,
        display: "flex",
        alignItems: "flex-start",
        gap: 12,
        marginBottom: 13,
      }}
    >
      {count === undefined ? (
        <div
          style={{
            width: 17,
            height: 17,
            marginTop: 2,
            flexShrink: 0,
            borderRadius: 4,
            border: `2px solid ${tone}`,
            background: ticked > 0.5 ? tone : "transparent",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {ticked > 0.5 ? (
            <div style={{ font: `900 11px ${F.ui}`, color: C.bg, lineHeight: 1 }}>✓</div>
          ) : null}
        </div>
      ) : (
        <div
          style={{
            width: 17,
            height: 17,
            marginTop: 2,
            flexShrink: 0,
            borderRadius: 4,
            border: `1px solid ${tone}44`,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            font: `700 11px ${F.mono}`,
            color: tone,
          }}
        >
          {count}
        </div>
      )}
      <div>
        <div
          style={{
            font: mono ? `500 16.5px ${F.mono}` : `450 17px ${F.ui}`,
            color: C.ink,
            lineHeight: 1.35,
          }}
        >
          {text}
        </div>
        {note ? (
          <div style={{ font: `450 14px ${F.ui}`, color: C.inkFaint, marginTop: 3 }}>{note}</div>
        ) : null}
      </div>
    </div>
  );
};

/** The gate the left lane cannot open by itself. Amber turns to a stalled red. */
export const Wall: React.FC<{ frame: number; at: number }> = ({ frame, at }) => {
  const t = appear(frame, at);
  return (
    <div
      style={{
        opacity: t,
        transform: `translateY(${rise(t)}px)`,
        marginTop: 4,
        padding: "11px 14px",
        borderRadius: 8,
        background: C.panel,
        border: `1px solid ${C.blocked}`,
      }}
    >
      <div style={{ font: `600 16px ${F.ui}`, color: C.blocked }}>
        Waiting on a workspace admin to approve the install…
      </div>
      <div style={{ font: `450 14px ${F.ui}`, color: C.inkDim, marginTop: 4 }}>
        Still zero API calls made
      </div>
    </div>
  );
};

/** Small caps chips — the fidelity claims, stated as nouns rather than sentences. */
export const Chips: React.FC<{ frame: number; at: number; items: string[] }> = ({
  frame,
  at,
  items,
}) => (
  <div style={{ display: "flex", gap: 7, flexWrap: "wrap", marginTop: 13 }}>
    {items.map((label, i) => {
      const t = appear(frame, at + i * 5);
      return (
        <div
          key={label}
          style={{
            opacity: t,
            font: `600 13px ${F.ui}`,
            color: C.mint,
            border: `1px solid ${C.panelEdge}`,
            borderRadius: 999,
            padding: "5px 11px",
            background: C.bg,
          }}
        >
          {label}
        </div>
      );
    })}
  </div>
);

/**
 * The payoff: an actual Slack-shaped response.
 *
 * The field names, their order and the value formats are what a real `conversations.history`
 * message carries — that is the claim the whole video is making, so it is the one place here that
 * must not be invented. The ids and text are from the bundled corpus, not from anyone's workspace.
 */
const FIELDS: [string, string][] = [
  ['"type"', '"message"'],
  ['"user"', '"U4947ECBA48"'],
  ['"text"', '"Anyone seeing 502s from the gateway?"'],
  ['"ts"', '"1770746400.000100"'],
  ['"reactions"', "[ … ]"],
];

export const ResponseCard: React.FC<{ frame: number; at: number }> = ({ frame, at }) => {
  const t = appear(frame, at);
  return (
    <div
      style={{
        opacity: t,
        transform: `translateY(${rise(t)}px)`,
        marginTop: 15,
        borderRadius: 9,
        border: `1px solid ${C.panelEdge}`,
        background: C.panel,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "8px 13px",
          borderBottom: `1px solid ${C.panelEdge}`,
          font: `500 13.5px ${F.mono}`,
          color: C.inkDim,
        }}
      >
        GET /slack/api/conversations.history
      </div>
      <div style={{ padding: "11px 13px" }}>
        {FIELDS.map(([k, v], i) => {
          const ft = appear(frame, at + 6 + i * 7);
          return (
            <div
              key={k}
              style={{
                opacity: ft,
                font: `500 14.5px ${F.mono}`,
                lineHeight: 1.65,
                whiteSpace: "nowrap",
              }}
            >
              <span style={{ color: C.inkDim }}>{k}</span>
              <span style={{ color: C.inkFaint }}>: </span>
              <span style={{ color: C.mint }}>{v}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

/** The closing number for a lane — the one thing a reader should remember from it. */
export const Verdict: React.FC<{
  frame: number;
  at: number;
  big: string;
  sub: string;
  accent: string;
}> = ({ frame, at, big, sub, accent }) => {
  const t = appear(frame, at);
  return (
    <div style={{ opacity: t, transform: `translateY(${rise(t, 14)}px)` }}>
      <div style={{ font: `700 46px ${F.ui}`, color: accent, letterSpacing: -0.8 }}>{big}</div>
      <div style={{ font: `450 17px ${F.ui}`, color: C.inkDim, marginTop: 2 }}>{sub}</div>
    </div>
  );
};

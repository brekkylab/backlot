import React from "react";
import { C, F } from "./theme";
import { appear, rise, IN_FRAMES } from "./anim";
import { AbsoluteFill, interpolate, Easing } from "./runtime";

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
  text: React.ReactNode;
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
            width: 19,
            height: 19,
            marginTop: 1,
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
            <div style={{ font: `900 15px ${F.ui}`, color: C.bg, lineHeight: 1 }}>✓</div>
          ) : null}
        </div>
      ) : (
        <div
          style={{
            width: 19,
            height: 19,
            marginTop: 1,
            flexShrink: 0,
            borderRadius: 4,
            border: `1px solid ${tone}44`,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            font: `700 12px ${F.mono}`,
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

/**
 * The closing number for a lane — the one thing a reader should remember from it.
 *
 * `sub` is optional, but the line it would occupy is reserved either way. The two verdicts are
 * bottom-aligned within their lanes, so dropping a subtitle on one side alone would push that
 * side's number down by its height — and two numbers at different heights stop reading as one
 * comparison.
 */
export const Verdict: React.FC<{
  frame: number;
  at: number;
  big: string;
  sub?: string;
  accent: string;
}> = ({ frame, at, big, sub, accent }) => {
  const t = appear(frame, at);
  return (
    <div style={{ opacity: t, transform: `translateY(${rise(t, 14)}px)` }}>
      <div style={{ font: `700 46px ${F.ui}`, color: accent, letterSpacing: -0.8 }}>{big}</div>
      <div style={{ font: `450 17px ${F.ui}`, color: C.inkDim, marginTop: 2, minHeight: 21 }}>
        {sub ?? ""}
      </div>
    </div>
  );
};

/**
 * The second act: the same two commands, one source after another.
 *
 * Every field below was read off this server. `GET /gmail/v1/users/{u}/messages/{id}` really answers
 * with `threadId` and `snippet`, Drive with `kind` and `mimeType`, S3 in XML the SDK surfaces as
 * `Key`/`ETag`, Jira with everything nested under `fields`. That is the whole claim of the piece, so
 * inventing any of it would hollow out the one thing it exists to show.
 *
 * Each entry carries exactly five fields, which is not tidiness: the card's height has to be
 * constant for one entry to cross-fade into the next without the chrome around it resizing.
 */
export type Served = {
  subject: string;
  prefix: string;
  request: string;
  fields: [string, string][];
};

export const SERVED: Served[] = [
  {
    subject: "Gmail",
    prefix: "/gmail/v1/",
    request: "GET /gmail/v1/users/{u}/messages/{id}",
    fields: [
      ['"id"', '"54f1612bc38074f"'],
      ['"threadId"', '"678c8fb471af4dd2"'],
      ['"labelIds"', '["INBOX"]'],
      ['"snippet"', '"Let\'s propose the growth tier…"'],
      ['"internalDate"', '"1773159900000"'],
    ],
  },
  {
    subject: "Google Drive",
    prefix: "/drive/v3/",
    request: "GET /drive/v3/files",
    fields: [
      ['"kind"', '"drive#file"'],
      ['"name"', '"Northwind Proposal"'],
      ['"mimeType"', '"application/vnd.google-apps.document"'],
      ['"modifiedTime"', '"2026-02-20T10:10:00Z"'],
      ['"owners"', '[ { "displayName": "Dana Whitfield", … } ]'],
    ],
  },
  {
    subject: "GitHub",
    prefix: "/github/",
    request: "GET /github/repos/{o}/{r}/issues",
    fields: [
      ['"number"', "87345"],
      ['"title"', '"Document the v2 flag"'],
      ['"state"', '"open"'],
      ['"user"', '{ "login": "lena-fischer", … }'],
      ['"labels"', '[ { "name": "docs", "color": "ededed", … } ]'],
    ],
  },
  {
    subject: "Jira",
    prefix: "/atlassian/rest/api/3/",
    request: "GET /atlassian/rest/api/3/search/jql",
    fields: [
      ['"key"', '"PLAD294FC-1825"'],
      ['"fields.summary"', '"Buffer the offset file write"'],
      ['"fields.status"', '{ "name": "To Do", … }'],
      ['"fields.issuetype"', '{ "name": "Sub-task", … }'],
      ['"fields.labels"', '["flaky-test"]'],
    ],
  },
  {
    subject: "Confluence",
    prefix: "/atlassian/wiki/rest/api/",
    request: "GET /atlassian/wiki/rest/api/content",
    fields: [
      ['"id"', '"1815518"'],
      ['"type"', '"page"'],
      ['"status"', '"current"'],
      ['"title"', '"On-call Expectations"'],
      ['"space"', '{ "key": "HANB4128D", "name": "handbook", … }'],
    ],
  },
  {
    subject: "Notion",
    prefix: "/notion/v1/",
    request: "POST /notion/v1/search",
    fields: [
      ['"object"', '"page"'],
      ['"id"', '"0a2a1fb8-5f7d-43cc-9208-3244c75c60bc"'],
      ['"created_time"', '"2026-02-19T09:55:00Z"'],
      ['"icon"', '{ "type": "emoji", "emoji": "🎨" }'],
      ['"parent"', '{ "type": "workspace", "workspace": true }'],
    ],
  },
  {
    subject: "HubSpot",
    prefix: "/hubspot/crm/v3/",
    request: "GET /hubspot/crm/v3/objects/{type}",
    fields: [
      ['"id"', '"3215882413"'],
      ['"properties"', '{ "email": "sam.ortiz@northwind.example", … }'],
      ['"createdAt"', '"2026-01-14T09:05:00.000Z"'],
      ['"updatedAt"', '"2026-01-14T09:05:00.000Z"'],
      ['"archived"', "false"],
    ],
  },
  {
    subject: "Amazon S3",
    prefix: "/s3/",
    request: "GET /s3/{bucket}?list-type=2",
    fields: [
      ['"Key"', '"builds/pipeline/2026-02-17/manifest.json"'],
      ['"LastModified"', '"2026-02-17T03:30:00+00:00"'],
      ['"ETag"', '"fb7eb5c32fbcc41f873658f2e9b2ab99"'],
      ['"Size"', "75"],
      ['"StorageClass"', '"STANDARD"'],
    ],
  },
];

export const activeServed = (frame: number, cycle: readonly number[]) => {
  let i = -1;
  for (let k = 0; k < cycle.length; k++) if (frame >= cycle[k]) i = k;
  return i;
};

/**
 * One line handing over to the next: out, then in, with no moment showing both.
 *
 * Rows are staggered a frame apart, so the block never loses more than a couple of lines at once
 * and the eye reads a ripple rather than a blank. That also keeps it cheap: the alternative — two
 * copies of the whole card cross-fading with a translateY — moved every pixel of a 136px block on
 * every frame of every handover, which defeats the delta compression and cost 700 KB.
 */
const OUT = 2;
const IN = 3;

const swapRow = (frame: number, at: number, n: number, prev: React.ReactNode, cur: React.ReactNode) => {
  const t = frame - (at + n);
  if (prev === null || t >= OUT) {
    const k = Math.min(1, Math.max(0, (t - OUT) / IN));
    return <div style={{ opacity: prev === null ? 1 : k }}>{cur}</div>;
  }
  return <div style={{ opacity: 1 - Math.max(0, t) / OUT }}>{prev}</div>;
};

const tick = (
  <div
    style={{
      width: 19,
      height: 19,
      marginTop: 1,
      flexShrink: 0,
      borderRadius: 4,
      border: `2px solid ${C.mint}`,
      background: C.mint,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
    }}
  >
    <div style={{ font: `900 15px ${F.ui}`, color: C.bg, lineHeight: 1 }}>✓</div>
  </div>
);

const prefixLine = (s: Served) => (
  <div style={{ font: `500 20px ${F.mono}`, color: C.ink, whiteSpace: "nowrap" }}>
    base_url=<span style={{ color: C.mint }}>&quot;http://localhost:8000{s.prefix}&quot;</span>
  </div>
);

const requestLine = (s: Served) => (
  <div style={{ font: `500 17px ${F.mono}`, color: C.inkDim }}>{s.request}</div>
);

const fieldRow = ([k, v]: [string, string]) => (
  <div style={{ font: `500 17px ${F.mono}`, lineHeight: 2.1, whiteSpace: "nowrap" }}>
    <span style={{ color: C.inkDim }}>{k}</span>
    <span style={{ color: C.inkFaint }}>: </span>
    <span style={{ color: C.mint }}>{v}</span>
  </div>
);

export const SourceCycle: React.FC<{
  frame: number;
  cycle: readonly number[];
  left: number;
  width: number;
}> = ({ frame, cycle, left, width }) => {
  const i = activeServed(frame, cycle);
  if (i < 0) return null;
  const cur = SERVED[i];
  const prev = i > 0 ? SERVED[i - 1] : null;
  const at = cycle[i];

  return (
    <div style={{ position: "absolute", left, top: 118, width }}>
      {/* The two lines that never change stay put and stay ticked: the point is that the third is
          the only part of the story that differs per source. */}
      {["pip install backlot", "backlot serve"].map((line) => (
        <div
          key={line}
          style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 17 }}
        >
          {tick}
          <div style={{ font: `500 20px ${F.mono}`, color: C.ink }}>{line}</div>
        </div>
      ))}

      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        {tick}
        <div style={{ flex: 1 }}>
          <div style={{ height: 26 }}>
            {swapRow(frame, at, 0, prev ? prefixLine(prev) : null, prefixLine(cur))}
          </div>
          <div style={{ font: `450 15px ${F.ui}`, color: C.inkFaint, marginTop: 4 }}>
            the only line that changes
          </div>
        </div>
      </div>

      <div
        style={{
          marginTop: 30,
          borderRadius: 9,
          border: `1px solid ${C.panelEdge}`,
          background: C.panel,
          overflow: "hidden",
        }}
      >
        <div style={{ padding: "14px 17px", borderBottom: `1px solid ${C.panelEdge}` }}>
          <div style={{ height: 20 }}>
            {swapRow(frame, at, 1, prev ? requestLine(prev) : null, requestLine(cur))}
          </div>
        </div>
        <div style={{ padding: "18px 17px" }}>
          <div style={{ height: 180 }}>
            {cur.fields.map(([k, v], n) => (
              <div key={n} style={{ height: 36 }}>
                {swapRow(
                  frame,
                  at,
                  2 + n,
                  prev ? fieldRow(prev.fields[n]) : null,
                  fieldRow([k, v]),
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

/**
 * The checklist in the half the Slack column vacated.
 *
 * It slides in complete, during the same frames the columns move, and then fills in: Slack is
 * already ticked because the first act just proved it, and every other row gets its tick as its own
 * card arrives. Accumulating the rows instead — adding a name at a time — kept the list's height
 * changing under the reader and never showed where the sequence was going; a checklist that is all
 * there from the start says how much is left.
 *
 * The rule to its left is doing real work: at 850 the list sat close enough to the longest base_url
 * to read as one crowded block. Pushed out to 908 with a divider between them, the frame has two
 * halves again, which is the shape the first act established.
 *
 * No total: it ends in "and more" because a count goes stale the next time a source is added, which
 * is the same reason the README is tested for not stating one.
 */
const TALLIED = ["Slack", ...SERVED.map((s) => s.subject)];
const ROW = 40;

export const SourceTally: React.FC<{
  frame: number;
  cycle: readonly number[];
  slide: number;
  left: number;
  rule: number;
}> = ({ frame, cycle, slide, left, rule }) => {
  if (frame < slide) return null;
  const inT = interpolate(frame, [slide, slide + 20], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  const tickedAt = (n: number) => (n === 0 ? slide : cycle[n - 1]);
  const current = activeServed(frame, cycle) + 1;

  return (
    <>
      {/* Drawn where it lands rather than sliding in with the list: a rule that moves reads as
          another object arriving, when its job is to say the frame has two sides. */}
      <div
        style={{
          position: "absolute",
          left: rule,
          top: 122,
          width: 1,
          height: 436,
          background: C.panelEdge,
          opacity: inT,
        }}
      />
      <div
        style={{
          position: "absolute",
          left,
          top: 122,
          opacity: inT,
          transform: `translateX(${(1 - inT) * 90}px)`,
        }}
      >
        <div style={{ font: `700 17px ${F.ui}`, color: C.inkDim, letterSpacing: 1.4 }}>
          EVERY SOURCE
        </div>
        <div style={{ marginTop: 16 }}>
          {TALLIED.map((name, n) => {
            const on = frame >= tickedAt(n);
            const now = n === current;
            const k = on ? appear(frame, tickedAt(n)) : 0;
            return (
              <div
                key={name}
                style={{ height: ROW, display: "flex", alignItems: "center", gap: 11 }}
              >
                <div
                  style={{
                    width: 19,
                    height: 19,
                    flexShrink: 0,
                    borderRadius: 4,
                    border: `1.5px solid ${on ? C.mint : C.inkFaint + "66"}`,
                    background: on ? C.mint : "transparent",
                    opacity: on ? k : 1,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  {on ? (
                    <div style={{ font: `900 13px ${F.ui}`, color: C.bg, lineHeight: 1 }}>✓</div>
                  ) : null}
                </div>
                <div
                  style={{
                    font: `${now ? 600 : 450} 26px ${F.ui}`,
                    color: now ? C.ink : on ? C.inkDim : C.inkFaint,
                  }}
                >
                  {name}
                </div>
              </div>
            );
          })}
          <div
            style={{
              height: ROW,
              display: "flex",
              alignItems: "center",
              font: `450 21px ${F.ui}`,
              color: C.inkFaint,
              marginLeft: 30,
            }}
          >
            and more
          </div>
        </div>
      </div>
    </>
  );
};

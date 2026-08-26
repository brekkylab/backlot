# README demo recorder

Renders the animation the README embeds at [`assets/demo.gif`](../../assets/demo.gif). A
[Remotion](https://remotion.dev) composition, encoded to a GIF. Build-only — nothing here ships in
the wheel. [← README](../../README.md)

## Rebuilding

```bash
brew install ffmpeg gifsicle
cd scripts/readme-demo-recorder && npm install && ./build.sh
```

`build.sh` renders to `out/video.mp4`, encodes `../../assets/demo.gif`, and prints its size.
`npm run studio` opens Remotion's editor with a scrubbable timeline, which is the fast way to judge
a timing change. `npx tsc --noEmit` typechecks.

The composition is `src/SlackOnboarding.tsx`; the storyboard is the `T` map of named beats in
`src/theme.ts`, in frames at 30fps.

## What the demo argues

Connecting Slack costs a day of dashboard work, or one changed line. Both lanes run on **one
clock, side by side**, and that is the whole device: the right lane is already returning messages
while the left is still filling in OAuth scopes. Told in sequence — first the slow way, then the
fast way — a reader has to hold the first half in memory to feel the second. Run in parallel, the
comparison is just what the frame looks like.

The left lane's markers are **numbers, not ticks**. Six amber checkmarks read as a job going well,
which is the opposite of the complaint; a growing numbered list is a backlog on sight. Only the
right lane earns ticks, and each lands a few frames after its row, so the reader sees something
*become* done rather than arrive done.

The response card is the one place that must not be invented. The field names, their order and the
value formats are what a real `conversations.history` message carries — that is the claim the whole
piece is making. The ids and text come from the bundled corpus, not from anyone's workspace.

No Slack logo or wordmark styling appears. The service is named in text, which is nominative use; a
redrawn vendor mark in our own README is a trademark question we have no reason to open.

## Why it is drawn rather than recorded

The demo this replaced was a real terminal session, and it failed on the only audience that
matters: a reader who has never seen the project. `curl | jq` output beside more `curl | jq` output
shows two JSON blobs matching, but it never says what that is *worth*, and it assumes the reader
already knows what setting up a Slack app costs. The comparison worth drawing is against the
alternative, and the alternative is a browser and a dashboard — which no terminal recorder can film.

That also retires the previous demo's central problem. Half of its frame had to be a real Slack
response, which cannot be captured here: an authenticated call would put a workspace token in a
public asset, and the values would differ anyway, because real Slack answers about a real workspace
and Backlot answers about your corpus. Staging it meant a file-swap that
[vhs](https://github.com/charmbracelet/vhs) could not hide — `Hide` stops frame capture while the
typed line stays in the terminal and returns on `Show`. An infographic has no such seam, because
nothing in it pretends to be a capture.

**Remotion's licence is the one thing to keep an eye on.** It is free for individuals and for
companies up to three employees, with no open-source exception — so this is inside the free tier at
our size and becomes a paid dependency above it. It is build-only and never enters the wheel, so
the exposure is this directory alone.

## Keeping the GIF small

348 KB at 900×480. The ceiling is **2 MB** — it is inline at the top of the README, and a reader on
a slow connection pays for it before they read a word.

Almost all of that is design, not encoder flags. The background never moves, fills are flat, and
every entrance is a **clamped ease that actually reaches its end value** rather than a spring.
A spring settles asymptotically, so a row that has visually arrived is still moving by a subpixel
for another twenty frames, and a subpixel move rewrites the row's whole bounding box. Because the
held stretches are byte-identical instead, `gifsicle` collapses 225 frames to 203 images and the
rest compress to almost nothing.

The encoder settings follow from the same idea: **one global palette** for the whole clip and
`dither=none`. A per-frame palette re-dithers flat panels, which makes every frame differ from the
last and defeats the delta compression entirely.

**15fps, not 30.** The terminal demo this replaced needed 30 because it typed at 50ms per character
and smooth typing needs a frame per character. Nothing here types; the motion is opacity and a 10px
rise, and it reads the same at 15fps for half the frames.

# Assets

Everything the README renders, plus what it takes to rebuild it. Nothing here ships in the wheel.
[← README](../README.md)

One directory per demo, plus the renderer they share:

| Path | What it is |
|---|---|
| `make_cast.py` | Turns a screenplay into a cast, running its real commands as it goes. Shared by every demo |
| `readme-demo/demo.screenplay` | What the README's demo says and does — the file to edit |
| `readme-demo/demo.cast` | The intermediate [asciicast](https://docs.asciinema.org/manual/asciicast/v2/); generated, committed so a reviewer can read the frames without rendering |
| `readme-demo/demo.gif` | What the README renders |

A second demo is a new directory beside `readme-demo/` with its own screenplay; nothing in
`make_cast.py` is specific to this one.

## Rebuilding

```bash
brew install agg gifsicle
D=assets/readme-demo
python assets/make_cast.py $D/demo.screenplay $D/demo.cast 100 27
agg --font-size 20 --theme asciinema --fps-cap 30 --idle-time-limit 2 \
    --last-frame-duration 4 $D/demo.cast /tmp/demo-raw.gif
gifsicle -O3 --lossy=80 --colors 48 /tmp/demo-raw.gif -o $D/demo.gif
```

`make_cast.py` really runs the `!` and `!!` lines, so this needs a working checkout: the screenplay
imports the bundled corpus, starts a server, and kills it again. `backlot import --bundled` is a `!`
line, so the per-source counts on screen are the ones it printed. `agg` at its defaults produces a 1.9MB GIF; `gifsicle` does most of the work of getting that down,
with no visible loss on flat terminal colour. Keep the result under 2MB — it is inline on the
README, and a reader on a slow connection pays for it before they read a word.

**Do not lower `--fps-cap` to save space.** It was 8 for a while and the typing came out visibly
choppy, because `make_cast.py` types at 50ms per character and a frame every 125ms means each one
jumps two or three characters. Smooth typing needs at least one frame per character. Measured over
this cast, the saving is not worth having:

| `--fps-cap` | after `gifsicle` |
|---|---|
| 8 | 218 KB, choppy |
| 20 | 231 KB |
| 30 | 242 KB, smooth |

Consecutive terminal frames differ by a few characters, so `gifsicle` compresses the extra ones to
almost nothing. Raising the frame rate 4x cost 24 KB.

## Why the demo is written rather than recorded

The demo shows two things side by side: what Backlot returns, which is real, and what the same call
against real Slack returns, which cannot be real here. An authenticated call would need a workspace
token in a public asset — publishing that workspace's channels and member ids — and the values would
differ anyway, because real Slack answers about a real workspace and Backlot answers about your
corpus. What matches is the schema, and that is the claim the demo makes.

A session recorder cannot do that half. [vhs](https://github.com/charmbracelet/vhs) was tried first:
it can print anything, but it cannot hide the command that produces it — `Hide` stops frame capture
while the typed line stays in the terminal and comes back on `Show` — so the file-swap needed to
stage the comparison appeared on camera. Remotion was considered and dropped: its free licence stops
at three employees with no open-source exception, it brings Node and headless Chromium, and its
strength is video, which a GitHub README cannot play.

Writing the session makes the seam explicit instead of hidden, which is the part worth keeping.
each demo's screenplay is committed, and its directives say which lines were executed and which were
authored:

- `!` and `!!` really run at build time; `!` splices the real stdout
- `$`, `|` and `J` are authored

Everything Backlot answers in the demo is a `!` line — really executed, really its output. The one
staged frame is the last: that `curl` is a `$` line, typed but not run, carrying `xoxb-xxxxxxxx` and
`YOUR_ID` where your own workspace token and channel go. It cannot run, which is why the body under
it is the inline `J <<LIVE` block in `readme-demo/demo.screenplay`.

So "which half is real" is something a reviewer reads, not something they take on trust — and a
recording could never have shown them that.

## The live Slack message in the screenplay

The last beat prints a `J <<LIVE` block: one real message from a live `conversations.history`
response, fetched with a workspace token on 2026-08-20, masked, and passed through the same jq
filter shown above it. It lives inline because it is nine lines and the demo is its only reader.

What survives is the measurement — the field names, their order as the API returned them, and the
format of every value: `ts` is `<10>.<6>`, `client_msg_id` is a 36-character UUID, a user id is
`U` + 10. What was replaced is every value that identifies the workspace: the ids, the team and the
text. `blocks` reads `"[...]"` because the filter collapsed it, not because Slack sends it that way.

It exists so the demo can put the live shape beside Backlot's without a workspace token in a public
asset — which would publish that workspace's channels and member ids. The comparison is of shape,
not values: real Slack answers about a real workspace and Backlot answers about your corpus, so
identical values would be the wrong claim.

Measured at the same time, and the reason the two line up: a plain message carries seven keys on
both sides — `type`, `user`, `text`, `ts`, `team`, `client_msg_id`, `blocks`. A thread **parent**
carries seven more on both, for fourteen, which is why both sides read `.messages[-1]` — the oldest
message on the page — rather than the newest. In this corpus every channel's newest message happens
to be a thread parent.

To re-measure, fetch a fresh message, mask it the same way, and replace the block. The stronger
version of this check is a test rather than a recording: the Slack tests already diff served field
sets against committed transcriptions of the live shape, so a divergence fails CI instead of waiting
to be noticed. This block is the demo's illustration of what those tests enforce.

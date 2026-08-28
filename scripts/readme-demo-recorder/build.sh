#!/usr/bin/env bash
# Render the composition and encode the GIF the README embeds.
#
#     npm install && npx playwright install chromium && ./build.sh
#
# Needs ffmpeg and gifsicle (brew install ffmpeg gifsicle). Writes out/ here, and the GIF into
# assets/ at the repo root, which is the only part of this directory the README refers to.
set -euo pipefail
cd "$(dirname "$0")"

W=900          # inline README width; the composition renders at 1200 and is downscaled
SRC_FPS=30     # the composition's own rate, one PNG per frame
FPS=30         # every captured frame; see "Why 30fps" in README.md
               # only rates dividing SRC_FPS evenly are safe — 20 or 25 resample
               # unevenly and judder, which is worse than a lower rate
COLORS=48
GIF=../../assets/demo.gif   # the repo's one committed asset

node capture.mjs

# One palette for the whole clip, not per-frame. A per-frame palette re-dithers flat fills, which
# makes every frame differ from the last and defeats the delta compression the design is built for.
ffmpeg -v error -framerate "$SRC_FPS" -i out/frames/%04d.png \
  -vf "fps=$FPS,scale=$W:-1:flags=lanczos,palettegen=max_colors=80:stats_mode=full" \
  -y out/pal.png

# dither=none for the same reason: flat panels stay flat instead of gaining animated noise.
ffmpeg -v error -framerate "$SRC_FPS" -i out/frames/%04d.png -i out/pal.png \
  -lavfi "fps=$FPS,scale=$W:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" \
  -y out/raw.gif

gifsicle -O3 --lossy=60 --colors "$COLORS" out/raw.gif -o "$GIF"

printf '\n%s  %s  ' "$GIF" "$(du -h "$GIF" | cut -f1)"
gifsicle --info "$GIF" | sed -n '1p;2p'

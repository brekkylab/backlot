#!/usr/bin/env bash
# Render the composition and encode the GIF the README embeds.
#
#     npm install && ./build.sh
#
# Needs ffmpeg and gifsicle (brew install ffmpeg gifsicle). Writes out/ and demo.gif.
set -euo pipefail
cd "$(dirname "$0")"

W=900          # inline README width; the composition renders at 1200 and is downscaled
FPS=15         # see "Why 15fps" in ../README.md
COLORS=48

npx remotion render SlackOnboarding out/video.mp4 --log=error

# One palette for the whole clip, not per-frame. A per-frame palette re-dithers flat fills, which
# makes every frame differ from the last and defeats the delta compression the design is built for.
ffmpeg -v error -i out/video.mp4 \
  -vf "fps=$FPS,scale=$W:-1:flags=lanczos,palettegen=max_colors=64:stats_mode=full" \
  -y out/pal.png

# dither=none for the same reason: flat panels stay flat instead of gaining animated noise.
ffmpeg -v error -i out/video.mp4 -i out/pal.png \
  -lavfi "fps=$FPS,scale=$W:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" \
  -y out/raw.gif

gifsicle -O3 --lossy=60 --colors "$COLORS" out/raw.gif -o demo.gif

printf '\ndemo.gif  %s  ' "$(du -h demo.gif | cut -f1)"
gifsicle --info demo.gif | sed -n '1p;2p'

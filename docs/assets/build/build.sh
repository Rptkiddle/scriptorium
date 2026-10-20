#!/bin/zsh
# Regenerate the README artwork in docs/assets/: banners, divider and skill figures.
# Downloads the two fonts (SIL Open Font License) into a temporary folder.
set -e
HERE=${0:A:h}
T=$(mktemp -d)
curl -sSfL -o "$T/UnifrakturMaguntia-Book.ttf" \
  https://github.com/google/fonts/raw/main/ofl/unifrakturmaguntia/UnifrakturMaguntia-Book.ttf
curl -sSfL -o "$T/EBGaramond[wght].ttf" \
  "https://github.com/google/fonts/raw/main/ofl/ebgaramond/EBGaramond%5Bwght%5D.ttf"
uv run --no-project --with fonttools python -I "$HERE/build.py" \
  "$T/UnifrakturMaguntia-Book.ttf" "${HERE:h}" "$T/test" "$T/EBGaramond[wght].ttf"
rm -rf "$T"

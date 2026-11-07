#!/usr/bin/env bash
# Rebuild the scene, publish it in place to the existing gist, print the stable link.
set -euo pipefail
cd "$(dirname "$0")/.."

GIST=d926e3eb817f57f994fbaae56c19636d
USER=saikatroy973
FILE=switch-ai-agent-flow.excalidraw

node .miro/build-scene.mjs ".miro/$FILE"
cp ".miro/$FILE" "diagrams/$FILE"

python3 - "$FILE" <<'PY' > /tmp/gist-payload.json
import json, sys
fname = sys.argv[1]
content = open(f'.miro/{fname}', encoding='utf-8').read()
print(json.dumps({"files": {fname: {"content": content}}}))
PY

gh api -X PATCH "gists/$GIST" --input /tmp/gist-payload.json --jq '.updated_at' > /dev/null
RAW="https://gist.githubusercontent.com/$USER/$GIST/raw/$FILE"
echo "SCENE: $RAW"
python3 -c "import urllib.parse,sys;print('LINK:  https://excalidraw.com/#url='+urllib.parse.quote(sys.argv[1],safe=''))" "$RAW"

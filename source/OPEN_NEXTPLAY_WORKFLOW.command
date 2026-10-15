#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

WORKSPACE=$(python3 - <<'PY'
from pathlib import Path
roots = [p for p in Path('products').glob('*') if p.is_dir() and (p / 'product.json').exists()]
if roots:
    roots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    print(roots[0])
PY
)

if [ -z "$WORKSPACE" ]; then
  echo "No product workspace was found under ./products"
  echo "Run the scraper first, then open this launcher again."
  read -r -p "Press ENTER to close..." _
  exit 1
fi

echo "Opening NextPlay UGC Workflow for:"
echo "$WORKSPACE"
python3 main.py dashboard "$WORKSPACE"

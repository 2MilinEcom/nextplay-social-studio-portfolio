#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi
python3 main.py dashboard "products/elroel-blanc-cover-cream-stick-v-white-foundation-4172417451"

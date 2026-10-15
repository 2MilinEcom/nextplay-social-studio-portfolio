#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print 'Install first: python3 -m venv .venv'
  print 'Then: .venv/bin/python -m pip install -r requirements-editor.txt'
  read 'reply?Press Return to close.'
  exit 1
fi
read 'workspace_path?Paste the existing product folder path (containing product.json): '
exec .venv/bin/python nextplay.py --workspace "$workspace_path" --port 8766

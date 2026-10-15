# Install NextPlay 4.0 safely

1. Stop the old dashboard. Keep its folder and all products/media.
2. Extract the ZIP into a **new folder**. Its root is `nextplay-v4.0.2`; do not extract it over the old scraper installation.
3. In Terminal, create a separate environment:

```sh
cd ~/Downloads/nextplay-v4.0.2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-editor.txt
```

Python 3.10+ is required. Python 3.14 was used for verification. FFmpeg is required; the installed `ffmpeg` is preferred, with imageio-ffmpeg as a fallback. On a Mac, `brew install ffmpeg` installs it if needed. Text overlays use Pillow and a system font; FFmpeg drawtext is not required.

4. Launch against the **original product directory**, the one containing `product.json`:

```sh
python nextplay.py --workspace "$HOME/Downloads/scraper/products/YOUR_PRODUCT_FOLDER"
```

Replace `YOUR_PRODUCT_FOLDER` with your existing folder name. The dashboard opens on `http://127.0.0.1:8766`. Click Edit ad. To start an empty edit, open `http://127.0.0.1:8766/timeline?name=New%20edit`.

Alternatively double-click `OPEN_NEXTPLAY_V4.command` after installing dependencies and enter that product directory. Use `--port 8876` if 8766 is busy, or `--no-browser` to launch without opening a window.

The launcher reads the original scraper `.env` in place when using its standard `products/<product>` layout. It never copies it. Custom layouts can set the existing `OPENAI_API_KEY` in the launching environment. AI uses `NEXTPLAY_AI_MODEL` when configured; the default is `gpt-5.6-terra`. Local precise directions work without an API call. Do not put a credential in the release folder or ZIP.

## Files and recovery

New revisions, imported copies, thumbnail caches, draft previews and exports live under the original product's `nextplay/<edit-id>/`. Imported files get unique filenames. Export MP4s have adjacent JSON manifests with the exact timeline, revision, hashes, rights and source metadata. Review rights in that manifest before using footage.

Original ad plans and renders remain unchanged. To recover an edit, select a saved revision and click Restore as new revision. Undo/redo use saved revisions during the current browser session; after reopening, use the persistent version list. A damaged head file or most recent revision is skipped in favor of the latest valid revision. Earlier files are retained.

Keep a normal backup of the whole original product folder. To roll back the application, stop V4 and launch the original scraper installation. V4's additional `nextplay` directory remains available for recovery.

If an original source cannot be found, the migrated timeline keeps an offline placeholder at its original timing. Import or select matching footage, select the placeholder, and click Relink / replace source. Source handles must fit the replacement file. Export fails if used media remains unavailable.

## Run the included checks

```sh
python -m unittest discover -s tests -v
node tests/test_timeline_shortcuts.js
```

These checks generate temporary synthetic media and local HTTP servers. No downloaded user media is required. The optional live AI check makes one small paid API request using a credential you explicitly select:

```sh
python tests/live_ai_smoke.py --env-file "$HOME/Downloads/scraper/.env"
```

For native Safari QA, run `python tests/serve_fixture.py --root /tmp/nextplay-safari-qa`, then open `http://127.0.0.1:8874/timeline?name=Safari%20QA`. Follow the manual checklist in TEST_STATUS.md. The fixture never uses your product media.

## Keyboard editing

Click a timeline clip before using shortcuts. Press Cmd+B (Mac), Ctrl+B (Windows), or S to split at the playhead; Space plays/pauses. Open **Shortcuts** in the header, or press ?, for the full guide. Shortcuts do not intercept typing inside caption, timecode, or inspector fields.

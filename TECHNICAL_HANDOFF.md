# NextPlay / SCRAPER technical migration handoff

Prepared 13 October 2026. Baseline: `4.0.2-nextplay-multitrack-editor`, timeline schema 4.

## Preservation contract and executive overview

This is a source snapshot and technical handoff, not a redesign or platform implementation. Existing runtime source, downloaded footage, rendered ads, product data and saved edits were not changed. The new files are handoff artifacts only. No server was restarted and no external AI requests were made for this handoff.

NextPlay combines a local TikTok Shop product-to-ad pipeline with a browser-based, frame-based multitrack short-form editor. The pipeline ingests a product, creates a search plan, discovers TikTok videos, ranks and downloads sources, transcribes/analyzes footage, proposes clips and ad structures, renders ads, and supports reviews. The dedicated V4 editor opens those ads or an empty timeline, edits independent tracks, saves immutable revisions, and renders drafts/exports with source provenance.

The V4 build began from available V3.7.3 source. The earlier conversation's claimed V3.8 archive was not available locally; do not assume every historical claim describes this snapshot. Current implemented behavior is represented by the enclosed code, tests and release documentation.

The application is a useful local editing foundation, not an existing multiuser social publishing platform. There is no application login, tenant isolation, publishing connector, scheduler, analytics warehouse, email marketing service or advertising account integration.

### Source of truth

- Development source: `release-work/scraper/` in the SCRAPER project mirror.
- Installed runtime: `~/Downloads/nextplay-v4.0.1/`. The folder retains its installation name, but VERSION is V4.0.2.
- Existing data root: `~/Downloads/scraper/products/`. Keep this separately from application source.
- Runtime `.py`, `.js`, `.html` and `.css` files in the development source match the installed copy byte for byte. The release builder differs in its archive root name; the development tree additionally contains the preview regression test and an unused legacy transcript fixture.
- Neither the development source, installed V4 folder nor original scraper folder has a `.git` directory. No GitHub repository, latest branch, commit ID or remote could be identified from these copies. The ZIP hashes are the authoritative snapshot identifiers.
- The local dashboard was not reachable on port 8766 during this handoff. Previous successful runs are historical evidence, not a claim that the service is running now.

## 1. Current behavior and verification

### Working and verified

The unchanged source passed **101 Python tests** during this handoff, using synthetic media and local HTTP fixtures. `node tests/test_timeline_shortcuts.js` also passed. These cover command validation, trim tools, linked clips, animation, captions, rendering, mixing, imports, concurrency conflicts, version recovery, path confinement, local posters and package hygiene. They do not prove every external TikTok flow or every browser interaction.

Earlier V4 acceptance checks used an isolated copy of 30 real ELROEL sources and verified multitrack editing, drafts, portrait exports, restore/reopen and legacy planning. All 166 original product files retained their hashes at that earlier checkpoint. Live AI checks used generated fixtures. Native Safari drag/trim/split/undo/detach and draft playback were exercised; later Safari automation timed out. The remaining tests used the in-app browser. Consult `TEST_STATUS.md` for the precise scope rather than interpreting automated tests as full Safari certification.

The later pajama-product funnel completed in approximately 14 minutes: 86 discovered videos, 30 downloads, 30 completed analyses, 163 usable segments and 10 rendered ads. No analysis failures were reported. The run's render logs repeatedly reported skipped text overlays, and the ten ads had not been quality-reviewed at that checkpoint. These counts describe that historical run only.

### Known limitations and issues to carry forward

1. **Two rendering paths:** V4 `timeline_render.py` composites text using Pillow; the older pipeline uses `splice_videos.py` with ASS/bitmap fallback. The real funnel and synthetic legacy tests reported skipped text overlays. Do not claim captions/headlines are reliable in the old funnel just because V4 text tests pass. Root cause and remediation remain outstanding.
2. **Live versus rendered preview:** browser elements approximate rendering. Multi-decoder playback can drift; reverse video is sought frame by frame and live reverse audio is muted. FFmpeg draft output is the authoritative timing/audio/compositing check. Browser and Pillow text layout/color can differ.
3. **External discovery:** TikTok UI changes, login challenges, network failures, rate limiting and downloader availability affect ingestion. A prior reported black screen did not stop the funnel; its actual cause was not established. Do not invent a resolved cause.
4. **Product metadata:** a previous screenshot of the pajama workspace displayed the earlier foundation product image, and inferred brand was “Women.” This is a recorded visual/metadata anomaly, not a diagnosed cache bug. Audit extraction, image assignment and caching before platform rollout.
5. **No full-reset UI:** the previous fresh run was created as a separate product workspace. Recreating the same URL normally derives the same product directory; it is not guaranteed to start a new run. Preserve the prior folder before any future reset implementation.
6. **Limits:** 32 tracks, 200 clips, ten minutes, 512 MB per imported file. Source handles use project frames. Maximum 200 keys per animated field. Timecode is non-drop; fractional FPS uses nominal labels.
7. **Audio ducking:** interval-based fixed attenuation of a marked bed beneath foreground audio; it does not analyze speech loudness. This is not a mastering suite.
8. **No multicam, nested sequences, speed ramps, tracking, masks, proxy workflow, LUTs, scopes, effects plugins or custom font management.** No layered image-document or standalone image-export editor.
9. **Local security model:** same-origin/loopback restrictions are not authentication. Current APIs must not be exposed directly to an untrusted network.
10. **Storage/concurrency:** filesystem revisions and in-process locks suit a local process, not distributed workers or collaborative editing. Edit identity is a hash of the timeline name; equal names within a product resolve to the same edit.
11. **Legacy workflows:** older dashboard AI iteration/generation and legacy scripts retain their earlier assumptions. Some rebuild paths replace active plan/review JSON and rely on archives; the stronger append-only guarantees belong to V4 timeline storage.
12. **Compatibility and reproducibility:** manifests use minimum versions, not lockfiles. Documentation says Python 3.10+, but `hashlib.file_digest` in asset hashing requires Python 3.11+. Use Python 3.11+ without code changes; the tested installation is Python 3.14.7. System fonts and FFmpeg builds affect output. There is no bundled production font.
13. **Rights vocabulary:** old product rights use statuses such as licensed/approved_for_ads; new import and export checks use owned/cleared. Keep original records and explicitly map statuses when migrating; do not silently treat unknown media as cleared.
14. **Export manifest:** includes revision/checksum, asset registry, used asset IDs, provenance and rights flag. It does not itself contain a complete explicit timeline field. Pair it with the immutable revision JSON when transferring an exact render recipe. Legacy provenance may include machine-specific absolute paths.

## 2. Technical architecture

### Languages and frameworks

Backend and CLI: Python. HTTP server: standard-library `ThreadingHTTPServer` and request handler, not Flask/FastAPI/Django. UI: vanilla JavaScript, HTML and CSS, not React/Vue. Native browser media elements, DOM compositing, Web Audio gain/pan and requestAnimationFrame provide interactive preview. FFmpeg subprocesses provide authoritative rendering and media decoding. Pillow rasterizes text overlays.

`nextplay.py` loads an existing product workspace, reads the original project environment configuration in place, and launches the dashboard. `main.py` provides the broader product pipeline CLI. The V4 UI is served at `/timeline?name=<edit-name>`. Dashboard HTML and its older JS are embedded in `dashboard.py`; V4 assets are separate static files.

### Request/state flow

1. UI loads the saved timeline through `POST /api/timeline/load`.
2. UI sends commands with its expected revision and `X-NextPlay: timeline`.
3. Backend applies commands to a copy, validates the complete result, commits a new immutable revision, and returns authoritative state.
4. UI replaces `P` with returned state and rebuilds the relevant controls/preview.
5. Render requests use a saved revision; FFmpeg writes a unique MP4 and manifest.

Frontend state is module-level variables in `timeline.js`: `P`, selection, active track, source asset, playhead, zoom, pending flag, undo/redo arrays, visualization cache, drag and playback state. It has no Redux-like store. The backend command engine owns persisted editing semantics. Do not move those semantics into a replacement UI without equivalence tests.

### Database and storage

There is **no database server**. JSON and local files store products, candidates, search plans, analyses, ad plans, reviews, rights and settings. `atomic_write_json` uses temporary files and atomic replace. V4 revisions have checksums, exclusive creation and fsync; `head.json` is a disposable index. Loading scans valid history and skips damaged records. Restore commits an older timeline as a new revision and retains subsequently imported assets.

Asset records include ID, relative path, kind, duration in project frames, audio presence, SHA-256 and provenance. Asset ID depends on relative path and content hash. Paths resolve within the product root. Relative V4 asset paths support moving an entire product directory, but older records often contain absolute paths and need an explicit audited rebase.

### Authentication and third parties

There is no application user authentication, OAuth account system or permission model. A persistent dedicated Chromium profile maintains TikTok website login for scraping. Never export this profile or reuse it as platform authentication.

OpenAI supplies transcription/analysis, creative review and timeline AI directions when configured. TikTok Shop/TikTok web pages are input sources, not an official posting integration. Browser discovery/download code and legacy SnapTik-related paths depend on external pages. Optional OpenCLIP/PyTorch supports visual similarity. Legacy LangGraph scripts are retained but are not the main V4 editor.

### Hosting/deployment

Local Python process with a loopback dashboard, normally `http://127.0.0.1:8766/`. No verified cloud deployment, Docker image, CI workflow, infrastructure configuration, production domain or hosted database is included. Mac launch scripts are convenience entry points. Ports and source data are external runtime choices.

## 3. Implemented editor capabilities

### Video and timeline

Independent video/audio/text/image tracks; add/reorder/select tracks, locks, visibility, mute and solo as applicable. Drag clips and edges; magnetic and non-magnetic placement; left/right trim and ripple trim; slip, slide and roll with source/neighbor constraints; split, compatible adjacent join, duplicate, lift, ripple delete, close gaps and markers. Linked detached audio follows supported clip edits and can be unlinked. Source relinking/replacement preserves timeline placement subject to available handles.

Source monitor with In/Out marking; ripple insert, overlap/place and overwrite; explicit frame controls; zoom/Fit, ruler scrubbing, playhead, one/ten-frame stepping, source filmstrip thumbnails and audio waveforms. Keyboard guide includes split via Cmd/Ctrl+B or S, duplicate, undo/redo, lift/ripple delete, frame steps, nudging, source I/O, markers, zoom/Fit and draft rendering. Inputs retain normal typing.

Constant speed 0.25–4×, reverse and freeze/unfreeze. Fit/fill/stretch, crop, scale, position, rotation, flips and opacity. Linear/hold keyframes for x/y, scale, rotation, opacity, volume and pan. Brightness, contrast and saturation are implemented; advanced grading is not.

### Images and overlays

Import PNG/JPEG/WebP and use image/logo tracks with duration, positioning, crop, transforms and opacity. These are timed overlays/compositing assets. There is no Photoshop-style layer document, painting, retouching, generative image tool or still-image export workflow.

### Audio

Original audio, dedicated audio tracks, detached/replacement audio, clip volume/pan/mute, track mute/solo, fades and interval ducking. Volume/pan keyframes and linked edits are supported. Reverse audio is available in rendered output. No multichannel mixing console, EQ/compressor UI, speech separation or automatic loudness mastering workflow.

### Text, captions and effects

Editable text/title/caption clips with timing, content, font size, color, background box and transforms. Import SRT/VTT as editable clips; export text tracks as SRT. Export includes titles as well as captions. Existing analyzed segment captions can migrate into text clips; the V4 UI does not independently generate fresh speech captions from arbitrary imported media in one click.

Cross dissolve at eligible adjacent cuts; fade through canvas background; alpha fades with stacked overlapping tracks. Background color and canvas dimensions are editable. No transition marketplace or effects library.

### Saving, export and AI

Every completed editing transaction autosaves a new revision. Pending/failed commands do not constitute saved edits; wait for the Saved indicator. Undo/redo stacks are session-local and restore revisions through new commits; persistent history supports later recovery. Stale tabs receive conflicts.

Draft preview adds output files but no permanent edit revision. Exports use H.264/AAC MP4; portrait 1080×1920, square 1080×1080, landscape 1920×1080 and custom canvas. FPS follows the project; drafts reduce resolution. Export MP4 and provenance JSON use unique names. No project interchange via XML/AAF/EDL, batch publishing or direct social upload.

AI directions target the current saved timeline, selection and playhead. Supported precise directions run locally; other requests use OpenAI Responses structured output returning command strings. Commands are validated before the UI commits the batch; ambiguous or invalid responses preserve the timeline. Model context strips asset paths but retains relevant project metadata. It is an editing assistant, not a complete content-management agent. Pipeline transcription, segment analysis, ad planning and quality review are separate functions.

All larger-platform features listed later are **proposed, not implemented**.

## 4. Source structure and data contracts

```text
source/
  nextplay.py                 V4 local launcher
  main.py                     Product pipeline CLI
  ffmpeg_bin.py               FFmpeg discovery/override/fallback
  splice_videos.py            Older pipeline rendering/text path
  requirements*.txt           Existing dependency manifests
  OPEN_NEXTPLAY_V4.command    Mac launcher
  README.md / INSTALL.md / RELEASE_NOTES.md / TEST_STATUS.md / VERSION
  pipeline_core/
    static/timeline.html      Main editor markup
    static/timeline.js        Main UI/state/keyboard/live preview
    static/timeline.css       Main editor layout/styles
    timeline.py               Schema 4, validation, atomic command semantics
    timeline_api.py           Editor routes, local/AI directions
    timeline_store.py         Assets/imports, migration, revisions, recovery/export
    timeline_render.py        FFmpeg graph and Pillow text rendering
    timeline_captions.py      SRT/VTT -> text clips; text -> SRT
    dashboard.py              Workflow UI/server/jobs/workspace selection
    dashboard_previews.py     Confined cached local source posters
    models.py / storage.py    Product/candidate models and JSON workspace
    pipeline.py               Ingest/discover/download/analyze orchestration
    product.py / search_plan.py / discovery.py / downloader.py
    ranking.py / visual.py / source_quality.py
    analysis_jobs.py / analyzer.py / ad_builder.py / ad_editor.py
    rights.py / review_state.py / creative_quality.py / studio_settings.py
  tests/                      Synthetic unit/HTTP/render/shortcut checks and fixtures
  tools/build_release.py      Original code-only release builder
  distutils/                  Compatibility shim for browser dependencies
  testdata/                   Local synthetic product HTML fixture
  graph_main.py / legacy_main.py / other root scripts
                              Retained historical utilities, not main editor
```

```text
<project-data-root>/products/<product>/   Not in source ZIP
  product.json / search_plan.json / videos.json
  rights.json / review.json / source_quality.json / studio_settings.json
  raw/ thumbnails/ discovery/ analysis/ clips/ ads/ debug/ quarantine/
  nextplay/
    dashboard-posters/                  Disposable local JPEG cache
    <24-character-hash-of-edit-name>/
      revision-00000001.json ...         Authoritative project/history
      head.json                         Disposable index
      imports/                          Unique copied user imports
      cache/                            Filmstrips and waveforms
      drafts/                           Unique draft MP4 + manifest JSON
      renders/                          Unique export MP4 + manifest JSON
```

Schema fields: schema/id/name/fps/revision/canvas/assets/tracks/clips/magnetic/snapping/markers/provenance. Clips hold id, track, asset, kind, start/duration, source handles, speed/reverse/freeze, properties, keyframes, linking and provenance. Track ordering controls compositing. Preserve integer-frame meanings, source hashes, IDs, checksums and unknown-rights status.

### API takeover reference

GET `/` dashboard; `/api/state` workflow state; `/timeline` editor; `/timeline-static/timeline.js|timeline.css`; `/file/<relative-path>` constrained media with byte ranges; `/api/source-poster?video_id=...` generated local poster.

POST `/api/timeline/{load,history,edit,restore,import,visualizations,captions,export-captions,render,direction}`. All editor routes require `X-NextPlay: timeline`; JSON actions use a timeline `name`. Edits/import/render/direction carry expected revision where checked. Import uses binary body with query name/revision/filename/rights. Conflict returns 409; invalid commands normally return 400.

Example contract, not a request to the running project:

```json
{"name":"Campaign draft","revision":7,"label":"Split hook","commands":[{"op":"split","id":"existing-clip-id","frame":60}]}
```

Dashboard API also includes workspace switch/product create, settings, login, reviews, rights, custom ads, editor actions and pipeline jobs. Consult `dashboard.py` for exact request schemas. Jobs/logs live in memory and are not a durable queue. The current workspace is global to the server process.

## 5. Dependencies and local startup

`requirements.txt`: beautifulsoup4, certifi, python-dotenv, requests, selenium, undetected-chromedriver. `requirements-editor.txt`: base plus OpenAI, Pillow and imageio-ffmpeg. `requirements-analysis.txt`: base plus OpenAI/Pillow. `requirements-ai.txt`: base plus OpenAI, open_clip_torch, Pillow, torch and langgraph for optional/legacy paths. Heavy optional packages are not required for basic V4 editing.

Observed installed runtime: Python 3.14.7; beautifulsoup4 4.15.0; certifi 2026.7.22; python-dotenv 1.2.4; requests 2.34.2; selenium 4.51.0; undetected-chromedriver 3.5.5; openai 3.28.0; Pillow 12.3.0; imageio-ffmpeg 0.6.0; FFmpeg 9.0.2. These are observations, not a complete portable dependency lock. Keep the original manifests; reproduce a tested environment before changing versions.

FFmpeg with H.264/AAC and required filters, installed Chromium/Chrome for scraping, and appropriate system fonts are external dependencies. Node is needed only for the shortcut regression check. No npm install or frontend bundler is required. No binary, font or browser profile is bundled.

```sh
cd nextplay-migration-2026-10-13/source
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-editor.txt
python nextplay.py --workspace "/absolute/path/to/project-data/products/PRODUCT_FOLDER" --port 8766
```

Use Python 3.11+; preserve a separate data root. If a port is occupied, use another `--port`; do not kill unrelated processes. An existing product directory containing product.json is required. For a synthetic demo, run `python tests/serve_fixture.py --root /tmp/nextplay-migration-qa` and open the printed port-8874 timeline URL.

The existing launcher reads `<product-root>/../../.env` when using standard products layout. A `.env` inside source is **not automatically loaded by nextplay.py**. Use the supplied `.env.example` as a template for a separate runtime config or pass variables from the launching environment. Local editing/rendering does not require OpenAI. Model defaults in code are historical configuration; confirm access/compatibility before paid API tests. FFMPEG_BINARY overrides executable selection; SCRAPER_FORCE_BITMAP_OVERLAYS affects the old text path.

## 6. Migration sequence for another developer/agent

1. Verify ZIP checksum and every manifest entry. Import `source/` into a new repository as an unchanged baseline commit, tagging V4.0.2 migration. Retain this ZIP for rollback. No repository was created by this handoff.
2. Back up the complete original product data separately, including raw footage, revisions/imports and export manifests. Browser profiles and credentials remain separate and private. A source-only archive cannot restore media-backed projects by itself.
3. Run the included tests in a separate environment. Open the synthetic fixture in Safari and the target browser; verify drag/trim, keyboard handling, overlays/audio, save/reload, conflicts, restore, draft and export. Do not start paid live tests by default.
4. For real-data acceptance, use a copied workspace. Initially preserve the directory layout and original mount paths if possible. Audit and rebase absolute references in product/candidate/analysis/legacy plans on the copy only. Reconcile by content hashes and IDs; do not guess a replacement source by filename alone. Verify rights records and historical checksums before accepting the copy.
5. Embed the existing editor as a served route or isolated same-origin module first. Keep schema 4, command API and filesystem storage behind an adapter. This avoids a frontend rewrite becoming a hidden timeline rewrite.
6. Introduce platform identity and authorization around workspace/asset/API access. Replace the single global workspace context with explicit tenant/project identifiers. Keep rendering and command semantics unchanged while routing access changes.
7. Move renders/analysis/discovery to durable jobs with immutable revision inputs, cancellation/progress, retries and isolated temporary paths. Keep publish jobs separate from render jobs.
8. Introduce metadata storage and object-store adapters. Preserve revisions as immutable documents and assets as content-addressed records. Map edit-name hashes to platform project IDs without renaming historical files prematurely. Preserve all imported/legacy provenance.
9. Add content/approval/publishing modules after equivalence acceptance. Compare old/new output frames and measured audio under a pinned FFmpeg/font environment; tolerate codec differences only with explicit acceptance thresholds. Cut over a copy first, retain rollback, then migrate originals with a verified inventory.

### Reuse directly

The command engine/validators, schema, captions parser/export, timeline UI and shortcuts, rendering graph/text rasterizer, media hashing, synthetic tests and provenance structures. Reuse filesystem store/legacy migration unchanged during initial embedding. Preserve old pipeline modules to avoid losing discovery/planning workflows.

### Refactor later, not in this handoff

HTTP framework and authentication; process-global workspace/job state; distributed revision locking; local-path/object-store boundary; upload streaming/quotas; durable render workers; UI modularization; media probing via FFmpeg stderr; font portability; unified legacy/V4 rendering; normalized rights; product metadata extraction; observability. Optional LangGraph/legacy utilities may become separate packages, but should not be silently deleted from the baseline.

### Non-negotiable acceptance checks

Every existing edit operation, source handles and timecode semantics; locked-track atomic rejection; linked-audio edits; replacement/relink; transparent text/image composition; volume/pan/fades/ducking/solo; reverse/freeze; draft/export exact revision; stale-save conflict; restore keeping newer assets; damaged-revision recovery; byte ranges and path safety; unchanged source files and rights. Include Safari manual interaction alongside backend tests. Archive a full product inventory before migration and compare hashes afterward.

## 7. Archive contents, configuration and exclusions

The handoff ZIP contains unchanged runtime source, original manifests/launch scripts/docs, tests including the preview regression, this document, a safe source `.env.example`, observed dependency versions, source inventory/comparison and SHA-256 manifests. The source is the development tree whose runtime files match the installation. Package scripts and tests are retained; this handoff packager adds the safe template explicitly because the original release builder excludes `.env*`.

Excluded: actual .env files, keys/tokens/passwords, browser profiles/cookies, products and downloaded media, rendered ads, imported assets, caches, virtual environments, node_modules, compiled/build files, attachments and the pasted terminal history. The transcript fixture is omitted because generated/legacy transcript content is unnecessary to the editor. Source media and project data are deliberately not downloadable in this code archive.

A safe template only enumerates supported variables with placeholders. It is not an active credential file. The pasted terminal history contains an exposed API credential; rotate it separately. Its value is neither repeated in this document nor exported. No credential rotation was performed as part of this read-only handoff.

No project LICENSE file was found in the inspected source. Clarify source ownership and redistribution terms, and preserve third-party license obligations, before shipping the platform. The code ZIP is a technical transfer, not a grant of rights to downloaded footage.

## 8. Proposed larger-platform integration — not implemented

Recommended separation: platform project/content records -> immutable edit revision -> render job -> rendered asset/manifest -> approval -> platform-specific publish job -> provider post ID/status -> analytics. The editor remains the creative asset module; discovery and analysis supply candidate media and metadata.

Suggested records: Workspace/Tenant, User/Role, Brand, Campaign, ContentItem, EditProject, EditRevision, MediaAsset, RenderJob, Approval, SocialAccount, PublishJob and MetricSnapshot. This is a proposed model, not current database schema. Track caption/description/hashtags and each channel variant independently from overlay text inside video frames.

- **AI content creation:** generate briefs, copy and channel variants around current assets. Tool-call the existing validated command engine for edits. Retain revision-bound suggestions and human approval. No generative image/video service is presently integrated.
- **Platform-specific posts:** reusable templates select aspect ratio/duration/overlay-safe layout; create derived revisions and render outputs, preserving a shared source project. Query provider limits rather than hard-coding assumptions.
- **TikTok/Instagram/Facebook:** add separate official API connectors with account consent, encrypted token storage, retries and provider status polling/webhooks. The present scraping browser login is not an API publishing credential. Provide export/manual handoff when automatic publishing is unsupported.
- **Copy/captions/hashtags:** proposed AI copy service writes draft metadata, provenance and channel variants; transcription service creates editable caption cues. Existing analysis and SRT tooling can be reused.
- **Music research:** keep recommendations, licensing/source records, permitted usage and availability separate from audio files. Current music support means using supplied/downloaded audio; no trending-music feed or blanket permission to use platform music exists.
- **Preview/approval:** aggregate rendered variants, metadata, rights evidence and revision checksums. Approval should refer to the exact rendered revision; edits invalidate or supersede approval. Current source/ad approve/reject flags are not a multiuser approval system.
- **Automated publishing:** schedule durable idempotent jobs only after required approval and account capabilities are established. Preserve provider post IDs and retry state to prevent duplicate posts.
- **Analytics:** collect authorized account/post metrics asynchronously, retain collection time and metric definitions, and link results to content variant/render revision. Current creative quality scores are not social performance analytics.
- **Email/advertising later:** separate permissioned connectors, audience/consent records, campaign IDs and spend approvals. Reuse creative assets and approvals; no email delivery, subscriber database or ad-buying integration is in this source.

Official integration starting points consulted for this handoff: [TikTok Direct Post](https://developers.tiktok.com/docs/en/content-posting-api-get-started?enter_method=left_navigation), [TikTok creator information](https://developers.tiktok.com/docs/en/content-posting-api-reference-query-creator-info?enter_method=left_navigation), [Meta Instagram content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/), [Meta Pages posts](https://developers.facebook.com/docs/pages-api/posts/). TikTok documents audit requirements and private visibility for unaudited Direct Post clients. Creator-info responses supply allowed privacy choices and duration limits. Meta connector details were not verified end to end here; verify current account eligibility, scopes, review requirements and supported post types before implementation. This proposal promises no platform approval or API availability.

## Receiving developer checklist

- Preserve source snapshot and ZIP SHA-256; create baseline commit in the receiving repository.
- Keep private data backup separate; confirm all workspace/media paths and original rights.
- Reproduce 101 Python tests and shortcut checks; manually accept Safari behavior.
- Address legacy caption failure and product metadata anomaly as explicit subsequent changes.
- Wrap existing editor first; move auth/storage/jobs behind adapters next; add publishing last.
- Never equate a successful render, AI quality score or unknown-rights source with an approved or licensed post.

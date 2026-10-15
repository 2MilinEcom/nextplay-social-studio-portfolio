# NextPlay Social Studio

NextPlay's original V4 editor is preserved byte for byte. The V5 social workspace wraps it with a responsive dashboard and a separate SQLite content and publishing store.

## Start

On this Mac, open `source/OPEN_NEXTPLAY_SOCIAL_STUDIO.command`. The app opens at http://127.0.0.1:8770. Its first run creates a virtual environment if needed. Python 3.11+ and FFmpeg are required; imageio-ffmpeg can supply FFmpeg when a system binary is unavailable.

For a terminal launch:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r source/requirements-platform.txt
cd source
../.venv/bin/python -m social_platform.server --port 8770
```

The application reuses `OPENAI_API_KEY` from its launching environment, or from an ignored `.env.local` in the `nextplay` directory. No key is included in the source package. The currently running local session uses the existing key authorized in this conversation. Finder-launched processes may not inherit that environment; set up local configuration securely if launching independently later.

## Workflow

1. Set a brand name, audience, voice, CTA, and timezone in Settings.
2. Upload one video, or create a draft and upload into it. Rights confirmation is optional for import but required for publishing approval.
3. Open the preserved editor. Click **Done → Prepare posts** when its Saved indicator is ready. NextPlay renders the exact saved timeline in portrait, generates missing channel captions, preserves existing written captions, and returns you to post review. Preparation never approves or publishes a post. Existing editor revision history is retained.
4. Write copy or generate/refine all three channel variants. AI sees the supplied brief, brand profile, and up to three sampled video frames; it does not hear the video or watch every frame.
5. Review the preview, caption, hashtags, hook, CTA, and publishing preferences. Save and approve each desired channel.
6. Schedule approved channels or choose Publish now. A running server processes due jobs. Missing connections produce a visible blocked job; they never count as published.
7. Download a ZIP containing the video, each caption, metadata, and the referenced timeline revision for manual platform finishing.

## Music

Music discovery performs a live web search using OpenAI and returns a dated report with source links. Trends, sound availability, commercial licenses, and the ability to attach native sounds are not guaranteed. Saved native sound suggestions can be selected on a post; they require an in-app finish and block automatic approval. Use licensed/owned audio in the original editor and render it for publishing from NextPlay. Source links support listening where the original source allows it; NextPlay does not host a searchable platform sound catalogue or native-audio preview library.

## Login-based publishing service

The recommended path for this personal workspace is the optional Upload-Post bridge. Sign in to Upload-Post, then use NextPlay Settings to securely link its API key and profile. NextPlay can create an empty profile during explicit setup if none exists. The Connect buttons then open Upload-Post's hosted social-account authorization page and return to NextPlay. No paid subscription is purchased by NextPlay.

The bridge uploads the approved local MP4 directly, so it does not require a manually hosted Instagram video URL. It checks account identity, expired connections, and TikTok publishing preferences; persists request IDs before uploading; supplies an idempotency key; and polls completion. TikTok inbox fallback is shown as requiring attention, never as a confirmed public post. Credentials stay encrypted on the server. These adapter checks are automated mocks; real account authorization and live posting still require the service account.

TikTok's current Direct Post guidelines reject upload tools limited to your own/team accounts. Consequently a custom personal-use developer app is not a promised route to public TikTok posting. An established publishing provider's connection is the intended route here, subject to its actual account eligibility and plan limits. See https://developers.tiktok.com/docs/en/content-sharing-guidelines and https://docs.upload-post.com/api/user-profiles/.

## Advanced direct account integrations

OAuth and publishing adapters are implemented for TikTok Direct Post, Instagram Reels with Instagram Login, and Facebook Page Reels. They have automated adapter tests but have not been authorized or verified against your real social accounts. Register developer applications, configure credentials and exact callback URLs, obtain the needed permissions/reviews, then connect accounts in Settings. Meta and TikTok can change requirements; confirm your app's API version and eligibility before activation.

Callback paths:

- `/oauth/callback/tiktok`
- `/oauth/callback/instagram`
- `/oauth/callback/facebook`

TikTok checks current creator privacy choices and duration limits at publish time. An approved client is needed for unrestricted public direct posting. Instagram requires an HTTPS video URL Meta can retrieve; the local file URL is insufficient. Make sure the hosted URL points to the exact approved render. Facebook connection currently expects exactly one authorized Page. Connections expire; automatic token refresh is not implemented, so reconnect when prompted.

Account tokens are encrypted at rest with a local Fernet key. Keep the complete private data directory backed up and private. The API only returns connection status, account name, and expiration. The UI never sees access tokens or the OpenAI key.

## Approval and publishing guarantees

Approval binds channel copy, video metadata/hash, music selection, privacy, interactions, and disclosures. Changes invalidate affected approvals and cancel queued jobs. Concurrency conflicts reject stale updates. Due jobs are claimed transactionally; a local video hash change blocks publishing. Accepted uploads remain Processing until provider status confirms publication. Interrupted or uncertain writes become Check outcome and require reconciliation; they are not blindly retried. Queued jobs survive restarts, but the server must be running to process them.

## iPhone and cloud hosting

The interface and editor wrapper were checked at 390 × 844. The hosted application is now available at https://nextplay-social-studio-production.up.railway.app. Choose your workspace password from the authenticated Settings page before signing in on your iPhone. The local loopback URL still refers only to the device opening it.

A Dockerfile and Compose configuration are supplied for deployment. The Python/FFmpeg backend cannot run in the Sites Cloudflare Worker runtime. It needs a container host, persistent volume at `/data`, HTTPS reverse proxy, and a registered public origin. Set `NEXTPLAY_ORIGIN` and `NEXTPLAY_PASSWORD` (16+ characters) before binding outside loopback. Network requests require the configured Host and Origin, with password sessions covering the original editor APIs as well as the new dashboard. Use a single process/replica with a durable volume; this release is a single-owner workspace, not a multiuser SaaS.

The first Railway Docker build and deployment succeeded. A separate NextPlay service, 500 MB volume, and HTTPS domain have been provisioned in DJ TUT. Social accounts are not yet connected. Email and advertising modules remain future work.

## Data and migration

The source archive excluded previous media and user projects. This application starts a new workspace under `runtime/products/`. To attach a separately backed-up original workspace without rewriting it, launch with `--workspace /absolute/path/to/product-folder`. The marketing database and encrypted connections remain in `runtime/marketing/` and `runtime/private/`. Workspace switching from the legacy UI is disabled in Social Studio to keep its media and publishing root consistent; the original standalone launcher is unchanged.

## Tests

```sh
cd source
../.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
../.venv/bin/python -m unittest discover -s social_platform/tests -p 'test_*.py'
node tests/test_timeline_shortcuts.js
```

See `VERIFICATION.md` for the reproduced test results and limits. Live AI checks are separate and use synthetic content; no social posting occurred during tests.

## Cloud deployment checkpoint — 14 October 2026

Source is saved in the private repository https://github.com/2MilinEcom/nextplay-social-studio. The Docker deployment entrypoint provisions a writable persistent `/data` directory and runs the application as an unprivileged user. Railway configuration is included for one replica and health checks.

A standalone new project was refused by Railway’s free-plan resource quota. With user authorization, a separate `nextplay-social-studio` service was successfully created inside DJ TUT, with a 500 MB `/data` volume and `nextplay-social-studio-production.up.railway.app` domain. Service-scoped origin, port, and private bootstrap authentication variables are set. Spotify remains on its original successful deployment; VibeProof was not modified. No paid plan was started.

Railway’s GitHub access now includes NextPlay while retaining VibeProof. The main branch is connected and deployment `8a3d8530-da11-4526-b3a5-057568ec9f32` succeeded. Live HTTPS, protected API access, one-second synthetic video upload, 1080-wide portrait rendering through Done, three real AI captions, and ranged media playback passed. Social sign-in URLs for TikTok, Instagram and Facebook were generated successfully. Upload-Post’s dedicated NextPlay credential is encrypted on the server, profile `default` is linked, and the existing authorized OpenAI key is connected. The owner still needs to choose a workspace password and authorize their three social accounts. No post was approved or sent. Documentation-only changes do not trigger builds; scheduled publishing runs with application sleeping disabled.

Cloud owner setup supports a 15-minute first-login invitation, single use per running process and disabled permanently after password selection. The owner chooses a 16+ character password in Settings; only a salted scrypt hash is stored encrypted on the volume. `bootstrap_cloud.py` transfers the already-authorized OpenAI key through authenticated HTTPS into encrypted server storage without printing it, then saves a private first-login link under ignored `runtime/`. The secure transfer completed against the live deployment. The hosted Settings page is open with a first-login session; the owner chooses their password themselves.

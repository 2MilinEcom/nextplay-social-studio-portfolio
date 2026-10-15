# Verification — NextPlay Social Studio

Verified 13 October 2026 (America/Los_Angeles). All source/runtime verification used a separate workspace or synthetic test media. No real social post was published.

- Original source inventory: all 71 listed files match their handoff SHA-256 hashes. The original editor source files are unchanged.
- Original Python suite: **101 passed**. Covers editor commands, media confinement, imports, revision recovery, rendering, and local HTTP behavior.
- Original JavaScript keyboard regression check: **passed**.
- New marketing suite: **36 passed**. Covers approval invalidation, channel independence, stale-save rejection, rights and native-music blocking, schedule timezones, persistent queues, atomic claiming/cancellation, duplicate prevention, uncertain-outcome recovery, encrypted credentials, OAuth state rejection, structured AI response validation, authentication and same-origin boundaries, API header checks, path confinement, binary upload, real FFmpeg render, byte ranges, post package download, and mocked publishing adapters.
- Live OpenAI copy: **passed**, generating three platform-specific variants from a synthetic brief with the authorized existing key.
- Live OpenAI music research: **passed**, returning a dated report with six cited web sources. This confirms the research service works; it does not verify licensing or trend popularity for every recommendation.
- Browser checks: desktop dashboard, draft creation, manual copy save, editor opening, import into an existing draft, rights guard, approval, and scheduling dialog checked. The preserved editor and dashboard were inspected at **390 × 844** for iPhone layout. A browser upload refresh issue was found and fixed.
- JavaScript syntax and Python compilation: **passed**.

## Limits

No real social accounts or registered developer apps were connected. OAuth completion, public TikTok eligibility, Instagram/Facebook publishing, and account metrics require real authorization and platform approvals; adapter tests are mocked. No native music API/catalogue or platform-native sound attachment is provided. No actual iPhone hardware/Safari certification was performed; viewport testing is not the same as device testing. The Railway container build and deployment passed in the follow-up checks. The protected Railway service is live; see the cloud verification below.

The original legacy rendering path can still skip text overlays. The preserved V4 timeline rendering tests pass; this does not resolve the separate legacy-path limitation from the handoff. Existing media-backed projects were not migrated because the handoff excludes their data.

## Follow-up verification — 14 October 2026

The marketing suite now has **50 passing tests** (151 Python tests including the previously reproduced 101 editor tests). Added coverage includes Done preparation with real rendering and mocked copy, preserving handwritten captions, idempotent preparation, stale editor rejection, retained editor assets, secure provider authorization URLs, placeholder/expired connections, setup rollback, destination-account mismatch, asynchronous uploads, request IDs/idempotency, confirmed provider publication, and handling of skipped channels and TikTok inbox fallback. Provider live authorization and posting remain untested pending sign-in. A separate service inside the existing DJ TUT project was provisioned after a standalone project hit quota. GitHub access was granted and the first deployment succeeded.

## Cloud preparation checks — 14 October 2026

The existing 50 platform tests passed again. Two additional cloud authentication tests passed: tampered/expired invitations; internal-host health checks; protected APIs; one-use owner sign-in; short-password rejection; encrypted password hashes; bootstrap revocation after permanent password setup; password changes requiring the current password. JavaScript syntax and Python compilation passed. The Spotify callback retains its original successful deployment. No social post was sent.

## Live cloud verification — 14 October 2026

Railway deployment 8a3d8530-da11-4526-b3a5-057568ec9f32 succeeded. HTTPS health and authenticated Settings page passed. Unauthenticated marketing API access returns 401. Authorized OpenAI key transfer to encrypted cloud storage passed; Upload-Post is configured for profile `default`. One-second synthetic sample upload and Done preparation passed in Linux: portrait render width 1080, three real AI caption variants, no approvals, zero publishing jobs, and HTTP 206 video playback. All three provider social sign-in links were generated successfully. The sample remains as `NextPlay setup sample` in Content studio. The owner has not yet selected a permanent password or authorized their social accounts. Spotify remains on its original successful deployment. No real social content was sent.

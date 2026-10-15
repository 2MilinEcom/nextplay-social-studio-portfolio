# NextPlay 4.0.2 verification

Verified on this Mac using Python 3.14, Pillow 12.3, FFmpeg 9.0.2 (without drawtext), Node and native Safari. Automated fixtures were generated locally. Additional acceptance checks used an isolated copy of real product media. No original product media or downloaded ads were changed or placed in the release.

## 4.0.2 preview fix verification

- Complete Python regression suite: **101 tests passed**. Four new tests cover local JPEG generation/cache reuse, source fingerprint changes, unsafe paths/cache symlinks, concurrent HTTP requests and unchanged source/provenance data.
- All **30 downloaded source posters** returned valid JPEGs on the installed dashboard. The eight Step 1 thumbnails loaded in the in-app browser and were verified visually. Dashboard JavaScript syntax passed.
- The installed app was patched and restarted on port 8766, retaining its existing installation folder. This preview check did not add a new native Safari playback acceptance result.

## Earlier automated results

- Final complete regression run: **95 Python tests passed**, including 35 legacy pipeline/editor tests, 49 timeline/model/render/store tests, nine real HTTP tests, and two package hygiene tests. After the export optimization, all 18 rendering/storage tests passed, including two new numerical opacity regressions: **97 distinct Python tests passed overall**.
- JavaScript syntax check and the actual keyboard handler's regression checks passed. The shortcut tests cover Mac/Windows modifier combinations, typing/media controls, editable elements, pending saves, modal dialogs, repetition, nudges, frame steps, zoom and drag cancellation.
- Live OpenAI checks passed with the authorized existing credential: generated narration transcription, video analysis, creative review and current-timeline AI edits. Credential values were not printed, copied or packaged; no real user footage was uploaded.
- Real-media acceptance passed: copied 30 existing videos, migrated the legacy ad (718 frames), edited a seven-clip multitrack sequence, rendered draft and 1080×1920 export, preserved unknown-rights status, restored/reopened revisions, and planned/rendered a fresh ad from existing analysis. All **166 original files** retained their original hashes.

The full run includes real synthetic FFmpeg renders, HTTP servers, revision restore and optimistic concurrency. Final render checks measure cross-dissolve pixel blending, music ducking and audio solo numerically. Added coverage includes all four trim variants, slip/slide/roll, linked edits and split/join, overwrite insertion, markers, FPS conversion, non-drop timecode/snapping, speed/reverse/freeze, animated transform/opacity/volume/pan, color/crop/fit/flips, text/image compositing, transparent text preservation, captions, thumbnail/waveform generation, mute/fades/pan, export presets, original-media preservation, bad-render cleanup, damaged-revision recovery and validated AI batches.

HTTP tests cover entry/static files, editing/restore/draft, import decoding and unique filenames, captions/directions, rejected stale-tab saves, atomic invalid batches, Host/Origin/editor-header checks, oversized requests, media ranges and unavailable/unsafe paths. Package tests use positive allowlists; the archive's file hashes and ZIP integrity are checked during construction.

## Native Safari checks actually performed

Using a separate disposable workspace and Safari window:

1. Dragged the middle clip to frame zero in magnetic mode; verified saved source bounds and position.
2. Dragged the left trim handle inward; verified the opposite edge stayed fixed and a leading gap appeared.
3. Reloaded and verified the saved edit persisted.
4. Entered playhead frame 30, split, and verified the selected source range shortened to 70–90 frames.
5. Undo restored 70–120 frames as a new revision; redo restored the split as another revision.
6. Detached audio; verified an independent linked audio clip appeared on A1.
7. Disabled magnetic mode and verified the saved setting.
8. Rendered a draft of revision 7; the saved revision did not change.
9. Played the resulting six-second MP4 through Safari's native media controls; picture and playhead advanced.

The remaining native UI paths were not exhaustively clicked. Automated model/render/HTTP coverage is broader than the native gesture coverage. Safari's active window changed during testing; actions were restricted to the separate QA window.

## Additional browser acceptance

A separate generated-media workspace was exercised through the in-app browser. Checks included both-edge trim/extension, slip, slide, roll, ripple trim, linked split/join, duplicate/lift/ripple delete, undo/redo, transitions, markers, locking/mute/solo/hide/ducking, detached and replacement audio, reverse/freeze/unfreeze, transforms/crop/color/fades/speed, all seven animation properties, file-chooser imports of video/audio/logo/captions, source insertion, relinking, text/image overlays and draft rendering. Imported captions exported to SRT. The new draft shortcut rendered the exact saved revision without creating an edit revision. Space toggled live playback from both the focused timeline and a focused clip. Clip focus survived the timeline redraw after selection. Square export was verified as 1080×1080 with the expected seven-second duration. A slow landscape render was deliberately cancelled; the saved revision remained unchanged, and its partial file was cleaned up before restarting the server. After optimizing opacity processing, the same landscape edit exported successfully at 1920×1080 with its expected seven-second duration. Further browser checks passed source I/O marking, overwrite and ripple insertion with undo, fractional FPS conversion with undo, independent track creation/removal/reordering, replacement audio/unlinking, and recovery as a new revision.

New split shortcuts were exercised with Cmd+B, Ctrl+B and S; duplicate/undo/redo/lift/ripple-delete, frame navigation, markers and nudges were also exercised. Caption/direction inputs retained normal typing. The Shortcuts guide was inspected visually. Source replacement and import-monitor refresh were verified from visible media URLs.

Native Safari checks above were completed in the earlier acceptance pass. Reconnection for the expanded acceptance and new shortcuts repeatedly failed with native automation timeouts. The in-app browser is Chromium; these additional checks must not be counted as native Safari coverage.

## Limits of this verification

Tests prove the implemented operations on controlled fixtures; they do not prove every combination on every codec, Safari version or real product footage. Native reverse playback and browser compositing are approximations; draft/export FFmpeg output is authoritative. Real-media editing/rendering/recovery and analysis-to-ad generation passed locally. The live public ELROEL product-page HTTP fetch passed. Fresh TikTok search/download and third-party downloader browser workflows were not exhaustively tested. Live AI used generated footage rather than user media. See RELEASE_NOTES.md for feature limits and INSTALL.md for non-destructive installation/recovery.

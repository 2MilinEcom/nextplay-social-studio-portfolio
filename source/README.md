# NextPlay 4.0.2 — multitrack short-form editor

A timeline editor integrated with the existing SCRAPER dashboard. This release starts from the available 3.7.3 code. The referenced 3.8 archive was unavailable locally. Original plans, downloaded media, rights records and rendered ads stay in their existing workspace.

Start with [INSTALL.md](INSTALL.md). See [RELEASE_NOTES.md](RELEASE_NOTES.md) for editing behavior and [TEST_STATUS.md](TEST_STATUS.md) for verification and limits.

The new editor saves each successful edit as an immutable revision. It renders from that exact timeline, including multiple video, audio, caption and image tracks. Failed commands preserve the prior state. Draft previews and exports create new files without replacing source media or previous renders.

The existing discovery, analysis, creative generation and review tools remain in the dashboard. Its Edit ad button opens the new timeline. New timeline AI directions operate on the current saved edit and preserve its media references and metadata.

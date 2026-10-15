# NextPlay 4.0.2 release notes

## 4.0.2 local dashboard previews

Downloaded source cards now use cached JPEG thumbnails generated from local media instead of remote TikTok thumbnail links. Original links and rights metadata remain intact. Preview requests validate registered sources and workspace boundaries; concurrent generation is bounded. Background polling only rebuilds changed galleries and defers refresh during playback.

## 4.0.1 acceptance fixes and shortcuts

Added an in-editor Shortcuts guide: Cmd/Ctrl+B or S splits, Space plays/pauses, Cmd/Ctrl+D duplicates, Cmd/Ctrl+Z undoes, Shift+Cmd/Ctrl+Z redoes, Delete lifts, Shift+Delete ripple-deletes, arrows step frames, Shift+arrows step ten frames, Option/Alt+arrows nudge clips, I/O mark sources, M adds markers, Cmd/Ctrl+0 fits, Cmd/Ctrl+plus/minus zooms, and Cmd/Ctrl+Enter renders a draft. Input fields preserve normal typing and native editing shortcuts. Selecting a timeline clip gives it keyboard focus.

Acceptance testing fixed live source replacement, source-monitor refresh after import, relinking newly inserted clips, inspector checkbox overflow, keyframe selector persistence, edit-readiness indicators, recovery history on failures, and zoom Fit bounds. Constant opacity avoids unnecessary per-pixel processing; animated opacity is evaluated before export-size scaling/rotation. Numerical pixel tests verify both blending paths.

## Audit findings and changes

The available 3.7.3 editor stored a sequential segment recipe with gap fields. Video/audio lanes represented that recipe rather than independent compositing tracks. It lacked editable overlays, keyframes, frame-rate choices, normal audio mixing, source insertion and the broader trim tools. Preview could continue showing an old render after changes. The new schema makes frame-based clips and independent tracks the source of truth for editing, live preview, AI commands, saved revisions and FFmpeg exports.

## Editing workflow

- Add independent video, audio, text/caption or image tracks. Track labels select the insertion destination. Reorder tracks for compositing order; lock, hide, mute, solo audio, or enable music ducking. Empty tracks can be removed.
- Select library media, preview it in the source monitor, mark frame In/Out or use the current source position, then ripple-insert, place with overlap, or overwrite at the playhead. Upload MP4/MOV/M4V/WebM, MP3/WAV/M4A/AAC/OGG, PNG/JPEG/WebP. Uploads are limited to 512 MB and probed before registration.
- Drag clips to move or reorder. Drag either edge to trim or extend within source handles. Choose move, ripple trim, slip, slide or roll tools. Use the precise frame-delta controls for repeatable adjustments. Magnetic mode ripples affected later tracks; non-magnetic placement preserves other clip positions. Locked tracks reject changes atomically.
- Split at the playhead, join matching adjacent source pieces, duplicate, lift to leave space, ripple-delete, close gaps, or add markers. Linked detached audio follows trims, moves, splits, joins, slip, speed, reverse and deletion. Unlink for independent edits. Replace audio or relink/replace a clip's source from the library.
- Choose project FPS including common fractional rates. Enter frame numbers or non-drop `HH:MM:SS:FF` timecode. Use snapping, zoom, Fit, scrub and frame nudges. Source thumbnails and waveforms help locate material.
- Set constant speed from 0.25× to 4×, reverse, or freeze a chosen source frame. Unfreeze restores the prior moving source handles and animation; undo can restore the complete earlier state. Speed changes update timeline duration. Set fit/fill/stretch, crop, scale, position, rotation, flips, opacity, brightness, contrast and saturation.
- Add linear or hold keyframes for position, scale, rotation, opacity, volume and pan. Keyframes use local clip frames; splitting rebases animation and joining checks continuity. Audio supports volume, pan, fades, clip/track mute, track solo, detached/replacement audio and interval ducking under foreground sound.
- Add editable text clips and image/logo overlays with timing, transforms, color, size and background boxes. Import SRT/VTT as editable text clips and export text tracks as SRT. Existing analyzed segment captions migrate into text clips. SRT export includes every text clip, including titles.
- Apply a cross dissolve at an adjacent cut or fade through the canvas background. Transition lengths are integer frames and bounded by clip duration. Canvas background and size are editable.
- Live program preview follows the current timeline. Render draft preview for the authoritative FFmpeg result; it adds no permanent edit revision. Export portrait, square, landscape or custom canvas H.264/AAC MP4 with a provenance manifest. Exporting never replaces earlier output.
- Every completed edit autosaves a new checksummed revision. Undo/redo and revision restoration preserve history. Stale browser tabs receive a conflict rather than overwriting another edit. Recovery retains newer imported assets.
- AI edit directions use the current saved timeline, selection and playhead. Precise supported directions run locally; creative directions use the existing configured OpenAI credential. The model returns validated atomic edit commands. Invalid responses, unavailable media or locked tracks leave the timeline unchanged.

## Practical limits

This is a focused short-form editor: maximum 32 tracks, 200 clips and ten minutes per timeline. It does not implement multicam, nested sequences, proxy workflows, effects plugins, motion tracking, masks, speed ramps, LUTs, advanced grading scopes, custom font management or drop-frame timecode. Source handles are quantized to project frames.

Live Safari preview approximates text layout, color and compositing with browser media elements; multiple decoder starts can drift. Reverse video is sought frame by frame, and reverse audio is muted in the live preview. Render a draft to verify exact audio, transitions, captions and timing; FFmpeg renders reverse audio. Ducking lowers the bed over foreground clip intervals, rather than analyzing speech loudness. Exports retain the chosen project's FPS and use the preset dimensions; changing canvas aspect ratio before framing gives the most predictable results.

The original dashboard's older AI generation/iteration buttons retain their earlier workflow. Use the new editor's direction box to edit the current multitrack timeline. Real product footage was copied into an isolated workspace for local editing/rendering/recovery/planning checks; all 166 original files retained their original hashes. Live transcription, analysis, creative review and timeline AI were tested with generated fixtures only. See TEST_STATUS.md for the remaining external-service and native Safari limits.

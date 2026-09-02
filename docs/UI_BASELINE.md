# VoiceStudio UI baseline

The approved visual baseline for the Phase 4 cover workspace is
`docs/ui-baseline/VoiceStudio_AI_Cover_UI_Preview.html` (copied from the
user-provided preview). It defines the dark studio palette, grouped sidebar,
song header, five-track timeline, synchronized lyrics plus quick mixer, target
voice/settings card, and bottom transport bar.

The Qt implementation keeps the existing import, UVR separation, profile
selection, RVC conversion, cancellation, and `handle_worker_event` contracts.
Unknown export/render backends remain UI-only until a backend API is available.

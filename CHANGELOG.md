# Changelog

## 1.3.0 - 2026-09-06

- Added an optional, locally served G-code toolpath viewer with 3D rotation, zoom, top view, layer selection, and travel visibility.
- Added authenticated Preview links for staged plain-text G-code, with browser-worker parsing, progress, cancellation, and bounded preview size/complexity.
- Added an opt-in installer prompt and `--with-gcode-viewer` / `--without-gcode-viewer` options. Existing configurations default to disabled.
- Added `upgrade-gcode-viewer.sh` to install the add-on over an existing installation, preserving credentials, configuration, staging, USB images, and service customizations, with backup and automatic rollback on failure.
- Added parser, web access, and upgrade regression checks, plus optional browser smoke checks.
- Kept release files at LF line endings for Linux scripts and portable checksum verification.

## 1.2.1 - 2026-09-05

- Increased contrast for downloadable links in the staged-file list.
- Staged file links now use light cyan on the dark table background, with a white hover and keyboard-focus state.
- This full installer includes every fix and feature from versions 1.0.1, 1.1.0, and 1.2.0.

## 1.2.0 - 2026-09-05

- Added a live upload monitor with an independent progress row for every selected file.
- Files upload sequentially so the Raspberry Pi and SD card handle only one incoming write at a time.
- Added per-file byte progress, percentage, average transfer rate, estimated time remaining, processing state, final stored path, and error reporting.
- Added overall batch progress and completed, failed, and skipped counts.
- Added a safe Stop after current file control that lets the active transfer finish before skipping queued files.
- Added a JSON upload endpoint with authentication, CSRF protection, capacity validation, FAT32 filename checks, and atomic per-file installation into staging.
- JavaScript-disabled browsers retain the original all-or-nothing batch upload form as a fallback.

## 1.1.0 - 2026-09-05

- Replaced the free-form upload destination field with a picker containing the USB drive root and every existing staged folder.
- Added creation of a new folder under any selected parent folder.
- Added a staged-folder table with descendant file counts and aggregate sizes.
- Added recursive folder deletion with confirmation and server-side protection against deleting the staging root.
- Empty folders are now retained when their last file is deleted, allowing them to remain first-class items on the published FAT32 image.
- Added an in-place upgrade package for existing 1.0.x installations. It preserves configuration, credentials, staging, and both USB image files.

## 1.0.1 - 2026-09-05

- Fixed browser uploads failing with `Errno 18: Invalid cross-device link`.
- The web service now exposes `/srv/piusb` as one writable systemd sandbox path instead of exposing `staging` and `incoming` as separate bind-mount boundaries.
- File ownership and normal Unix permissions still prevent the unprivileged web account from modifying the root-owned image directory.

## 1.0.0 - 2026-09-05

- Initial release.

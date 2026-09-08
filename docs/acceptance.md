# Acceptance checks

## Automated evidence

Run the Linux test suite from the root:

```bash
docker build -f manager/Dockerfile -t piusb-manager:local .
docker run --rm piusb-manager:local python -m pytest -q -p no:cacheprovider tests
```

Tests cover manifest/path rejection, FAT32/capacity limits, enrollment and
credential isolation, revocation, naming, collection and group snapshot
freezing, draft conflicts, CSRF, Range downloads, holds, lost authorization and
success acknowledgments, pause/cancel/retry, local ownership, scheduling windows,
retention, preparation/activation idempotence, interrupted operations, hash and
symlink rejection, and preservation of standalone publishing. USB operations
are mocked in publisher tests.

Use a **disposable** Compose project for real PostgreSQL/HTTP integration; the
simulation creates named test devices and deployment history:

```bash
docker run --rm -it --mount "type=bind,source=$PWD,target=/work" --workdir /work piusb-manager:local python -m manager.configure --output .test-manager.env --test
docker compose --env-file .test-manager.env -p piusb-manager-test up -d
docker compose --env-file .test-manager.env -p piusb-manager-test run --rm manager python -m tests.simulate
docker compose --env-file .test-manager.env -p piusb-manager-test down
```

In PowerShell replace `source=$PWD` with `source=${PWD}` if needed. The simulator
uses two actual agent instances with real HTTP downloads and PostgreSQL state.
Only hardware building and USB swapping are mocked. It checks an offline Pi,
catch-up, held builds, approval, restart without duplicate switching, and partial
fleet failure. HTTP is enabled only on the simulator's internal test configuration;
normal enrollment and agent execution require HTTPS.

Implementation-run evidence, 2026-09-08:

- **35 automated tests passed** in Linux Docker, including local-web regressions,
  serialized preparation, disk preflight, and stale prepared-image rejection.
- **Compose integration passed** with PostgreSQL 17 and two agent instances:
  offline catch-up, held builds, approval, restart idempotence, and partial failure.
- **Browser flow verified** in Chrome against the disposable manager: sign-in,
  inventory rendering, stable target selection, collection review, submission,
  and queued/history display. Fixed unnecessary refreshes that detached controls.
- JavaScript syntax and Bash syntax checks passed. PowerShell launcher parsing
  passed; the Compose workflow ran from PowerShell. Interactive launcher password
  prompting and a native Linux launcher startup were not separately exercised.
- **One physical Pi passed real prepare/activate/restore** on aarch64 Linux
  `6.18.39+rpt-rpi-v8`. An isolated 64 MiB image passed FAT32 formatting, copy,
  fsck, mounted file/empty-folder verification, and duplicate preparation.
  With the printer confirmed idle, a separate 256 MiB image retained all 12
  existing staged files plus an acceptance text file. Every file hash was checked
  from a read-only mount. Preparation left the active drive bound; activation
  bound the expected read-only image and recorded success. Duplicate activation
  remained idempotent. The original 16 GiB image B was restored with unchanged
  inode, size, modification time, and active-slot record. All existing publisher
  services and `piusbctl verify` passed afterward. Neither original image was
  rebuilt, and no permanent agent installation/enrollment was performed.

Still pending: a second physical Pi, printer-side file browsing, hardware agent
installation/enrollment against a permanent HTTPS manager, large-file/long-running
transfer and power-loss testing, and a backup/restore drill. The real hardware
test exercised the new privileged bridge directly; the outbound network agent
was exercised in Compose. These are distinct pieces of evidence.

## Required physical acceptance (not replaceable by simulation)

Run on **two real Pis**, each attached to a USB host, before production rollout.
Record OS/kernel, model, SD size/free space, agent version, deployment IDs, and
host behavior. Keep a backup of current staging first.

1. Verify existing read-only USB access and `sudo piusbctl verify` on both Pis.
2. Run the non-destructive agent installer. Confirm image hashes, staging and
   local credentials are retained and the host did not see a USB disconnect.
3. Enroll both Pis, verify serials, name/group them, and deploy a small file to
   only Pi A. Verify the host sees exactly that file on A and B is unchanged.
4. Deploy a folder/empty-folder collection to both. Check SHA-256 and exact
   contents on both USB hosts after successful switches.
5. Build-and-hold on both. Confirm the active slot and host contents do not
   change until approval; approve one and verify independent activation.
6. Test schedules and a short maintenance window; disconnect one Pi from the
   network and verify its old contents stay readable and it catches up later.
7. Cancel a download and a prepared deployment. Pause/resume a Pi. Verify the
   acknowledgment display and that no canceled prepared image is activated.
8. Induce a controlled build failure (for example, insufficient free space on a
   test card). Confirm the original active image remains available. Correct the
   cause and explicitly retry.
9. Restart the agent after a successful switch and while a download is partial.
   Confirm download resume and no second USB switch for an acknowledged job.
10. Request local takeover during a held build. Verify local editing remains
    blocked until takeover acknowledgment, then publish locally. Return control
    with an explicit reviewed replacement and verify exact manager contents.
11. Redeploy a pinned historical version, then perform a backup/restore drill
    against a separate manager instance with production Pis paused.

Do not infer USB-host compatibility, power-loss behavior, or multi-GB SD-card
performance solely from automated checks. Physical power-interruption testing
should use disposable data and include reboot during both build and switch.

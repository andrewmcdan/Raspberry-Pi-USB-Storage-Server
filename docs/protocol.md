# Manager protocol v1

All manager APIs use `/api/v1`. Administrator requests use the secure Flask session
cookie and `X-CSRF-Token` for mutations; the page supplies the token. Device
requests use `Authorization: Bearer <device credential>` and cannot access
administrator endpoints. Enrollment alone accepts a single-use 15-minute token.
The credential is returned once; only its SHA-256 digest is stored on the server.
Use HTTPS; no inbound Pi connectivity is needed.

## Administrator interfaces

| Method and path | Behavior |
| --- | --- |
| `GET /inventory` | Devices with telemetry and online state, groups, collections |
| `POST /enrollment-tokens` | Token, expiration, enrollment command |
| `PATCH /devices/{uuid}` | Name, location, tags, notes, window, paused; `revoke: true` revokes access |
| `POST /groups` | Create or update `{id?, name, devices: [uuid]}` |
| `POST /collections` | Create `{name}` |
| `GET, PUT /sets/{devices\|collections}/{uuid}` | Read or replace `{manifest, revision}`; optimistic revision rejects stale saves |
| `POST /content` | Stream raw file body; returns `{sha256, size}` |
| `GET /content/{sha256}` | Authenticated download with Range support |
| `POST /reviews` | Freeze target expansion and manifests; returns full diffs and snapshot ID |
| `POST /reviews/{uuid}/deploy` | Idempotently submit frozen review within 15 minutes |
| `GET /deployments` | Latest 500 deployment metadata rows, including batch IDs and retention state |
| `GET /deployments/{uuid}/manifest` | Historical contents, or `null` after retention expiry |
| `POST /deployments/{uuid}/{approve\|cancel\|retry\|pin}` | Transition/control action; retry creates a new ID |
| `GET /audit` | Latest 500 events (older events remain in database) |
| `POST /logout` | Clear administrator session |

Review input: `devices` and/or `groups`, optional `collection` or historical
`version`, `policy` (`auto`, `hold`, `scheduled`, `window`), `due` (UTC epoch
seconds), and `resume_local` (explicit takeover return). Without a source,
each target's own draft is used. File sets are complete replacements. The
review response includes `entries` with device IDs, names/serials, manifests,
hashes, totals, diffs, and whether current contents are known. Collection copies
use GET source plus revision-checked PUT destination.

A device window is `{timezone: "America/New_York", days: [0,1,2,3,4],
start: 120, end: 240}`. Weekdays are Monday=0; start/end are local minutes from
midnight. End earlier than start means overnight. Empty `{}` disables the window.

## Manifest format

```json
[
  {"path": "jobs", "kind": "dir"},
  {"path": "jobs/example.txt", "kind": "file", "size": 5,
   "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"},
  {"path": "empty-folder", "kind": "dir"}
]
```

Paths are canonical relative POSIX paths and must pass Windows/FAT validation.
Every parent directory is explicit. Reject traversal, symlinks, special files,
case/Unicode-normalized collisions, reserved DOS names, trailing dots/spaces,
oversized files, and aggregate content above the Pi's safe capacity. Root
preparation also checks actual files and their hashes. Manifest identity is
SHA-256 of compact, key-sorted JSON with entries sorted by path.

## Device interfaces

`POST /enroll` receives `{token, id, serial, hostname}` and returns
`{id, credential}`. UUID and serial conflicts return 409. Reenrollment requires
revocation of an existing credential. Token consumption and device creation are
one database transaction.

`POST /device/check-in` receives:

```json
{
  "id": "device-uuid",
  "serial": "hardware-serial",
  "telemetry": {
    "hostname": "workshop-pi", "ips": ["192.0.2.10"], "version": "2.0.0",
    "capacity": 3865470566, "free_bytes": 12000000000,
    "usb_state": "idle", "active_deployment": null,
    "prepared_deployment": null, "local": false, "paused": false
  },
  "report": {"id": "deployment-uuid", "state": "downloading",
             "progress": {"message": "jobs/example.txt", "bytes": 3, "total": 5},
             "error": ""}
}
```

`report` is omitted before any assignment. Responses contain `paused`,
`poll_seconds: 15`, and either `assignment: null` or
`{id, manifest, hash, state, cancel, activate, resume_local}`. The first eligible
queued assignment is claimed transactionally. Only one executes per device.

`GET /device/content/{sha256}` supports standard `Range`/`If-Range`/ETag behavior.
The digest must occur in a nonterminal deployment belonging to that device.
The agent resumes partial bytes, restarts if Range is ignored, and verifies the
completed file before preparation.

Reported lifecycle: `downloading → building → prepared → switching → succeeded`.
Failures become `failed`; cancellation becomes `canceled` before activation.
Terminal reports are idempotent. The manager durably marks `switching` before
returning activation authorization. Repeated `prepared` reports at this point
reissue that same authorization without regressing server state. A succeeded
report must identify the deployment as active in telemetry.

Durable agent and root journals reconcile a lost response or power interruption.
A root operation never accepts an arbitrary command or user-selected source/image
path. A valid root preparation is bound to an immutable deployment ID and image
slot. Activation checks that the prepared slot has not been superseded.

## Errors and limits

400: invalid path, manifest, capacity, policy, revision payload, or shape.
401/403: missing/invalid credentials or unauthorized content. 409: identity,
concurrent draft edit, expired review, or invalid transition conflict. 413: file
body too large. Clients retry transient network errors with backoff; domain
failures require an explicit operator action. Uploads are capped at FAT32's
4 GiB minus one byte. A manifest has at most 100,000 entries.

## Pi contents import

Admin `GET/POST /api/v1/devices/{id}/snapshots` lists or requests an `active`
or `staging` snapshot. `POST .../snapshots/{snapshot}/draft` requires the current
`revision` and copies a ready snapshot into the device draft. `POST
.../snapshots/{snapshot}/cancel` cancels pending capture/transfer.
Check-in returns an optional `snapshot: {id, source}` instead of issuing a
new deployment while an import is pending. Device-authenticated endpoints are:

- `POST /api/v1/device/snapshots/{id}/manifest`: immutable manifest or terminal
  error; returns missing hashes with sizes and resumable offsets.
- `PUT .../{id}/content/{sha256}?offset=N`: at most 8 MiB raw bytes; validates
  offset, size and final SHA-256. A lost acknowledgment is reconciled by querying
  the manifest again. An empty file uses one zero-byte PUT.
- `POST .../{id}/complete`: validates all blobs, marks ready, idempotent on retry.

Each endpoint restricts the device to its own import. Metadata is durable in
`pi_snapshots`; retention protects imported manifests. Privileged `export`
requests accept only a UUID request ID and fixed `active`/`staging` source.

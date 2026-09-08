# Central manager operations

The manager is an optional addition to Pi USB Publisher. Unenrolled Pis retain the
existing upload/publish workflow. The manager supports a single administrator and
an initial fleet of 1–20 Pis. It does not provide remote shells, reboot commands,
OS settings, or software self-updates.

## Start from the repository root

Install Docker with Linux containers and Compose. Choose a DNS name reachable
from your browser and all Pis over the LAN/VPN. Run one of these equivalent
launchers; the first run asks for a manager administrator password:

```powershell
.\start-manager.ps1 -PublicUrl https://piusb.example.internal
```

```bash
bash ./start-manager.sh https://piusb.example.internal
```

The launchers build the image, create `.env` if absent, and start Compose. They
never replace existing credentials. `.env` is ignored by Git; keep it private.
The database password is generated as URL-safe hexadecimal. The password hash
must remain single-quoted in `.env` so Compose does not interpolate its `$`
characters. To choose another host port set `MANAGER_PORT` in `.env`.

Services are `db` (PostgreSQL 17), `migrate` (initial schema creation), `manager`
(Flask/Gunicorn), and `scheduler` (retention). Named volumes `database` and
`content` retain state. The initial migration creates new tables; it does not
alter an existing Pi installation. Future schema changes must add explicit
migrations; `create_all` is not an upgrade migration engine.

```bash
docker compose ps
docker compose logs --tail=100 manager scheduler
```

## HTTPS on your LAN/VPN

The manager binds only to server loopback, `127.0.0.1:8881`. Put your existing
HTTPS reverse proxy in front of it. Configure the proxy to preserve the Host
header, support streaming uploads and Range responses, and allow requests up
to 4 GiB with long transfer timeouts. Example nginx location within an HTTPS
server configured with your certificate and private key:

```nginx
location / {
    proxy_pass http://127.0.0.1:8881;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    client_max_body_size 4g;
    proxy_request_buffering off;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

Set `PUBLIC_URL` to the exact HTTPS browser origin and recreate the containers
after changing it. Secure cookies are always enabled. Access the manager through
that HTTPS origin; HTTP on loopback is intended only for health checks and
isolated development tests. Avoid adding another login page in front of the
device API; device authentication uses its own bearer credentials.

Use a certificate trusted by the Pis. For a private CA, install its root in the
Pi's system trust store or pass `--ca-bundle /etc/piusb/manager-ca.pem` during
enrollment. Certificate verification cannot be disabled. The CA path must be
readable by `piusb` and outside protected home directories. Do not expose the
Pi's existing HTTP web app to the internet.

## Install and identify Pis

On each already installed Pi, copy this repository and run:

```bash
sudo bash ./install-agent.sh
```

The upgrade drains active publishing, stops the local web service briefly,
installs the agent and bridge, and restarts services. It does **not** invoke
`init-images`, reformat images, edit staged files, change local credentials,
or disconnect the gadget. It installs `python3-requests` from the OS repository.
The supplied systemd sandbox paths target the standard `/srv/piusb`,
`/var/lib/piusb`, and `/run/piusb` installation.

In the manager choose **Enroll a Pi** and run the displayed command on that Pi
within 15 minutes. Then run `sudo systemctl restart piusb-agent.service`.
Enrollment creates a persistent UUID in `/etc/piusb/device-id`, records the
hardware serial, and saves the credential in root-owned, group-readable
`/etc/piusb/agent.json`. Verify the displayed serial, then name the Pi, for
example **Workshop CNC**. Set its location, tags, notes, and optional maintenance
window. Names can change; UUIDs and hardware identity remain stable.

Do not clone an enrolled SD card onto another Pi. A mismatched serial is rejected
locally and by the manager. To reenroll the same device after losing credentials,
revoke the old credential in the manager, preserve `device-id`, and issue a new
enrollment token. If the enrollment response is lost, the device may already
exist in inventory; revoke it before retrying with a new token.

## Files and publishing

1. Select Pis in **Devices**, or select a named group. Groups can be saved from
   the current selection and edited to use a new selection.
2. In **Files & collections**, select a Pi draft or create a collection. Upload
   files, create folders, and delete unwanted entries. Uploads stream one file
   at a time with byte progress; **Stop after current file** preserves completed
   uploads. A folder deletion removes its descendants from the draft.
3. Copy a collection into a Pi draft to customize it, or select the collection
   directly as the source in **Deploy**. Copies are independent; there are no
   hidden live subscriptions.
4. Select a switch policy and review changes. Review displays target names and
   serials, full intended contents, and additions/replacements/deletions.
   Unknown or locally managed current contents are explicitly identified as
   a full replacement. Capacity failures block review.
5. Deploy the reviewed snapshot within 15 minutes. Submission is idempotent:
   repeating it does not add another deployment. Subsequent edits and group
   membership changes cannot alter this snapshot or its target devices.
6. Watch **History**. A fleet batch can partially succeed; each Pi executes
   independently. Click **Approve switch** for held builds, or **Retry** after
   correcting a failed deployment. **Redeploy version** opens a new review using
   retained historical contents. Pin versions needed for long-term recovery.

Each Pi has one executing deployment and a FIFO queue. Cancel queued items to
skip them. Failed deployments are terminal and do not block later queued work.
Retry creates a new deployment ID and does not reuse a failed prepared image.

Immediate deployment automatically downloads, verifies, builds, and switches.
Held deployment builds but waits for approval. Scheduled deployment builds
immediately and switches at or after its timestamp; an offline Pi catches up
when it reconnects. Maintenance-window deployment activates only in the next
eligible device window. Windows use IANA timezones and local weekdays,
including overnight windows and timezone/DST conversion. Times are stored as
UTC timestamps and displayed in the browser timezone.

Pause and cancellation are requests until the Pi acknowledges them. Downloads
stop at a transfer checkpoint; an active build is allowed to finish. Once the
manager records activation authorization, that switch finishes or recovers even
if a pause/cancel arrives immediately afterward. This also permits recovery
when the authorization response is lost. USB is intentionally disconnected;
the manager cannot know whether a host application has files open. Success
means the Pi verified the expected read-only backing image was bound, not that
the host application has reopened it.

## Local takeover and recovery

While enrolled, the local web app blocks file mutations and local publishing.
Choose **Request local takeover** there. The root bridge waits for an active
build/switch, copies the active managed contents into local staging, then grants
local control. Refresh the page to see acknowledgment. Pending central work stays
paused by local mode. To return, cancel any obsolete earlier queued deployments,
select the intended manager snapshot, enable **Return from local takeover** in
Deploy, and review the full replacement. The queue remains FIFO.

The Pi continues serving its last image if the manager or network is unavailable.
The agent polls at 15 seconds with jitter and backs off to roughly five minutes
on network failures. Inventory marks a Pi offline after 60 seconds. Download
partials resume with HTTP Range and are verified with SHA-256 before use.
Validation/build errors require explicit retry. Revoked or conflicting credentials
stop successful check-ins; inspect `journalctl -u piusb-agent.service`.

The root bridge accepts only prepare, activate, and takeover, with UUID identifiers
and fixed paths. It opens candidate components without following symlinks, copies
and verifies files into root-owned snapshots, and builds from those snapshots.
Durable per-deployment records prevent repeated switching after restarts or lost
acknowledgments. Interrupted builds fail closed. Interrupted activation is
reconciled against the actual bound slot; uncertain recovery requires explicit
retry. Existing publisher USB recovery remains in place.

Managed delivery needs more disk headroom than standalone publishing: downloads,
the old active source snapshot, the new verified snapshot, local staging, and
two image files can coexist. Preflight checks conservatively reserve a full
image allocation plus snapshot space. Candidate files hard-link verified cached
blobs, avoiding a second download copy. Active/prepared snapshots remain locally
available; obsolete candidates and cache entries are reclaimed for later jobs.

## Retention, backup, and restore

The worker runs every minute. It retains the latest ten content-bearing deployment
versions per Pi plus active, prepared, pending, and pinned versions. Metadata and
audit history remain. Unreferenced blobs and abandoned uploads have a 24-hour
grace period. Expired reviews release their snapshot references. Current drafts
and collections also protect their content from collection.

For a consistent backup, stop writers, dump the database inside its container,
then archive content. These commands work in PowerShell or Bash; create a `backup`
directory first with the appropriate shell command. Protect it as sensitive data.

```bash
docker compose stop manager scheduler
docker compose exec -T db pg_dump -U piusb -d piusb -Fc -f /tmp/piusb.dump
docker compose cp db:/tmp/piusb.dump ./backup/piusb.dump
docker compose run --name piusb-backup --no-deps --user 0 manager tar -czf /tmp/content.tar.gz -C /data/content .
docker cp piusb-backup:/tmp/content.tar.gz ./backup/content.tar.gz
docker rm piusb-backup
docker compose start manager scheduler
```

Also back up `.env` securely. On a replacement server, restore `.env` and use
the same Compose project name. Start only `db`, run `migrate` once to create the
content volume/schema, keep manager/scheduler stopped, then:

```bash
docker compose cp ./backup/piusb.dump db:/tmp/piusb.dump
docker compose exec -T db pg_restore -U piusb -d piusb --clean --if-exists /tmp/piusb.dump
docker compose run -d --no-deps --user 0 --name piusb-restore manager sleep 3600
docker cp ./backup/content.tar.gz piusb-restore:/tmp/content.tar.gz
docker exec piusb-restore tar -xzf /tmp/content.tar.gz -C /data/content
docker exec piusb-restore chown -R 10001:10001 /data/content
docker rm -f piusb-restore
docker compose up -d
```

Restore into new, empty volumes. Keep the same manager URL and credentials so
Pis reconnect. Avoid restoring an older database while active deployments are
running: pause the fleet first and reconcile device-reported active versions.
Never use `docker compose down -v` on production unless intentionally discarding
its database and all stored files.

## Repository additions

| Location | Purpose |
| --- | --- |
| `manager/` | Central API/UI, SQLAlchemy schema, retention worker, environment generator, Docker image |
| `manager/templates/` | Administrator sign-in and fleet/file/deployment interface |
| `manager/static/` | Browser interaction code and responsive styles |
| `opt/piusb/agent.py` | Unprivileged outbound agent and enrollment command |
| `opt/piusb/fleet.py`, `protocol.py` | Privileged operation bridge and shared manifest validation |
| `tests/` | API, protocol, agent, mocked publisher, and Compose simulation checks |
| `docs/` | Operations, protocol reference, and acceptance evidence |
| `compose.yaml`, `start-manager.*` | Root-level startup for Bash and PowerShell |
| `install-agent.sh` | Non-destructive Pi upgrade/enrollment prerequisite |
| `/srv/piusb/agent/` (Pi) | Agent state, resumable cache, candidate snapshots |
| `/var/lib/piusb/fleet/` (Pi) | Root-owned control state, deployment journal, verified source snapshots |

See [protocol.md](protocol.md) for API semantics and [acceptance.md](acceptance.md)
for reproducible checks and hardware release gates.

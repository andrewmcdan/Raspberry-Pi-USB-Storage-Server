# Pi USB Publisher

## Central fleet manager

This repository now includes an optional Docker-hosted manager and outbound Pi
agents. Name and group Pis, manage collections or per-Pi file drafts, review
exact deployment targets, and automatically download, build, and switch USB.
Schedules, approval holds, pause/cancel, history, retries, and version rollback
are available in the manager.

From the repository root, with Docker running:

```powershell
.\start-manager.ps1 -PublicUrl https://piusb.example.internal
```

```bash
bash ./start-manager.sh https://piusb.example.internal
```

The first run asks for an administrator password. Configure your HTTPS reverse
proxy, then upgrade each existing Pi with `sudo bash ./install-agent.sh` and
enroll it using the manager's copyable command. This upgrade preserves existing
images, staging, and local credentials. Do not rerun the original installer to
upgrade an existing Pi: it recreates the images.

Read [manager setup and operations](docs/manager.md),
[API protocol](docs/protocol.md), and [acceptance checks](docs/acceptance.md).
Physical two-Pi USB acceptance remains required before production rollout;
automated simulation does not establish hardware compatibility.

## Standalone Pi publisher

Automatic Wi-Fi recovery is included in new installations. To add it to an
existing Pi without changing USB contents or publisher services, run
`sudo bash ./install-wifi-watchdog.sh`. It monitors every 30 seconds and retries
saved Wi-Fi connections indefinitely. See [Wi-Fi recovery](docs/wifi-recovery.md).

Version 1.3.0, 2026-09-06

Pi USB Publisher turns a Raspberry Pi into a read-only USB mass-storage device whose contents are managed through a small web interface.

It uses two FAT32 image files:

- `storage-a.img`
- `storage-b.img`

Only one image is connected to the USB host at a time. The other image is rebuilt from `/srv/piusb/staging`. When publishing finishes, the USB gadget disconnects for a few seconds, switches to the newly built image, and reconnects.

```text
Browser upload
     |
     v
/srv/piusb/staging     canonical file set
     |
     v
Inactive FAT32 image    rebuilt and checked while active image stays online
     |
     v
USB disconnect          default 3 seconds
     |
     v
New image attached      A and B alternate on every successful publish
```

The connected host never receives write access to either image. That is what makes it safe for the Pi to rebuild the inactive image while the other image is being read over USB.

Version 1.2.1 includes the folder picker and recursive folder deletion, the per-file upload progress monitor, the cross-device upload fix, and higher-contrast staged-file download links.


## Updating an existing installation

Use the separate `piusb-upload-monitor-upgrade-1.2.0.tar.gz` package to update an existing 1.0.x or 1.1.0 installation without recreating the images, changing credentials, or modifying staged data:

```bash
tar -xzf piusb-upload-monitor-upgrade-1.2.0.tar.gz
cd piusb-upload-monitor-upgrade-1.2.0
sha256sum --check MANIFEST.sha256
sudo ./upgrade.sh
```

The upgrade includes the folder-management features from 1.1.0 and the upload monitor from 1.2.0. It stops and restarts only the web service. The USB gadget remains connected throughout the upgrade.

### Manual correction for an unpatched 1.0.0 installation

Version 1.0.0 can report `Errno 18: Invalid cross-device link` during an upload because the systemd sandbox exposes the incoming and staging directories as separate bind-mount boundaries. On an already installed Pi, replace this line in `/etc/systemd/system/piusb-web.service`:

```ini
ReadWritePaths=/srv/piusb/staging /srv/piusb/incoming /run/piusb
```

with:

```ini
ReadWritePaths=/srv/piusb /run/piusb
```

Then reload and restart the service:

```bash
sudo systemctl daemon-reload
sudo systemctl restart piusb-web.service
```

The parent path is writable inside the service sandbox, but `/srv/piusb/images` remains protected by its root ownership and normal Unix permissions.

## Recommended hardware

This guide assumes:

- Raspberry Pi Zero 2 W
- 32 GB or larger microSD card
- Raspberry Pi OS Lite 64-bit, based on Debian Trixie
- A real USB data cable
- Wi-Fi access for the web interface

On a Pi Zero, Zero W, or Zero 2 W, the USB data port is the micro-USB connector closest to the HDMI connector. The connector marked `PWR IN` has no data connection.

This walkthrough targets a Pi Zero 2 W and the bundle follows the Pi Zero family USB layout. Raspberry Pi 4 and Pi 5 use the onboard USB-C port for gadget mode, but their power requirements are higher. Compute Modules require carrier-board-specific USB device wiring. Pi 3 Model B and Pi 3 Model B+ are not suitable for this setup because they do not expose the required USB device-mode port.

## Important operating rules

1. The USB host sees the published drive as read-only.
2. Uploading files changes the staging folder only.
3. The host sees those changes only after a publish.
4. Close files opened from the USB drive before publishing.
5. Publishing intentionally disconnects the USB device for the configured delay, which defaults to 3 seconds.
6. Do not enable Raspberry Pi Imager's built-in USB Gadget Mode. That feature configures a USB Ethernet gadget, which conflicts with this custom mass-storage gadget.
7. Do not expose the web interface directly to the public internet. It uses normal HTTP and is intended for a trusted LAN.

## 1. Flash Raspberry Pi OS Lite

In Raspberry Pi Imager:

1. Select the correct Raspberry Pi model.
2. Select `Raspberry Pi OS Lite (64-bit)`.
3. Set the hostname, for example `piusb`.
4. Set a normal Linux username and password.
5. Configure Wi-Fi and the wireless country.
6. Enable SSH, preferably with public-key authentication.
7. Do not enable the Imager option named `USB Gadget Mode`.
8. Write and verify the microSD card.

Insert the card and initially power the Pi through `PWR IN`. Connect over Wi-Fi:

```bash
ssh your-linux-user@piusb.local
```

Update the installation:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Reconnect after the reboot.

## 2. Copy this bundle to the Pi

From a computer that has the downloaded archive:

```bash
scp piusb-double-buffer-1.3.0.tar.gz your-linux-user@piusb.local:~
```

On the Pi:

```bash
cd ~
tar -xzf piusb-double-buffer-1.3.0.tar.gz
cd piusb-double-buffer
sha256sum --check MANIFEST.sha256
```

Every listed file should report `OK` before installation.

## 3. Run the installer

```bash
sudo bash ./install.sh
```

The installer asks for:

- FAT32 image size in MiB, default 4096
- FAT volume label, default `PIUSB`
- Web username, default `admin`
- Web password
- Whether to install the optional G-code viewer, default **No**

For a 3D printer, select **Yes** at the viewer prompt, or select the add-on in advance:

```bash
sudo bash ./install.sh --with-gcode-viewer
```

Use `--without-gcode-viewer` to skip that prompt and install the standard web UI. Neither option skips the image-size or credential prompts. The viewer adds no system packages and uses no CDN or external service. Its files are copied only when selected; a standard installation does not load viewer code or expose viewer routes.

The volume label may contain 1 through 11 characters using `A-Z`, `0-9`, underscore, or hyphen.

Re-running the installer retains `/srv/piusb/staging`, but it recreates both image files and replaces the web credentials with the newly entered values.

For the default 4096 MiB images, use a 32 GB or larger SD card. In the worst case, the card holds staged data plus two populated image files, so total storage consumption can approach three times the staged data size.

Every publish formats the inactive image and copies the complete staged file set into it. For frequent publishing of large data sets, use a reputable high-endurance microSD card and maintain a backup of the staging folder.

When installation finishes, shut the Pi down instead of immediately moving cables:

```bash
sudo poweroff
```

Wait until the activity LED stops, then disconnect the `PWR IN` supply.

## 4. Connect the USB host

For a Pi Zero 2 W, connect the host computer to the micro-USB port closest to HDMI. This single connection normally supplies both USB data and power, and the Pi boots with peripheral mode enabled.

Do not leave an ordinary supply on `PWR IN` while also allowing the host cable to supply 5 V. A permanent externally powered installation should use a properly designed arrangement that isolates USB VBUS while retaining the data lines.

After boot, verify the services on the Pi:

```bash
sudo piusbctl verify
sudo piusbctl status
```

The first command should report `OK` for the UDC, gadget configuration, and gadget binding. The host should then detect a read-only drive named `PIUSB`.

## 5. Open the web interface

Open:

```text
http://piusb.local:8080
```

If `.local` name resolution does not work, find the address over SSH:

```bash
hostname -I
```

Then open:

```text
http://PI_IP_ADDRESS:8080
```

Sign in with the web credentials chosen during installation.

## 6. Normal workflow

### Upload

1. Select one or more files.
2. Choose an existing destination from the folder picker. Choose `/ (USB drive root)` to place the files at the top level.
3. Select `Upload`.

The browser uploads the selected files sequentially. The upload monitor shows one row per file with:

- Queued, uploading, processing, complete, failed, or skipped state
- Percentage and byte progress
- Average transfer rate and estimated time remaining while data is being sent
- The final staged path after a successful upload
- The server error for an unsuccessful upload

An overall progress bar shows the batch status. `Stop after current file` lets the active file finish safely and skips files that have not started. This avoids the ambiguous result that can occur if a browser aborts a request after the Raspberry Pi has already received the complete file.

Each completed file is committed to staging independently. Therefore, files that finish successfully remain staged even if a later file fails. The original all-or-nothing batch form remains available automatically when JavaScript is disabled.

The uploaded files now exist under:

```text
/srv/piusb/staging
```

They are not yet on the active USB image. Uploading another file to the same staged path replaces the staged copy. Browser-supplied names are normalized to conservative Windows/FAT-safe names. Select `Refresh staged file list` after the batch finishes to refresh the file table while retaining the completed upload results until then.

### Create and delete folders

The Folder management panel lists every staged folder, including empty folders.

To create a folder:

1. Choose its parent folder.
2. Enter the new folder name.
3. Select `Create folder`.

The new folder becomes the selected upload destination. Folder names are normalized to conservative FAT32-safe names, so a name such as `Pen Plotter Jobs` becomes `Pen_Plotter_Jobs`.

Use `Use for upload` beside any existing folder to select it in the upload panel.

`Delete folder` recursively removes the selected folder, all of its subfolders, and all files inside it from staging. The web interface displays the descendant file count and total size before deletion. The currently connected USB image does not change until the next publish. The USB drive root cannot be deleted.

Deleting the last file inside a folder no longer automatically deletes the empty folder. Use the explicit folder-delete action when the folder itself is no longer needed.

### Publish

1. Close files currently opened from the USB drive on the host.
2. Select `Build and switch USB image`.
3. The inactive image is recreated and populated from staging.
4. The image is checked with `fsck.vfat`.
5. The USB gadget disconnects briefly.
6. The new image becomes active and the gadget reconnects.

The host should rediscover the drive with the new file set. Image A and image B alternate after every successful publish. If building the inactive image fails, the currently active image stays connected.

### Preview 3D printer G-code (optional)

After installing the add-on, choose **Preview** beside a staged `.gcode`, `.gco`, `.g`, or `.nc` file (extensions are case-insensitive). After a new upload, use **Refresh staged file list** to see its Preview link.

- Drag the toolpath to rotate, scroll or use the zoom buttons to zoom, and use **Fit view**, **Top view**, or **3D view** to reset the view.
- Move the layer slider or use **Previous** / **Next**. Orange is the selected layer; cyan is earlier extrusion. Enable **Selected layer only** to isolate a layer or **Show travel** to include non-extruding moves.
- Keyboard users can focus the canvas and use arrow keys to rotate, `+` / `-` to zoom, and `0` to fit. The layer slider also supports arrow keys.
- Loading and parsing happen in a browser worker, with progress and cancellation. Everything stays between your browser and the Pi; an internet connection is not needed.

This is an approximate preview of the **staged copy**, which can differ from the active USB image. Previewing never modifies or publishes files and does not start the printer.

The parser supports `G0`/`G1`, XY-plane `G2`/`G3` arcs with `I`/`J` or `R`, `G90`/`G91`, `M82`/`M83`, `G92`, and millimeter/inch units. Extruder mode changes follow Marlin semantics: `G90` and `G91` clear the `M82`/`M83` override. Layers are inferred from extrusion heights, so travel Z-hops do not create layers. Files without extrusion can be viewed as travel paths.

The preview is not a firmware simulation: macros, bed leveling, home/tool offsets, and printer-specific motions are not simulated. Unsupported commands produce warnings or an error for unsupported motion modes. Binary/compressed G-code is not supported. Limits are 100 MiB, one million rendered segments, and 20,000 extrusion-height changes; unusually large, spiral, or non-planar files may require your slicer's viewer. These limits apply only to previews, not normal uploads, downloads, or publishing.

## Add the viewer to an existing installation

Copy and extract the **complete new release bundle** (or update your checkout) on the Pi, then run from that directory:

```bash
sha256sum --check MANIFEST.sha256
sudo bash ./upgrade-gcode-viewer.sh
```

Use this upgrade script instead of rerunning `install.sh`, which recreates both USB images. The upgrade:

- Updates the web app/templates, installs viewer assets, and sets `gcode_viewer = true` under `[web]` in `/etc/piusb/web.ini`.
- Preserves usernames, password hashes, session keys, other configuration settings, staged files, USB images, publisher state, and systemd service customizations such as the web port.
- Briefly stops and starts **only** `piusb-web.service` if it was running. Finish browser uploads before upgrading. The USB gadget and publish services keep running; no reboot is required. A stopped web service remains stopped.
- Saves replaced files to a private directory under `/var/backups/piusb/gcode-viewer-*` and restores them if installation or validation fails. The printed backup path contains a `files.txt` mapping for manual recovery.

Rerunning the upgrade is supported. Refresh the browser afterward. The add-on's release version is recorded at `/opt/piusb/web/gcode_viewer/VERSION`; an upgrade leaves `/opt/piusb/VERSION` unchanged because it does not replace the USB manager.

To disable an installed viewer, edit `/etc/piusb/web.ini` and set:

```ini
[web]
# Keep the existing username, password_hash, secret_key, and other settings.
gcode_viewer = false
```

Then run `sudo systemctl restart piusb-web.service`. The Preview links, viewer page, source endpoint, and viewer asset routes are disabled. Run the upgrade script again to re-enable and update it.

### First end-to-end test

1. Confirm the host can open `README.txt` from the read-only USB drive.
2. In the web interface, upload a small text file named `test.txt`.
3. Confirm the page says staging has unpublished changes.
4. Close any File Explorer or application window actively using a file on the drive.
5. Select `Build and switch USB image`.
6. Watch the active image change from A to B, or B to A.
7. Reopen the USB drive on the host and confirm `test.txt` is present.

## What the software installs

```text
/etc/piusb/piusb.ini                 Main settings
/etc/piusb/web.ini                   Web username, password hash, session key
/opt/piusb/manager.py                Image and USB gadget manager
/opt/piusb/VERSION                   Installed software version
/opt/piusb/web/app.py                Flask web application
/opt/piusb/web/templates/            Web templates
/opt/piusb/web/gcode_viewer/         Optional G-code viewer (when selected)
/srv/piusb/staging/                  Canonical file set to publish
/srv/piusb/incoming/                 Temporary upload transaction area
/srv/piusb/images/storage-a.img      Image A
/srv/piusb/images/storage-b.img      Image B
/var/lib/piusb/active                Active slot, A or B
/var/lib/piusb/status.json           Current and most recent publish status
/run/piusb/publish.request           Web-to-systemd publish request
```

System services:

```text
piusb-gadget.service
piusb-publish.path
piusb-publish.service
piusb-web.service
```

## Useful commands

Show status:

```bash
sudo piusbctl status
```

Run installation checks:

```bash
sudo piusbctl verify
```

Publish from the shell instead of the web page:

```bash
sudo piusbctl publish
```

Show logs:

```bash
sudo piusbctl logs
```

Or follow the publish log live:

```bash
sudo journalctl -fu piusb-publish.service
```

Restart the USB gadget:

```bash
sudo piusbctl restart-gadget
```

Restart the web interface:

```bash
sudo piusbctl restart-web
```

Check all services:

```bash
systemctl status piusb-gadget.service piusb-publish.path piusb-web.service
```

Check the USB Device Controller:

```bash
ls -l /sys/class/udc
```

Check which image is connected:

```bash
sudo cat /sys/kernel/config/usb_gadget/piusb/functions/mass_storage.usb0/lun.0/file
```

## Change the web password

```bash
sudo piusb-change-web-password
```

## Change image size or volume label

Changing either setting requires recreating both images. The staging folder is retained, but both image files are destroyed and rebuilt.

1. Close files on the host.
2. Stop the web interface, publish watcher, and gadget.
3. Edit the configuration.
4. Recreate the images.
5. Start all three services again.

```bash
sudo systemctl stop piusb-web.service piusb-publish.path piusb-gadget.service
sudo nano /etc/piusb/piusb.ini
sudo /usr/bin/python3 /opt/piusb/manager.py init-images
sudo systemctl start piusb-gadget.service piusb-publish.path piusb-web.service
```

Relevant settings:

```ini
image_size_mib = 4096
volume_label = PIUSB
staging_fill_percent = 90
disconnect_seconds = 3.0
```

## Change the web port

The default port is 8080. To change it:

```bash
sudo systemctl edit --full piusb-web.service
```

Change the final part of the `ExecStart` bind argument:

```text
--bind 0.0.0.0:8080
```

Then apply the change:

```bash
sudo systemctl daemon-reload
sudo systemctl restart piusb-web.service
```

## Failure behavior

### Failure while building the inactive image

The active image remains connected. The failed inactive image is discarded. Fix the reported filename, capacity, or filesystem problem and publish again.

### Power loss while building

The active image is not being modified, so it should remain usable. On the next boot, the active slot recorded in `/var/lib/piusb/active` is exported again.

### Power loss during the short switch

Both completed images remain valid. The active-slot record is updated atomically after the new backing image is selected and before USB is rebound. Depending on the exact instruction at which power was lost, the next boot exports either the prior completed image or the newly completed image. Publishing again produces a known current copy of staging.

## Troubleshooting

### The host does not detect a drive

Confirm the correct physical port and a data-capable cable. On Zero boards, use the connector closest to HDMI.

Run:

```bash
sudo piusbctl verify
sudo systemctl status piusb-gadget.service
ls -l /sys/class/udc
sudo journalctl -b -u piusb-gadget.service --no-pager
sudo journalctl -b -k | grep -Ei 'dwc2|gadget|mass.storage|usb'
```

Confirm `/boot/firmware/config.txt` contains:

```ini
dtoverlay=dwc2,dr_mode=peripheral
```

A reboot is required after changing this setting.

### The host still shows old contents

Wait for the drive to disappear and reconnect. Close and reopen File Explorer or the file manager. The publisher gives every newly built FAT filesystem a new volume serial number, which normally causes the host to refresh it. If the host is slow to notice, increase:

```ini
disconnect_seconds = 5.0
```

Then restart the gadget service.

### Publishing fails because of a filename

FAT32 is case-insensitive. Paths such as `Jobs/File.nc` and `jobs/file.nc` cannot coexist even though Linux permits both spellings.


The publisher rejects names that are unsafe or incompatible with FAT32 and Windows, including:

- `< > : " / \\ | ? *`
- names ending in a space or period
- reserved names such as `CON`, `NUL`, `COM1`, and `LPT1`
- symbolic links and special files
- individual files larger than the FAT32 limit

Rename or remove the reported item from `/srv/piusb/staging` and publish again.

### Publishing fails because the image is full

Delete staged files, increase `image_size_mib`, or cautiously raise `staging_fill_percent`. The default 90 percent limit leaves room for FAT metadata and cluster rounding.

### The drive is detected but a particular host cannot read it reliably

The default setting uses `stall = 0`, which avoids a known DWC2 mass-storage problem seen on some Raspberry Pi kernels. A host with unusual SCSI behavior may work better with:

```ini
stall = 1
```

After changing it, run:

```bash
sudo systemctl restart piusb-gadget.service
```

### The web page does not open

```bash
hostname -I
systemctl status piusb-web.service avahi-daemon.service
sudo journalctl -u piusb-web.service -n 100 --no-pager
ss -ltnp | grep 8080
```

Use the numeric IP address if `piusb.local` is not resolved by the client.

### Another gadget already owns the USB controller

The Raspberry Pi OS Trixie `rpi-usb-gadget` feature configures USB Ethernet. Turn it off, then reboot:

```bash
sudo rpi-usb-gadget off
sudo reboot
```

After running that command, verify that the Pi USB Publisher block still exists in `/boot/firmware/config.txt`. Re-running `sudo ./install.sh` restores it.

## Security notes

- The web application requires a username and password.
- The password is stored as a Werkzeug password hash, not plaintext.
- The application runs as an unprivileged `piusb` user.
- A systemd path unit converts a publish-request file into a root-managed publish operation. The web process is not permitted to run arbitrary root commands.
- Uploads are staged transactionally before replacing existing files.
- Folder deletion is authenticated, CSRF-protected, staging-locked, and cannot target the staging root.
- POST actions use a session-bound CSRF token.
- HTTP traffic is not encrypted. Use this only on a trusted local network unless you place it behind a properly configured HTTPS reverse proxy or private VPN.

## Prototype USB VID/PID warning

The included USB vendor and product IDs are intended for private experimentation only. Before selling or distributing a finished product, use USB identifiers you are authorized to use.

## Reference documentation

- Raspberry Pi OS documentation: https://www.raspberrypi.com/documentation/computers/os.html
- Raspberry Pi USB gadget mode overview: https://www.raspberrypi.com/news/usb-gadget-mode-in-raspberry-pi-os-ssh-over-usb/
- Linux USB gadget configfs documentation: https://docs.kernel.org/usb/gadget_configfs.html
- Linux mass-storage gadget documentation: https://docs.kernel.org/usb/mass-storage.html
- Flask file upload documentation: https://flask.palletsprojects.com/en/stable/patterns/fileuploads/
- Marlin arc commands: https://marlinfw.org/docs/gcode/G002-G003.html
- Marlin extruder positioning modes: https://marlinfw.org/docs/gcode/M082.html

## Development checks

On Linux (including WSL), with Python 3, Flask, and Node.js 18 or newer available:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/gcode-parser.test.js
bash -n install.sh
bash -n upgrade-gcode-viewer.sh
```

The Python tests use temporary directories and mocked service commands. They do not change a real Pi installation or USB images. Optional end-to-end UI checks use `python3 tests/browser_smoke.py` with Playwright and Chromium installed in a development environment; neither is required on the Pi.

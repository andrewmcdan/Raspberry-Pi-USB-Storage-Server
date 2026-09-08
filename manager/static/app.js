const csrf = document.querySelector("meta[name=csrf]").content;
const $ = (id) => document.getElementById(id),
  esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
let inventory = { devices: [], groups: [], collections: [] },
  selected = new Set(),
  draft = { manifest: [], revision: 0 },
  selectedSet = "",
  uploading = false,
  stopUpload = false;
function notice(error) {
  $("notice").textContent = error.message || error;
  $("notice").style.display = "block";
}
async function api(path, method = "GET", body) {
  const r = await fetch("/api/v1/" + path, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw Error(data.error || r.statusText);
  return data;
}
function action(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      notice(e);
    }
  };
}
function modal(html) {
  $("dialog-body").innerHTML = html;
  if (!$("dialog").open) $("dialog").showModal();
}
function tab(name) {
  for (const x of ["fleet", "files", "publish", "history", "audit"])
    $(x).classList.toggle("hidden", x !== name);
  if (name === "files") action(loadSet)();
  if (name === "history") action(loadHistory)();
  if (name === "audit") action(loadAudit)();
  $("target-summary").textContent =
    "Selected: " +
    inventory.devices
      .filter((d) => selected.has(d.id))
      .map((d) => d.name + " (" + d.serial + ")")
      .join(", ");
}
document
  .querySelectorAll("[data-tab]")
  .forEach((b) => (b.onclick = () => tab(b.dataset.tab)));
$("close-dialog").onclick = () => $("dialog").close();
function size(n) {
  return n == null
    ? "—"
    : n < 1024
      ? n + " B"
      : (n / 1024 ** 2).toFixed(1) + " MiB";
}
function when(t) {
  return t ? new Date(t * 1000).toLocaleString() : "Never";
}
function deviceName(id) {
  return inventory.devices.find((d) => d.id === id)?.name || id;
}
let deviceSignature = "",
  pickerSignature = "";
async function refresh() {
  inventory = await api("inventory");
  const next = JSON.stringify(inventory);
  if (next !== deviceSignature) {
    deviceSignature = next;
    renderDevices();
  }
  const pick = JSON.stringify([
    inventory.devices.map((d) => [d.id, d.name]),
    inventory.collections,
  ]);
  if (pick === pickerSignature) return;
  pickerSignature = pick;
  const old = $("set-picker").value;
  $("set-picker").innerHTML =
    inventory.devices
      .map((d) => `<option value="devices/${d.id}">Pi: ${esc(d.name)}</option>`)
      .join("") +
    inventory.collections
      .map(
        (c) =>
          `<option value="collections/${c.id}">Collection: ${esc(c.name)}</option>`,
      )
      .join("");
  if ([...$("set-picker").options].some((o) => o.value === old))
    $("set-picker").value = old;
  const source = $("source").value;
  $("source").innerHTML =
    '<option value="">Each selected Pi’s draft</option>' +
    inventory.collections
      .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`)
      .join("");
  $("source").value = source;
  $("apply-picker").innerHTML = inventory.collections
    .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`)
    .join("");
}
function renderDevices() {
  const q = $("search").value.toLowerCase();
  $("devices").innerHTML =
    inventory.devices
      .filter((d) =>
        JSON.stringify([d.name, d.location, d.serial, d.tags, d.id])
          .toLowerCase()
          .includes(q),
      )
      .map(
        (d) =>
          `<tr><td><input type="checkbox" data-select="${d.id}" ${selected.has(d.id) ? "checked" : ""}></td><td><button data-edit="${d.id}">${esc(d.name)}</button><br><small>${esc(d.location)} · ${esc(d.tags.join(", "))}</small><br><code>${esc(d.serial)}</code><br><small>${esc(d.telemetry.hostname || "")} · ${esc((d.telemetry.ips || []).join(", "))}</small></td><td><span class="badge ${d.online ? "online" : ""}">${d.revoked ? "Revoked" : d.online ? "Online" : "Offline"}</span><br><small>${when(d.last_seen)}</small></td><td>${esc(d.telemetry.usb_state || "Unknown")}<br><small>Active: ${esc(d.telemetry.active_deployment || "Local / unknown")}<br>Prepared: ${esc(d.telemetry.prepared_deployment || "None")}<br>Safe capacity: ${size(d.telemetry.capacity)} · Free: ${size(d.telemetry.free_bytes)}</small></td><td>${d.local ? '<span class="badge">Local takeover</span>' : ""}<button data-pause="${d.id}">${d.paused ? "Resume" : "Pause"}</button><br><small>Requested: ${d.paused ? "paused" : "running"}<br>Acknowledged: ${d.telemetry.paused ? "paused" : "running"}</small></td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="5">No matching Pis. Enroll your first device to get started.</td></tr>';
  $("groups").innerHTML = inventory.groups
    .map(
      (g) =>
        `<button data-group="${g.id}">${esc(g.name)} (${g.devices.length})</button><button data-edit-group="${g.id}" aria-label="Edit ${esc(g.name)}">Edit</button>`,
    )
    .join("");
  document.querySelectorAll("[data-select]").forEach(
    (x) =>
      (x.onchange = () => {
        x.checked
          ? selected.add(x.dataset.select)
          : selected.delete(x.dataset.select);
      }),
  );
  document.querySelectorAll("[data-group]").forEach(
    (x) =>
      (x.onclick = () => {
        for (const id of inventory.groups.find((g) => g.id === x.dataset.group)
          .devices)
          selected.add(id);
        renderDevices();
      }),
  );
  document.querySelectorAll("[data-edit-group]").forEach(
    (x) =>
      (x.onclick = action(async () => {
        const g = inventory.groups.find((g) => g.id === x.dataset.editGroup);
        const name = prompt(
          "Group name. Membership will become the currently selected Pis.",
          g.name,
        );
        if (name) {
          await api("groups", "POST", {
            id: g.id,
            name,
            devices: [...selected],
          });
          await refresh();
        }
      })),
  );
  document.querySelectorAll("[data-pause]").forEach(
    (x) =>
      (x.onclick = action(async () => {
        const d = inventory.devices.find((d) => d.id === x.dataset.pause);
        await api("devices/" + d.id, "PATCH", { paused: !d.paused });
        await refresh();
      })),
  );
  document
    .querySelectorAll("[data-edit]")
    .forEach((x) => (x.onclick = () => editDevice(x.dataset.edit)));
}
$("search").oninput = renderDevices;
function editDevice(id) {
  const d = inventory.devices.find((d) => d.id === id);
  modal(
    `<h2>${esc(d.name)}</h2><p><code>${esc(d.id)}</code><br>Hardware serial: ${esc(d.serial)}<br>Agent: ${esc(d.telemetry.version || "unknown")}</p><label>Name<input id="d-name" value="${esc(d.name)}"></label><label>Location<input id="d-location" value="${esc(d.location)}"></label><label>Tags (comma separated)<input id="d-tags" value="${esc(d.tags.join(", "))}"></label><label>Notes<textarea id="d-notes">${esc(d.notes)}</textarea></label><h3>Recurring maintenance window</h3><label>Timezone<input id="d-zone" value="${esc(d.window.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone)}"></label><label>Weekdays (0=Monday, 6=Sunday; blank disables window)<input id="d-days" value="${esc((d.window.days || []).join(","))}"></label><div class="row"><label>Start<input id="d-start" type="time" value="${minuteTime(d.window.start ?? 120)}"></label><label>End<input id="d-end" type="time" value="${minuteTime(d.window.end ?? 240)}"></label></div><button id="save-device" class="primary">Save</button> <button id="revoke-device" class="danger">Revoke device credential</button>`,
  );
  $("save-device").onclick = action(async () => {
    const days = $("d-days").value.trim();
    await api("devices/" + id, "PATCH", {
      name: $("d-name").value,
      location: $("d-location").value,
      tags: $("d-tags")
        .value.split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      notes: $("d-notes").value,
      window: days
        ? {
            timezone: $("d-zone").value,
            days: days.split(",").map(Number),
            start: timeMinute($("d-start").value),
            end: timeMinute($("d-end").value),
          }
        : {},
    });
    $("dialog").close();
    await refresh();
  });
  $("revoke-device").onclick = action(async () => {
    if (
      confirm(
        "Revoke this Pi’s manager access? Its USB contents remain available.",
      )
    ) {
      await api("devices/" + id, "PATCH", { revoke: true });
      $("dialog").close();
      await refresh();
    }
  });
}
function minuteTime(n) {
  return (
    String(Math.floor(n / 60)).padStart(2, "0") +
    ":" +
    String(n % 60).padStart(2, "0")
  );
}
function timeMinute(s) {
  const [h, m] = s.split(":").map(Number);
  return h * 60 + m;
}
$("enroll").onclick = action(async () => {
  const r = await api("enrollment-tokens", "POST", {});
  modal(
    `<h2>Enroll a Pi</h2><p>First run <code>sudo bash ./install-agent.sh</code> from this repository on the Pi. Then run this command within 15 minutes. The token works once.</p><pre>${esc(r.command)}</pre><p>After enrollment, verify its serial and give it a recognizable name before deploying.</p>`,
  );
});
$("group").onclick = action(async () => {
  const name = prompt("Name for the group of selected Pis");
  if (name) {
    await api("groups", "POST", { name, devices: [...selected] });
    await refresh();
  }
});
$("new-collection").onclick = action(async () => {
  const name = prompt("Collection name");
  if (name) {
    const c = await api("collections", "POST", { name });
    await refresh();
    $("set-picker").value = "collections/" + c.id;
    await loadSet();
  }
});
$("set-picker").onchange = action(loadSet);
async function loadSet() {
  if (uploading) return;
  selectedSet = $("set-picker").value;
  if (!selectedSet) return;
  draft = await api("sets/" + selectedSet);
  renderFiles();
}
function renderFiles() {
  const folder = $("folder").value;
  $("folder").innerHTML =
    '<option value="">USB root</option>' +
    draft.manifest
      .filter((x) => x.kind === "dir")
      .map((x) => `<option value="${esc(x.path)}">${esc(x.path)}</option>`)
      .join("");
  $("folder").value = folder;
  $("file-list").innerHTML = draft.manifest
    .map(
      (x, i) =>
        `<tr><td>${x.kind === "dir" ? "📁 " : ""}${esc(x.path)}</td><td>${x.kind === "dir" ? "Folder" : size(x.size)}</td><td>${x.kind === "file" ? `<a href="/api/v1/content/${x.sha256}" download="${esc(x.path.split("/").pop())}">Download</a> ` : ""}<button data-delete="${i}">Delete</button></td></tr>`,
    )
    .join("");
  document.querySelectorAll("[data-delete]").forEach(
    (b) =>
      (b.onclick = action(async () => {
        if (uploading) throw Error("Wait for the upload to finish");
        const x = draft.manifest[+b.dataset.delete];
        if (
          confirm("Delete " + x.path + " and any descendants from this draft?")
        )
          await saveDraft(
            draft.manifest.filter(
              (y) => y.path !== x.path && !y.path.startsWith(x.path + "/"),
            ),
          );
      })),
  );
}
async function saveDraft(manifest) {
  const r = await api("sets/" + selectedSet, "PUT", {
    revision: draft.revision,
    manifest,
  });
  draft = { manifest, revision: r.revision };
  renderFiles();
}
$("new-folder").onclick = action(async () => {
  if (uploading) return;
  const name = prompt("New folder name");
  if (name)
    await saveDraft([
      ...draft.manifest,
      {
        kind: "dir",
        path: ($("folder").value ? $("folder").value + "/" : "") + name,
      },
    ]);
});
$("apply-collection").onclick = action(async () => {
  if (uploading) return;
  if (!$("apply-picker").value) return;
  if (
    confirm("Replace this draft with an independent copy of the collection?")
  ) {
    const c = await api("sets/collections/" + $("apply-picker").value);
    await saveDraft(c.manifest);
  }
});
$("stop-upload").onclick = () => {
  stopUpload = true;
};
$("uploads").onchange = action(async () => {
  if (!selectedSet) return;
  uploading = true;
  stopUpload = false;
  $("set-picker").disabled = true;
  const folder = $("folder").value;
  try {
    for (const file of $("uploads").files) {
      if (stopUpload) break;
      const path = (folder ? folder + "/" : "") + file.name;
      $("upload-progress").textContent = "Uploading " + path;
      const record = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/v1/content");
        xhr.setRequestHeader("X-CSRF-Token", csrf);
        xhr.upload.onprogress = (e) => {
          $("upload-progress").textContent =
            `${path}: ${size(e.loaded)} / ${size(file.size)} (${Math.round((e.loaded / file.size) * 100) || 0}%)`;
        };
        xhr.onload = () => {
          try {
            const r = JSON.parse(xhr.responseText);
            xhr.status < 300
              ? resolve(r)
              : reject(Error(r.error || "Upload failed"));
          } catch (e) {
            reject(e);
          }
        };
        xhr.onerror = () => reject(Error("Upload connection failed"));
        xhr.send(file);
      });
      await saveDraft([
        ...draft.manifest.filter((x) => x.path !== path),
        { kind: "file", path, ...record },
      ]);
      $("upload-progress").textContent = "Saved " + path;
    }
  } finally {
    uploading = false;
    $("set-picker").disabled = false;
    $("uploads").value = "";
  }
});
$("review").onclick = action(async () => {
  const due = $("due").value ? new Date($("due").value).getTime() / 1000 : 0;
  const r = await api("reviews", "POST", {
    devices: [...selected],
    collection: $("source").value || undefined,
    policy: $("policy").value,
    due,
    resume_local: $("resume-local").checked,
  });
  showReview(r);
});
function showReview(r) {
  modal(
    `<h2>Review deployment</h2><p>Policy: ${esc(r.policy)} ${r.due ? "· " + when(r.due) : ""}. These targets and contents are frozen.</p>` +
      r.entries
        .map(
          (e) =>
            `<h3>${esc(e.name)} · ${esc(e.serial)}</h3><p>${size(e.total)}${!e.current_contents_known ? " · Current USB contents are local or unknown; this replaces the entire file set." : ""}</p><details open><summary>Changes</summary><pre>${esc(JSON.stringify(e.diff, null, 2))}</pre></details><details><summary>Complete intended contents</summary><pre>${esc(e.manifest.map((x) => x.path).join("\n"))}</pre></details>`,
        )
        .join("") +
      '<button id="commit-deployment" class="primary">Deploy this snapshot to these Pis</button>',
  );
  $("commit-deployment").onclick = action(async () => {
    await api("reviews/" + r.id + "/deploy", "POST", {});
    $("dialog").close();
    tab("history");
  });
}
let historySignature = "";
async function loadHistory() {
  const r = await api("deployments");
  const signature = JSON.stringify([r, inventory.devices.map((d) => [d.id, d.name])]);
  if (signature === historySignature) return;
  historySignature = signature;
  const batches = {};
  for (const d of r.deployments) {
    (batches[d.batch] ??= []).push(d);
  }
  $("batch-summary").innerHTML = Object.entries(batches)
    .slice(0, 5)
    .map(
      ([id, rows]) =>
        `<p><code>${esc(id.slice(0, 8))}</code>: ${rows.filter((x) => x.state === "succeeded").length}/${rows.length} succeeded · ${rows.filter((x) => x.state === "failed").length} failed · ${rows.filter((x) => !["succeeded", "failed", "canceled"].includes(x.state)).length} pending</p>`,
    )
    .join("");
  $("deployments").innerHTML = r.deployments
    .map(
      (d) =>
        `<tr><td>${esc(deviceName(d.device_id))}<br><code>${esc(d.id)}</code><br><small>Batch ${esc(d.batch.slice(0, 8))}</small></td><td>${when(d.created)}<br>${esc(d.policy)} ${d.due ? when(d.due) : ""}</td><td><span class="badge ${d.state === "failed" ? "error" : ""}">${esc(d.state)}</span>${d.canceled ? "<br>Cancellation requested" : ""}</td><td>${esc(d.progress.message || "")}${d.progress.bytes != null ? "<br>" + size(d.progress.bytes) : ""}<br>${esc(d.error)}</td><td class="actions">${!["succeeded", "failed", "canceled"].includes(d.state) ? `<button data-job="${d.id}" data-action="approve">Approve switch</button><button data-job="${d.id}" data-action="cancel">Cancel</button>` : ""}${["failed", "canceled"].includes(d.state) && d.retained ? `<button data-job="${d.id}" data-action="retry">Retry</button>` : ""}${d.retained ? `<button data-job="${d.id}" data-action="pin">${d.pinned ? "Unpin" : "Pin"}</button><button data-rollback="${d.id}" data-device="${d.device_id}">Redeploy version</button>` : "Content expired"}</td></tr>`,
    )
    .join("");
  document.querySelectorAll("[data-job]").forEach(
    (b) =>
      (b.onclick = action(async () => {
        await api(
          "deployments/" + b.dataset.job + "/" + b.dataset.action,
          "POST",
          {},
        );
        await loadHistory();
      })),
  );
  document.querySelectorAll("[data-rollback]").forEach(
    (b) =>
      (b.onclick = action(async () => {
        const r = await api("reviews", "POST", {
          devices: [b.dataset.device],
          version: b.dataset.rollback,
          policy: "auto",
          resume_local: $("resume-local").checked,
        });
        showReview(r);
      })),
  );
}
async function loadAudit() {
  const r = await api("audit");
  $("audit-list").innerHTML = r.events
    .map(
      (x) =>
        `<p><small>${when(x.at)} · ${esc(x.actor)}</small><br><strong>${esc(x.action)}</strong><br><code>${esc(JSON.stringify(x.detail))}</code></p>`,
    )
    .join("");
}
$("logout").onclick = action(async () => {
  await api("logout", "POST", {});
  location.reload();
});
action(refresh)();
setInterval(
  action(async () => {
    if (document.hidden) return;
    await refresh();
    if (!$("history").classList.contains("hidden")) await loadHistory();
  }),
  5000,
);

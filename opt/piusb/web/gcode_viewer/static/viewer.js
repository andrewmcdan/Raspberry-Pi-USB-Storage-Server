"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const root = byId("viewer"), canvas = byId("toolpath"), context = canvas.getContext("2d");
  const status = byId("status"), progress = byId("progress"), controls = byId("controls");
  const layerInput = byId("layer"), travel = byId("travel"), single = byId("single");
  let worker, model, yaw = -0.65, pitch = 0.8, zoom = 1, frame = 0, drag;
  let width = 0, height = 0;

  function stopLoading(message) {
    if (worker) worker.terminate();
    worker = null;
    status.textContent = message;
    progress.hidden = true;
    byId("cancel").hidden = true;
  }

  function schedule() {
    if (!frame) frame = requestAnimationFrame(draw);
  }

  function draw() {
    frame = 0;
    if (!context) return;
    const box = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    width = box.width; height = box.height;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    if (!model) return;
    const bounds = model.bounds;
    const center = [0, 1, 2].map(i => (bounds[i] + bounds[i + 3]) / 2);
    const diameter = Math.max(1, Math.hypot(...[0, 1, 2].map(i => bounds[i + 3] - bounds[i])));
    const scale = Math.min(width, height) * 0.84 / diameter * zoom;
    const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
    const project = (x, y, z) => {
      x -= center[0]; y -= center[1]; z -= center[2];
      return [width / 2 + (x * cy - y * sy) * scale,
        height / 2 - ((x * sy + y * cy) * sp + z * cp) * scale];
    };
    const selected = Number(layerInput.value) - 1;
    const layer = model.layers[selected];
    const start = single.checked ? layer.start : 0, end = layer.end;
    const coords = model.positions;
    // Group strokes by type; cap each canvas path to keep large previews usable.
    for (const kind of [0, 1, 2]) {
      if (kind === 0 && !travel.checked) continue;
      context.strokeStyle = ["#64748b", "#65c9ed", "#ffb454"][kind];
      context.lineWidth = kind === 0 ? 0.6 : 1.15;
      context.beginPath();
      let batch = 0;
      for (let i = start; i < end; i++) {
        const segmentKind = model.extrusions[i] ? (i >= layer.start ? 2 : 1) : 0;
        if (segmentKind !== kind) continue;
        const offset = i * 6;
        const a = project(coords[offset], coords[offset + 1], coords[offset + 2]);
        const b = project(coords[offset + 3], coords[offset + 4], coords[offset + 5]);
        context.moveTo(a[0], a[1]); context.lineTo(b[0], b[1]);
        if (++batch === 4096) { context.stroke(); context.beginPath(); batch = 0; }
      }
      context.stroke();
    }
    // Fixed orientation axes, independent of the object's dimensions.
    const origin = [42, height - 42];
    for (const [name, color, dx, dy] of [
      ["X", "#f58b8b", cy, -sy * sp], ["Y", "#80d9a2", -sy, -cy * sp], ["Z", "#9bbdff", 0, -cp],
    ]) {
      context.strokeStyle = color; context.fillStyle = color; context.lineWidth = 2;
      context.beginPath(); context.moveTo(...origin); context.lineTo(origin[0] + dx * 28, origin[1] + dy * 28); context.stroke();
      context.font = "12px system-ui"; context.fillText(name, origin[0] + dx * 36, origin[1] + dy * 36);
    }
  }

  function updateLayer() {
    if (!model) return;
    const value = Number(layerInput.value);
    const label = `${value} / ${model.layers.length} · Z ${model.layers[value - 1].z.toFixed(3)} mm`;
    byId("layer-label").textContent = label;
    layerInput.setAttribute("aria-valuetext", label);
    byId("previous").disabled = value <= 1;
    byId("next").disabled = value >= model.layers.length;
    schedule();
  }

  const changeZoom = factor => { zoom = Math.min(30, Math.max(0.2, zoom * factor)); schedule(); };
  byId("fit").onclick = () => { zoom = 1; schedule(); };
  byId("top").onclick = () => { pitch = Math.PI / 2; yaw = 0; schedule(); };
  byId("orbit").onclick = () => { pitch = 0.8; yaw = -0.65; schedule(); };
  byId("zoom-in").onclick = () => changeZoom(1.2);
  byId("zoom-out").onclick = () => changeZoom(1 / 1.2);
  byId("previous").onclick = () => { layerInput.stepDown(); updateLayer(); };
  byId("next").onclick = () => { layerInput.stepUp(); updateLayer(); };
  layerInput.oninput = updateLayer;
  travel.onchange = single.onchange = schedule;
  canvas.onpointerdown = event => {
    if (!model || event.button !== 0) return;
    canvas.focus(); canvas.setPointerCapture(event.pointerId);
    drag = {x: event.clientX, y: event.clientY};
  };
  canvas.onpointermove = event => {
    if (!drag) return;
    yaw += (event.clientX - drag.x) * 0.008;
    pitch = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, pitch + (event.clientY - drag.y) * 0.008));
    drag = {x: event.clientX, y: event.clientY}; schedule();
  };
  canvas.onpointerup = canvas.onpointercancel = canvas.onlostpointercapture = () => { drag = null; };
  canvas.addEventListener("wheel", event => {
    if (!model) return;
    event.preventDefault(); changeZoom(event.deltaY < 0 ? 1.1 : 1 / 1.1);
  }, {passive: false});
  canvas.onkeydown = event => {
    if (!model) return;
    if (["+", "="].includes(event.key)) changeZoom(1.2);
    else if (event.key === "-") changeZoom(1 / 1.2);
    else if (event.key === "0") zoom = 1;
    else if (event.key === "ArrowLeft") yaw -= 0.1;
    else if (event.key === "ArrowRight") yaw += 0.1;
    else if (event.key === "ArrowUp") pitch = Math.min(Math.PI / 2, pitch + 0.1);
    else if (event.key === "ArrowDown") pitch = Math.max(-Math.PI / 2, pitch - 0.1);
    else return;
    event.preventDefault(); schedule();
  };
  window.addEventListener("resize", schedule);
  byId("cancel").onclick = () => stopLoading("Preview loading canceled. Reload this page to try again.");
  window.addEventListener("pagehide", () => { if (worker) worker.terminate(); });
  // A page restored from the back/forward cache may have had its worker stopped.
  window.addEventListener("pageshow", event => {
    if (event.persisted && !model) stopLoading("Reload this page to load the preview again.");
  });

  if (!context || !window.Worker) {
    stopLoading("This browser needs Canvas and Web Worker support to preview G-code.");
    return;
  }
  try {
    worker = new Worker(root.dataset.worker);
    worker.onerror = () => stopLoading("The preview worker could not start or ran out of memory. Reload to try again, or use your slicer's preview.");
    worker.onmessage = ({data}) => {
      if (data.type === "progress") {
        const percent = Math.min(100, data.received / data.total * 100);
        progress.value = percent;
        status.textContent = `Loading and parsing G-code… ${Math.round(percent)}%`;
      } else if (data.type === "error") stopLoading(data.message);
      else if (data.type === "ready") {
        model = data.model;
        stopLoading("Preview ready");
        controls.disabled = false;
        layerInput.max = model.layers.length; layerInput.value = model.layers.length;
        if (!model.printCount) travel.checked = true;
        byId("warnings").textContent = model.warnings.join("\n");
        byId("warnings").hidden = !model.warnings.length;
        const dimensions = [0, 1, 2].map(i => (model.bounds[i + 3] - model.bounds[i]).toFixed(1));
        byId("summary").textContent = `${model.count.toLocaleString()} move segments · ${model.layers.length.toLocaleString()} layers · ${model.printCount ? "Extrusion" : "Travel"} bounds: ${dimensions.join(" × ")} mm (X × Y × Z)`;
        updateLayer();
      }
    };
    worker.postMessage({url: root.dataset.source, maxBytes: Number(root.dataset.maxBytes), size: Number(root.dataset.size)});
  } catch (_) {
    stopLoading("The preview worker could not start. Check browser support and reload this page.");
  }
  schedule();
})();

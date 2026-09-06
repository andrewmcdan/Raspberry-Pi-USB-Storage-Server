"use strict";
importScripts("parser.js");

self.onmessage = async ({data}) => {
  let reader;
  try {
    const response = await fetch(data.url, {credentials: "same-origin", cache: "no-store"});
    if (response.status === 401 || response.redirected) throw new Error("Your session expired. Return to staged files and sign in again.");
    if (!response.ok) {
      const messages = {404: "The staged file no longer exists. Refresh the file list.",
        415: "This file type is not supported.", 422: "Preview is limited to 100 MiB. You can still download and publish this file."};
      throw new Error(messages[response.status] || `Could not load G-code (HTTP ${response.status}).`);
    }
    const total = Number(response.headers.get("Content-Length")) || data.size;
    const limit = Math.min(data.maxBytes, 100 * 1024 * 1024);
    if (total > limit) throw new Error("Preview is limited to 100 MiB.");
    if (!response.body) throw new Error("This browser does not support streaming file previews.");
    reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    const parser = new GCodeParser();
    let pending = "", received = 0, lastUpdate = 0;
    const consume = (text, final = false) => {
      pending += text;
      const lines = pending.split(/\r\n|\n|\r/);
      pending = lines.pop();
      for (const line of lines) parser.line(line);
      if (pending.length > 65536) throw new Error("A G-code line is too long to preview.");
      if (final && pending) parser.line(pending);
    };
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > limit) throw new Error("Preview is limited to 100 MiB.");
      consume(decoder.decode(value, {stream: true}));
      if (performance.now() - lastUpdate > 150) {
        self.postMessage({type: "progress", received, total});
        lastUpdate = performance.now();
      }
    }
    consume(decoder.decode(), true);
    const model = parser.finish();
    self.postMessage({type: "ready", model}, [model.positions.buffer, model.extrusions.buffer]);
  } catch (error) {
    if (reader) await reader.cancel().catch(() => {});
    self.postMessage({type: "error", message: error.message || "Could not preview this file."});
  }
};

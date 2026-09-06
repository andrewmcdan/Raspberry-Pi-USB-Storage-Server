"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {Parser} = require("../opt/piusb/web/gcode_viewer/static/parser.js");
const parse = (text, options) => {
  const parser = new Parser(options);
  text.split(/\r\n|\n|\r/).forEach(line => parser.line(line));
  return parser.finish();
};
const lastPoint = model => Array.from(model.positions.slice(-3));
const close = (actual, expected, tolerance = 0.0001) => assert.ok(Math.abs(actual - expected) < tolerance, `${actual} != ${expected}`);

test("absolute extrusion, G92 E reset, comments, compact words, and travel", () => {
  const model = parse("G90\nM82\nG1 Z.2\nN1 G1X10Y0E1*99 ; E10000\nG1 Y10 E2 (X999)\nG92 E0\nG1 X0 E1\nG0 X5");
  assert.equal(model.count, 5);
  assert.equal(model.printCount, 3);
  assert.equal(model.layers.length, 1);
  assert.deepEqual(Array.from(model.extrusions), [0, 1, 1, 1, 0]);
  assert.deepEqual(model.bounds, [0, 0, 0.2, 10, 10, 0.2]);
});

test("Z hops and retract/recover moves do not create layers or extrusion", () => {
  const model = parse("M83\nG1 Z.2\nG1 X10 E1\nG1 E-1\nG0 Z.6\nG0 X20\nG0 Z.2\nG1 X21 E1\nG1 X30 E1\nG1 Z.4\nG1 X40 E1");
  assert.equal(model.layers.length, 2);
  assert.equal(model.printCount, 3);
  assert.deepEqual(model.layers.map(l => l.z), [0.2, 0.4]);
  assert.equal(model.extrusions[5], 0);
});

test("relative XYZ and explicit absolute E override; G90 clears override", () => {
  const model = parse("G1 X10 E5\nG91\nM82\nG1 X2 E6\nG1 X2 E6\nM83\nG1 X2 E1\nG90\nG1 X20 E8");
  assert.deepEqual(lastPoint(model), [20, 0, 0]);
  assert.equal(model.printCount, 4);
  assert.equal(model.extrusions[2], 0);
});

test("G92 XYZ changes logical coordinates without moving physical position", () => {
  const model = parse("G1 X100 Y20\nG92 X0 Y0\nG1 X10 Y5 E1\nG91\nG1 X5 Y2 E1");
  assert.deepEqual(lastPoint(model), [115, 27, 0]);
  assert.deepEqual(Array.from(model.positions.slice(6, 9)), [100, 20, 0]);
});

test("inches, signed decimals, CRLF and lowercase", () => {
  const model = parse("g20\r\ng91\r\ng1x+1y-.5e.1\r\ng21\r\ng1x1e1");
  const point = lastPoint(model);
  close(point[0], 26.4); close(point[1], -12.7);
  assert.equal(model.printCount, 2);
});

test("clockwise and counterclockwise I/J arcs, including a complete circle", () => {
  const ccw = parse("G1 X10\nG3 X0 Y10 I-10 J0 E1");
  const cw = parse("G1 X10\nG2 X0 Y-10 I-10 J0 E1");
  assert.deepEqual(lastPoint(ccw), [0, 10, 0]);
  assert.deepEqual(lastPoint(cw), [0, -10, 0]);
  assert.ok(ccw.printCount > 20);
  assert.ok(ccw.positions[10] > 0);
  assert.ok(cw.positions[10] < 0);
  const circle = parse("G1 X10\nG2 I-10 J0 E5");
  close(circle.bounds[0], -10, 0.01); close(circle.bounds[1], -10, 0.01);
  close(circle.bounds[4], 10, 0.01);
  assert.deepEqual(lastPoint(circle), [10, 0, 0]);
});

test("positive and negative R select short and long arcs", () => {
  const short = parse("G1 X10\nG3 X0 Y10 R10 E1");
  const long = parse("G1 X10\nG3 X0 Y10 R-10 E1");
  assert.ok(long.printCount > short.printCount * 2);
  assert.deepEqual(lastPoint(short), lastPoint(long));
});

test("helical arc interpolates Z and preserves exact endpoint", () => {
  const model = parse("G1 X10\nG3 X0 Y10 Z1 I-10 J0 E1");
  assert.deepEqual(lastPoint(model), [0, 10, 1]);
  assert.ok(model.positions[11] > 0 && model.positions[11] < 1);
});

test("homing moves are excluded and unknown macros warn", () => {
  const model = parse("START_PRINT\nG1 X10\nG28 X\nG1 X1\nG29\nT1");
  assert.equal(model.count, 2);
  assert.deepEqual(Array.from(model.positions.slice(6, 9)), [0, 0, 0]);
  assert.ok(model.warnings.some(w => w.includes("macros")));
  assert.ok(model.warnings.some(w => w.includes("Homing")));
});

test("travel-only files get a visible fallback layer", () => {
  const model = parse("G1 X20\nY10\nG0 Z2");
  assert.equal(model.printCount, 0);
  assert.equal(model.layers.length, 1);
  assert.deepEqual(model.bounds, [0, 0, 0, 20, 10, 2]);
  assert.ok(model.warnings.some(w => w.includes("No extrusion")));
});

test("empty, binary, oversized lines and unsupported motion fail clearly", () => {
  for (const text of ["; empty", "GCDE123", "G1X1\u0000", "G1X1\ufffd", "a".repeat(65537),
    "G18\nG2 X10 I5", "G5 X20", "G90.1", "G2 X10 R1", "G2 X10", "G2 I0 J0", "G1 X999999999999999999999999999"]) {
    assert.throws(() => parse(text));
  }
});

test("segment limit rejects whole preview instead of silently truncating", () => {
  assert.throws(() => parse("G1 X1\nG1 X2\nG1 X3", {maxSegments: 2}), /segment preview limit/);
  assert.throws(() => parse("G2 I10 J0", {maxSegments: 2}), /segment preview limit/);
});

test("buffer growth retains every segment", () => {
  const parser = new Parser();
  for (let i = 1; i <= 20000; i++) parser.line(`G1 X${i} E${i}`);
  const model = parser.finish();
  assert.equal(model.count, 20000);
  assert.deepEqual(lastPoint(model), [20000, 0, 0]);
});

// Exercise the actual worker protocol, including failures before parsing and
// streamed chunks that split both commands and UTF-8 characters.
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
async function runWorker(response, maxBytes = 100 * 1024 * 1024) {
  const messages = [];
  const sandbox = vm.createContext({GCodeParser: Parser, importScripts() {}, TextDecoder, performance,
    fetch: async () => response, self: {postMessage: message => messages.push(message)}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../opt/piusb/web/gcode_viewer/static/worker.js"), "utf8"), sandbox);
  await sandbox.self.onmessage({data: {url: "/api/gcode/file?path=test.gcode", maxBytes, size: 100}});
  return messages.at(-1);
}

test("worker reports expired sessions and rejects oversized downloads", async () => {
  assert.match((await runWorker(new Response("{}", {status: 401}))).message, /session expired/);
  assert.match((await runWorker(new Response("", {status: 404}))).message, /no longer exists/);
  assert.match((await runWorker(new Response("", {headers: {"Content-Length": "104857601"}}))).message, /100 MiB/);
  assert.match((await runWorker(new Response("G1 X20 E1\n"), 5)).message, /100 MiB/);
});

test("worker preserves commands split across streamed chunks", async () => {
  const input = new TextEncoder().encode("; café\r\nG90\nM83\nG1 X20 E1\nG1 Y10 E1");
  const stream = new ReadableStream({start(controller) {
    for (const byte of input) controller.enqueue(new Uint8Array([byte]));
    controller.close();
  }});
  const result = await runWorker(new Response(stream));
  assert.equal(result.type, "ready");
  assert.equal(result.model.printCount, 2);
  assert.deepEqual(lastPoint(result.model), [20, 10, 0]);
});

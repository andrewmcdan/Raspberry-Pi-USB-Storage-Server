/* Streaming parser, shared by the browser worker and Node regression tests.
 * Coordinate/extruder modes follow Marlin: G90/G91 clear M82/M83 overrides.
 * No G-code is executed. Segments contain [x0,y0,z0,x1,y1,z1] in millimeters.
 */
"use strict";
(function (root) {
  const EPSILON = 0.00001;
  const MAX_SEGMENTS = 1000000;
  const MAX_LAYERS = 20000;

  class Parser {
    constructor({maxSegments = MAX_SEGMENTS} = {}) {
      this.limit = Math.min(maxSegments, MAX_SEGMENTS);
      this.capacity = Math.min(8192, this.limit);
      this.positions = new Float32Array(this.capacity * 6);
      this.extrusions = new Uint8Array(this.capacity);
      this.count = 0;
      this.printCount = 0;
      this.lines = 0;
      this.position = [0, 0, 0];
      this.offset = [0, 0, 0];
      this.e = 0;
      this.retracted = 0;
      this.relative = false;
      this.eRelative = false;
      this.units = 1;
      this.plane = 17;
      this.motion = null;
      this.layers = [];
      this.warnings = new Set();
      this.bounds = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
      this.travelBounds = [...this.bounds];
    }

    warn(message) {
      if (this.warnings.size < 20) this.warnings.add(message);
    }

    line(raw) {
      this.lines++;
      if (raw.length > 65536) throw new Error("A G-code line is too long to preview.");
      if (/[\x00-\x08\x0e-\x1f\ufffd]/.test(raw) || (this.lines === 1 && raw.startsWith("GCDE"))) {
        throw new Error("Binary or invalid text G-code is not supported. Export plain-text G-code from your slicer.");
      }
      const line = raw.replace(/\([^)]*\)/g, "").split(/[;*]/, 1)[0].trim().toUpperCase().replace(/^N\d+\s*/, "");
      if (!line || line === "%") return;
      const commandMatch = line.match(/^([GMT])\s*(\d+(?:\.\d+)?)/);
      let command;
      let tail;
      if (commandMatch) {
        command = commandMatch[1] + Number(commandMatch[2]);
        tail = line.slice(commandMatch[0].length);
      } else if (/^[XYZEF][-+\d.]/.test(line) && this.motion) {
        command = this.motion;
        tail = line;
      } else {
        this.warn("Unrecognized commands or firmware macros were skipped.");
        return;
      }
      const params = {};
      for (const match of tail.matchAll(/([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))/g)) {
        const value = Number(match[2]);
        if (!Number.isFinite(value) || Math.abs(value) > 10000000) throw new Error("G-code contains an out-of-range value.");
        params[match[1]] = value;
      }
      if (command === "G90" || command === "G91") {
        this.relative = this.eRelative = command === "G91";
      } else if (command === "M82" || command === "M83") {
        this.eRelative = command === "M83";
      } else if (command === "G20" || command === "G21") {
        this.units = command === "G20" ? 25.4 : 1;
      } else if (["G17", "G18", "G19"].includes(command)) {
        this.plane = Number(command.slice(1));
      } else if (command === "G92") {
        for (const [axis, i] of [["X", 0], ["Y", 1], ["Z", 2]]) {
          if (axis in params) this.offset[i] = this.position[i] - params[axis] * this.units;
        }
        if ("E" in params) this.e = params.E * this.units;
      } else if (command === "G28") {
        // The actual home location is firmware-specific. Do not draw homing.
        const axes = tail.match(/[XYZ]/g) || ["X", "Y", "Z"];
        for (const axis of axes) {
          const i = "XYZ".indexOf(axis);
          this.position[i] = this.offset[i] = 0;
        }
        this.warn("Homing assumes an origin of 0; firmware home and tool offsets are not simulated.");
      } else if (["G0", "G1", "G2", "G3"].includes(command)) {
        this.motion = command;
        this.move(command, params);
      } else if (["G5", "G6", "G53", "G54", "G55", "G56", "G57", "G58", "G59", "G90.1"].includes(command)) {
        throw new Error(`Unsupported motion or coordinate mode ${command}. Use your slicer's preview for this file.`);
      } else if (command.startsWith("T")) {
        this.warn("Tool changes are shown without firmware tool offsets or tool-change motions.");
      } else if (command.startsWith("G") && !["G4", "G21", "G91.1"].includes(command)) {
        this.warn(`Command ${command} is not simulated.`);
      }
    }

    move(command, params) {
      const start = this.position;
      const end = start.map((value, i) => {
        const axis = "XYZ"[i];
        return axis in params ? params[axis] * this.units + (this.relative ? value : this.offset[i]) : value;
      });
      if (end.some(value => !Number.isFinite(value) || Math.abs(value) > 1000000)) {
        throw new Error("Toolpath coordinates are outside the preview range.");
      }
      let deltaE = 0;
      if ("E" in params) {
        const nextE = params.E * this.units + (this.eRelative ? this.e : 0);
        deltaE = nextE - this.e;
        this.e = nextE;
      }
      const deposited = Math.max(0, deltaE - this.retracted);
      this.retracted = Math.max(0, this.retracted - deltaE);
      const printing = deposited > EPSILON;
      if (command === "G2" || command === "G3") this.arc(start, end, params, command === "G2", printing);
      else this.segment(start, end, printing);
      this.position = end;
    }

    arc(start, end, params, clockwise, printing) {
      if (this.plane !== 17) throw new Error("Only XY-plane G2/G3 arcs are supported. Export linear moves from your slicer.");
      if ((params.P || 0) !== 0) throw new Error("Multi-turn arcs are not supported. Export linear moves from your slicer.");
      const sweepFor = (cx, cy) => {
        const a = Math.atan2(start[1] - cy, start[0] - cx);
        let sweep = Math.atan2(end[1] - cy, end[0] - cx) - a;
        if (clockwise && sweep >= -EPSILON) sweep -= Math.PI * 2;
        if (!clockwise && sweep <= EPSILON) sweep += Math.PI * 2;
        return [a, sweep];
      };
      let cx, cy;
      if ("R" in params) {
        if ("I" in params || "J" in params) throw new Error("An arc mixes radius and center offsets.");
        const radius = Math.abs(params.R * this.units);
        const dx = end[0] - start[0], dy = end[1] - start[1];
        const chord = Math.hypot(dx, dy);
        if (chord < EPSILON || radius < chord / 2 - EPSILON) throw new Error("Invalid radius arc in G-code.");
        const h = Math.sqrt(Math.max(0, radius * radius - chord * chord / 4));
        for (const sign of [1, -1]) {
          cx = (start[0] + end[0]) / 2 - sign * dy * h / chord;
          cy = (start[1] + end[1]) / 2 + sign * dx * h / chord;
          if ((Math.abs(sweepFor(cx, cy)[1]) <= Math.PI + EPSILON) === (params.R >= 0)) break;
        }
      } else if ("I" in params || "J" in params) {
        cx = start[0] + (params.I || 0) * this.units;
        cy = start[1] + (params.J || 0) * this.units;
      } else throw new Error("Arc has no I/J center or R radius.");
      const radius = Math.hypot(start[0] - cx, start[1] - cy);
      const endRadius = Math.hypot(end[0] - cx, end[1] - cy);
      if (radius < EPSILON || Math.abs(radius - endRadius) > Math.max(0.1, radius * 0.01)) {
        throw new Error("Arc endpoints do not match its radius.");
      }
      const [angle, sweep] = sweepFor(cx, cy);
      const steps = Math.max(1, Math.ceil(Math.abs(sweep) * Math.max(radius / 0.5, 1 / 0.0873)));
      if (steps > this.limit - this.count) throw new Error("Toolpath exceeds the one-million-segment preview limit.");
      let previous = start;
      for (let i = 1; i <= steps; i++) {
        const fraction = i / steps;
        const next = i === steps ? end : [cx + radius * Math.cos(angle + sweep * fraction),
          cy + radius * Math.sin(angle + sweep * fraction), start[2] + (end[2] - start[2]) * fraction];
        this.segment(previous, next, printing);
        previous = next;
      }
    }

    segment(start, end, printing) {
      if (Math.hypot(...end.map((v, i) => v - start[i])) < EPSILON) return;
      if (this.count >= this.limit) throw new Error("Toolpath exceeds the one-million-segment preview limit.");
      if (this.count === this.capacity) {
        this.capacity = Math.min(this.limit, this.capacity * 2);
        const positions = new Float32Array(this.capacity * 6);
        const extrusions = new Uint8Array(this.capacity);
        positions.set(this.positions); extrusions.set(this.extrusions);
        this.positions = positions; this.extrusions = extrusions;
      }
      if (printing) {
        const last = this.layers[this.layers.length - 1];
        if (!last || Math.abs(last.z - end[2]) > 0.001) {
          if (this.layers.length >= MAX_LAYERS) throw new Error("Too many height changes to preview (20,000 maximum). Spiral/non-planar prints may need your slicer's preview.");
          if (last) last.end = this.count;
          this.layers.push({z: end[2], start: last ? this.count : 0, end: this.count});
        }
        this.printCount++;
      }
      this.positions.set([...start, ...end], this.count * 6);
      this.extrusions[this.count] = printing ? 1 : 0;
      this.count++;
      for (const point of [start, end]) for (let i = 0; i < 3; i++) {
        this.travelBounds[i] = Math.min(this.travelBounds[i], point[i]);
        this.travelBounds[i + 3] = Math.max(this.travelBounds[i + 3], point[i]);
        if (printing) {
          this.bounds[i] = Math.min(this.bounds[i], point[i]);
          this.bounds[i + 3] = Math.max(this.bounds[i + 3], point[i]);
        }
      }
    }

    finish() {
      if (!this.count) throw new Error("No supported toolpath moves were found in this file.");
      if (!this.layers.length) {
        this.layers.push({z: this.position[2], start: 0, end: this.count});
        this.bounds = this.travelBounds;
        this.warn("No extrusion was found. Showing travel moves only.");
      }
      this.layers[this.layers.length - 1].end = this.count;
      return {positions: this.positions.slice(0, this.count * 6), extrusions: this.extrusions.slice(0, this.count),
        layers: this.layers, bounds: this.bounds, count: this.count, printCount: this.printCount,
        lines: this.lines, warnings: [...this.warnings]};
    }
  }
  root.GCodeParser = Parser;
  if (typeof module !== "undefined") module.exports = {Parser};
})(globalThis);

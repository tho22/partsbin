// Web Bluetooth client for the SUPVAN / Katasymbol E11 label printer (reports itself as "G15").
//
// Frame formats as documented by https://github.com/heeen/supvan-cups (MIT); the print flow follows
// the vendor Android app's E11 path (G15Print.doPrint / transfer). Responses arrive as notifications
// that echo the command byte at offset 7; on the E11 every 512-byte data frame is acknowledged
// with a notification carrying 0xBB. The server renders and compresses the label
// (/api/printer/supvan/...); this module drives the flow and relays the bytes.

const sig = (short) => `0000${short}-0000-1000-8000-00805f9b34fb`;

// Known service -> (notify, write) characteristic patterns, first match wins (vendor BLEUtils).
export const SERVICE_PATTERNS = [
  { service: sig("fee7"), notify: sig("fec1"), write: sig("fec1") },
  { service: "0000e0ff-3c17-d293-8e48-14fe2e4da212", notify: sig("ffe1"), write: sig("ffe9") },
  { service: sig("ff00"), notify: sig("ff01"), write: sig("ff02") },
];
const EXTRA_SERVICES = [sig("180a"), sig("180f")]; // device information, battery

// Name prefixes the printers advertise (firmware serial names, e.g. "T0184C…" for an E11).
const NAME_PREFIXES = ["T0", "E1", "G1", "T5", "Katasymbol", "SUPVAN", "Supvan"];

export const CMD = {
  BUF_FULL: 0x10, INQUIRY_STA: 0x11, CHECK_DEVICE: 0x12, START_PRINT: 0x13, STOP_PRINT: 0x14,
  RD_DEV_NAME: 0x16, READ_REV: 0x17, RD_DPI: 0x22, RETURN_MAT: 0x30, NEXT_ZIPPEDBULK: 0x5c,
  SEND_DATA: 0xbb, READ_FWVER: 0xc5,
};

const RESPONSE_TIMEOUT_MS = 4000;
const CHUNK_DELAY_MS = 10;
const DATA_FRAME_LEN = 512;
const RESP_HEADER_LEN = 22;
const STATUS_OFFSET = 14;
// BluetoothCharacteristicProperties exposes its flags as prototype getters, so Object.entries() sees none.
const PROP_NAMES = ["broadcast", "read", "writeWithoutResponse", "write", "notify", "indicate",
  "authenticatedSignedWrites", "reliableWrite", "writableAuxiliaries"];
const propsOf = (c) => PROP_NAMES.filter((n) => c.properties[n]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Build a 16-byte command frame (make_cmd_start_trans in supvan-proto). */
export function makeCmd(cmd, param = 0, blockCount = 0) {
  const f = new Uint8Array(16);
  f.set([0x7e, 0x5a, 0x0c, 0x00, 0x10, 0x01, 0xaa, cmd]);
  f[11] = 0x01;
  f[12] = param & 0xff; f[13] = (param >> 8) & 0xff;
  f[14] = blockCount & 0xff; f[15] = (blockCount >> 8) & 0xff;
  const chk = f.slice(10).reduce((a, b) => (a + b) & 0xffff, 0);
  f[8] = chk & 0xff; f[9] = chk >> 8;
  return f;
}

export const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join(" ");

/** Decode an INQUIRY_STA response (status registers at offset 14; bits per the app's PRINTER_FLAG). */
export function parseStatus(resp) {
  if (!resp || resp.length < STATUS_OFFSET + 6 || resp[7] !== CMD.INQUIRY_STA) return null;
  const [b0, b1, b2, b3] = resp.slice(STATUS_OFFSET, STATUS_OFFSET + 4);
  const s = {
    bufFull: !!(b0 & 0x01), labelEnd: !!(b0 & 0x04), lowBattery: !!(b0 & 0x40), busy: !!(b1 & 0x04),
    printing: !!(b2 & 0x40), printCount: resp[STATUS_OFFSET + 4] | (resp[STATUS_OFFSET + 5] << 8),
  };
  // The checks the app runs for the E11 (G15Print.devE11CheckErrMsg).
  const errors = [
    [b2 & 0x08, "cover open"], [b3 & 0x01, "no label installed"], [b0 & 0x02, "label read error"],
    [b0 & 0x04, "labels used up"], [b0 & 0x40, "low battery"],
  ].filter(([on]) => on).map(([, msg]) => msg);
  const unknown = [[b0 & 0x10, "bit m0.4"], [b0 & 0x20, "bit m0.5"], [b1 & 0x08, "bit m1.3"]]
    .filter(([on]) => on).map(([, msg]) => msg);
  s.errors = errors;
  s.raw = hex(resp.slice(STATUS_OFFSET, Math.min(resp.length, STATUS_OFFSET + 8)));
  s.summary = [...errors, s.busy && "busy", s.printing && "printing", s.bufFull && "buffer full",
    ...unknown].filter(Boolean).join(", ") || "ready";
  return s;
}

/** Decode a RETURN_MAT response: the loaded label roll (status.rs parse_material_payload). */
export function parseMaterial(resp) {
  if (!resp || resp.length < RESP_HEADER_LEN + 21 || resp[7] !== CMD.RETURN_MAT) return null;
  const p = resp.slice(RESP_HEADER_LEN);
  return { type: p[17], widthMm: p[18], lengthMm: p[19], gapMm: p[20] };
}

export class SupvanPrinter {
  constructor(device, server, pattern, notifyChar, writeChar) {
    Object.assign(this, { device, server, pattern, notifyChar, writeChar });
    this.waiters = [];
    this.chunk = 128;
    // The vendor app writes with response, but the E11 only accepts write-without-response.
    const props = writeChar.properties;
    this.writeMode = props.write || !props.writeWithoutResponse ? "withResponse" : "withoutResponse";
    notifyChar.addEventListener("characteristicvaluechanged", (e) => {
      const v = new Uint8Array(e.target.value.buffer.slice(0));
      const i = this.waiters.findIndex((w) => v[7] === w.cmd);
      if (i >= 0) this.waiters.splice(i, 1)[0].resolve(v);
    });
  }

  static isSupported() { return !!navigator.bluetooth; }

  get connected() { return !!this.device.gatt.connected; }

  /** Ask the user to pick the printer and connect. Must be called from a user gesture. */
  static async connect({ showAll = false } = {}) {
    const services = [...SERVICE_PATTERNS.map((p) => p.service), ...EXTRA_SERVICES];
    const device = await navigator.bluetooth.requestDevice(showAll
      ? { acceptAllDevices: true, optionalServices: services }
      : {
        filters: [...NAME_PREFIXES.map((namePrefix) => ({ namePrefix })), ...SERVICE_PATTERNS.map((p) => ({ services: [p.service] }))],
        optionalServices: services,
      });
    const server = await device.gatt.connect();
    const inventory = [];
    for (const svc of await server.getPrimaryServices()) {
      const chars = await svc.getCharacteristics().catch(() => []);
      inventory.push({ uuid: svc.uuid, chars: chars.map((c) => ({ uuid: c.uuid, props: propsOf(c) })) });
    }
    for (const p of SERVICE_PATTERNS) {
      if (!inventory.some((s) => s.uuid === p.service)) continue;
      const service = await server.getPrimaryService(p.service);
      const notifyChar = await service.getCharacteristic(p.notify);
      const writeChar = p.write === p.notify ? notifyChar : await service.getCharacteristic(p.write);
      await notifyChar.startNotifications();
      return { printer: new SupvanPrinter(device, server, p, notifyChar, writeChar), inventory };
    }
    return { printer: null, device, server, inventory };
  }

  async writeChunk(chunk) {
    const send = (mode) => (mode === "withResponse"
      ? this.writeChar.writeValueWithResponse(chunk)
      : this.writeChar.writeValueWithoutResponse(chunk));
    try {
      await send(this.writeMode);
    } catch (e) {
      if (e.name !== "NotSupportedError" && !/not permitted/i.test(e.message)) throw e;
      const other = this.writeMode === "withResponse" ? "withoutResponse" : "withResponse";
      await send(other);
      this.writeMode = other;
    }
  }

  /** Write bytes in paced chunks (vendor: 128-byte pieces, ~10 ms apart). */
  async write(bytes) {
    for (let i = 0; i < bytes.length;) {
      const piece = bytes.slice(i, i + this.chunk);
      try {
        await this.writeChunk(piece);
      } catch (e) {
        // A small ATT MTU rejects 128-byte writes; retry this piece with the 20-byte minimum.
        if (this.chunk > 20) { this.chunk = 20; continue; }
        throw e;
      }
      i += piece.length;
      if (i < bytes.length) await sleep(CHUNK_DELAY_MS);
    }
  }

  /** Write bytes and wait for a notification with `cmd` at offset 7 (null on timeout). */
  async exchange(bytes, cmd) {
    const reply = new Promise((resolve) => {
      const w = { cmd, resolve };
      this.waiters.push(w);
      setTimeout(() => {
        const i = this.waiters.indexOf(w);
        if (i >= 0) { this.waiters.splice(i, 1); resolve(null); }
      }, RESPONSE_TIMEOUT_MS);
    });
    await this.write(bytes);
    return reply;
  }

  /** Send a command frame and wait for the notification echoing it (null on timeout). */
  command(cmd, param = 0, blockCount = 0) { return this.exchange(makeCmd(cmd, param, blockCount), cmd); }

  async status() { return parseStatus(await this.command(CMD.INQUIRY_STA)); }

  /** Resolution in dots per mm (command 0x22 reports dots/mm x 100; the app falls back to 8). */
  async dotsPerMm() {
    const r = await this.command(CMD.RD_DPI);
    const v = r && r.length >= 24 ? r[22] | (r[23] << 8) : 0;
    return v >= 700 && v <= 800 ? v / 100 : 8;
  }

  async material() { return parseMaterial(await this.command(CMD.RETURN_MAT)); }

  async waitFor(what, timeoutMs, intervalMs, done) {
    const start = Date.now();
    for (;;) {
      const s = await this.status();
      if (s && s.raw !== this.lastRaw) {
        // Status register trace for reverse engineering: logged whenever the bytes change.
        this.lastRaw = s.raw;
        this.onstatus?.(`  status ${s.raw} (${s.summary}) after ${Date.now() - start} ms, waiting for ${what}`);
      }
      if (s) {
        if (s.errors.length) throw new Error(`Printer error while waiting for ${what}: ${s.errors.join(", ")}`);
        if (done(s)) return s;
      }
      if (Date.now() - start > timeoutMs) throw new Error(`Timed out waiting for ${what}${s ? ` (${s.summary})` : ""}`);
      await sleep(intervalMs);
    }
  }

  /**
   * Print one label. `job` comes from /api/printer/supvan/...: {blocks: [{length, packets, frames}]}
   * with frames as base64 of the concatenated 512-byte data frames. Mirrors the app's E11 path
   * (G15Print.doPrint, initLZMADataB, transfer, returnPrintResult).
   */
  async print(job, log = () => {}) {
    this.lastRaw = null;
    this.onstatus = log;
    const first = await this.status();
    if (!first) throw new Error("Printer did not answer");
    if (first.printing) {
      log("printer still printing: STOP_PRINT");
      await this.command(CMD.STOP_PRINT);
    }
    if (!(await this.command(CMD.CHECK_DEVICE))) throw new Error("Printer did not answer CHECK_DEVICE");
    await this.waitFor("the printer to become idle", 6000, 100, (s) => !s.busy);
    log("START_PRINT");
    if (!(await this.command(CMD.START_PRINT, 0))) throw new Error("No answer to START_PRINT");
    await this.waitFor("printing to start", 6000, 100, (s) => s.printing);
    try {
      for (const [i, block] of job.blocks.entries()) {
        await this.waitFor("the printer buffer", 15000, 20, (s) => !s.bufFull);
        const frames = Uint8Array.from(atob(block.frames), (c) => c.charCodeAt(0));
        if (frames.length !== block.packets * DATA_FRAME_LEN) throw new Error("Malformed print job");
        log(`block ${i + 1}/${job.blocks.length}: ${block.length} bytes in ${block.packets} frames`);
        if (!(await this.command(CMD.NEXT_ZIPPEDBULK, DATA_FRAME_LEN, block.packets))) {
          throw new Error("No answer to NEXT_ZIPPEDBULK");
        }
        for (let f = 0; f < block.packets; f++) {
          const ack = await this.exchange(frames.subarray(f * DATA_FRAME_LEN, (f + 1) * DATA_FRAME_LEN), CMD.SEND_DATA);
          if (!ack) throw new Error(`No acknowledgement for data frame ${f + 1}/${block.packets}`);
        }
        await sleep(20);
        if (!(await this.command(CMD.BUF_FULL, 0))) throw new Error("No answer to BUF_FULL");
      }
    } catch (e) {
      log("sending STOP_PRINT");
      await this.command(CMD.STOP_PRINT).catch(() => {});
      throw e;
    }
    // The app polls every 500 ms for up to 8 s and treats a still-running print as done.
    try {
      await this.waitFor("the print to finish", 8000, 500, (s) => !s.printing);
    } catch (e) {
      if (!/^Timed out/.test(e.message)) throw e;
      log("printer still busy after 8 s");
    }
    log("done");
  }

  disconnect() { this.device.gatt.disconnect(); }
}

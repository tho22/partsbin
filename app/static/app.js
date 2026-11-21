// Partsbin frontend: hash-routed single page app without build step.

import { CMD, SupvanPrinter } from "./supvan.js";

const view = document.getElementById("view");
const state = { meta: null, locations: null, draft: null, cleanup: null };

// ------------------------------------------------------------ helpers

class Raw { constructor(s) { this.s = s; } }
const raw = (s) => new Raw(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function html(strings, ...vals) {
  let out = strings[0];
  vals.forEach((v, i) => {
    if (v instanceof Raw) out += v.s;
    else if (Array.isArray(v)) out += v.map((x) => (x instanceof Raw ? x.s : esc(x))).join("");
    else out += esc(v);
    out += strings[i + 1];
  });
  return raw(out);
}
const render = (tpl) => { view.innerHTML = tpl.s; };
const $ = (sel, root = view) => root.querySelector(sel);
const $$ = (sel, root = view) => [...root.querySelectorAll(sel)];

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch("/api" + path, init);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { /* not JSON */ }
    if (typeof msg !== "string") msg = JSON.stringify(msg);
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2800);
}

const CATEGORY_ICONS = {
  resistor: "〰️", capacitor: "🔋", inductor: "🌀", diode: "▶️", led: "💡", transistor: "🔺", ic: "🔲",
  microcontroller: "🧠", "dev-board": "🟦", "single-board-computer": "🍓", sensor: "🌡️", module: "🧩",
  display: "🖥️", connector: "🔌", switch: "🔘", relay: "🧲", power: "⚡", crystal: "💎", cable: "➰",
  mechanical: "🔩", tool: "🔧", other: "📦",
};
const icon = (cat) => CATEGORY_ICONS[cat] || "📦";
const imgUrl = (name) => `/api/images/${encodeURIComponent(name)}`;
const thumb = (p) => (p.image ? html`<img class="thumb" src="${imgUrl(p.image)}" alt="" loading="lazy">` : html`<span class="thumb">${icon(p.category)}</span>`);
const isLow = (p) => p.min_quantity > 0 && p.quantity <= p.min_quantity;

function partRow(p, extra = "") {
  const sub = [p.value, p.package, p.location_name].filter(Boolean).join(" · ");
  return html`<li><a href="#/part/${p.id}">${thumb(p)}
    <div class="grow"><div class="title">${p.name}</div><div class="sub">${sub || p.category}</div></div>
    <span class="qty ${isLow(p) ? "low" : ""}">${p.quantity}</span>${raw(extra)}</a></li>`;
}

async function getMeta() {
  if (!state.meta) state.meta = await api("/meta");
  return state.meta;
}
async function getLocations(force = false) {
  if (!state.locations || force) state.locations = await api("/locations");
  return state.locations;
}
const invalidateLocations = () => { state.locations = null; };

function locationOptions(locs, selected) {
  return [html`<option value="">— none —</option>`,
    ...locs.map((l) => html`<option value="${l.id}" ${l.id === selected ? raw("selected") : ""}>${l.path}</option>`)];
}

function pickImage({ capture = true } = {}) {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    if (capture) input.capture = "environment";
    input.onchange = () => resolve(input.files[0] || null);
    input.click();
  });
}

async function uploadImage(blob) {
  const fd = new FormData();
  fd.append("file", blob, "photo.jpg");
  return (await api("/images", { method: "POST", body: fd })).image;
}

// ------------------------------------------------------------ router

const routes = [
  [/^\/$/, homeView],
  [/^\/parts$/, partsView],
  [/^\/part\/new$/, (params) => partFormView(null, params)],
  [/^\/part\/(\d+)$/, (id) => partView(+id)],
  [/^\/part\/(\d+)\/edit$/, (id) => partFormView(+id)],
  [/^\/capture$/, captureView],
  [/^\/locations$/, locationsView],
  [/^\/location\/(\d+)$/, (id) => locationView(+id)],
  [/^\/labels$/, labelsView],
  [/^\/printer$/, printerView],
  [/^\/projects$/, projectsView],
  [/^\/project\/(\d+)$/, (id) => projectView(+id)],
];

async function router() {
  if (state.cleanup) { state.cleanup(); state.cleanup = null; }
  const full = location.hash.slice(1) || "/";
  const [path, query] = full.split("?");
  const params = new URLSearchParams(query || "");
  const tab = path.split("/")[1] || "home";
  $$(".tabbar a", document).forEach((a) => {
    const t = a.dataset.tab;
    a.classList.toggle("active", t === tab || (t === "parts" && tab === "part") || (t === "locations" && tab === "location") || (t === "projects" && tab === "project"));
  });
  for (const [re, fn] of routes) {
    const m = path.match(re);
    if (m) {
      try { await fn(...m.slice(1), params); }
      catch (e) { render(html`<div class="card">⚠️ ${e.message}</div>`); }
      window.scrollTo(0, 0);
      return;
    }
  }
  render(html`<p class="empty">Not found</p>`);
}
window.addEventListener("hashchange", router);

document.getElementById("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = document.getElementById("search-input").value.trim();
  location.hash = `#/parts?q=${encodeURIComponent(q)}`;
});

// ------------------------------------------------------------ home

async function homeView() {
  const [stats, recent, low] = await Promise.all([
    api("/stats"), api("/parts?limit=8"), api("/parts?low_stock=true&limit=20"),
  ]);
  render(html`
    <div class="card stats">
      <div class="stat"><b>${stats.parts}</b><small>part types</small></div>
      <div class="stat"><b>${stats.pieces}</b><small>pieces</small></div>
      <a class="stat ${stats.low_stock ? "warn" : ""}" href="#/parts?low=1"><b>${stats.low_stock}</b><small>low stock</small></a>
    </div>
    <div class="actions">
      <a class="btn primary" href="#/capture">📷 Capture / scan</a>
      <a class="btn" href="#/part/new">＋ Add manually</a>
    </div>
    ${low.length ? html`<h2>Running low</h2><div class="card"><ul class="list">${low.map((p) => partRow(p))}</ul></div>` : ""}
    <h2>Recently changed</h2>
    <div class="card">${recent.length ? html`<ul class="list">${recent.map((p) => partRow(p))}</ul>` : html`<p class="empty">No parts yet. Start by capturing one!</p>`}</div>
  `);
}

// ------------------------------------------------------------ parts list

async function partsView(params) {
  const meta = await getMeta();
  const q = params.get("q") || "";
  const cat = params.get("category") || "";
  const low = params.get("low") === "1";
  document.getElementById("search-input").value = q;
  const qs = new URLSearchParams({ q, category: cat, low_stock: low, limit: 500 });
  const parts = await api(`/parts?${qs}`);
  const link = (c) => {
    const p = new URLSearchParams({ q, category: c });
    if (low) p.set("low", "1");
    return `#/parts?${p}`;
  };
  render(html`
    <h1>Parts ${q ? html`<span class="muted">“${q}”</span>` : ""}</h1>
    <div class="chips">
      <a class="chip ${!cat && !low ? "active" : ""}" href="#/parts?q=${encodeURIComponent(q)}">All</a>
      <a class="chip ${low ? "active" : ""}" href="#/parts?low=1&q=${encodeURIComponent(q)}">⚠️ Low</a>
      ${meta.categories.map((c) => html`<a class="chip ${c === cat ? "active" : ""}" href="${link(c)}">${icon(c)} ${c}</a>`)}
    </div>
    <div class="card">${parts.length ? html`<ul class="list">${parts.map((p) => partRow(p))}</ul>` : html`<p class="empty">Nothing found.</p>`}</div>
    <div class="actions"><a class="btn" href="#/part/new">＋ Add part</a></div>
  `);
}

// ------------------------------------------------------------ part detail

async function partView(id) {
  const p = await api(`/parts/${id}`);
  const available = p.quantity - p.reserved;
  render(html`
    ${p.image ? html`<img class="hero" src="${imgUrl(p.image)}" alt="">` : ""}
    <h1>${icon(p.category)} ${p.name}</h1>
    <div class="card">
      <div class="stock">
        <button data-delta="-1" aria-label="Remove one">−</button>
        <span class="qty ${isLow(p) ? "low" : ""}">${p.quantity}</span>
        <button data-delta="1" aria-label="Add one">＋</button>
      </div>
      <div class="actions" style="justify-content:center">
        <button data-delta-ask="-">− n</button><button data-delta-ask="+">＋ n</button>
      </div>
      <p class="muted" style="text-align:center;margin:0">
        ${p.min_quantity ? html`min. ${p.min_quantity} · ` : ""}${p.reserved ? html`${p.reserved} reserved · ${available} available` : "none reserved"}
      </p>
    </div>
    <div class="card">
      <dl class="props">
        <dt>Category</dt><dd>${p.category}</dd>
        ${p.value ? html`<dt>Value</dt><dd>${p.value}</dd>` : ""}
        ${p.package ? html`<dt>Package</dt><dd>${p.package}</dd>` : ""}
        ${p.manufacturer ? html`<dt>Manufacturer</dt><dd>${p.manufacturer}</dd>` : ""}
        ${p.mpn ? html`<dt>MPN</dt><dd>${p.mpn}</dd>` : ""}
        <dt>Location</dt><dd>${p.location_id ? html`<a href="#/location/${p.location_id}">${p.location_path}</a>` : html`<span class="muted">not set</span>`}</dd>
      </dl>
      ${p.description ? html`<p>${p.description}</p>` : ""}
      ${p.notes ? html`<p class="muted" style="white-space:pre-wrap">${p.notes}</p>` : ""}
    </div>
    ${p.datasheet_url || p.links.length ? html`<h2>Datasheet & links</h2><div class="card"><ul class="list">
      ${p.datasheet_url ? html`<li><a href="${p.datasheet_url}" target="_blank" rel="noopener">📄 <span class="grow">Datasheet</span></a></li>` : ""}
      ${p.links.map((l) => html`<li><a href="${l.url}" target="_blank" rel="noopener">🔗 <span class="grow">${l.label || l.url}</span></a></li>`)}
    </ul></div>` : ""}
    ${p.projects.length ? html`<h2>Projects</h2><div class="card"><ul class="list">
      ${p.projects.map((pr) => html`<li><a href="#/project/${pr.id}">🛠️ <span class="grow">${pr.name}</span>
        <span class="badge">${pr.consumed ? "used" : "reserved"} ${pr.quantity}</span></a></li>`)}
    </ul></div>` : ""}
    <h2>Label</h2>
    <div class="card"><div class="label"><img src="/api/qr/part/${p.id}.svg" alt="QR"><div><b>${p.name}</b><small>${p.value} ${p.package}</small></div></div>
      ${SupvanPrinter.isSupported() ? html`<div class="actions no-print">${printerButton("bt-print")}</div>` : ""}</div>
    ${p.history.length ? html`<h2>History</h2><div class="card"><ul class="list">
      ${p.history.map((h) => html`<li><div class="row"><span class="qty">${h.delta > 0 ? "+" : ""}${h.delta}</span>
        <span class="grow sub">${h.reason}</span><span class="sub">${h.created_at.slice(0, 16)}</span></div></li>`)}
    </ul></div>` : ""}
    <div class="actions">
      <a class="btn primary" href="#/part/${p.id}/edit">✏️ Edit</a>
      <button id="reserve">🛠️ Add to project</button>
      <button id="del" class="danger">🗑️ Delete</button>
    </div>
  `);

  const change = async (delta) => {
    await api(`/parts/${id}/stock`, { json: { delta, reason: delta > 0 ? "added" : "taken" } });
    partView(id);
  };
  $$("[data-delta]").forEach((b) => (b.onclick = () => change(+b.dataset.delta)));
  $$("[data-delta-ask]").forEach((b) => (b.onclick = () => {
    const n = parseInt(prompt(b.dataset.deltaAsk === "+" ? "Add how many?" : "Remove how many?"), 10);
    if (n > 0) change(b.dataset.deltaAsk === "+" ? n : -n);
  }));
  $("#del").onclick = async () => {
    if (!confirm(`Delete “${p.name}”?`)) return;
    await api(`/parts/${id}`, { method: "DELETE" });
    toast("Deleted");
    location.hash = "#/parts";
  };
  $("#bt-print") && ($("#bt-print").onclick = () => printLabels([{ kind: "part", id: p.id }]));
  $("#reserve").onclick = async () => {
    const projects = await api("/projects");
    if (!projects.length) { toast("Create a project first"); location.hash = "#/projects"; return; }
    const choice = prompt("Project number:\n" + projects.map((pr, i) => `${i + 1}: ${pr.name}`).join("\n"));
    const pr = projects[parseInt(choice, 10) - 1];
    if (!pr) return;
    const qty = parseInt(prompt("Quantity", "1"), 10);
    if (!(qty > 0)) return;
    await api(`/projects/${pr.id}/parts`, { json: { part_id: id, quantity: qty } });
    toast(`Added to ${pr.name}`);
    partView(id);
  };
}

// ------------------------------------------------------------ part form

async function partFormView(id, params) {
  const [meta, locs] = await Promise.all([getMeta(), getLocations(true)]);
  let p;
  if (id) p = await api(`/parts/${id}`);
  else {
    p = { name: "", category: "other", value: "", package: "", manufacturer: "", mpn: "", description: "", quantity: 1,
      min_quantity: 0, location_id: null, datasheet_url: "", notes: "", image: null, links: [], ...(state.draft || {}) };
    state.draft = null;
    const locParam = params?.get("location");
    if (locParam) p.location_id = +locParam;
  }
  let image = p.image;
  const linksText = p.links.map((l) => (l.label ? `${l.label} ${l.url}` : l.url)).join("\n");

  render(html`
    <h1>${id ? "Edit part" : "New part"}</h1>
    <form class="stack" id="pf">
      <div id="img-box">${image ? html`<img class="hero" src="${imgUrl(image)}" alt="">` : ""}</div>
      <div class="actions"><button type="button" id="photo">📷 ${image ? "Replace photo" : "Add photo"}</button>
        ${image ? html`<button type="button" id="rmphoto">Remove photo</button>` : ""}</div>
      <label>Name<input name="name" required value="${p.name}"></label>
      <div class="grid2">
        <label>Category<select name="category">${meta.categories.map((c) => html`<option ${c === p.category ? raw("selected") : ""}>${c}</option>`)}</select></label>
        <label>Value<input name="value" value="${p.value}" placeholder="10 kΩ, 100 nF…"></label>
      </div>
      <div class="grid2">
        <label>Quantity<input name="quantity" type="number" min="0" inputmode="numeric" value="${p.quantity}"></label>
        <label>Min. quantity<input name="min_quantity" type="number" min="0" inputmode="numeric" value="${p.min_quantity}"></label>
      </div>
      <label>Location<select name="location_id">${locationOptions(locs, p.location_id)}</select></label>
      <div class="grid2">
        <label>Package<input name="package" value="${p.package}" placeholder="0805, DIP-8…"></label>
        <label>Manufacturer<input name="manufacturer" value="${p.manufacturer}"></label>
      </div>
      <label>Part number (MPN)<input name="mpn" value="${p.mpn}"></label>
      <label>Description<textarea name="description">${p.description}</textarea></label>
      <label>Datasheet URL<input name="datasheet_url" type="url" value="${p.datasheet_url}" placeholder="https://…"></label>
      <label>Links (one per line: “Label https://…”)<textarea name="links" placeholder="Pinout https://…">${linksText}</textarea></label>
      <label>Notes<textarea name="notes">${p.notes}</textarea></label>
      <div class="actions end">
        <a class="btn" href="${id ? `#/part/${id}` : "#/"}">Cancel</a>
        <button class="primary" type="submit">Save</button>
      </div>
    </form>
  `);

  $("#photo").onclick = async () => {
    const file = await pickImage();
    if (!file) return;
    image = await uploadImage(file);
    $("#img-box").innerHTML = html`<img class="hero" src="${imgUrl(image)}" alt="">`.s;
  };
  const rm = $("#rmphoto");
  if (rm) rm.onclick = () => { image = null; $("#img-box").innerHTML = ""; rm.remove(); };

  $("#pf").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    const links = f.links.split("\n").map((l) => l.trim()).filter(Boolean).map((line) => {
      const m = line.match(/^(.*?)\s*(https?:\/\/\S+)$/);
      return m ? { label: m[1], url: m[2] } : { label: "", url: line };
    });
    const body = {
      ...f, links, image,
      quantity: parseInt(f.quantity, 10) || 0,
      min_quantity: parseInt(f.min_quantity, 10) || 0,
      location_id: f.location_id ? +f.location_id : null,
    };
    const saved = id ? await api(`/parts/${id}`, { method: "PUT", json: body }) : await api("/parts", { json: body });
    // Capturing into a location: go straight back to the camera for the next part.
    if (!id && params?.get("return") === "capture" && saved.location_id) {
      toast(`Saved – next part for ${saved.location_name || "this location"}`);
      location.hash = `#/capture?location=${saved.location_id}`;
      return;
    }
    toast("Saved");
    location.hash = `#/part/${saved.id}`;
  };
}

// ------------------------------------------------------------ capture (live view, scan, recognize)

async function captureView(params) {
  const meta = await getMeta();
  // Optional target location: parts created from this capture session are pre-assigned to it.
  let target = null;
  const targetId = +(params?.get("location") || 0);
  if (targetId) target = await api(`/locations/${targetId}`).catch(() => null);
  const newPart = () => { location.hash = "#/part/new" + (target ? `?location=${target.id}&return=capture` : ""); };
  render(html`
    <div class="card target no-print" id="target" ${target ? "" : raw("hidden")}>
      📥 Adding to <b id="target-name">${target?.path || ""}</b>
      <button id="target-clear" title="Stop adding to this location">✕</button>
    </div>
    <div class="camera" id="cam">
      <video id="video" playsinline muted autoplay></video>
      <div class="frame"></div>
      <div class="overlay" id="cam-status">Starting camera…</div>
      <button id="torch" class="torch" hidden title="Light" aria-pressed="false">🔦</button>
    </div>
    <div class="shutter-row">
      <button id="gallery" title="From gallery">🖼️</button>
      <button id="shutter" class="shutter" title="${meta.recognition ? "Take photo & identify" : "Take photo"}">📸</button>
      <button id="manual" title="Enter code">⌨️</button>
    </div>
    <label class="no-print" style="margin-bottom:12px">Hint for recognition (optional)
      <input id="hint" placeholder="e.g. bag of 100 resistors, ESP32 board…"></label>
    <p class="muted" style="text-align:center;margin:0">
      QR labels and barcodes are detected automatically in the live view.
      ${meta.recognition ? `Tap the shutter to identify components (${meta.provider}).` : "AI recognition is disabled (no API key)."}
    </p>
    <div id="result"></div>
  `);

  $("#target-clear").onclick = () => { location.hash = "#/capture"; };

  const video = $("#video");
  const status = $("#cam-status");
  let stream = null;
  let scanTimer = null;
  let torchAfterPhoto = () => {}; // set by setupTorch once the camera runs
  let lastCode = null;
  let stopped = false;

  state.cleanup = () => {
    stopped = true;
    clearTimeout(scanTimer);
    stream?.getTracks().forEach((t) => t.stop());
  };

  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("no camera API (HTTPS required)");
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1440 } },
      audio: false,
    });
    if (stopped) { stream.getTracks().forEach((t) => t.stop()); return; }
    video.srcObject = stream;
    await video.play();
    status.textContent = "Point at a part, label or QR code";
    setupTorch(stream.getVideoTracks()[0]);
  } catch (e) {
    status.textContent = `Live camera unavailable: ${e.message}. Use the shutter to take a photo.`;
    stream = null;
  }

  // Camera light (Chrome on Android). The browser offers no flash-on-capture, but photos are grabbed
  // from the live view, so a lit torch lights them. One button cycles off -> once -> always:
  // "once" switches off after the next photo, "always" stays on and is remembered.
  function setupTorch(track) {
    const btn = $("#torch");
    if (!btn || !track?.getCapabilities?.().torch) return;
    const MODES = ["off", "once", "always"];
    const LABELS = { off: "🔦", once: "🔦1×", always: "🔦∞" };
    const TITLES = { off: "Light off", once: "Light on for the next photo", always: "Light always on" };
    let mode = "off";
    const apply = async (next) => {
      try {
        await track.applyConstraints({ advanced: [{ torch: next !== "off" }] });
        mode = next;
        btn.textContent = LABELS[mode];
        btn.title = TITLES[mode];
        btn.dataset.mode = mode;
        btn.setAttribute("aria-pressed", String(mode !== "off"));
        try { localStorage.setItem("pb-torch", mode === "always" ? "always" : "off"); } catch { /* storage unavailable */ }
      } catch (e) {
        toast(`Light: ${e.message}`);
      }
    };
    btn.hidden = false;
    btn.onclick = () => apply(MODES[(MODES.indexOf(mode) + 1) % MODES.length]);
    torchAfterPhoto = () => { if (mode === "once") apply("off"); };
    let saved = "off";
    try { saved = localStorage.getItem("pb-torch") === "always" ? "always" : "off"; } catch { /* storage unavailable */ }
    if (saved === "always") apply("always");
  }

  // Local barcode/QR detection on the live stream (Chrome/Android, Safari 17+).
  if (stream && "BarcodeDetector" in window) {
    let detector;
    try {
      const supported = await BarcodeDetector.getSupportedFormats();
      const wanted = ["qr_code", "data_matrix", "ean_13", "ean_8", "code_128", "code_39", "upc_a"];
      detector = new BarcodeDetector({ formats: wanted.filter((f) => supported.includes(f)) });
    } catch { detector = null; }
    const scan = async () => {
      if (stopped || !detector) return;
      try {
        if (video.readyState >= 2) {
          const codes = await detector.detect(video);
          if (codes.length && codes[0].rawValue !== lastCode) {
            lastCode = codes[0].rawValue;
            navigator.vibrate?.(60);
            await handleCode(lastCode);
          }
        }
      } catch { /* frame not ready */ }
      scanTimer = setTimeout(scan, 350);
    };
    scan();
  }

  async function handleCode(code) {
    const r = await api(`/scan?code=${encodeURIComponent(code)}`).catch((e) => ({ error: e.message }));
    if (r.error) { toast(r.error); return; }
    if (r.kind === "location") {
      // While capturing into a location, scanning another location label switches the target.
      if (target) { location.hash = `#/capture?location=${r.id}`; return; }
      location.hash = `#/location/${r.id}`;
      return;
    }
    if (r.kind === "part") { location.hash = `#/part/${r.id}`; return; }
    $("#result").innerHTML = html`<h2>Code ${r.code}</h2><div class="card">
      ${r.parts.length ? html`<ul class="list">${r.parts.map((p) => partRow(p))}</ul>` : html`<p class="muted">No part with this code.</p>`}
      <div class="actions"><button id="code-new">＋ New part with this code</button></div></div>`.s;
    $("#code-new").onclick = () => { state.draft = { mpn: code, notes: `Barcode: ${code}` }; newPart(); };
  }

  function grabFrame() {
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
  }

  $("#shutter").onclick = async () => {
    const live = stream && video.videoWidth;
    const blob = live ? await grabFrame() : await pickImage();
    if (live) torchAfterPhoto();
    if (blob) processPhoto(blob);
  };
  $("#gallery").onclick = async () => {
    const file = await pickImage({ capture: false });
    if (file) processPhoto(file);
  };
  $("#manual").onclick = () => {
    const code = prompt("Code (QR content, barcode or MPN)");
    if (code) handleCode(code.trim());
  };

  async function processPhoto(blob) {
    const preview = URL.createObjectURL(blob);
    const result = $("#result");
    result.innerHTML = html`<div class="card"><img class="hero" src="${preview}" alt=""><div class="spinner"></div>
      <p class="muted" style="text-align:center" id="rec-status">${meta.recognition ? `Identifying with ${meta.provider}…` : "Uploading…"}</p></div>`.s;
    result.scrollIntoView({ behavior: "smooth" });

    const imagePromise = uploadImage(blob);
    if (!meta.recognition) {
      state.draft = { image: await imagePromise };
      newPart();
      return;
    }
    const fd = new FormData();
    fd.append("file", blob, "photo.jpg");
    fd.append("hint", $("#hint")?.value || "");
    let rec;
    try {
      const job = await api("/recognize", { method: "POST", body: fd });
      [rec] = await Promise.all([pollRecognition(job.job_id), imagePromise]);
    } catch (e) {
      if (stopped) return;
      const image = await imagePromise.catch(() => null);
      result.innerHTML = html`<div class="card">⚠️ ${e.message}
        <div class="actions"><button id="manual-new">Create part manually</button></div></div>`.s;
      $("#manual-new").onclick = () => { state.draft = { image }; newPart(); };
      return;
    }
    if (stopped) return;
    const image = await imagePromise;
    renderCandidates(rec, image, preview);
  }

  // Recognition runs server-side as a job; local models may need several minutes.
  async function pollRecognition(jobId) {
    const deadline = Date.now() + 20 * 60 * 1000;
    let failures = 0;
    while (!stopped && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2000));
      let job;
      try {
        job = await api(`/recognize/${jobId}`);
        failures = 0;
      } catch (e) {
        // Tolerate short network drops (screen lock, mobile handover); give up on persistent errors.
        if (/expired/.test(e.message) || ++failures >= 10) throw e;
        continue;
      }
      if (job.status === "done") return job;
      if (job.status === "error") throw new Error(job.error);
      const el = document.getElementById("rec-status");
      if (el) el.textContent = `Identifying with ${meta.provider}… ${Math.round(job.elapsed)} s`;
    }
    throw new Error(stopped ? "cancelled" : "Recognition took too long");
  }

  function renderCandidates(rec, image, preview) {
    const result = $("#result");
    if (!rec.candidates.length) {
      result.innerHTML = html`<div class="card"><img class="hero" src="${preview}" alt="">
        <p>Nothing identified. ${rec.hint}</p><div class="actions"><button id="manual-new">Create part manually</button></div></div>`.s;
      $("#manual-new").onclick = () => { state.draft = { image }; newPart(); };
      return;
    }
    result.innerHTML = html`
      <img class="hero" src="${preview}" alt="" style="margin-top:12px">
      ${rec.hint ? html`<p class="muted">💡 ${rec.hint}</p>` : ""}
      ${rec.candidates.map((c, i) => html`
        <div class="card candidate">
          <h3>${icon(c.category)} ${c.name} <span class="badge ${c.confidence === "low" ? "warn" : ""}">${c.confidence}</span></h3>
          <div class="sub">${[c.value, c.package, c.manufacturer, c.mpn].filter(Boolean).join(" · ")}</div>
          <p>${c.description}</p>
          ${c.markings ? html`<p class="muted">Markings: ${c.markings}</p>` : ""}
          <p class="muted">Pieces visible: ${c.quantity}</p>
          ${c.matches.length ? html`<p><b>Already in stock?</b></p><ul class="list">
            ${c.matches.map((m) => html`<li><div class="row">${thumb(m)}<div class="grow"><div class="title">${m.name}</div>
              <div class="sub">${m.value} · ${m.quantity} pcs · ${m.location_name || "no location"}</div></div>
              <button data-add="${m.id}" data-qty="${c.quantity}">＋${c.quantity}</button></div></li>`)}
          </ul>` : ""}
          <div class="actions"><button class="primary" data-new="${i}">＋ Create new part</button></div>
        </div>`)}
    `.s;
    $$("[data-new]", result).forEach((b) => (b.onclick = () => {
      const c = rec.candidates[+b.dataset.new];
      state.draft = {
        name: c.name, category: c.category, value: c.value, package: c.package, manufacturer: c.manufacturer,
        mpn: c.mpn, description: c.description, quantity: c.quantity, datasheet_url: c.datasheet_url,
        notes: c.markings ? `Markings: ${c.markings}` : "", image,
      };
      newPart();
    }));
    $$("[data-add]", result).forEach((b) => (b.onclick = async () => {
      const n = parseInt(prompt("Add how many?", b.dataset.qty), 10);
      if (!(n > 0)) return;
      await api(`/parts/${b.dataset.add}/stock`, { json: { delta: n, reason: "added (photo)" } });
      toast(`Added ${n}`);
      location.hash = `#/part/${b.dataset.add}`;
    }));
  }
}

// ------------------------------------------------------------ locations

async function locationsView() {
  const locs = await getLocations(true);
  const depth = (l) => l.path.split(" › ").length - 1;
  render(html`
    <h1>Storage locations</h1>
    <div class="card">${locs.length ? html`<ul class="list">
      ${locs.map((l) => html`<li><a href="#/location/${l.id}" style="padding-left:${depth(l) * 18}px">🗄️
        <span class="grow"><span class="title">${l.name}</span><span class="sub">${l.description}</span></span>
        <span class="badge">${l.part_count}</span></a></li>`)}
    </ul>` : html`<p class="empty">No locations yet. Add your first box, drawer or shelf.</p>`}</div>
    <div class="actions">
      <button class="primary" id="add">＋ New location</button>
      ${locs.length ? html`<a class="btn" href="#/labels">🏷️ Print labels</a>` : ""}
    </div>
  `);
  $("#add").onclick = () => editLocation(null, null);
}

async function editLocation(loc, parentId) {
  const locs = await getLocations();
  const name = prompt("Name (e.g. 'Box A', 'Drawer 3', 'Compartment 12')", loc?.name || "");
  if (!name) return;
  const description = prompt("Description (optional)", loc?.description || "") ?? "";
  let parent = loc ? loc.parent_id : parentId;
  if (!parentId && locs.length) {
    const choice = prompt("Inside which location? Number or empty for top level:\n" +
      locs.filter((l) => l.id !== loc?.id).map((l) => `${l.id}: ${l.path}`).join("\n"), parent ?? "");
    parent = choice ? +choice : null;
  }
  const body = { name, description, parent_id: parent };
  const saved = loc ? await api(`/locations/${loc.id}`, { method: "PUT", json: body }) : await api("/locations", { json: body });
  invalidateLocations();
  const target = `#/location/${saved.id}`;
  if (location.hash === target) router();
  else location.hash = target;
}

async function locationView(id) {
  const loc = await api(`/locations/${id}`);
  render(html`
    <h1>🗄️ ${loc.path}</h1>
    ${loc.description ? html`<p class="muted">${loc.description}</p>` : ""}
    ${loc.children.length ? html`<h2>Contains</h2><div class="card"><ul class="list">
      ${loc.children.map((c) => html`<li><a href="#/location/${c.id}">🗄️ <span class="grow">${c.name}</span></a></li>`)}
    </ul></div>` : ""}
    <h2>Parts (${loc.parts.length})</h2>
    <div class="card">${loc.parts.length ? html`<ul class="list">${loc.parts.map((p) => partRow(p))}</ul>` : html`<p class="empty">Empty</p>`}</div>
    <div class="actions">
      <a class="btn primary" href="#/capture?location=${id}">📷 Capture here</a>
      <a class="btn" href="#/part/new?location=${id}">＋ Part here</a>
      <button id="sub">＋ Sub-location</button>
      <button id="edit">✏️ Edit</button>
      <button id="del" class="danger">🗑️</button>
    </div>
    <h2>Label</h2>
    <div class="card"><div class="label"><img src="/api/qr/location/${id}.svg" alt="QR"><div><b>${loc.name}</b><small>${loc.path}</small></div></div>
      <div class="actions no-print"><button id="print">🖨️ Print</button>${printerButton("bt-print")}</div></div>
  `);
  $("#bt-print") && ($("#bt-print").onclick = () => printLabels([{ kind: "location", id }]));
  $("#sub").onclick = () => editLocation(null, id);
  $("#edit").onclick = () => editLocation(loc, null);
  $("#print").onclick = () => window.print();
  $("#del").onclick = async () => {
    if (!confirm(`Delete “${loc.name}”? Parts stay but lose their location.`)) return;
    await api(`/locations/${id}`, { method: "DELETE" });
    invalidateLocations();
    location.hash = "#/locations";
  };
}

async function labelsView() {
  const locs = await getLocations(true);
  render(html`
    <div class="no-print"><h1>Labels</h1><p class="muted">Untick what you don't need, then print.</p>
      <div class="actions"><button class="primary" id="print">🖨️ Print</button>
        ${printerButton("bt-print", "🔵 Print selected on label printer")}
        <a class="btn" href="#/printer">⚙️ Label printer</a></div></div>
    <div id="labels" style="display:flex;flex-wrap:wrap;gap:8px">
      ${locs.map((l) => html`<label class="label" style="cursor:pointer"><input type="checkbox" class="no-print" checked style="width:auto" data-id="${l.id}">
        <img src="/api/qr/location/${l.id}.svg" alt=""><div><b>${l.name}</b><small>${l.path}</small></div></label>`)}
    </div>
  `);
  $$("#labels input").forEach((cb) => (cb.onchange = () => cb.parentElement.classList.toggle("no-print", !cb.checked)));
  $("#print").onclick = () => window.print();
  $("#bt-print") && ($("#bt-print").onclick = () => {
    const items = $$("#labels input:checked").map((cb) => ({ kind: "location", id: +cb.dataset.id }));
    if (items.length) printLabels(items);
  });
}

// ------------------------------------------------------------ label printer (Web Bluetooth)

// The connection outlives page changes so labels can be printed from any view.
const printerState = { printer: null, material: null, dpm: 8, log: [], onlog: null };
const PRINT_DEFAULTS = { widthMm: 12, lengthMm: 40, density: 4, flip: false };

function printSettings() {
  try { return { ...PRINT_DEFAULTS, ...JSON.parse(localStorage.getItem("pb-printer") || "{}") }; }
  catch { return { ...PRINT_DEFAULTS }; }
}
function savePrintSettings(settings) {
  try { localStorage.setItem("pb-printer", JSON.stringify(settings)); } catch { /* storage unavailable */ }
}
function plog(line) { printerState.log.push(line); printerState.onlog?.(); }

const printerButton = (id, text = "🔵 Print on label printer") =>
  (SupvanPrinter.isSupported() ? html`<button id="${id}">${text}</button>` : "");

/** Connect (or reuse the connection). Must be started from a click: the device chooser needs a user gesture. */
async function connectPrinter(showAll = false) {
  if (printerState.printer?.connected) return printerState.printer;
  plog(`Requesting device${showAll ? " (all devices)" : ""}…`);
  const r = await SupvanPrinter.connect({ showAll });
  const dev = r.printer?.device || r.device;
  plog(`Connected: ${dev.name || "(no name)"}`);
  for (const svc of r.inventory) {
    plog(`service ${svc.uuid}`);
    for (const c of svc.chars) plog(`  char ${c.uuid} [${c.props.join(", ")}]`);
  }
  if (!r.printer) {
    r.server.disconnect();
    throw new Error("No known SUPVAN service on this device");
  }
  const printer = r.printer;
  dev.addEventListener("gattserverdisconnected", () => {
    plog("Disconnected.");
    if (printerState.printer === printer) printerState.printer = null;
    printerState.onlog?.();
  });
  printerState.printer = printer;
  const name = await printer.command(CMD.RD_DEV_NAME);
  if (name) plog(`Model: ${String.fromCharCode(...name.slice(22).filter((b) => b >= 32 && b < 127))}`);
  printerState.material = await printer.material().catch(() => null);
  const m = printerState.material;
  if (m) plog(`Label roll: ${m.widthMm} × ${m.lengthMm} mm, gap ${m.gapMm} mm, type ${m.type}`);
  printerState.dpm = await printer.dotsPerMm().catch(() => 8);
  plog(`Resolution: ${printerState.dpm} dots/mm`);
  const st = await printer.status();
  if (st) plog(`Status: ${st.summary}`);
  return printer;
}

function jobUrl(kind, id, s = printSettings()) {
  return `/printer/supvan/${kind}/${id}?width_mm=${s.widthMm}&length_mm=${s.lengthMm}&density=${s.density}`
    + `&flip=${!!s.flip}&dpm=${printerState.dpm}`;
}

/** Print labels for [{kind, id}] one after another. */
async function printLabels(items) {
  let printer;
  try { printer = await connectPrinter(); }
  catch (e) { if (e.name !== "NotFoundError") toast(e.message); return; }
  for (const [i, it] of items.entries()) {
    plog(`Printing ${it.kind} ${it.id} (${i + 1}/${items.length})`);
    toast(`Printing ${i + 1}/${items.length}…`);
    try {
      const job = await api(jobUrl(it.kind, it.id));
      plog(`job: ${job.cols} columns × ${job.bytes_per_line} bytes/line, margins ${job.margins.join("/")}, ${job.blocks.length} block(s)`);
      await printer.print(job, plog);
    } catch (e) {
      plog(`Error: ${e.message}`);
      toast(`Print failed: ${e.message}`);
      return;
    }
  }
  toast(items.length > 1 ? `Printed ${items.length} labels` : "Printed");
}

async function printerView() {
  const locs = await getLocations(true);
  const s = printSettings();
  render(html`
    <h1>🔵 Label printer</h1>
    <p class="muted">SUPVAN / Katasymbol Bluetooth label printers (E11 …), via Chrome on Android or desktop.
      Printing is experimental: the protocol is reverse-engineered.</p>
    ${SupvanPrinter.isSupported() ? "" : html`<p class="error">Web Bluetooth is not available in this browser.</p>`}
    <div class="actions">
      <button class="primary" id="connect">Connect printer</button>
      <button id="connect-all">Show all devices</button>
      <button id="status">Status</button>
      <button id="disconnect">Disconnect</button>
    </div>
    <h2>Label settings</h2>
    <form class="stack card" id="ps">
      <div class="grid2">
        <label>Label width (mm)<input name="widthMm" type="number" min="5" max="25" step="0.5" value="${s.widthMm}"></label>
        <label>Label length (mm)<input name="lengthMm" type="number" min="10" max="100" step="1" value="${s.lengthMm}"></label>
        <label>Density (0–15)<input name="density" type="number" min="0" max="15" value="${s.density}"></label>
        <label>Orientation<select name="flip">
          <option value="0" ${s.flip ? "" : raw("selected")}>normal</option>
          <option value="1" ${s.flip ? raw("selected") : ""}>upside down</option></select></label>
      </div>
      <p class="muted" id="mat"></p>
      <label>Test label<select name="loc">${locs.map((l) => html`<option value="${l.id}">${l.path}</option>`)}</select></label>
      <img id="preview" alt="Label preview" style="image-rendering:pixelated;border:1px dashed var(--border);background:#fff;max-width:100%">
      <div class="actions"><button type="button" class="primary" id="test" ${locs.length ? "" : raw("disabled")}>🖨️ Print test label</button></div>
    </form>
    <h2>Log</h2>
    <div class="actions"><button id="copy">📋 Copy log</button><button id="clear">Clear</button></div>
    <pre class="card" id="pr-log" style="white-space:pre-wrap;font-size:12px;min-height:3em"></pre>
  `);
  const form = $("#ps");
  const logEl = $("#pr-log");
  const showState = () => {
    logEl.textContent = printerState.log.join("\n");
    const m = printerState.material;
    $("#mat").textContent = m ? `Printer reports a ${m.widthMm} × ${m.lengthMm} mm label roll.` : "";
    $("#status").disabled = $("#disconnect").disabled = !printerState.printer?.connected;
  };
  printerState.onlog = showState;
  state.cleanup = () => { printerState.onlog = null; };
  const readForm = () => {
    const f = Object.fromEntries(new FormData(form));
    return { widthMm: +f.widthMm || 12, lengthMm: +f.lengthMm || 40, density: +f.density || 0, flip: f.flip === "1" };
  };
  const updatePreview = () => {
    const cur = readForm();
    savePrintSettings(cur);
    if (form.loc.value) {
      $("#preview").src = `/api/printer/supvan/location/${form.loc.value}.png?width_mm=${cur.widthMm}&length_mm=${cur.lengthMm}&dpm=${printerState.dpm}`;
    }
  };
  form.onchange = updatePreview;
  updatePreview();
  showState();

  const connect = async (showAll) => {
    try { await connectPrinter(showAll); }
    catch (e) { plog(`Error: ${e.message}`); }
  };
  $("#connect").onclick = () => connect(false);
  $("#connect-all").onclick = () => connect(true);
  $("#disconnect").onclick = () => printerState.printer?.disconnect();
  $("#status").onclick = async () => {
    const st = await printerState.printer?.status();
    plog(st ? `Status: ${st.summary}` : "Status: no answer");
  };
  $("#test").onclick = () => printLabels([{ kind: "location", id: +form.loc.value }]);
  $("#clear").onclick = () => { printerState.log.length = 0; showState(); };
  $("#copy").onclick = async () => {
    const text = printerState.log.join("\n");
    try { await navigator.clipboard.writeText(text); toast("Copied"); }
    catch { prompt("Copy this log:", text); }
  };
}

// ------------------------------------------------------------ projects

const STATUSES = ["planned", "active", "done", "archived"];

async function projectsView() {
  const projects = await api("/projects");
  render(html`
    <h1>Projects</h1>
    <div class="card">${projects.length ? html`<ul class="list">
      ${projects.map((p) => html`<li><a href="#/project/${p.id}">🛠️ <span class="grow"><span class="title">${p.name}</span>
        <span class="sub">${p.part_count} parts</span></span><span class="badge">${p.status}</span></a></li>`)}
    </ul>` : html`<p class="empty">No projects yet.</p>`}</div>
    <div class="actions"><button class="primary" id="add">＋ New project</button></div>
  `);
  $("#add").onclick = async () => {
    const name = prompt("Project name");
    if (!name) return;
    const p = await api("/projects", { json: { name } });
    location.hash = `#/project/${p.id}`;
  };
}

async function projectView(id) {
  const pr = await api(`/projects/${id}`);
  const missing = pr.parts.filter((p) => !p.consumed && p.in_stock < p.needed);
  render(html`
    <h1>🛠️ ${pr.name}</h1>
    <form class="stack card" id="pf">
      <label>Name<input name="name" value="${pr.name}" required></label>
      <label>Status<select name="status">${STATUSES.map((s) => html`<option ${s === pr.status ? raw("selected") : ""}>${s}</option>`)}</select></label>
      <label>Description<textarea name="description">${pr.description}</textarea></label>
      <div class="actions end"><button id="del" type="button" class="danger">🗑️ Delete</button><button class="primary">Save</button></div>
    </form>
    <h2>Parts ${missing.length ? html`<span class="badge warn">${missing.length} short</span>` : ""}</h2>
    <div class="card">${pr.parts.length ? html`<ul class="list">
      ${pr.parts.map((p) => html`<li><div class="row">${thumb(p)}
        <a class="grow" href="#/part/${p.id}" style="display:block;padding:0"><div class="title">${p.name}</div>
          <div class="sub">${p.value} · need ${p.needed} · stock ${p.in_stock} · ${p.location_name || "no location"}</div></a>
        ${p.consumed ? html`<span class="badge">used</span>` : html`
          ${p.in_stock < p.needed ? html`<span class="badge warn">short</span>` : ""}
          <button data-consume="${p.entry_id}" title="Take from stock">✔︎</button>`}
        <button data-remove="${p.entry_id}" title="Remove">✕</button>
      </div></li>`)}
    </ul>` : html`<p class="empty">No parts assigned.</p>`}</div>
    <h2>Add part</h2>
    <div class="card"><input id="ps" type="search" placeholder="Search inventory…"><ul class="list" id="ps-res"></ul></div>
  `);

  $("#pf").onsubmit = async (e) => {
    e.preventDefault();
    await api(`/projects/${id}`, { method: "PUT", json: Object.fromEntries(new FormData(e.target)) });
    toast("Saved");
  };
  $("#del").onclick = async () => {
    if (!confirm("Delete project? Stock is not changed.")) return;
    await api(`/projects/${id}`, { method: "DELETE" });
    location.hash = "#/projects";
  };
  $$("[data-consume]").forEach((b) => (b.onclick = async () => {
    await api(`/projects/${id}/parts/${b.dataset.consume}/consume`, { method: "POST" });
    toast("Taken from stock");
    projectView(id);
  }));
  $$("[data-remove]").forEach((b) => (b.onclick = async () => {
    await api(`/projects/${id}/parts/${b.dataset.remove}`, { method: "DELETE" });
    projectView(id);
  }));
  let t;
  $("#ps").oninput = (e) => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = e.target.value.trim();
      if (!q) { $("#ps-res").innerHTML = ""; return; }
      const res = await api(`/parts?q=${encodeURIComponent(q)}&limit=15`);
      $("#ps-res").innerHTML = res.map((p) => html`<li><div class="row">${thumb(p)}<div class="grow"><div class="title">${p.name}</div>
        <div class="sub">${p.value} · ${p.quantity} in stock</div></div><button data-pick="${p.id}">＋</button></div></li>`.s).join("");
      $$("[data-pick]").forEach((b) => (b.onclick = async () => {
        const qty = parseInt(prompt("Quantity needed", "1"), 10);
        if (!(qty > 0)) return;
        await api(`/projects/${id}/parts`, { json: { part_id: +b.dataset.pick, quantity: qty } });
        projectView(id);
      }));
    }, 250);
  };
}

// ------------------------------------------------------------ boot

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
router();

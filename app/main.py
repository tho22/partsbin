"""Partsbin: mobile inventory for electronic components."""

import asyncio
import io
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import segno
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import recognize as rec
from . import supvan
from .db import CATEGORIES, IMAGE_DIR, connect, init_db, row, rows

logging.basicConfig(level=os.environ.get("PARTSBIN_LOG_LEVEL", "INFO"))

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD = 15 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

# QR payloads printed on labels. Short and stable so labels never need reprinting.
QR_PREFIX_LOCATION = "PB:L:"
QR_PREFIX_PART = "PB:P:"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Partsbin", lifespan=lifespan)


# ---------------------------------------------------------------- models


class LocationIn(BaseModel):
    name: str = Field(min_length=1)
    parent_id: int | None = None
    description: str = ""


class LinkIn(BaseModel):
    label: str = ""
    url: str = Field(min_length=1)


class PartIn(BaseModel):
    name: str = Field(min_length=1)
    category: str = "other"
    value: str = ""
    package: str = ""
    manufacturer: str = ""
    mpn: str = ""
    description: str = ""
    quantity: int = Field(default=0, ge=0)
    min_quantity: int = Field(default=0, ge=0)
    location_id: int | None = None
    datasheet_url: str = ""
    notes: str = ""
    image: str | None = None
    links: list[LinkIn] = []


class StockChange(BaseModel):
    delta: int
    reason: str = ""


class ProjectIn(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    status: str = "planned"


class ProjectPartIn(BaseModel):
    part_id: int
    quantity: int = Field(default=1, ge=1)


PART_FIELDS = [
    "name", "category", "value", "package", "manufacturer", "mpn", "description",
    "quantity", "min_quantity", "location_id", "datasheet_url", "notes", "image",
]

PART_SELECT = """
SELECT p.*, l.name AS location_name,
       COALESCE((SELECT SUM(pp.quantity) FROM project_parts pp
                 WHERE pp.part_id = p.id AND pp.consumed = 0), 0) AS reserved
FROM parts p LEFT JOIN locations l ON l.id = p.location_id
"""


# ---------------------------------------------------------------- meta


@app.get("/api/meta")
def meta():
    return {"categories": CATEGORIES, "recognition": rec.is_enabled(), "provider": rec.provider_label()}


@app.get("/api/stats")
def stats():
    with connect() as c:
        totals = row(c.execute("SELECT COUNT(*) AS parts, COALESCE(SUM(quantity),0) AS pieces FROM parts"))
        low = row(c.execute("SELECT COUNT(*) AS n FROM parts WHERE min_quantity > 0 AND quantity <= min_quantity"))
        by_cat = rows(c.execute("SELECT category, COUNT(*) AS n FROM parts GROUP BY category ORDER BY n DESC"))
    return {**totals, "low_stock": low["n"], "by_category": by_cat}


# ---------------------------------------------------------------- locations


def _location_path(c, loc_id: int | None) -> str:
    parts, seen = [], set()
    while loc_id and loc_id not in seen:
        seen.add(loc_id)
        r = row(c.execute("SELECT name, parent_id FROM locations WHERE id=?", (loc_id,)))
        if not r:
            break
        parts.append(r["name"])
        loc_id = r["parent_id"]
    return " › ".join(reversed(parts))


@app.get("/api/locations")
def list_locations():
    with connect() as c:
        locs = rows(c.execute(
            "SELECT l.*, (SELECT COUNT(*) FROM parts p WHERE p.location_id=l.id) AS part_count FROM locations l ORDER BY name"
        ))
        for loc in locs:
            loc["path"] = _location_path(c, loc["id"])
    locs.sort(key=lambda l: l["path"].lower())
    return locs


@app.post("/api/locations", status_code=201)
def create_location(data: LocationIn):
    with connect() as c:
        cur = c.execute("INSERT INTO locations(name, parent_id, description) VALUES (?,?,?)",
                        (data.name, data.parent_id, data.description))
        return get_location(cur.lastrowid, c)


@app.get("/api/locations/{loc_id}")
def get_location(loc_id: int, c=None):
    def _get(c):
        loc = row(c.execute("SELECT * FROM locations WHERE id=?", (loc_id,)))
        if not loc:
            raise HTTPException(404, "Location not found")
        loc["path"] = _location_path(c, loc_id)
        loc["children"] = rows(c.execute("SELECT id, name FROM locations WHERE parent_id=? ORDER BY name", (loc_id,)))
        loc["parts"] = rows(c.execute(PART_SELECT + " WHERE p.location_id=? ORDER BY p.name", (loc_id,)))
        return loc
    if c is not None:
        return _get(c)
    with connect() as c:
        return _get(c)


@app.put("/api/locations/{loc_id}")
def update_location(loc_id: int, data: LocationIn):
    if data.parent_id == loc_id:
        raise HTTPException(400, "A location cannot be its own parent")
    with connect() as c:
        if data.parent_id and loc_id in _ancestors(c, data.parent_id):
            raise HTTPException(400, "This would create a cycle")
        cur = c.execute("UPDATE locations SET name=?, parent_id=?, description=? WHERE id=?",
                        (data.name, data.parent_id, data.description, loc_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Location not found")
        return get_location(loc_id, c)


def _ancestors(c, loc_id: int) -> set[int]:
    seen = set()
    while loc_id and loc_id not in seen:
        seen.add(loc_id)
        r = row(c.execute("SELECT parent_id FROM locations WHERE id=?", (loc_id,)))
        loc_id = r["parent_id"] if r else None
    return seen


@app.delete("/api/locations/{loc_id}", status_code=204)
def delete_location(loc_id: int):
    with connect() as c:
        c.execute("DELETE FROM locations WHERE id=?", (loc_id,))


# ---------------------------------------------------------------- parts


def _load_part(c, part_id: int) -> dict:
    part = row(c.execute(PART_SELECT + " WHERE p.id=?", (part_id,)))
    if not part:
        raise HTTPException(404, "Part not found")
    part["location_path"] = _location_path(c, part["location_id"])
    part["links"] = rows(c.execute("SELECT id, label, url FROM part_links WHERE part_id=? ORDER BY id", (part_id,)))
    part["projects"] = rows(c.execute(
        "SELECT pr.id, pr.name, pr.status, pp.quantity, pp.consumed FROM project_parts pp "
        "JOIN projects pr ON pr.id = pp.project_id WHERE pp.part_id=? ORDER BY pr.name", (part_id,)))
    part["history"] = rows(c.execute(
        "SELECT delta, reason, created_at FROM stock_log WHERE part_id=? ORDER BY id DESC LIMIT 20", (part_id,)))
    return part


@app.get("/api/parts")
def list_parts(
    q: str = "",
    category: str = "",
    location_id: int | None = None,
    low_stock: bool = False,
    limit: int = Query(200, le=1000),
):
    sql, args = PART_SELECT + " WHERE 1=1", []
    if q:
        like = f"%{q}%"
        sql += (" AND (p.name LIKE ? OR p.value LIKE ? OR p.mpn LIKE ? OR p.manufacturer LIKE ?"
                " OR p.description LIKE ? OR p.package LIKE ? OR p.notes LIKE ?)")
        args += [like] * 7
    if category:
        sql += " AND p.category = ?"
        args.append(category)
    if location_id is not None:
        sql += " AND p.location_id = ?"
        args.append(location_id)
    if low_stock:
        sql += " AND p.min_quantity > 0 AND p.quantity <= p.min_quantity"
    sql += " ORDER BY p.updated_at DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return rows(c.execute(sql, args))


@app.post("/api/parts", status_code=201)
def create_part(data: PartIn):
    with connect() as c:
        values = [getattr(data, f) for f in PART_FIELDS]
        cur = c.execute(
            f"INSERT INTO parts({','.join(PART_FIELDS)}) VALUES ({','.join('?' * len(PART_FIELDS))})", values)
        part_id = cur.lastrowid
        _save_links(c, part_id, data.links)
        if data.quantity:
            c.execute("INSERT INTO stock_log(part_id, delta, reason) VALUES (?,?,?)", (part_id, data.quantity, "initial"))
        return _load_part(c, part_id)


@app.get("/api/parts/{part_id}")
def get_part(part_id: int):
    with connect() as c:
        return _load_part(c, part_id)


@app.put("/api/parts/{part_id}")
def update_part(part_id: int, data: PartIn):
    with connect() as c:
        old = row(c.execute("SELECT quantity, image FROM parts WHERE id=?", (part_id,)))
        if not old:
            raise HTTPException(404, "Part not found")
        sets = ",".join(f"{f}=?" for f in PART_FIELDS)
        c.execute(f"UPDATE parts SET {sets}, updated_at=datetime('now') WHERE id=?",
                  [getattr(data, f) for f in PART_FIELDS] + [part_id])
        c.execute("DELETE FROM part_links WHERE part_id=?", (part_id,))
        _save_links(c, part_id, data.links)
        if data.quantity != old["quantity"]:
            c.execute("INSERT INTO stock_log(part_id, delta, reason) VALUES (?,?,?)",
                      (part_id, data.quantity - old["quantity"], "edit"))
        if old["image"] and old["image"] != data.image:
            _delete_image(old["image"])
        return _load_part(c, part_id)


@app.delete("/api/parts/{part_id}", status_code=204)
def delete_part(part_id: int):
    with connect() as c:
        old = row(c.execute("SELECT image FROM parts WHERE id=?", (part_id,)))
        c.execute("DELETE FROM parts WHERE id=?", (part_id,))
    if old and old["image"]:
        _delete_image(old["image"])


@app.post("/api/parts/{part_id}/stock")
def change_stock(part_id: int, data: StockChange):
    with connect() as c:
        cur = c.execute(
            "UPDATE parts SET quantity = MAX(0, quantity + ?), updated_at=datetime('now') WHERE id=?",
            (data.delta, part_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Part not found")
        c.execute("INSERT INTO stock_log(part_id, delta, reason) VALUES (?,?,?)", (part_id, data.delta, data.reason))
        return _load_part(c, part_id)


def _save_links(c, part_id: int, links: list[LinkIn]) -> None:
    for link in links:
        c.execute("INSERT INTO part_links(part_id, label, url) VALUES (?,?,?)", (part_id, link.label, link.url))


# ---------------------------------------------------------------- images & recognition


async def _read_image(file: UploadFile) -> bytes:
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(415, "Only JPEG, PNG and WebP images are supported")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image too large")
    return data


@app.post("/api/images", status_code=201)
async def upload_image(file: UploadFile = File(...)):
    data = await _read_image(file)
    name = uuid.uuid4().hex + ALLOWED_IMAGE_TYPES[file.content_type]
    (IMAGE_DIR / name).write_bytes(data)
    return {"image": name}


@app.get("/api/images/{name}")
def get_image(name: str):
    path = (IMAGE_DIR / name).resolve()
    if path.parent != IMAGE_DIR or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _delete_image(name: str) -> None:
    path = (IMAGE_DIR / name).resolve()
    if path.parent == IMAGE_DIR:
        path.unlink(missing_ok=True)


# Recognition runs as a background job: local models can take minutes, longer than reverse proxies
# (and phones with locked screens) keep a request open. The client polls the job instead.
RECOGNITION_JOB_TTL = 3600
_jobs: dict[str, dict] = {}
_job_tasks: set[asyncio.Task] = set()


async def _run_recognition(job_id: str, data: bytes, hint: str) -> None:
    job = _jobs[job_id]
    try:
        # The provider calls are blocking; run them in the threadpool.
        job["result"] = await run_in_threadpool(rec.recognize, data, hint)
        job["status"] = "done"
    except rec.RecognitionError as e:
        job["status"], job["error"] = "error", str(e)
    except Exception:
        logging.getLogger(__name__).exception("recognition job %s failed", job_id)
        job["status"], job["error"] = "error", "Unexpected error during recognition"
    job["finished"] = time.monotonic()


@app.post("/api/recognize", status_code=202)
async def start_recognition(file: UploadFile = File(...), hint: str = Form("")):
    data = await _read_image(file)
    now = time.monotonic()
    for old_id in [k for k, j in _jobs.items() if now - j["started"] > RECOGNITION_JOB_TTL]:
        del _jobs[old_id]
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {"status": "pending", "started": now}
    task = asyncio.create_task(_run_recognition(job_id, data, hint))
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)
    return {"job_id": job_id, "provider": rec.provider_label()}


@app.get("/api/recognize/{job_id}")
def recognition_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown or expired recognition job")
    elapsed = round((job.get("finished") or time.monotonic()) - job["started"], 1)
    if job["status"] == "pending":
        return {"status": "pending", "elapsed": elapsed}
    if job["status"] == "error":
        return {"status": "error", "elapsed": elapsed, "error": job["error"]}
    # Matches are computed on read so parts created meanwhile are taken into account.
    result = job["result"]
    with connect() as c:
        candidates = [{**cand.model_dump(), "matches": _find_matches(c, cand)} for cand in result.candidates]
    return {"status": "done", "elapsed": elapsed, "candidates": candidates, "hint": result.hint}


def _find_matches(c, cand: "rec.Candidate") -> list[dict]:
    """Existing parts that probably are the same, so the user can add stock instead of duplicating."""
    clauses, args = [], []
    if cand.mpn:
        clauses.append("(p.mpn <> '' AND (p.mpn LIKE ? OR ? LIKE '%' || p.mpn || '%'))")
        args += [f"%{cand.mpn}%", cand.mpn]
    if cand.value and cand.category:
        clauses.append("(p.category = ? AND p.value = ? AND p.value <> '')")
        args += [cand.category, cand.value]
    clauses.append("p.name LIKE ?")
    args.append(f"%{cand.name}%")
    return rows(c.execute(PART_SELECT + f" WHERE {' OR '.join(clauses)} LIMIT 5", args))


# ---------------------------------------------------------------- QR codes & scanning


@app.get("/api/qr/{kind}/{obj_id}.svg")
def qr_svg(kind: str, obj_id: int):
    prefix = {"location": QR_PREFIX_LOCATION, "part": QR_PREFIX_PART}.get(kind)
    if not prefix:
        raise HTTPException(404)
    # Force a regular QR code: segno would pick Micro QR for short payloads, which phone cameras
    # and the browser BarcodeDetector cannot read.
    qr = segno.make_qr(f"{prefix}{obj_id}", error="m")
    buf = io.BytesIO()
    # Standalone SVG (with xmlns, needed for <img>) that scales via its viewBox; 2-module quiet zone.
    qr.save(buf, kind="svg", scale=4, border=2, xmldecl=False, svgclass=None, lineclass=None, omitsize=True)
    return Response(buf.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


def _label_content(kind: str, obj_id: int) -> tuple[str, str, str]:
    """QR payload, title and subtitle for a printed label."""
    with connect() as c:
        if kind == "location":
            loc = row(c.execute("SELECT * FROM locations WHERE id=?", (obj_id,)))
            if not loc:
                raise HTTPException(404, "Location not found")
            return f"{QR_PREFIX_LOCATION}{obj_id}", loc["name"], _location_path(c, obj_id)
        if kind == "part":
            part = row(c.execute("SELECT * FROM parts WHERE id=?", (obj_id,)))
            if not part:
                raise HTTPException(404, "Part not found")
            sub = " ".join(x for x in (part["value"], part["package"]) if x) or part["mpn"] or ""
            return f"{QR_PREFIX_PART}{obj_id}", part["name"], sub
    raise HTTPException(404)


def _render_label(kind: str, obj_id: int, width_mm: float, length_mm: float, dpm: float):
    payload, title, subtitle = _label_content(kind, obj_id)
    return supvan.render_label(payload, title, subtitle, width_mm, length_mm, dpm)


@app.get("/api/printer/supvan/{kind}/{obj_id}.png")
def supvan_preview(kind: str, obj_id: int, width_mm: float = Query(12, ge=5, le=25),
                   length_mm: float = Query(40, ge=10, le=100), dpm: float = Query(8, ge=6, le=9)):
    """Preview of the printable area exactly as it will be printed (1 pixel = 1 printer dot)."""
    buf = io.BytesIO()
    _render_label(kind, obj_id, width_mm, length_mm, dpm).save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/api/printer/supvan/{kind}/{obj_id}")
def supvan_job(kind: str, obj_id: int, width_mm: float = Query(12, ge=5, le=25),
               length_mm: float = Query(40, ge=10, le=100), density: int = Query(4, ge=0, le=15),
               flip: bool = False, dpm: float = Query(8, ge=6, le=9)):
    """Printer-ready data for the SUPVAN E11; the client relays it over BLE.

    `dpm` is the printer's resolution in dots per mm as reported by command 0x22.
    """
    label = _render_label(kind, obj_id, width_mm, length_mm, dpm)
    return supvan.print_job(label, length_mm, density=density, flip=flip, dots_per_mm=dpm)


@app.get("/api/scan")
def resolve_scan(code: str):
    """Resolve a scanned barcode/QR payload to an inventory object."""
    code = code.strip()
    with connect() as c:
        for prefix, kind, table in ((QR_PREFIX_LOCATION, "location", "locations"), (QR_PREFIX_PART, "part", "parts")):
            if code.startswith(prefix) and code[len(prefix):].isdigit():
                obj_id = int(code[len(prefix):])
                if row(c.execute(f"SELECT id FROM {table} WHERE id=?", (obj_id,))):
                    return {"kind": kind, "id": obj_id}
                raise HTTPException(404, f"Unknown {kind} {obj_id}")
        # Foreign barcode (e.g. distributor bag label): search by MPN / notes.
        hits = rows(c.execute(PART_SELECT + " WHERE p.mpn = ? OR p.notes LIKE ? LIMIT 10", (code, f"%{code}%")))
    return {"kind": "search", "code": code, "parts": hits}


# ---------------------------------------------------------------- projects


@app.get("/api/projects")
def list_projects():
    with connect() as c:
        return rows(c.execute(
            "SELECT pr.*, (SELECT COUNT(*) FROM project_parts pp WHERE pp.project_id = pr.id) AS part_count "
            "FROM projects pr ORDER BY pr.created_at DESC"))


@app.post("/api/projects", status_code=201)
def create_project(data: ProjectIn):
    with connect() as c:
        cur = c.execute("INSERT INTO projects(name, description, status) VALUES (?,?,?)",
                        (data.name, data.description, data.status))
        return _load_project(c, cur.lastrowid)


def _load_project(c, project_id: int) -> dict:
    pr = row(c.execute("SELECT * FROM projects WHERE id=?", (project_id,)))
    if not pr:
        raise HTTPException(404, "Project not found")
    pr["parts"] = rows(c.execute(
        "SELECT pp.id AS entry_id, pp.quantity AS needed, pp.consumed, p.id, p.name, p.value, p.category, "
        "p.quantity AS in_stock, p.image, l.name AS location_name "
        "FROM project_parts pp JOIN parts p ON p.id = pp.part_id LEFT JOIN locations l ON l.id = p.location_id "
        "WHERE pp.project_id=? ORDER BY p.name", (project_id,)))
    return pr


@app.get("/api/projects/{project_id}")
def get_project(project_id: int):
    with connect() as c:
        return _load_project(c, project_id)


@app.put("/api/projects/{project_id}")
def update_project(project_id: int, data: ProjectIn):
    with connect() as c:
        c.execute("UPDATE projects SET name=?, description=?, status=? WHERE id=?",
                  (data.name, data.description, data.status, project_id))
        return _load_project(c, project_id)


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: int):
    with connect() as c:
        c.execute("DELETE FROM projects WHERE id=?", (project_id,))


@app.post("/api/projects/{project_id}/parts")
def add_project_part(project_id: int, data: ProjectPartIn):
    with connect() as c:
        c.execute(
            "INSERT INTO project_parts(project_id, part_id, quantity) VALUES (?,?,?) "
            "ON CONFLICT(project_id, part_id) DO UPDATE SET quantity = excluded.quantity",
            (project_id, data.part_id, data.quantity))
        return _load_project(c, project_id)


@app.delete("/api/projects/{project_id}/parts/{entry_id}")
def remove_project_part(project_id: int, entry_id: int):
    with connect() as c:
        c.execute("DELETE FROM project_parts WHERE id=? AND project_id=?", (entry_id, project_id))
        return _load_project(c, project_id)


@app.post("/api/projects/{project_id}/parts/{entry_id}/consume")
def consume_project_part(project_id: int, entry_id: int):
    """Take the reserved quantity out of stock and mark the entry as consumed."""
    with connect() as c:
        entry = row(c.execute("SELECT * FROM project_parts WHERE id=? AND project_id=?", (entry_id, project_id)))
        if not entry:
            raise HTTPException(404, "Entry not found")
        if not entry["consumed"]:
            pr = row(c.execute("SELECT name FROM projects WHERE id=?", (project_id,)))
            c.execute("UPDATE parts SET quantity = MAX(0, quantity - ?), updated_at=datetime('now') WHERE id=?",
                      (entry["quantity"], entry["part_id"]))
            c.execute("INSERT INTO stock_log(part_id, delta, reason) VALUES (?,?,?)",
                      (entry["part_id"], -entry["quantity"], f"project: {pr['name']}"))
            c.execute("UPDATE project_parts SET consumed=1 WHERE id=?", (entry_id,))
        return _load_project(c, project_id)


# ---------------------------------------------------------------- frontend


@app.get("/sw.js")
def service_worker():
    # Served from root so its scope covers the whole app.
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

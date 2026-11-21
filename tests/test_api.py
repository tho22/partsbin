import io
import threading
import time

from PIL import Image

from app import recognize as rec


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, format="JPEG")
    return buf.getvalue()


def test_part_lifecycle_and_stock(client):
    box = client.post("/api/locations", json={"name": "Box A"}).json()
    drawer = client.post("/api/locations", json={"name": "Drawer 1", "parent_id": box["id"]}).json()
    assert drawer["path"] == "Box A › Drawer 1"

    part = client.post("/api/parts", json={
        "name": "10k resistor", "category": "resistor", "value": "10 kΩ", "quantity": 20,
        "min_quantity": 10, "location_id": drawer["id"],
        "links": [{"label": "Shop", "url": "https://example.com"}],
    }).json()
    assert part["location_path"] == "Box A › Drawer 1"
    assert part["links"][0]["url"] == "https://example.com"

    part = client.post(f"/api/parts/{part['id']}/stock", json={"delta": -12}).json()
    assert part["quantity"] == 8
    assert [p["id"] for p in client.get("/api/parts?low_stock=true").json()] == [part["id"]]

    # Stock never goes negative.
    assert client.post(f"/api/parts/{part['id']}/stock", json={"delta": -100}).json()["quantity"] == 0
    assert client.get("/api/parts?q=10k").json()[0]["id"] == part["id"]


def test_location_cycle_rejected(client):
    a = client.post("/api/locations", json={"name": "A"}).json()
    b = client.post("/api/locations", json={"name": "B", "parent_id": a["id"]}).json()
    r = client.put(f"/api/locations/{a['id']}", json={"name": "A", "parent_id": b["id"]})
    assert r.status_code == 400


def test_project_reserve_and_consume(client):
    part = client.post("/api/parts", json={"name": "ESP32-DevKitC", "category": "dev-board", "quantity": 3}).json()
    pr = client.post("/api/projects", json={"name": "Weather station"}).json()
    pr = client.post(f"/api/projects/{pr['id']}/parts", json={"part_id": part["id"], "quantity": 2}).json()
    assert client.get(f"/api/parts/{part['id']}").json()["reserved"] == 2

    entry = pr["parts"][0]["entry_id"]
    client.post(f"/api/projects/{pr['id']}/parts/{entry}/consume")
    client.post(f"/api/projects/{pr['id']}/parts/{entry}/consume")  # idempotent
    p = client.get(f"/api/parts/{part['id']}").json()
    assert p["quantity"] == 1 and p["reserved"] == 0


def test_scan_and_qr(client):
    loc = client.post("/api/locations", json={"name": "Shelf"}).json()
    assert client.get(f"/api/scan?code=PB:L:{loc['id']}").json() == {"kind": "location", "id": loc["id"]}
    assert client.get("/api/scan?code=PB:L:99999").status_code == 404
    client.post("/api/parts", json={"name": "Timer", "mpn": "NE555P"})
    r = client.get("/api/scan?code=NE555P").json()
    assert r["kind"] == "search" and r["parts"][0]["mpn"] == "NE555P"
    svg = client.get(f"/api/qr/location/{loc['id']}.svg")
    assert svg.status_code == 200 and "<svg" in svg.text
    # Must be standalone (renders as <img>) and a regular 21x21 QR (+2 quiet zone), not Micro QR.
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg.text
    assert 'viewBox="0 0 100 100"' in svg.text


def test_image_upload_and_path_traversal(client):
    r = client.post("/api/images", files={"file": ("x.jpg", _jpeg(), "image/jpeg")})
    name = r.json()["image"]
    assert client.get(f"/api/images/{name}").status_code == 200
    assert client.get("/api/images/..%2Fpartsbin.db").status_code == 404
    assert client.post("/api/images", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 415


def test_recognize_with_matches(client, monkeypatch):
    existing = client.post("/api/parts", json={"name": "Timer IC", "category": "ic", "mpn": "NE555P"}).json()

    def fake(data, hint):
        assert hint == "bag"
        return rec.Recognition(hint="", candidates=[rec.Candidate(
            name="NE555 timer", category="ic", value="", package="DIP-8", manufacturer="TI", mpn="NE555P",
            description="Timer", markings="NE555P", quantity=5, confidence="high", datasheet_url="")])

    monkeypatch.setattr(rec, "recognize", fake)
    start = client.post("/api/recognize", files={"file": ("x.jpg", _jpeg(), "image/jpeg")}, data={"hint": "bag"})
    assert start.status_code == 202
    r = _wait_for_job(client, start.json()["job_id"])
    assert r["status"] == "done"
    assert existing["id"] in [m["id"] for m in r["candidates"][0]["matches"]]


def test_recognize_slow_job_reports_pending(client, monkeypatch):
    release = threading.Event()

    def slow(data, hint):
        release.wait(5)
        return rec.Recognition(hint="", candidates=[])

    monkeypatch.setattr(rec, "recognize", slow)
    job_id = client.post("/api/recognize", files={"file": ("x.jpg", _jpeg(), "image/jpeg")}).json()["job_id"]
    assert client.get(f"/api/recognize/{job_id}").json()["status"] == "pending"
    release.set()
    assert _wait_for_job(client, job_id)["status"] == "done"


def test_recognize_error_reported_by_job(client, monkeypatch):
    def boom(data, hint):
        raise rec.RecognitionError("Anthropic API key is missing or invalid")
    monkeypatch.setattr(rec, "recognize", boom)
    job_id = client.post("/api/recognize", files={"file": ("x.jpg", _jpeg(), "image/jpeg")}).json()["job_id"]
    r = _wait_for_job(client, job_id)
    assert r["status"] == "error" and "API key" in r["error"]
    assert client.get("/api/recognize/doesnotexist").status_code == 404


def _wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/recognize/{job_id}").json()
        if r["status"] != "pending":
            return r
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_prepare_image_downscales():
    buf = io.BytesIO()
    Image.new("RGBA", (4000, 3000)).save(buf, format="PNG")
    b64, media = rec._prepare_image(buf.getvalue())
    import base64
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert media == "image/jpeg" and max(img.size) == rec.MAX_EDGE


def test_spa_fallback(client):
    assert "Partsbin" in client.get("/some/deep/link").text
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/sw.js").status_code == 200

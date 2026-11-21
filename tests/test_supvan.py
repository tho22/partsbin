import base64
import lzma
import os

from app import supvan


def test_lzma_has_exact_size_and_no_end_marker():
    for data in (bytes(4000), os.urandom(3000), b"label" * 700):
        z = supvan.compress_lzma(data)
        assert z[0] == 0x5D and int.from_bytes(z[1:5], "little") == 8192
        assert int.from_bytes(z[5:13], "little") == len(data)
        assert lzma.decompress(z, format=lzma.FORMAT_ALONE) == data
    # The same stream with an end marker (what Python's own encoder writes) is longer.
    data = bytes(4000)
    eopm = lzma.compress(data, format=lzma.FORMAT_ALONE, filters=[{
        "id": lzma.FILTER_LZMA1, "dict_size": 8192, "lc": 3, "lp": 0, "pb": 2, "nice_len": 128}])
    assert len(supvan.compress_lzma(data)) < len(eopm)


def test_print_buffer_header_and_checksum():
    data = bytes(range(256)) * 16
    buf = supvan.build_print_buffer(data, cols=288, first=True, last=True, density=4,
                                    margin_top=16, margin_bottom=16)
    assert len(buf) == 4000
    assert buf[2] == 0x02 | 0x04 | 0x08 and buf[3] == (4 << 2) | 0x40
    assert int.from_bytes(buf[4:6], "little") == 288 and buf[6] == 12
    assert int.from_bytes(buf[8:10], "little") == 16 and int.from_bytes(buf[10:12], "little") == 16
    assert buf[12] == 0 and buf[13] == 0
    end = 288 * 12 + 14
    expected = sum(buf[2:14]) + sum(buf[i] for i in range(255, end, 256))
    assert int.from_bytes(buf[0:2], "little") == expected & 0xFFFF


def test_data_frames():
    frames = supvan.build_data_frames(bytes([7]) * 1200)
    assert len(frames) == 3 * 512
    first = frames[:512]
    assert first[:6] == bytes([0x7E, 0x5A, 0xFC, 0x01, 0x10, 0x02])
    assert first[6:8] == bytes([0xAA, 0xBB]) and first[10] == 0 and first[11] == 3
    assert int.from_bytes(first[8:10], "little") == sum(first[10:]) & 0xFFFF


def test_label_geometry_matches_vendor_flow():
    img = supvan.render_label("PB:L:1", "Box", "Shelf / Box", 12, 40)
    assert img.size == (288, 96)  # (40 - 4) mm x 12 mm at 8 dots/mm
    data, cols = supvan.to_columns(img)
    assert cols == 288 and len(data) == cols * 12
    # Feed column x, dot y: the QR's top-left module (black) is near column 0 / dot 0.
    first_inked = next(i for i in range(cols) if any(data[i * 12 : (i + 1) * 12]))
    assert first_inked < 20
    assert supvan.centre_margins(288, 320) == (16, 16)
    assert supvan.centre_margins(320, 320) == (4, 4)
    bufs = supvan.split_into_buffers(data, cols, 4, (16, 16))
    assert len(bufs) == 1 and int.from_bytes(bufs[0][4:6], "little") == 288
    long_bufs = supvan.split_into_buffers(bytes(500 * 12), 500, 4, (4, 4))
    assert [int.from_bytes(b[4:6], "little") for b in long_bufs] == [332, 168]
    assert long_bufs[0][2] == 0x02 and long_bufs[1][2] == 0x0C
    # Flipping reverses the feed order.
    flipped, _ = supvan.to_columns(img, flip=True)
    assert next(i for i in range(cols) if any(flipped[i * 12 : (i + 1) * 12])) > 100


def test_print_job_endpoint(client):
    loc = client.post("/api/locations", json={"name": "Drawer"}).json()
    job = client.get(f"/api/printer/supvan/location/{loc['id']}?width_mm=12&length_mm=40").json()
    assert job["cols"] == 288 and job["bytes_per_line"] == 12 and job["margins"] == [16, 16]
    assert len(job["blocks"]) == 1
    for block in job["blocks"]:
        frames = base64.b64decode(block["frames"])
        assert len(frames) == block["packets"] * 512
        z = b"".join(frames[i * 512 + 12 : i * 512 + 512] for i in range(block["packets"]))[: block["length"]]
        assert len(lzma.decompress(z, format=lzma.FORMAT_ALONE)) == 4000
    png = client.get(f"/api/printer/supvan/location/{loc['id']}.png")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert client.get("/api/printer/supvan/location/99999").status_code == 404
    assert client.get(f"/api/printer/supvan/location/{loc['id']}?dpm=20").status_code == 422

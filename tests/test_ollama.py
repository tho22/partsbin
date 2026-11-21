import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image

from app import recognize as rec

ANSWER = {"candidates": [{"name": "Raspberry Pi 4 Model B", "category": "single-board-computer", "value": "",
                          "package": "", "manufacturer": "Raspberry Pi Ltd", "mpn": "", "description": "SBC",
                          "markings": "Raspberry Pi 4 Model B", "quantity": 1, "confidence": "high",
                          "datasheet_url": ""}], "hint": ""}


@pytest.fixture()
def fake_ollama(monkeypatch):
    state = {"status": 200, "content": json.dumps(ANSWER), "request": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["request"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"message": {"role": "assistant", "content": state["content"]}, "done": True}).encode()
            if state["status"] != 200:
                body = b'{"error":"model not found"}'
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(rec, "PROVIDER", "ollama")
    monkeypatch.setattr(rec, "OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    yield state
    server.shutdown()


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "green").save(buf, format="PNG")
    return buf.getvalue()


def test_ollama_success(fake_ollama):
    result = rec.recognize(_png(), "board")
    assert result.candidates[0].category == "single-board-computer"
    req = fake_ollama["request"]
    assert req["model"] == rec.OLLAMA_MODEL and req["stream"] is False
    assert req["messages"][1]["images"] and "board" in req["messages"][1]["content"]
    assert "$ref" not in json.dumps(req["format"])
    assert req["format"]["properties"]["candidates"]["items"]["properties"]["category"]["enum"][0] == "resistor"


def test_ollama_invalid_output(fake_ollama):
    fake_ollama["content"] = '{"candidates": [{"name": "x"}]}'
    with pytest.raises(rec.RecognitionError, match="unusable"):
        rec.recognize(_png())


def test_ollama_model_missing(fake_ollama):
    fake_ollama["status"] = 404
    with pytest.raises(rec.RecognitionError, match="ollama pull"):
        rec.recognize(_png())


def test_ollama_unreachable(monkeypatch):
    monkeypatch.setattr(rec, "PROVIDER", "ollama")
    monkeypatch.setattr(rec, "OLLAMA_URL", "http://127.0.0.1:9")
    with pytest.raises(rec.RecognitionError, match="Cannot reach Ollama"):
        rec.recognize(_png())


def test_ollama_connection_reset(monkeypatch):
    def reset(*a, **kw):
        raise ConnectionResetError(104, "Connection reset by peer")
    monkeypatch.setattr(rec, "PROVIDER", "ollama")
    monkeypatch.setattr(rec.urllib.request, "urlopen", reset)
    with pytest.raises(rec.RecognitionError, match="crashed or ran out of memory"):
        rec.recognize(_png())

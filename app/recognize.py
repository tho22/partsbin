"""Identify electronic components on a photo with a vision LLM.

Two providers are supported, selected via PARTSBIN_PROVIDER:
- "anthropic": Claude via the Anthropic API (needs ANTHROPIC_API_KEY)
- "ollama": a local vision model served by Ollama (needs OLLAMA_URL)
"""

import base64
import copy
import io
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Literal

import anthropic
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, ValidationError

from .db import CATEGORIES

log = logging.getLogger(__name__)

PROVIDER = os.environ.get("PARTSBIN_PROVIDER", "anthropic").lower()
LANGUAGE = os.environ.get("PARTSBIN_LANGUAGE", "English")

# Anthropic
MODEL = os.environ.get("PARTSBIN_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("PARTSBIN_EFFORT", "medium")

# Ollama
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "300"))
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "10m")
# Reasoning makes thinking-capable models much slower on CPU for little gain on this task.
OLLAMA_THINK = os.environ.get("OLLAMA_THINK", "false").lower() in ("1", "true", "yes")

# Longest image edge sent to the model. Claude uses up to 1568 px without downscaling; local models get
# slower with every extra image token, so they default to less.
MAX_EDGE = int(os.environ.get("PARTSBIN_MAX_EDGE", "1568" if PROVIDER == "anthropic" else "1024"))

Category = Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]


class Candidate(BaseModel):
    name: str = Field(description="Short display name, e.g. 'ESP32-DevKitC V4' or '10k resistor'")
    category: Category
    value: str = Field(description="Electrical value with unit if applicable, e.g. '10 kΩ', '100 nF', '3.3 V'; empty otherwise")
    package: str = Field(description="Package/form factor, e.g. 'THT 1/4 W', '0805', 'TO-220', 'DIP-8'; empty if unknown")
    manufacturer: str = Field(description="Manufacturer if identifiable, else empty")
    mpn: str = Field(description="Manufacturer part number or marking-derived type, e.g. 'NE555P'; empty if unknown")
    description: str = Field(description="One or two sentences: what it is and key specs")
    markings: str = Field(description="Text, color bands or labels actually visible on the part")
    quantity: int = Field(description="Number of identical pieces visible in the image")
    confidence: Literal["high", "medium", "low"]
    datasheet_url: str = Field(description="Official datasheet or product page URL only if you are certain it exists; else empty")


class Recognition(BaseModel):
    candidates: list[Candidate] = Field(description="One entry per distinct component type visible, most prominent first")
    hint: str = Field(description="Short tip for a better photo if identification was uncertain; empty otherwise")


SYSTEM_PROMPT = f"""You identify electronic components for a hobbyist's parts inventory from photos taken with a phone.

The photo may show anything from passive parts (resistors, capacitors) through ICs and sensor modules to
development boards (ESP32, ESP8266, Arduino, Raspberry Pi Pico) and single-board computers (Raspberry Pi).
It may also show bags or reels with printed labels or barcodes.

Guidelines:
- Read resistor color bands carefully (4-, 5- and 6-band codes); state the bands you see in `markings`
  and derive the value. Mention tolerance in the description.
- For ceramic capacitors decode three-digit codes (e.g. 104 = 100 nF).
- For ICs read the top marking and map it to the part type (e.g. 'NE555P' -> 555 timer).
- For boards identify the exact variant if possible (e.g. 'Raspberry Pi 4 Model B', 'ESP32-S3-DevKitC-1',
  'Wemos D1 mini'); include RAM/flash size only if printed or clearly identifiable.
- Bag labels from distributors (LCSC, Mouser, DigiKey, Reichelt) usually carry MPN, manufacturer and quantity;
  prefer that information over guessing.
- Never invent part numbers or URLs. Leave a field empty rather than guessing; lower `confidence` instead.
- Allowed categories: {", ".join(CATEGORIES)}.
- Write all text fields in {LANGUAGE}, except part numbers and units."""


def _prepare_image(data: bytes) -> tuple[str, str]:
    """Normalize orientation, downscale and re-encode as JPEG. Returns (base64, media_type)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


class RecognitionError(Exception):
    pass


def is_enabled() -> bool:
    if PROVIDER == "ollama":
        return True
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def provider_label() -> str:
    return f"Ollama ({OLLAMA_MODEL})" if PROVIDER == "ollama" else f"Claude ({MODEL})"


def _user_text(user_hint: str) -> str:
    text = "Identify the electronic components in this photo."
    if user_hint:
        text += f"\nAdditional context from the user: {user_hint}"
    return text


def recognize(image_bytes: bytes, user_hint: str = "") -> Recognition:
    b64, media_type = _prepare_image(image_bytes)
    if PROVIDER == "ollama":
        return _recognize_ollama(b64, user_hint)
    if PROVIDER == "anthropic":
        return _recognize_anthropic(b64, media_type, user_hint)
    raise RecognitionError(f"Unknown PARTSBIN_PROVIDER '{PROVIDER}'")


# ---------------------------------------------------------------- Anthropic

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _recognize_anthropic(b64: str, media_type: str, user_hint: str) -> Recognition:
    try:
        response = _get_client().beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            output_format=Recognition,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                        {"type": "text", "text": _user_text(user_hint)},
                    ],
                }
            ],
        )
    except anthropic.AuthenticationError as e:
        raise RecognitionError("Anthropic API key is missing or invalid") from e
    except anthropic.RateLimitError as e:
        raise RecognitionError("Rate limit reached, please retry in a moment") from e
    except anthropic.BadRequestError as e:
        raise RecognitionError(f"Request rejected: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise RecognitionError(f"API error {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise RecognitionError("Cannot reach the Anthropic API") from e

    log.info("recognition request_id=%s model=%s stop=%s", response._request_id, response.model, response.stop_reason)
    if response.stop_reason == "refusal":
        raise RecognitionError("The model declined to analyze this image")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RecognitionError("No usable answer from the model")
    return response.parsed_output


# ---------------------------------------------------------------- Ollama


def _inline_refs(schema: dict) -> dict:
    """Resolve local $ref/$defs, since grammar-based constrained decoding handles flat schemas best."""
    defs = schema.get("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


OLLAMA_SCHEMA = _inline_refs(Recognition.model_json_schema())


def _recognize_ollama(b64: str, user_hint: str) -> Recognition:
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "think": OLLAMA_THINK,
        "format": OLLAMA_SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                # Small models follow the schema more reliably when it is also stated in the prompt.
                "content": _user_text(user_hint) + "\nAnswer as JSON matching this schema:\n" + json.dumps(OLLAMA_SCHEMA),
                "images": [b64],
            },
        ],
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as res:
            body = json.load(res)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 404:
            raise RecognitionError(f"Ollama model '{OLLAMA_MODEL}' not found - run: ollama pull {OLLAMA_MODEL}") from e
        raise RecognitionError(f"Ollama error {e.code}: {detail}") from e
    except TimeoutError as e:
        raise RecognitionError(f"Ollama did not answer within {OLLAMA_TIMEOUT:.0f} s") from e
    except ConnectionResetError as e:
        # Connection accepted, then dropped mid-request: usually the model runner crashed (e.g. out of memory).
        raise RecognitionError(f"Ollama aborted the request - model '{OLLAMA_MODEL}' crashed or ran out of memory?") from e
    except (urllib.error.URLError, OSError) as e:
        raise RecognitionError(f"Cannot reach Ollama at {OLLAMA_URL}") from e

    content = body.get("message", {}).get("content", "")
    log.info("recognition provider=ollama model=%s eval_count=%s total_s=%.1f", OLLAMA_MODEL,
             body.get("eval_count"), body.get("total_duration", 0) / 1e9)
    try:
        return Recognition.model_validate_json(content)
    except ValidationError as e:
        log.warning("invalid model output: %s", content[:500])
        raise RecognitionError("The local model returned an unusable answer, please retry") from e

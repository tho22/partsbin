"""Print jobs for the SUPVAN / Katasymbol E11 label printer (reports itself as "G15").

The browser only relays bytes over Web Bluetooth; everything printer-specific happens here:
rendering the label, packing it into the printer's column-major print buffers, LZMA compression
and framing into 512-byte data frames.

The buffer layout follows the vendor Android app's E11 path (G15Print.initLZMADataB: 4000-byte
buffers of at most 332 columns x 12 bytes, one LZMA block per buffer, margins that centre the
image on the label). Frame and command formats match https://github.com/heeen/supvan-cups (MIT).
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import io
from dataclasses import dataclass

import segno
from PIL import Image, ImageDraw, ImageFont, ImageOps

# --- E11 geometry (G15Print) ----------------------------------------------------------------------
DOTS_PER_MM = 8          # default; the printer reports its own value via command 0x22 (dots/mm x 100)
LINE_DOTS = 96           # fMaxDotValue for E11/E12: one print line = 96 dots = 12 bytes
BPL = LINE_DOTS // 8
LENGTH_TRIM_MM = 4       # the app prints mMatLength = label length - 4 mm and centres it via margins
MIN_MARGIN = 4

# --- print buffers -------------------------------------------------------------------------------
BUF_LEN = 4000
PRINT_BUF_HEADER = 14
MAX_COLUMN_PER_BUF = 332  # 332 x 12 + 14 = 3998 <= 4000
CHECKSUM_STRIDE = 256
MAX_DENSITY = 15
PAGE_ST, PAGE_END, PRT_END = 0x02, 0x04, 0x08
MAT_LABEL = 1

# --- compression and framing ---------------------------------------------------------------------
DICT_SIZE, LC, LP, PB, NICE_LEN, PRESET = 8192, 3, 0, 2, 128, 6  # LzmaUtils.LzmaEncode
DATA_PAYLOAD_SIZE = 500
DATA_FRAME_LEN = 512


# ============================================================ LZMA1 without end marker

class _LzmaOptions(ctypes.Structure):
    _fields_ = [
        ("dict_size", ctypes.c_uint32), ("preset_dict", ctypes.c_void_p),
        ("preset_dict_size", ctypes.c_uint32), ("lc", ctypes.c_uint32), ("lp", ctypes.c_uint32),
        ("pb", ctypes.c_uint32), ("mode", ctypes.c_int), ("nice_len", ctypes.c_uint32),
        ("mf", ctypes.c_int), ("depth", ctypes.c_uint32), ("ext_flags", ctypes.c_uint32),
        ("ext_size_low", ctypes.c_uint32), ("ext_size_high", ctypes.c_uint32),
        ("reserved_int", ctypes.c_uint32 * 5), ("reserved_enum", ctypes.c_int * 4),
        ("reserved_ptr", ctypes.c_void_p * 2),
    ]


class _LzmaFilter(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint64), ("options", ctypes.c_void_p)]


class _LzmaStream(ctypes.Structure):
    _fields_ = [
        ("next_in", ctypes.c_void_p), ("avail_in", ctypes.c_size_t), ("total_in", ctypes.c_uint64),
        ("next_out", ctypes.c_void_p), ("avail_out", ctypes.c_size_t), ("total_out", ctypes.c_uint64),
        ("allocator", ctypes.c_void_p), ("internal", ctypes.c_void_p),
        ("reserved_ptr", ctypes.c_void_p * 4), ("reserved_u64", ctypes.c_uint64 * 2),
        ("reserved_size", ctypes.c_size_t * 2), ("reserved_enum", ctypes.c_int * 2),
        ("_pad", ctypes.c_uint8 * 64),  # slack in case a future liblzma grows the struct
    ]


LZMA_FILTER_LZMA1EXT = 0x4000000000000002
LZMA_VLI_UNKNOWN = 2**64 - 1
LZMA_OK, LZMA_STREAM_END, LZMA_FINISH = 0, 1, 3
_lib = None


def _liblzma():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(ctypes.util.find_library("lzma") or "liblzma.so.5")
        lib.lzma_lzma_preset.argtypes = [ctypes.POINTER(_LzmaOptions), ctypes.c_uint32]
        lib.lzma_lzma_preset.restype = ctypes.c_uint8
        lib.lzma_raw_encoder.argtypes = [ctypes.POINTER(_LzmaStream), ctypes.POINTER(_LzmaFilter)]
        lib.lzma_raw_encoder.restype = ctypes.c_int
        lib.lzma_code.argtypes = [ctypes.POINTER(_LzmaStream), ctypes.c_int]
        lib.lzma_code.restype = ctypes.c_int
        lib.lzma_end.argtypes = [ctypes.POINTER(_LzmaStream)]
        _lib = lib
    return _lib


def compress_lzma(data: bytes) -> bytes:
    """LZMA1 "alone" stream with exact size in the header and no end-of-payload marker.

    The firmware drops data when the end marker is present, and Python's lzma module always
    writes one, so liblzma's LZMA1EXT raw encoder is called directly.
    """
    lib = _liblzma()
    opts = _LzmaOptions()
    if lib.lzma_lzma_preset(ctypes.byref(opts), PRESET):
        raise RuntimeError("lzma preset unsupported")
    opts.dict_size, opts.lc, opts.lp, opts.pb, opts.nice_len, opts.ext_flags = DICT_SIZE, LC, LP, PB, NICE_LEN, 0
    filters = (_LzmaFilter * 2)(
        _LzmaFilter(LZMA_FILTER_LZMA1EXT, ctypes.cast(ctypes.pointer(opts), ctypes.c_void_p)),
        _LzmaFilter(LZMA_VLI_UNKNOWN, None),
    )
    strm = _LzmaStream()
    ret = lib.lzma_raw_encoder(ctypes.byref(strm), filters)
    if ret != LZMA_OK:
        raise RuntimeError(f"lzma_raw_encoder failed: {ret}")
    src = ctypes.create_string_buffer(data, len(data))
    out = ctypes.create_string_buffer(len(data) + 65536)
    try:
        strm.next_in, strm.avail_in = ctypes.cast(src, ctypes.c_void_p), len(data)
        strm.next_out, strm.avail_out = ctypes.cast(out, ctypes.c_void_p), len(out)
        ret = lib.lzma_code(ctypes.byref(strm), LZMA_FINISH)
        if ret != LZMA_STREAM_END:
            raise RuntimeError(f"lzma_code failed: {ret}")
        payload = out.raw[: len(out) - strm.avail_out]
    finally:
        lib.lzma_end(ctypes.byref(strm))
    header = bytes([(PB * 5 + LP) * 9 + LC]) + DICT_SIZE.to_bytes(4, "little") + len(data).to_bytes(8, "little")
    return header + payload


# ============================================================ label rendering

def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_w: int, start: int, min_size: int = 10):
    size = start
    while size > min_size and draw.textlength(text, font=_font(size)) > max_w:
        size -= 1
    font = _font(size)
    while text and draw.textlength(text, font=font) > max_w:
        text = text[:-1]
    return text, font


def render_label(payload: str, title: str, subtitle: str, width_mm: float, length_mm: float,
                 dots_per_mm: float = DOTS_PER_MM) -> Image.Image:
    """Landscape image of the printable area as it should look when read: QR left, text right.

    Like the vendor app, the printable length is the label length minus LENGTH_TRIM_MM; the image is
    at most LINE_DOTS high.
    """
    w = max(16, min(LINE_DOTS, round(width_mm * dots_per_mm)))
    length = max(w, round((length_mm - LENGTH_TRIM_MM) * dots_per_mm))
    img = Image.new("L", (length, w), 255)
    draw = ImageDraw.Draw(img)
    margin = max(2, w // 16)
    qr = segno.make_qr(payload, error="m")
    modules = qr.symbol_size(scale=1, border=1)[0]  # 1-module quiet zone on top of the margin
    scale = max(1, (w - 2 * margin) // modules)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=1)
    qr_img = Image.open(io.BytesIO(buf.getvalue())).convert("L")
    qy = (w - qr_img.height) // 2
    img.paste(qr_img, (margin, qy))
    tx = margin * 2 + qr_img.width
    max_w = length - tx - margin
    if subtitle == title:
        subtitle = ""
    if max_w > 10:
        title_t, title_f = _fit_text(draw, title, max_w, start=int(w * 0.36))
        sub_t, sub_f = _fit_text(draw, subtitle, max_w, start=int(w * 0.2)) if subtitle else ("", None)
        th = title_f.getbbox("Ag")[3]
        sh = sub_f.getbbox("Ag")[3] if sub_f else 0
        gap = margin if sub_t else 0
        y = (w - th - sh - gap) // 2
        draw.text((tx, y), title_t, font=title_f, fill=0)
        if sub_t:
            draw.text((tx, y + th + gap), sub_t, font=sub_f, fill=0)
    return img.point(lambda v: 0 if v < 128 else 255, mode="1")


# ============================================================ packing

_REV = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def to_columns(label: Image.Image, flip: bool = False) -> tuple[bytes, int]:
    """Column-major, LSB-first 1bpp data like the app's ImgConverter: feed column = image x, dot = image y.

    The image is centred vertically in a LINE_DOTS-high line. `flip` rotates the label by 180 degrees.
    Returns (data, columns).
    """
    if flip:
        label = label.rotate(180)
    canvas = Image.new("1", (label.width, LINE_DOTS), 1)
    canvas.paste(label, (0, (LINE_DOTS - label.height) // 2))
    raster = canvas.transpose(Image.Transpose.TRANSPOSE)  # rows = feed columns, x = dot
    ink = ImageOps.invert(raster.convert("L")).convert("1")  # 1 bit = black dot
    return ink.tobytes().translate(_REV), raster.height


def centre_margins(cols: int, label_dots: int) -> tuple[int, int]:
    """Margins (bytes 8-11) that centre `cols` on the label, as in G15Print (minimum 4 dots)."""
    top = (label_dots - cols) // 2
    bottom = label_dots - cols - top
    return max(MIN_MARGIN, top), max(MIN_MARGIN, bottom)


def build_print_buffer(image_data: bytes, cols: int, first: bool, last: bool, density: int,
                       margin_top: int, margin_bottom: int) -> bytes:
    buf = bytearray(BUF_LEN)
    buf[2] = (PAGE_ST if first else 0) | (PAGE_END | PRT_END if last else 0)
    buf[3] = ((min(density, MAX_DENSITY) & 0x0F) << 2) | (MAT_LABEL << 6)
    buf[4:6] = cols.to_bytes(2, "little")
    buf[6] = BPL
    buf[8:10] = margin_top.to_bytes(2, "little")
    buf[10:12] = margin_bottom.to_bytes(2, "little")
    data = image_data[: cols * BPL]
    buf[PRINT_BUF_HEADER : PRINT_BUF_HEADER + len(data)] = data
    data_end = cols * BPL + PRINT_BUF_HEADER
    chk = sum(buf[2:PRINT_BUF_HEADER]) + sum(buf[i] for i in range(CHECKSUM_STRIDE - 1, data_end, CHECKSUM_STRIDE))
    buf[0:2] = (chk & 0xFFFF).to_bytes(2, "little")
    return bytes(buf)


def split_into_buffers(image_data: bytes, cols: int, density: int, margins: tuple[int, int]) -> list[bytes]:
    """Tile all columns into 4000-byte buffers of at most MAX_COLUMN_PER_BUF columns."""
    chunks = [(c, min(MAX_COLUMN_PER_BUF, cols - c)) for c in range(0, cols, MAX_COLUMN_PER_BUF)]
    return [
        build_print_buffer(image_data[start * BPL : (start + n) * BPL], n, i == 0, i == len(chunks) - 1,
                           density, *margins)
        for i, (start, n) in enumerate(chunks)
    ]


def build_data_frames(compressed: bytes) -> bytes:
    chunks = [compressed[i : i + DATA_PAYLOAD_SIZE] for i in range(0, len(compressed), DATA_PAYLOAD_SIZE)]
    frames = bytearray()
    for idx, chunk in enumerate(chunks):
        pkt = bytearray(6 + DATA_PAYLOAD_SIZE)
        pkt[0], pkt[1], pkt[4], pkt[5] = 0xAA, 0xBB, idx, len(chunks)
        pkt[6 : 6 + len(chunk)] = chunk
        pkt[2:4] = (sum(pkt[4:]) & 0xFFFF).to_bytes(2, "little")
        frames += bytes([0x7E, 0x5A]) + (DATA_FRAME_LEN - 4).to_bytes(2, "little") + bytes([0x10, 0x02]) + pkt
    return bytes(frames)


@dataclass
class Block:
    length: int      # compressed bytes
    packets: int     # data frames (NEXT_ZIPPEDBULK block count)
    frames: bytes


def print_job(label: Image.Image, length_mm: float, density: int = 4, flip: bool = False,
              dots_per_mm: float = DOTS_PER_MM) -> dict:
    data, cols = to_columns(label, flip)
    margins = centre_margins(cols, round(length_mm * dots_per_mm))
    blocks = []
    for buf in split_into_buffers(data, cols, density, margins):
        z = compress_lzma(buf)  # one LZMA block per buffer, like G15Print
        blocks.append(Block(len(z), -(-len(z) // DATA_PAYLOAD_SIZE), build_data_frames(z)))
    return {
        "cols": cols,
        "bytes_per_line": BPL,
        "margins": list(margins),
        "blocks": [{"length": b.length, "packets": b.packets, "frames": base64.b64encode(b.frames).decode()} for b in blocks],
    }

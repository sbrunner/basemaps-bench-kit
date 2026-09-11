#!/usr/bin/env python3
"""Shared helpers: tile math, zoom-level scale assertions, WMS fetching, pure-stdlib PNG decoding."""

import json
import math
import os
import re
import struct
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter
from dataclasses import dataclass

TILE_SIZE = 1024
WEB_MERCATOR_HALF = 20037508.342789244
OGC_PIXEL_M = 0.00028  # OGC WMS reference pixel: 0.28 mm
DPI96_PIXEL_M = 0.0254 / 96  # 96 dpi convention

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
RESULTS = os.path.join(KIT, "results")
MAPDIR = os.path.join(KIT, "runtime")

VARIANTS = ("before", "after", "fix")
MAPFILES = {v: "/etc/mapserver/osm-google-%s.map" % v for v in VARIANTS}
MAPFILES["after_default"] = "/etc/mapserver/osm-google.map"

# Layer visibility windows from generate_style.py (minscales/maxscales),
# identical in base 89fb10a and PR head f793716 for the benchmarked levels
# (level 18 lower bound differs: 0 in base, 1270 in PR -> intersection used).
LEVEL_RANGES = {
    12: (81252, 162504),
    14: (20313, 40626),
    16: (5078, 10156),
    18: (1270, 2539),
}


@dataclass(frozen=True)
class Tile:
    zoom: int  # target scale level (roads<zoom> is the layer drawn)
    x: int  # coordinates on the underlying tile grid (zoom - size_shift)
    y: int
    bbox: tuple[float, float, float, float]  # minx, miny, maxx, maxy (EPSG:3857)
    roads_count: int = -1
    tile_zoom: int = -1


def size_shift(size: int) -> int:
    """A size-px request keeps the scaledenom of zoom z when it covers the bbox of a (z - shift) tile."""
    shift = int(round(math.log2(size / TILE_SIZE)))
    assert 2**shift * TILE_SIZE == size, "size must be 256 * 2^n"
    return shift


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_bbox(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    size = 2.0 * WEB_MERCATOR_HALF / 2**zoom
    minx = -WEB_MERCATOR_HALF + x * size
    maxx = minx + size
    maxy = WEB_MERCATOR_HALF - y * size
    miny = maxy - size
    return minx, miny, maxx, maxy


def scaledenom(bbox: tuple[float, float, float, float], pixel_m: float = OGC_PIXEL_M, size: int = TILE_SIZE) -> float:
    width_m = bbox[2] - bbox[0]
    return width_m / (size * pixel_m)


def check_zoom(zoom: int, bbox: tuple[float, float, float, float], size: int = TILE_SIZE) -> None:
    """Fail fast unless a size-px request on this bbox lands strictly inside level zoom's scale window."""
    lo, hi = LEVEL_RANGES[zoom]
    for pixel_m, label in ((OGC_PIXEL_M, "OGC 0.28mm"), (DPI96_PIXEL_M, "96dpi")):
        sd = scaledenom(bbox, pixel_m, size)
        assert lo < sd < hi, (
            "zoom %d: scaledenom %.0f (%s) outside level window [%d, %d]" % (zoom, sd, label, lo, hi)
        )


def tile_grid(center_lon: float, center_lat: float, zoom: int, grid: int, size: int = TILE_SIZE) -> list[Tile]:
    """Tiles of the (zoom - shift) XYZ grid, labelled with the target scale level zoom."""
    shift = size_shift(size)
    tile_zoom = zoom - shift
    assert tile_zoom >= 0, "zoom %d too low for size %d" % (zoom, size)
    cx, cy = lonlat_to_tile(center_lon, center_lat, tile_zoom)
    half = grid // 2
    tiles = []
    for dy in range(-half, grid - half):
        for dx in range(-half, grid - half):
            x, y = cx + dx, cy + dy
            bbox = tile_bbox(x, y, tile_zoom)
            check_zoom(zoom, bbox, size)
            tiles.append(Tile(zoom, x, y, bbox, tile_zoom=tile_zoom))
    return tiles


def wms_url(port: int, mapfile: str, bbox: tuple[float, float, float, float], layers: str | None = None,
            size: int = TILE_SIZE, host: str = "localhost") -> str:
    params = [
        "SERVICE=WMS",
        "VERSION=1.3.0",
        "REQUEST=GetMap",
        "map=%s" % mapfile,
        "STYLES=",
        "CRS=EPSG:3857",
        "BBOX=%.6f,%.6f,%.6f,%.6f" % bbox,
        "WIDTH=%d" % size,
        "HEIGHT=%d" % size,
        "FORMAT=image/png",
        "TRANSPARENT=FALSE",
    ]
    if layers is not None:
        params.append("LAYERS=%s" % layers)
    return "http://%s:%d/?%s" % (host, port, "&".join(params))


class FetchError(RuntimeError):
    pass


def fetch_tile(url: str) -> tuple[float, bytes]:
    """GET a tile, validate it is a real PNG (WMS exceptions come back as XML with HTTP 200)."""
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.URLError as exc:
        raise FetchError("HTTP error: %s" % exc) from exc
    elapsed = time.perf_counter() - t0
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        snippet = data[:300].decode("utf-8", "replace").replace("\n", " ")
        raise FetchError("not a PNG (Content-Type: %s): %s" % (ctype, snippet))
    return elapsed, data


# --- minimal PNG decoder (stdlib only), 8-bit, non-interlaced ---------------

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> tuple[int, int, list[tuple[int, ...]]]:
    """Return (width, height, rows of per-pixel tuples). Supports 8-bit color types 0/2/3/4/6."""
    pos = 8
    ihdr = None
    idat = []
    plte = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            plte = chunk
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
        pos += 12 + length
    assert ihdr, "no IHDR"
    width, height, depth, color, _comp, _filt, interlace = ihdr
    assert depth == 8, "unsupported bit depth %d" % depth
    assert interlace == 0, "interlaced PNG not supported"
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(height * stride)
    prev = bytearray(stride)
    src = 0
    for row in range(height):
        ftype = raw[src]
        src += 1
        line = bytearray(raw[src : src + stride])
        src += stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        elif ftype != 0:
            raise ValueError("unknown filter %d" % ftype)
        out[row * stride : (row + 1) * stride] = line
        prev = line
    pixels = []
    for row in range(height):
        base = row * stride
        if color == 3:
            assert plte, "palette PNG without PLTE"
            pixels.append([tuple(plte[out[base + i] * 3 : out[base + i] * 3 + 3]) for i in range(width)])
        else:
            pixels.append([tuple(out[base + i * channels : base + (i + 1) * channels]) for i in range(width)])
    return width, height, pixels


def png_stats(data: bytes) -> dict:
    width, height, rows = decode_png(data)
    counter = Counter(px for row in rows for px in row)
    bg, bg_count = counter.most_common(1)[0]
    total = width * height
    return {
        "width": width,
        "height": height,
        "bytes": len(data),
        "distinct_colors": len(counter),
        "background": bg,
        "non_bg_pct": round(100.0 * (total - bg_count) / total, 3),
    }


def pixel_diff_pct(a: bytes, b: bytes) -> float:
    wa, ha, rows_a = decode_png(a)
    wb, hb, rows_b = decode_png(b)
    if (wa, ha) != (wb, hb):
        return 100.0
    diff = sum(1 for ra, rb in zip(rows_a, rows_b) for pa, pb in zip(ra, rb) if pa[:3] != pb[:3])
    return round(100.0 * diff / (wa * ha), 3)


def load_tiles(path: str) -> list[Tile]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [Tile(t["zoom"], t["x"], t["y"], tuple(t["bbox"]), t.get("roads_count", -1), t.get("tile_zoom", t["zoom"]))
            for t in raw["tiles"]]


def load_tiles_size(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        return int(json.load(f).get("size", TILE_SIZE))


# --- mapfile parsing: explicit LAYERS lists instead of server-side guesswork --

def local_mapfile(variant: str, mapdir: str = MAPDIR) -> str:
    return os.path.join(mapdir, "osm-google-%s.map" % variant)


def parse_map_layers(path: str) -> list[dict]:
    """Extract (name, minscaledenom, maxscaledenom) for every LAYER of a generated mapfile.

    Scale directives may sit before AND/OR after the NAME line depending on the
    template, so walk back to the LAYER keyword and forward to the first CLASS
    (never stop on END: it may close a nested PROJECTION/COMPOSITE block).
    Stop the backward walk at a previous NAME line: the candidate was then not
    inside a layer header (e.g. SYMBOL/OUTPUTFORMAT names).
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    def collect(text: str, info: dict) -> None:
        if text.startswith("MINSCALEDENOM"):
            info["min"] = float(text.split()[1])
        elif text.startswith("MAXSCALEDENOM"):
            info["max"] = float(text.split()[1])

    layers = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("NAME"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        info: dict = {"name": parts[1].strip('"'), "min": None, "max": None}
        found_layer = False
        j = i - 1
        while j >= 0:
            t = lines[j].strip()
            if t == "LAYER":
                found_layer = True
                break
            if t.startswith("NAME"):
                break
            collect(t, info)
            j -= 1
        if not found_layer:
            continue
        for k in range(i + 1, len(lines)):
            t = lines[k].strip()
            if t in ("CLASS", "LAYER"):
                break
            collect(t, info)
        layers.append(info)
    return layers


def visible_layers(path: str, sd: float) -> list[str]:
    """Layer names drawn at scale denominator sd, in mapfile order (= draw order)."""
    out = []
    for info in parse_map_layers(path):
        lo = info["min"] if info["min"] is not None else 0.0
        hi = info["max"] if info["max"] is not None else float("inf")
        if lo <= sd <= hi:
            out.append(info["name"])
    return out


def check_roads_layer(path: str, zoom: int, sd: float) -> list[str]:
    """Return the visible layers, asserting roads<zoom> is the only roads layer drawn."""
    vis = visible_layers(path, sd)
    expected = "roads%d" % zoom
    assert expected in vis, "%s: %s not visible at scaledenom %.0f (visible: %s)" % (
        os.path.basename(path), expected, sd, vis)
    wrong = [n for n in vis if re.fullmatch(r"roads\d+", n) and n != expected]
    assert not wrong, "%s: unexpected roads layers visible at z%d: %s" % (
        os.path.basename(path), zoom, wrong)
    return vis

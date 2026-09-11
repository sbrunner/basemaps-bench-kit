#!/usr/bin/env python3
"""
Timing runner: alternates variants (A/B/C rotation) tile by tile so cache/thermal
drift affects all variants equally. Writes results/raw.csv incrementally.
"""

import argparse
import csv
import os
import statistics
import sys
import time

from benchlib import MAPFILES, VARIANTS, FetchError, fetch_tile, load_tiles, wms_url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("BENCH_PORT", "80")))
    parser.add_argument("--tiles", default="../results/tiles.json")
    parser.add_argument("--out", default="../results/raw.csv")
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--scopes", default="full,roads", help="full and/or roads")
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--max-tiles", type=int, default=0, help="0 = all tiles per zoom")
    parser.add_argument("--floor-repeats", type=int, default=3, help="probes with a nonexistent LAYERS value (overhead floor)")
    args = parser.parse_args()

    variants = args.variants.split(",")
    scopes = args.scopes.split(",")
    all_tiles = load_tiles(args.tiles)
    zooms = sorted({t.zoom for t in all_tiles})
    tiles_by_zoom = {z: [t for t in all_tiles if t.zoom == z] for z in zooms}
    if args.max_tiles:
        tiles_by_zoom = {z: ts[: args.max_tiles] for z, ts in tiles_by_zoom.items()}

    total_requests = sum(
        len(ts) * (args.warmup + args.repeats) * len(variants) * len(scopes) for ts in tiles_by_zoom.values()
    ) + len(zooms) * args.floor_repeats
    print("variants=%s scopes=%s zooms=%s repeats=%d(+%d warmup) -> %d requests" % (
        variants, scopes, zooms, args.repeats, args.warmup, total_requests))

    timings: dict[tuple[int, str, str], list[float]] = {}
    done = 0
    t_start = time.time()
    aborted = False

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["zoom", "tile_x", "tile_y", "scope", "variant", "repeat", "ms", "bytes"])

        def run(zoom: int, tile, scope: str, variant: str, repeat: int, measured: bool) -> bool:
            nonlocal done
            # floor scope: a real layer whose scale window excludes every benchmarked
            # zoom (level 0 draws only at scaledenom >= 332M) -> valid empty PNG that
            # measures WMS + mapfile parsing + process overhead
            layers = {"full": None, "roads": "roads%d" % zoom, "floor": "roads0"}[scope]
            url = wms_url(args.port, MAPFILES[variant], tile.bbox, layers)
            try:
                elapsed, data = fetch_tile(url)
            except FetchError as exc:
                print("RETRY %s z%d %s %s: %s" % (variant, zoom, scope, repeat, exc))
                time.sleep(1)
                try:
                    elapsed, data = fetch_tile(url)
                except FetchError as exc2:
                    print("ABORT: second failure: %s" % exc2, file=sys.stderr)
                    return False
            ms = elapsed * 1000.0
            done += 1
            if measured:
                writer.writerow([zoom, tile.x, tile.y, scope, variant, repeat, round(ms, 1), len(data)])
                fh.flush()
                timings.setdefault((zoom, scope, variant), []).append(ms)
            if done % 50 == 0:
                eta = (time.time() - t_start) / done * (total_requests - done)
                print("  ... %d/%d requests, ETA %.0f min" % (done, total_requests, eta / 60))
            return True

        for zoom in zooms:
            tiles = tiles_by_zoom[zoom]
            # overhead floor: request with a nonexistent LAYERS value (WMS draws nothing)
            for r in range(args.floor_repeats):
                if not run(zoom, tiles[0], "floor", "after", r, True):
                    aborted = True
                    break
            if aborted:
                break
            for tile in tiles:
                for w in range(args.warmup):  # warm DB/page cache and font caches
                    for scope in scopes:
                        for variant in variants:
                            if not run(zoom, tile, scope, variant, w, False):
                                aborted = True
                                break
                        if aborted:
                            break
                    if aborted:
                        break
                if aborted:
                    break
                for r in range(args.repeats):
                    shift = (tile.x + tile.y + r) % len(variants)
                    order = variants[shift:] + variants[:shift]
                    scope_order = scopes[r % len(scopes):] + scopes[: r % len(scopes)]
                    for scope in scope_order:
                        for variant in order:
                            if not run(zoom, tile, scope, variant, r, True):
                                aborted = True
                                break
                        if aborted:
                            break
                    if aborted:
                        break
                if aborted:
                    break
                meds = {v: statistics.median(timings[(zoom, "roads", v)]) for v in variants if (zoom, "roads", v) in timings}
                print("z%d tile %d/%d done - roads medians (ms): %s" % (
                    zoom, tile.x, tile.y, {k: round(v) for k, v in meds.items()}))
            if aborted:
                break

    print("\n%s after %d requests in %.1f min -> %s" % (
        "ABORTED" if aborted else "DONE", done, (time.time() - t_start) / 60, args.out))
    return 1 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())

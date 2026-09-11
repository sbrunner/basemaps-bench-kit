#!/usr/bin/env python3
"""
Select benchmark tiles and PROVE they are not empty:
 - road counts per tile straight from PostGIS (docker compose exec psql, or a
   direct connection via --dsn when running from another machine)
 - zoom-level scale-window assertions (fail fast), for the requested image size
With --size 1024 the tiles keep the scaledenom of zoom z by covering the bbox of
a (z-2) XYZ tile, so roads<z> remains the layer exercised.
Writes results/tiles.json.
"""

import argparse
import json
import os
import subprocess
import sys

from benchlib import LEVEL_RANGES, RESULTS, TILE_SIZE, scaledenom, size_shift, tile_grid

# roads_data picks prod.osm_roads for every benchmarked zoom (level dict keys: 11 -> roads, 14 -> roads)
ROADS_TABLE = "prod.osm_roads"

CENTERS = {
    "geneva": (6.1432, 46.2044),
    "lausanne": (6.6323, 46.5197),
}


def make_psql(compose_cmd: list[str], dsn: str | None):
    if dsn:
        def query(sql: str) -> str:
            proc = subprocess.run(["psql", dsn, "-tA", "-c", sql], capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise RuntimeError("psql failed: %s\n%s" % (sql, proc.stderr.strip()))
            return proc.stdout.strip()
        return query

    def query(sql: str) -> str:
        cmd = compose_cmd + ["exec", "-T", "postgis", "psql", "-U", "osm", "-d", "osm", "-tA", "-c", sql]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError("psql failed: %s\n%s" % (sql, proc.stderr.strip()))
        return proc.stdout.strip()
    return query


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-cmd", default="docker compose", help="compose invocation from the kit root")
    parser.add_argument("--dsn", default=None,
                        help='direct psql connection string, e.g. "host=10.0.2.2 port=5433 user=osm password=osm dbname=osm"')
    parser.add_argument("--center", default="geneva", choices=sorted(CENTERS))
    parser.add_argument("--zooms", default="12,14,16,18")
    parser.add_argument("--grid", type=int, default=4, help="grid x grid candidate tiles per zoom")
    parser.add_argument("--size", type=int, default=TILE_SIZE, help="GetMap WIDTH/HEIGHT (256, 512, 1024...)")
    parser.add_argument("--min-roads", type=int, default=50, help="reject tiles with fewer roads")
    parser.add_argument("--out", default=os.path.join(RESULTS, "tiles.json"))
    args = parser.parse_args()

    psql = make_psql(args.compose_cmd.split(), args.dsn)
    zooms = [int(z) for z in args.zooms.split(",")]
    center = CENTERS[args.center]

    version = psql("SELECT version()")
    extent = psql("SELECT ST_AsEWKT(ST_SetSRID(ST_Extent(geometry),3857)) FROM %s" % ROADS_TABLE)
    total = psql("SELECT count(*) FROM %s" % ROADS_TABLE)
    print("DB: %s" % version.split(",")[0])
    print("roads extent: %s" % extent)
    print("roads rows: %s" % total)
    if int(total) < 10000:
        print("ERROR: DB looks empty - import finished?", file=sys.stderr)
        return 1

    selected = []
    for zoom in zooms:
        assert zoom in LEVEL_RANGES, "no scale window for zoom %d" % zoom
        kept = 0
        for tile in tile_grid(center[0], center[1], zoom, args.grid, size=args.size):
            sql = (
                "SELECT count(*) FROM %s WHERE geometry && ST_SetSRID("
                "ST_MakeEnvelope(%.3f,%.3f,%.3f,%.3f),3857)" % ((ROADS_TABLE,) + tile.bbox)
            )
            count = int(psql(sql))
            if count < args.min_roads:
                print("z%d tile %d/%d (grid z%d) REJECTED (roads=%d < %d)" % (
                    zoom, tile.x, tile.y, tile.tile_zoom, count, args.min_roads))
                continue
            selected.append(
                {
                    "zoom": zoom,
                    "tile_zoom": tile.tile_zoom,
                    "x": tile.x,
                    "y": tile.y,
                    "bbox": list(tile.bbox),
                    "roads_count": count,
                    "scaledenom_ogc": round(scaledenom(tile.bbox, size=args.size)),
                }
            )
            kept += 1
        print("z%d@%dpx: kept %d/%d tiles (grid z%d), scale window %s" % (
            zoom, args.size, kept, args.grid**2, zoom - size_shift(args.size), LEVEL_RANGES[zoom]))
        if kept == 0:
            print("ERROR: no valid tile at zoom %d - wrong center, size too large or empty DB" % zoom, file=sys.stderr)
            return 1
        if kept < 4:
            print("WARNING: only %d valid tile(s) at z%d - fewer independent samples" % (kept, zoom))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"center": args.center, "lonlat": center, "size": args.size, "tiles": selected}, f, indent=1)
    print("wrote %s (%d tiles, size=%d)" % (args.out, len(selected), args.size))
    return 0


if __name__ == "__main__":
    sys.exit(main())

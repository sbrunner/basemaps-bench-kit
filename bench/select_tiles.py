#!/usr/bin/env python3
"""
Select benchmark tiles and PROVE they are not empty:
 - road counts per tile straight from PostGIS (docker compose exec psql)
 - zoom-level scale-window assertions (fail fast)
Writes results/tiles.json.
"""

import argparse
import json
import os
import subprocess
import sys

from benchlib import LEVEL_RANGES, RESULTS, scaledenom, tile_grid

# roads_data picks prod.osm_roads for every benchmarked zoom (level dict keys: 11 -> roads, 14 -> roads)
ROADS_TABLE = "prod.osm_roads"

CENTERS = {
    "geneva": (6.1432, 46.2044),
    "lausanne": (6.6323, 46.5197),
}


def psql(compose_cmd: list[str], sql: str) -> str:
    cmd = compose_cmd + ["exec", "-T", "postgis", "psql", "-U", "osm", "-d", "osm", "-tA", "-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("psql failed: %s\n%s" % (sql, proc.stderr.strip()))
    return proc.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-cmd", default="docker compose", help="compose invocation from the kit root")
    parser.add_argument("--center", default="geneva", choices=sorted(CENTERS))
    parser.add_argument("--zooms", default="12,14,16,18")
    parser.add_argument("--grid", type=int, default=4, help="grid x grid tiles per zoom")
    parser.add_argument("--min-roads", type=int, default=50, help="reject tiles with fewer roads")
    parser.add_argument("--out", default=os.path.join(RESULTS, "tiles.json"))
    args = parser.parse_args()

    compose_cmd = args.compose_cmd.split()
    zooms = [int(z) for z in args.zooms.split(",")]
    center = CENTERS[args.center]

    version = psql(compose_cmd, "SELECT version()")
    extent = psql(compose_cmd, "SELECT ST_AsEWKT(ST_SetSRID(ST_Extent(geometry),3857)) FROM %s" % ROADS_TABLE)
    total = psql(compose_cmd, "SELECT count(*) FROM %s" % ROADS_TABLE)
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
        for tile in tile_grid(center[0], center[1], zoom, args.grid):
            sql = (
                "SELECT count(*) FROM %s WHERE geometry && ST_SetSRID("
                "ST_MakeEnvelope(%.3f,%.3f,%.3f,%.3f),3857)" % ((ROADS_TABLE,) + tile.bbox)
            )
            count = int(psql(compose_cmd, sql))
            if count < args.min_roads:
                print("z%d tile %d/%d REJECTED (roads=%d < %d)" % (zoom, tile.x, tile.y, count, args.min_roads))
                continue
            selected.append(
                {
                    "zoom": zoom,
                    "x": tile.x,
                    "y": tile.y,
                    "bbox": list(tile.bbox),
                    "roads_count": count,
                    "scaledenom_ogc": round(scaledenom(tile.bbox)),
                }
            )
            kept += 1
        print("z%d: kept %d/%d tiles, scale window %s" % (zoom, kept, args.grid**2, LEVEL_RANGES[zoom]))
        if kept == 0:
            print("ERROR: no valid tile at zoom %d - wrong center or empty DB" % zoom, file=sys.stderr)
            return 1

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"center": args.center, "lonlat": center, "tiles": selected}, f, indent=1)
    print("wrote %s (%d tiles)" % (args.out, len(selected)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

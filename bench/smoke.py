#!/usr/bin/env python3
"""
Smoke tests BEFORE any timing:
 - every variant x zoom x scope renders a valid non-empty PNG (pixel-level check)
 - wrong-zoom layer detection via mapserver debug logs (requires MS_DEBUGLEVEL>=2)
 - after-vs-fix roads tiles must be (near) pixel-identical
Samples saved under results/samples/, report in results/smoke.json.
Exit code != 0 on failure.
"""

import argparse
import datetime
import json
import re
import subprocess
import sys

from benchlib import (
    MAPFILES,
    VARIANTS,
    fetch_tile,
    load_tiles,
    pixel_diff_pct,
    png_stats,
    wms_url,
)

MIN_NON_BG = {"full": 20.0, "roads": 0.2}


def compose_logs(compose_cmd: list[str], since_iso: str) -> str:
    cmd = compose_cmd + ["logs", "--since", since_iso, "mapserver"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-cmd", default="docker compose")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--tiles", default="results/tiles.json")
    parser.add_argument("--samples", default="results/samples")
    parser.add_argument("--report", default="results/smoke.json")
    parser.add_argument("--variants", default=",".join(VARIANTS))
    args = parser.parse_args()
    compose_cmd = args.compose_cmd.split()
    variants = args.variants.split(",")

    with open(args.tiles, encoding="utf-8") as f:
        tiles_meta = json.load(f)
    zooms = sorted({t["zoom"] for t in tiles_meta["tiles"]})
    first_tiles = {z: next(t for t in load_tiles(args.tiles) if t.zoom == z) for z in zooms}

    failures: list[str] = []
    report: dict = {"tiles": {}, "log_layer_checks": [], "after_vs_fix_diff_pct": {}}
    road_images: dict[tuple[int, str], bytes] = {}

    for zoom in zooms:
        tile = first_tiles[zoom]
        for scope in ("full", "roads"):
            layers = None if scope == "full" else "roads%d" % zoom
            for variant in variants:
                since = (datetime.datetime.now() - datetime.timedelta(seconds=1)).astimezone().isoformat()
                url = wms_url(args.port, MAPFILES[variant], tile.bbox, layers)
                name = "z%d_%s_%s" % (zoom, scope, variant)
                #print(url)
                try:
                    elapsed, data = fetch_tile(url)
                except Exception as exc:  # a broken mapfile must fail the smoke test loudly
                    failures.append("%s: %s" % (name, exc))
                    print("FAIL %-24s %s" % (name, exc))
                    continue
                with open("%s/%s.png" % (args.samples, name), "wb") as f:
                    f.write(data)
                try:
                    stats = png_stats(data)
                except Exception as exc:
                    failures.append("%s: PNG decode failed: %s" % (name, exc))
                    print("FAIL %-24s PNG decode: %s" % (name, exc))
                    continue
                ok = stats["non_bg_pct"] >= MIN_NON_BG[scope]
                status = "ok  " if ok else "FAIL"
                if not ok:
                    failures.append("%s: non_bg_pct=%.3f < %.1f (empty image?)" % (name, stats["non_bg_pct"], MIN_NON_BG[scope]))
                print("%s %-24s %6.0f ms  %7.1f KB  colors=%-5d non_bg=%6.2f%%" % (
                    status, name, elapsed * 1000, stats["bytes"] / 1024, stats["distinct_colors"], stats["non_bg_pct"]))
                report["tiles"][name] = {"ms": round(elapsed * 1000, 1), **stats, "background": list(stats["background"])}

                # layer-name check through mapserver debug logs (best effort: needs MS_DEBUGLEVEL>=2)
                logs = compose_logs(compose_cmd, since)
                mentioned = sorted({int(m) for m in re.findall(r"roads(\d+)", logs)})
                if mentioned:
                    wrong = [m for m in mentioned if m != zoom]
                    entry = {"request": name, "roads_levels_in_logs": mentioned, "ok": not wrong}
                    report["log_layer_checks"].append(entry)
                    if wrong:
                        failures.append("%s: logs mention roads levels %s (expected only %d)" % (name, mentioned, zoom))
                        print("     FAIL log check: roads levels %s seen, expected [%d]" % (mentioned, zoom))
                    elif scope == "roads" and zoom not in mentioned:
                        failures.append("%s: roads%d never drawn according to logs" % (name, zoom))
                else:
                    print("     (no roads layer info in logs - MS_DEBUGLEVEL too low?)")

                if scope == "roads":
                    road_images[(zoom, variant)] = data

    # after vs fix: same rendering expected (the fix only changes the classification path)
    for zoom in zooms:
        if (zoom, "after") in road_images and (zoom, "fix") in road_images:
            diff = pixel_diff_pct(road_images[(zoom, "after")], road_images[(zoom, "fix")])
            report["after_vs_fix_diff_pct"][zoom] = diff
            ok = diff <= 1.0
            if not ok:
                failures.append("z%d roads: after vs fix pixel diff %.2f%% > 1%%" % (zoom, diff))
            print("%s z%d roads after-vs-fix pixel diff: %.3f%%" % ("ok  " if ok else "FAIL", zoom, diff))

    report["failures"] = failures
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    print("\nSMOKE %s (%d failure(s)) - report: %s" % ("PASSED" if not failures else "FAILED", len(failures), args.report))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

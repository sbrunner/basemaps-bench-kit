#!/usr/bin/env python3
"""Aggregate results/raw.csv into per-(zoom, scope, variant) stats + markdown summary."""

import argparse
import csv
import statistics
import sys
from collections import defaultdict


def pct(new: float, ref: float) -> str:
    if ref <= 0:
        return "n/a"
    delta = (new - ref) / ref * 100.0
    return "%+.1f%%" % delta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default="../results/raw.csv")
    parser.add_argument("--out", default="../results/summary.md")
    args = parser.parse_args()

    data: dict[tuple[int, str, str], list[float]] = defaultdict(list)
    with open(args.raw, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            data[(int(row["zoom"]), row["scope"], row["variant"])].append(float(row["ms"]))
    if not data:
        print("no data", file=sys.stderr)
        return 1

    zooms = sorted({k[0] for k in data})
    scopes = [s for s in ("roads", "full") if any(k[1] == s for k in data)]
    variants = sorted({k[2] for k in data if k[1] != "floor"})
    ordered = [v for v in ("before", "after", "fix") if v in variants] + [v for v in variants if v not in ("before", "after", "fix")]

    lines = ["# Benchmark summary", ""]
    header = "| zoom | scope | " + " | ".join("%s med (ms)" % v for v in ordered) + " | after vs before | fix vs before | p90 before/after/fix | n |"
    sep = "|---|---|" + "---|" * (len(ordered) + 4)
    lines += [header, sep]
    for zoom in zooms:
        for scope in scopes:
            stats = {}
            for v in ordered:
                vals = data.get((zoom, scope, v), [])
                if vals:
                    q = statistics.quantiles(vals, n=10) if len(vals) >= 10 else [max(vals)]
                    stats[v] = (statistics.median(vals), statistics.fmean(vals), q[-1], len(vals))
            if not stats:
                continue
            meds = ["%.0f" % stats[v][0] if v in stats else "-" for v in ordered]
            p90s = "/".join("%.0f" % stats[v][2] if v in stats else "-" for v in ordered)
            n = stats[ordered[0]][3] if ordered[0] in stats else "-"
            after_vs = pct(stats["after"][0], stats["before"][0]) if "after" in stats and "before" in stats else "-"
            fix_vs = pct(stats["fix"][0], stats["before"][0]) if "fix" in stats and "before" in stats else "-"
            lines.append("| %d | %s | %s | %s | %s | %s | %s |" % (
                zoom, scope, " | ".join(meds), after_vs, fix_vs, p90s, n))
    floors = {k[0]: statistics.median(v) for k, v in data.items() if k[1] == "floor"}
    lines += ["", "Overhead floor (empty render: WMS + mapfile parse, ms median): " +
              ", ".join("z%d=%.0f" % (z, m) for z, m in sorted(floors.items())), ""]

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

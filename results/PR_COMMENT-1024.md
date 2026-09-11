# Draft — comment for https://github.com/MapServer/basemaps/pull/103 (1024x1024 run)

---

Re: the performance concern about the class expressions — here are before/after timings.

**Variants** (same PostGIS DB, same container, same machine; requests strictly alternated per tile so cache/thermal drift affects all variants equally):

- `before` — PR base `89fb10a`: `CLASSITEM "type"` + plain string expressions, bridge/tunnel encoded in the `roads_data` SQL (`type||bridge||tunnel as type`)
- `after` — PR head `f793716`: logical expressions (`("[type]" IN ... AND "[bridge]" = "0" AND "[tunnel]" = "0")` — 33 of them in the google-style mapfile, no CLASSITEM on the roads layer)
- `fix` — PR head with the suggested approach applied: expression logic moved back into the template engine/SQL (`CLASSITEM "type"` restored, suffixes computed in `roads_data`, PR class structure kept; only the 5 service-overlay classes keep logical expressions since `service=*` values cannot be encoded in the 2-digit suffix). Patch attached.

**Setup**: MapServer 8.4 (camptocamp/mapserver:8.4-gdal3.10, apache + CGI), PostGIS (postgres 14.22 / postgis 3), imposm3 schema, data = Vaud + Geneva cantons (CH, 289k roads rows). WMS GetMap **1024x1024** EPSG:3857, bboxes of the (z-2) tile grid so each request keeps the exact scaledenom of level z (`roads<z>` verified to be the only roads layer drawn). 6-16 DB-validated tiles per zoom, 2 warmups + 9 timed repeats, medians in ms. `roads` = GetMap restricted to `LAYERS=roads<z>` (isolates the classification change); `full` = all visible layers (also includes the new PR features: trees, extended landuse, oneway arrows, subway...).

| zoom | scope | before | after | fix | after vs before | fix vs before | after vs fix |
|---|---|---|---|---|---|---|---|
| 12 | roads | 211 | 251 | 223 | +18.8% | +5.3% | +12.6% |
| 12 | full | 471 | 611 | 556 | +29.7% | +17.9% | +9.9% |
| 14 | roads | 138 | 182 | 148 | +32.0% | +7.1% | +23.0% |
| 14 | full | 338 | 694 | 649 | +105.6% | +92.1% | +6.9% |
| 16 | roads | 138 | 180 | 154 | +30.1% | +11.4% | +16.9% |
| 16 | full | 254 | 571 | 540 | +124.7% | +112.7% | +5.7% |
| 18 | roads | 62 | 75 | 71 | +21.2% | +14.2% | +5.6% |
| 18 | full | 106 | 342 | 338 | +222.8% | +218.9% | +1.2% |

(n = 9 repeats x 6/12/15/16 tiles per zoom; roads-only tiles of `after` and `fix` are byte-identical, so the fix changes no rendering.)

**Reading the numbers**

- `after` vs `fix` (same mapfile size, same rendering, only the classification path differs) isolates the cost of the logical expressions: **+6% to +23%** on roads-only rendering.
- `fix` vs `before` residual (+5% to +14%) is mostly the larger generated mapfile (12 -> 15-16 visible layers, more classes), i.e. per-request mapfile parsing — not classification. At z12, where all three variants render byte-identical images, it also bounds the measurement noise (~5%).
- `full` tile deltas (+30% to +223%) are dominated by the **new features** themselves (trees from z16, oneway arrows, subway, extended landuse), not by the expression change — `fix` barely differs from `after` there. That cost is inherent to what the PR adds and worth discussing separately (e.g. trees18/oneway_arrows18 at z18).

**Proposal**: fold the CLASSITEM + SQL-suffix approach into the PR (`fix` patch): identical rendering to the current PR head, roads classification back to `before` levels. The service-overlay classes can keep their logical expressions, or `service` could be folded into the suffix encoding as a follow-up.

Side observation while porting the expressions back: in the current PR head the `pier` class still uses `EXPRESSION {pier,pier00,pier10}` but the roads layer no longer has a `CLASSITEM`, so piers are presumably no longer rendered; restoring the CLASSITEM also fixes that.

Benchmark harness (compose stack, DB-validated tile selection, pixel-level non-empty checks, rotation-based timing runner) available on request to reproduce.

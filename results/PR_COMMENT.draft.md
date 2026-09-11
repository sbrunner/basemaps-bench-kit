# Draft — comment for https://github.com/MapServer/basemaps/pull/103

(fill `{{...}}` from results/summary.md, attach samples if useful)

---

Re: the performance concern about the class expressions — here are before/after timings.

**Variants** (all rendered against the same PostGIS DB, in the same container, requests strictly alternated per tile so cache/thermal drift affects all variants equally):

- `before` — PR base `89fb10a`: `CLASSITEM "type"` + plain string expressions, bridge/tunnel encoded in the `roads_data` SQL (`type||bridge||tunnel as type`)
- `after` — PR head `f793716`: logical expressions (`("[type]" IN ... AND "[bridge]" = "0" AND "[tunnel]" = "0")`, 33 of them in the google-style mapfile)
- `fix` — PR head with your suggestion applied: the expression logic moved back into the template engine (`CLASSITEM "type"` restored, suffixes computed in SQL, only the 5 service-overlay classes keep logical expressions since `service=` values cannot be encoded in the 2-digit suffix). Patch: {{link or attached}}.

**Setup**: MapServer {{8.4}} (camptocamp/mapserver:8.4-gdal3.10, apache + CGI), PostGIS (postgres:14-postgis-3), imposm3 schema, data = Vaud + Geneva cantons (CH). WMS GetMap 256×256 EPSG:3857, 16 tiles per zoom (tile content validated against the DB beforehand), 2 warmups + 9 timed repeats, medians in ms.
`roads` = GetMap restricted to `LAYERS=roads<z>` — isolates the classification change. `full` = complete tile — also includes the new PR features (trees, extended landuse, oneway arrows), so its delta is not only the expression regression. Overhead floor (empty render, WMS + mapfile parsing): ~{{35}} ms, identical for all variants.

| zoom | scope | before | after | fix | after vs before | fix vs before |
|---|---|---|---|---|---|---|
| {{12}} | roads | {{60}} | {{86}} | {{60}} | {{+43%}} | {{~0%}} |
| {{12}} | full   | … | … | … | … | … |
| {{14}} | roads | … | … | … | … | … |
| {{14}} | full   | … | … | … | … | … |
| {{16}} | roads | … | … | … | … | … |
| {{16}} | full   | … | … | … | … | … |
| {{18}} | roads | … | … | … | … | … |
| {{18}} | full   | … | … | … | … | … |

The `fix` variant renders roads-only tiles pixel-identically to `after` (max diff over the smoke-tested tiles: {{0.00}}%), i.e. same styling, classification cost back to `before` levels.

If the numbers look acceptable to you, I can fold the CLASSITEM/SQL-suffix approach into the PR (the service-overlay classes would stay logical expressions, or `service` could be folded into the suffix encoding as a follow-up).

Side observation while porting the expressions back: in the current PR head the `pier` class still uses `EXPRESSION {pier,pier00,pier10}` but the layer no longer has a `CLASSITEM`, so I suspect piers are no longer rendered — the fix restores them.

Benchmark harness (compose stack + tile validation + timing scripts) available if you want to reproduce: {{link}}.

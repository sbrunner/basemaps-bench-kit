# Kit benchmark — PR MapServer/basemaps#103

Timings avant/après pour répondre au commentaire de tbonfort sur
https://github.com/MapServer/basemaps/pull/103 (expressions logiques vs CLASSITEM).

Trois variantes, même base de données, même conteneur, requêtes alternées:

| variante | mapfile | contenu |
|---|---|---|
| `before` | `runtime/osm-google-before.map` | base upstream `89fb10a`: `CLASSITEM "type"` + expressions chaîne, suffixes `type\|\|bridge\|\|tunnel` calculés en SQL |
| `after` | `runtime/osm-google-after.map` | tête de PR `f793716`: expressions logiques `("[type]" IN ... AND "[bridge]" = "0" ...)` |
| `fix` | `runtime/osm-google-fix.map` | `f793716` + suggestion de tbonfort: CLASSITEM restauré, logique bridge/tunnel déplacée dans le SQL/template (voir `fix-classitem.patch`) |

Style `google` (style existant affecté par les templates partagés). Données: cantons **Vaud + Genève** (extraits osm.fr, zones urbaines denses).

## Contenu du kit

- `docker-compose.yml` — postgis (postgres:14-postgis-3) + imposm3 (mapping de la PR monté) + mapserver (camptocamp/mapserver:8.4-gdal3.10)
- `repo/` — arbre source de la PR (contextes de build docker, mapping imposm, sources pour rebuild)
- `runtime/` — les 3 mapfiles pré-buildés + `config.conf` (montés fichier par fichier dans le conteneur mapserver)
- `bench/` — scripts python3 **stdlib uniquement** (rien à installer sur l'hôte)
- `fix-classitem.patch`, `patch_classitem.py` — reproduction de la variante `fix`
- `results/` — sorties (tiles.json, samples/, raw.csv, summary.md)

## Prérequis (machine cible)

- Docker + docker compose v2, **port 80 libre** (ou `BENCH_PORT`), port 5433 libre
- ~12 GB libres pour docker (images + volumes; ~16 GB si Suisse entière)
- python3 sur l'hôte
- Réseau: pulls d'images, PBF (~80 MB), et le build de l'image webserver télécharge les land polygons (~650 MB depuis osmdata.openstreetmap.de)
- Pendant les mesures: rien d'autre qui tourne sur la machine

## Procédure

```bash
cd basemaps-bench-kit
export BENCH_PORT=8485

# 1. Base de données + import imposm (~10-25 min pour Vaud+Genève)
docker compose up -d postgis imposm
docker compose logs -f imposm        # attendre la fin (deployproduction), Ctrl-C pour sortir
docker compose exec postgis psql -U osm -d osm -c 'select count(*) from prod.osm_roads'

# 2. Serveur de cartes (première fois: build long, ~650 MB téléchargés)
docker compose up -d --build mapserver

# 3. Sélection des tuiles (validées NON VIDES via comptage SQL dans PostGIS)
python3 bench/select_tiles.py              # -> results/tiles.json

# 4. Smoke tests OBLIGATOIRES (PNG non vides au niveau pixel, bons layers
#    dans les logs debug, after ~= fix en pixels)
python3 bench/smoke.py                     # doit afficher SMOKE PASSED

# 5. Passer en mode mesure (sans logs debug), recréer le conteneur
MS_DEBUGLEVEL=0 docker compose up -d mapserver
sleep 5

# 6. Benchmark (~20-45 min)
python3 bench/run_bench.py                 # -> results/raw.csv
#    options: --repeats 11 (vrai p90), --max-tiles 8 (plus court), --zooms via tiles.json

# 7. Agrégation
python3 bench/aggregate.py                 # -> results/summary.md (tableau markdown)
```

## Après

- Remplir `results/PR_COMMENT.draft.md` avec le tableau de `summary.md` et poster sur la PR.
- Conserver `results/samples/*.png` (preuves visuelles) et `results/smoke.json`.
- Nettoyage: `docker compose stop` (garde la base importée) ou `docker compose down` (**jamais** `down -v` si tu veux rejouer sans réimporter).

## Variantes de configuration

- **Port occupé**: `BENCH_PORT=8080 docker compose up -d mapserver` puis `python3 smoke.py --port 8080` / `python3 run_bench.py --port 8080`.
- **Suisse entière** (plus représentatif, ~15 GB docker): dans `docker-compose.yml`, remplacer les 2 URLs `PBF_FILES` par `https://download.geofabrik.de/europe/switzerland-latest.osm.pbf` (commentaire sur place), puis relancer l'étape 1.
- **Rebuild des mapfiles** (optionnel, ils sont fournis):
  ```bash
  cd repo && make -f docker.mk          # -> osm-google.map (variante after)
  # before: worktree de 89fb10a (MapServer/basemaps main), même commande
  # fix:    git checkout f793716 && git apply fix-classitem.patch && make -f docker.mk
  ```

## Garde-fous intégrés (images vides / bon zoom)

1. `select_tiles.py` compte les roads **en base** par tuile (`geometry && ST_MakeEnvelope`) et rejette toute tuile sous le seuil → impossible de mesurer des tuiles hors zone.
2. `benchlib.check_zoom` asserte que le scaledenom de chaque tuile (conventions 0.28 mm OGC **et** 96 dpi) tombe strictement dans la fenêtre `[minscales[z], maxscales[z]]` du niveau z. De plus, `smoke.py`/`run_bench.py` parsent les mapfiles locaux et assertent que `roads<z>` est le **seul** layer roads visible à ce scaledenom; le scope `full` envoie la liste explicite des layers visibles (un GetMap sans paramètre LAYERS est refusé par MapServer).
3. `smoke.py` décode les PNG (décodeur stdlib intégré), exige un contenu réel (pixels non-fond ≥ seuils), vérifie dans les logs mapserver qu'aucun `roads<autre niveau>` n'apparaît (fenêtre de 2 s de silence avant chaque requête pour une attribution sûre), et compare after vs fix pixel à pixel.
4. `run_bench.py` valide chaque réponse (magic bytes PNG, les exceptions WMS renvoient du XML en HTTP 200) et s'arrête après 2 échecs consécutifs.

## Notes / pièges connus

- Scope `roads` = GetMap avec `LAYERS=roads<z>`: isole le changement de classification (c'est LE chiffre qui répond au commentaire). Scope `full` = tuile complète: inclut les nouveautés de la PR (trees dès z16, landuse étendus, flèches oneway) → son delta n'est pas uniquement la régression d'expressions.
- Le floor (scope interne `floor`, couche invisible `roads0`) mesure l'overhead WMS + parsing du mapfile, identique pour les 3 variantes.
- Dans `after`, la classe `pier` (`EXPRESSION {pier,pier00,pier10}`) n'a plus de CLASSITEM — à vérifier si les piers s'affichent encore (le diff pixel after-vs-fix de `smoke.py` peut le révéler sur les tuiles avec piers; un petit diff non nul mais < 1% peut venir de là).
- MapServer des mesures: 8.4 (image camptocamp), PostGIS: postgres 14 / postgis 3, mêmes `shared_buffers` par défaut pour les 3 variantes.
- Ne pas lancer `select_tiles.py`/`smoke.py` pendant que d'autres charges tournent; le benchmark lui-même alterne les variantes requête par requête (dérive annulée).

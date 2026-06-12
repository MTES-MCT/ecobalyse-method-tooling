# food/transformed-ingredients/

Two independent Python scripts that support the Ecobalyse "composant" /
transformed-ingredient workflow. Both talk to a running Volca server (an
LCA engine) loaded with an Agribalyse database (and optionally Ecoinvent,
WFLDB, Ginko, …) and produce artefacts consumed by the Ecobalyse data
pipeline (`lci_catalog/` / `activities_to_create.json` / composant CSV).

They solve two different problems and can be run independently.

---

## 1. `extract_transform_params.py`

### Objective

Produce the per-transformation parameters required by the Ecobalyse
"composant" module — raw material yield, electricity, heat, allocation
factor, added water, co-products, biowaste share, `is_byproduct` flag —
which are not directly exposed by Agribalyse and must be reverse-engineered
from sub-activity trees.

### How it works

Calls `volca.agribalyse.decompose` on each target process. `decompose`
walks the supply chain via Volca's `/aggregate` and `/get_activity`
endpoints and classifies the activity as one of three authoring patterns:

- **Pattern A — `wrapper_wfldb`**: Agribalyse wrapper pointing to a WFLDB
  activity that carries the real inventory (e.g. *Beurre*, *Lait en
  poudre*).
- **Pattern B — `direct`**: the transformation inventory lives directly on
  the activity (e.g. *Jus de pomme NFC*).
- **Pattern C — `layered`**: thin layer over another Agribalyse
  sub-activity (e.g. *Tomate pelée*).

The resulting `Decomposition` is then mapped to a CSV row, applying the
two domain rules documented in the file (allocation-key selection + loss
fallback via biowaste for multi-product processes).

### Inputs

- A reachable Volca server (default `http://localhost:8080`).
- A Volca database — i.e. an Agribalyse build loaded into Volca (default
  database name: `agribalyse-3.2`).
- Either the 4 hard-coded reference PIDs (default), or every activity
  matched by a `transformed` classification preset declared in the Volca
  TOML config (with `--all`).

### Outputs

- A CSV file matching the spreadsheet schema expected by the composant
  module (default: `transform_params.csv`).
- A fixed-width table printed to stdout.

### Usage

```bash
# 4 reference processes, default CSV name
uv run python extract_transform_params.py

# Custom output file
uv run python extract_transform_params.py --output transform_params.csv

# Every activity matched by preset=transformed (capped)
uv run python extract_transform_params.py --all --limit 50

# Full options
uv run python extract_transform_params.py --help
```

---

## 2. `generate_transformed_ingredients.py`

### Objective

For every transformed Agribalyse product `T` that already uses one raw
ingredient variant `V_src` (e.g. `radish-fr`), generate new variants of
`T` that use each other raw variant `V_tgt` (e.g. `radish-organic`,
`radish-default`) instead — both as substitution rules and as full
`activities.json` entries.

### How it works

1. Walk the `lci_catalog/` directory (per-activity files grouped by source
   slug, which replaced the monolithic `activities.json`), group ingredients
   by base name, and collect their known variants (`-fr` / `-organic` /
   `-default` / `-eu` / `-non-eu`) with associated activity / source /
   location.
2. For each variant, call Volca `get_consumers` (`include_edges=True`,
   no server-side preset) and keep the full BFS subgraph. Transformed-
   product filtering is applied client-side on
   `ConsumerResult.classifications` (Category ∈ `TRANSFORMED_CATEGORIES`
   with `Category type = material`). Keeping the unfiltered response
   preserves consumption-mix metadata needed to walk paths.
3. For each transformed consumer `C` and each variant `V_tgt` it does NOT
   yet use, reconstruct the shortest path `C → … → V_src` locally from
   the edge subgraph (no extra HTTP call), then build a `from_existing`
   block with:
   ```
   existingActivity = path[0]
   upstreamPath     = path[1:-1]
   replace.from     = path[-1]   (= V_src)
   replace.to       = V_tgt
   ```
   **Special case:** if a hop has
   `Category = "Agricultural\Food\Consumption mixes"`, the mix itself
   becomes `replace.from`, so the whole sourcing blend is swapped.
4. Also emit an `activities.json` entry for each new transformed activity.
   Physical metadata (`ingredientDensity`, `transportCooling`, `cropGroup`,
   `ingredientCategories`) is **predicted** from the transformed-product
   name by `../metadata/predict.py` (FoodOn ontology + nearest-neighbour).
   The English activity name is translated to French via
   `Helsinki-NLP/opus-mt-en-fr` for `displayName`. `inediblePart = 0` and
   `rawToCookedRatio = 1.0` are hardcoded. Only `scenario` and
   `defaultOrigin` come from the target raw variant.

### Inputs

- A reachable Volca server (default `http://localhost:8080`).
- The path to the ecobalyse repository. Every input is derived from it:
  - `data/lci_catalog/` — raw ingredient variants + metadata, one file per
    activity (replaces the old monolithic `activities.json`); also the merge
    target.
  - `data/activities_to_create.json` — existing aliases to avoid duplicates;
    also where the generated `from_existing` blocks are appended.
  - `public/data/food/ingredients.json` — flat ingredients file used to train
    the metadata predictor.

### Outputs

- `generated_activities_to_create.json` — `from_existing` substitution
  blocks to merge into `activities_to_create.json`.
- `generated_activities.json` — new activity entries merged into the
  `lci_catalog/` automatically (pass `--no-merge` to skip and only write the
  generated files).
- `transformed_ingredients.csv` — review report, one row per generated
  variant: base ingredient, existing activity, variant, display name,
  alias, the lci_catalog metadata block, and the upstream-replacement
  specifics (replacement depth = number of intermediate upstream steps,
  path, replaced/replacement activities). The `ecs` column is left empty
  by the generator: the environmental cost is produced by the ecobalyse
  pipeline (`just import-all && just export-all`) and backfilled into the
  CSV afterwards with `uv run python report.py /path/to/ecobalyse`.
- `.predictor.pkl` — cached predictor, sibling of the script,
  git-ignored — regenerated on first run, reused afterwards.
- `.translation_cache.json` — persistent English → French cache; the
  post-translation corrections in `translation_corrections.csv` are
  re-applied on every run.

### Usage

The full workflow is three steps — generate, run the ecobalyse pipeline,
backfill the `ecs` column of the report:

```bash
# 1. Generate (merges into the ecobalyse repo unless --no-merge)
uv run python generate_transformed_ingredients.py /path/to/ecobalyse \
    [--output-dir .] \
    [--volca-url http://localhost:8080] \
    [--max-depth 2] \
    [--no-merge]

# 2. In the ecobalyse repo: create the activities and compute the impacts
just import-all && just export-all

# 3. Backfill ecs into transformed_ingredients.csv from the pipeline output
#    (same ecobalyse path as step 1)
uv run python report.py /path/to/ecobalyse

uv run python generate_transformed_ingredients.py --help
```

---

## Prerequisites (both scripts)

- [`uv`](https://docs.astral.sh/uv/) to manage the virtualenv and
  dependencies declared in `pyproject.toml` / `uv.lock`.
- A running Volca server with the needed databases loaded (Agribalyse,
  Ecoinvent, WFLDB, Ginko, … — names as configured in the Volca TOML).
- Python 3.12+.

Install / sync dependencies once:

```bash
uv sync
```

> `pyvolca` is sourced from a local path (see `[tool.uv.sources]` in
> `pyproject.toml`); make sure the sibling Volca checkout is present at
> the expected location, or update that entry before running `uv sync`.

---

## Related files in this directory

| File | Purpose |
|------|---------|
| `pyproject.toml` / `uv.lock` | Dependency manifest |
| `transform_params.csv` | Output of `extract_transform_params.py` (4 reference processes) |
| `transform_params_all.csv` | Output of `extract_transform_params.py --all` |
| `generated_activities_to_create.json` | Output of `generate_transformed_ingredients.py` |
| `generated_activities.json` | Output of `generate_transformed_ingredients.py` |
| `transformed_ingredients.csv` | Output of `generate_transformed_ingredients.py` (review report) |
| `report.py` | Report row building + CSV writing for `generate_transformed_ingredients.py` |
| `translation_corrections.csv` | Manual post-translation overrides applied after MarianMT EN→FR |
| `.translation_cache.json` | Auto-generated translation cache |
| `.predictor.pkl` | Cached metadata predictor (auto-generated) |
| `prez_extract_transform_params_fr.md` / `.pdf` | French slide deck |
| `prez_generate_transformed_fr.md` / `.pdf` | French slide deck |

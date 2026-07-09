# brightway_vs_volca

Diagonal parity clouds comparing **ecobalyse-Brightway** and **VoLCA** on the BAFU database,
same base and same method (EF 3.1 adapted). x = ecobalyse-Brightway, y = each VoLCA collection;
**on the diagonal = the two engines agree**. It answers "does our Brightway import reproduce
VoLCA?" and, by adding the 1.05 collection, exposes the categories 1.03 under-covers (Water Use).

Both sides read the **same** SimaPro CSV (`BAFU2026v1.CSV.zip`), so a divergence is the engine or
the method coverage, not the input.

## Directory structure

```
brightway_vs_volca/
├── compare.py          # the tool (self-contained)
├── pyproject.toml      # uv project (pyvolca, requests, bw2data, bw2calc, matplotlib, dotenv)
├── .env.example        # copy to .env and adjust paths
└── README.md
```

## Setup

Copy `.env.example` to `.env`. The two sides:

- **ecobalyse (x)** — `BRIGHTWAY2_DIR`, `EB_PROJECT`, `EB_DATABASE` (`BAFU 2026v1`), `EB_METHOD`.
- **VoLCA (y)** — the tool **starts VoLCA itself**: `VOLCA_PORT` (dedicated, e.g. 8091), `VOLCA_DB`
  (`bafu-2026v1`), `BAFU_DB_PATH` (the same CSV zip ecobalyse imports), `VOLCA_CONFIG_BASE` (a config
  providing the EF method collections + curated flow synonyms, e.g. `examples/volca-bafu.toml`),
  optional `VOLCA_BINARY` (else `volca.download()` fetches one), and `VOLCA_COLLECTIONS`.

`VOLCA_COLLECTIONS` is the list of VoLCA method collections plotted as y-series. Default is the
1.03 collection (same method as ecobalyse). Add the 1.05 collection to expose the Water Use gap:

```
VOLCA_COLLECTIONS=Environmental Footprint 3.1 (adapted 1.03),Environmental Footprint 3.1 (adapted 1.05)
```

## Usage

```bash
uv run compare.py --report etat.html          # full matched panel
uv run compare.py --sample 500 --report etat.html   # quick sample
uv run compare.py --sample 500 --exclude-long-term --report etat.html   # both sides drop long-term
```

`--exclude-long-term` asks VoLCA to drop long-term emissions too, matching ecobalyse's `noLT`
strategy, so `Eutrophication freshwater` and `Ionising radiation` become comparable (otherwise
VoLCA counts long-term groundwater phosphate / long-term radon that ecobalyse zeroes). It uses
per-activity VoLCA calls (the bulk endpoint has no such flag), so it is slower — pair it with
`--sample`.

First run starts the engine and parses BAFU + the EF methods (a few minutes; later runs hit the
cache). It writes a CSV of rows (`product, category, ecobalyse, v0, ...`) and a self-contained HTML
report:

1. **heatmap** — median `|VoLCA/ecobalyse − 1|` per indicator × collection (⚠ over 10 %).
2. **nuages diagonaux** — one log-log scatter per EF category, x = ecobalyse, y = each collection.
3. **biggest-gap tables** — per collection, the (product, indicator) pairs that diverge most.

## Reading the result

- 1.03 vs ecobalyse-1.03 **on the diagonal** = engine + import agree.
- A category that leaves the diagonal is the target: use `bafu/flow-characterization/` and a
  per-flow contribution comparison to attribute the cause (coverage hole, name/compartment
  mismatch, unit, or a legitimate engine/import difference), then close coverage holes with
  synonyms/mapping in the ecobalyse repo.
- Electricity (kWh vs MJ) is reconciled by the unit factor, not a real divergence.

## Provenance

The VoLCA plumbing (activities, bulk `impacts` POST, unit factors, scatter/report layout) is
adapted from `/home/dadafkas/projets/VoLCA/volca-deploy/volca/examples/bafu_oracle_compare.py`
(which compares VoLCA against the official BAFU oracle spreadsheet).

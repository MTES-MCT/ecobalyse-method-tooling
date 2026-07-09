# bafu/ — Ecobalyse BAFU tooling

Tooling around the **BAFU** (Swiss KBOB) LCA database imported into Brightway
alongside ecoinvent. Each subfolder is one topic, an independent `uv` project
(own `pyproject.toml` / `uv.lock`), with its own `README.md`. Start with the
table below, then open that folder's README.

## Where to go

| Folder | Go here when you want to… |
|--------|---------------------------|
| [`flow-characterization/`](flow-characterization/) | List the BAFU biosphere flows the EF 3.1 method does not characterize, to target the mapping / synonym work instead of guessing. |
| [`brightway_vs_volca/`](brightway_vs_volca/) | Diagonal parity clouds comparing ecobalyse-Brightway vs VoLCA on BAFU (same base, same EF 3.1 method), to validate the import and localize every diverging impact category. |

## Shared context

All topics read a Brightway project (`BRIGHTWAY2_DIR`) that already holds the
imported `BAFU 2026v1` database and the `Environmental Footprint 3.1 (adapted)`
method (imported via the ecobalyse-data pipeline, `just import-all`). Each
folder's README documents its own inputs/outputs and `.env`.

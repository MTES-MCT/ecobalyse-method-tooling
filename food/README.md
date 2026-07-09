# food/ — Ecobalyse food data tooling

Each subfolder is one topic, an independent `uv` project (own
`pyproject.toml` / `uv.lock`), with its own `README.md`. Start with the
table below, then open that folder's README.

## Where to go

| Folder | Go here when you want to… |
|--------|---------------------------|
| [`metadata/`](metadata/) | Predict ingredient metadata (foodType, NOVA group, density, cropGroup, …) for new ingredients, and export them into Ecobalyse's `ingredients.json` + impacts CSV. |
| [`transformed-ingredients/`](transformed-ingredients/) | Reverse-engineer per-transformation parameters (yield, electricity, heat, allocation, …) for the "composant" module, and generate transformed-ingredient variants (organic / import / …) from an existing one. |
| [`agribalyse_recipe/`](agribalyse_recipe/) | Extract the ingredient bill of materials (recipe) of any Agribalyse transformed product to Excel. |

## Shared context

All three talk to a **VoLCA** server (an LCA calculation engine) loaded
with an Agribalyse database, and either predict metadata via the same
`metadata/predict.py` `Predictor`, or read/write Ecobalyse repo data
(`lci_catalog/`, `activities_to_create.json`, `ingredients.json`). Each
folder's README documents its own inputs/outputs and how it plugs into
that shared pipeline.

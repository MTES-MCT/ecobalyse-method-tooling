# Agribalyse recipe extraction

One self-contained script (PEP 723 inline deps, no `pyproject.toml` needed):
`extract_agribalyse_recipes.py`.

## What it does

For every selected Agribalyse transformed product (e.g. *Pizza, cheese,
frozen, prepacked*), writes one row per ingredient — its technosphere
bill of materials:

```
1 kg "Pizza, ..."  ->  X kg tomato + Y kg cheese + Z kg flour + ...
```

The "recipe" is just `get_activity(pid).technosphere_inputs`: (ingredient
name, amount, unit, upstream process id), minus the reference product
itself. Each row is classified (`raw_material` / `water` / `electricity`
/ `heat` / `transport` / `other`) via `volca.agribalyse.classify_exchange`
so you can filter down to food ingredients only.

## What `volca.download()` gives you (and what it doesn't)

`download()` fetches the VoLCA engine binary + the *reference-data* bundle
(flow synonyms, units, compartments, geographies) — no LCA database. You
supply Agribalyse yourself: the official SimaPro CSV export, a free public
download from ADEME (data.gouv.fr / doc.agribalyse.fr). Pass its path with
`--agribalyse`; a Brightway/Excel export works too, the engine detects the
format.

## Usage

```bash
# a few products by name (default: --select Pizza, --limit 3)
uv run extract_agribalyse_recipes.py \
    --agribalyse "/path/to/AGB32_final.CSV.zip" \
    --select Pizza --select Bread --out recipes.xlsx

# the whole Agribalyse food catalogue
uv run extract_agribalyse_recipes.py \
    --agribalyse "/path/to/AGB32_final.CSV.zip" \
    --all --limit 0 --out all_recipes.xlsx
```

Selection: `--select NAME` (substring match), `--classification
"System=Value"` (e.g. `Category=Agricultural\Food`), or `--all` (shortcut
for the food catalogue). `--limit` caps each selector (`0` = no cap);
truncation is always reported, never a silent cut.

`uv run extract_agribalyse_recipes.py --help` for every option (engine
version pin, port, startup timeout, …).

## Output

An `.xlsx` workbook, one row per ingredient:

| Column | Meaning |
|--------|---------|
| `product_process_id` / `product_name` / `location` | The transformed product |
| `functional_unit_amount` / `functional_unit` | Its reference amount (usually `1 kg`) |
| `ingredient_name` / `ingredient_amount` / `ingredient_unit` | One technosphere input |
| `role` | `raw_material` / `water` / `electricity` / `heat` / `transport` / `other` |
| `ingredient_process_id` | Upstream process id, for chasing further |

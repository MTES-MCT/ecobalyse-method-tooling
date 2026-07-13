# Agribalyse recipe extraction

Extracts the **ingredient bill** of Agribalyse transformed products to Excel —
one row per technosphere input:

```
1 kg "Pizza, …"  ->  X kg tomato + Y kg cheese + Z kg flour + …
```

`extract_agribalyse_recipes.py` is self-contained (PEP 723 inline deps, run with
[uv](https://docs.astral.sh/uv/)): it downloads the VoLCA engine binary +
reference-data bundle via [pyvolca](https://www.volca.run/docs/python/), starts
it locally, loads the Agribalyse database you point it at, and writes the recipe
rows. You supply Agribalyse yourself — the official SimaPro CSV export is a free
public download from ADEME. The engine auto-detects the format: SimaPro CSV
(`.csv`, `.csv.zip`, `.7z`), EcoSpold, ILCD, or a Brightway/Excel export
(`.xlsx`).

## Run

```bash
# a few products by name
uv run extract_agribalyse_recipes.py \
    --agribalyse "/path/to/AGB32_final.CSV.zip" \
    --select Pizza --select Bread --out recipes.xlsx

# the whole food catalogue, edible ingredients only
uv run extract_agribalyse_recipes.py \
    --agribalyse "/path/to/AGB32_final.CSV.zip" \
    --all --limit 0 --ingredients-only --out all_recipes.xlsx
```

Selection: `--select NAME` (substring, repeatable), `--classification
"System=Value"` (e.g. `"Category=Agricultural\Food\Recipes"`), or `--all`
(shortcut for the food catalogue). `--limit` caps each selector (0 = no cap);
truncation is always reported, never silent.

## `--ingredients-only`

Keeps only the edible ingredients and drops everything else a recipe pulls in —
cooking, `[Dummy]` operations, waste treatment, electricity, heat, transport.
An input is kept when all three hold:

- its producing activity is tagged **`Category type = material`** (Agribalyse's
  tag for edible materials; cooking is `processing`, energy `energy`, transport
  `transport`, end-of-life `waste treatment`),
- its role is a **food role** — `raw_material`, `other` (gram/litre dairy, sugar,
  chocolate) or `water` (recipe water is kept: its amount matters),
- its unit is a **food unit** — not `m3`, which drops the last `material`-tagged
  utilities (natural gas, compressed air).

One classification sweep builds the `material` set once; membership plus the
role and unit guards then filter each recipe.

## Output columns

| Column | Meaning |
|--------|---------|
| `product_process_id`, `product_name`, `location` | the transformed product |
| `functional_unit_amount`, `functional_unit` | its functional unit (typically 1 kg) |
| `ingredient_name`, `ingredient_amount`, `ingredient_unit` | one input of the recipe |
| `role` | `raw_material` / `other` / `water` / … (from `classify_exchange`) |
| `ingredient_process_id` | the ingredient's own process, to recurse further |

# Agribalyse recipe extraction

Extracts the **edible ingredients** and **packaging systems** of Agribalyse 3.2 foods to an Excel workbook, per functional unit:

```text
1 kg "Aioli sauce, …"  ->  0.728 kg olive oil + 0.108 kg garlic + …
                       ->  2.35 × "Mayonnaise, 425g | Packaging System, N0, …"
```

The methodology, database structure, results and silent failure modes are documented in:

**[The beef was packed in its frying oil's bottle](https://volca.run/blog/agribalyse-ingredients-packaging-extraction/)**

## Run

The script is self-contained through PEP 723. Install [uv](https://docs.astral.sh/uv/), download the official Agribalyse SimaPro CSV export, then run:

```bash
# first run: upload Agribalyse, then extract every Ciqual product
uv run extract_agribalyse_recipes.py \
  --agribalyse "/path/to/AGB32_final.CSV.zip" \
  --out agribalyse_ciqual.xlsx

# later runs reuse the uploaded database
uv run extract_agribalyse_recipes.py --out agribalyse_ciqual.xlsx

# dry run on the first 20 products
uv run extract_agribalyse_recipes.py --limit 20 --out sample.xlsx

# one row per composite-food recipe instead of per Ciqual product
uv run extract_agribalyse_recipes.py \
  --scope recipes \
  --out agribalyse_recipes.xlsx
```

The script downloads and starts the pinned VoLCA engine and reference-data bundle through [pyvolca](https://www.volca.run/docs/python/). It accepts SimaPro CSV (`.csv`, `.csv.zip`, `.7z`), EcoSpold, ILCD and Brightway/Excel (`.xlsx`) database exports.

Use `--replace` with `--agribalyse FILE` after the source database changes. Run `uv run extract_agribalyse_recipes.py --help` for every option.

## Scopes

| Scope | Subject of one product | Agribalyse 3.2 count |
|---|---|---:|
| `ciqual` (default) | Ciqual product, with its own code and packaging format | 2,451 |
| `recipes` | Composite-food recipe, which may represent several Ciqual products | 763 |

Neither scope contains the other. Seventeen recipes are reached by no Ciqual product, while many Ciqual products lead to a transformation or consumption mix rather than a recipe.

## Output

The workbook contains two sheets joined on `product_process_id`:

- `ingredients`: product identity and functional unit, ingredient name, amount, unit, role and `ingredient_process_id`;
- `packaging`: product identity and functional unit, packaging-system name and process ID, systems per functional unit, and the system reference amount and unit.

The packaging is kept as a process that an impact engine can resolve, not expanded into a material list.

## Self-check

```bash
uv run test_extract.py
```

The tests cover packaging-stage selection, proxy foods, quantities per functional unit, co-product identifiers and the distinction between no packaging stage and a stage that packs in nothing. They run without an engine or network connection.

## Versioning

`_ENGINE_VERSION` is pinned because VoLCA and pyvolca version independently and must agree on the JSON wire revision. Check the compatibility table on [pyvolca's PyPI page](https://pypi.org/project/pyvolca/) before moving the pin.

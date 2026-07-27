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

# every recipe with its ingredients and its packaging, in one sheet
uv run extract_agribalyse_recipes.py \
    --agribalyse "/path/to/AGB32_final.CSV.zip" \
    --classification 'Category=Agricultural\Food\Recipes' --limit 0 \
    --ingredients-only --packaging --out recipes_and_packaging.xlsx
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

## `--packaging`

A recipe carries no packaging — Agribalyse models it one stage downstream:

```
Pizza, … | Chilled | Cardboard | at packaging {FR}     Category = Agricultural\Food\Packaging\…
├─ 1 kg  Pizza, …, at plant {FR}                       Category = Agricultural\Food\Recipes  ← the recipe
└─ 1 kg  Pizzas, chilled, 450g | Packaging System, N0, All, Cardboard support with plastic bag
         ├─ 1 p    N1 retail elements: bag, cardboard support, two labels
         └─ 0,167 p N2/N3 grouping and logistics: cardboard box (120 boxes), pallet
```

`--packaging` finds that stage — the recipe's direct consumer under
`Agricultural\Food\Packaging` — walks the system, its components and their
materials, and appends the rows to the **same sheet** as the ingredients, with
role `packaging_material` or `packaging_eol` and one added column,
`packaging_system`. Amounts are per functional unit of the product, in kg:
a 450 g packaging system is counted 1/0,45 = 2,22 times per kg, so the pizza's
9,81 g plastic bag becomes 21,8 g/kg.

Kept: the packaging materials and their end of life. Dropped: the conversion
steps (`Cardboard finishing, cutting and folding`, `Plastic processing, Cast
film extrusion`) and the packaging's own upstream transport — a choice, not an
oversight.

Over the 763 processes of `Category=Agricultural\Food\Recipes` in Agribalyse
3.2: 741 recipes come out with a packaging bill (8 681 rows, a median of 130 g
of packaging material per kg of food), 17 have no packaging stage and 5 have a
stage that packs in nothing.

### Things to know before reading the numbers

- **The bill is read from the exchanges, not from the solved supply chain.**
  Packaging materials are authored in grams and the released engine (0.9.3)
  leaves gram-denominated links unresolved — asking the solver would silently
  return the grouping box alone, in kg, and no primary packaging at all. So each
  level is walked and scaled here, and `ingredient_process_id` stays empty on
  material rows: the database itself does not resolve their target. The rows do
  match, to the digit, what engine 0.9.4 computes once it resolves them.
- **One row per material per component**, not per material: the pizza's LDPE
  shows up twice, 21,8 g for the bag and 2,65 g for the secondary film. Sum them
  in a pivot if you want the material total.
- **One bill per distinct packaging system, not per stage.** A recipe often
  stands in for a whole family of Ciqual products — 28 breakfast cereals share
  one — and each of those products has its own packaging stage naming the same
  system. Those stages collapse into one bill; a product genuinely offered in a
  glass jar and in a plastic pot keeps both, and the run prints how many systems
  each product ended up with.
- **A packaging system belongs to a format and a subcategory.** The pizza system
  may not be reused to represent the same packaging for another product — PACK_AGB
  says so explicitly, and these inventories are not meant for comparing packaging
  solutions with each other.
- **`Pack proxy` in the stage name** means the packaging is a stand-in, not a
  measurement of that product.
- **Several stages mean several variants** (chilled and frozen, glass and PET):
  all are written, told apart by `packaging_system`.
- **Some recipes get nothing, and the run says so.** `no packaging stage` means
  no stage consumes this recipe — it happens when Agribalyse packs a proxy
  recipe instead: the stage for light mayonnaise consumes the full-fat
  mayonnaise recipe, so the light recipe has no consumer at all. `no packaging
  (No pack)` means the stage exists and packs in nothing, like raw fruit sold
  loose. Selecting the stage processes themselves (`Category=Agricultural\Food\Packaging`)
  reaches the packaging of those products, attached to the stage instead of the recipe.

## Output columns

| Column | Meaning |
|--------|---------|
| `product_process_id`, `product_name`, `location` | the transformed product |
| `functional_unit_amount`, `functional_unit` | its functional unit (typically 1 kg) |
| `ingredient_name`, `ingredient_amount`, `ingredient_unit` | one input of the recipe, or one packaging material |
| `role` | `raw_material` / `other` / `water` / … (from `classify_exchange`), or `packaging_material` / `packaging_eol` |
| `ingredient_process_id` | the ingredient's own process, to recurse further (empty on packaging materials) |
| `packaging_system` | `--packaging` only: which packaging system the row comes from, empty on ingredient rows |

## Self-check

`uv run test_packaging_rows.py` — asserts on the row builder (scaling, grams to
kilos, which exchanges are kept) and on the walk down a packaging stage (the
division by the food the stage packs), no engine and no network needed.

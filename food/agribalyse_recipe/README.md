# Agribalyse recipe extraction

Extracts every Agribalyse recipe to Excel — its **edible ingredients** and its
**packaging**, per functional unit of the product, one row each:

```
1 kg "Aioli sauce, …"  ->  0,728 kg olive oil + 0,108 kg garlic + …
                       ->  67,4 g PET + 33,7 g cardboard + …
```

`extract_agribalyse_recipes.py` is self-contained (PEP 723 inline deps, run with
[uv](https://docs.astral.sh/uv/)): it downloads the VoLCA engine binary +
reference-data bundle via [pyvolca](https://www.volca.run/docs/python/), starts
it locally, loads the Agribalyse database you point it at, and writes the rows.
You supply Agribalyse yourself — the official SimaPro CSV export is a free
public download from ADEME. The engine auto-detects the format: SimaPro CSV
(`.csv`, `.csv.zip`, `.7z`), EcoSpold, ILCD, or a Brightway/Excel export
(`.xlsx`).

The engine release is pinned (`_ENGINE_VERSION`, currently 0.9.1) rather than
tracking the latest: engine and pyvolca version independently and must agree on
the JSON wire revision, which neither version number announces — engine 0.9.3
speaks wire 3 while every released pyvolca (≤ 0.8.2) decodes wire 2, so "latest"
warns and can return rows that fail to decode. Move the pin once a pyvolca
speaking the newer wire ships. The database name, the port and the startup
timeout are constants next to it, for the same reason: none of them is a choice.

## Run

```bash
# first run: uploads the export, then extracts every recipe
uv run extract_agribalyse_recipes.py \
    --agribalyse "/path/to/AGB32_final.CSV.zip" --out recipes.xlsx

# later runs reuse the uploaded copy
uv run extract_agribalyse_recipes.py --out recipes.xlsx

# a dry run on the first 20 recipes
uv run extract_agribalyse_recipes.py --limit 20 --out sample.xlsx
```

Four options, and no selection to make: the run always covers the 763 processes
of `Category=Agricultural\Food\Recipes` — the composite foods — and always
writes both the ingredients and the packaging. `--limit N` is for a dry run and
says out loud what it left out. `--agribalyse` is only read on the first run,
which uploads the export into the engine; later runs reuse the uploaded copy
(`--replace` re-uploads it after the source file changed).

The **lifecycle stages** of those foods (`at packaging`, `at distribution`,
`at supermarket`, `at consumer`) are deliberately out: they carry logistics,
cold and retail losses, not a bill of ingredients — which is where the transport
and electricity rows came from when the whole `Agricultural\Food` branch was
extracted.

## What counts as an ingredient

Only the edible inputs; cooking, `[Dummy]` operations, waste treatment,
electricity, heat and transport are dropped. An input is kept when all three
hold:

- its producing activity is tagged **`Category type = material`** (Agribalyse's
  tag for edible materials; cooking is `processing`, energy `energy`, transport
  `transport`, end-of-life `waste treatment`),
- its role is a **food role** — `raw_material`, `other` (gram/litre dairy, sugar,
  chocolate) or `water` (recipe water is kept: its amount matters),
- its unit is a **food unit** — not `m3`, which drops the last `material`-tagged
  utilities (natural gas, compressed air).

One classification sweep builds the `material` set once; membership plus the
role and unit guards then filter each recipe.

## Packaging

A recipe carries no packaging — Agribalyse models it one stage downstream:

```
Pizza, … | Chilled | Cardboard | at packaging {FR}     Category = Agricultural\Food\Packaging\…
├─ 1 kg  Pizza, …, at plant {FR}                       Category = Agricultural\Food\Recipes  ← the recipe
└─ 1 kg  Pizzas, chilled, 450g | Packaging System, N0, All, Cardboard support with plastic bag
         ├─ 1 p    N1 retail elements: bag, cardboard support, two labels
         └─ 0,167 p N2/N3 grouping and logistics: cardboard box (120 boxes), pallet
```

The script finds that stage — the recipe's direct consumer under
`Agricultural\Food\Packaging` — then walks **upwards** into the system, its
components and their materials, stopping as soon as an input is no longer itself
a packaging (`Cardboard, Flat, Production {FR}` is a material: its mass is
written and the walk goes no further, how that cardboard is made being the
engine's business at impact time).

The walk is written twice. The `packaging_systems` sheet keeps the system as a
single process — a black box, but one an impact engine resolves on its own. The
`recipes` sheet holds the materials, next to the ingredients, with role
`packaging_material` or `packaging_eol` and the `packaging_system` column naming
where they come from — which is what you need to compare formats or change one.
Amounts are per functional unit of the product, in kg: a 450 g packaging system
is counted 1/0,45 = 2,22 times per kg, so the pizza's 9,81 g plastic bag becomes
21,8 g/kg.

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
  loose.

## `ingredient_process_id` names the co-product, not the activity

The typed `target_process_id` pyvolca reports is the target **activity**'s, and
an activity with several co-products answers with one of them arbitrarily: the
biscuit's `Palm oil, crude, consumption mix` came back as the process of `Palm
kernel oil, crude` — a different oil. Over the whole food branch that was one
resolvable target in eight (2 321 of 19 174), plus 628 rows pointing at their
own product, which would send a recursion in circles.

The raw exchange carries the pair that does name the product — `flowId` for the
product flow, `activityLinkId` for the activity producing it — and a process-id
is exactly their join, which is what this column now holds. The engine's own
solve was never affected: `get_supply_chain` places the right palm oil under the
biscuit, and `get_consumers` agrees. Only the reported identifier was ambiguous.

## Output columns

Two sheets, the same packaging seen from two heights. The first five columns are
the same in both, so they join on `product_process_id` + `packaging_system`.

**`packaging_systems`** — the packaging as a single process, one row per system.
Enough on its own if you only want an impact: the engine resolves everything
below that process id.

| Column | Meaning |
|--------|---------|
| `product_process_id`, `product_name`, `location` | the recipe |
| `functional_unit_amount`, `functional_unit` | its functional unit (typically 1 kg) |
| `packaging_system`, `system_process_id` | the packaging system it is packed in |
| `systems_per_functional_unit` | how many of it one functional unit carries — a 425 g system counts 1/0,425 = 2,35 times per kg |
| `system_reference_amount`, `system_reference_unit` | what the system is authored for (0,425 kg of packed food) |
| `material_mass_kg` | total of its material rows on the second sheet — materials only, no end of life, no pallet counted in pieces |

**`recipes`** — the detail: ingredients, and one row per packaging material.
What a model that swaps or drops a material needs.

| Column | Meaning |
|--------|---------|
| `product_process_id`, `product_name`, `location` | the recipe (product flow name, so co-products of one activity stay distinct) |
| `functional_unit_amount`, `functional_unit` | its functional unit (typically 1 kg) |
| `ingredient_name`, `ingredient_amount`, `ingredient_unit` | one edible ingredient, or one packaging material |
| `role` | `raw_material` / `other` / `water` (from `classify_exchange`), or `packaging_material` / `packaging_eol` |
| `ingredient_process_id` | the ingredient's own process, to recurse further (empty on packaging materials) |
| `packaging_system` | which packaging system the row comes from, empty on ingredient rows |

## Self-check

`uv run test_packaging_rows.py` — asserts on the row builder (scaling, grams to
kilos, which exchanges are kept), on the walk down a packaging stage (the
division by the food the stage packs), and on the product columns (co-products
of one activity told apart by their product flow name), no engine and no network
needed.

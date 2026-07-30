# Agribalyse recipe extraction

Extracts every Agribalyse recipe to Excel — its **edible ingredients** and its
**packaging**, per functional unit of the product:

```
1 kg "Aioli sauce, …"  ->  0,728 kg olive oil + 0,108 kg garlic + …
                       ->  2,35 × "Mayonnaise, 425g | Packaging System, N0, …"
```

`extract_agribalyse_recipes.py` is self-contained (PEP 723 inline deps, run with
[uv](https://docs.astral.sh/uv/)): it downloads the VoLCA engine binary +
reference-data bundle via [pyvolca](https://www.volca.run/docs/python/), starts
it locally, loads the Agribalyse database you point it at, and writes the rows.
You supply Agribalyse yourself — the official SimaPro CSV export is a free
public download from ADEME. The engine auto-detects the format: SimaPro CSV
(`.csv`, `.csv.zip`, `.7z`), EcoSpold, ILCD, or a Brightway/Excel export
(`.xlsx`).

The engine release is pinned (`_ENGINE_VERSION`, currently 0.9.3) rather than
tracking the latest: engine and pyvolca version independently and must agree on
the JSON wire revision, which neither version number announces — the
compatibility table on [pyvolca's PyPI page](https://pypi.org/project/pyvolca/)
is the authority. pyvolca ≥ 0.8.2 (required by the script) decodes wire 2 and 3,
which engine 0.9.3 speaks; move the pin together with that table. The database
name, the port and the startup timeout are constants next to it, for the same
reason: none of them is a choice.

## Run

```bash
# first run: uploads the export, then extracts every recipe
uv run extract_agribalyse_recipes.py \
    --agribalyse "/path/to/AGB32_final.CSV.zip" --out recipes.xlsx

# later runs reuse the uploaded copy
uv run extract_agribalyse_recipes.py --out recipes.xlsx

# a dry run on the first 20 recipes
uv run extract_agribalyse_recipes.py --limit 20 --out sample.xlsx

# every process of the database, not only the recipes
uv run extract_agribalyse_recipes.py --all --out agribalyse_all.xlsx
```

No selection to make: the run covers the 763 processes of
`Category=Agricultural\Food\Recipes` — the composite foods — or, with `--all`,
every process of the database, and always writes both the ingredients and the
packaging. `--limit N` is for a dry run and says out loud what it left out.
`--agribalyse` is only read on the first run, which uploads the export into the
engine; later runs reuse the uploaded copy (`--replace` re-uploads it after the
source file changed).

Nothing needs to be installed beyond [uv](https://docs.astral.sh/uv/) and the
Agribalyse export: the script itself fetches its Python dependencies and the
VoLCA engine binary on first run.

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
```

The script sweeps those stages once — every process under
`Agricultural\Food\Packaging` — and each one names the food it packs and the
system it packs it in. A product then only looks itself up. Reading it in this
direction, rather than asking each product "who packs you?", is what makes the
sheet complete: over the whole database the question-per-product version lost
8 products and invented 236 rows (see below).

The system is written as a single process: a black box, but one an impact engine
resolves on its own. `systems_per_functional_unit` says how many of it one
functional unit of the product carries — a 450 g system is counted 1/0,45 = 2,22
times per kg — obtained by dividing the stage's system amount by the food amount
it packs, not by assuming the 1 kg for 1 kg Agribalyse happens to use.

Over Agribalyse 3.2: 2 286 packaging stages, giving 1 540 (product, system)
pairs. Restricted to the 763 recipes, 746 of them come out with a packaging
system, for 820 rows.

### Telling a packaging from a packed food

Both sit under `Agricultural\Food\Packaging`, so the branch alone cannot separate
them. PACK_AGB files the systems and their elements under a **dotted segment**
(`.Packaging systems`, `.Packaging II and III`) and the stages under the food
families, which is the distinction the script uses — 1 558 dotted processes, and
not one of them consumes a food, so the rule holds throughout the database.

It is load-bearing because reading "under Packaging" as "is a packaging" fails
twice:

- a stage's food can itself be an `at packaging` process, Agribalyse packing the
  wholemeal sandwich by consuming the French-bread sandwich as a proxy. Both
  inputs then look like packaging, the stage is left with no food, and 8 products
  (sandwiches, Petit-Suisse, margarines) silently lost their packaging;
- and a packaging system, asked "who packs you?", answers with the stage that
  consumes it — which yielded 236 rows stating that a packaging system is packed
  in itself.

### Things to know before reading the numbers

- **One row per distinct packaging system, not per stage.** A recipe often
  stands in for a whole family of Ciqual products — 28 breakfast cereals share
  one — and each of those products has its own packaging stage naming the same
  system. Those stages collapse into one row; a product genuinely offered in a
  glass jar and in a plastic pot keeps both, and the run prints how many systems
  each product ended up with.
- **The quantity is per functional unit, which is not always a kilo.** Dried
  grain maize is billed by the ton, so its 10 bags per kg are written
  10 000 — read `systems_per_functional_unit` next to `functional_unit_amount`,
  never alone.
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

Two sheets. The first five columns are the same in both, so they join on
`product_process_id`.

**`ingredients`** — one row per edible ingredient.

| Column | Meaning |
|--------|---------|
| `product_process_id`, `product_name`, `location` | the recipe (product flow name, so co-products of one activity stay distinct) |
| `functional_unit_amount`, `functional_unit` | its functional unit (typically 1 kg) |
| `ingredient_name`, `ingredient_amount`, `ingredient_unit` | one edible ingredient |
| `role` | `raw_material` / `other` / `water` (from `classify_exchange`) |
| `ingredient_process_id` | the ingredient's own process, to recurse further |

**`packaging`** — the packaging as a single process, one row per system.
Enough on its own for an impact: the engine resolves everything below that
process id.

| Column | Meaning |
|--------|---------|
| `product_process_id`, `product_name`, `location` | the recipe |
| `functional_unit_amount`, `functional_unit` | its functional unit (typically 1 kg) |
| `packaging_system`, `system_process_id` | the packaging system it is packed in |
| `systems_per_functional_unit` | how many of it one functional unit carries — a 425 g system counts 1/0,425 = 2,35 times per kg |
| `system_reference_amount`, `system_reference_unit` | what the system is authored for (0,425 kg of packed food) |

## Self-check

`uv run test_extract.py` — asserts on the walk down a packaging stage
(the division by the food the stage packs), on the corrected ingredient target
ids, and on the product columns (co-products of one activity told apart by
their product flow name), no engine and no network needed.

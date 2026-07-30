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
is the authority. pyvolca ≥ 0.9.0 (required by the script — it is the release
whose supply-chain entries carry `depth`) decodes wire 2 to 4, which engine
0.9.3 speaks; move the pin together with that table. The database
name, the port and the startup timeout are constants next to it, for the same
reason: none of them is a choice.

## Run

```bash
# first run: uploads the export, then extracts every Ciqual product
uv run extract_agribalyse_recipes.py \
    --agribalyse "/path/to/AGB32_final.CSV.zip" --out agribalyse_ciqual.xlsx

# later runs reuse the uploaded copy
uv run extract_agribalyse_recipes.py --out agribalyse_ciqual.xlsx

# a dry run on the first 20 products
uv run extract_agribalyse_recipes.py --limit 20 --out sample.xlsx

# one row per recipe instead of one per product
uv run extract_agribalyse_recipes.py --scope recipes --out agribalyse_recipes.xlsx
```

`--scope` says what a row is *about*, and both sheets are always written:

| Scope | Rows | What it is |
|-------|------|------------|
| `ciqual` (default) | the 2 451 products of the Ciqual table | one row per product, each with its own Ciqual code and its own packaging format |
| `recipes` | the 763 composite foods | one row per recipe, which stands in for a whole family of Ciqual products |

**Neither covers the other.** A Ciqual product carries a code, a single packaging
format and the ingredients of the food at the bottom of its lifecycle chain — a
real recipe for most of them, a transformation or a consumption mix for the
rest, an apple having no recipe to speak of. But 17 recipes are reached by no
Ciqual product at all, 13 of them carrying ingredients: rice noodles,
reconstituted broths, the ITK yogurt variants, snail in parsley butter, anchovy
fillets, grated carrots. Those are only visible under `--scope recipes`.

`--limit N` is for a dry run and says out loud what it left out. `--agribalyse`
is only read on the first run, which uploads the export into the engine; later
runs reuse the uploaded copy (`--replace` re-uploads it after the source file
changed).

Nothing needs to be installed beyond [uv](https://docs.astral.sh/uv/) and the
Agribalyse export: the script itself fetches its Python dependencies and the
VoLCA engine binary on first run.

The **lifecycle stages** of those foods (`at packaging`, `at distribution`,
`at supermarket`, `at consumer`) carry logistics, cold and retail losses, not a
bill of ingredients: their own inputs are never read as a recipe. `--scope
ciqual` starts from the last of them, the `at consumer` process being the Ciqual
product itself, but reads both sheets off the food at the bottom of its chain.

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

These guards isolate a recipe because Agribalyse authors one as a clean bill of
ingredients. They do not travel that far: on a process copied from ecoinvent
(`yogurt production`) the very same guards keep the whole factory inventory —
the Petit-Suisse comes out with 39 rows of which one, its 1,13 kg of cow milk,
is an ingredient, the other 38 being the cleaning station (nitric acid, soda,
EDTA) and the refrigerant. The `ciqual` scope stays clear of that by construction:
it reads the food its packaging stage names, which is a recipe, a transformation
or a consumption mix, never a factory.

Amounts are per the functional unit the row declares. A recipe labels its own
rows, so nothing is rescaled there; a Ciqual product reads a food that may be
billed on another unit — the white wine mix per 0,75 kg, the grain maize per
ton — and the amounts are brought back to the product's kilo.

## Packaging

A recipe carries no packaging — Agribalyse models it one stage downstream:

```
Pizza, … | Chilled | Cardboard | at packaging {FR}     Category = Agricultural\Food\Packaging\…
├─ 1 kg  Pizza, …, at plant {FR}                       Category = Agricultural\Food\Recipes  ← the recipe
└─ 1 kg  Pizzas, chilled, 450g | Packaging System, N0, All, Cardboard support with plastic bag
```

The script asks each product for its **supply chain filtered on that branch** —
one engine call, and the `depth` each entry carries (pyvolca ≥ 0.9.0) names the
product's own stage: the shallowest undotted entry. A recipe is asked the other
way round, its packaging sitting among its direct **consumers**. Either way the
quantities are then read off the stage's own bill, not off the solver: the
division stays exact, and the two silent failures a naive reading produced —
8 products losing their packaging, 236 invented rows — stay guarded (see
below).

The system is written as a single process: a black box, but one an impact engine
resolves on its own. `systems_per_functional_unit` says how many of it one
functional unit of the product carries — a 450 g system is counted 1/0,45 = 2,22
times per kg — obtained by dividing the stage's system amount by the food amount
it packs, not by assuming the 1 kg for 1 kg Agribalyse happens to use.

Over Agribalyse 3.2: 2 286 packaging stages, giving 1 540 (product, system)
pairs. Restricted to the 763 recipes, 746 of them come out with a packaging
system, for 820 rows.

### A Ciqual product is three stages downstream

The product of the Ciqual table — the one carrying `[Ciqual code: 11168]` in its
name — is the `at consumer` process, and nothing packs *it*: its packaging sits
back up its own lifecycle chain.

```
Aioli … | at consumer {FR} [Ciqual code: 11168]     Preparation
└─ 1,05 kg  Aioli … | at retail {FR}                Retail
   └─ 1,01 kg  Aioli … | at distribution {FR}       Distribution
      └─ 1 kg  Aioli … | at packaging {FR}          Packaging   ← the stage
         ├─ 1 kg  Mayonnaise, 425g | Packaging System, N0, All, Plastic squeeze
         └─ 1 kg  Aioli sauce …, recipe, at plant {FR}
```

`--scope ciqual` reads that chain in one filtered call, four levels deep — the
bound the chain above shows — and takes the shallowest undotted entry: the
product's own stage always sits above whatever packaging its ingredients carry.
That ranking is load-bearing. A product cooked *at consumer level* (the
pan-fried beef, the falafel, the puree reconstituted with milk) consumes its
raw product **and** its frying fat, both with a packaging stage upstream — and
the walk that took the first stage found, instead of the shallowest, shipped
78 such products packed in their oil's bottle, the fried beef's "ingredients"
being the sunflower oil's. All 2 451 Ciqual products reach their stage — 2 294
with a system, 157 packed in nothing.

The quantity stays **per kilo of the packed food**, so the aioli reads
2,35 systems and not the 2,61 that folding in the 11 % of retail and consumer
losses would give. That is the number Ecobalyse expects, applying those losses
itself.

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
  (sandwiches, Petit-Suisse, margarines) silently lost their packaging — the
  dotted test in `stage_bill` is what keeps the proxy readable as a food;
- and a packaging system read as a stage comes out packed in itself — 236 such
  rows the day every process was swept. In the filtered chain the system can
  even be the shallowest entry; `stage_of` never lets a dotted entry be the
  stage.

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
`product_process_id`. They name the extracted product — the recipe under
`--scope recipes`, the Ciqual product under `--scope ciqual` — while the
ingredients and the packaging both describe the food that product is.

**`ingredients`** — one row per edible ingredient.

| Column | Meaning |
|--------|---------|
| `product_process_id`, `product_name`, `location` | the product (product flow name, so co-products of one activity stay distinct) |
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

`uv run test_extract.py` — asserts on the reading of a packaging stage (the
division by the food the stage packs, the stage picked as the shallowest
undotted entry of the filtered chain), on the corrected ingredient target ids,
and on the product columns (co-products of one activity told apart by their
product flow name), no engine and no network needed.

# ecoinvent to BAFU mapping, impact by impact

`compare_mapping.py` reads a mapping workbook that pairs ecoinvent 3.11 processes with
BAFU 2026 processes, scores both processes of every pair on EF 3.1, and shows how far
apart they are: one scatter per impact category plus the ECS and PEF single scores, each
point one pair, coloured by the technical grade the workbook gives it.

```bash
cd ecobalyse-method-tooling/bafu/ei_bafu_mapping
uv run compare_mapping.py mapping.xlsx \
  --ecoinvent Ecoinvent3.11.CSV.zip \
  --bafu "BAFU-2026 v1_ecoSpold v1.zip" \
  --method "Environmental Footprint 3.1 (adapted).1.03.CSV.zip" \
  --top 30
```

It stands on its own: it downloads the engine release pinned in the script (`_ENGINE_VERSION`), starts it on a free port,
loads both databases from their files and stops it at the end. `VOLCA_BINARY` (with
`VOLCA_DATA_DIR`) runs a local build instead.

Linux, macOS and Windows alike: `uv` brings its own Python, and the engine release
carries a build for each. On Windows, run the command from PowerShell, on one line.

## What it reads

- **The workbook**, sheet `Matching Full` by default (`--sheet`). The script checks the
  header of the columns it reads and stops if the layout differs. Its `conversion`
  column gives one BAFU unit in ecoinvent units (0.2778 kWh for one MJ, 1200 kg for one
  car). The script reads it only between units of different dimensions, and only when
  the two units the workbook writes measure what the engine reads on each side: a
  factor written between other units would be applied to the wrong quantity.
- **ecoinvent 3.11 as a SimaPro CSV export** (`--ecoinvent`): the workbook writes its
  names the SimaPro way, `product {GEO}| activity`, which the export carries with a
  ` | Cut-off, U` suffix.
- **BAFU 2026 as its EcoSpold 1 archive** (`--bafu`), joined on the name and location
  the workbook gives.
- **A method collection** (`--method`), any SimaPro method export, zipped or as the
  plain CSV. Ecobalyse uses EF 3.1 adapted 1.03. The ECS
  and PEF single scores are declared on top of it inside the script, on EF 3.1 category
  names, so another family of method needs those two blocks rewritten. This ECS is the
  Ecobalyse weighting on EF 3.1 alone, without Ecobalyse's own corrections: it ranks the
  pairs, it is not the figure the ecobalyse pipeline publishes.

## What it writes

Next to the workbook, named after it and the sheet, plus ` - long-term` when the run
counts long-term emissions, so the two conventions never overwrite each other:

- `… - Matching Full.svg`: the scatters. x is the ecoinvent process, y the BAFU process,
  both for one unit of the ecoinvent product. The engine's unit table converts units that
  measure the same thing; between units that do not (m3 of gas against MJ, a car counted
  in units against kg), the workbook's `conversion` column does, and a pair it leaves
  empty is left out. The grey bands are ±10% and
  a factor 2. The three widest gaps among well graded pairs are named on each graph.
- `… - Matching Full.csv`: every distinct pair, with a `status` saying whether it was
  compared or why not (a name absent from a database, units of different dimensions
  with no conversion in the workbook), a `conversion` column saying where the unit
  factor came from, `unit table` or `sheet`, and both scores of every category.

The terminal report counts what was compared, then gives per category the median
BAFU / ecoinvent ratio, how many pairs sit within 10%, within a factor 1.5, 2 and 10
(a factor up counting like the same factor down), and how often BAFU is the lower of the
two. It ends with the `--top` widest gaps on ECS and on climate change, the two
indicators the workbook itself tracks. Statistics and gap lists only count pairs graded
`--min-grade` (2) or more: a gap on a pair the workbook grades "no match" says nothing
about the mapping.

## Reading the numbers

- Long-term emissions are excluded by default, as EF prescribes, and `--long-term`
  counts them. Both databases mark them the same way, so the switch moves the two sides
  of a pair together; it matters when holding one side against published results, which
  usually include them. Excluded, freshwater eutrophication and ionising radiation come
  out a fifth and a quarter of what BAFU publishes; with `--long-term`, both land on it.
- Which version of the method to pass depends on what the comparison is for. On its own
  the choice moves both sides together. Against BAFU's published results, pass the
  adapted 1.05 they used: water use goes from 0.829 to 0.993 of the published figure,
  particulate matter and marine eutrophication to 1.000.
- From adapted 1.05 on, the whole "Ecotoxicity, freshwater" category carries almost no
  factor of its own, its organics and inorganics parts carrying them instead. The two
  single scores declared here read the parts; the whole-category column stays in the
  output and scores near zero with that version.
- Photochemical ozone formation reads about 0.72 of the published figure on the BAFU
  side, for a reason outside this script: the EcoSpold 1 database names its NMVOC flow
  with an origin suffix the method does not use, and the engine's curated flow registry
  does not bridge the two, so the flow reaches no characterization factor. Bridging the
  two spellings brings the median to 1.001.
- The ecoinvent side agrees with Brightway on the ecoinvent 3.11 processes Ecobalyse
  publishes.
- Land use climate change was unreadable on the BAFU side up to v0.12.0: the engine gave
  BAFU's fossil and biogenic methane the factor of land transformation methane, matched
  through their shared CAS number, which put that sub-category hundreds of times above
  what BAFU publishes. From v0.13.0 a factor whose name matches no flow stays unmatched
  where the registry says the method named another substance. Measured against the
  published table on sixty processes under EF 3.1 adapted 1.03: the median ratio is
  1.0000 on that sub-category and on the climate change total, and sixty of sixty sit
  inside the one percent band.

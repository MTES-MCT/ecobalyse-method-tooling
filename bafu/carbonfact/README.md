# Carbonfact processes, with BAFU in the background

Carbonfact publishes eight Brightway workbooks, one per textile process step
(spinning, knitting, weaving, dyeing, printing, finishing, assembly, use
phase), each naming its suppliers in ecoinvent 3.12 cut-off.
`carbonfact_bafu.py` gathers every step into one database, Carbonfact, and
loads it twice: as published, on ecoinvent, and relinked onto BAFU through an
ecoinvent 3.11 to BAFU mapping. It scores every process on both, draws one
against the other, and can export the BAFU version for another tool.

[README.fr.md](README.fr.md) says the same in French.

## What you need

- **The Carbonfact folder** (`Carbonfact database`, one sub-folder per step).
- **The mapping**, from ecoinvent 3.11 to BAFU, either:
  - the expert workbook (`.xlsx`), sheet `Matching Full`, its columns found by
    their title, in this order: `EI 3.11 dataset`, `BAFU Name`, `Geography`,
    `unit`, `conversion`, whatever other columns lie between;
  - or the mapping exported from VoLCA's mapping page (`.csv`), made with
    ecoinvent 3.11 as source. Only decided rows are read: a suggestion nobody
    accepted links nothing.
- **ecoinvent 3.12 cut-off**, its ecoSpold02 archive. ecoinvent ships it as
  `.7z`, which needs [7-Zip](https://www.7-zip.org/) installed; a `.zip` of the
  same files needs nothing.
- **BAFU**, its ecoSpold v1 archive, the `.zip` as BAFU distributes it.
- **The impact method**, EF 3.1 adapted exported from SimaPro as CSV: adapted
  1.03 is the one Ecobalyse uses.
- Optional, **ecoinvent's correspondence file**, `Correspondence File v3.11 -
  v.3.12.xlsx` (`--correspondence`). The mapping is written against 3.11; without
  this file a 3.12 supplier is looked up under the same name, and the script
  lists the ones it misses.

The script needs no other installation than `uv`, which brings its own Python
and the libraries the script declares; the script downloads the VoLCA engine
itself.

## Install, once (Windows)

Open PowerShell (Start menu, type `PowerShell`) and install `uv`:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close PowerShell and open it again, so it finds `uv`. On Linux or macOS,
`curl -LsSf https://astral.sh/uv/install.sh | sh` installs it.

## Run

In PowerShell, on one line, each path in quotes, none ending with a `\`
(PowerShell would read it as escaping the closing quote):

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py "C:\data\Carbonfact database" "C:\data\mapping.xlsx" --ecoinvent "C:\data\ecoinvent 3.12_cutoff_ecoSpold02.7z" --bafu "C:\data\BAFU-2026 v1_ecoSpold v1.zip" --method "C:\data\Environmental Footprint 3.1 (adapted).1.03.CSV.zip" --correspondence "C:\data\Correspondence File v3.11 - v.3.12.xlsx"
```

`uv` fetches the script from this repository, so there is nothing to download
by hand; each run takes the latest version. To keep one version, replace `main`
in the address by a commit number. From a copy of the repository, run
`uv run carbonfact_bafu.py` in this folder instead.

The first run downloads the engine, and the engine reads ecoinvent from its
archive, which takes several minutes. It unpacks each archive into a folder
beside it, named after it with `.d` added, and keeps what it read there, so the
next runs take a couple of minutes.

Options, all optional:

| Option | Effect |
|---|---|
| `--correspondence FILE` | look a 3.12 supplier up under its 3.11 name |
| `--complement FILE` | your own BAFU choices, in the columns of `mapping.csv`, instead of the ones built into the script (below); they win over the mapping |
| `--export FORMAT` | also write the BAFU version as `brightway`, `simapro`, `ecospold2` or `openlca` |
| `--no-quality` | skip the quality report |
| `--out FOLDER` | where to write, by default `<Carbonfact folder> - BAFU` beside it |
| `--top N` | how many of the widest gaps to list, 20 by default |

`VOLCA_BINARY` with `VOLCA_DATA_DIR`, set as environment variables, run a local
engine build instead of the downloaded one.

## What it writes

In `<Carbonfact folder> - BAFU`:

- `mapping.csv`: for every ecoinvent supplier, the BAFU process it is relinked
  to, `opposite` in `direction` where BAFU counts it the other way (below), the
  3.11 dataset the mapping was read under, and a `status` saying why a supplier
  has none;
- `comparison.csv`: each activity, its step, the climate change figure Carbonfact
  reports for it (blank for the thirteen it reports none for: the heat mix, the
  dyes, the inks), then every category and the ECS score with ecoinvent in the
  background and with BAFU;
- `comparison.svg`: one scatter per category, ecoinvent background across, BAFU
  background up, coloured by step; the grey bands are ±10 % and a factor 2;
- `quality.csv`: the engine's quality report of the BAFU version;
- with `--export`, `carbonfact.<format>.<ext>`: the BAFU version, each input written
  under the BAFU process it is linked to, in a database named after the BAFU
  archive (`bafu-2026-v1-ecospold-v1` for `BAFU-2026 v1_ecoSpold v1.zip`). Read
  again beside BAFU under that name, it links to the same suppliers with no
  mapping, and gives the same scores.

The terminal says how close the ecoinvent version comes to Carbonfact's own
figures, then per category the median ratio BAFU / ecoinvent and the share of
activities within 10 %, a factor 1.5, 2 and 10, and ends with the widest gaps
on ECS and on climate change.

## What was measured

With the expert mapping of 28 October 2026 as it comes, the built-in choices
(below) and EF 3.1 adapted 1.03: on ecoinvent, climate change is within
0.03 % of the figure Carbonfact reports, on each of the 294 activities it
reports one for. On BAFU, the median ratio BAFU / ecoinvent is 0.84 on climate
change, with 99 % of the activities within a factor 1.5, and
0.77 on ECS, where finishing and dyeing processes come out nine to twenty-five
times lower than on ecoinvent.

That gap is almost all freshwater ecotoxicity, inorganics, about a hundred
times lower on BAFU for « Fabric finishing, Softening, Exhaust, Jet ». It comes
from ecoinvent's own wastewater market for the rest of the world, which sends
part of the water untreated to the river with the chlorine it carries, a flow
EF 3.1 weighs heavily. BAFU treats the same water in a Swiss plant. The gap is
a difference between the two databases, not an error of the relink.

Water use comes out slightly negative on BAFU for the washing and hand washing
of the use phase. It was already so before the direction was read, and is not
examined here.

## When it stops

- **`none for … : not in the mapping`**, followed by **`Name a BAFU supplier for
  each in a --complement file`**: some ecoinvent suppliers have no BAFU
  equivalent. Copy `mapping.csv`, fill `target_product` and `target_location`
  for those rows, and pass it with `--complement`. When the status adds
  `--correspondence may find it`, pass the correspondence file first.
- **`no column … after …`** or **`two columns …`**: the workbook's titles
  changed; the message names the column it misses, or the title found twice,
  which must be renamed on the column not to read.
- **`… differs between the steps …`**: one activity name is written two
  different ways in two workbooks; the script will not pick one.
- **`VoLCA … is too old`**: this needs an engine more recent than the latest
  release. Each run downloads the latest one, so it resolves itself once it is
  out.
- **`7z extraction failed`**: install 7-Zip, or pass a `.zip`.
- **`CERTIFICATE_VERIFY_FAILED` while downloading the engine**: point Python at
  the certificates `certifi` ships, then run again:

  ```powershell
  $env:SSL_CERT_FILE = (uv run --with certifi python -c "import certifi; print(certifi.where())")
  ```

- **`Engine exited with code 3221225781`** (Windows): a DLL the engine needs is
  missing; install the latest Microsoft Visual C++ Redistributable x64
  (`vc_redist.x64.exe`).

## The choices built into the script

The steps use 112 distinct ecoinvent suppliers. The script carries a BAFU choice
for the twelve below, and uses it whatever the workbook says, unless
`--complement` gives others. The workbook decides for the other hundred. The
choices are **proposals, not validated**: each should be checked by someone who
knows both databases, and the mapping corrected upstream where it can be.

Each row is there because the workbook of 9 October 2026
(`mapping.20261009.xlsx`) has nothing for it. The workbook of 28 October 2026
fills several of them with an automatic proposal (`Semantic2`, its least
reliable source), and two of those were far off: the kettle went to « Household
devices production plant » {CH}, a whole factory per appliance, which made
ironing about 290 000 times higher than on ecoinvent, and the dryer to
« Technical wood drying, infrastructure » {RER}, seven times higher. The
choices below win over such proposals, so the script gives the same result
whichever workbook it is given. The appliance is a small share of these
activities anyway: one minute of ironing uses 0.027 kWh, which weighs far more
than the iron.

| ecoinvent 3.12 supplier | why `mapping.20261009.xlsx` has nothing | BAFU choice | strength |
|---|---|---|---|
| market for alkyl sulfate (C12-14) {GLO} | mapping says #N/A | Fatty alcohol sulfate, mix, at plant {RER} | close |
| market for chemical, organic, unspecified {GLO} | new in 3.12, no 3.11 dataset | Chemicals organic, at plant {GLO} | what the mapping gives for the 3.11 « chemical, organic » |
| market for polytetrafluoroethylene {GLO} | new in 3.12, no 3.11 dataset | Tetrafluoroethylene, at plant {RER} | proxy: the monomer, not the polymer |
| market for dryer {GLO} | mapping says #N/A | Washing machine, at retailer {CH} | weak proxy: BAFU has no dryer |
| market for electric kettle {GLO} | mapping says #N/A | Convector radiator {CH}, 1.2 kg per appliance | weak proxy for the iron: BAFU has no small appliance; the electricity, not the iron, makes most of ironing |
| market for tissue paper {GLO} | mapping says #N/A | Paper, woodfree, uncoated, at regional storage {RER} | close |
| market for waste mineral oil {RoW} | mapping says #N/A | Disposal, used mineral oil, 10% water, to hazardous waste incineration {CH} | close |
| market for wastewater from textile production {RoW} | mapping says #N/A | Treatment, sewage, to wastewater treatment, class 3 {CH} | same as the mapping's « wastewater, average » |
| market for cationic resin {RoW} | typo in the mapping: « Cationic resin, at plan », no location | Cationic resin, at plant {CH} | the mapping's intent |
| market for polyethylene, high density, granulate {RER} | split by region in 3.12, the mapping has the 3.11 {GLO} | Polyethylene, HDPE, granulate, at plant {RER} | the mapping's choice for {GLO} |
| market for polypropylene, granulate {RER} | split by region in 3.12, the mapping has the 3.11 {GLO} | Polypropylene, granulate, at plant {RER} | the mapping's choice for {GLO} |
| market for polypropylene, granulate {RoW} | split by region in 3.12, the mapping has the 3.11 {GLO} | Polypropylene, granulate, at plant {RER} | the mapping's choice for {GLO} |

## Mappings to review

The mapping links the following suppliers, and Carbonfact uses them, but the
BAFU process it names is a different product or a different service. The script
applies them as they are; they are listed so the mapping can be improved. The
list was read by hand against the workbook of 28 October 2026.

Clearly a different product:

| ecoinvent supplier | BAFU process the mapping gives |
|---|---|
| market for enzymes {GLO}, enzymes production {RER} | Potato starch, at plant {DE} |
| market for aluminium chloride {GLO} | Aluminium fluoride, at plant {RER} |
| market for citric acid {GLO} | Sulphuric acid, liquid, at plant {RER} |
| market for copper sulfate {GLO} | Copper oxide, at plant {RER} |
| market for cyanuric chloride {GLO} | Hydrogen cyanide, at plant {RER} |
| market for magnesium chloride, from titanium sponge production {GLO} | Titanium dioxide, chloride process, at plant {RER} |
| market for naphthalene sulfonic acid {GLO} | Polyphenylene sulfide, at plant {GLO} |
| market for o-phenylene diamine {GLO} | Polyphenylene sulfide, at plant {GLO} |
| market for polyacrylamide {GLO} | Polylactide, granulate, at plant {GLO} |
| market for sodium amide {RoW} | Sodium chloride, powder, at plant {RER} |
| market for sodium nitrite {RoW} | Sodium dichromate, at plant {RER} |

Proxies worth a second look:

| ecoinvent supplier | BAFU process the mapping gives | doubt |
|---|---|---|
| market for cooling energy {GLO} | Heat, from natural gas {AT} | cooling is not heat; the mapping's factor 0.599 is between two energy units, so the engine converts MJ to MJ at 1 and does not apply it |
| market for 2-nitroaniline {GLO} | Dinitroaniline-compounds, at regional storehouse {RER} | a family of herbicides, not the dye intermediate |
| market for sodium hydrosulfide {GLO} | Hydrogen sulphide, H2S, at plant {RER} | precursor, not the product |
| market for methacrylic acid {GLO} | Methyl methacrylate, at plant {RER} | the ester, not the acid |
| market for carton board box production, with gravure printing {GLO} | Packaging, corrugated board, mixed fibre, single wall, at plant {RER} | no printing |
| market for printed paper {GLO} | Paper, newsprint, at plant {CH} | no printing |
| market for waste packaging paper {RoW} | Waste paper, mixed, from public collection, for further treatment {RER} | a collected material, not a waste treatment |
| market for waste polypropylene {RoW} | xx Disposal, polypropylene, 15.9% water, to sanitary landfill {CH} | a BAFU name starting with « xx », worth asking BAFU why |
| market for ethanol, … from fermentation {RoW} | Ethanol, 95% in H2O, from sugar cane, at fermentation plant {BR} | one feedstock and one country for the rest of the world |

## Checking the script

`uv run test_carbonfact_bafu.py` checks the workbook reader and the order
between the built-in choices and the mapping, without the engine.

## How it works

- **One database.** Every workbook's activities go into one Brightway workbook
  named Carbonfact. An activity several steps share (the heat mix, four dyes) is
  kept once; the steps write it with different unit spellings (MJ, megajoule) and
  capitals, which the script reads as the same, and any other difference stops
  the run.
- **Relinking.** Each ecoinvent supplier is walked back to 3.11 (with the
  correspondence file, else under its own name), looked up in the built-in
  choices and, where they have nothing, in the mapping. Two ecoinvent
  suppliers share one product and place (« heat, district or industrial, other
  than natural gas » {RoW}, from hard coal and from light fuel oil) but map to
  different BAFU processes; a relink keys an input on its product and place, so
  the BAFU version names its ecoinvent inputs after their activity instead.
- **Direction.** ecoinvent counts a waste as a negative output of its market
  (« market for wastewater from textile production », −1 m³), BAFU as the
  positive output of a treatment (« Treatment, sewage, … class 3 », +1 m³). A
  line keeping ecoinvent's sign would have BAFU treat a negative volume and
  score credits. Once both databases are loaded, the script asks each the
  reference amount of every supplier and its BAFU process, and marks
  `opposite` the rows where the signs differ; the engine turns those lines. A
  mapping exported from VoLCA's mapping page already carries this column.
- **ECS** is Ecobalyse's weighting on EF 3.1 alone, without Ecobalyse's own
  corrections. The script adds it as a scoring set to a copy of the method once
  the engine has started.
- The relink lives in the engine's memory, which the script stops at the end:
  `--export` is the way to keep the BAFU version.

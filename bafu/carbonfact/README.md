# Carbonfact processes, with BAFU in the background

Carbonfact publishes eight databases in the Brightway format (one `.xlsx`
workbook each), one per textile process step: spinning, knitting, weaving,
dyeing, printing, finishing, assembly, use phase. Each names its suppliers in
ecoinvent 3.12 cut-off, and none calls another's processes: shared processes,
such as the heat mix, are copied into every database that uses them.
`carbonfact_bafu.py` therefore works one database at a time, and a new
database goes through it the same way as the first eight; only `merge` takes
several. It has four independent commands:

| Command | What it does | What it writes |
|---|---|---|
| `relink` | puts a database on BAFU: each ecoinvent supplier replaced by its BAFU equivalent | the database on BAFU, and the mapping it applied |
| `compare` | scores two versions of a database, each on its own background, one against the other | a table, a figure, a summary in the terminal |
| `quality` | the engine's structural quality report of a database | a table of the defects found |
| `merge` | writes several databases as one, each activity they share once | the merged database |

Most of the time, the three run in a row on one database: `relink`, then
`compare` between the published database and its BAFU version, then `quality`
on the BAFU version. `merge` comes last, when one database of every step is wanted.

[README.fr.md](README.fr.md) says the same in French.

## Install, once (Windows)

The script needs no other installation than `uv`, which brings its own Python
and the libraries the script declares; the script downloads the VoLCA engine
itself.

Open PowerShell (Start menu, type `PowerShell`) and install `uv`:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close PowerShell and open it again, so it finds `uv`. On Linux or macOS,
`curl -LsSf https://astral.sh/uv/install.sh | sh` installs it.

## Run

Each command runs in PowerShell, on one line, each path in quotes, none ending
with a `\` (PowerShell would read it as escaping the closing quote). The
examples take the dyeing database.

`uv` fetches the script from this repository, so there is nothing to download
by hand; each run takes the latest version. To keep one version, replace `main`
in the address by a commit number. From a copy of the repository, run
`uv run carbonfact_bafu.py` in this folder instead.

The first run downloads the engine, and the engine reads ecoinvent from its
archive, which takes several minutes. It unpacks each archive into a folder
beside it, named after it with `.d` added, and keeps what it read there, so the
next runs take seconds.

Each command writes beside the database it is given, under that database's
name followed by what it made; `--out FOLDER` writes elsewhere.
`uv run carbonfact_bafu.py <command> --help` describes every option.

### `relink`: putting a database on BAFU

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py relink "C:\data\Dyeing_Inventory_Brightway.xlsx" --mapping "C:\data\mapping.xlsx" --ecoinvent "C:\data\ecoinvent 3.12_cutoff_ecoSpold02.7z" --bafu "C:\data\BAFU-2026 v1_ecoSpold v1.zip"
```

It needs:

- **the database**, a Brightway workbook whose suppliers are in ecoinvent 3.12
  cut-off;
- **the mapping** from ecoinvent 3.11 to BAFU (`--mapping`), either:
  - the expert workbook (`.xlsx`), sheet `Matching Full`, its columns found by
    their title, in this order: `EI 3.11 dataset`, `BAFU Name`, `Geography`,
    `unit`, `conversion`, whatever other columns lie between;
  - or the mapping exported from VoLCA's mapping page (`.csv`), made with
    ecoinvent 3.11 as source. Only decided rows are read: a suggestion nobody
    accepted links nothing;
- **ecoinvent 3.12 cut-off** (`--ecoinvent`), its ecoSpold02 archive. ecoinvent
  ships it as `.7z`, which needs [7-Zip](https://www.7-zip.org/) installed; a
  `.zip` of the same files needs nothing;
- **BAFU** (`--bafu`), its ecoSpold v1 archive, the `.zip` as BAFU distributes
  it;
- only when the script asks for it, **ecoinvent's correspondence file**,
  `Correspondence File v3.11 - v.3.12.xlsx` (`--correspondence`). The mapping is
  written against 3.11, so a supplier renamed in 3.12 is not found under its
  new name. The script carries the renames the eight databases need (below)
  and looks up every other supplier under its own name; when one is missing,
  it stops and suggests this file, which then replaces the built-in renames.

It writes:

- `Dyeing_Inventory_Brightway.bafu.brightway.xlsx`: the database on BAFU, each
  input written under the BAFU process it is linked to, attached to a database
  named after the BAFU archive (`bafu-2026-v1-ecospold-v1` for `BAFU-2026
  v1_ecoSpold v1.zip`). Read again beside BAFU under that name, it links to the
  same suppliers with no mapping, and gives the same scores. `--format simapro`,
  `ecospold2`, `openlca` or `ilcd` writes it in another format, and the name
  follows: `….bafu.simapro.csv`, `….bafu.ecospold2.zip`, `….bafu.openlca.zip`,
  `….bafu.ilcd.zip`. The ILCD package names each BAFU supplier by its product
  and its location, which is how a reader finds it again beside BAFU;
- `Dyeing_Inventory_Brightway.bafu.mapping.csv`: for each ecoinvent supplier,
  the BAFU process it is linked to, `opposite` in `direction` when BAFU counts
  it the other way (below), the 3.11 dataset the mapping was read under, and a
  `status` saying why a supplier has none.

`--complement FILE` gives your own BAFU choices, in the columns of that file,
instead of the ones built into the script (below); like those, they fill only
what the mapping leaves without a BAFU process.

### `compare`: two versions of a database, one against the other

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py compare "C:\data\Dyeing_Inventory_Brightway.xlsx" "C:\data\Dyeing_Inventory_Brightway.bafu.brightway.xlsx" --ecoinvent "C:\data\ecoinvent 3.12_cutoff_ecoSpold02.7z" --bafu "C:\data\BAFU-2026 v1_ecoSpold v1.zip" --method "C:\data\EF-v3.1.zip" --reported "C:\data\Carbonfact_Dyeing_DB_LCIA.csv"
```

The two versions can be in any format the engine reads, not necessarily the
same one: activities pair on their name, product and place. The engine looks
for each version's suppliers in ecoinvent and BAFU, and says which it found for
each. It also needs **the impact method** (`--method`), any the engine reads:
EF 3.1 as the JRC's ILCD package (`EF-v3.1.zip`) or the adapted version exported
from SimaPro as CSV, ecological scarcity 2021 as a SimaPro CSV, and so on.
`--reported`, optional, gives the results Carbonfact publishes for the database
(`Carbonfact_<step>_DB_LCIA.csv`), to check that the first version finds them
again: it reads them on the method's `Climate change` category, so it needs EF 3.1.

It writes, under the name of the second version:

- `….comparison.csv`: each activity, then each category, for the first version
  and for the second;
- `….comparison.svg`: one scatter per category, the first version across, the
  second up; the grey bands mark ±10 % and a factor 2.

The terminal says, when `--reported` is given, how close the first version
comes to Carbonfact's figures, then, per category, the median ratio between the
two versions, the share of activities within 10 %, a factor 1.5, 2 and 10,
and how many no ratio can judge (a zero or a negative on one side), and ends
with the widest gaps, every category together (`--top`, 20 by default): a ratio
has no unit, so a gap on one category ranks against a gap on another. An activity found on one side only is listed, then left out.

### `quality`: the quality report

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py quality "C:\data\Dyeing_Inventory_Brightway.bafu.brightway.xlsx"
```

It needs only the database, in any format the engine reads, and writes
`….quality.csv`: the structural defects the engine finds, such as a process
without a reference, an allocation that does not add up to 100 % or missing
metadata. Inputs the database does not make itself are listed for information:
they come from the background. This report is not a data quality rating (DQR):
the script neither reads nor writes the DQR Carbonfact publishes.

### `merge`: several databases as one

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/carbonfact/carbonfact_bafu.py merge "C:\data\Dyeing_Inventory_Brightway.bafu.brightway.xlsx" "C:\data\Printing_Inventory_Brightway.bafu.ecospold2.zip" `
  --bafu "C:\data\BAFU-2026 v1_ecoSpold v1.zip" --format ecospold2
```

It takes databases in any formats the engine reads, on ecoinvent, on BAFU or
some on each, and the backgrounds they take their suppliers from: `--ecoinvent`,
`--bafu` or both. It writes `merged.<format>.<extension>` in the current folder
(`--name` and `--out` change the name and the folder). An activity that several
databases copy, such as the heat mix, is kept once; written differently in two
of them, it stops the merge, since which one to keep is not the script's call.

`VOLCA_BINARY` with `VOLCA_DATA_DIR`, set as environment variables, run a local
engine build instead of the downloaded one.

## What was measured

With the expert mapping of 28 October 2026 as it comes, the built-in choices
(below) and EF 3.1 adapted 1.03, on the eight databases: on ecoinvent, climate change is within
0.03 % of the figure Carbonfact reports, on each of the 294 activities it
reports one for. On BAFU, the median ratio BAFU / ecoinvent is 0.84 on climate
change, with 99 % of the activities within a factor 1.5. With the JRC's EF 3.1
package instead, on the dyeing database alone, climate change is within 0.06 %
of Carbonfact's figure on its 38 activities, and the median ratio is 0.92.

The widest gap is on freshwater ecotoxicity, inorganics, about a hundred
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
  equivalent. Copy `….bafu.mapping.csv`, fill `target_product` and `target_location`
  for those rows, and pass it with `--complement`. When the status adds
  `--correspondence may find it`, pass the correspondence file first.
- **`no column … after …`** or **`two columns …`**: the workbook's titles
  changed; the message names the column it misses, or the title found twice,
  which must be renamed on the column not to read.
- **`… inputs find no supplier in …`**: a version calls suppliers neither
  ecoinvent nor BAFU has; the message lists them.
- **`references in different units`** or **`several activities answer to …`**
  (`compare`): the two versions write an activity in different units, or one
  version has two activities of the same name, product and place; the script
  will not pick.
- **`… is written differently in … and …`** (`merge`): two databases copy one
  activity but do not write it the same way, a database on ecoinvent and its
  BAFU version for instance; the script will not pick.
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

The mapping decides first. The script carries a BAFU choice for the nine
suppliers below, and uses it only where the mapping names no BAFU process for
one: a row saying #N/A, a supplier new in ecoinvent 3.12, or one the mapping
lacks. `--complement` gives other choices in their place. The
choices are **proposals, not validated**: each should be checked by someone who
knows both databases, and the mapping corrected upstream where it can be.

With the workbook of 3 November 2026, four of them are still used: alkyl
sulfate, organic chemical, polytetrafluoroethylene and the kettle. The mapping
now names a process for the other five, and that process
is used; for the dryer it is « Technical wood drying, infrastructure » {RER},
which came out seven times higher than ecoinvent on the 28 October workbook.

| ecoinvent 3.12 supplier | why a mapping had nothing | BAFU choice | strength |
|---|---|---|---|
| market for alkyl sulfate (C12-14) {GLO} | mapping says #N/A | Fatty alcohol sulfate, mix, at plant {RER} | close |
| market for chemical, organic, unspecified {GLO} | new in 3.12, no 3.11 dataset | Chemicals organic, at plant {GLO} | what the mapping gives for the 3.11 « chemical, organic » |
| market for polytetrafluoroethylene {GLO} | new in 3.12, no 3.11 dataset | Tetrafluoroethylene, at plant {RER} | proxy: the monomer, not the polymer |
| market for dryer {GLO} | mapping says #N/A | Washing machine, at retailer {CH} | weak proxy: BAFU has no dryer |
| market for electric kettle {GLO} | mapping says #N/A | Convector radiator {CH}, 1.5 kg per appliance, the mass ecoinvent gives its kettle | weak proxy for the iron: BAFU has no small appliance; the electricity, not the iron, makes most of ironing |
| market for tissue paper {GLO} | mapping says #N/A | Paper, woodfree, uncoated, at regional storage {RER} | close |
| market for waste mineral oil {RoW} | mapping says #N/A | Disposal, used mineral oil, 10% water, to hazardous waste incineration {CH} | close |
| market for wastewater from textile production {RoW} | mapping says #N/A | Treatment, sewage, to wastewater treatment, class 3 {CH} | same as the mapping's « wastewater, average » |
| market for cationic resin {RoW} | typo in the mapping: « Cationic resin, at plan », no location | Cationic resin, at plant {CH} | the mapping's intent |

A row saying « Dummy » is no gap: the mapping marks an ecoinvent inventory that
is empty in practice, without impact. The line to it is dropped, so it scores
nothing, and no choice stands in for it. The workbook of 3 November 2026 says
so of the magnesium chloride from titanium sponge production, among others.

Three more suppliers are not choices but renames: ecoinvent 3.12 splits by
region a market 3.11 had for the world, and the mapping knows only the latter.
The script reads them as ecoinvent's correspondence file does, so the mapping
decides for them; `--correspondence` replaces these three with the whole file.

| ecoinvent 3.12 supplier | read as the 3.11 dataset |
|---|---|
| market for polyethylene, high density, granulate {RER} | market for polyethylene, high density, granulate {GLO} |
| market for polypropylene, granulate {RER} | market for polypropylene, granulate {GLO} |
| market for polypropylene, granulate {RoW} | market for polypropylene, granulate {GLO} |

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
| market for naphthalene sulfonic acid {GLO} | Polyphenylene sulfide, at plant {GLO} |
| market for o-phenylene diamine {GLO} | Polyphenylene sulfide, at plant {GLO} |
| market for polyacrylamide {GLO} | Polylactide, granulate, at plant {GLO} |
| market for sodium amide {RoW} | Sodium chloride, powder, at plant {RER} |
| market for sodium nitrite {RoW} | Sodium dichromate, at plant {RER} |

Proxies worth a second look:

| ecoinvent supplier | BAFU process the mapping gives | doubt |
|---|---|---|
| market for cooling energy {GLO} | Heat, from natural gas {ENTSO-E}, 1.67 MJ of heat per MJ of cold | cooling is not heat, though an absorption chiller does turn heat into cold at about this rate |
| market for dryer {GLO} | Technical wood drying, infrastructure {RER} | a drying plant for timber, not a household appliance |
| market for 2-nitroaniline {GLO} | Dinitroaniline-compounds, at regional storehouse {RER} | a family of herbicides, not the dye intermediate |
| market for sodium hydrosulfide {GLO} | Hydrogen sulphide, H2S, at plant {RER} | precursor, not the product |
| market for methacrylic acid {GLO} | Methyl methacrylate, at plant {RER} | the ester, not the acid |
| market for carton board box production, with gravure printing {GLO} | Packaging, corrugated board, mixed fibre, single wall, at plant {RER} | no printing |
| market for printed paper {GLO} | Paper, newsprint, at plant {CH} | no printing |
| market for waste packaging paper {RoW} | Waste paper, mixed, from public collection, for further treatment {RER} | a collected material, not a waste treatment |
| market for waste polypropylene {RoW} | xx Disposal, polypropylene, 15.9% water, to sanitary landfill {CH} | a BAFU name starting with « xx », worth asking BAFU why |
| market for ethanol, … from fermentation {RoW} | Ethanol, 95% in H2O, from sugar cane, at fermentation plant {BR} | one feedstock and one country for the rest of the world |

## Checking the script

`uv run test_carbonfact_bafu.py` checks the workbook reader, that the mapping
decides before the built-in choices, how the activities of two
versions pair, and what a merge keeps once, without the engine.

## How it works

- **Relinking.** Each ecoinvent supplier is walked back to 3.11 (with the
  correspondence file, else the built-in renames, else under its own name), looked up in the mapping
  and, where it has no BAFU process, in the built-in choices. Two ecoinvent
  suppliers share one product and place (« heat, district or industrial, other
  than natural gas » {RoW}, from hard coal and from light fuel oil) but map to
  different BAFU processes; a relink keys an input on its product and place, so
  the BAFU version names its ecoinvent inputs after their activity instead.
- **Conversion.** The mapping's `conversion` says how many units of the
  ecoinvent product make one unit of the BAFU process: 2.17 kg of urea per kg
  of « Urea, as N », 0.599 MJ of cold per MJ of heat. The engine applies it
  (1 kg of urea becomes 0.46 kg of « Urea, as N »). Between two units of one
  measure and different sizes, such as kWh and MJ, the engine's unit table is
  right and refuses a factor that disagrees with it, so the script leaves the
  factor out there and prints each one it leaves out.
- **Direction.** ecoinvent counts a waste as a negative output of its market
  (« market for wastewater from textile production », −1 m³), BAFU as the
  positive output of a treatment (« Treatment, sewage, … class 3 », +1 m³). A
  line keeping ecoinvent's sign would have BAFU treat a negative volume and
  score credits. Once both databases are loaded, the script asks each the
  reference amount of every supplier and its BAFU process, and marks
  `opposite` the rows where the signs differ; the engine turns those lines. A
  mapping exported from VoLCA's mapping page already carries this column.

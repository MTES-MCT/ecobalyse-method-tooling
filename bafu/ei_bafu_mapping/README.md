# ecoinvent to BAFU mapping, impact by impact

`compare_mapping.py` reads a mapping workbook that pairs ecoinvent 3.11 processes with
BAFU 2026 processes, scores both processes of every pair on EF 3.1, and shows how far
apart they are: one scatter per impact category plus the ECS and PEF single scores, each
point one pair, coloured by the technical grade the workbook gives it. It also writes one
CSV line per pair and prints the widest gaps.

## What you need

- **The mapping workbook** (`.xlsx`), sheet `Matching Full`. The script reads these
  columns, by position, and stops with a message naming the first one that differs:

  | Column | Header | Read as |
  |---|---|---|
  | A | `EI 3.11 dataset` | the ecoinvent process, written the SimaPro way: `product {GEO}\| activity` |
  | D | `Unit` | its unit |
  | E | `BAFU Name` | the BAFU process |
  | F | `Geography` | its location |
  | G | `unit` | its unit |
  | H | `conversion` | one BAFU unit in ecoinvent units, when the two units measure different things |
  | I | `Source` | |
  | J | `Tech Grade` | 0 (no match) to 3 (perfect match) |
  | K | `Geo Grade` | |

- **ecoinvent 3.11, cut-off, exported from SimaPro as CSV** (zipped or not). The export
  carries the names with a ` | Cut-off, U` suffix, which the script expects.
- **BAFU 2026 as its EcoSpold 1 archive**, the `.zip` as BAFU distributes it.
- **The impact method, exported from SimaPro as CSV**: EF 3.1 adapted 1.03 is the one
  Ecobalyse uses. Export it with its version in the file name (`… (adapted).1.03.CSV.zip`):
  an export without a version can come from a release whose category names differ, and
  the run then stops (see "When it stops").

The script needs no other installation than `uv`: `uv` brings its own Python and the
libraries the script declares, and the script downloads the VoLCA engine itself.

## Install, once (Windows)

Open PowerShell (Start menu, type `PowerShell`) and install `uv`:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close PowerShell and open it again, so it finds `uv`. There is nothing else to install,
not even this repository: the script declares what it needs and `uv` fetches it.

On Linux or macOS, `curl -LsSf https://astral.sh/uv/install.sh | sh` installs `uv`.

## Run

In PowerShell, run the script from its address on GitHub with your four files, all on
one line; quote paths that hold spaces.

```powershell
uv run https://raw.githubusercontent.com/MTES-MCT/ecobalyse-method-tooling/main/bafu/ei_bafu_mapping/compare_mapping.py "C:\data\mapping.xlsx" --ecoinvent "C:\data\Ecoinvent3.11.CSV.zip" --bafu "C:\data\BAFU-2026 v1_ecoSpold v1.zip" --method "C:\data\Environmental Footprint 3.1 (adapted).1.03.CSV.zip"
```

The same command runs on Linux or macOS, with paths of those systems.

Each run takes the script as it stands on `main`, so there is nothing to update. To run
a fixed copy instead, download
[`compare_mapping.py`](compare_mapping.py) (**Download raw file** on its GitHub page) and
pass its path to `uv run` in place of the address.

The first run downloads the latest engine release; every run starts the engine on a free
port of your machine, loads both databases from their files, scores every mapped
process and stops the engine at the end. Expect several minutes: loading and scoring the
whole of ecoinvent is most of it. The results land next to the workbook (see below).

Options, all optional:

| Option | Default | Effect |
|---|---|---|
| `--sheet NAME` | `Matching Full` | the sheet to read |
| `--top N` | 20 | how many of the widest gaps to list at the end |
| `--min-grade N` | 2 | lowest technical grade the statistics and gap lists count |
| `--long-term` | off | count emissions beyond a hundred years, on both sides |

`VOLCA_BINARY` (with `VOLCA_DATA_DIR`, the data directory of the same build), set as
environment variables, runs a local engine build instead of the downloaded release.

## When it stops

- **`the engine returned no ECS, PEF: this method names other categories`**: the method
  file has a category the two single scores read under another name. A release without a
  version number splits "Ecotoxicity, freshwater - organics" into two parts, `p.1` and
  `p.2`, which the script does not know. Pass adapted 1.03 or 1.05.
- **`CERTIFICATE_VERIFY_FAILED` while downloading the engine**: this Python does not trust
  the certificates of the download site. In PowerShell, point it at the bundle that
  `certifi` ships, then run the same command again:

  ```powershell
  $env:SSL_CERT_FILE = (uv run --with certifi python -c "import certifi; print(certifi.where())")
  ```

  The variable holds for this window only. `setx SSL_CERT_FILE "<that path>"` keeps it,
  from the next window on. Behind a company firewall that re-signs web traffic, the
  bundle of your IT department is the one to give instead.
- **`Engine exited with code 3221225781 before reporting its bound port`** (Windows): that
  code is `0xC0000135`, a DLL the engine needs is missing. Install the latest Microsoft
  Visual C++ Redistributable x64 (`vc_redist.x64.exe`), or run the engine's `.exe` by
  hand: Windows then names the missing DLL.

## How it reads the inputs

- **The workbook**: the `conversion` column is read only between units of different
  dimensions (0.2778 kWh for one MJ, 1200 kg for one car), and only when the two units
  the workbook writes measure what the engine reads on each side: a factor written
  between other units would be applied to the wrong quantity.
- **ecoinvent** is joined on the SimaPro name of column A, **BAFU** on the name and
  location of columns E and F.
- **The method**: the ECS and PEF single scores are declared on top of it inside the
  script, on EF 3.1 category names, so another family of method needs those two blocks
  rewritten. This ECS is the Ecobalyse weighting on EF 3.1 alone, without Ecobalyse's own
  corrections: it ranks the pairs, it is not the figure the ecobalyse pipeline publishes.

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
  compared or why not (no BAFU process in the workbook, a workbook cell in error such as
  `#REF!`, a name absent from a database, units of different dimensions with no
  conversion in the workbook), a `conversion` column saying where the unit
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
- Land use climate change on the BAFU side matches the published table: measured on
  sixty processes under EF 3.1 adapted 1.03, the median ratio is 1.0000 on that
  sub-category and on the climate change total. Engines before v0.13.0 gave BAFU's
  fossil and biogenic methane the factor of land transformation methane, which put that
  sub-category hundreds of times too high.

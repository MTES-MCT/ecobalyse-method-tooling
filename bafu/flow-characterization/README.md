# flow-characterization

List the biosphere flows of a Brightway database that the impact method leaves
**uncharacterized** (no characterization factor, so a silent zero in the score).

Built for **BAFU** freshly imported alongside ecoinvent: it tells apart the flows that EF 3.1
genuinely has no factor for (waste heat, biogenic CO2, COD/BOD, noise, ocean water) from a real
mapping gap where a BAFU flow name or compartment fails to line up with the characterized
`biosphere3` flow it should match.

## Directory structure

```
flow-characterization/
├── diagnose_flows.py   # the script
├── pyproject.toml      # standalone uv project (deps: bw2data, python-dotenv)
├── .env.example        # copy to .env and adjust
└── README.md
```

## Setup

Copy `.env.example` to `.env` and point it at the Brightway project that holds the imported
database and the method:

```bash
BRIGHTWAY2_DIR=/path/to/brightway-dirs/<project>
EB_PROJECT=ecobalyse
EB_METHOD=Environmental Footprint 3.1 (adapted)
EB_BIOSPHERE=biosphere3
EB_DATABASE=BAFU 2026v1
```

The script loads `.env` **before** importing `bw2data`, because `bw2data` reads `BRIGHTWAY2_DIR`
from the environment at import time.

## Usage

```bash
uv run diagnose_flows.py                 # summary + top 40 uncharacterized flows
uv run diagnose_flows.py uncharacterized.csv   # + full list to CSV
```

## Output

```
database                          : 'BAFU 2026v1'
distinct biosphere flows used     : 2367
  characterized by the method     : 1745
  NOT characterized (silent zero) : 622

Top uncharacterized flows (by nb of emitting processes):
 #proc   sum|amount|  name  [categories]  unit
  2048     2.943e+08  'Heat, waste'  [air, urban air close to ground]  megajoule
   ...
```

CSV columns: `nb_processes, sum_abs_amount, name, categories, unit, code`.

## Reading the result

- A flow that is uncharacterized here **and** for ecoinvent (both link to the same `biosphere3`
  nodes) is one EF has no factor for. That is expected, not a defect.
- A flow that **should** carry an impact but shows up here is the real target: its name or
  compartment does not match the characterized `biosphere3` flow. Fix it with a database-side
  biosphere migration (name/compartment) or, if the method's CF name differs, a method synonym.

## Caveat

The check compares biosphere **node ids**. Importing a database grows `biosphere3` and renumbers
its nodes, which orphans an already-imported method until it is re-imported and the datapackages
re-synced. Run this only against a project where the database, the method and the datapackages
were imported and synced together (i.e. after a full `just import-all`), or every flow will look
uncharacterized.

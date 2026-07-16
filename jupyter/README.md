# jupyter/ — JupyterLab against the ecobalyse Brightway project

JupyterLab, pointed at a Brightway project directory, to browse LCA
databases and methods interactively. Independent `uv` project, same
`BRIGHTWAY2_DIR` convention as `bafu/*`.

## Requirement: sibling checkout

`ecobalyse-method-tooling` must be checked out **next to** `ecobalyse`
(same parent directory):

```
some-dir/
  ecobalyse/                  # the app repo (public/data/, data/)
  ecobalyse-method-tooling/   # this repo
    jupyter/
      notebooks/
```

The notebooks reach the app repo's data through `../../../ecobalyse/...`
(a notebook's kernel CWD is always its own file's directory, i.e.
`jupyter/notebooks/`).

## Setup

```sh
cp .env.example .env
# edit .env: BRIGHTWAY2_DIR=/path/to/brightway-dirs/<project>
```

## Run

From the repo root: `just jupyter`. Or directly:

```sh
cd jupyter && uv run jupyter lab
```

Open `notebooks/explore.py` or `notebooks/textile_processes.py` — they're
plain-`.py` notebooks (via `jupytext`), not `.ipynb`.

## Notebooks

- `explore.py` — interactive Brightway activity/impact browser (widgets:
  pick a project/database/method, search activities, inspect impacts).
- `textile_processes.py` — plots per-category ECS breakdown for every
  textile process, from the app repo's `public/data/textile/processes.json`.

# ecobalyse-method-tooling

LCA methodology tooling for [Ecobalyse](https://ecobalyse.beta.gouv.fr/):
one-off analyses, parity checks, and exploration tools that don't belong in
the app repo itself. Each subfolder is one topic, an independent `uv`
project (own `pyproject.toml` / `uv.lock`), with its own `README.md`.

**Requirement**: check this repo out as a **sibling** of `ecobalyse` (same
parent directory) — several tools reach the app repo's data through
relative paths.

## Where to go

| Folder | Go here when you want to… |
|--------|---------------------------|
| [`bafu/`](bafu/) | BAFU (Swiss KBOB) import / VoLCA parity tooling. |
| [`food/`](food/) | Food ingredient metadata, transformed-ingredient params, Agribalyse recipes. |
| [`textile/`](textile/) | Textile modeling notes. |
| [`jupyter/`](jupyter/) | JupyterLab against the ecobalyse Brightway project — `just jupyter`. |
| [`volca/`](volca/) | VoLCA server config (ECS/EFC/PEF scoring, 7-database catalog) — `just volca`. |

## Shared context

Most tools read a Brightway project via `BRIGHTWAY2_DIR` (see each folder's
`.env.example`), or talk to a VoLCA server. See `jupyter/README.md` and
`volca/README.md` for how to run those two locally.

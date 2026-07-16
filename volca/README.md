# volca/ — VoLCA server configuration

`volca.toml`: the canonical VoLCA config (ECS + EFC + PEF + raw-ecobalyse
scoring, the 7-database SimaPro CSV catalog, EF 3.1 + Biomaps method). Every
path in it is rooted at `/data`, the Docker mount point — the same file runs
unmodified locally (`just volca`) and in production (systemd), only the
host directory bind-mounted to `/data` changes.

## Data directory layout

`just volca` mounts `$VOLCA_DATA_DIR` (defaults to `./volca_data/`, gitignored)
to `/data`. It must contain:

```
volca_data/
  dbfiles/
    AGB32_final.CSV.zip
    Ecoinvent3.9.1.CSV.zip
    Ecoinvent3.11.CSV.zip
    ginko2025.2.csv.zip
    pastoeco.2.CSV.zip
    wool.2.CSV.zip
    WFLDB.CSV.zip
  methods/
    ef31-biomaps/          # copied from the VoLCA repo's examples/methods/ef31-biomaps
  geographies.csv
  flows.csv
  compartments.csv
  units.csv
```

None of this is provisioned by this repo: the SimaPro CSV exports are
multi-gigabyte proprietary LCA data, and `ef31-biomaps` currently lives in
the VoLCA source repo's `examples/methods/`. Copy them into place by hand on
every host that runs `just volca` (dev machine or server) before starting
it — `volca.toml` will fail to load databases/methods it can't find.

## Run

```sh
export VOLCA_DATA_DIR=/path/to/volca_data   # optional, defaults to ./volca_data
export VOLCA_SERVER_ARGS='--password yourtoken'  # optional, never commit a real password
just volca
```

This runs the `volca-with-frontend` Docker image (built separately, from the
VoLCA source repo — not part of this repo) with `volca.toml` and
`$VOLCA_DATA_DIR` bind-mounted in. Once up: `http://localhost:8080/` (UI),
`http://localhost:8080/api/v1/docs` (API docs).

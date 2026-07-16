# https://github.com/casey/just

volca_data_dir := env_var_or_default("VOLCA_DATA_DIR", justfile_directory() + "/volca_data")

default:
    @just --list

# JupyterLab against the Brightway project set in jupyter/.env
jupyter:
    cd jupyter && uv run jupyter lab

# VoLCA server, config from volca/volca.toml, data from $VOLCA_DATA_DIR (defaults to ./volca_data)
volca:
    docker run --rm --name volca -p 8080:8080 \
      -v {{ volca_data_dir }}:/data \
      -v {{ justfile_directory() }}/volca/volca.toml:/config/volca.toml:ro \
      volca-with-frontend --config /config/volca.toml server ${VOLCA_SERVER_ARGS:-}

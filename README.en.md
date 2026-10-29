# Maestro

[中文](README.md)

Maestro lets an invited, trusted team submit, watch, compare and rerun experiments built on [Harbor](https://github.com/harbor-framework/harbor) from a web page and a Python SDK. Datasets are chosen, experiment configurations are pinned, results are shared with the whole team, and every run keeps who submitted it. It is a self-hosted platform for one team, not a multi-tenant service.

The project is in 0.x: anything may change between versions, and the detailed documentation under `docs/` is in Chinese for now. The web UI is available in Chinese and English; the English text is AI-translated and has not been reviewed by a human — please report anything that reads wrong.

## What it does

- Imports Harbor Hub datasets, prepares their environments once and shares the prepared images between tasks.
- Layered run configuration (team defaults, experiment plans, per-run values) with model configurations, team-held provider credentials (encrypted, never sent back to the browser) and harnesses: Terminus-2, OpenCode, Codex and Claude Code.
- Content-addressed attachments, runs and batches, reruns, live execution pages, scoring that keeps a valid zero, a missing score and an execution failure apart, artifacts and exports, a recycle bin.
- Comparison across runs, collections and frozen analysis snapshots.
- `maestroctl` for a system-wide install, updates, backup and restore on one Linux host.

## Install

On a Linux host with systemd and Docker, from a release package (Releases: tagged versions from `master`, and pre-releases for every passing `dev` commit, for trying out only):

```bash
sha256sum -c SHA256SUMS --ignore-missing
tar xf maestro-<version>.tar.gz && cd maestro-<version>
sudo ./maestroctl install --uv "$(command -v uv)"   # prints the address and a one-time setup token
```

Then `sudo maestroctl status / logs / backup / update <package> / uninstall`. Agents and scripts use the [Python SDK](sdk/README.md) (`pip install ./sdk`) with a personal access token created on the web page's Settings.

## Try it from source

Needs Linux, Python 3.12–3.13 and uv; build the web pages first with `npm ci && npm run build` in `frontend/`. Always use a fresh directory, never an existing team state:

```bash
uv sync --locked
root=$(mktemp -d)
cat > "$root/config.toml" <<EOF
data_dir = "$root/data"
cache_dir = "$root/cache"
master_key_dir = "$root/secrets"
cookie_secure = false
EOF
uv run maestro --config "$root/config.toml" init      # prints the one-time setup token
uv run maestro --config "$root/config.toml" serve --port 8420 --demo-executor
```

Open `http://127.0.0.1:8420` and create the first admin with the setup token. `--demo-executor` uses a fake executor and a fake dataset source (add `demo/arith` on the Datasets page); its results are not real experiments. Running real Harbor tasks is described in the Chinese [README](README.md).

## License

[Apache License 2.0](LICENSE); see also [NOTICE](NOTICE).

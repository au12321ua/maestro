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
tar xf maestro-<version>-<commit>.tar.gz && cd maestro-<version>-<commit>
sudo ./maestroctl install --uv "$(command -v uv)"   # prints the address and a one-time setup token
```

Then `sudo maestroctl status / logs / backup / update <package> / uninstall`.

## Use it from an agent

An agent never logs in to the server. It needs the [Python SDK](sdk/README.md) (the agent skills ship inside it), a personal access token and a workspace. Install the SDK of the service's version from this project's package index on GitHub Pages, `https://au12321ua.github.io/maestro/simple` (from 0.4.0 on). Without an index, install the wheel attached to the release: `uv pip install ./maestro_sdk-<version>-py3-none-any.whl`.

```bash
mkdir my-work && cd my-work
uv venv && uv pip install "maestro-sdk==<version>" --index maestro=https://au12321ua.github.io/maestro/simple
.venv/bin/python -c "import maestro_sdk.workspace as w; w.init('.')"   # AGENTS.md, notes/, attachments/, skills
```

Create a token with the `experiment` scope on the web page's Settings and keep it in a file outside the workspace. Fill in the service address, the token file, the budget and the current goal in `AGENTS.md`, then start the agent in that directory; the skills are in `.agents/skills/` and `.claude/skills/`.

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

Open `http://127.0.0.1:8420` and create the first admin with the setup token. `--demo-executor` uses a fake executor and a fake dataset source (add `demo/arith` on the Datasets page); its results are not real experiments.

## License

[Apache License 2.0](LICENSE); see also [NOTICE](NOTICE).

# Continuous integration (GitHub Actions)

The CI workflow is [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
It is the executable evidence that the test suite, the contract-doc check and
the packaging gates stay green. It runs **Ubuntu only** (`ubuntu-latest`): the
suite is POSIX-specific (Unix domain sockets, `0700` permissions, `SIGKILL`,
the detached worker), so no other runner OS is supported. There are **no
secrets and no live-model jobs** — the repository is public and the logs are
public.

## Triggers

| Event | `fast` | `full` | `release` |
| --- | --- | --- | --- |
| `pull_request` (any branch) | yes | only when the PR targets `main` | no |
| `push` to `main` | yes | no | no |
| `push` of a `v*` tag | yes | no | yes |
| `workflow_dispatch` | yes | yes | only when `release: true` |
| nightly `schedule` (`17 3 * * *`) | yes | yes | no |

`concurrency: ci-${{ github.ref }}` with `cancel-in-progress: true` cancels a
superseded run on the same ref. The workflow declares
`permissions: contents: read`.

## Jobs

- **`fast`** (matrix: Python 3.11 and 3.14, `timeout-minutes: 30`) — the quick
  gate on every event. Steps: checkout (full history, needed for the whitespace
  check), `setup-python`, `python -m compileall -q src scripts`,
  `python scripts/check_contract_docs.py`,
  `python -m unittest discover -s scripts -p 'test_*.py'`, a
  `git diff --check` whitespace check against the PR base (or `HEAD~1`), then
  the default test tier
  `python scripts/run_tests.py -j 4 --top 15 --timeout 1200`.
  The env var `FAST_BUDGET_SECONDS` is the marked spot for a wall-clock budget;
  it is empty today and, when set, is passed through as `--budget`.
- **`full`** (PRs targeting `main`, nightly, manual) — the whole suite including
  the slow tier, sharded and balanced by the recorded per-module durations in
  `tests/durations.json`. 6 shards on Python 3.14 and 2 shards on Python 3.11
  (`fail-fast: false`, `timeout-minutes: 45`). Each shard runs
  `python scripts/run_tests.py --slow -j 4 --top 10 --timeout 1800
  --durations-out durations-<py>-<shard>.json $(python scripts/test_shards.py
  --shards <shards> --index <shard>)` and uploads its durations JSON as an
  artifact (`if: always()`, 14-day retention).
- **`full-durations`** (`needs: full`, `if: always()` and only when `full` ran)
  — downloads the 3.14 shard duration artifacts, merges them with
  `python scripts/test_shards.py --merge durations-merged.json
  durations-3.14-*.json`, and uploads `durations-merged.json`. This is the
  artifact used to refresh `tests/durations.json`.
- **`release`** (manual dispatch with `release: true`, or a `v*` tag push;
  `timeout-minutes: 60`) — the gated checks: the clean-environment venv install
  and secret-scan check (`RAA_RELEASE_CHECK=1`) and the 1k/10k-candidate scale
  run (`RAA_SCALE_TESTS=1`). These need `python -m venv`/`pip` and possibly the
  network, which is why they are not in the default jobs.

The TypeScript spike tests (`spikes/runtime/typescript`) are deliberately **not**
in CI.

## Running the same checks locally

Fast tier (default; excludes the slow tier):

```bash
python3 scripts/run_tests.py -j 4 --top 15 --timeout 1200
```

Full tier, including the slow tier:

```bash
python3 scripts/run_tests.py --slow -j 4 --top 10 --timeout 1800
```

Run a couple of modules while iterating:

```bash
python3 scripts/run_tests.py test_records test_registry -j 2
```

The other `fast` steps, by hand:

```bash
python3 -m compileall -q src scripts
python3 scripts/check_contract_docs.py
python3 -m unittest discover -s scripts -p 'test_*.py'
git diff --check HEAD~1 HEAD
```

### Sharding

`scripts/test_shards.py` balances modules across shards by the durations
recorded in `tests/durations.json` (longest-processing-time greedy). Preview a
plan, or list one shard exactly as the `full` job does:

```bash
python3 scripts/test_shards.py --plan --shards 6
python3 scripts/test_shards.py --shards 6 --index 5
```

A shard's module list is passed positionally to `run_tests.py`, so the exact
CI shard command is:

```bash
python3 scripts/run_tests.py --slow -j 4 --top 10 --timeout 1800 \
  --durations-out durations-3.14-5.json \
  $(python3 scripts/test_shards.py --shards 6 --index 5)
```

### Refreshing `tests/durations.json`

The `full-durations` job merges the Python 3.14 shard timings into
`durations-merged.json`. To refresh the seeded table:

1. Download the `durations-merged` artifact from a recent `full` run.
2. Copy it over `tests/durations.json` (same schema: `{"schema": 1,
   "unit": "seconds", "modules": {...}}`).
3. Commit the refreshed file. The next `full` run re-balances shards from it.

To merge artifacts by hand, use the same command the job uses:

```bash
python3 scripts/test_shards.py --merge durations-merged.json durations-3.14-*.json
```

## Repository settings

Branch protection on `main` is a **repository setting** the owner enables (CI
cannot change it): require the `fast` and `full` checks to pass before merge.
Because `full` only runs on PRs that target `main`, require it there; `fast`
runs on every PR.

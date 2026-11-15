# github-trending-dataset-period-reconcile

**Is `stars_period` in the Trending archive a real 24-hour delta, or a number you should not trust? This tool measures it — because the archive's own reader prints a drift figure with no elapsed-time correction and no threshold, so today every ordinary day looks like an anomaly.**

Upstream: https://github.com/linmoxi33/github-trending-dataset

## What the upstream is, and what it lacks

[`linmoxi33/github-trending-dataset`](https://github.com/linmoxi33/github-trending-dataset) (CC0-1.0) keeps 28 daily snapshots of GitHub's Trending lists with `stars_period`, `stars_total` and `captured_at`. Its reader has five commands. Two of them touch this problem and neither solves it:

- `stats` is advertised as "archive coverage and **integrity**" ([query.py L597](https://github.com/linmoxi33/github-trending-dataset/blob/de4a06f1fba32cfcbf4b2a334cd8a7abf8f1bcff/scripts/query.py#L597)), but the integrity check only compares `latest.json` with the newest dated file ([L202](https://github.com/linmoxi33/github-trending-dataset/blob/de4a06f1fba32cfcbf4b2a334cd8a7abf8f1bcff/scripts/query.py#L202), [L245](https://github.com/linmoxi33/github-trending-dataset/blob/de4a06f1fba32cfcbf4b2a334cd8a7abf8f1bcff/scripts/query.py#L245)). No field-level arithmetic is ever checked.
- `timeline` prints `drift = (change in stars_total) - (stars_period)` per row and says "A large drift means GitHub's period figure and the running total disagree" ([L426](https://github.com/linmoxi33/github-trending-dataset/blob/de4a06f1fba32cfcbf4b2a334cd8a7abf8f1bcff/scripts/query.py#L426), formula at [L444](https://github.com/linmoxi33/github-trending-dataset/blob/de4a06f1fba32cfcbf4b2a334cd8a7abf8f1bcff/scripts/query.py#L444)). It never divides by the actual elapsed hours between captures (the README itself documents captures landing 15–26 h apart, [README L7](https://github.com/linmoxi33/github-trending-dataset/blob/de4a06f1fba32cfcbf4b2a334cd8a7abf8f1bcff/README.md#L7)), never aggregates, and never returns a verdict.

So the archive cannot answer: *which* period figures are self-contradictory, and how much of the apparent disagreement is just capture-clock skew?

## What this tool does / does not do

Does:

- **W check — window monotonicity.** Within one capture the windows nest (`daily ⊆ weekly ⊆ monthly`), so for any repo present in two of them the shorter window's `stars_period` must be ≤ the longer window's. Reported per capture with a per-repo witness.
- **R check — rate reconciliation.** For calendar-consecutive captures, `delta_total = stars_total(t) - stars_total(t-1)` is compared against `stars_period(t)` scaled by the real elapsed hours, i.e. `expected = period * hours / 24`. Both the upstream raw drift and the normalised residual are reported, so you can see how many "anomalies" are only clock skew.
- `--json`, `--tolerance` (fraction of the reported period), `--max-outlier-rate`, `--top`, deterministic `replay_sha256`.
- Exits `0` when the archive reconciles, `1` when it does not, `2` on usage/input errors.

Does not: model GitHub's ranking algorithm, decide which number is "the true one" (only the arm's-length difference is measurable), score repos, or use the network.

## Install / run

Python 3.8+, zero dependencies, no network:

```bash
python3 period_reconcile.py examples/in
```

Against a real archive directory (files named `YYYY-MM-DD.json`; `latest.json` and `awesome_index.json` are ignored):

```bash
python3 period_reconcile.py /path/to/github-trending-dataset/data
python3 period_reconcile.py /path/to/data --json > report.json
python3 period_reconcile.py /path/to/data --tolerance 0.10 --max-outlier-rate 0.20
```

## Measured on the real archive (28 snapshots, 2026-10-19 .. 2026-11-15)

Command: `python3 period_reconcile.py /tmp/ds/data --json` (default `--tolerance 0.20`).

| quantity | value |
| --- | --- |
| window-monotonicity pairs | 183 |
| window violations | **0** |
| consecutive-day repo pairs scored | 172 |
| elapsed time range between captures | 15.91 h .. 25.76 h |
| raw drift outliers (> 20% of period) | 59 / 172 = **34.30%** |
| normalised outliers (> 20% of period) | 51 / 172 = **29.65%** |
| outliers explained by capture skew | 8 of 59 (13.6%) |
| `replay_sha256` | `f360e71e34c1edf3bbb5964dbbed2bd40df1c3932d5767e80e9473d7d9433249` |

Reading: the nesting invariant holds on every one of 183 same-capture pairs, so the three lists are mutually consistent. The rate check does **not** hold: roughly a third of day-pairs miss the 20% band even after normalising for elapsed time, and correcting for the 15.9–25.8 h capture skew removes only 13.6% of the raw anomalies. The remaining ones are genuine field-level disagreements (largest: `meta/astryx` on 2026-11-09, total grew 2,512 while the period claimed 875).

## Exit codes

| code | meaning |
| --- | --- |
| 0 | no window violation and normalised outlier rate ≤ `--max-outlier-rate` |
| 1 | a window violation exists, or the normalised outlier rate is above the threshold |
| 2 | usage or input error (missing path, non-snapshot JSON, duplicate date, bad flag) |

## 本轮创新

First calibrated reconciliation of the archive's `stars_period` against `stars_total`: it separates **capture-clock skew** from genuine field disagreement, which the upstream's unnormalised `drift` line cannot do, and it proves the three window lists agree on every same-capture pair. Before this, "a large drift means the numbers disagree" was a claim nobody had measured; the measurement says only 13.6% of apparent disagreements are clock skew.

## Limits

- Only repos present in both consecutive daily snapshots are scored; a repo that leaves and returns contributes nothing to the rate check.
- `stars_period` is a rounded/floored display number, so small periods dominate the residual ratio; the 20% default is therefore lenient for repos with < 20 stars in a day (they are counted, not weighted).
- Calendar-consecutive is required (a missing day is skipped, never interpolated), so gaps reduce coverage rather than distort it.
- The check is about internal arithmetic only: it cannot tell whether GitHub's own period figure is correct, only whether it is consistent with the total it ships next to.

## Tests

```bash
python3 tests/run.py
```

17 tests, all behavioural (subprocess + exit codes + JSON): clean archive, window violation, gap skipping, 25-hour capture, tolerance sensitivity, missing path, non-snapshot JSON, duplicate dates, replay-hash stability, and reproducibility of `examples/out/report.json`.

MIT © 2026 linmoxi33
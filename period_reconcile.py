#!/usr/bin/env python3
"""Calibrated reconciliation of `stars_period` against `stars_total` in a
GitHub Trending snapshot archive.

Two independent checks, both computed offline from on-disk JSON:

  W  window monotonicity  - within one capture, a repo's daily gain cannot exceed
                            its weekly gain, and weekly cannot exceed monthly.
  R  rate reconciliation  - between consecutive captures, the growth of
                            `stars_total` should be `stars_period` scaled by the
                            real elapsed time between the two captures.  The
                            upstream reader prints the *raw* difference
                            (`delta_total - period`, query.py:426/444) with no
                            elapsed-time correction; this tool measures both and
                            reports the residual in units of the reported period.

Exit codes: 0 = archive reconciles within the tolerance; 1 = problems found;
2 = usage or input error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

TOL_DEFAULT = 0.20
MAX_OUTLIER_DEFAULT = 0.15
SNAPSHOT_GLOB_HINT = "YYYY-MM-DD.json"
_SNAP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[error] {msg}", file=sys.stderr)
    raise SystemExit(2)


def _parse_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"bad captured_at: {value!r}")


def load_snapshots(paths: list[str]) -> list[dict]:
    """Load snapshot files, sorted by their `date` field. latest.json is a copy."""
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            names = sorted(n for n in os.listdir(p) if _SNAP_NAME.match(n))
            files.extend(os.path.join(p, n) for n in names if n != "latest.json")
        elif os.path.isfile(p):
            files.append(p)
        else:
            die(f"no such file or directory: {p}")
    if not files:
        die("no snapshot JSON files found")
    snaps = []
    seen: set[str] = set()
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            die(f"cannot read {f}: {exc}")
        if not isinstance(payload, dict) or not isinstance(payload.get("lists"), dict):
            die(f"{f}: not a snapshot (missing `lists` object)")
        d = payload.get("date")
        try:
            d = date.fromisoformat(str(d))
        except ValueError:
            die(f"{f}: not a snapshot (bad `date`: {payload.get('date')!r})")
        if d.isoformat() in seen:
            die(f"duplicate snapshot date {d.isoformat()} (drop date-suffixed copies)")
        seen.add(d.isoformat())
        payload["_file"] = os.path.basename(f)
        payload["_date"] = d
        snaps.append(payload)
    snaps.sort(key=lambda s: s["_date"])
    return snaps


def _rows(snap: dict, period: str) -> dict[str, dict]:
    rows = snap["lists"].get(period) or []
    out = {}
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("repo"), str):
            out[r["repo"].lower()] = r
    return out


def _int(v) -> "int | None":
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def check_windows(snaps: list[dict]) -> dict:
    """Intra-capture invariant: daily <= weekly <= monthly period gains."""
    pairs = 0
    violations: list[dict] = []
    for snap in snaps:
        daily, weekly, monthly = (_rows(snap, p) for p in ("daily", "weekly", "monthly"))
        for lower_name, lower, upper_name, upper in (
            ("daily", daily, "weekly", weekly),
            ("weekly", weekly, "monthly", monthly),
        ):
            for key, low in lower.items():
                up = upper.get(key)
                if up is None:
                    continue
                lo, hi = _int(low.get("stars_period")), _int(up.get("stars_period"))
                if lo is None or hi is None:
                    continue
                pairs += 1
                if lo > hi:
                    violations.append(
                        {
                            "date": snap["_date"].isoformat(),
                            "repo": low["repo"],
                            "check": f"{lower_name} > {upper_name}",
                            "lower_period": lo,
                            "upper_period": hi,
                            "by": lo - hi,
                        }
                    )
    return {"pairs": pairs, "violations": len(violations), "worst": violations[:10]}


def check_rates(snaps: list[dict], tolerance: float) -> dict:
    """Inter-capture check: delta_total vs stars_period scaled by elapsed time."""
    pairs = 0
    skipped_gap = 0
    skipped_data = 0
    raw_outliers = 0
    norm_outliers = 0
    residuals: list[dict] = []
    hours_seen: list[float] = []
    for prev, cur in zip(snaps, snaps[1:]):
        if (cur["_date"] - prev["_date"]).days != 1:
            skipped_gap += 1
            continue
        try:
            t0, t1 = _parse_ts(prev.get("captured_at")), _parse_ts(cur.get("captured_at"))
        except ValueError:
            skipped_data += 1
            continue
        hours = (t1 - t0).total_seconds() / 3600.0
        if not (0 < hours < 72):
            skipped_data += 1
            continue
        prev_rows, cur_rows = _rows(prev, "daily"), _rows(cur, "daily")
        for key, row in cur_rows.items():
            old = prev_rows.get(key)
            if old is None:
                continue
            total_now, total_old = _int(row.get("stars_total")), _int(old.get("stars_total"))
            period = _int(row.get("stars_period"))
            if total_now is None or total_old is None or not period:
                skipped_data += 1
                continue
            delta = total_now - total_old
            raw = delta - period
            expected = period * hours / 24.0
            norm = delta - expected
            pairs += 1
            hours_seen.append(hours)
            if abs(raw) > tolerance * abs(period):
                raw_outliers += 1
            ratio = abs(norm) / abs(period)
            if ratio > tolerance:
                norm_outliers += 1
            residuals.append(
                {
                    "date": cur["_date"].isoformat(),
                    "prev_date": prev["_date"].isoformat(),
                    "repo": row["repo"],
                    "hours": round(hours, 2),
                    "delta_total": delta,
                    "period": period,
                    "raw_drift": raw,
                    "expected_total": round(expected, 2),
                    "norm_residual": round(norm, 2),
                    "norm_ratio": round(ratio, 4),
                }
            )
    residuals.sort(key=lambda r: (-r["norm_ratio"], r["repo"]))
    return {
        "pairs": pairs,
        "skipped_non_consecutive_days": skipped_gap,
        "skipped_unusable_rows": skipped_data,
        "tolerance": tolerance,
        "raw_outliers": raw_outliers,
        "raw_outlier_rate": round(raw_outliers / pairs, 4) if pairs else 0.0,
        "norm_outliers": norm_outliers,
        "norm_outlier_rate": round(norm_outliers / pairs, 4) if pairs else 0.0,
        "hours_min": round(min(hours_seen), 2) if hours_seen else None,
        "hours_max": round(max(hours_seen), 2) if hours_seen else None,
        "worst": residuals[:10],
    }


def build_report(snaps: list[dict], tolerance: float, max_outlier_rate: float) -> dict:
    windows = check_windows(snaps)
    rates = check_rates(snaps, tolerance)
    problems = []
    if windows["violations"]:
        problems.append(f"{windows['violations']} window-monotonicity violation(s)")
    if rates["norm_outlier_rate"] > max_outlier_rate:
        problems.append(
            f"normalized outlier rate {rates['norm_outlier_rate']:.2%} > {max_outlier_rate:.2%}"
        )
    report = {
        "archive": {
            "snapshots": len(snaps),
            "first": snaps[0]["_date"].isoformat(),
            "last": snaps[-1]["_date"].isoformat(),
        },
        "windows": windows,
        "rates": rates,
        "calibration": (
            "raw_drift = delta_total - period (upstream formula); "
            "norm_residual = delta_total - period * elapsed_hours / 24"
        ),
        "verdict": "consistent" if not problems else "problems",
        "problems": problems,
    }
    report["replay_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def render(report: dict) -> None:
    a, w, r = report["archive"], report["windows"], report["rates"]
    print(f"Archive      {a['first']} .. {a['last']} ({a['snapshots']} snapshots)")
    print(f"W windows    {w['pairs']} same-capture pairs, {w['violations']} violation(s)")
    print(
        f"R rates      {r['pairs']} consecutive-day pairs, "
        f"elapsed {r['hours_min']}..{r['hours_max']} h"
    )
    print(
        f"  raw drift  > {r['tolerance']:.0%} of period: "
        f"{r['raw_outliers']}/{r['pairs']} ({r['raw_outlier_rate']:.2%})"
    )
    print(
        f"  normalized > {r['tolerance']:.0%} of period: "
        f"{r['norm_outliers']}/{r['pairs']} ({r['norm_outlier_rate']:.2%})"
    )
    for v in w["worst"]:
        print(f"  ! {v['date']} {v['repo']}: {v['check']} {v['lower_period']} > {v['upper_period']}")
    for x in r["worst"][:5]:
        print(
            f"  ! {x['date']} {x['repo']}: delta {x['delta_total']} vs period {x['period']} "
            f"in {x['hours']} h -> normalized residual {x['norm_residual']} ({x['norm_ratio']:.2f}x)"
        )
    print(f"Verdict      {report['verdict']}: {'; '.join(report['problems']) or 'no problems found'}")
    print(f"replay_sha256 {report['replay_sha256']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="period-reconcile",
        description="Reconcile stars_period against stars_total in a trending snapshot archive.",
    )
    ap.add_argument("paths", nargs="+", help="archive directory or snapshot JSON files")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable report")
    ap.add_argument("--tolerance", type=float, default=TOL_DEFAULT,
                    help="outlier threshold as a fraction of stars_period (default 0.2)")
    ap.add_argument("--max-outlier-rate", type=float, default=MAX_OUTLIER_DEFAULT,
                    help="fail if the normalized outlier rate exceeds this (default 0.15)")
    ap.add_argument("--top", type=int, default=10, help="worst offenders to include (default 10)")
    args = ap.parse_args(argv)
    if args.tolerance <= 0 or args.max_outlier_rate < 0:
        die("--tolerance must be > 0 and --max-outlier-rate >= 0")
    if args.top < 1:
        die("--top must be >= 1")
    snaps = load_snapshots(args.paths)
    report = build_report(snaps, args.tolerance, args.max_outlier_rate)
    for key in ("windows", "rates"):
        report[key]["worst"] = report[key]["worst"][: args.top]
    if not report["replay_sha256"]:
        raise RuntimeError("unreachable")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        render(report)
    return 0 if report["verdict"] == "consistent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
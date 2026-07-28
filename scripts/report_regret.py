"""Render the README's three-way dispatch comparison from the regret marts.

Reads ``mart_dispatch_regret`` and ``mart_forward_dispatch_daily`` and writes a PNG plus a JSON
sidecar of exactly the numbers it plotted. ``--check`` re-reads the warehouse and diffs against the
committed sidecar instead of drawing anything, so CI can fail a stale figure on its *values* without
ever asserting on PNG bytes -- which differ across font stacks and matplotlib versions and would
make the check about rendering rather than about the result. Same "value-stable, not
hash-guaranteed" line the derived zone already draws.

**Why the chart plots differences and not costs.** Over the seeded span all four scenarios cost
about the same -- around EUR -145 -- because they are the same house under the same tariff, and
dispatch moves a fraction of a euro of it. Four bars at that scale are four identical bars. So the
baseline is naive dispatch, drawn as zero, and everything is plotted relative to it. That is the
axis on which the result is legible, and it is also the axis the question is asked on: not "what did
it cost" but "what did dispatching differently change".

matplotlib is a **dev** dependency and this script lives outside ``src/`` on purpose -- see the
comment on it in pyproject.toml and the guard in tests/test_import_containment.py.

Run with: ``just figure`` (or ``uv run python scripts/report_regret.py``)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import psycopg

from energy_platform.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]
IMAGE_PATH = ROOT / "docs" / "img" / "regret_three_way.png"
DATA_PATH = ROOT / "docs" / "img" / "regret_three_way.json"

# Validated categorical slots 1-3 from the data-viz palette, in fixed order: the deliverable first,
# then the two references. Assigned to the entity and never to its rank, so adding a scenario cannot
# repaint these. Aqua sits below 3:1 on a light surface, which is why every bar is directly labelled
# -- the relief rule, not decoration.
COLOUR_FORECAST_DRIVEN = "#2a78d6"  # blue
COLOUR_PERFECT_PLAN = "#eb6834"  # orange
COLOUR_HINDSIGHT = "#1baf7a"  # aqua

SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"

# Money differences are reported to the cent in prose but compared here at the settlement quantum,
# so the check fails on a real change in dispatch and not on a rounding step.
_TOLERANCE_EUR = 1e-6

_SERIES = (
    ("forecast_driven", "Forecast-driven (executed)", COLOUR_FORECAST_DRIVEN),
    ("perfect_foresight_plan", "Same planner, perfect forecast", COLOUR_PERFECT_PLAN),
    ("optimal", "Hindsight-optimal", COLOUR_HINDSIGHT),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Recompute the plotted numbers and diff them against the committed JSON sidecar "
        "instead of rendering. Fails if the figure is stale.",
    )
    args = parser.parse_args(argv)

    payload = _read_warehouse()
    if not payload["windows"]:
        print(
            "No simulated window in mart_dispatch_regret -- run `just warehouse` first.",
            file=sys.stderr,
        )
        return 1

    if args.check:
        return _check(payload)

    _render(payload)
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {IMAGE_PATH.relative_to(ROOT)} and {DATA_PATH.relative_to(ROOT)}")
    return 0


def _read_warehouse() -> dict[str, Any]:
    config = AppConfig.from_env()
    marts = config.postgres.marts_schema
    with psycopg.connect(config.postgres.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            select tariff_id, region, sim_start, sim_end, simulated_days, fallback_days,
                   naive_cost_eur, forecast_driven_cost_eur, perfect_foresight_cost_eur,
                   hindsight_cost_eur, available_savings_eur, regret_eur,
                   forecast_error_cost_eur, myopia_cost_eur,
                   captured_value_share, attainable_value_share,
                   pv_model_key, load_model_key
            from {marts}.mart_dispatch_regret
            where is_simulated
            order by tariff_id
            """
        )
        columns = [column.name for column in cur.description or ()]
        windows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

        cur.execute(
            f"""
            select tariff_id, scenario, sim_day, local_date, cumulative_net_cost_eur
            from {marts}.mart_forward_dispatch_daily
            where is_simulated
            order by tariff_id, scenario, sim_day
            """
        )
        columns = [column.name for column in cur.description or ()]
        daily = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    return {
        "windows": [_jsonable(row) for row in windows],
        "daily": [_jsonable(row) for row in daily],
    }


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """Dates to ISO strings and Decimals to floats, so the sidecar is a stable text diff."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif value is None or isinstance(value, str | bool | int):
            out[key] = value
        else:
            out[key] = float(value)
    return out


def _check(current: dict[str, Any]) -> int:
    if not DATA_PATH.exists():
        print(
            f"{DATA_PATH.relative_to(ROOT)} is missing -- run `just figure` and commit it.",
            file=sys.stderr,
        )
        return 1
    committed = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    problems = _diff(committed, current)
    if problems:
        print(
            "The committed README figure no longer matches the warehouse:",
            *problems,
            sep="\n  - ",
            file=sys.stderr,
        )
        print("\nRegenerate it with `just figure` and commit the result.", file=sys.stderr)
        return 1
    print(f"{DATA_PATH.relative_to(ROOT)} matches the warehouse.")
    return 0


def _diff(committed: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for section in ("windows", "daily"):
        old, new = committed.get(section, []), current[section]
        if len(old) != len(new):
            problems.append(f"{section}: {len(old)} committed rows, {len(new)} in the warehouse")
            continue
        for index, (before, after) in enumerate(zip(old, new, strict=True)):
            for key, value in after.items():
                was = before.get(key)
                if isinstance(value, float) and isinstance(was, int | float):
                    if not math.isclose(value, was, abs_tol=_TOLERANCE_EUR):
                        problems.append(f"{section}[{index}].{key}: {was} -> {value}")
                elif was != value:
                    problems.append(f"{section}[{index}].{key}: {was!r} -> {value!r}")
    return problems


def _render(payload: dict[str, Any]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    windows = payload["windows"]
    tariffs = [row["tariff_id"] for row in windows]

    figure, (bars_ax, lines_ax) = plt.subplots(
        1, 2, figsize=(12.8, 5.6), gridspec_kw={"width_ratios": [1.0, 1.35]}
    )
    figure.patch.set_facecolor(SURFACE)

    handles = _bars(bars_ax, windows, tariffs)
    _lines(lines_ax, payload["daily"], windows)

    span = f"{windows[0]['sim_start']} to {windows[0]['sim_end']}"
    days = windows[0]["simulated_days"]
    figure.suptitle(
        "Forward-looking battery dispatch: what day-ahead forecasts actually captured",
        x=0.010,
        y=0.972,
        ha="left",
        fontsize=14,
        color=INK_PRIMARY,
        fontweight="bold",
    )
    figure.text(
        0.010,
        0.921,
        f"{days} rolling days, {span} • synthetic demo data • "
        "naive self-consumption = 0; below the line is cheaper",
        ha="left",
        fontsize=9.5,
        color=INK_SECONDARY,
    )
    # A figure-level legend rather than a per-axes one: inside the left panel it sat on top of the
    # tallest bar and its value label, which is the one number a reader most needs to see.
    figure.legend(
        handles=handles,
        loc="upper left",
        bbox_to_anchor=(0.010, 0.895),
        ncol=len(handles),
        frameon=False,
        fontsize=9.5,
        handlelength=1.2,
        columnspacing=1.8,
    )
    # The two panels measure two different quantities and the footnote says which, because their
    # endpoints do not match and a reader is entitled to know why rather than to assume an error.
    figure.text(
        0.010,
        0.022,
        "Left: adjusted net cost -- the objective, including the value of energy still stored at "
        "the end of the span, and the only quantity the scenarios may be ranked by.\n"
        "Right: energy cost only. The terminal credit belongs to the whole span and cannot be "
        "honestly attributed to one day, so the curves exclude it and end at a different number.",
        ha="left",
        va="bottom",
        fontsize=8,
        color=INK_MUTED,
    )
    figure.tight_layout(rect=(0.0, 0.085, 1.0, 0.865))
    IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(IMAGE_PATH, dpi=170, facecolor=SURFACE)
    plt.close(figure)


def _bars(ax: Any, windows: list[dict[str, Any]], tariffs: list[str]) -> list[Any]:
    """Cost relative to naive dispatch, per tariff. The headline panel.

    Returns the bar handles so the caller can put the legend outside the axes.
    """
    handles: list[Any] = []
    width = 0.24
    positions = range(len(tariffs))
    for offset, (key, label, colour) in enumerate(_SERIES):
        values = [row[f"{_cost_column(key)}"] - row["naive_cost_eur"] for row in windows]
        xs = [index + (offset - 1) * width for index in positions]
        bars = ax.bar(
            xs,
            values,
            width * 0.88,  # a 2px-equivalent surface gap between adjacent fills
            label=label,
            color=colour,
            zorder=3,
        )
        handles.append(bars)
        # Every bar directly labelled: the relief rule for the sub-3:1 aqua, and the only way a
        # reader can see that the green bar is EUR -0.29 rather than "about zero".
        for x, value in zip(xs, values, strict=True):
            ax.annotate(
                f"{value:+.2f}",
                (x, value),
                textcoords="offset points",
                xytext=(0, 4 if value >= 0 else -12),
                ha="center",
                fontsize=8.5,
                color=INK_SECONDARY,
            )

    ax.axhline(0.0, color=BASELINE, linewidth=1.4, zorder=2)
    ax.annotate(
        "naive self-consumption",
        (len(tariffs) - 0.52, 0.0),
        textcoords="offset points",
        xytext=(0, 5),
        ha="right",
        fontsize=8.5,
        color=INK_MUTED,
        style="italic",
    )
    ax.set_title(
        "Adjusted net cost against the naive baseline (EUR)",
        fontsize=10.5,
        color=INK_PRIMARY,
        loc="left",
        pad=8,
    )
    ax.set_xticks(list(positions))
    ax.set_xticklabels([_tariff_label(tariff) for tariff in tariffs], fontsize=9.5)
    # Headroom so the value labels sit inside the panel rather than against its edge.
    low, high = ax.get_ylim()
    ax.set_ylim(low - (high - low) * 0.10, high + (high - low) * 0.12)
    _chrome(ax)
    return handles


def _lines(ax: Any, daily: list[dict[str, Any]], windows: list[dict[str, Any]]) -> None:
    """Cumulative cost against naive, day by day, for the dynamic tariff.

    One tariff rather than both: the two curves tell the same story and overlaying six lines to
    say it twice would be less legible, not more. The dynamic tariff is the only one where optimal
    dispatch is worth anything at all, so it is the one where the comparison has content.
    """
    tariff = (
        "dynamic_2024"
        if any(r["tariff_id"] == "dynamic_2024" for r in windows)
        else (windows[0]["tariff_id"])
    )
    naive = {
        row["sim_day"]: row["cumulative_net_cost_eur"]
        for row in daily
        if row["tariff_id"] == tariff and row["scenario"] == "naive_continuous"
    }
    for key, label, colour in _SERIES:
        series = [
            row
            for row in daily
            if row["tariff_id"] == tariff and row["scenario"] == key and row["sim_day"] in naive
        ]
        if not series:
            continue
        xs = [row["sim_day"] for row in series]
        ys = [row["cumulative_net_cost_eur"] - naive[row["sim_day"]] for row in series]
        ax.plot(xs, ys, label=label, color=colour, linewidth=2.0, zorder=3)
        # One direct label at the end of each line rather than a number on every point.
        ax.annotate(
            f"{ys[-1]:+.2f}",
            (xs[-1], ys[-1]),
            textcoords="offset points",
            xytext=(6, -3),
            fontsize=8.5,
            color=INK_SECONDARY,
        )

    ax.axhline(0.0, color=BASELINE, linewidth=1.4, zorder=2)
    ax.set_title(
        f"Cumulative energy cost against naive, {_tariff_label(tariff)} (EUR)",
        fontsize=10.5,
        color=INK_PRIMARY,
        loc="left",
        pad=8,
    )
    ax.set_xlabel("day of the simulation", fontsize=9, color=INK_SECONDARY)
    ax.set_xlim(1, max((row["sim_day"] for row in daily), default=1) * 1.06)
    _chrome(ax)


def _chrome(ax: Any) -> None:
    """Recessive grid and axes: the marks carry the information, the frame does not."""
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.8, zorder=1)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=INK_MUTED, labelsize=9, length=0)


def _cost_column(scenario: str) -> str:
    return {
        "forecast_driven": "forecast_driven_cost_eur",
        "perfect_foresight_plan": "perfect_foresight_cost_eur",
        "optimal": "hindsight_cost_eur",
    }[scenario]


def _tariff_label(tariff_id: str) -> str:
    return {"static_2024": "static tariff", "dynamic_2024": "dynamic tariff"}.get(
        tariff_id, tariff_id
    )


if __name__ == "__main__":
    raise SystemExit(main())

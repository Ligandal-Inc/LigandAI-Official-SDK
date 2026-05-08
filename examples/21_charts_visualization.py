# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
21 — Server-rendered chart generation via ``client.charts``.

Covers every public method on :class:`Charts`:
  client.charts.generate(chart_type, title, data, style=, save_to_program=)
  client.charts.get(chart_id)

The platform renders matplotlib charts server-side and stores the PNG on
disk so it can be embedded in reports/dashboards or downloaded later. This
script just shows the call shape — chart generation does NOT consume
generation/fold credits, only the small per-render quota.

Run with:

    LIGANDAI_API_KEY=lgai_pro_... python 21_charts_visualization.py

Free tier note: chart endpoints require a paid tier (basic+). On a free
key you'll get a 402 ``LigandAICreditError`` / ``LigandAIPaidTierRequired``
and the script falls through gracefully.
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    client = LigandAI(api_key=key)

    try:
        # 1) Generate a simple bar chart from in-memory data ---------------
        print("== charts.generate(chart_type='bar', ...) ==")
        try:
            chart = client.charts.generate(
                chart_type="bar",
                title="Top 5 EGFR binders by predicted Kd",
                data={
                    "labels": [
                        "EGFR_001", "EGFR_002", "EGFR_003", "EGFR_004", "EGFR_005",
                    ],
                    "values": [0.42, 0.61, 0.88, 1.34, 2.10],  # nM
                    "x_label": "Peptide",
                    "y_label": "Predicted Kd (nM)",
                },
                style={"palette": "viridis", "log_y": True},
            )
            print(f"  chart_id={chart.id}  type={chart.chart_type}  url={chart.url}")
        except LigandAIError as e:
            print(f"  (charts.generate skipped: {type(e).__name__}: {e})")
            chart = None

        # 2) Scatter plot — typical iPSAE × predicted Kd visualization -----
        print("\n== charts.generate(chart_type='scatter', ...) ==")
        try:
            client.charts.generate(
                chart_type="scatter",
                title="iPSAE vs predicted Kd (lower-left = elite binders)",
                data={
                    "x": [0.42, 0.55, 0.71, 0.78, 0.83, 0.91],
                    "y": [350.0, 120.0, 38.0, 12.0, 3.4, 0.9],
                    "x_label": "iPSAE",
                    "y_label": "Predicted Kd (nM)",
                    "hover_labels": [
                        "EGFR_001", "EGFR_002", "EGFR_003",
                        "EGFR_004", "EGFR_005", "EGFR_006",
                    ],
                },
                style={"log_y": True, "highlight_threshold": 0.66},
            )
            print("  scatter chart submitted")
        except LigandAIError as e:
            print(f"  (charts.generate scatter skipped: {type(e).__name__}: {e})")

        # 3) Save chart directly to a program (saveToProgram=) -------------
        print("\n== charts.generate(..., save_to_program=<program_id>) ==")
        program_id_env = os.environ.get("LIGANDAI_PROGRAM_ID")
        if program_id_env and program_id_env.isdigit():
            try:
                chart_p = client.charts.generate(
                    chart_type="line",
                    title="Cumulative folds over time",
                    data={
                        "x": list(range(1, 11)),
                        "y": [3, 7, 12, 18, 25, 33, 42, 52, 63, 75],
                        "x_label": "Day",
                        "y_label": "Folds completed",
                    },
                    save_to_program=int(program_id_env),
                )
                print(f"  chart_id={chart_p.id}  saved_to_program={program_id_env}")
            except LigandAIError as e:
                print(f"  (save_to_program skipped: {type(e).__name__}: {e})")
        else:
            print("  set LIGANDAI_PROGRAM_ID=<int> to test save_to_program")

        # 4) Re-fetch a chart by ID ---------------------------------------
        if chart is not None:
            print("\n== charts.get(chart_id) ==")
            try:
                refetched = client.charts.get(chart.id)
                print(f"  chart_id={refetched.id}  type={refetched.chart_type}")
            except LigandAIError as e:
                print(f"  (charts.get skipped: {type(e).__name__}: {e})")

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())

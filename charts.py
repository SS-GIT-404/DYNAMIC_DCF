"""
charts.py — Matplotlib figures for the valuation output (tornado + sensitivity).

Kept separate from the engine so both the CLI (save PNGs) and the Streamlit app
(st.pyplot) can render the same figures. Colours follow a CVD-validated palette:
categorical blue/orange for the tornado's two directions, a single-hue blue ramp
for the sequential sensitivity heatmap.
"""

from __future__ import annotations

from typing import List, Optional

import matplotlib
matplotlib.use("Agg")                      # headless-safe default
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

# --- palette (validated in the dataviz reference) -------------------------- #
BLUE = "#2a78d6"       # categorical slot 1  -> "higher input"
ORANGE = "#eb6834"     # categorical slot 6  -> "lower input"
INK = "#0b0b0b"
SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"
BASELINE = "#c3c2b7"
# sequential blue ramp (steps 100 -> 700)
_BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5",
              "#256abf", "#184f95", "#0d366b"]
BLUE_CMAP = LinearSegmentedColormap.from_list("seq_blue", _BLUE_RAMP)


def _style(ax):
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.title.set_color(INK)


def tornado_figure(rows: List[dict], current_price: Optional[float] = None,
                   title: str = "Sensitivity of implied price"):
    """Horizontal tornado: each assumption's low/high implied-price outcomes vs base."""
    rows = [r for r in rows if r["swing"] == r["swing"]]          # drop NaNs
    rows = sorted(rows, key=lambda r: r["swing"])                 # smallest at top
    base = rows[0]["base"] if rows else 0.0
    labels = [r["label"] for r in rows]
    y = range(len(rows))

    fig, ax = plt.subplots(figsize=(8.2, 0.62 * len(rows) + 1.4))
    _style(ax)
    for i, r in enumerate(rows):
        lo, hi = r["price_low"], r["price_high"]
        left, right = min(lo, hi), max(lo, hi)
        # bar from base to each endpoint, coloured by which input produced it
        ax.barh(i, lo - base, left=base, color=ORANGE, height=0.62,
                edgecolor=SURFACE, linewidth=1.5, zorder=3)
        ax.barh(i, hi - base, left=base, color=BLUE, height=0.62,
                edgecolor=SURFACE, linewidth=1.5, zorder=3)
        # direct labels at each end
        ax.text(left - (right - left) * 0.01 - 0.5, i, f"${left:,.0f}",
                va="center", ha="right", color=SECONDARY, fontsize=8.5)
        ax.text(right + (right - left) * 0.01 + 0.5, i, f"${right:,.0f}",
                va="center", ha="left", color=SECONDARY, fontsize=8.5)

    ax.axvline(base, color=INK, linewidth=1.4, zorder=4)
    ax.text(base, len(rows) - 0.35, f"  base ${base:,.0f}", color=INK,
            fontsize=8.5, va="bottom", ha="left")
    if current_price:
        ax.axvline(current_price, color="#d03b3b", linewidth=1.4,
                   linestyle=(0, (4, 2)), zorder=4)
        ax.text(current_price, -0.9, f"market ${current_price:,.0f}",
                color="#d03b3b", fontsize=8.5, va="top", ha="center")

    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, color=INK, fontsize=9.5)
    ax.set_xlabel("Implied share price ($)", color=SECONDARY, fontsize=9)
    ax.set_title(title, fontsize=12, loc="left", pad=12, fontweight="bold")
    ax.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)

    # legend (two directions) — identity never by colour alone -> labelled
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=BLUE, label="higher input value"),
                       Patch(color=ORANGE, label="lower input value")],
              loc="lower right", frameon=False, fontsize=8.5, labelcolor=SECONDARY)
    fig.tight_layout()
    return fig


def sensitivity_figure(grid: dict, title: str = "Implied price ($): WACC x terminal growth"):
    """Heatmap of implied price across the WACC x terminal-growth grid."""
    matrix = grid["matrix"]
    waccs = grid["waccs"]
    gs = grid["growths"]

    fig, ax = plt.subplots(figsize=(1.05 * len(gs) + 2.4, 0.55 * len(waccs) + 2.0))
    _style(ax)
    im = ax.imshow(matrix, cmap=BLUE_CMAP, aspect="auto")

    ax.set_xticks(range(len(gs)))
    ax.set_xticklabels([f"{g*100:.1f}%" for g in gs])
    ax.set_yticks(range(len(waccs)))
    ax.set_yticklabels([f"{w*100:.2f}%" for w in waccs])
    ax.set_xlabel("Terminal growth", color=SECONDARY, fontsize=9)
    ax.set_ylabel("WACC", color=SECONDARY, fontsize=9)
    ax.set_title(title, fontsize=12, loc="left", pad=12, fontweight="bold")

    # value labels; ink flips to white on the darker (higher-value) cells
    flat = [v for row in matrix for v in row if v == v]
    vmax, vmin = (max(flat), min(flat)) if flat else (1, 0)
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if v != v:
                continue
            frac = (v - vmin) / (vmax - vmin) if vmax > vmin else 0.5
            ax.text(j, i, f"{v:,.0f}", ha="center", va="center", fontsize=8.5,
                    color="#ffffff" if frac > 0.55 else INK)

    # mark the base cell with a ring
    try:
        bi = min(range(len(waccs)), key=lambda k: abs(waccs[k] - grid["base_wacc"]))
        bj = min(range(len(gs)), key=lambda k: abs(gs[k] - grid["base_growth"]))
        ax.add_patch(plt.Rectangle((bj - 0.5, bi - 0.5), 1, 1, fill=False,
                                   edgecolor=INK, linewidth=2.0))
    except (ValueError, KeyError):
        pass

    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()
    return fig


def save_all(res, grid, tornado_rows, outdir: str = "output", prefix: str = ""):
    """Write both figures to PNG; returns the file paths."""
    import os
    os.makedirs(outdir, exist_ok=True)
    p = (prefix + "_") if prefix else ""
    t_path = os.path.join(outdir, f"{p}tornado.png")
    s_path = os.path.join(outdir, f"{p}sensitivity.png")
    cp = res.assumptions.current_price
    tornado_figure(tornado_rows, current_price=cp,
                   title=f"{res.ticker}: implied price sensitivity").savefig(
        t_path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    sensitivity_figure(grid, title=f"{res.ticker}: implied price ($) — WACC x terminal g").savefig(
        s_path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    plt.close("all")
    return t_path, s_path

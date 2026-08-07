"""
app.py — Interactive DCF valuation (Streamlit).

Type a ticker, and the app runs the *same* engine the CLI and the Excel workbook
use (`valuation.py`), showing the WACC build, the FCFF forecast, the valuation
bridge, a WACC x terminal-growth sensitivity grid and a tornado chart — all live,
with every assumption editable in the sidebar.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import io
from typing import Optional

import pandas as pd
import streamlit as st

import charts
from build_excel import build_workbook
from market_data import MarketData, fetch_market_data
from sec_pull import DISPLAY_ORDER, Financials, pull_financials
from valuation import (Assumptions, derive_assumptions, run_dcf,
                       sensitivity_grid, tornado)

st.set_page_config(page_title="Dynamic DCF Valuation", page_icon="📊",
                   layout="wide", initial_sidebar_state="expanded")


# --------------------------------------------------------------------------- #
# Cached data access
# --------------------------------------------------------------------------- #

@st.cache_data(show_spinner=False, ttl=60 * 60)
def load_financials(ticker: str) -> Financials:
    return pull_financials(ticker)


@st.cache_data(show_spinner=False, ttl=60 * 5)
def load_market(ticker: str) -> MarketData:
    return fetch_market_data(ticker)


def fmt_money(x: Optional[float], unit: str = "M") -> str:
    """Compact money label. The sign leads the currency symbol: -$3.9B, not $-3.9B."""
    if x is None or x != x:
        return "n/a"
    v = x / 1e6 if unit == "M" else x
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e6:
        return f"{sign}${v/1e6:,.2f}T"
    if v >= 1e3:
        return f"{sign}${v/1e3:,.1f}B"
    return f"{sign}${v:,.0f}M"


def pct(x: Optional[float]) -> str:
    return "n/a" if x is None or x != x else f"{x*100:.1f}%"


# --------------------------------------------------------------------------- #
# Header / input
# --------------------------------------------------------------------------- #

st.title("Dynamic DCF Valuation Model")
st.caption("Ticker-driven discounted cash flow built on SEC EDGAR XBRL filings. "
           "Every assumption is explicit and editable — nothing is hidden in a formula.")

with st.form("ticker_form"):
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        ticker = st.text_input("Ticker (US-listed)", value="AAPL",
                               placeholder="AAPL, JPM, O, RIVN…").strip().upper()
    with c2:
        years = st.selectbox("Forecast horizon", [5, 7, 10], index=0)
    with c3:
        st.write("")
        submitted = st.form_submit_button("Run valuation", width="stretch",
                                          type="primary")

if not ticker:
    st.stop()

# Re-run on first load as well as on submit.
if "ran" not in st.session_state:
    st.session_state["ran"] = True

try:
    with st.spinner(f"Pulling {ticker} filings from SEC EDGAR…"):
        fin = load_financials(ticker)
except Exception as exc:  # noqa: BLE001 — surface a clean message to the user
    st.error(f"Could not load **{ticker}** — {exc}")
    st.info("Check the symbol is a US-listed company that files with the SEC.")
    st.stop()

market = load_market(ticker)
base = derive_assumptions(fin, market, forecast_years=years)


# --------------------------------------------------------------------------- #
# Sidebar — editable assumptions
# --------------------------------------------------------------------------- #

st.sidebar.header("Assumptions")
st.sidebar.caption(f"Seeded from {fin.ticker}'s own filings. Edit anything.")

if base.defaulted:
    st.sidebar.warning(
        "Some inputs could not be derived from the filings and use generic "
        "defaults (marked ⚠ below). Set them manually for a credible result.")


def flag(attr: str, label: str) -> str:
    return f"⚠ {label}" if base.is_defaulted(attr) else label


def pct_slider(attr: str, label: str, lo: float, hi: float, value: float,
               step: float = 0.1, decimals: int = 1) -> float:
    """
    Percentage slider. Streamlit's `format` renders the RAW value, so a decimal
    like 0.052 would display as "0.1%". We therefore work in percentage units
    (5.2) on screen and convert back to a decimal (0.052) for the model.
    """
    shown = st.slider(flag(attr, label), lo, hi, float(value) * 100.0, step,
                      format=f"%.{decimals}f%%")
    return shown / 100.0


a = Assumptions(**base.to_dict())

with st.sidebar.expander("Growth & margins", expanded=True):
    a.initial_growth = pct_slider("initial_growth", "Revenue growth — year 1",
                                  -20.0, 60.0, base.initial_growth, 0.5)
    a.terminal_growth = pct_slider("terminal_growth", "Terminal growth",
                                   0.0, 5.0, base.terminal_growth, 0.1)
    a.target_ebit_margin = pct_slider("target_ebit_margin", "EBIT margin — steady state",
                                      -20.0, 70.0, base.target_ebit_margin, 0.5)
    a.tax_rate = pct_slider("tax_rate", "Effective tax rate",
                            0.0, 40.0, base.tax_rate, 0.5)

with st.sidebar.expander("Capital intensity (% of revenue)", expanded=False):
    a.da_pct = pct_slider("da_pct", "D&A", 0.0, 50.0, base.da_pct, 0.2)
    a.capex_pct = pct_slider("capex_pct", "Capex", 0.0, 60.0, base.capex_pct, 0.2)
    a.nwc_pct = pct_slider("nwc_pct", "Net working capital",
                           -30.0, 40.0, base.nwc_pct, 0.5)

with st.sidebar.expander("Cost of capital", expanded=True):
    a.risk_free_rate = pct_slider("risk_free_rate", "Risk-free rate",
                                  0.0, 10.0, base.risk_free_rate, 0.1, decimals=2)
    a.equity_risk_premium = pct_slider("equity_risk_premium", "Equity risk premium",
                                       2.0, 10.0, base.equity_risk_premium, 0.25,
                                       decimals=2)
    a.beta = st.slider(flag("beta", "Beta"), 0.0, 2.5, float(base.beta), 0.01)
    a.pretax_cost_of_debt = pct_slider("pretax_cost_of_debt", "Pre-tax cost of debt",
                                       0.0, 15.0, base.pretax_cost_of_debt, 0.25,
                                       decimals=2)

with st.sidebar.expander("Terminal / market", expanded=False):
    a.exit_ev_ebitda = st.slider("Exit EV/EBITDA", 2.0, 35.0,
                                 float(base.exit_ev_ebitda), 0.5)
    if base.current_price:
        a.current_price = st.number_input("Current share price ($)",
                                          value=float(base.current_price), step=1.0)
    else:
        a.current_price = st.number_input(
            "Current share price ($) — live quote unavailable", value=0.0, step=1.0) or None
    if base.shares:
        a.shares = st.number_input("Shares outstanding (M)",
                                   value=float(base.shares) / 1e6, step=1.0) * 1e6
    a.market_cap = (a.current_price * a.shares) if (a.current_price and a.shares) else base.market_cap

if st.sidebar.button("Reset to filing-derived defaults", width="stretch"):
    st.cache_data.clear()
    st.rerun()

res = run_dcf(a, ticker=fin.ticker, entity_name=fin.entity_name, sector=fin.sector)


# --------------------------------------------------------------------------- #
# Company header + warnings
# --------------------------------------------------------------------------- #

st.subheader(f"{fin.entity_name} ({fin.ticker})")
st.caption(f"CIK {fin.cik} · SIC {fin.sic} — {fin.sic_description} · "
           f"sector bucket: **{fin.sector}** · fiscal years {min(fin.fiscal_years)}–"
           f"{max(fin.fiscal_years)}")

for w in res.warnings:
    if w.startswith("FABRICATED") or w.startswith("BANK") or w.startswith("REIT"):
        st.error(f"⚠ {w}")
    else:
        st.warning(w)


# --------------------------------------------------------------------------- #
# Headline metrics
# --------------------------------------------------------------------------- #

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Current price",
          f"${a.current_price:,.2f}" if a.current_price else "n/a")

# A negative implied price is not a price — equity's floor is zero. Show the
# floored value and say plainly that the assumptions support no equity value.
if res.has_negative_equity:
    m2.metric("Implied — Gordon", "$0.00", delta="no equity value",
              delta_color="inverse")
    m3.metric("Implied — exit multiple", "$0.00", delta="no equity value",
              delta_color="inverse")
else:
    m2.metric("Implied — Gordon", f"${res.price_gordon:,.2f}",
              delta=pct(res.upside_gordon) if res.upside_gordon is not None else None)
    m3.metric("Implied — exit multiple", f"${res.price_exit:,.2f}",
              delta=pct(res.upside_exit) if res.upside_exit is not None else None)
m4.metric("WACC", pct(res.wacc.wacc))
m5.metric("Enterprise value", fmt_money(res.ev_gordon))

if res.has_negative_equity:
    st.info(f"Raw (unfloored) arithmetic gives "
            f"${res.price_gordon:,.2f} / ${res.price_exit:,.2f} per share. Shown as "
            "$0.00 above because a share cannot be worth less than nothing.")

st.divider()

tab_val, tab_fc, tab_sens, tab_hist, tab_assum = st.tabs(
    ["Valuation", "Forecast", "Sensitivity", "Historical (SEC)", "Assumptions & sources"])


# --------------------------------------------------------------------------- #
# Valuation tab
# --------------------------------------------------------------------------- #

with tab_val:
    left, right = st.columns([1.1, 1])
    with left:
        st.markdown("#### Valuation bridge")
        bridge = pd.DataFrame({
            "Gordon growth": [res.pv_fcff_sum, res.pv_tv_gordon, res.ev_gordon,
                              -a.net_debt, res.equity_gordon, res.price_gordon],
            "Exit multiple": [res.pv_fcff_sum, res.pv_tv_exit, res.ev_exit,
                              -a.net_debt, res.equity_exit, res.price_exit],
        }, index=["PV of explicit FCFF", "PV of terminal value", "Enterprise value",
                  "Less: net debt", "Equity value", "Implied share price ($)"])
        disp = bridge.copy()
        for c in disp.columns:
            disp[c] = [f"{v/1e6:,.0f}" if i < 5 else f"${v:,.2f}"
                       for i, v in enumerate(bridge[c])]
        st.dataframe(disp, width="stretch")
        st.caption("Values in $ millions except the implied share price.")

    with right:
        st.markdown("#### WACC build (CAPM)")
        w = res.wacc
        wacc_df = pd.DataFrame({
            "Value": [pct(a.risk_free_rate), f"{a.beta:.2f}", pct(a.equity_risk_premium),
                      pct(w.cost_of_equity), pct(a.pretax_cost_of_debt),
                      pct(w.after_tax_cost_of_debt), pct(w.weight_equity),
                      pct(w.weight_debt), pct(w.wacc)],
        }, index=["Risk-free rate", "Beta", "Equity risk premium",
                  "→ Cost of equity", "Pre-tax cost of debt", "→ After-tax cost of debt",
                  "Weight of equity", "Weight of debt", "→ WACC"])
        st.dataframe(wacc_df, width="stretch")

        st.markdown("#### Cross-checks")
        cc = [f"Terminal value is **{pct(res.tv_pct_of_ev_gordon)}** of enterprise value",
              f"Gordon TV implies an exit multiple of "
              f"**{res.implied_exit_multiple_from_gordon:,.1f}x** EV/EBITDA",
              f"Exit multiple implies perpetuity growth of "
              f"**{pct(res.implied_growth_from_exit)}**"]
        if res.current_ev_ebitda:
            cc.append(f"Stock currently trades at ~**{res.current_ev_ebitda:,.1f}x** EV/EBITDA")
        for line in cc:
            st.markdown(f"- {line}")

    st.markdown("#### What moves the valuation")
    trn = tornado(a, sector=fin.sector)
    st.pyplot(charts.tornado_figure(trn, current_price=a.current_price,
                                    title=f"{fin.ticker}: implied price sensitivity"),
              width="stretch")


# --------------------------------------------------------------------------- #
# Forecast tab
# --------------------------------------------------------------------------- #

with tab_fc:
    st.markdown("#### Unlevered free cash flow forecast")
    rows = []
    for r in res.forecast:
        rows.append({
            "Year": r.year, "Growth": pct(r.growth),
            "Revenue": r.revenue / 1e6, "EBIT margin": pct(r.ebit_margin),
            "EBIT": r.ebit / 1e6, "NOPAT": r.nopat / 1e6, "D&A": r.da / 1e6,
            "Capex": r.capex / 1e6, "Δ NWC": r.d_nwc / 1e6,
            "FCFF": r.fcff / 1e6, "Disc. factor": round(r.discount_factor, 3),
            "PV of FCFF": r.pv_fcff / 1e6,
        })
    df = pd.DataFrame(rows).set_index("Year")
    st.dataframe(df.style.format({
        "Revenue": "{:,.0f}", "EBIT": "{:,.0f}", "NOPAT": "{:,.0f}", "D&A": "{:,.0f}",
        "Capex": "{:,.0f}", "Δ NWC": "{:,.0f}", "FCFF": "{:,.0f}",
        "PV of FCFF": "{:,.0f}",
    }), width="stretch")
    st.caption("$ millions. Sum of PV of FCFF: "
               f"**{res.pv_fcff_sum/1e6:,.0f}**")
    st.bar_chart(df[["FCFF", "PV of FCFF"]])


# --------------------------------------------------------------------------- #
# Sensitivity tab
# --------------------------------------------------------------------------- #

with tab_sens:
    st.markdown("#### Implied price — WACC × terminal growth")
    grid = sensitivity_grid(a, sector=fin.sector)
    sens_df = pd.DataFrame(
        grid["matrix"],
        index=[f"{w*100:.2f}%" for w in grid["waccs"]],
        columns=[f"{g*100:.1f}%" for g in grid["growths"]])
    sens_df.index.name = "WACC \\ g"
    st.dataframe(
        sens_df.style.format("${:,.2f}").background_gradient(cmap="Blues", axis=None),
        width="stretch")
    st.pyplot(charts.sensitivity_figure(
        grid, title=f"{fin.ticker}: implied price ($) — WACC × terminal g"),
        width="stretch")


# --------------------------------------------------------------------------- #
# Historical tab
# --------------------------------------------------------------------------- #

with tab_hist:
    st.markdown("#### Reported annual financials (SEC EDGAR 10-K)")
    years_ = fin.fiscal_years
    recs = {}
    for name in DISPLAY_ORDER:
        status = fin.item_status.get(name)
        if status == "not_applicable":
            recs[name] = ["n/a (sector)"] * len(years_)
        elif status == "not_found":
            recs[name] = ["—"] * len(years_)
        else:
            recs[name] = [
                f"{fin.line_items[name][y]/1e6:,.0f}"
                if fin.line_items[name].get(y) is not None else "—"
                for y in years_]
    hist = pd.DataFrame(recs, index=[f"FY{y}" for y in years_]).T
    st.dataframe(hist, width="stretch")
    st.caption("$ millions. 'n/a (sector)' means the concept does not apply to this "
               "filer's industry (e.g. COGS for a bank) — not a data error.")

    with st.expander("XBRL tags used"):
        st.dataframe(pd.DataFrame(
            {"Line item": list(fin.tags_used), "Tag(s)": list(fin.tags_used.values())}
        ).set_index("Line item"), width="stretch")


# --------------------------------------------------------------------------- #
# Assumptions tab + Excel export
# --------------------------------------------------------------------------- #

with tab_assum:
    st.markdown("#### Every assumption, and where it came from")
    src = base.sources
    labels = [
        ("initial_growth", "Revenue growth — year 1", pct(a.initial_growth)),
        ("terminal_growth", "Terminal growth", pct(a.terminal_growth)),
        ("start_ebit_margin", "EBIT margin — latest actual", pct(a.start_ebit_margin)),
        ("target_ebit_margin", "EBIT margin — steady state", pct(a.target_ebit_margin)),
        ("tax_rate", "Effective tax rate", pct(a.tax_rate)),
        ("da_pct", "D&A % of revenue", pct(a.da_pct)),
        ("capex_pct", "Capex % of revenue", pct(a.capex_pct)),
        ("nwc_pct", "NWC % of revenue", pct(a.nwc_pct)),
        ("risk_free_rate", "Risk-free rate", pct(a.risk_free_rate)),
        ("equity_risk_premium", "Equity risk premium", pct(a.equity_risk_premium)),
        ("beta", "Beta", f"{a.beta:.2f}"),
        ("pretax_cost_of_debt", "Pre-tax cost of debt", pct(a.pretax_cost_of_debt)),
        ("exit_ev_ebitda", "Exit EV/EBITDA", f"{a.exit_ev_ebitda:.1f}x"),
        ("base_revenue", "Base revenue", fmt_money(a.base_revenue)),
        ("total_debt", "Total debt", fmt_money(a.total_debt)),
    ]
    st.dataframe(pd.DataFrame([
        {"Assumption": lbl,
         "Value": val,
         "Derived?": "generic default" if base.is_defaulted(key) else "from filings/market",
         "Source": src.get(key, "—")}
        for key, lbl, val in labels
    ]).set_index("Assumption"), width="stretch")

    st.divider()
    st.markdown("#### Export")
    st.caption("Downloads a live Excel workbook — every calculation is a real formula, "
               "so it recalculates when you edit an assumption cell.")
    if st.button("Build Excel workbook", type="primary"):
        with st.spinner("Building workbook…"):
            import tempfile, os
            tmp = os.path.join(tempfile.mkdtemp(), f"{fin.ticker}_DCF_Model.xlsx")
            build_workbook(fin.ticker, forecast_years=a.forecast_years, out_path=tmp,
                           fin=fin, market=market, assumptions=a)
            with open(tmp, "rb") as fh:
                st.download_button("Download .xlsx", fh.read(),
                                   file_name=f"{fin.ticker}_DCF_Model.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument."
                                        "spreadsheetml.sheet")

st.divider()
st.caption("Data: SEC EDGAR XBRL company facts · Market data: Yahoo Finance snapshot. "
           "Educational model, not investment advice.")

"""
valuation.py — A transparent, ticker-driven DCF (unlevered free-cash-flow) engine.

Everything the model needs to know is exposed as a *named assumption* on the
`Assumptions` dataclass — nothing that drives value is buried inside a formula.
Assumptions are seeded with defaults derived from a company's own SEC history
(revenue growth, margins, capital intensity, effective tax rate) and from live
market data (price, market cap, beta), then can be overridden freely.

Pipeline
--------
    sec_pull.pull_financials(ticker)  ->  Financials  (historical drivers)
    market_data.fetch_market_data()   ->  MarketData  (price / cap / beta)
    derive_assumptions(...)           ->  Assumptions (defaults from the above)
    run_dcf(assumptions, ...)         ->  ValuationResult

Method (unlevered FCFF)
-----------------------
    FCFF_t = EBIT_t*(1 - tax) + D&A_t - Capex_t - ΔNWC_t
    WACC   = (E/V)*[rf + β*ERP] + (D/V)*[kd*(1 - tax)]
    EV     = Σ FCFF_t / (1+WACC)^t  +  PV(terminal value)
    Equity = EV - net debt ;  implied price = Equity / shares

Terminal value is computed two ways — Gordon growth and an EV/EBITDA exit
multiple — and both are carried through to an implied price.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from sec_pull import Financials, pull_financials
from market_data import MarketData, fetch_market_data

# Placeholder steady-state margin used ONLY for currently-lossmaking companies,
# where no meaningful margin can be derived from history. Always flagged as a
# fabricated default so it is never mistaken for a figure from the filings.
PRE_PROFIT_TARGET_MARGIN = 0.10


# --------------------------------------------------------------------------- #
# Assumptions
# --------------------------------------------------------------------------- #

@dataclass
class Assumptions:
    # --- forecast horizon -------------------------------------------------- #
    forecast_years: int = 5

    # --- revenue ----------------------------------------------------------- #
    initial_growth: float = 0.08        # year-1 revenue growth
    terminal_growth: float = 0.025      # perpetual growth (Gordon) & fade target
    fade_growth: bool = True            # linearly fade initial -> terminal

    # --- profitability ----------------------------------------------------- #
    target_ebit_margin: float = 0.30    # steady-state operating margin
    start_ebit_margin: float = 0.30     # latest actual margin (fade starts here)
    ramp_margin: bool = True            # fade start -> target over the horizon
    tax_rate: float = 0.21              # effective cash tax rate

    # --- capital intensity (% of revenue) ---------------------------------- #
    da_pct: float = 0.03                # depreciation & amortisation
    capex_pct: float = 0.03             # capital expenditure
    nwc_pct: float = 0.00               # net working capital / revenue

    # --- WACC / CAPM ------------------------------------------------------- #
    risk_free_rate: float = 0.042
    equity_risk_premium: float = 0.050
    beta: float = 1.10
    pretax_cost_of_debt: float = 0.05

    # --- terminal exit multiple ------------------------------------------- #
    exit_ev_ebitda: float = 14.0

    # --- capital structure / bridge (from market + SEC) -------------------- #
    market_cap: Optional[float] = None
    total_debt: float = 0.0
    net_debt: float = 0.0
    shares: Optional[float] = None
    current_price: Optional[float] = None
    base_revenue: float = 0.0           # latest actual revenue (forecast anchor)
    base_nwc: float = 0.0               # latest actual net working capital

    # provenance notes, keyed by assumption -> short citation string
    sources: Dict[str, str] = field(default_factory=dict)
    # assumptions that could NOT be derived from the filings and fell back to a
    # hard-coded default — these are guesses, and every surface must say so.
    defaulted: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def is_defaulted(self, name: str) -> bool:
        return name in self.defaulted


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #

@dataclass
class ForecastRow:
    year: int
    growth: float
    revenue: float
    ebit_margin: float
    ebit: float
    nopat: float
    da: float
    capex: float
    nwc: float
    d_nwc: float
    fcff: float
    discount_factor: float
    pv_fcff: float


@dataclass
class WaccResult:
    cost_of_equity: float
    after_tax_cost_of_debt: float
    weight_equity: float
    weight_debt: float
    wacc: float


@dataclass
class ValuationResult:
    ticker: str
    entity_name: str
    sector: str
    assumptions: Assumptions
    wacc: WaccResult
    forecast: List[ForecastRow]
    pv_fcff_sum: float
    # Gordon-growth terminal value
    tv_gordon: float
    pv_tv_gordon: float
    ev_gordon: float
    equity_gordon: float
    price_gordon: float
    # exit-multiple terminal value
    tv_exit: float
    pv_tv_exit: float
    ev_exit: float
    equity_exit: float
    price_exit: float
    # diagnostics / cross-checks
    tv_pct_of_ev_gordon: float
    implied_exit_multiple_from_gordon: float
    implied_growth_from_exit: Optional[float]
    current_ev_ebitda: Optional[float]
    upside_gordon: Optional[float]
    upside_exit: Optional[float]
    warnings: List[str] = field(default_factory=list)

    @property
    def has_negative_equity(self) -> bool:
        """True when the assumptions support no equity value at all."""
        return (self.price_gordon == self.price_gordon and self.price_gordon < 0) or \
               (self.price_exit == self.price_exit and self.price_exit < 0)

    def display_price(self, method: str = "gordon") -> float:
        """
        Implied price floored at zero for presentation.

        The raw `price_gordon` / `price_exit` stay untouched so the arithmetic is
        auditable, but equity is a limited-liability claim: a negative implied
        price means "no equity value", not "worth less than nothing".
        """
        raw = self.price_gordon if method == "gordon" else self.price_exit
        if raw != raw:            # NaN
            return raw
        return max(raw, 0.0)


# --------------------------------------------------------------------------- #
# Deriving default assumptions from history + market
# --------------------------------------------------------------------------- #

def _clean(series: Dict[int, Optional[float]]) -> Dict[int, float]:
    return {y: v for y, v in series.items() if v is not None}


def _cagr(series: Dict[int, float], years: int) -> Optional[float]:
    if len(series) < 2:
        return None
    ys = sorted(series)
    ys = ys[-(years + 1):]                       # need years+1 points for `years` steps
    start, end = series[ys[0]], series[ys[-1]]
    n = ys[-1] - ys[0]
    if n <= 0 or start is None or end is None or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1.0 / n) - 1.0


def _mean_ratio(num: Dict[int, float], den: Dict[int, float], n: int = 5,
                lo: float = -1e9, hi: float = 1e9) -> Optional[float]:
    common = sorted(set(num) & set(den))[-n:]
    vals = []
    for y in common:
        d = den[y]
        if d and d != 0:
            r = num[y] / d
            if lo <= r <= hi:
                vals.append(r)
    return statistics.mean(vals) if vals else None


def derive_assumptions(fin: Financials, market: MarketData,
                       forecast_years: int = 5) -> Assumptions:
    """Seed an Assumptions object from the company's SEC history and live market data."""
    rev = _clean(fin.series("Revenue"))
    ebit = _clean(fin.series("OperatingIncome"))
    da = _clean(fin.series("DA"))
    capex = _clean(fin.series("Capex"))
    tax = _clean(fin.series("IncomeTaxExpense"))
    pretax = _clean(fin.series("PretaxIncome"))
    ar = _clean(fin.series("AccountsReceivable"))
    inv = _clean(fin.series("Inventory"))
    ap = _clean(fin.series("AccountsPayable"))
    interest = _clean(fin.series("InterestExpense"))
    total_debt = _clean(fin.series("TotalDebt"))
    net_debt = _clean(fin.series("NetDebt"))

    a = Assumptions(forecast_years=forecast_years)
    src = a.sources

    def mark_default(name: str, why: str) -> None:
        """Record that an assumption is a hard-coded guess, not a derived figure."""
        a.defaulted.append(name)
        src[name] = f"DEFAULT (not derivable from filings) — {why}"

    latest_year = max(rev) if rev else None
    a.base_revenue = rev[latest_year] if latest_year else 0.0
    if latest_year:
        src["base_revenue"] = f"{fin.ticker} FY{latest_year} Revenue (10-K)"

    # --- revenue growth: anchor on 5y CAGR, blended with 3y, clamped ------- #
    cagr5 = _cagr(rev, 5)
    cagr3 = _cagr(rev, 3)
    growth_est = next((g for g in (cagr5, cagr3) if g is not None), a.initial_growth)
    if cagr5 is not None and cagr3 is not None:
        growth_est = 0.5 * cagr5 + 0.5 * cagr3       # balance trend vs. recent
    a.initial_growth = _bound(growth_est, a.terminal_growth, 0.25)
    src["initial_growth"] = (f"blend of 3y ({_pct(cagr3)}) & 5y ({_pct(cagr5)}) "
                             f"historical revenue CAGR")

    # --- margins ----------------------------------------------------------- #
    # NOTE: banks and REITs do not report OperatingIncome, so there is no EBIT to
    # derive a margin from. Falling back to a default here would silently fabricate
    # the single most important driver of the valuation — so it is flagged loudly.
    margin = _mean_ratio(ebit, rev, n=5, lo=-1.0, hi=1.0)
    latest_margin = (ebit[max(ebit)] / rev[max(ebit)]) if ebit and max(ebit) in rev else margin
    if margin is not None:
        a.start_ebit_margin = latest_margin if latest_margin is not None else margin
        if a.start_ebit_margin < 0:
            # Pre-profit company. Extrapolating today's loss margin forever would
            # drive FCFF and terminal value permanently negative and produce a
            # meaningless negative "fair value". For a lossmaking business the path
            # to profitability IS the valuation question, so we surface it as an
            # explicit, flagged assumption rather than silently extrapolating losses.
            a.target_ebit_margin = PRE_PROFIT_TARGET_MARGIN
            mark_default("target_ebit_margin",
                         f"company is currently lossmaking "
                         f"({a.start_ebit_margin*100:.0f}% EBIT margin) — a path to "
                         f"profitability must be assumed; "
                         f"{PRE_PROFIT_TARGET_MARGIN*100:.0f}% placeholder, set this "
                         f"yourself")
            src["start_ebit_margin"] = "latest FY operating margin (EBIT/Revenue)"
        else:
            a.target_ebit_margin = margin
            src["target_ebit_margin"] = "5y avg operating margin (EBIT/Revenue)"
            src["start_ebit_margin"] = "latest FY operating margin (EBIT/Revenue)"
    else:
        mark_default("target_ebit_margin", "no OperatingIncome reported (typical for "
                                           "banks/REITs) — set this manually")
        a.defaulted.append("start_ebit_margin")
        src["start_ebit_margin"] = src["target_ebit_margin"]

    # --- effective tax rate ------------------------------------------------ #
    eff = _mean_ratio(tax, pretax, n=5, lo=0.0, hi=0.6)
    if eff is not None:
        a.tax_rate = _bound(eff, 0.05, 0.40)
        src["tax_rate"] = "5y avg effective tax (tax expense / pre-tax income)"
    else:
        mark_default("tax_rate", "using 21% US federal statutory rate")

    # --- capital intensity ------------------------------------------------- #
    da_pct = _mean_ratio(da, rev, n=5, lo=0.0, hi=0.5)
    capex_pct = _mean_ratio(capex, rev, n=5, lo=0.0, hi=0.6)
    if da_pct is not None:
        a.da_pct = da_pct
        src["da_pct"] = "5y avg D&A / revenue"
    else:
        mark_default("da_pct", "no D&A reported")
    if capex_pct is not None:
        a.capex_pct = capex_pct
        src["capex_pct"] = "5y avg capex / revenue"
    else:
        mark_default("capex_pct", "no capex reported (banks have no meaningful capex)")

    # --- net working capital ---------------------------------------------- #
    nwc = {y: (ar.get(y, 0.0) + inv.get(y, 0.0) - ap.get(y, 0.0))
           for y in (set(ar) | set(inv) | set(ap))}
    nwc_pct = _mean_ratio(nwc, rev, n=5, lo=-1.0, hi=1.0)
    if nwc_pct is not None:
        a.nwc_pct = nwc_pct
        src["nwc_pct"] = "5y avg net working capital (AR + inventory - AP) / revenue"
    else:
        a.nwc_pct = 0.0
        mark_default("nwc_pct", "AR/inventory/AP not reported — NWC change set to zero")
    if latest_year and latest_year in nwc:
        a.base_nwc = nwc[latest_year]
    else:
        a.base_nwc = a.base_revenue * a.nwc_pct

    # --- cost of debt ------------------------------------------------------ #
    kd = _mean_ratio(interest, total_debt, n=5, lo=0.0, hi=0.20)
    if kd is not None and kd > 0:
        a.pretax_cost_of_debt = _bound(kd, 0.02, 0.10)
        src["pretax_cost_of_debt"] = "interest expense / total debt (historical avg)"
    else:
        mark_default("pretax_cost_of_debt", "interest expense and/or total debt not "
                                            "reported")

    # --- capital structure / bridge --------------------------------------- #
    a.total_debt = total_debt[max(total_debt)] if total_debt else 0.0
    if net_debt:
        a.net_debt = net_debt[max(net_debt)]
    else:
        # fall back to (total debt - cash - short-term investments) at latest year
        cash = _clean(fin.series("Cash"))
        sti = _clean(fin.series("ShortTermInvestments"))
        ly = max(total_debt) if total_debt else (max(cash) if cash else None)
        a.net_debt = (a.total_debt
                      - (cash.get(ly, 0.0) if ly else 0.0)
                      - (sti.get(ly, 0.0) if ly else 0.0))
    a.shares = market.shares or fin.shares_outstanding
    a.current_price = market.price
    a.market_cap = market.market_cap or (
        (a.current_price * a.shares) if a.current_price and a.shares else None)
    if a.total_debt:
        src["total_debt"] = f"{fin.ticker} latest 10-K total debt"
    if market.beta is not None:
        a.beta = market.beta
        src["beta"] = f"{market.source} equity beta"
    else:
        mark_default("beta", "live market beta unavailable")

    src["risk_free_rate"] = "assumption — proxy for 10y US Treasury yield"
    src["equity_risk_premium"] = "assumption — long-run US equity risk premium (~5%)"
    src["terminal_growth"] = "assumption — near long-run nominal GDP growth"
    src["exit_ev_ebitda"] = "assumption — mature-business EV/EBITDA exit multiple"

    return a


# --------------------------------------------------------------------------- #
# Core calculations
# --------------------------------------------------------------------------- #

def compute_wacc(a: Assumptions) -> WaccResult:
    cost_equity = a.risk_free_rate + a.beta * a.equity_risk_premium
    after_tax_kd = a.pretax_cost_of_debt * (1.0 - a.tax_rate)
    e = a.market_cap or 0.0
    d = max(a.total_debt, 0.0)
    v = e + d
    if v <= 0:
        we, wd = 1.0, 0.0
    else:
        we, wd = e / v, d / v
    wacc = we * cost_equity + wd * after_tax_kd
    return WaccResult(cost_equity, after_tax_kd, we, wd, wacc)


def build_forecast(a: Assumptions, wacc: float) -> List[ForecastRow]:
    n = a.forecast_years
    rows: List[ForecastRow] = []
    rev_prev = a.base_revenue
    nwc_prev = a.base_nwc
    for t in range(1, n + 1):
        # revenue growth fades linearly from initial to terminal over the horizon
        if a.fade_growth and n > 1:
            g = a.initial_growth + (a.terminal_growth - a.initial_growth) * (t - 1) / (n - 1)
        else:
            g = a.initial_growth
        rev = rev_prev * (1.0 + g)

        # operating margin ramps from the latest actual toward the target margin
        if a.ramp_margin and n >= 1:
            margin = a.start_ebit_margin + (a.target_ebit_margin - a.start_ebit_margin) * t / n
        else:
            margin = a.target_ebit_margin

        ebit = rev * margin
        nopat = ebit * (1.0 - a.tax_rate)
        da = rev * a.da_pct
        capex = rev * a.capex_pct
        nwc = rev * a.nwc_pct
        d_nwc = nwc - nwc_prev
        fcff = nopat + da - capex - d_nwc
        df = 1.0 / (1.0 + wacc) ** t
        rows.append(ForecastRow(
            year=t, growth=g, revenue=rev, ebit_margin=margin, ebit=ebit,
            nopat=nopat, da=da, capex=capex, nwc=nwc, d_nwc=d_nwc,
            fcff=fcff, discount_factor=df, pv_fcff=fcff * df,
        ))
        rev_prev, nwc_prev = rev, nwc
    return rows


def run_dcf(a: Assumptions, ticker: str = "", entity_name: str = "",
            sector: str = "general") -> ValuationResult:
    warnings: List[str] = []
    wacc_res = compute_wacc(a)
    wacc = wacc_res.wacc

    if wacc <= a.terminal_growth:
        warnings.append(
            f"WACC ({_pct(wacc)}) <= terminal growth ({_pct(a.terminal_growth)}): "
            "Gordon terminal value is undefined/negative. Lower terminal growth or "
            "revisit WACC.")

    forecast = build_forecast(a, wacc)
    pv_fcff_sum = sum(r.pv_fcff for r in forecast)
    last = forecast[-1]
    n = a.forecast_years
    df_n = 1.0 / (1.0 + wacc) ** n

    # --- terminal value: Gordon growth ------------------------------------ #
    if wacc > a.terminal_growth:
        tv_gordon = last.fcff * (1.0 + a.terminal_growth) / (wacc - a.terminal_growth)
    else:
        tv_gordon = float("nan")
    pv_tv_gordon = tv_gordon * df_n
    ev_gordon = pv_fcff_sum + pv_tv_gordon
    equity_gordon = ev_gordon - a.net_debt
    price_gordon = equity_gordon / a.shares if a.shares else float("nan")

    # --- terminal value: exit EV/EBITDA multiple -------------------------- #
    ebitda_n = last.ebit + last.da
    tv_exit = ebitda_n * a.exit_ev_ebitda
    pv_tv_exit = tv_exit * df_n
    ev_exit = pv_fcff_sum + pv_tv_exit
    equity_exit = ev_exit - a.net_debt
    price_exit = equity_exit / a.shares if a.shares else float("nan")

    # --- diagnostics / cross-checks --------------------------------------- #
    tv_pct = pv_tv_gordon / ev_gordon if ev_gordon else float("nan")
    implied_exit_mult = tv_gordon / ebitda_n if ebitda_n else float("nan")
    # exit multiple implies a perpetuity growth g: TV = FCFF*(1+g)/(wacc-g)
    implied_g = _implied_growth(tv_exit, last.fcff, wacc)

    current_ev_ebitda = None
    if a.market_cap is not None and ebitda_n:
        # use the latest ACTUAL EBITDA proxy = base revenue * (target margin + da)
        base_ebitda = a.base_revenue * (a.start_ebit_margin + a.da_pct)
        if base_ebitda:
            current_ev_ebitda = (a.market_cap + a.net_debt) / base_ebitda

    upside_g = (price_gordon / a.current_price - 1.0) if a.current_price else None
    upside_x = (price_exit / a.current_price - 1.0) if a.current_price else None

    # Fabricated inputs are the most dangerous failure mode: the model still
    # produces a confident-looking number. Name them first and by name.
    if a.defaulted:
        key_drivers = [d for d in a.defaulted
                       if d in ("target_ebit_margin", "start_ebit_margin",
                                "capex_pct", "tax_rate", "da_pct")]
        if key_drivers:
            pretty = ", ".join(sorted(set(key_drivers)))
            warnings.append(
                f"FABRICATED INPUTS: {pretty} could NOT be derived from this "
                "company's filings and fell back to generic defaults. The implied "
                "price is not evidence-based until you set these manually.")

    # --- pre-profit / negative-cash-flow diagnostics ----------------------- #
    if a.start_ebit_margin < 0:
        warnings.append(
            f"PRE-PROFIT: {ticker or 'this company'} is currently lossmaking "
            f"(EBIT margin {_pct(a.start_ebit_margin)}). A DCF on a lossmaking "
            "business is driven almost entirely by the assumed path to "
            "profitability and terminal margin — the output is a scenario, not a "
            "measurement. Set the steady-state EBIT margin deliberately.")
    if last.fcff < 0:
        warnings.append(
            "Terminal-year FCFF is negative, so the Gordon terminal value is "
            "meaningless (a perpetuity of losses). Raise the steady-state margin or "
            "extend the horizon until the business turns cash-generative before "
            "reading any implied price.")
    if equity_gordon < 0 or equity_exit < 0:
        warnings.append(
            "Implied equity value is NEGATIVE. Equity is a limited-liability claim, "
            "so its floor is zero — read this as 'these assumptions support no "
            "equity value', not as a price target.")

    if tv_pct == tv_pct and tv_pct > 0.85:
        warnings.append(
            f"Terminal value is {_pct(tv_pct)} of enterprise value — the result is "
            "highly sensitive to terminal assumptions.")
    if a.net_debt < 0 and abs(a.net_debt) > 0.25 * (a.market_cap or float("inf")):
        warnings.append(
            f"Net cash ({_money(-a.net_debt)}) exceeds 25% of market cap, so it adds "
            "materially to equity value. Verify that this cash is genuinely surplus "
            "and not operating float (it usually is not, for financial firms).")
    if sector == "bank":
        warnings.append(
            "BANK: an unlevered FCFF DCF does not fit a bank's economics (deposits "
            "are operating funding, not debt; there is no meaningful capex/NWC). "
            "Treat this output as illustrative only — banks are better valued with a "
            "dividend-discount / residual-income / P-B-vs-ROE model.")
    if sector in ("reit", "real_estate"):
        warnings.append(
            "REIT: heavy real-estate depreciation makes GAAP FCFF understate cash "
            "generation. A REIT is better valued on FFO/AFFO or NAV — treat this DCF "
            "as a cross-check, not the primary lens.")

    return ValuationResult(
        ticker=ticker, entity_name=entity_name, sector=sector,
        assumptions=a, wacc=wacc_res, forecast=forecast, pv_fcff_sum=pv_fcff_sum,
        tv_gordon=tv_gordon, pv_tv_gordon=pv_tv_gordon, ev_gordon=ev_gordon,
        equity_gordon=equity_gordon, price_gordon=price_gordon,
        tv_exit=tv_exit, pv_tv_exit=pv_tv_exit, ev_exit=ev_exit,
        equity_exit=equity_exit, price_exit=price_exit,
        tv_pct_of_ev_gordon=tv_pct, implied_exit_multiple_from_gordon=implied_exit_mult,
        implied_growth_from_exit=implied_g, current_ev_ebitda=current_ev_ebitda,
        upside_gordon=upside_g, upside_exit=upside_x, warnings=warnings,
    )


def value_ticker(ticker: str, forecast_years: int = 5,
                 overrides: Optional[dict] = None,
                 fin: Optional[Financials] = None,
                 market: Optional[MarketData] = None) -> ValuationResult:
    """Full convenience path: pull data, seed assumptions, apply overrides, run DCF."""
    fin = fin or pull_financials(ticker)
    market = market if market is not None else fetch_market_data(ticker)
    a = derive_assumptions(fin, market, forecast_years=forecast_years)
    if overrides:
        for k, v in overrides.items():
            if hasattr(a, k):
                setattr(a, k, v)
    return run_dcf(a, ticker=fin.ticker, entity_name=fin.entity_name, sector=fin.sector)


# --------------------------------------------------------------------------- #
# Sensitivity analysis
# --------------------------------------------------------------------------- #

def sensitivity_grid(a: Assumptions, sector: str = "general",
                     wacc_deltas: Tuple[float, ...] = (-0.015, -0.01, -0.005, 0, 0.005, 0.01, 0.015),
                     g_deltas: Tuple[float, ...] = (-0.01, -0.005, 0, 0.005, 0.01),
                     method: str = "gordon") -> dict:
    """
    Implied share price over a WACC x terminal-growth grid.

    Standard data-table semantics: the *explicit* FCFF forecast is held fixed and
    only the discounting (WACC) and the terminal-value growth vary. This isolates
    the discount/terminal effect and matches the live Excel sensitivity table
    exactly (build_excel.py). Returns axis labels and a matrix of implied prices.
    """
    base = run_dcf(a, sector=sector)
    base_wacc = base.wacc.wacc
    fcff = [r.fcff for r in base.forecast]            # fixed explicit stream
    ebitda_n = base.forecast[-1].ebit + base.forecast[-1].da
    n = a.forecast_years
    waccs = [base_wacc + d for d in wacc_deltas]
    gs = [a.terminal_growth + d for d in g_deltas]
    matrix: List[List[float]] = []
    for w in waccs:
        row = []
        for g in gs:
            row.append(_price_from_stream(fcff, w, g, a.net_debt, a.shares, n,
                                          ebitda_n, a.exit_ev_ebitda, method))
        matrix.append(row)
    return {"waccs": waccs, "growths": gs, "matrix": matrix,
            "base_wacc": base_wacc, "base_growth": a.terminal_growth}


def _price_from_stream(fcff: List[float], wacc: float, g: float, net_debt: float,
                       shares: Optional[float], n: int, ebitda_n: float,
                       exit_mult: float, method: str = "gordon") -> float:
    """Implied share price from a fixed FCFF stream (matches the Excel data table)."""
    if not shares:
        return float("nan")
    pv = sum(f / (1.0 + wacc) ** (t + 1) for t, f in enumerate(fcff))
    df_n = 1.0 / (1.0 + wacc) ** n
    if method == "gordon":
        if wacc <= g:
            return float("nan")
        tv = fcff[-1] * (1.0 + g) / (wacc - g)
    else:
        tv = ebitda_n * exit_mult
    ev = pv + tv * df_n
    return (ev - net_debt) / shares


def tornado(a: Assumptions, sector: str = "general") -> List[dict]:
    """
    One-at-a-time sensitivity of the implied (Gordon) price to key assumptions.
    Returns a list sorted by swing magnitude (largest mover first).
    """
    base = run_dcf(a, sector=sector).price_gordon
    # (label, attribute, low, high)
    specs = [
        ("Revenue growth (yr 1)", "initial_growth", a.initial_growth - 0.02, a.initial_growth + 0.02),
        ("EBIT margin", "target_ebit_margin", a.target_ebit_margin - 0.03, a.target_ebit_margin + 0.03),
        ("Terminal growth", "terminal_growth", a.terminal_growth - 0.005, a.terminal_growth + 0.005),
        ("Beta (-> WACC)", "beta", a.beta - 0.2, a.beta + 0.2),
        ("Equity risk premium", "equity_risk_premium", a.equity_risk_premium - 0.01, a.equity_risk_premium + 0.01),
        ("Capex % revenue", "capex_pct", a.capex_pct + 0.01, a.capex_pct - 0.01),
    ]
    # NOTE: the exit EV/EBITDA multiple is intentionally excluded — it drives the
    # exit-multiple valuation, not the Gordon price this tornado measures.
    out = []
    for label, attr, lo, hi in specs:
        p_lo = _price_with(a, attr, lo, sector)
        p_hi = _price_with(a, attr, hi, sector)
        out.append({"label": label, "attr": attr, "low_input": lo, "high_input": hi,
                    "price_low": p_lo, "price_high": p_hi, "base": base,
                    "swing": abs(p_hi - p_lo)})
    out.sort(key=lambda d: (d["swing"] if d["swing"] == d["swing"] else -1), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #

def _price_with(a: Assumptions, attr: str, value: float, sector: str) -> float:
    a2 = Assumptions(**a.to_dict())
    setattr(a2, attr, value)
    return run_dcf(a2, sector=sector).price_gordon


def _beta_for_wacc(a: Assumptions, target_wacc: float) -> float:
    """Invert compute_wacc for beta so the resulting WACC equals target_wacc."""
    after_tax_kd = a.pretax_cost_of_debt * (1.0 - a.tax_rate)
    e = a.market_cap or 0.0
    d = max(a.total_debt, 0.0)
    v = e + d
    we = 1.0 if v <= 0 else e / v
    wd = 0.0 if v <= 0 else d / v
    if we <= 0:
        return a.beta
    # target = we*(rf + beta*ERP) + wd*after_tax_kd
    cost_equity = (target_wacc - wd * after_tax_kd) / we
    return (cost_equity - a.risk_free_rate) / a.equity_risk_premium


def _implied_growth(tv: float, fcff: float, wacc: float) -> Optional[float]:
    # TV = fcff*(1+g)/(wacc-g)  ->  g = (TV*wacc - fcff) / (TV + fcff)
    denom = tv + fcff
    if denom == 0:
        return None
    g = (tv * wacc - fcff) / denom
    return g


def _bound(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None or x != x else f"{x*100:.1f}%"


def _money(x: Optional[float]) -> str:
    if x is None or x != x:
        return "n/a"
    a = abs(x) / 1e6
    s = f"${a:,.0f}M" if a < 1e6 else f"${a/1e3:,.1f}B"
    return f"({s})" if x < 0 else s


# --------------------------------------------------------------------------- #
# CLI / pretty report
# --------------------------------------------------------------------------- #

def print_report(res: ValuationResult) -> None:
    a = res.assumptions
    bar = "=" * 80
    print(bar)
    print(f"DCF VALUATION — {res.entity_name} ({res.ticker})   [{res.sector}]")
    print(bar)
    px = f"${a.current_price:,.2f}" if a.current_price else "n/a"
    mc = _money(a.market_cap)
    sh = f"{a.shares/1e6:,.0f}M" if a.shares else "n/a"
    print(f"Current price: {px}    Market cap: {mc}    Shares: {sh}")
    print()

    print("KEY ASSUMPTIONS")
    print(f"  Forecast horizon        {a.forecast_years} yrs")
    print(f"  Revenue growth (yr1)    {_pct(a.initial_growth)}  -> terminal {_pct(a.terminal_growth)} "
          f"({'faded' if a.fade_growth else 'flat'})")
    print(f"  EBIT margin             {_pct(a.start_ebit_margin)} -> {_pct(a.target_ebit_margin)} "
          f"({'ramped' if a.ramp_margin else 'flat'})")
    print(f"  Effective tax rate      {_pct(a.tax_rate)}")
    print(f"  D&A / Capex / NWC %rev  {_pct(a.da_pct)} / {_pct(a.capex_pct)} / {_pct(a.nwc_pct)}")
    print(f"  Exit EV/EBITDA          {a.exit_ev_ebitda:.1f}x")
    print()

    w = res.wacc
    print("WACC (CAPM)")
    print(f"  Cost of equity          {_pct(w.cost_of_equity)}  = rf {_pct(a.risk_free_rate)} "
          f"+ beta {a.beta:.2f} x ERP {_pct(a.equity_risk_premium)}")
    print(f"  After-tax cost of debt  {_pct(w.after_tax_cost_of_debt)}  "
          f"(pre-tax {_pct(a.pretax_cost_of_debt)})")
    print(f"  Weights E / D           {_pct(w.weight_equity)} / {_pct(w.weight_debt)}")
    print(f"  WACC                    {_pct(w.wacc)}")
    print()

    print("FORECAST (unlevered FCFF, $M)")
    hdr = f"  {'Yr':>3} {'Growth':>7} {'Revenue':>11} {'EBIT':>10} {'NOPAT':>10} " \
          f"{'D&A':>8} {'Capex':>8} {'dNWC':>8} {'FCFF':>10} {'PV':>10}"
    print(hdr)
    for r in res.forecast:
        print(f"  {r.year:>3} {_pct(r.growth):>7} {r.revenue/1e6:>11,.0f} {r.ebit/1e6:>10,.0f} "
              f"{r.nopat/1e6:>10,.0f} {r.da/1e6:>8,.0f} {r.capex/1e6:>8,.0f} "
              f"{r.d_nwc/1e6:>8,.0f} {r.fcff/1e6:>10,.0f} {r.pv_fcff/1e6:>10,.0f}")
    print(f"  {'Sum PV of FCFF:':>52} {res.pv_fcff_sum/1e6:>10,.0f}")
    print()

    print("VALUATION BRIDGE")
    print(f"  {'':22}{'Gordon growth':>18}{'Exit multiple':>18}")
    print(f"  PV of explicit FCFF   {res.pv_fcff_sum/1e6:>18,.0f}{res.pv_fcff_sum/1e6:>18,.0f}")
    print(f"  PV of terminal value  {res.pv_tv_gordon/1e6:>18,.0f}{res.pv_tv_exit/1e6:>18,.0f}")
    print(f"  Enterprise value      {res.ev_gordon/1e6:>18,.0f}{res.ev_exit/1e6:>18,.0f}")
    print(f"  Less: net debt        {a.net_debt/1e6:>18,.0f}{a.net_debt/1e6:>18,.0f}")
    print(f"  Equity value          {res.equity_gordon/1e6:>18,.0f}{res.equity_exit/1e6:>18,.0f}")
    print(f"  Implied share price   {res.price_gordon:>18,.2f}{res.price_exit:>18,.2f}")
    if a.current_price:
        print(f"  Upside vs. ${a.current_price:,.2f}     {_pct(res.upside_gordon):>18}{_pct(res.upside_exit):>18}")
    print()
    print("  Cross-checks:")
    print(f"    Terminal value = {_pct(res.tv_pct_of_ev_gordon)} of EV (Gordon)")
    print(f"    Gordon TV implies exit multiple of {res.implied_exit_multiple_from_gordon:,.1f}x EV/EBITDA")
    print(f"    Exit multiple implies perpetuity growth of {_pct(res.implied_growth_from_exit)}")
    if res.current_ev_ebitda:
        print(f"    Stock currently trades at ~{res.current_ev_ebitda:,.1f}x EV/EBITDA")
    print()

    if res.warnings:
        print("NOTES / WARNINGS")
        for wmsg in res.warnings:
            print(f"  ! {wmsg}")
        print()


def _print_sensitivity(grid: dict) -> None:
    print("SENSITIVITY — implied price (Gordon), WACC (rows) x terminal growth (cols)")
    header = "   WACC\\g  " + "".join(f"{g*100:>8.1f}%" for g in grid["growths"])
    print(header)
    for w, row in zip(grid["waccs"], grid["matrix"]):
        cells = "".join(f"{p:>9,.0f}" for p in row)
        marker = " <-base" if abs(w - grid["base_wacc"]) < 1e-9 else ""
        print(f"  {w*100:>6.2f}%  {cells}{marker}")
    print()


def _print_tornado(rows: List[dict]) -> None:
    print("TORNADO — swing in implied price (Gordon) from moving one assumption")
    for r in rows:
        print(f"  {r['label']:<24} {r['price_low']:>8,.0f}  ...  {r['price_high']:>8,.0f}   "
              f"(swing ${r['swing']:,.0f})")
    print()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Transparent DCF valuation for a US ticker.")
    p.add_argument("ticker")
    p.add_argument("--years", type=int, default=5)
    p.add_argument("--no-market", action="store_true",
                   help="Skip live market fetch (use assumption defaults).")
    args = p.parse_args(argv)

    fin = pull_financials(args.ticker)
    market = MarketData(ticker=args.ticker.upper()) if args.no_market \
        else fetch_market_data(args.ticker)
    a = derive_assumptions(fin, market, forecast_years=args.years)
    res = run_dcf(a, ticker=fin.ticker, entity_name=fin.entity_name, sector=fin.sector)

    print_report(res)
    _print_sensitivity(sensitivity_grid(a, sector=fin.sector))
    _print_tornado(tornado(a, sector=fin.sector))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
sec_pull.py — Standardized annual financials for any US-listed ticker.

Pulls from the SEC EDGAR XBRL `companyfacts` API and maps the messy, per-company
XBRL tag universe onto a small set of *canonical* line items that a DCF model can
consume (Revenue, COGS, Operating Income, D&A, Capex, Debt, ... ).

Design notes
------------
* Different filers tag the same economic concept differently (e.g. revenue can be
  `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, or
  `SalesRevenueNet`). For each canonical item we try a *priority list* of candidate
  tags and record which one actually supplied the data (`tags_used`).
* Some concepts genuinely do not exist for a sector — banks have no COGS or
  inventory, REITs have no COGS. We classify the filer by SIC code and mark those
  items `not_applicable` instead of letting them fail silently as `[NOT FOUND]`.
* Annual figures are extracted from 10-K filings only. Duration concepts (income
  statement / cash flow) are filtered to full-year (~365-day) periods; instant
  concepts (balance sheet) are taken at each fiscal-year-end. Where a year has been
  restated in a later filing, the most-recently-filed value wins.

The SEC requires a descriptive User-Agent with a real contact. Set it via the
SEC_UA_EMAIL environment variable (recommended) or edit UA_EMAIL below.

Usage
-----
    python sec_pull.py AAPL                # pretty table for one ticker
    python sec_pull.py AAPL JPM O          # several tickers
    python sec_pull.py AAPL --json         # also write data/AAPL_financials.json

Programmatic
------------
    from sec_pull import pull_financials
    fin = pull_financials("AAPL")
    fin.series("Revenue")                  # {2020: ..., 2021: ..., ...}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Dict, List, Optional

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _load_local_settings() -> None:
    """
    Load KEY=VALUE pairs from settings.local.env (or .env) beside this file.

    Keeps a personal contact address out of the source while still letting the
    tool run with no manual setup. Real environment variables always win, and a
    missing file is not an error.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("settings.local.env", ".env"):
        path = os.path.join(here, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except OSError:
            pass          # unreadable settings file is not fatal
        break


_load_local_settings()

# The SEC asks every automated caller to identify itself with a real contact
# string and rejects placeholder ones. It is read from the environment (or
# settings.local.env above) rather than hard-coded, so a personal address never
# ends up inside the source:
#
#     PowerShell:  $env:SEC_UA_EMAIL = "you@example.com"
#     bash/zsh:    export SEC_UA_EMAIL="you@example.com"
UA_EMAIL = os.environ.get("SEC_UA_EMAIL", "").strip() or "contact-not-set@example.com"
UA_IS_PLACEHOLDER = "example.com" in UA_EMAIL


class SECContactError(RuntimeError):
    """Raised when the SEC rejects us for not supplying a real contact string."""

    MESSAGE = (
        "The SEC refused the request (HTTP 403).\n\n"
        "This almost always means SEC_UA_EMAIL is not set. The SEC requires "
        "automated callers to identify themselves with a real contact address "
        "and blocks placeholder ones.\n\n"
        "  PowerShell:  $env:SEC_UA_EMAIL = \"you@example.com\"\n"
        "  bash/zsh:    export SEC_UA_EMAIL=\"you@example.com\"\n\n"
        "Then run the command again."
    )

    def __init__(self, url: str = ""):
        super().__init__(self.MESSAGE + (f"\n\n(url: {url})" if url else ""))

HEADERS = {
    "User-Agent": f"dynamic-dcf-model/1.0 ({UA_EMAIL})",
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov",
}

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# How many trailing fiscal years to keep in the standardized output.
MAX_YEARS = 10

# Full-year duration window (days). 53-week fiscal years run to ~371 days.
_MIN_DAYS, _MAX_DAYS = 340, 380


# --------------------------------------------------------------------------- #
# Canonical line-item definitions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Item:
    name: str                    # canonical name used everywhere downstream
    unit: str                    # XBRL unit key: "USD" or "shares"
    instant: bool                # True = balance-sheet (instant), False = flow
    tags: List[str]              # candidate us-gaap tags, in priority order
    taxonomy: str = "us-gaap"


# Base tag map. Sector-specific revenue tags are layered on in pull_financials().
ITEMS: List[Item] = [
    Item("Revenue", "USD", False, [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ]),
    Item("COGS", "USD", False, [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
        "CostOfServices",
    ]),
    Item("OperatingIncome", "USD", False, ["OperatingIncomeLoss"]),
    Item("PretaxIncome", "USD", False, [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ]),
    Item("IncomeTaxExpense", "USD", False, ["IncomeTaxExpenseBenefit"]),
    Item("NetIncome", "USD", False, ["NetIncomeLoss", "ProfitLoss"]),
    Item("InterestExpense", "USD", False, [
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestAndDebtExpense",
    ]),
    Item("DA", "USD", False, [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ]),
    Item("Capex", "USD", False, [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireRealEstateHeldForInvestment",
    ]),
    Item("CFO", "USD", False, [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ]),
    Item("Cash", "USD", True, [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
    ]),
    Item("ShortTermInvestments", "USD", True, [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesCurrent",
    ]),
    Item("TotalAssets", "USD", True, ["Assets"]),
    Item("TotalEquity", "USD", True, [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ]),
    Item("Inventory", "USD", True, ["InventoryNet"]),
    Item("AccountsReceivable", "USD", True, [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
    ]),
    Item("AccountsPayable", "USD", True, [
        "AccountsPayableCurrent",
        "AccountsPayableAndAccruedLiabilitiesCurrent",
    ]),
    # --- debt components (combined into TotalDebt below) ---
    Item("DebtCombined", "USD", True, ["DebtLongtermAndShorttermCombinedAmount"]),
    Item("LongTermDebtNoncurrent", "USD", True, ["LongTermDebtNoncurrent"]),
    Item("LongTermDebtCurrent", "USD", True, ["LongTermDebtCurrent"]),
    Item("LongTermDebtCombined", "USD", True, [
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
    ]),
    Item("ShortTermBorrowings", "USD", True, [
        "ShortTermBorrowings",
        "CommercialPaper",
        "DebtCurrent",
    ]),
    # --- instrument-level debt tags, used as a fallback for filers (esp. REITs)
    #     that never report the LongTermDebt* aggregates above ---
    Item("NotesPayable", "USD", True, ["NotesPayable", "NotesPayableNet"]),
    Item("LoansPayable", "USD", True, ["LoansPayable"]),
    Item("SecuredDebt", "USD", True, ["SecuredDebt", "SecuredLongTermDebt"]),
    Item("UnsecuredDebt", "USD", True, ["UnsecuredDebt", "UnsecuredLongTermDebt"]),
    Item("SeniorNotes", "USD", True, ["SeniorNotes", "SeniorNotesNet"]),
    Item("LineOfCredit", "USD", True, [
        "LineOfCreditFacilityAmountOutstanding",
        "LongTermLineOfCredit",
    ]),
]

# Canonical items that carry over into the model (raw debt components are hidden
# behind the derived TotalDebt / NetDebt figures).
DISPLAY_ORDER = [
    "Revenue", "COGS", "GrossProfit", "OperatingIncome", "PretaxIncome",
    "IncomeTaxExpense", "NetIncome", "InterestExpense", "DA", "Capex", "CFO",
    "Cash", "ShortTermInvestments", "AccountsReceivable", "Inventory",
    "AccountsPayable", "TotalDebt", "NetDebt", "TotalAssets", "TotalEquity",
]

# Items that are genuinely not applicable to a sector (flagged, not [NOT FOUND]).
# A "not_applicable" label is only applied when the item also has no data — real
# data always wins over the label (see status logic in pull_financials).
SECTOR_NA: Dict[str, set] = {
    "bank":        {"COGS", "GrossProfit", "Inventory", "Capex", "OperatingIncome",
                    "ShortTermInvestments", "AccountsReceivable", "AccountsPayable",
                    "TotalDebt", "NetDebt"},
    "insurance":   {"COGS", "GrossProfit", "Inventory", "Capex", "OperatingIncome",
                    "ShortTermInvestments", "AccountsReceivable", "AccountsPayable",
                    "TotalDebt", "NetDebt"},
    "reit":        {"COGS", "GrossProfit", "Inventory", "OperatingIncome",
                    "ShortTermInvestments", "AccountsReceivable", "AccountsPayable"},
    "real_estate": {"COGS", "GrossProfit", "Inventory", "OperatingIncome",
                    "ShortTermInvestments", "AccountsReceivable", "AccountsPayable"},
    "general":     set(),
}

# Sector-specific revenue tags, prepended to the general list.
SECTOR_REVENUE_TAGS: Dict[str, List[str]] = {
    "bank": ["Revenues", "RevenuesNetOfInterestExpense", "InterestAndDividendIncomeOperating"],
    "insurance": ["Revenues", "PremiumsEarnedNet"],
    "reit": ["Revenues", "OperatingLeasesIncomeStatementLeaseRevenue", "RealEstateRevenueNet"],
    "real_estate": ["Revenues", "OperatingLeasesIncomeStatementLeaseRevenue"],
}


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #

@dataclass
class Financials:
    ticker: str
    cik: str
    entity_name: str
    sic: str
    sic_description: str
    sector: str
    currency: str
    fiscal_years: List[int]
    shares_outstanding: Optional[float]
    line_items: Dict[str, Dict[int, Optional[float]]] = field(default_factory=dict)
    item_status: Dict[str, str] = field(default_factory=dict)   # ok|not_found|not_applicable
    tags_used: Dict[str, str] = field(default_factory=dict)

    def series(self, name: str) -> Dict[int, Optional[float]]:
        """Return {year: value} for a canonical item (empty dict if missing)."""
        return self.line_items.get(name, {})

    def latest(self, name: str) -> Optional[float]:
        s = {y: v for y, v in self.series(name).items() if v is not None}
        return s[max(s)] if s else None

    def to_dict(self) -> dict:
        d = asdict(self)
        # JSON object keys must be strings; convert the int year keys.
        d["line_items"] = {
            k: {str(y): v for y, v in sub.items()} for k, sub in self.line_items.items()
        }
        return d


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def _get(url: str, host: Optional[str] = None) -> requests.Response:
    """GET with the required SEC headers, light retry, and rate-limit courtesy."""
    headers = dict(HEADERS)
    if host:
        headers["Host"] = host
    last_exc = None
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 403 and UA_IS_PLACEHOLDER:
                # Retrying will not help: the SEC is rejecting the contact string
                # itself. Fail immediately with an actionable message.
                raise SECContactError(url)
            if resp.status_code in (403, 429):
                time.sleep(1.0 + attempt)     # back off; SEC throttles aggressive callers
                continue
            if resp.status_code == 404:
                return resp                    # let caller handle "not found"
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(0.5 + attempt)
    if last_exc:
        raise last_exc
    return resp


_ticker_map_cache: Optional[dict] = None


def resolve_cik(ticker: str) -> str:
    """Map a ticker symbol to a zero-padded 10-digit CIK string."""
    global _ticker_map_cache
    if _ticker_map_cache is None:
        resp = _get(TICKERS_URL, host="www.sec.gov")
        resp.raise_for_status()
        _ticker_map_cache = resp.json()
    t = ticker.strip().upper()
    for row in _ticker_map_cache.values():
        if row["ticker"].upper() == t:
            return str(row["cik_str"]).zfill(10)
    raise ValueError(f"Ticker {ticker!r} not found in SEC ticker map.")


def classify_sector(sic: str) -> str:
    """Coarse sector bucket from SIC code, tuned for DCF applicability rules."""
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return "general"
    if code == 6798:
        return "reit"
    if 6000 <= code <= 6199:
        return "bank"
    if 6300 <= code <= 6411:
        return "insurance"
    if 6500 <= code <= 6799:
        return "real_estate"
    return "general"


# --------------------------------------------------------------------------- #
# Fact extraction
# --------------------------------------------------------------------------- #

def _extract_series(facts: dict, item: Item) -> (Dict[int, float], List[str]):
    """
    Return ({year: value}, [tags_used]) merged across all candidate tags.

    A single economic series often spans multiple tags over time (e.g. revenue
    under `SalesRevenueNet` pre-ASC606, then `RevenueFromContractWithCustomer...`).
    We therefore MERGE by year across the candidate list: higher-priority tags
    (earlier in the list) win for any overlapping year, and lower-priority tags
    backfill the years the higher ones don't cover.

    Annual values only: 10-K forms, full-year durations for flows, fiscal-year-end
    instants for balances. Latest-filed value wins per year (captures restatements).
    """
    node_root = facts.get(item.taxonomy, {})
    merged: Dict[int, tuple] = {}   # year -> (filed_date, value)
    used: List[str] = []
    for tag in item.tags:
        node = node_root.get(tag)
        if not node:
            continue
        recs = node.get("units", {}).get(item.unit)
        if not recs:
            continue
        per_year: Dict[int, tuple] = {}
        for r in recs:
            if not r.get("form", "").startswith("10-K"):
                continue
            end = r.get("end")
            val = r.get("val")
            if end is None or val is None:
                continue
            if not item.instant:
                start = r.get("start")
                if not start:
                    continue
                try:
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                except ValueError:
                    continue
                if not (_MIN_DAYS <= days <= _MAX_DAYS):
                    continue
            year = int(end[:4])
            filed = r.get("filed", "")
            prev = per_year.get(year)
            if prev is None or filed > prev[0]:
                per_year[year] = (filed, float(val))
        contributed = False
        for year, fv in per_year.items():
            if year not in merged:          # keep higher-priority tag's value
                merged[year] = fv
                contributed = True
        if contributed:
            used.append(tag)
    return {y: fv[1] for y, fv in merged.items()}, used


def _latest_shares(facts: dict) -> Optional[float]:
    """Most recent common-shares-outstanding figure (cover-page or balance sheet)."""
    candidates = [
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ]
    best_end, best_val = "", None
    for tax, tag in candidates:
        node = facts.get(tax, {}).get(tag)
        if not node:
            continue
        for recs in node.get("units", {}).values():   # unit is "shares"
            for r in recs:
                end = r.get("end", "")
                val = r.get("val")
                if val and end > best_end:
                    best_end, best_val = end, float(val)
        if best_val is not None:
            return best_val   # prefer the higher-priority source once it has data
    return best_val


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def pull_financials(ticker: str) -> Financials:
    cik = resolve_cik(ticker)

    sub = _get(SUBMISSIONS_URL.format(cik10=cik), host="data.sec.gov").json()
    sic = str(sub.get("sic", ""))
    sic_desc = sub.get("sicDescription", "") or ""
    entity_name = sub.get("name", ticker.upper())
    sector = classify_sector(sic)

    facts = _get(FACTS_URL.format(cik10=cik), host="data.sec.gov").json().get("facts", {})

    # Layer sector-specific revenue tags ahead of the generic ones.
    items = list(ITEMS)
    if sector in SECTOR_REVENUE_TAGS:
        base_rev = next(i for i in items if i.name == "Revenue")
        merged = SECTOR_REVENUE_TAGS[sector] + [t for t in base_rev.tags
                                                if t not in SECTOR_REVENUE_TAGS[sector]]
        items = [Item("Revenue", "USD", False, merged) if i.name == "Revenue" else i
                 for i in items]

    raw: Dict[str, Dict[int, float]] = {}
    tags_used: Dict[str, str] = {}
    for item in items:
        series, used = _extract_series(facts, item)
        raw[item.name] = series
        if used:
            tags_used[item.name] = " + ".join(used)

    # Determine the trailing set of fiscal years from the revenue/net-income series.
    year_pool = set(raw.get("Revenue", {})) | set(raw.get("NetIncome", {})) \
        | set(raw.get("TotalAssets", {}))
    years = sorted(year_pool)[-MAX_YEARS:]

    # -------- derived items -------------------------------------------------- #
    def _sub(a: str, b: str) -> Dict[int, float]:
        out = {}
        for y in years:
            va, vb = raw.get(a, {}).get(y), raw.get(b, {}).get(y)
            if va is not None and vb is not None:
                out[y] = va - vb
        return out

    gross_profit = _sub("Revenue", "COGS")

    # -------- total debt (sector-aware, layered strategy) -------------------- #
    # Strategy per fiscal year, first that applies wins:
    #   1. a single combined total-debt tag, if the filer reports one;
    #   2. long-term debt (noncurrent + current portion) + short-term borrowings
    #      — the standard industrial capital structure (AAPL etc.);
    #   3. a combined LongTermDebt tag + short-term borrowings;
    #   4. instrument-level fallback (notes/loans/secured/unsecured/senior/LOC)
    #      for filers — mainly REITs — that never use the LongTermDebt* tags.
    # Strategy 4 only fires when 1-3 find nothing, which avoids double-counting.
    _instr_tags = ["NotesPayable", "LoansPayable", "SecuredDebt",
                   "UnsecuredDebt", "SeniorNotes", "LineOfCredit"]
    is_property = sector in ("reit", "real_estate")
    total_debt: Dict[int, float] = {}
    debt_method: set = set()
    for y in years:
        comb = raw.get("DebtCombined", {}).get(y)
        ltn = raw.get("LongTermDebtNoncurrent", {}).get(y)
        ltc = raw.get("LongTermDebtCurrent", {}).get(y)
        ltcomb = raw.get("LongTermDebtCombined", {}).get(y)
        stb = raw.get("ShortTermBorrowings", {}).get(y)

        # aggregate strategy (clean; primary for industrials)
        agg = None
        if comb is not None:
            agg = comb
            debt_method.add("DebtLongtermAndShorttermCombinedAmount")
        elif ltn is not None:
            agg = ltn + (ltc or 0.0) + (stb or 0.0)
            debt_method.add("LongTermDebt(noncurrent+current)+short-term")
        elif ltcomb is not None:
            agg = ltcomb + (stb or 0.0)
            debt_method.add("LongTermDebt+short-term")

        # instrument-sum strategy — trusted only when the primary notes-payable
        # component is present, so we never report a misleading partial (e.g. a
        # lone term loan) as if it were total debt.
        instr_sum = None
        notes = raw.get("NotesPayable", {}).get(y)
        instr_vals = [raw.get(t, {}).get(y) for t in _instr_tags]
        instr_present = [v for v in instr_vals if v is not None]
        if instr_present and notes is not None:
            instr_sum = sum(instr_present)
            debt_method.add("sum(notes/loans/secured/unsecured/senior/LOC)")

        # REITs list debt as separate instruments and often tag the aggregate
        # only partially, so prefer the larger of the two estimates; industrials
        # use the clean aggregate and fall back to instruments only if needed.
        if is_property:
            cands = [v for v in (agg, instr_sum) if v is not None]
            total_debt[y] = max(cands) if cands else None
        else:
            total_debt[y] = agg if agg is not None else instr_sum
        if total_debt[y] is None:
            del total_debt[y]

    net_debt: Dict[int, float] = {}
    for y in years:
        td = total_debt.get(y)
        if td is None:
            continue
        cash = raw.get("Cash", {}).get(y) or 0.0
        sti = raw.get("ShortTermInvestments", {}).get(y) or 0.0
        net_debt[y] = td - cash - sti

    raw["GrossProfit"] = gross_profit
    raw["TotalDebt"] = total_debt
    raw["NetDebt"] = net_debt
    if total_debt:
        tags_used["TotalDebt"] = "derived: " + "; ".join(sorted(debt_method))
    if net_debt:
        tags_used["NetDebt"] = "derived: TotalDebt - Cash - ShortTermInvestments"
    if gross_profit:
        tags_used["GrossProfit"] = "derived: Revenue - COGS"

    # -------- assemble line items + status ---------------------------------- #
    na = SECTOR_NA.get(sector, set())
    line_items: Dict[str, Dict[int, Optional[float]]] = {}
    status: Dict[str, str] = {}
    for name in DISPLAY_ORDER:
        series = {y: raw.get(name, {}).get(y) for y in years}
        line_items[name] = series
        # Real data always wins over a not-applicable label.
        if any(v is not None for v in series.values()):
            status[name] = "ok"
        elif name in na:
            status[name] = "not_applicable"
        else:
            status[name] = "not_found"

    return Financials(
        ticker=ticker.upper(),
        cik=cik,
        entity_name=entity_name,
        sic=sic,
        sic_description=sic_desc,
        sector=sector,
        currency="USD",
        fiscal_years=years,
        shares_outstanding=_latest_shares(facts),
        line_items=line_items,
        item_status=status,
        tags_used=tags_used,
    )


# --------------------------------------------------------------------------- #
# Pretty-printing / CLI
# --------------------------------------------------------------------------- #

def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "        --"
    millions = v / 1e6
    if millions < 0:
        return f"({abs(millions):,.0f})".rjust(10)
    return f"{millions:,.0f}".rjust(10)


def print_financials(fin: Financials) -> None:
    bar = "=" * 78
    print(bar)
    print(f"{fin.entity_name}  ({fin.ticker})   CIK {fin.cik}")
    print(f"SIC {fin.sic} — {fin.sic_description}   |   sector bucket: {fin.sector.upper()}")
    so = f"{fin.shares_outstanding/1e6:,.1f}M" if fin.shares_outstanding else "n/a"
    print(f"Shares outstanding (latest): {so}")
    print(f"Values in USD millions. Fiscal years: {fin.fiscal_years}")
    print(bar)

    header = "Line item".ljust(22) + "".join(f"FY{y % 100:02d}".rjust(11) for y in fin.fiscal_years)
    print(header)
    print("-" * len(header))
    for name in DISPLAY_ORDER:
        st = fin.item_status.get(name, "not_found")
        row = name.ljust(22)
        if st == "not_applicable":
            row += "  [ n/a for this sector ]".ljust(len(header) - 22)
        elif st == "not_found":
            row += "  [ NOT FOUND ]".ljust(len(header) - 22)
        else:
            row += "".join(f"{_fmt(fin.line_items[name][y])}".rjust(11) for y in fin.fiscal_years)
        print(row)

    print("-" * len(header))
    print("Tags used:")
    for name in DISPLAY_ORDER:
        if name in fin.tags_used:
            print(f"    {name:<22} <- {fin.tags_used[name]}")
    # Surface any true gaps (not the expected sector N/A ones).
    gaps = [n for n in DISPLAY_ORDER if fin.item_status.get(n) == "not_found"]
    if gaps:
        print(f"\n  Unresolved (NOT FOUND): {', '.join(gaps)}")
    print(bar + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Standardized SEC annual financials.")
    parser.add_argument("tickers", nargs="+", help="One or more US-listed tickers.")
    parser.add_argument("--json", action="store_true",
                        help="Also write data/<TICKER>_financials.json")
    args = parser.parse_args(argv)

    if "example.com" in UA_EMAIL:
        print("WARNING: using a placeholder SEC contact. The SEC may throttle or "
              "block requests. Set SEC_UA_EMAIL to a real contact string.\n",
              file=sys.stderr)

    exit_code = 0
    for ticker in args.tickers:
        try:
            fin = pull_financials(ticker)
        except Exception as exc:   # noqa: BLE001 — CLI: report and continue
            print(f"[{ticker}] ERROR: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        print_financials(fin)
        if args.json:
            os.makedirs("data", exist_ok=True)
            path = os.path.join("data", f"{fin.ticker}_financials.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(fin.to_dict(), fh, indent=2)
            print(f"  -> wrote {path}\n")
        time.sleep(0.2)   # be polite to the SEC endpoint between tickers
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

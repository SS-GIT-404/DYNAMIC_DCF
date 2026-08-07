"""
market_data.py — Live market inputs for the WACC / price comparison.

The valuation engine needs three things the SEC does not provide: the current
share price, the market capitalisation, and an equity beta. We fetch these from
Yahoo Finance via `yfinance`, but every field is optional and overridable — the
DCF is designed to run on explicit, named assumptions, not to break when a
scraping endpoint rate-limits. If a live fetch fails, callers fall back to the
values the user supplies in `Assumptions`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MarketData:
    ticker: str
    price: Optional[float] = None            # current share price
    market_cap: Optional[float] = None       # price * shares outstanding
    shares: Optional[float] = None
    beta: Optional[float] = None             # equity beta vs. the market
    source: str = "unavailable"

    @property
    def ok(self) -> bool:
        return self.price is not None


def fetch_market_data(ticker: str) -> MarketData:
    """Best-effort live quote. Never raises — returns an empty MarketData on failure."""
    md = MarketData(ticker=ticker.upper())
    try:
        import yfinance as yf
    except ImportError:
        return md

    try:
        t = yf.Ticker(ticker)
        # fast_info is the most reliable path for price / market cap / shares.
        fi = getattr(t, "fast_info", None)
        if fi is not None:
            md.price = _num(getattr(fi, "last_price", None))
            md.market_cap = _num(getattr(fi, "market_cap", None))
            md.shares = _num(getattr(fi, "shares", None))
        # .info is richer but slower / more fragile; only used to fill gaps.
        info = {}
        try:
            info = t.info or {}
        except Exception:  # noqa: BLE001 — .info frequently throws; tolerate it
            info = {}
        md.price = md.price or _num(info.get("currentPrice") or info.get("regularMarketPrice"))
        md.market_cap = md.market_cap or _num(info.get("marketCap"))
        md.shares = md.shares or _num(info.get("sharesOutstanding"))
        md.beta = _num(info.get("beta"))
        if md.price is not None:
            md.source = "yfinance"
    except Exception:  # noqa: BLE001 — any network / parsing failure -> graceful empty
        pass
    return md


def _num(x) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    import sys
    for tk in sys.argv[1:] or ["AAPL"]:
        md = fetch_market_data(tk)
        print(f"{md.ticker}: price={md.price} market_cap={md.market_cap} "
              f"shares={md.shares} beta={md.beta} ({md.source})")

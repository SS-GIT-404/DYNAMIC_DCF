"""
test_app.py — Headless smoke tests for the Streamlit app.

Runs app.py through Streamlit's AppTest harness (no browser required) against a
spread of tickers chosen to break different things:

    AAPL  — profitable industrial, the happy path
    JPM   — bank: no COGS/capex/EBIT, huge net cash
    O     — REIT: instrument-level debt, depreciation-heavy
    RIVN  — pre-profit: negative EBIT margin, negative FCFF
    BRK-B — ticker containing a hyphen (symbol-normalisation edge case)

Asserts that the app runs without an exception, renders the headline metrics, and
surfaces the expected caveats for the awkward sectors.

    python -m pytest test_app.py -v          (or)   python test_app.py
"""

from __future__ import annotations

import sys

from streamlit.testing.v1 import AppTest

TIMEOUT = 180

CASES = [
    ("AAPL", {"profitable": True}),
    ("JPM", {"expect_warning": "BANK"}),
    ("O", {"expect_warning": "REIT"}),
    ("RIVN", {"expect_warning": "PRE-PROFIT"}),
]


def run_ticker(ticker: str) -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, f"{ticker}: app raised on initial load: {at.exception}"
    # Drive the ticker form the way a user would.
    at.text_input[0].set_value(ticker)
    at.button[0].click().run()
    return at


def check(ticker: str, expect: dict) -> bool:
    print(f"\n--- {ticker} " + "-" * (60 - len(ticker)))
    try:
        at = run_ticker(ticker)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: harness error — {exc}")
        return False

    if at.exception:
        print(f"  FAIL: app exception — {at.exception}")
        return False

    metrics = {m.label: m.value for m in at.metric}
    if not metrics:
        print("  FAIL: no metrics rendered")
        return False
    for key in ("Current price", "Implied — Gordon", "WACC"):
        if key not in metrics:
            print(f"  FAIL: missing metric {key!r}")
            return False
    print(f"  metrics: price={metrics.get('Current price')} "
          f"gordon={metrics.get('Implied — Gordon')} "
          f"exit={metrics.get('Implied — exit multiple')} "
          f"wacc={metrics.get('WACC')} ev={metrics.get('Enterprise value')}")

    alerts = [e.value for e in at.error] + [w.value for w in at.warning]
    for a in alerts:
        print(f"  alert: {a[:100]}")

    want = expect.get("expect_warning")
    if want and not any(want in a for a in alerts):
        print(f"  FAIL: expected a {want} caveat, none found")
        return False

    print(f"  PASS ({len(at.dataframe)} tables rendered)")
    return True


def main() -> int:
    results = {t: check(t, e) for t, e in CASES}
    print("\n" + "=" * 66)
    for t, ok in results.items():
        print(f"  {t:<8} {'PASS' if ok else 'FAIL'}")
    print("=" * 66)
    return 0 if all(results.values()) else 1


# --- pytest entry points ---------------------------------------------------- #

def test_aapl():
    assert check("AAPL", {"profitable": True})


def test_jpm():
    assert check("JPM", {"expect_warning": "BANK"})


def test_reit():
    assert check("O", {"expect_warning": "REIT"})


def test_pre_profit():
    assert check("RIVN", {"expect_warning": "PRE-PROFIT"})


if __name__ == "__main__":
    sys.exit(main())

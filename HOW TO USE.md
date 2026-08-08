# How to use the DCF Valuation Model

This tool runs **entirely on this computer**. Nothing is uploaded anywhere, and
no other device can reach it.

---

## Starting it

**Double-click `DCF Valuation Model` on your desktop.**

(Or double-click `Launch DCF Model.cmd` inside this folder — same thing.)

A black window opens, then your browser opens to the tool. The first launch
takes a couple of minutes while it sets itself up; after that it starts in a few
seconds.

**Leave the black window open while you use the tool. Closing it shuts the tool
down.**

---

## Using it

1. Type a ticker — `AAPL`, `MSFT`, `JPM`, `KO`, anything US-listed that files
   with the SEC.
2. Press **Run valuation**.
3. Read across the five boxes at the top: current price, the two implied prices,
   the discount rate, and enterprise value.

The five tabs underneath:

| Tab | What's in it |
|---|---|
| **Valuation** | The bridge from cash flows to a share price, plus the tornado chart |
| **Forecast** | Year-by-year projected free cash flow |
| **Sensitivity** | How the price changes across different discount rates and growth rates |
| **Historical (SEC)** | The raw filed financials the model is built on |
| **Assumptions & sources** | Every input, and exactly where it came from |

Every assumption is editable in the **left sidebar** — drag any slider and the
whole model recalculates instantly.

---

## Reading the warnings

Coloured banners are the tool telling you something important. They are not
errors.

**⚠ FABRICATED INPUTS** — an input could not be found in that company's filings,
so the model substituted a generic figure. Banks and REITs don't report
"operating income" the way an industrial company does, so the profit margin is a
guess. Set it yourself in the sidebar, or treat the output as illustrative.

**⚠ BANK / ⚠ REIT** — this valuation method genuinely does not suit that kind of
company. Banks fund themselves with deposits, and REITs have depreciation that
makes cash flow look far worse than it is. The number will be wrong. This is why
`JPM` shows an implausible result.

**⚠ PRE-PROFIT** — the company is currently lossmaking, so the whole valuation
depends on when you assume it becomes profitable. Try `RIVN` to see this.

Rule of thumb: **AAPL-style profitable industrials give trustworthy output.**
Everything else, read the banner first.

---

## Getting an Excel file

Open the **Assumptions & sources** tab → **Build Excel workbook** → **Download**.

The workbook contains live formulas, not frozen numbers. Change a yellow
assumption cell in Excel and the whole model recalculates, exactly like the web
version.

---

## Stopping it

Close the black window.

---

## If something goes wrong

**Browser doesn't open** — go to <http://localhost:8501> manually.

**"Python was not found"** — install Python from
<https://www.python.org/downloads/>, and tick **"Add python.exe to PATH"** during
installation. Then double-click the launcher again.

**"The SEC refused the request"** — open `settings.local.env` in Notepad and
check it contains a real email address. The SEC blocks anonymous automated
requests.

**A ticker won't load** — it must be US-listed and file with the SEC. Foreign
listings, ETFs, and index symbols won't work.

**Port already in use** — the tool is already running. Look for an existing black
window, or restart your computer.

---

## Privacy

- The server binds to `127.0.0.1`, the loopback address. Only this computer can
  connect — verified: the tool is unreachable from this machine's own network
  address.
- Usage statistics are disabled.
- Your email sits in `settings.local.env` and is not referenced in any source
  file. That file is excluded from version control.
- Outbound requests go only to the SEC (for filings) and Yahoo Finance (for share
  prices). Nothing about you is sent anywhere.

---

## What the model actually found

At Apple's own historical numbers, the model values it around **$113 a share**
against a market price near **$312**.

That gap is the interesting part. To justify today's price you'd need roughly
**29% annual revenue growth** — Apple's actual five-year average is **5.2%**. The
stock trades at about **32x** enterprise value to EBITDA where the fundamentals
support something closer to **11x**.

The honest reading is not "Apple is overpriced" but "a conventional DCF cannot
explain Apple's valuation" — the market is paying for something this method
doesn't capture. The tool's job is to show you the size of that gap instead of
hiding it.

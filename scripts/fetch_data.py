#!/usr/bin/env python3
"""Daily data job for Stock Journal.

Reads data/tickers.json and writes data/market.json with, per ticker:
  - price: last close, day change, 52-week range, 1-year weekly closes
  - fundamentals: annual + trailing-twelve-month figures from SEC EDGAR filings,
    balance sheet, valuation, and plain-language flags
  - news: recent headlines from Yahoo Finance and Google News RSS

Standard library only. Every source is optional: if one fails for a ticker,
the previous day's value is kept and the error is recorded.
"""
import gzip
import json
import os
import urllib.error
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKERS_FILE = os.path.join(ROOT, "data", "tickers.json")
OUT_FILE = os.path.join(ROOT, "data", "market.json")

# SEC asks every client to identify itself with a contact. Set the
# SEC_USER_AGENT repository variable to e.g. "Stock Journal you@example.com".
SEC_UA = os.environ.get("SEC_USER_AGENT") or "Stock Journal research max-stax@users.noreply.github.com"
WEB_UA = "Mozilla/5.0 (compatible; StockJournal/1.0)"


# ---------------------------------------------------------------- http

def get(url, ua=WEB_UA, timeout=20, tries=3, headers=None):
    last = None
    for i in range(tries):
        try:
            h = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate", "Accept": "*/*"}
            h.update(headers or {})
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            snippet = ""
            try:
                raw = e.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace")))[:160]
            except Exception:  # noqa: BLE001
                pass
            last = RuntimeError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}: {snippet}".strip())
            if e.code in (401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001
            last = RuntimeError(f"{type(e).__name__}: {e}")
        time.sleep(2 * (i + 1))
    raise last


def get_json(url, ua=WEB_UA):
    return json.loads(get(url, ua))


# ---------------------------------------------------------------- prices

def price_yahoo(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=5y&interval=1d"
    res = get_json(url)["chart"]["result"][0]
    meta = res["meta"]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    hist = [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), round(c, 4))
            for t, c in zip(ts, closes) if c is not None]
    return build_price(hist, meta.get("regularMarketPrice"), meta.get("currency", "USD"))


def price_stooq(sym):
    url = f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d"
    rows = get(url).decode().strip().splitlines()[1:]
    hist = []
    for line in rows[-1300:]:
        p = line.split(",")
        if len(p) >= 5 and p[4] not in ("", "N/D"):
            hist.append((p[0], float(p[4])))
    return build_price(hist, None, "USD")


def build_price(hist, last=None, currency="USD"):
    """hist: [(iso_date, close)] daily, up to ~5 years (split-adjusted)."""
    if not hist:
        raise ValueError("no price history")
    last = float(last) if last else hist[-1][1]
    prev = hist[-2][1] if len(hist) > 1 else last
    if hist[-1][1] != last:
        prev = hist[-1][1]
    year = hist[-252:]
    closes = [c for _, c in year]
    weekly = year[::5] + ([year[-1]] if (len(year) - 1) % 5 else [])
    monthly = {}
    for d, c in hist:
        monthly[d[:7]] = (d, c)  # last close of each month
    return {
        "last": round(last, 4),
        "prevClose": round(prev, 4),
        "change": round(last - prev, 4),
        "changePct": round((last / prev - 1) * 100, 2) if prev else None,
        "hi52": max(closes), "lo52": min(closes),
        "asOf": hist[-1][0],
        "currency": currency,
        "history": [[d, c] for d, c in weekly],
        "monthly": [[d, c] for d, c in monthly.values()],
    }


def fetch_price(sym):
    errors = []
    for fn in (price_yahoo, price_stooq):
        try:
            return fn(sym), None
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e}")
    return None, "; ".join(errors)


# ---------------------------------------------------------------- SEC fundamentals

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT"}
QUARTER_FORMS = {"10-Q", "10-Q/A", "6-K"}

CONCEPTS = {
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "RevenuesNetOfInterestExpense"),
        ("ifrs-full", "Revenue"),
    ],
    "grossProfit": [("us-gaap", "GrossProfit"), ("ifrs-full", "GrossProfit")],
    "costOfRevenue": [
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsAndServicesSold"),
        ("ifrs-full", "CostOfSales"),
    ],
    "operatingIncome": [
        ("us-gaap", "OperatingIncomeLoss"),
        ("ifrs-full", "ProfitLossFromOperatingActivities"),
    ],
    "netIncome": [
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
        ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
        ("ifrs-full", "ProfitLoss"),
    ],
    "operatingCashFlow": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ],
    "capex": [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipment"),
    ],
    "dilutedShares": [
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("ifrs-full", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
    ],
    "sbc": [("us-gaap", "ShareBasedCompensation"), ("us-gaap", "AllocatedShareBasedCompensationExpense")],
}

BALANCE = {
    "cash": [
        ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
        ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
        ("ifrs-full", "CashAndCashEquivalents"),
    ],
    "shortTermInvestments": [
        ("us-gaap", "ShortTermInvestments"),
        ("us-gaap", "MarketableSecuritiesCurrent"),
        ("us-gaap", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
        ("ifrs-full", "CurrentInvestments"),
    ],
    "longTermDebt": [
        ("us-gaap", "LongTermDebtNoncurrent"),
        ("us-gaap", "LongTermDebt"),
        ("us-gaap", "ConvertibleNotesPayableNoncurrent"),
        ("us-gaap", "SeniorNotesNoncurrent"),
        ("ifrs-full", "NoncurrentBorrowings"),
        ("ifrs-full", "Borrowings"),
    ],
    "currentDebt": [
        ("us-gaap", "LongTermDebtCurrent"),
        ("us-gaap", "DebtCurrent"),
        ("us-gaap", "ConvertibleNotesPayableCurrent"),
        ("ifrs-full", "CurrentBorrowings"),
    ],
    "equity": [
        ("us-gaap", "StockholdersEquity"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        ("ifrs-full", "EquityAttributableToOwnersOfParent"),
        ("ifrs-full", "Equity"),
    ],
    "currentAssets": [("us-gaap", "AssetsCurrent"), ("ifrs-full", "CurrentAssets")],
    "currentLiabilities": [("us-gaap", "LiabilitiesCurrent"), ("ifrs-full", "CurrentLiabilities")],
    "totalAssets": [("us-gaap", "Assets"), ("ifrs-full", "Assets")],
}

SHARES_OUT = [("dei", "EntityCommonStockSharesOutstanding")]


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _unit_facts(facts, tax, concept, unit_pref):
    node = facts.get(tax, {}).get(concept)
    if not node:
        return None, None
    units = node.get("units", {})
    for u in unit_pref:
        if u in units:
            return units[u], u
    # Fall back to any currency-looking unit (IFRS filers in other currencies)
    for u, v in units.items():
        if re.fullmatch(r"[A-Z]{3}", u):
            return v, u
    return None, None


def duration_facts(facts, candidates, unit_pref=("USD",)):
    """All duration facts across candidate concepts (earlier concept wins on ties)."""
    out, unit_used = {}, None
    for tax, concept in candidates:
        rows, unit = _unit_facts(facts, tax, concept, unit_pref)
        if not rows:
            continue
        for r in rows:
            if "start" not in r or "end" not in r:
                continue
            key = (r["start"], r["end"])
            prev = out.get(key)
            if prev is None or (prev["_c"] == concept and r.get("filed", "") > prev.get("filed", "")):
                out[key] = {**r, "_c": concept, "_u": unit}
        unit_used = unit_used or unit
    return list(out.values()), unit_used


def instant_facts(facts, candidates, unit_pref=("USD",)):
    out = {}
    for tax, concept in candidates:
        rows, unit = _unit_facts(facts, tax, concept, unit_pref)
        if not rows:
            continue
        for r in rows:
            if "start" in r or "end" not in r:
                continue
            prev = out.get(r["end"])
            if prev is None or (prev["_c"] == concept and r.get("filed", "") > prev.get("filed", "")):
                out[r["end"]] = {**r, "_c": concept, "_u": unit}
    return out


def annual(rows):
    """end -> value for ~12-month periods reported in annual forms."""
    res = {}
    for r in rows:
        if r.get("form") not in ANNUAL_FORMS:
            continue
        d = _days(r["start"], r["end"])
        if 330 <= d <= 400:
            if r["end"] not in res or r.get("filed", "") > res[r["end"]][1]:
                res[r["end"]] = (r["val"], r.get("filed", ""))
    return {k: v[0] for k, v in sorted(res.items())}


def ttm(rows, last_fy_end, fy_val):
    """Trailing twelve months = last FY + current YTD - prior-year YTD."""
    if last_fy_end is None or fy_val is None:
        return None, None, None
    ytd = [r for r in rows if r["end"] > last_fy_end and 60 <= _days(r["start"], r["end"]) <= 300
           and r.get("form") in QUARTER_FORMS | ANNUAL_FORMS]
    if not ytd:
        return fy_val, last_fy_end, None
    latest_end = max(r["end"] for r in ytd)
    cur = max((r for r in ytd if r["end"] == latest_end), key=lambda r: _days(r["start"], r["end"]))
    span = _days(cur["start"], cur["end"])
    prior = [r for r in rows if 355 <= _days(r["end"], cur["end"]) <= 375
             and abs(_days(r["start"], r["end"]) - span) <= 10]
    if not prior:
        return fy_val, last_fy_end, None
    prev = max(prior, key=lambda r: r.get("filed", ""))
    return fy_val + cur["val"] - prev["val"], cur["end"], pct(cur["val"], prev["val"])


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a / b - 1) * 100, 1) if b > 0 else None


def ratio(a, b):
    if a is None or b in (None, 0):
        return None
    return round(a / b * 100, 1)


def fundamentals(facts, price):
    out = {"fy": [], "ttm": {}, "balance": {}, "valuation": {}, "flags": [], "strengths": []}
    series, ttm_vals, currency = {}, {}, None
    rev_rows, rev_unit = duration_facts(facts, CONCEPTS["revenue"])
    currency = rev_unit or "USD"
    unit_pref = (currency,)
    rows_by = {}
    for k, cands in CONCEPTS.items():
        rows, _ = duration_facts(facts, cands, ("shares",) if k == "dilutedShares" else unit_pref)
        rows_by[k] = rows
        series[k] = annual(rows)

    fy_ends = sorted(series["revenue"] or series["netIncome"])[-5:]
    if not fy_ends:
        return None
    out["currency"] = currency
    for end in fy_ends:
        g = lambda k: series[k].get(end)  # noqa: E731
        rev = g("revenue")
        gp = g("grossProfit")
        if gp is None and rev is not None and g("costOfRevenue") is not None:
            gp = rev - g("costOfRevenue")
        ocf, capex = g("operatingCashFlow"), g("capex")
        fcf = ocf - abs(capex) if ocf is not None and capex is not None else None
        out["fy"].append({
            "end": end, "revenue": rev, "grossProfit": gp, "operatingIncome": g("operatingIncome"),
            "netIncome": g("netIncome"), "operatingCashFlow": ocf, "capex": capex, "fcf": fcf,
            "dilutedShares": g("dilutedShares"), "sbc": g("sbc"),
        })
    prev = None
    for row in out["fy"]:
        row["revenueGrowth"] = pct(row["revenue"], prev["revenue"]) if prev else None
        row["shareChange"] = pct(row["dilutedShares"], prev["dilutedShares"]) if prev else None
        row["grossMargin"] = ratio(row["grossProfit"], row["revenue"])
        row["operatingMargin"] = ratio(row["operatingIncome"], row["revenue"])
        row["netMargin"] = ratio(row["netIncome"], row["revenue"])
        row["fcfMargin"] = ratio(row["fcf"], row["revenue"])
        prev = row

    last_end = fy_ends[-1]
    t_end, ytd_growth = None, None
    for k in ("revenue", "grossProfit", "operatingIncome", "netIncome", "operatingCashFlow", "capex", "sbc"):
        v, e, gr = ttm(rows_by[k], last_end, series[k].get(last_end))
        ttm_vals[k] = v
        if k == "revenue":
            t_end, ytd_growth = e, gr
    if ttm_vals.get("operatingCashFlow") is not None and ttm_vals.get("capex") is not None:
        ttm_vals["fcf"] = ttm_vals["operatingCashFlow"] - abs(ttm_vals["capex"])
    else:
        ttm_vals["fcf"] = None
    # Growth: latest year-to-date vs same period last year; else last fiscal year
    growth = ytd_growth if ytd_growth is not None else out["fy"][-1].get("revenueGrowth")
    out["ttm"] = {**ttm_vals, "asOf": t_end,
                  "revenueGrowth": growth,
                  "grossMargin": ratio(ttm_vals.get("grossProfit"), ttm_vals.get("revenue")),
                  "operatingMargin": ratio(ttm_vals.get("operatingIncome"), ttm_vals.get("revenue")),
                  "fcfMargin": ratio(ttm_vals.get("fcf"), ttm_vals.get("revenue"))}

    # Balance sheet: most recent instant date that has cash or equity
    inst = {k: instant_facts(facts, c, unit_pref) for k, c in BALANCE.items()}
    dates = sorted(set(inst["cash"]) | set(inst["equity"]))
    if dates:
        d = dates[-1]
        b = {k: (inst[k].get(d) or {}).get("val") for k in BALANCE}
        b["asOf"] = d
        debt = (b.get("longTermDebt") or 0) + (b.get("currentDebt") or 0)
        cashlike = (b.get("cash") or 0) + (b.get("shortTermInvestments") or 0)
        b["totalDebt"], b["cashAndInvestments"] = debt, cashlike
        b["netCash"] = cashlike - debt
        b["currentRatio"] = round(b["currentAssets"] / b["currentLiabilities"], 2) \
            if b.get("currentAssets") and b.get("currentLiabilities") else None
        b["debtToEquity"] = round(debt / b["equity"], 2) if b.get("equity") and b["equity"] > 0 else None
        out["balance"] = b
        # debt a year earlier, for trend
        year_ago = [x for x in dates if 330 <= _days(x, d) <= 400]
        if year_ago:
            y = year_ago[-1]
            out["balance"]["totalDebtYearAgo"] = ((inst["longTermDebt"].get(y) or {}).get("val") or 0) + \
                ((inst["currentDebt"].get(y) or {}).get("val") or 0)

    # Shares outstanding and valuation
    so = instant_facts(facts, SHARES_OUT, ("shares",))
    shares = so[max(so)]["val"] if so else (out["fy"][-1].get("dilutedShares"))
    v = out["valuation"]
    v["sharesOutstanding"] = shares
    if price and shares and currency == price.get("currency", "USD"):
        mc = price["last"] * shares
        v["marketCap"] = mc
        if ttm_vals.get("revenue"):
            v["priceToSales"] = round(mc / ttm_vals["revenue"], 1)
            if out["balance"]:
                v["evToSales"] = round((mc - out["balance"]["netCash"]) / ttm_vals["revenue"], 1)
        if ttm_vals.get("fcf") and ttm_vals["fcf"] > 0:
            v["priceToFcf"] = round(mc / ttm_vals["fcf"], 1)
        if ttm_vals.get("netIncome") and ttm_vals["netIncome"] > 0:
            v["pe"] = round(mc / ttm_vals["netIncome"], 1)

    # Balance sheet at each fiscal year end (for the yearly grade)
    for row in out["fy"]:
        bal = {}
        for k in ("cash", "shortTermInvestments", "longTermDebt", "currentDebt", "equity",
                  "currentAssets", "currentLiabilities"):
            near = [d for d in inst[k] if abs(_days(d, row["end"])) <= 10]
            bal[k] = inst[k][near[0]]["val"] if near else None
        if bal["cash"] is not None or bal["longTermDebt"] is not None:
            debt = (bal["longTermDebt"] or 0) + (bal["currentDebt"] or 0)
            row["netCash"] = (bal["cash"] or 0) + (bal["shortTermInvestments"] or 0) - debt
        else:
            row["netCash"] = None
        row["currentRatio"] = round(bal["currentAssets"] / bal["currentLiabilities"], 2) \
            if bal["currentAssets"] and bal["currentLiabilities"] else None
        row["grade"] = grade(row)

    latest = {
        "revenueGrowth": out["ttm"].get("revenueGrowth"),
        "operatingMargin": out["ttm"].get("operatingMargin"),
        "fcfMargin": out["ttm"].get("fcfMargin"),
        "fcf": out["ttm"].get("fcf"),
        "revenue": out["ttm"].get("revenue"),
        "netCash": out["balance"].get("netCash") if out["balance"] else None,
        "currentRatio": out["balance"].get("currentRatio") if out["balance"] else None,
        "shareChange": out["fy"][-1].get("shareChange"),
    }
    out["grade"] = grade(latest)
    out["grade"]["asOf"] = out["ttm"].get("asOf")

    flag_and_strengths(out)
    return out


# ---------------------------------------------------------------- grading

def _interp(x, pts):
    """Piecewise-linear score from [(x, score), ...] sorted by x."""
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def grade(r):
    """0-100 financial-health score from one period's numbers.

    A heuristic, not a forecast. Missing parts are left out and the score is
    rescaled over the parts that exist.
    """
    parts = []

    def add(key, label, mx, value, score, why):
        parts.append({"key": key, "label": label, "max": mx, "value": value,
                      "score": round(score, 1), "why": why})

    g = r.get("revenueGrowth")
    if g is not None:
        add("growth", "Growth", 25, g, _interp(g, [(-10, 0), (0, 8), (10, 15), (20, 20), (35, 25)]),
            f"Revenue {'grew' if g >= 0 else 'shrank'} {abs(g):.0f}% vs the year before.")
    om = r.get("operatingMargin")
    if om is not None:
        add("profit", "Profitability", 20, om, _interp(om, [(-25, 0), (-10, 3), (0, 7), (5, 11), (15, 16), (25, 20)]),
            f"Keeps {om:.0f}¢ of operating profit per $1 of sales." if om >= 0
            else f"Loses {abs(om):.0f}¢ on operations per $1 of sales.")
    fm = r.get("fcfMargin")
    if fm is not None:
        add("cash", "Cash generation", 20, fm, _interp(fm, [(-20, 0), (-10, 3), (0, 8), (10, 15), (20, 20)]),
            f"Turns {fm:.0f}¢ of every sales dollar into free cash." if fm >= 0
            else f"Burns {abs(fm):.0f}¢ of cash per $1 of sales.")
    nc, rev, cr = r.get("netCash"), r.get("revenue"), r.get("currentRatio")
    if nc is not None and rev:
        lev = -nc / rev  # net debt as a multiple of revenue
        s1 = _interp(lev, [(0, 12), (0.5, 8), (1, 5), (2, 0)]) if lev > 0 else 12
        s2 = _interp(cr, [(0.8, 0), (1, 5), (1.5, 8)]) if cr is not None else None
        score = s1 + s2 if s2 is not None else s1 / 12 * 20
        why = ("More cash than debt." if nc >= 0 else
               f"Net debt equal to {lev:.1f}x a year of sales.")
        fcf = r.get("fcf")
        if fcf is not None and fcf < 0:
            runway = max(nc, 0) / -fcf  # years of cash burn covered
            score *= min(1.0, runway / 3)
            why += (f" At the current burn, cash lasts about {runway:.1f} years." if runway < 10
                    else " Cash covers 10+ years of the current burn.")
        if cr is not None:
            why += f" Current ratio {cr:.2f}."
        add("balance", "Balance sheet", 20, round(-lev, 2), score, why)
    sc = r.get("shareChange")
    if sc is not None:
        add("dilution", "Share count", 15, sc, _interp(sc, [(-2, 15), (0, 13), (3, 9), (8, 4), (15, 0)]),
            f"Shares {'rose' if sc >= 0 else 'fell'} {abs(sc):.1f}% — "
            + ("your slice of the company shrank." if sc > 1 else
               "your slice grew (buybacks)." if sc < -1 else "roughly flat."))
    got = sum(p["score"] for p in parts)
    mx = sum(p["max"] for p in parts)
    if mx < 40:
        return {"score": None, "letter": None, "parts": parts, "coverage": mx}
    score = round(got / mx * 100)
    letter = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "F"
    return {"score": score, "letter": letter, "parts": parts, "coverage": mx}


def flag_and_strengths(f):
    fy, t, b = f["fy"], f["ttm"], f["balance"]
    flags, good = f["flags"], f["strengths"]
    last = fy[-1]
    g = t.get("revenueGrowth") if t.get("revenueGrowth") is not None else last.get("revenueGrowth")
    if g is not None:
        if g < 0:
            flags.append(f"Revenue shrinking ({g:+.0f}% year over year)")
        elif g >= 20:
            good.append(f"Revenue growing {g:.0f}% year over year")
    if t.get("fcf") is not None:
        if t["fcf"] < 0:
            flags.append("Negative free cash flow (trailing 12 months) — burning cash")
        else:
            good.append("Positive free cash flow (trailing 12 months)")
    if t.get("netIncome") is not None and t["netIncome"] < 0:
        flags.append("Net loss (trailing 12 months)")
    sc = last.get("shareChange")
    if sc is not None:
        if sc > 5:
            flags.append(f"Share count up {sc:.0f}% last year — dilution")
        elif sc < -1:
            good.append(f"Share count down {abs(sc):.0f}% last year — buybacks")
    if len(fy) >= 2 and fy[-1].get("operatingMargin") is not None and fy[-2].get("operatingMargin") is not None:
        dm = fy[-1]["operatingMargin"] - fy[-2]["operatingMargin"]
        if dm <= -3:
            flags.append(f"Operating margin fell {abs(dm):.0f} points last year")
        elif dm >= 3:
            good.append(f"Operating margin up {dm:.0f} points last year")
    if b:
        if b.get("netCash") is not None:
            if b["netCash"] > 0:
                good.append("More cash than debt")
            elif b.get("totalDebtYearAgo") is not None and b["totalDebt"] > b["totalDebtYearAgo"] * 1.25:
                flags.append("Debt exceeds cash and rose 25%+ in a year")
        cr = b.get("currentRatio")
        if cr is not None and cr < 1:
            flags.append(f"Current ratio {cr} — short-term liabilities exceed short-term assets")
        if b.get("equity") is not None and b["equity"] < 0:
            flags.append("Negative shareholders' equity")
    sbc, rev = t.get("sbc"), t.get("revenue")
    if sbc and rev and sbc / rev > 0.15:
        flags.append(f"Stock comp is {sbc / rev * 100:.0f}% of revenue")


def sec_ticker_map():
    """ticker -> (10-digit CIK, company name). Tries SEC's JSON list, then its plain-text list."""
    errs = []
    try:
        raw = get_json("https://www.sec.gov/files/company_tickers.json", SEC_UA)
        return {v["ticker"].upper(): (str(v["cik_str"]).zfill(10), v["title"]) for v in raw.values()}, None
    except Exception as e:  # noqa: BLE001
        errs.append(f"company_tickers.json: {e}")
    try:
        txt = get("https://www.sec.gov/include/ticker.txt", SEC_UA).decode()
        m = {}
        for line in txt.splitlines():
            t, _, c = line.partition("\t")
            if t and c.strip().isdigit():
                m[t.upper()] = (c.strip().zfill(10), None)
        if m:
            return m, None
    except Exception as e:  # noqa: BLE001
        errs.append(f"ticker.txt: {e}")
    return {}, "; ".join(errs)


def sec_facts(cik):
    return get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", SEC_UA)["facts"]


# ---------------------------------------------------------------- news

def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_rss(xml_bytes, default_source):
    items = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        src_el = it.find("source")
        source = _clean(src_el.text) if src_el is not None and src_el.text else default_source
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            dt = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat() if pub else None
        except Exception:  # noqa: BLE001
            dt = None
        if title and link:
            items.append({"title": title, "link": link, "source": source, "date": dt})
    return items


def short_name(title):
    t = re.sub(r"[,.]?\s+(inc|corp|corporation|holdings|group|ltd|plc|n\.v|co|company|technologies|platforms)\.?$",
               "", title or "", flags=re.I)
    return t.strip() or title


def fetch_news(sym, company):
    items, errs = [], []
    try:
        items += parse_rss(get(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"),
                           "Yahoo Finance")
    except Exception as e:  # noqa: BLE001
        errs.append(f"yahoo: {e}")
    q = f'"{short_name(company)}" OR "{sym} stock"' if company else f"{sym} stock"
    try:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q + " when:14d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        items += parse_rss(get(url), "Google News")
    except Exception as e:  # noqa: BLE001
        errs.append(f"google: {e}")
    seen, out = set(), []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
    for it in sorted(items, key=lambda x: x["date"] or "", reverse=True):
        key = re.sub(r"\W+", "", it["title"].lower())[:60]
        if key in seen or (it["date"] and it["date"] < cutoff):
            continue
        seen.add(key)
        out.append(it)
    return out[:20], ("; ".join(errs) if errs and not out else None)


# ---------------------------------------------------------------- main

def main():
    with open(TICKERS_FILE) as fh:
        tickers = [t["symbol"].upper() for t in json.load(fh)["tickers"]]
    try:
        with open(OUT_FILE) as fh:
            old = json.load(fh).get("tickers", {})
    except Exception:  # noqa: BLE001
        old = {}

    # CIKs never change, so remember the ones we've already resolved
    known = {s: (r.get("cik"), r.get("name")) for s, r in old.items() if r.get("cik")}
    tmap, sec_err = sec_ticker_map()
    if sec_err:
        print("SEC ticker list failed:", sec_err, file=sys.stderr)
    meta = {"secTickerList": "ok" if tmap else sec_err}

    out = {}
    for sym in tickers:
        prev = old.get(sym, {})
        rec = {"symbol": sym, "errors": {}}
        cik, title = tmap.get(sym) or known.get(sym) or (None, None)
        if tmap.get(sym) and not tmap[sym][1]:
            title = (known.get(sym) or (None, None))[1]
        rec["cik"], rec["name"] = cik, title

        price, err = fetch_price(sym)
        rec["price"] = price or prev.get("price")
        if err and not price:
            rec["errors"]["price"] = err

        if cik:
            try:
                rec["fundamentals"] = fundamentals(sec_facts(cik), rec["price"])
                if rec["fundamentals"] is None:
                    rec["errors"]["fundamentals"] = "No annual financials in SEC filings yet"
            except Exception as e:  # noqa: BLE001
                rec["fundamentals"] = prev.get("fundamentals")
                rec["errors"]["fundamentals"] = f"SEC filings request failed: {e}"
                print(f"{sym} SEC facts failed: {e}", file=sys.stderr)
            time.sleep(0.25)
        else:
            rec["fundamentals"] = prev.get("fundamentals")
            rec["errors"]["fundamentals"] = ("Not in SEC company list (ETF, fund, or foreign listing)" if tmap
                                             else f"Couldn't load SEC company list: {sec_err}")

        news, err = fetch_news(sym, title)
        rec["news"] = news or prev.get("news", [])
        if err:
            rec["errors"]["news"] = err
        if not rec["errors"]:
            del rec["errors"]
        out[sym] = rec
        print(f"{sym}: price={'ok' if rec['price'] else 'none'} "
              f"fund={'ok' if rec.get('fundamentals') else 'none'} news={len(rec['news'])}")

    with open(OUT_FILE, "w") as fh:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "meta": meta, "tickers": out},
                  fh, separators=(",", ":"))


if __name__ == "__main__":
    main()

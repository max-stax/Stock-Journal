#!/usr/bin/env python3
"""Daily data job for Stock Journal.

Reads data/tickers.json and writes data/market.json. Per ticker:
  price         last close, 52-week range, 1y weekly + 5y monthly closes,
                moving averages, returns, up/down volume
  fundamentals  income, cash flow and balance sheet by fiscal year, last 12
                months, latest balance sheet, valuation, health grade, flags.
                Source: SEC EDGAR when reachable, otherwise Yahoo Finance's
                fundamentals feed (built from the same filings).
  canslim       pass/fail per CAN SLIM letter
  institutions  % held by institutions, number of holders, history
  news          recent headlines (Yahoo Finance + Google News RSS)

Standard library only. Every source is optional: if one fails, the previous
day's value is kept and the reason is recorded under "errors".
"""
import gzip
import http.cookiejar
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKERS_FILE = os.path.join(ROOT, "data", "tickers.json")
OUT_FILE = os.path.join(ROOT, "data", "market.json")

SEC_UA = os.environ.get("SEC_USER_AGENT") or "Stock Journal research max-stax@users.noreply.github.com"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Default grade weights (the app lets you change these)
WEIGHTS = {"growth": 20, "profit": 20, "cash": 20, "balance": 25, "dilution": 15}


# ================================================================ http

def _decode(body, enc):
    if enc == "gzip" or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body


socket.setdefaulttimeout(15)
TIMINGS = {}  # "SYM source" -> seconds, written to market.json so slow sources are visible


SLOW = set()  # sources that hit their time limit; skipped for the rest of the run


class Budget(BaseException):  # bypasses inner `except Exception` retries
    pass


def _alarm(*_):
    raise Budget("time limit reached")


def timed(label, fn, *a, limit=40):
    """Run fn with a hard wall-clock limit (covers DNS stalls and slow trickling responses)."""
    source = label.split(" ", 1)[-1]
    if source in SLOW:
        raise RuntimeError(f"skipped: {source} was too slow earlier this run")
    t = time.time()
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(limit)
    try:
        return fn(*a)
    except Budget:
        SLOW.add(source)
        raise RuntimeError(f"gave up after {limit}s (source too slow)") from None
    finally:
        signal.alarm(0)
        TIMINGS[label] = round(time.time() - t, 1)


HOST_FAILS = {}  # host -> consecutive timeouts; after 2, skip the host for the rest of the run


def get(url, ua=BROWSER_UA, timeout=12, tries=2, opener=None):
    host = urllib.parse.urlsplit(url).netloc
    if HOST_FAILS.get(host, 0) >= 2:
        raise RuntimeError(f"{host} skipped (not responding this run)")
    last = None
    op = opener or urllib.request.build_opener()
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate",
                                                       "Accept": "*/*"})
            with op.open(req, timeout=timeout) as r:
                HOST_FAILS[host] = 0
                return _decode(r.read(), r.headers.get("Content-Encoding"))
        except urllib.error.HTTPError as e:
            snippet = ""
            try:
                raw = _decode(e.read(), e.headers.get("Content-Encoding"))
                title = re.search(rb"<title>(.*?)</title>", raw, re.S)
                snippet = (title.group(1) if title else raw[:120]).decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                pass
            last = RuntimeError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc} {snippet}".strip())
            if e.code in (400, 401, 402, 403, 404, 429):
                break
        except Exception as e:  # noqa: BLE001
            last = RuntimeError(f"{type(e).__name__}: {e}")
            if "timed out" in str(e).lower() or isinstance(e, TimeoutError):
                HOST_FAILS[host] = HOST_FAILS.get(host, 0) + 1
                if HOST_FAILS[host] >= 2:
                    break
        time.sleep(1.5 * (i + 1))
    raise last


def get_json(url, ua=BROWSER_UA, opener=None):
    return json.loads(get(url, ua, opener=opener))


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1) * 100, 1) if b > 0 else None


def ratio(a, b):
    if a is None or b in (None, 0):
        return None
    return round(a / b * 100, 1)


def _interp(x, pts):
    """Piecewise-linear value from [(x, y), ...] sorted by x."""
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


# ================================================================ Yahoo session (cookies + crumb)

class Yahoo:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.crumb = None

    def _crumb(self):
        if self.crumb is None:
            try:
                get("https://fc.yahoo.com", opener=self.op, tries=1, timeout=6)
            except Exception:  # noqa: BLE001  (404 is normal; it still sets the cookie)
                pass
            try:
                self.crumb = get("https://query2.finance.yahoo.com/v1/test/getcrumb", opener=self.op,
                                 tries=1, timeout=8).decode().strip()
            except Exception:  # noqa: BLE001
                self.crumb = ""
        return self.crumb

    def json(self, url, crumb=False):
        if crumb or "quoteSummary" in url:
            c = self._crumb()
            if c:
                url += ("&" if "?" in url else "?") + "crumb=" + urllib.parse.quote(c)
        try:
            return get_json(url, opener=self.op)
        except RuntimeError as e:
            if "401" in str(e) and not crumb:
                return self.json(url, crumb=True)
            raise


YH = Yahoo()


# ---------------------------------------------------------------- Financial Modeling Prep (free key)

FMP_KEY = os.environ.get("FMP_API_KEY", "").strip()


def fmp(path, **params):
    params["apikey"] = FMP_KEY
    data = get_json(f"https://financialmodelingprep.com/stable/{path}?" + urllib.parse.urlencode(params))
    if isinstance(data, dict) and (data.get("Error Message") or data.get("error")):
        raise RuntimeError(f"FMP {path}: {data.get('Error Message') or data.get('error')}")
    return data if isinstance(data, list) else []


def _f(r, *keys):
    for k in keys:
        if r.get(k) is not None:
            return r[k]
    return None



# ================================================================ prices

def price_yahoo(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=5y&interval=1d"
    res = YH.json(url)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    hist = [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), round(c, 4), v or 0)
            for t, c, v in zip(res.get("timestamp") or [], q.get("close") or [], q.get("volume") or [])
            if c is not None]
    p = build_price(hist, res["meta"].get("regularMarketPrice"), res["meta"].get("currency", "USD"))
    m = res["meta"]
    p["_name"] = m.get("longName") or m.get("shortName")
    p["_type"] = m.get("instrumentType")
    return p


def price_stooq(sym):
    rows = get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d").decode().strip().splitlines()[1:]
    hist = []
    for line in rows[-1300:]:
        p = line.split(",")
        if len(p) >= 5 and p[4] not in ("", "N/D"):
            hist.append((p[0], float(p[4]), float(p[5]) if len(p) > 5 and p[5] else 0))
    return build_price(hist, None, "USD")


def build_price(hist, last=None, currency="USD"):
    """hist: [(iso_date, close, volume)] daily, up to ~5 years (split-adjusted)."""
    if not hist:
        raise ValueError("no price history")
    hist = [h if len(h) == 3 else (h[0], h[1], 0) for h in hist]
    last = float(last) if last else hist[-1][1]
    prev = hist[-2][1] if len(hist) > 1 else last
    if hist[-1][1] != last:
        prev = hist[-1][1]
    closes = [c for _, c, _ in hist]
    year = hist[-252:]
    yc = [c for _, c, _ in year]
    weekly = [(d, c) for d, c, _ in year[::5]] + ([(year[-1][0], year[-1][1])] if (len(year) - 1) % 5 else [])
    monthly = {}
    for d, c, _ in hist:
        monthly[d[:7]] = (d, c)

    def ret(n):
        return round((last / closes[-n - 1] - 1) * 100, 1) if len(closes) > n else None

    def ma(n):
        return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None

    up = dn = 0.0
    for (_, c0, _), (_, c1, v1) in zip(hist[-51:-1], hist[-50:]):
        if c1 > c0:
            up += v1
        elif c1 < c0:
            dn += v1
    return {
        "last": round(last, 4), "prevClose": round(prev, 4),
        "change": round(last - prev, 4), "changePct": round((last / prev - 1) * 100, 2) if prev else None,
        "hi52": max(yc), "lo52": min(yc), "asOf": hist[-1][0], "currency": currency,
        "ma50": ma(50), "ma200": ma(200),
        "ret3m": ret(63), "ret6m": ret(126), "ret9m": ret(189), "ret12m": ret(252),
        "upDownVol50": round(up / dn, 2) if dn else None,
        "history": [[d, c] for d, c in weekly],
        "monthly": [[d, c] for d, c in monthly.values()],
    }


def price_fmp(sym):
    start = (date.today() - timedelta(days=5 * 365 + 10)).isoformat()
    rows = fmp("historical-price-eod/full", symbol=sym, **{"from": start})
    if isinstance(rows, dict):
        rows = rows.get("historical", [])
    hist = sorted((r["date"], float(r.get("adjClose") or r["close"]), float(r.get("volume") or 0))
                  for r in rows if r.get("close") is not None)
    return build_price(hist, None, "USD")


def fetch_price(sym):
    errors = []
    for fn in ((price_fmp,) if FMP_KEY else ()) + (price_yahoo, price_stooq):
        try:
            return fn(sym), None
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e}")
    return None, "; ".join(errors)


def rs_weighted(p):
    """IBD-style weighting: latest quarter counts double."""
    if not p or p.get("ret12m") is None:
        return None
    r3, r6, r9, r12 = (p.get(k) or 0 for k in ("ret3m", "ret6m", "ret9m", "ret12m"))
    return round(0.4 * r3 + 0.2 * r6 + 0.2 * r9 + 0.2 * r12, 1)


# ================================================================ normalized fundamentals
#
# Both sources produce:
#   fy:        [{end, revenue, grossProfit, operatingIncome, netIncome, operatingCashFlow, capex,
#                dilutedShares, sbc, eps, <balance fields>}]   (oldest -> newest)
#   ttm:       {asOf, revenue, grossProfit, operatingIncome, netIncome, operatingCashFlow, capex, sbc, eps}
#   growth:    latest revenue growth vs a year ago (or None)
#   bal:       {date: {cash, sti, totalDebt, equity, currentAssets, currentLiabilities,
#                      totalAssets, totalLiabilities, intangibles}}
#   quarters:  [{end, revenue, eps, netIncome}]   (oldest -> newest)
#   shares:    latest shares outstanding
#   currency

BAL_KEYS = ("cash", "sti", "totalDebt", "equity", "currentAssets", "currentLiabilities",
            "totalAssets", "totalLiabilities", "intangibles")


# ---------------------------------------------------------------- SEC EDGAR

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT"}
QUARTER_FORMS = {"10-Q", "10-Q/A", "6-K"}

SEC_DUR = {
    "revenue": [("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"), ("us-gaap", "Revenues"),
                ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"), ("us-gaap", "SalesRevenueNet"),
                ("us-gaap", "RevenuesNetOfInterestExpense"), ("ifrs-full", "Revenue")],
    "grossProfit": [("us-gaap", "GrossProfit"), ("ifrs-full", "GrossProfit")],
    "costOfRevenue": [("us-gaap", "CostOfRevenue"), ("us-gaap", "CostOfGoodsAndServicesSold"),
                      ("ifrs-full", "CostOfSales")],
    "operatingIncome": [("us-gaap", "OperatingIncomeLoss"), ("ifrs-full", "ProfitLossFromOperatingActivities")],
    "netIncome": [("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss"),
                  ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"), ("ifrs-full", "ProfitLoss")],
    "operatingCashFlow": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
                          ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
                          ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")],
    "capex": [("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
              ("us-gaap", "PaymentsToAcquireProductiveAssets"),
              ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
              ("ifrs-full", "PurchaseOfPropertyPlantAndEquipment")],
    "dilutedShares": [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
                      ("ifrs-full", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
                      ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic")],
    "sbc": [("us-gaap", "ShareBasedCompensation"), ("us-gaap", "AllocatedShareBasedCompensationExpense")],
    "eps": [("us-gaap", "EarningsPerShareDiluted"), ("us-gaap", "EarningsPerShareBasicAndDiluted"),
            ("ifrs-full", "DilutedEarningsLossPerShare")],
}
SEC_INST = {
    "cash": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
             ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
             ("ifrs-full", "CashAndCashEquivalents")],
    "sti": [("us-gaap", "ShortTermInvestments"), ("us-gaap", "MarketableSecuritiesCurrent"),
            ("us-gaap", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"), ("ifrs-full", "CurrentInvestments")],
    "ltDebt": [("us-gaap", "LongTermDebtNoncurrent"), ("us-gaap", "LongTermDebt"),
               ("us-gaap", "ConvertibleNotesPayableNoncurrent"), ("us-gaap", "SeniorNotesNoncurrent"),
               ("ifrs-full", "NoncurrentBorrowings"), ("ifrs-full", "Borrowings")],
    "stDebt": [("us-gaap", "LongTermDebtCurrent"), ("us-gaap", "DebtCurrent"),
               ("us-gaap", "ConvertibleNotesPayableCurrent"), ("ifrs-full", "CurrentBorrowings")],
    "equity": [("us-gaap", "StockholdersEquity"),
               ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
               ("ifrs-full", "EquityAttributableToOwnersOfParent"), ("ifrs-full", "Equity")],
    "currentAssets": [("us-gaap", "AssetsCurrent"), ("ifrs-full", "CurrentAssets")],
    "currentLiabilities": [("us-gaap", "LiabilitiesCurrent"), ("ifrs-full", "CurrentLiabilities")],
    "totalAssets": [("us-gaap", "Assets"), ("ifrs-full", "Assets")],
    "totalLiabilities": [("us-gaap", "Liabilities"), ("ifrs-full", "Liabilities")],
    "goodwill": [("us-gaap", "Goodwill"), ("ifrs-full", "Goodwill")],
    "otherIntangibles": [("us-gaap", "IntangibleAssetsNetExcludingGoodwill"),
                         ("us-gaap", "FiniteLivedIntangibleAssetsNet"),
                         ("ifrs-full", "IntangibleAssetsOtherThanGoodwill")],
}


def _unit_rows(facts, tax, concept, unit_pref):
    node = facts.get(tax, {}).get(concept)
    if not node:
        return None, None
    units = node.get("units", {})
    for u in unit_pref:
        if u in units:
            return units[u], u
    for u, v in units.items():
        if re.fullmatch(r"[A-Z]{3}(/shares)?", u) and ("/shares" in u) == any("/shares" in x for x in unit_pref):
            return v, u
    return None, None


def _dur(facts, cands, unit_pref):
    out, unit_used = {}, None
    for tax, concept in cands:
        rows, unit = _unit_rows(facts, tax, concept, unit_pref)
        if not rows:
            continue
        for r in rows:
            if "start" not in r:
                continue
            key = (r["start"], r["end"])
            p = out.get(key)
            if p is None or (p["_c"] == concept and r.get("filed", "") > p.get("filed", "")):
                out[key] = {**r, "_c": concept}
        unit_used = unit_used or unit
    return list(out.values()), unit_used


def _inst(facts, cands, unit_pref):
    out = {}
    for tax, concept in cands:
        rows, _ = _unit_rows(facts, tax, concept, unit_pref)
        for r in rows or []:
            if "start" in r:
                continue
            p = out.get(r["end"])
            if p is None or (p["_c"] == concept and r.get("filed", "") > p.get("filed", "")):
                out[r["end"]] = {**r, "_c": concept}
    return {d: v["val"] for d, v in out.items()}


def _annual(rows):
    res = {}
    for r in rows:
        if r.get("form") in ANNUAL_FORMS and 330 <= _days(r["start"], r["end"]) <= 400:
            if r["end"] not in res or r.get("filed", "") > res[r["end"]][1]:
                res[r["end"]] = (r["val"], r.get("filed", ""))
    return {k: v[0] for k, v in sorted(res.items())}


def _ttm(rows, fy_end, fy_val):
    """last FY + current YTD - prior-year YTD. Returns (value, asOf, ytd_growth)."""
    if fy_end is None or fy_val is None:
        return None, None, None
    ytd = [r for r in rows if r["end"] > fy_end and 60 <= _days(r["start"], r["end"]) <= 300]
    if not ytd:
        return fy_val, fy_end, None
    end = max(r["end"] for r in ytd)
    cur = max((r for r in ytd if r["end"] == end), key=lambda r: _days(r["start"], r["end"]))
    span = _days(cur["start"], cur["end"])
    prior = [r for r in rows if 355 <= _days(r["end"], cur["end"]) <= 375
             and abs(_days(r["start"], r["end"]) - span) <= 10]
    if not prior:
        return fy_val, fy_end, None
    prev = max(prior, key=lambda r: r.get("filed", ""))
    return fy_val + cur["val"] - prev["val"], cur["end"], pct(cur["val"], prev["val"])


def _quarters(rows):
    """end -> 3-month value, including Q4 derived as FY minus 9-month YTD."""
    q = {}
    for r in rows:
        if 80 <= _days(r["start"], r["end"]) <= 100:
            q[r["end"]] = r["val"]
    for r in rows:
        if r.get("form") in ANNUAL_FORMS and 330 <= _days(r["start"], r["end"]) <= 400 and r["end"] not in q:
            nine = [x for x in rows if x["start"] == r["start"] and 250 <= _days(x["start"], x["end"]) <= 290]
            if nine:
                q[r["end"]] = r["val"] - max(nine, key=lambda x: x["end"])["val"]
    return q


def from_sec(facts):
    rev_rows, currency = _dur(facts, SEC_DUR["revenue"], ("USD",))
    currency = currency or "USD"
    up = (currency,)
    rows = {k: (rev_rows if k == "revenue" else
                _dur(facts, c, ("shares",) if k == "dilutedShares" else
                     (f"{currency}/shares",) if k == "eps" else up)[0])
            for k, c in SEC_DUR.items()}
    ann = {k: _annual(v) for k, v in rows.items()}
    ends = sorted(ann["revenue"] or ann["netIncome"])[-5:]
    if not ends:
        return None
    inst = {k: _inst(facts, c, up) for k, c in SEC_INST.items()}

    def bal_at(d, tol=10):
        b = {}
        for k in inst:
            near = [x for x in inst[k] if abs(_days(x, d)) <= tol]
            b[k] = inst[k][min(near, key=lambda x: abs(_days(x, d)))] if near else None
        debt = None if b["ltDebt"] is None and b["stDebt"] is None else (b["ltDebt"] or 0) + (b["stDebt"] or 0)
        intang = None if b["goodwill"] is None and b["otherIntangibles"] is None else \
            (b["goodwill"] or 0) + (b["otherIntangibles"] or 0)
        tl = b["totalLiabilities"]
        if tl is None and b["totalAssets"] is not None and b["equity"] is not None:
            tl = b["totalAssets"] - b["equity"]
        return {"cash": b["cash"], "sti": b["sti"], "totalDebt": debt, "equity": b["equity"],
                "currentAssets": b["currentAssets"], "currentLiabilities": b["currentLiabilities"],
                "totalAssets": b["totalAssets"], "totalLiabilities": tl,
                "intangibles": intang if intang is not None or b["totalAssets"] is None else 0}

    fy = []
    for e in ends:
        row = {"end": e, **{k: ann[k].get(e) for k in SEC_DUR}}
        if row["grossProfit"] is None and row["revenue"] is not None and row["costOfRevenue"] is not None:
            row["grossProfit"] = row["revenue"] - row["costOfRevenue"]
        row.update(bal_at(e))
        fy.append(row)

    last = ends[-1]
    ttm, as_of, growth = {}, last, None
    for k in ("revenue", "grossProfit", "operatingIncome", "netIncome", "operatingCashFlow", "capex", "sbc", "eps"):
        v, e, g = _ttm(rows[k], last, ann[k].get(last))
        ttm[k] = v
        if k == "revenue":
            as_of, growth = e, g
    ttm["asOf"] = as_of

    dates = sorted(set(inst["cash"]) | set(inst["equity"]) | set(inst["totalAssets"]))
    bal = {d: bal_at(d, 0) for d in dates[-6:]}
    so = _inst(facts, [("dei", "EntityCommonStockSharesOutstanding")], ("shares",))
    qr = {k: _quarters(rows[k]) for k in ("revenue", "eps", "netIncome")}
    qends = sorted(set(qr["revenue"]) | set(qr["eps"]))[-9:]
    quarters = [{"end": e, **{k: qr[k].get(e) for k in qr}} for e in qends]
    return {"source": "SEC EDGAR", "currency": currency, "fy": fy, "ttm": ttm, "growth": growth, "bal": bal,
            "quarters": quarters, "shares": so[max(so)] if so else None}


def sec_ticker_map():
    errs = []
    try:
        raw = get_json("https://www.sec.gov/files/company_tickers.json", SEC_UA)
        return {v["ticker"].upper(): (str(v["cik_str"]).zfill(10), v["title"]) for v in raw.values()}, None
    except Exception as e:  # noqa: BLE001
        errs.append(str(e))
    return {}, "; ".join(errs)


def sec_facts(cik):
    return get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", SEC_UA)["facts"]


# ---------------------------------------------------------------- Yahoo fundamentals feed

Y_FLOW = {  # income + cash flow: annual, quarterly, trailing
    "revenue": "TotalRevenue", "grossProfit": "GrossProfit", "operatingIncome": "OperatingIncome",
    "netIncome": "NetIncomeCommonStockholders", "netIncomeAlt": "NetIncome",
    "operatingCashFlow": "OperatingCashFlow", "capex": "CapitalExpenditure", "sbc": "StockBasedCompensation",
    "eps": "DilutedEPS", "dilutedShares": "DilutedAverageShares",
}
Y_BAL = {  # balance sheet: annual, quarterly
    "cash": "CashAndCashEquivalents", "sti": "OtherShortTermInvestments",
    "cashSti": "CashCashEquivalentsAndShortTermInvestments", "totalDebt": "TotalDebt",
    "equity": "StockholdersEquity", "currentAssets": "CurrentAssets", "currentLiabilities": "CurrentLiabilities",
    "totalAssets": "TotalAssets", "totalLiabilities": "TotalLiabilitiesNetMinorityInterest",
    "intangibles": "GoodwillAndOtherIntangibleAssets", "goodwill": "Goodwill",
    "otherIntangibles": "OtherIntangibleAssets", "sharesOut": "OrdinarySharesNumber",
}


def from_yahoo(sym):
    types = []
    for prefix in ("annual", "quarterly", "trailing"):
        types += [prefix + v for v in Y_FLOW.values()]
    for prefix in ("annual", "quarterly"):
        types += [prefix + v for v in Y_BAL.values()]
    now = int(time.time())
    url = (f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
           f"{urllib.parse.quote(sym)}?symbol={urllib.parse.quote(sym)}&type={','.join(types)}"
           f"&period1={now - 7 * 365 * 86400}&period2={now}")
    res = YH.json(url)["timeseries"]["result"]
    data, currency = {}, None
    for r in res:
        t = (r.get("meta", {}).get("type") or [None])[0]
        for v in r.get(t) or []:
            if not v or v.get("reportedValue") is None:
                continue
            data.setdefault(t, {})[v["asOfDate"]] = v["reportedValue"]["raw"]
            currency = currency or v.get("currencyCode")
    if not data:
        return None

    def series(prefix, key, table):
        return data.get(prefix + table[key], {})

    def flow(prefix):
        out = {}
        for k in Y_FLOW:
            for d, v in series(prefix, k, Y_FLOW).items():
                out.setdefault(d, {})[k] = v
        for d, r in out.items():
            if r.get("netIncome") is None:
                r["netIncome"] = r.get("netIncomeAlt")
            if r.get("capex") is not None:
                r["capex"] = abs(r["capex"])
        return out

    def balance(prefix):
        out = {}
        for k in Y_BAL:
            for d, v in series(prefix, k, Y_BAL).items():
                out.setdefault(d, {})[k] = v
        res = {}
        for d, b in out.items():
            cash, sti = b.get("cash"), b.get("sti")
            if cash is None and b.get("cashSti") is not None:
                cash, sti = b["cashSti"], 0
            intang = b.get("intangibles")
            if intang is None and (b.get("goodwill") is not None or b.get("otherIntangibles") is not None):
                intang = (b.get("goodwill") or 0) + (b.get("otherIntangibles") or 0)
            if intang is None and b.get("totalAssets") is not None:
                intang = 0
            tl = b.get("totalLiabilities")
            if tl is None and b.get("totalAssets") is not None and b.get("equity") is not None:
                tl = b["totalAssets"] - b["equity"]
            res[d] = {"cash": cash, "sti": sti, "totalDebt": b.get("totalDebt"), "equity": b.get("equity"),
                      "currentAssets": b.get("currentAssets"), "currentLiabilities": b.get("currentLiabilities"),
                      "totalAssets": b.get("totalAssets"), "totalLiabilities": tl, "intangibles": intang,
                      "_sharesOut": b.get("sharesOut")}
        return res

    afl, qfl, tfl = flow("annual"), flow("quarterly"), flow("trailing")
    abal, qbal = balance("annual"), balance("quarterly")
    ends = sorted(d for d, r in afl.items() if r.get("revenue") is not None or r.get("netIncome") is not None)[-5:]
    if not ends:
        return None
    fy = []
    for e in ends:
        row = {"end": e, **{k: afl[e].get(k) for k in Y_FLOW if k != "netIncomeAlt"}}
        row.update({k: v for k, v in (abal.get(e) or {}).items() if not k.startswith("_")})
        fy.append(row)

    t_end = max(tfl) if tfl else ends[-1]
    ttm = {k: (tfl.get(t_end) or {}).get(k) for k in
           ("revenue", "grossProfit", "operatingIncome", "netIncome", "operatingCashFlow", "capex", "sbc", "eps")}
    for k in ttm:  # fall back to last fiscal year where trailing is missing
        if ttm[k] is None:
            ttm[k] = afl[ends[-1]].get(k)
    ttm["asOf"] = t_end
    quarters = [{"end": d, "revenue": r.get("revenue"), "eps": r.get("eps"), "netIncome": r.get("netIncome")}
                for d, r in sorted(qfl.items())]
    growth = None
    if len(quarters) >= 5 and quarters[-5]["revenue"]:
        growth = pct(quarters[-1]["revenue"], quarters[-5]["revenue"])
    bal = {**{d: {k: v for k, v in b.items() if not k.startswith("_")} for d, b in abal.items()},
           **{d: {k: v for k, v in b.items() if not k.startswith("_")} for d, b in qbal.items()}}
    shares = None
    for src in (qbal, abal):
        sd = [d for d, b in src.items() if b.get("_sharesOut")]
        if sd:
            shares = src[max(sd)]["_sharesOut"]
            break
    return {"source": "Yahoo Finance (from company filings)", "currency": currency or "USD", "fy": fy, "ttm": ttm,
            "growth": growth, "bal": bal, "quarters": quarters, "shares": shares}


def from_fmp(sym):
    inc = {r["date"]: r for r in fmp("income-statement", symbol=sym, period="annual", limit=5)}
    if not inc:
        return None
    bs = {r["date"]: r for r in fmp("balance-sheet-statement", symbol=sym, period="annual", limit=5)}
    cf = {r["date"]: r for r in fmp("cash-flow-statement", symbol=sym, period="annual", limit=5)}
    currency = next(iter(inc.values())).get("reportedCurrency") or "USD"

    def bal(b):
        if not b:
            return {k: None for k in BAL_KEYS}
        return {"cash": _f(b, "cashAndCashEquivalents"), "sti": _f(b, "shortTermInvestments"),
                "totalDebt": _f(b, "totalDebt"), "equity": _f(b, "totalStockholdersEquity", "totalEquity"),
                "currentAssets": _f(b, "totalCurrentAssets"), "currentLiabilities": _f(b, "totalCurrentLiabilities"),
                "totalAssets": _f(b, "totalAssets"), "totalLiabilities": _f(b, "totalLiabilities"),
                "intangibles": _f(b, "goodwillAndIntangibleAssets")}

    def flow(i, c):
        c = c or {}
        capex = _f(c, "capitalExpenditure", "investmentsInPropertyPlantAndEquipment")
        return {"revenue": _f(i, "revenue"), "grossProfit": _f(i, "grossProfit"),
                "operatingIncome": _f(i, "operatingIncome"), "netIncome": _f(i, "netIncome"),
                "operatingCashFlow": _f(c, "operatingCashFlow", "netCashProvidedByOperatingActivities"),
                "capex": abs(capex) if capex is not None else None, "sbc": _f(c, "stockBasedCompensation"),
                "eps": _f(i, "epsDiluted", "epsdiluted", "eps"),
                "dilutedShares": _f(i, "weightedAverageShsOutDil", "weightedAverageShsOut")}

    ends = sorted(inc)[-5:]
    fy = [{"end": e, **flow(inc[e], cf.get(e)), **bal(bs.get(e))} for e in ends]
    ttm, quarters, growth, bal_map = dict(fy[-1]), [], None, {e: bal(bs[e]) for e in bs}
    ttm["asOf"] = ends[-1]
    try:  # quarterly data may not be on every free plan; annual still works without it
        qi = {r["date"]: r for r in fmp("income-statement", symbol=sym, period="quarter", limit=8)}
        qc = {r["date"]: r for r in fmp("cash-flow-statement", symbol=sym, period="quarter", limit=4)}
        qb = fmp("balance-sheet-statement", symbol=sym, period="quarter", limit=5)
        qs = sorted(qi)
        quarters = [{"end": d, "revenue": _f(qi[d], "revenue"), "eps": _f(qi[d], "epsDiluted", "epsdiluted", "eps"),
                     "netIncome": _f(qi[d], "netIncome")} for d in qs]
        last4 = qs[-4:]
        if len(last4) == 4:
            for k in ("revenue", "grossProfit", "operatingIncome", "netIncome", "eps"):
                vals = [flow(qi[d], None)[k] for d in last4]
                ttm[k] = sum(vals) if None not in vals else ttm[k]
            cvals = [flow(qi[d], qc.get(d)) for d in last4]
            for k in ("operatingCashFlow", "capex", "sbc"):
                vals = [x[k] for x in cvals]
                if None not in vals:
                    ttm[k] = sum(vals)
            ttm["asOf"] = last4[-1]
        if len(quarters) >= 5 and quarters[-5]["revenue"]:
            growth = pct(quarters[-1]["revenue"], quarters[-5]["revenue"])
        for r in qb:
            bal_map[r["date"]] = bal(r)
    except Exception as e:  # noqa: BLE001
        print(f"{sym}: FMP quarterly data unavailable ({e}); using annual only", file=sys.stderr)
    shares = fy[-1].get("dilutedShares")
    return {"source": "Financial Modeling Prep (from company filings)", "currency": currency, "fy": fy,
            "ttm": {k: ttm.get(k) for k in ("asOf", "revenue", "grossProfit", "operatingIncome", "netIncome",
                                            "operatingCashFlow", "capex", "sbc", "eps")},
            "growth": growth, "bal": bal_map, "quarters": quarters, "shares": shares}


# ---------------------------------------------------------------- Finnhub (free key): as-reported SEC financials

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()


def finnhub(path, **params):
    params["token"] = FINNHUB_KEY
    time.sleep(1.1)  # free plan: ~60 calls/minute
    data = get_json(f"https://finnhub.io/api/v1/{path}?" + urllib.parse.urlencode(params))
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Finnhub {path}: {data['error']}")
    return data


def _fh_items(report):
    """{concept-without-taxonomy: value} for one statement list."""
    out = {}
    for it in report or []:
        c = str(it.get("concept", "")).split("_", 1)[-1].split(":")[-1]
        v = it.get("value")
        if c and isinstance(v, (int, float)) and c not in out:
            out[c] = v
    return out


def _pick(items, cands):
    for _, concept in cands:
        if concept in items:
            return items[concept]
    return None


def _fh_balance(bs):
    g = lambda k: _pick(bs, SEC_INST[k])  # noqa: E731
    lt, st = g("ltDebt"), g("stDebt")
    gw, oi = g("goodwill"), g("otherIntangibles")
    ta, eq, tl = g("totalAssets"), g("equity"), g("totalLiabilities")
    if tl is None and ta is not None and eq is not None:
        tl = ta - eq
    return {"cash": g("cash"), "sti": g("sti"), "totalDebt": None if lt is None and st is None else (lt or 0) + (st or 0),
            "equity": eq, "currentAssets": g("currentAssets"), "currentLiabilities": g("currentLiabilities"),
            "totalAssets": ta, "totalLiabilities": tl,
            "intangibles": (gw or 0) + (oi or 0) if (gw is not None or oi is not None or ta is not None) else None}


def _fh_flow(ic, cf):
    g = lambda d, k: _pick(d, SEC_DUR[k])  # noqa: E731
    rev, gp, cor = g(ic, "revenue"), g(ic, "grossProfit"), g(ic, "costOfRevenue")
    if gp is None and rev is not None and cor is not None:
        gp = rev - cor
    capex = g(cf, "capex")
    return {"revenue": rev, "grossProfit": gp, "operatingIncome": g(ic, "operatingIncome"),
            "netIncome": g(ic, "netIncome"), "operatingCashFlow": g(cf, "operatingCashFlow"),
            "capex": abs(capex) if capex is not None else None, "sbc": g(cf, "sbc"), "eps": g(ic, "eps"),
            "dilutedShares": g(ic, "dilutedShares")}


def from_finnhub(sym):
    ann = finnhub("stock/financials-reported", symbol=sym, freq="annual").get("data") or []
    ann = [a for a in ann if a.get("endDate")]
    if not ann:
        return None
    rows = {}
    for a in sorted(ann, key=lambda a: a.get("filedDate") or ""):  # later filings (amendments) win
        end = a["endDate"][:10]
        r = a.get("report") or {}
        bs, ic, cf = _fh_items(r.get("bs")), _fh_items(r.get("ic")), _fh_items(r.get("cf"))
        rows[end] = {"end": end, **_fh_flow(ic, cf), **_fh_balance(bs)}
    ends = sorted(rows)[-5:]
    fy = [rows[e] for e in ends]
    bal = {e: {k: rows[e][k] for k in BAL_KEYS} for e in ends}
    ttm = {k: fy[-1].get(k) for k in ("revenue", "grossProfit", "operatingIncome", "netIncome",
                                      "operatingCashFlow", "capex", "sbc", "eps")}
    ttm["asOf"], quarters, growth = ends[-1], [], None
    try:
        qd = finnhub("stock/financials-reported", symbol=sym, freq="quarterly").get("data") or []
        qmap = {}
        for q in sorted(qd, key=lambda q: q.get("filedDate") or ""):
            if not q.get("endDate"):
                continue
            end = q["endDate"][:10]
            r = q.get("report") or {}
            ic, bs = _fh_items(r.get("ic")), _fh_items(r.get("bs"))
            f = _fh_flow(ic, {})
            qmap[end] = {"end": end, "revenue": f["revenue"], "eps": f["eps"], "netIncome": f["netIncome"],
                         "_ic": f}
            if bs:
                bal[end] = _fh_balance(bs)
        # Annual 10-K rows hold full-year figures; derive Q4 as FY minus the three reported quarters
        for e in ends:
            if e in qmap:
                continue
            prior = [qmap[d] for d in sorted(qmap) if 0 < _days(d, e) < 300][-3:]
            if len(prior) == 3:
                q4 = {"end": e}
                for k in ("revenue", "eps", "netIncome"):
                    vals = [p[k] for p in prior]
                    q4[k] = rows[e][k] - sum(vals) if rows[e].get(k) is not None and None not in vals else None
                q4["_ic"] = {k: q4.get(k) for k in ("revenue", "eps", "netIncome")}
                qmap[e] = q4
        qs = sorted(qmap)[-9:]
        quarters = [{k: qmap[d][k] for k in ("end", "revenue", "eps", "netIncome")} for d in qs]
        last4 = qs[-4:]
        if len(last4) == 4 and _days(last4[0], last4[-1]) < 300 and last4[-1] > ends[-1]:
            for k in ("revenue", "netIncome", "eps"):
                vals = [qmap[d][k] for d in last4]
                if None not in vals:
                    ttm[k] = sum(vals)
            ttm["asOf"] = last4[-1]
        if len(quarters) >= 5:
            ya = next((x for x in reversed(quarters[:-1]) if 355 <= _days(x["end"], quarters[-1]["end"]) <= 375), None)
            if ya and ya.get("revenue"):
                growth = pct(quarters[-1]["revenue"], ya["revenue"])
    except Exception as e:  # noqa: BLE001
        print(f"{sym}: Finnhub quarterly unavailable ({e}); annual only", file=sys.stderr)
    return {"source": "Finnhub (as reported to the SEC)", "currency": "USD", "fy": fy, "ttm": ttm, "growth": growth,
            "bal": bal, "quarters": quarters, "shares": fy[-1].get("dilutedShares")}


# ================================================================ analysis

def derive_balance(b, revenue=None):
    """Adds ratios to a balance-sheet snapshot."""
    if not b:
        return {}
    b = dict(b)
    cashlike = None if b.get("cash") is None else (b.get("cash") or 0) + (b.get("sti") or 0)
    b["cashAndInvestments"] = cashlike
    b["netCash"] = None if cashlike is None or b.get("totalDebt") is None else cashlike - b["totalDebt"]
    if b["netCash"] is None and cashlike is not None and b.get("totalDebt") is None:
        b["netCash"] = cashlike  # no reported borrowings
    b["currentRatio"] = round(b["currentAssets"] / b["currentLiabilities"], 2) \
        if b.get("currentAssets") and b.get("currentLiabilities") else None
    b["liabToAssets"] = round(b["totalLiabilities"] / b["totalAssets"] * 100, 1) \
        if b.get("totalLiabilities") is not None and b.get("totalAssets") else None
    b["intangibleShare"] = round(b["intangibles"] / b["totalAssets"] * 100, 1) \
        if b.get("intangibles") is not None and b.get("totalAssets") else None
    b["debtToEquity"] = round(b["totalDebt"] / b["equity"], 2) \
        if b.get("totalDebt") is not None and b.get("equity") and b["equity"] > 0 else None
    return b


def balance_part(b, fcf, bvps_growth):
    """Balance-sheet part of the grade, built from assets, liabilities and equity."""
    checks = []

    def add(key, label, weight, value, score, text):
        checks.append({"key": key, "label": label, "weight": weight, "value": value,
                       "score": round(score, 3), "text": text})

    la = b.get("liabToAssets")
    if la is not None:
        add("liabToAssets", "Liabilities ÷ assets", 0.25, la,
            _interp(la, [(30, 1), (50, 0.7), (70, 0.35), (90, 0)]),
            f"{la:.0f}% of what it owns is funded by what it owes.")
    cr = b.get("currentRatio")
    if cr is not None:
        add("currentRatio", "Current ratio", 0.20, cr, _interp(cr, [(0.8, 0), (1, 0.4), (1.5, 0.75), (2, 1)]),
            f"${cr:.2f} of short-term assets per $1 due within a year.")
    nc = b.get("netCash")
    if nc is not None:
        lev = -nc / b["_revenue"] if b.get("_revenue") and nc < 0 else 0
        add("netCash", "Net cash", 0.25, nc, 1 if nc >= 0 else _interp(lev, [(0, 1), (0.5, 0.6), (1, 0.3), (2, 0)]),
            "More cash than debt." if nc >= 0 else f"Net debt equal to {lev:.1f}x a year of sales.")
    if bvps_growth is not None:
        add("bvpsGrowth", "Equity per share trend", 0.15, bvps_growth,
            _interp(bvps_growth, [(-10, 0), (0, 0.4), (10, 0.8), (20, 1)]),
            f"Book value per share {'grew' if bvps_growth >= 0 else 'fell'} {abs(bvps_growth):.0f}% in a year.")
    if b.get("equity") is not None and b["equity"] <= 0:
        add("bvpsGrowth", "Shareholders' equity", 0.15, None, 0, "Equity is negative: liabilities exceed assets.")
    ish = b.get("intangibleShare")
    if ish is not None:
        add("intangibleShare", "Goodwill + intangibles", 0.15, ish,
            _interp(ish, [(10, 1), (30, 0.6), (50, 0.2), (70, 0)]),
            f"{ish:.0f}% of assets are goodwill and intangibles.")
    if not checks:
        return None
    tw = sum(c["weight"] for c in checks)
    r = sum(c["score"] * c["weight"] for c in checks) / tw
    note = ""
    if fcf is not None and fcf < 0 and nc is not None:
        runway = max(nc, 0) / -fcf
        r *= min(1.0, runway / 3)
        note = (f" Burning cash: about {runway:.1f} years of runway." if runway < 10 else
                " Burning cash, but cash covers 10+ years.")
    return {"ratio": r, "checks": checks, "why": " ".join(c["text"] for c in checks[:2]) + note}


def grade(r, b, bvps_growth, weights=WEIGHTS):
    parts = []

    def add(key, label, value, rat, why, checks=None):
        p = {"key": key, "label": label, "value": value, "ratio": round(rat, 3), "why": why}
        if checks:
            p["checks"] = checks
        parts.append(p)

    g = r.get("revenueGrowth")
    if g is not None:
        add("growth", "Growth", g, _interp(g, [(-10, 0), (0, 0.32), (10, 0.6), (20, 0.8), (35, 1)]),
            f"Revenue {'grew' if g >= 0 else 'shrank'} {abs(g):.0f}% vs a year earlier.")
    om = r.get("operatingMargin")
    if om is not None:
        add("profit", "Profitability", om, _interp(om, [(-25, 0), (-10, .15), (0, .35), (5, .55), (15, .8), (25, 1)]),
            f"Keeps {om:.0f}¢ of operating profit per $1 of sales." if om >= 0
            else f"Loses {abs(om):.0f}¢ on operations per $1 of sales.")
    fm = r.get("fcfMargin")
    if fm is not None:
        add("cash", "Cash generation", fm, _interp(fm, [(-20, 0), (-10, .15), (0, .4), (10, .75), (20, 1)]),
            f"Turns {fm:.0f}¢ of every sales dollar into free cash." if fm >= 0
            else f"Burns {abs(fm):.0f}¢ of cash per $1 of sales.")
    bp = balance_part({**b, "_revenue": r.get("revenue")}, r.get("fcf"), bvps_growth) if b else None
    if bp:
        add("balance", "Balance sheet", None, bp["ratio"], bp["why"], bp["checks"])
    sc = r.get("shareChange")
    if sc is not None:
        add("dilution", "Share count", sc, _interp(sc, [(-2, 1), (0, .87), (3, .6), (8, .27), (15, 0)]),
            f"Shares {'rose' if sc >= 0 else 'fell'} {abs(sc):.1f}% — "
            + ("your slice of the company shrank." if sc > 1 else
               "your slice grew (buybacks)." if sc < -1 else "roughly flat."))
    return score_parts(parts, weights)


def score_parts(parts, weights):
    for p in parts:
        p["max"] = weights[p["key"]]
        p["score"] = round(p["ratio"] * p["max"], 1)
    mx = sum(p["max"] for p in parts)
    if mx < sum(weights.values()) * 0.4:
        return {"score": None, "letter": None, "parts": parts, "coverage": mx}
    score = round(sum(p["score"] for p in parts) / mx * 100)
    return {"score": score, "letter": letter(score), "parts": parts, "coverage": mx}


def letter(s):
    return "A" if s >= 80 else "B" if s >= 65 else "C" if s >= 50 else "D" if s >= 35 else "F"


def analyze(raw, price):
    fy = raw["fy"]
    prev = None
    for row in fy:
        row["fcf"] = row["operatingCashFlow"] - abs(row["capex"]) \
            if row.get("operatingCashFlow") is not None and row.get("capex") is not None else None
        row["revenueGrowth"] = pct(row.get("revenue"), prev.get("revenue")) if prev else None
        row["shareChange"] = pct(row.get("dilutedShares"), prev.get("dilutedShares")) if prev else None
        row["epsGrowth"] = pct(row.get("eps"), prev.get("eps")) if prev and (prev.get("eps") or 0) > 0 else None
        for k, n in (("grossMargin", "grossProfit"), ("operatingMargin", "operatingIncome"),
                     ("netMargin", "netIncome"), ("fcfMargin", "fcf")):
            row[k] = ratio(row.get(n), row.get("revenue"))
        b = derive_balance({k: row.get(k) for k in BAL_KEYS})
        row.update({k: b.get(k) for k in ("netCash", "currentRatio", "liabToAssets", "intangibleShare",
                                          "debtToEquity", "cashAndInvestments")})
        row["bvps"] = row["equity"] / row["dilutedShares"] \
            if row.get("equity") is not None and row.get("dilutedShares") else None
        row["bvpsGrowth"] = pct(row["bvps"], prev.get("bvps")) if prev and prev.get("bvps") and row["bvps"] else None
        avg_eq = [x for x in (row.get("equity"), prev.get("equity") if prev else None) if x]
        row["roe"] = ratio(row.get("netIncome"), sum(avg_eq) / len(avg_eq)) if avg_eq and sum(avg_eq) > 0 else None
        row["grade"] = grade(row, b, row["bvpsGrowth"])
        prev = row

    t = dict(raw["ttm"])
    t["fcf"] = t["operatingCashFlow"] - abs(t["capex"]) \
        if t.get("operatingCashFlow") is not None and t.get("capex") is not None else None
    t["revenueGrowth"] = raw["growth"] if raw["growth"] is not None else fy[-1].get("revenueGrowth")
    for k, n in (("grossMargin", "grossProfit"), ("operatingMargin", "operatingIncome"),
                 ("netMargin", "netIncome"), ("fcfMargin", "fcf")):
        t[k] = ratio(t.get(n), t.get("revenue"))

    bal_dates = sorted(d for d, b in raw["bal"].items() if b.get("totalAssets") or b.get("cash") is not None)
    latest = derive_balance(raw["bal"][bal_dates[-1]]) if bal_dates else {}
    if latest:
        latest["asOf"] = bal_dates[-1]
        ago = [d for d in bal_dates if 330 <= _days(d, bal_dates[-1]) <= 400]
        if ago:
            a = raw["bal"][ago[-1]]
            latest["totalDebtYearAgo"] = a.get("totalDebt")
            latest["equityYearAgo"] = a.get("equity")
    shares = raw.get("shares") or fy[-1].get("dilutedShares")
    bvps_g = fy[-1].get("bvpsGrowth")
    if latest.get("equity") and latest.get("equityYearAgo") and latest["equityYearAgo"] > 0:
        bvps_g = pct(latest["equity"], latest["equityYearAgo"])  # equity trend over the latest 12 months
        if fy[-1].get("shareChange"):
            bvps_g = round(((1 + bvps_g / 100) / (1 + fy[-1]["shareChange"] / 100) - 1) * 100, 1)
    latest["bvpsGrowth"] = bvps_g

    snap = {**t, "shareChange": fy[-1].get("shareChange")}
    out = {"source": raw["source"], "currency": raw["currency"], "fy": fy, "ttm": t, "balance": latest,
           "quarters": raw["quarters"], "valuation": {"sharesOutstanding": shares}}
    out["grade"] = grade(snap, latest, bvps_g)
    out["grade"]["asOf"] = t.get("asOf")

    v = out["valuation"]
    if price and shares and raw["currency"] == price.get("currency", "USD"):
        mc = price["last"] * shares
        v["marketCap"] = mc
        if t.get("revenue"):
            v["priceToSales"] = round(mc / t["revenue"], 1)
            if latest.get("netCash") is not None:
                v["evToSales"] = round((mc - latest["netCash"]) / t["revenue"], 1)
        if t.get("fcf") and t["fcf"] > 0:
            v["priceToFcf"] = round(mc / t["fcf"], 1)
        if t.get("netIncome") and t["netIncome"] > 0:
            v["pe"] = round(mc / t["netIncome"], 1)
        if latest.get("equity") and latest["equity"] > 0:
            v["priceToBook"] = round(mc / latest["equity"], 1)
    flags(out)
    return out


def flags(f):
    fy, t, b = f["fy"], f["ttm"], f["balance"]
    bad, good = [], []
    last = fy[-1]
    g = t.get("revenueGrowth")
    if g is not None:
        if g < 0:
            bad.append(f"Revenue shrinking ({g:+.0f}% vs a year earlier)")
        elif g >= 20:
            good.append(f"Revenue growing {g:.0f}% vs a year earlier")
    if t.get("fcf") is not None:
        (bad if t["fcf"] < 0 else good).append(
            "Negative free cash flow (last 12 months)" if t["fcf"] < 0 else "Positive free cash flow (last 12 months)")
    if t.get("netIncome") is not None and t["netIncome"] < 0:
        bad.append("Net loss (last 12 months)")
    sc = last.get("shareChange")
    if sc is not None:
        if sc > 5:
            bad.append(f"Share count up {sc:.0f}% last year — dilution")
        elif sc < -1:
            good.append(f"Share count down {abs(sc):.0f}% last year — buybacks")
    if len(fy) >= 2 and None not in (fy[-1].get("operatingMargin"), fy[-2].get("operatingMargin")):
        dm = fy[-1]["operatingMargin"] - fy[-2]["operatingMargin"]
        if dm <= -3:
            bad.append(f"Operating margin fell {abs(dm):.0f} points last year")
        elif dm >= 3:
            good.append(f"Operating margin up {dm:.0f} points last year")
    if b:
        if b.get("netCash") is not None:
            if b["netCash"] > 0:
                good.append("More cash than debt")
            elif b.get("totalDebtYearAgo") and b.get("totalDebt") and b["totalDebt"] > b["totalDebtYearAgo"] * 1.25:
                bad.append("Debt exceeds cash and rose 25%+ in a year")
        if b.get("currentRatio") is not None and b["currentRatio"] < 1:
            bad.append(f"Current ratio {b['currentRatio']} — short-term bills exceed short-term assets")
        if b.get("equity") is not None and b["equity"] < 0:
            bad.append("Negative shareholders' equity — liabilities exceed assets")
        if b.get("liabToAssets") is not None and b["liabToAssets"] > 80 and b.get("currentRatio") is not None:
            bad.append(f"Liabilities are {b['liabToAssets']:.0f}% of assets")
        if b.get("intangibleShare") is not None and b["intangibleShare"] > 40:
            bad.append(f"{b['intangibleShare']:.0f}% of assets are goodwill/intangibles")
        if b.get("bvpsGrowth") is not None and b["bvpsGrowth"] >= 10:
            good.append(f"Equity per share up {b['bvpsGrowth']:.0f}% in a year")
    if t.get("sbc") and t.get("revenue") and t["sbc"] / t["revenue"] > 0.15:
        bad.append(f"Stock comp is {t['sbc'] / t['revenue'] * 100:.0f}% of revenue")
    f["flags"], f["strengths"] = bad, good


# ================================================================ CAN SLIM

def chart_pattern(p):
    """Classify the last ~6 months: sideways / sideways-slightly-up (good) vs straight up / straight down."""
    if not p or p.get("ret6m") is None or not p.get("ma200"):
        return None, None, "Not enough price history."
    r6, d200 = p["ret6m"], (p["last"] / p["ma200"] - 1) * 100
    desc = f"6-month change {r6:+.0f}%, price {d200:+.0f}% vs its 200-day average."
    if r6 >= 40 or d200 >= 30:
        return "Straight up", False, desc + " Extended: buying here chases a run."
    if r6 <= -20 or d200 <= -15:
        return "Straight down", False, desc + " Falling: wait for it to stop and base."
    if -10 <= r6 <= 25 and -10 <= d200 <= 20:
        return ("Sideways, slightly up" if r6 > 5 else "Sideways"), True, desc
    return ("Rising fast" if r6 > 0 else "Drifting down"), False, desc


def my_rules(price, f):
    """Max's own criteria (Trading/Investing project rules)."""
    R = {}
    v, b, t = (f or {}).get("valuation", {}), (f or {}).get("balance", {}), (f or {}).get("ttm", {})
    fy = (f or {}).get("fy", [])
    ps = v.get("priceToSales")
    R["sales"] = {"name": "Under 2x sales", "pass": None if ps is None else ps < 2, "value": ps,
                  "why": f"Price/sales is {ps}." if ps is not None else "No sales data."}
    nc = b.get("netCash")
    R["cash"] = {"name": "More cash than debt", "pass": None if nc is None else nc > 0, "value": nc,
                 "why": (f"Net cash ${nc / 1e9:.2f}B." if abs(nc) >= 1e9 else f"Net cash ${nc / 1e6:.0f}M.").replace("$-", "−$")
                 if nc is not None else "No balance sheet data."}
    g = t.get("revenueGrowth")
    R["revenue"] = {"name": "Revenue growing", "pass": None if g is None else g > 0, "value": g,
                    "why": f"Revenue {g:+.0f}% vs a year earlier." if g is not None else "No revenue data."}
    q = [x for x in (f or {}).get("quarters", []) if x.get("eps") is not None]
    eg_q = None
    if len(q) >= 5:
        ya = next((x for x in reversed(q[:-1]) if 355 <= _days(x["end"], q[-1]["end"]) <= 375), None)
        if ya:
            eg_q = (q[-1]["eps"], ya["eps"])
    eg_a = fy[-1].get("epsGrowth") if fy else None
    if eg_q:
        cur, prev = eg_q
        ok = cur > prev
        why = f"Latest quarter EPS {cur:.2f} vs {prev:.2f} a year ago."
    elif fy and fy[-1].get("eps") is not None and len(fy) > 1 and fy[-2].get("eps") is not None:
        ok = fy[-1]["eps"] > fy[-2]["eps"]
        why = f"Yearly EPS {fy[-1]['eps']:.2f} vs {fy[-2]['eps']:.2f} the year before."
    else:
        ok, why = None, "No earnings data."
    R["earnings"] = {"name": "Earnings growing", "pass": ok, "value": eg_a, "why": why}
    label, ok, why = chart_pattern(price)
    R["chart"] = {"name": "Chart: sideways or slightly up", "pass": ok, "value": label,
                  "why": (label + ". " if label else "") + why}
    return R


def canslim(price, f, inst, market, spy):
    L = {}

    def put(k, name, ok, value, why):
        L[k] = {"name": name, "pass": ok, "value": value, "why": why}

    q = [x for x in (f or {}).get("quarters", []) if x.get("eps") is not None]
    if len(q) >= 5:
        cur, ya = q[-1], next((x for x in reversed(q[:-1]) if 355 <= _days(x["end"], q[-1]["end"]) <= 375), None)
        if ya:
            e0, e1 = ya["eps"], cur["eps"]
            g = pct(e1, e0)
            sg = pct(cur.get("revenue"), ya.get("revenue"))
            if e1 <= 0:
                put("C", "Current quarterly earnings", False, e1, f"Lost money last quarter (EPS {e1:.2f}).")
            elif e0 <= 0:
                put("C", "Current quarterly earnings", True, None,
                    f"Turned profitable: EPS {e1:.2f} vs {e0:.2f} a year ago." + (f" Sales {sg:+.0f}%." if sg is not None else ""))
            else:
                put("C", "Current quarterly earnings", g >= 25, g,
                    f"Quarterly EPS {g:+.0f}% vs a year ago (needs +25%)." + (f" Sales {sg:+.0f}%." if sg is not None else ""))
    if "C" not in L:
        put("C", "Current quarterly earnings", None, None, "Not enough quarterly data.")

    fy = (f or {}).get("fy", [])
    eg = [x["epsGrowth"] for x in fy[-3:] if x.get("epsGrowth") is not None]
    roe = fy[-1].get("roe") if fy else None
    if len(eg) >= 2:
        ok = all(x >= 25 for x in eg) and (roe is None or roe >= 17)
        put("A", "Annual earnings growth", ok, min(eg),
            f"Yearly EPS growth: {', '.join(f'{x:+.0f}%' for x in eg)} (needs +25% each)."
            + (f" ROE {roe:.0f}% (needs 17%)." if roe is not None else ""))
    else:
        put("A", "Annual earnings growth", False if fy and (fy[-1].get("eps") or 0) <= 0 else None, None,
            "Not consistently profitable yet." if fy and (fy[-1].get("eps") or 0) <= 0 else "Not enough yearly data.")

    if price:
        off = round((1 - price["last"] / price["hi52"]) * 100, 1) if price.get("hi52") else None
        put("N", "New highs", off is not None and off <= 15, off,
            f"{off:.0f}% below its 52-week high (look for within ~15%). The 'new product or leadership' part is your call."
            if off is not None else "No price data.")
        sc = fy[-1].get("shareChange") if fy else None
        ud = price.get("upDownVol50")
        bits = []
        if ud is not None:
            bits.append(f"up-day volume is {ud:.2f}x down-day volume over 50 days")
        if sc is not None:
            bits.append(f"shares {sc:+.1f}% last year")
        put("S", "Supply and demand", (ud is None or ud >= 1) and (sc is None or sc <= 3) if bits else None, ud,
            (bits[0][0].upper() + "; ".join(bits)[1:] + ".") if bits else "No data.")
        rs, spy_rs = rs_weighted(price), rs_weighted(spy)
        if rs is not None and spy_rs is not None:
            put("L", "Leader (relative strength)", rs - spy_rs >= 10, round(rs - spy_rs, 1),
                f"Weighted 12-month return {rs:+.0f}% vs S&P 500 {spy_rs:+.0f}% (look for 10+ points ahead).")
        else:
            put("L", "Leader (relative strength)", None, None, "Not enough price history.")
    if inst and inst.get("pct") is not None:
        chg = inst.get("change")
        txt = f"{inst['pct']:.0f}% held by {inst.get('count') or '?'} institutions."
        txt += f" {chg:+.1f} pts since {inst['since']}." if chg is not None else " Change builds up as daily data accumulates; check Finviz 'Inst Trans' meanwhile."
        ok = inst["pct"] >= 30 and (chg is None or chg >= 0)
        if inst["pct"] > 95:
            txt += " Very crowded: little room for new buyers."
        put("I", "Institutional sponsorship", ok, inst["pct"], txt)
    else:
        put("I", "Institutional sponsorship", None, None, "No ownership data.")
    if market:
        put("M", "Market direction", market["state"] == "uptrend", None, market["why"])
    return L


def market_direction(spy):
    if not spy or spy.get("ma200") is None:
        return None
    p, m50, m200 = spy["last"], spy["ma50"], spy["ma200"]
    if p > m50 > m200:
        state, why = "uptrend", "S&P 500 is above its 50- and 200-day averages: uptrend."
    elif p < m200:
        state, why = "downtrend", "S&P 500 is below its 200-day average: downtrend. O'Neil avoids new buys here."
    else:
        state, why = "pressure", "S&P 500 is mixed around its averages: uptrend under pressure."
    return {"state": state, "why": why, "spy": p, "ma50": m50, "ma200": m200}


# ================================================================ institutions

def institutions(sym, prev):
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(sym)}"
           f"?modules=majorHoldersBreakdown")
    r = YH.json(url)["quoteSummary"]["result"][0]["majorHoldersBreakdown"]
    pct_held = (r.get("institutionsPercentHeld") or {}).get("raw")
    count = (r.get("institutionsCount") or {}).get("raw")
    if pct_held is None:
        return prev
    hist = list((prev or {}).get("history", []))
    today = date.today().isoformat()
    if not hist or hist[-1]["pct"] != round(pct_held * 100, 2):
        hist.append({"date": today, "pct": round(pct_held * 100, 2), "count": count})
    hist = hist[-24:]
    out = {"pct": round(pct_held * 100, 2), "count": count, "history": hist, "asOf": today}
    base = [h for h in hist if _days(h["date"], today) >= 80]
    if base:
        out["change"] = round(out["pct"] - base[-1]["pct"], 2)
        out["since"] = base[-1]["date"]
    return out


# ================================================================ news

def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_rss(xml_bytes, default_source):
    items = []
    for it in ET.fromstring(xml_bytes).iter("item"):
        title, link = _clean(it.findtext("title")), (it.findtext("link") or "").strip()
        src_el = it.find("source")
        source = _clean(src_el.text) if src_el is not None and src_el.text else default_source
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).isoformat()
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
        items += parse_rss(get("https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q + " when:14d", "hl": "en-US", "gl": "US", "ceid": "US:en"})), "Google News")
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


# ================================================================ main

def main():
    with open(TICKERS_FILE) as fh:
        tickers = [t["symbol"].upper() for t in json.load(fh)["tickers"]]
    try:
        with open(OUT_FILE) as fh:
            old = json.load(fh).get("tickers", {})
    except Exception:  # noqa: BLE001
        old = {}

    try:
        tmap, sec_err = timed("SEC ticker list", sec_ticker_map, limit=20)
    except Exception as e:  # noqa: BLE001
        tmap, sec_err = {}, str(e)
    sec_ok = bool(tmap)
    if sec_err:
        print("SEC unavailable, using Yahoo fundamentals:", sec_err, file=sys.stderr)
    try:
        spy, _ = timed("SPY price", fetch_price, "SPY", limit=30)
    except Exception:  # noqa: BLE001
        spy = None
    market = market_direction(spy)
    meta = {"sec": "ok" if sec_ok else sec_err, "market": market,
            "spy": {k: spy.get(k) for k in ("last", "ret3m", "ret6m", "ret9m", "ret12m")} if spy else None,
            "weights": WEIGHTS}

    out = {}
    for sym in tickers:
        prev = old.get(sym, {})
        rec = {"symbol": sym, "errors": {}}
        cik, name = tmap.get(sym) or (prev.get("cik"), prev.get("name"))
        try:
            price, err = timed(f"{sym} price", fetch_price, sym, limit=30)
        except Exception as e:  # noqa: BLE001
            price, err = None, str(e)
        qtype = prev.get("quoteType")
        if price:
            name = name or price.pop("_name", None)
            qtype = price.pop("_type", None) or qtype
            price.pop("_name", None)
        if FMP_KEY and (not name or not qtype):
            try:
                prof = (fmp("profile", symbol=sym) or [{}])[0]
                name = name or prof.get("companyName")
                qtype = qtype or ("ETF" if prof.get("isEtf") else "MUTUALFUND" if prof.get("isFund") else "EQUITY")
            except Exception as e:  # noqa: BLE001
                print(f"{sym} FMP profile failed: {e}", file=sys.stderr)
        rec.update({"cik": cik, "name": name, "quoteType": qtype})
        rec["price"] = price or prev.get("price")
        if err and not price:
            rec["errors"]["price"] = err

        raw, ferr = None, []
        if qtype in ("ETF", "MUTUALFUND", "INDEX"):
            ferr.append("Fund/ETF: no company financials to grade")
        else:
            if sec_ok and cik:
                try:
                    raw = timed(f"{sym} SEC", lambda: from_sec(sec_facts(cik)), limit=30)
                except Exception as e:  # noqa: BLE001
                    ferr.append(f"SEC: {e}")
                time.sleep(0.25)
            if raw is None and FMP_KEY:
                try:
                    raw = timed(f"{sym} FMP", from_fmp, sym, limit=40)
                    if raw is None:
                        ferr.append("FMP: no statements for this symbol")
                except Exception as e:  # noqa: BLE001
                    ferr.append(f"FMP: {e}")
            if raw is None and FINNHUB_KEY:
                try:
                    raw = timed(f"{sym} Finnhub", from_finnhub, sym, limit=40)
                    if raw is None:
                        ferr.append("Finnhub: no filings for this symbol")
                except Exception as e:  # noqa: BLE001
                    ferr.append(f"Finnhub: {e}")
            if raw is None:
                try:
                    raw = timed(f"{sym} Yahoo fundamentals", from_yahoo, sym, limit=30)
                    if raw is None:
                        ferr.append("Yahoo: no financial statements returned")
                except Exception as e:  # noqa: BLE001
                    ferr.append(f"Yahoo: {e}")
        if raw:
            try:
                rec["fundamentals"] = analyze(raw, rec["price"])
            except Exception as e:  # noqa: BLE001
                rec["fundamentals"] = prev.get("fundamentals")
                rec["errors"]["fundamentals"] = f"Analysis failed: {type(e).__name__}: {e}"
        else:
            rec["fundamentals"] = None if qtype in ("ETF", "MUTUALFUND") else prev.get("fundamentals")
            rec["errors"]["fundamentals"] = "; ".join(ferr)

        try:
            rec["institutions"] = timed(f"{sym} institutions", institutions, sym, prev.get("institutions"), limit=20)
        except Exception as e:  # noqa: BLE001
            rec["institutions"] = prev.get("institutions")
            rec["errors"]["institutions"] = str(e)

        rec["rules"] = my_rules(rec["price"], rec.get("fundamentals")) \
            if qtype not in ("ETF", "MUTUALFUND") else None
        rec["canslim"] = canslim(rec["price"], rec.get("fundamentals"), rec.get("institutions"), market, spy) \
            if qtype not in ("ETF", "MUTUALFUND") else None

        try:
            news, err = timed(f"{sym} news", fetch_news, sym, name, limit=30)
        except Exception as e:  # noqa: BLE001
            news, err = [], str(e)
        rec["news"] = news or prev.get("news", [])
        if err:
            rec["errors"]["news"] = err
        if not rec["errors"]:
            del rec["errors"]
        out[sym] = rec
        f = rec.get("fundamentals") or {}
        print(f"{sym}: price={'ok' if rec['price'] else 'none'} fund={f.get('source', 'none')} "
              f"grade={(f.get('grade') or {}).get('letter')} news={len(rec['news'])} "
              f"errors={rec.get('errors', {})}")

    with open(OUT_FILE, "w") as fh:
        meta["timings"] = TIMINGS
        meta["skippedHosts"] = [h for h, n in HOST_FAILS.items() if n >= 2]
        meta["slowSources"] = sorted(SLOW)
        json.dump({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "meta": meta,
                   "tickers": out}, fh, separators=(",", ":"))


if __name__ == "__main__":
    main()

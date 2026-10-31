#!/usr/bin/env python3
"""Diagnostic: show which SEC concept names a company uses for key line items, with latest annual values.
Usage (on the Mac):  python3 scripts/inspect_sec.py SOFI NBIS
"""
import json, os, re, sys, urllib.request, gzip

UA = os.environ.get("SEC_USER_AGENT") or "Stock Journal research"
PATTERNS = {
    "net income": r"NetIncome|ProfitLoss",
    "debt": r"Debt|Borrowing|NotesPayable|Warehouse|Securitization",
    "revenue": r"^Revenue|InterestAndDividendIncome|NoninterestIncome",
}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        b = r.read()
        return json.loads(gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b)


tick = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in get("https://www.sec.gov/files/company_tickers.json").values()}
for sym in [a.upper() for a in sys.argv[1:] if not a.startswith("--")]:
    facts = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{tick[sym]}.json")["facts"]
    print(f"\n===== {sym}  taxonomies: {', '.join(facts)}")
    for label, pat in PATTERNS.items():
        print(f"-- {label}")
        for tax, concepts in facts.items():
            for name, node in concepts.items():
                if not re.search(pat, name):
                    continue
                for unit, rows in node.get("units", {}).items():
                    ann = [r for r in rows if r.get("form") in ("10-K", "20-F", "40-F")]
                    if not ann:
                        continue
                    last = max(ann, key=lambda r: (r["end"], r.get("filed", "")))
                    if last["end"] < "2024-06-01":
                        continue
                    print(f"   {tax}:{name}  [{unit}]  {last['end']}  {last['val']:,}")


# --parse: run the app's own parser and show what it extracts
if "--parse" in sys.argv:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fetch_data as fd
    for sym in [a.upper() for a in sys.argv[1:] if not a.startswith("--")]:
        facts = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{tick[sym]}.json")["facts"]
        node = facts.get("us-gaap", {}).get("NetIncomeLoss", {}).get("units", {}).get("USD", [])
        fy = [r for r in node if r.get("end", "").startswith("2025-12") or r.get("end", "").startswith("2025-09")]
        print(f"\n##### {sym} raw NetIncomeLoss rows near FY2025:")
        for r in fy[-8:]:
            print("   ", {k: r.get(k) for k in ("start", "end", "val", "form", "fp", "fy", "filed", "frame")})
        raw = fd.from_sec(facts)
        print(f"##### {sym} parsed fiscal years:")
        for row in raw["fy"]:
            print(f"   {row['end']}  revenue={row.get('revenue')}  netIncome={row.get('netIncome')}  debt={row.get('totalDebt')}")
        print("   ttm:", {k: raw["ttm"].get(k) for k in ("asOf", "revenue", "netIncome")})

# Stock Journal

A home-screen app for researching and tracking stock picks:

- **Thesis journal** per stock: why you own it, what has to go right, what would prove you wrong, buy/trim/sell rules, conviction, review date. Every decision is logged with the price at the time.
- **Health grade** (0–100, A–F): growth, profitability, cash generation, balance sheet (liabilities ÷ assets, current ratio, net cash, equity-per-share trend, goodwill share), and share dilution. Weights adjustable in Settings (default 20/20/20/25/15). Graded for each fiscal year.
- **CAN SLIM**: pass/fail for each letter, with institutional ownership from Yahoo and market direction from the S&P 500.
- **Grade vs. price**: 5 years of price with each year's grade marked, and what the stock did in the 12 months after.
- **Learn mode**: every number has a "?" with a plain-English explainer using the company's own figures, plus a step-by-step walkthrough.
- **Portfolio**: positions, weights, gains. Import Fidelity's Positions CSV.
- **News**: recent headlines per stock.

## How it works

- `scripts/fetch_data.py` runs on GitHub Actions each weekday after the close (`.github/workflows/update-data.yml`). It pulls prices (Yahoo Finance, Stooq fallback), financials (SEC EDGAR when reachable, then Financial Modeling Prep if `FMP_API_KEY` is set as a repo secret, then Yahoo Finance), and news (Yahoo Finance + Google News RSS) for every ticker in `data/tickers.json`, and commits `data/market.json`.
- The app (`index.html`, `app.js`, `learn.js`) is static and served by GitHub Pages.
- Your journal and positions are saved on your phone. With a token, they sync to the private `Stock-Journal-data` repo as `journal.json`, so your notes never touch this public repo.

## Setup (one time)

1. **Turn on Pages:** Settings → Pages → Source: *Deploy from a branch* → `main` / `(root)` → Save. The app will be at `https://max-stax.github.io/Stock-Journal/`.
2. **First data run:** Actions → *Update market data* → *Run workflow*. Takes a few minutes.
3. **Install on your phone:** open the Pages URL in Safari → Share → *Add to Home Screen*.
4. **Sync token (recommended):** GitHub → Settings → Developer settings → Fine-grained tokens → Generate. Repository access: only `Stock-Journal` and `Stock-Journal-data`. Permissions: *Contents: Read and write*. Paste it in the app under Settings.

## Adding stocks

Tap **+ Add a stock** in the app (with a token set, it updates `data/tickers.json` and the data job runs automatically), or edit `data/tickers.json` directly.

## Limits

- ETFs and funds (e.g. DRAM) have no SEC financials, so no grade.
- Foreign companies that file in IFRS are supported where their filings include structured data; some won't.
- The grade describes the business, not whether the stock is a good buy at today's price.

#!/usr/bin/env python3
"""
Short-Squeeze-US access probe (GitHub Actions).
Checks every planned free source and stores status + a small sample in probe/result.json.
Secret: SEC_USER_AGENT (same value as the other SEC sites).
"""
import datetime as dt, ftplib, io, json, os, re, zipfile
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "probe" / "result.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "SJ Market Desk research@example.com"
R = {"run_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds"), "checks": []}
TODAY = dt.date.today()


def rec(name, ok, **kw):
    kw.update({"name": name, "ok": ok}); R["checks"].append(kw)
    print(("OK   " if ok else "FAIL ") + name, {k: str(v)[:100] for k, v in kw.items() if k not in ("name", "ok", "sample")})


def get(url, headers=None, timeout=60):
    try:
        return requests.get(url, headers=headers or {"User-Agent": UA}, timeout=timeout)
    except Exception as e:
        class X: status_code = None; content = b""; text = f"error {e}"; headers = {}
        return X()


def bdays(n):
    d, out = TODAY, []
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


# 1) FINRA daily short sale volume (Reg SHO)
for d in bdays(4):
    u = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt"
    r = get(u)
    ok = r.status_code == 200 and "ShortVolume" in r.text[:300]
    rec("finra_regsho_daily", ok, url=u, status=r.status_code, lines=r.text.count("\n") if ok else 0, sample=r.text[:500])
    if ok:
        break

# 2) FINRA short interest (bi-monthly)
r = requests.post("https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
                  json={"limit": 5, "sortFields": ["-settlementDate"]}, headers={"Accept": "application/json", "User-Agent": UA}, timeout=60) \
    if True else None
rec("finra_api_short_interest_post", r.status_code == 200, status=r.status_code, sample=r.text[:900])
r = get("https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest?limit=5", headers={"Accept": "application/json", "User-Agent": UA})
rec("finra_api_short_interest_get", r.status_code == 200, status=r.status_code, sample=r.text[:900])
for d in (dt.date(2026, 10, 15), dt.date(2026, 9, 30), dt.date(2026, 9, 15)):
    for u in (f"https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d:%Y%m%d}.csv",
              f"https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d:%Y%m%d}.txt"):
        r = get(u)
        rec("finra_cdn_short_interest", r.status_code == 200 and len(r.content) > 1000, url=u, status=r.status_code, bytes=len(r.content), sample=r.text[:400] if r.status_code == 200 else "")

# 3) Nasdaq per-symbol short interest (fallback)
r = get("https://api.nasdaq.com/api/quote/GME/short-interest?assetClass=stocks",
        headers={"User-Agent": UA, "Accept": "application/json", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
rec("nasdaq_short_interest_GME", r.status_code == 200 and "shortInterest" in r.text, status=r.status_code, sample=r.text[:900])

# 4) Interactive Brokers stock-loan availability / fee (public FTP, user 'shortstock')
try:
    ftp = ftplib.FTP("ftp3.interactivebrokers.com", timeout=60)
    ftp.login("shortstock", "")
    names = ftp.nlst()
    buf = io.BytesIO()
    ftp.retrbinary("RETR usa.txt", buf.write)
    ftp.quit()
    txt = buf.getvalue().decode("utf-8", "replace")
    rec("ibkr_ftp_usa", "SYM" in txt[:500].upper() or "#BOF" in txt[:200], files=names[:30], lines=txt.count("\n"), sample=txt[:900])
except Exception as e:
    rec("ibkr_ftp_usa", False, error=str(e)[:200])

# 5) SEC fails-to-deliver (half-month zip)
sec = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip"}
for ym in (f"{TODAY:%Y%m}", f"{(TODAY.replace(day=1) - dt.timedelta(days=1)):%Y%m}"):
    for half in ("a", "b"):
        u = f"https://www.sec.gov/files/data/fails-deliver-data/cnsfails{ym}{half}.zip"
        r = get(u, headers=sec)
        sample = ""
        if r.status_code == 200:
            try:
                z = zipfile.ZipFile(io.BytesIO(r.content)); sample = z.read(z.namelist()[0])[:400].decode("latin-1")
            except Exception as e:
                sample = f"zip error {e}"
        rec("sec_ftd", r.status_code == 200, url=u, status=r.status_code, bytes=len(r.content), sample=sample)

# 6) Shares outstanding for all filers (SEC XBRL frames)
for q in ("CY2026Q3I", "CY2026Q2I"):
    u = f"https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{q}.json"
    r = get(u, headers=sec)
    n = len(r.json().get("data", [])) if r.status_code == 200 else 0
    rec("sec_frames_shares_out", n > 1000, url=u, status=r.status_code, companies=n, sample=r.text[:300])
r = get("https://www.sec.gov/files/company_tickers.json", headers=sec)
rec("sec_company_tickers", r.status_code == 200, status=r.status_code, bytes=len(r.content))

# 7) Exchange-listed universe (Nasdaq Trader symbol directory)
for u in ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"):
    r = get(u)
    rec("symdir", r.status_code == 200 and "|" in r.text[:200], url=u, status=r.status_code, lines=r.text.count("\n"), sample=r.text[:200])

# 8) Prices (Yahoo chart) and Reddit mentions (ApeWisdom, incl. short-squeeze community)
r = get("https://query1.finance.yahoo.com/v8/finance/chart/GME?range=3mo&interval=1d")
rec("yahoo_chart", r.status_code == 200 and "timestamp" in r.text, status=r.status_code)
for f in ("all-stocks", "Shortsqueeze", "shortsqueeze", "SqueezePlays"):
    u = f"https://apewisdom.io/api/v1.0/filter/{f}/page/1"
    r = get(u)
    rec(f"apewisdom_{f}", r.status_code == 200 and '"results"' in r.text, url=u, status=r.status_code, sample=r.text[:300])
r = get("https://apewisdom.io/api/v1.0/filters")
rec("apewisdom_filters", r.status_code == 200, status=r.status_code, sample=r.text[:1500])

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")
print("wrote", OUT)

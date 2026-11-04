#!/usr/bin/env python3
"""
Short-Squeeze-US daily build.

Sources (verified by probe 2026-11-04)
  FINRA short interest      cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv   (twice a month)
  FINRA Reg SHO daily       cdn.finra.org/equity/regsho/daily/CNMSshvolYYYYMMDD.txt     (daily short SALE volume)
  SEC XBRL frames           dei:EntityCommonStockSharesOutstanding                       (shares outstanding)
  Nasdaq Trader symbol dir  nasdaqlisted.txt / otherlisted.txt                           (exchange-listed, ETF flag)
  Yahoo chart               price / volume (threaded, candidates only)
  ApeWisdom                 Reddit mentions (all-stocks + Shortsqueeze + SqueezePlays)
  iBorrowDesk (optional)    borrow fee / available shares, top candidates only           (skipped if unreachable)

Output: data/squeeze.json
"""
import concurrent.futures as cf
import datetime as dt
import io
import json
import math
import os
import re
import statistics
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip()
TODAY = dt.date.today()
DIAG = []
SI_PERIODS = 8            # keep ~4 months of short-interest history
REGSHO_DAYS = 20
MIN_MCAP = 50e6           # build universe floor (UI filters can raise it)
MIN_PRICE = 1.0


def log(*a):
    print(*a, flush=True)


def diag(src, msg):
    DIAG.append({"src": src, "msg": str(msg)[:300]})
    log("DIAG", src, msg)


def get(url, headers=None, timeout=60, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers or {"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 404):
                return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ================================================================== universe
def universe():
    out = {}
    for url, sym_i, etf_key, test_key in (("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", 0, "ETF", "Test Issue"),
                                          ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", 0, "ETF", "Test Issue")):
        r = get(url)
        if not r:
            diag("symdir", f"failed {url}")
            continue
        lines = r.text.splitlines()
        head = lines[0].split("|")
        for ln in lines[1:]:
            p = ln.split("|")
            if len(p) != len(head):
                continue
            row = dict(zip(head, p))
            sym = row.get("Symbol") or row.get("ACT Symbol") or p[sym_i]
            name = row.get("Security Name", "")
            if row.get(etf_key) == "Y" or row.get(test_key) == "Y":
                continue
            if re.search(r"\b(Warrant|Warrants|Unit|Units|Right|Rights|Notes?|Preferred|Depositary Shares? Each|Debentures?)\b", name, re.I):
                continue
            exch = "NASDAQ" if "nasdaqlisted" in url else {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}.get(row.get("Exchange", ""), row.get("Exchange", ""))
            out[sym.replace(".", "-") if "$" not in sym else sym] = {"name": re.sub(r"\s+-\s+(Class|Common|Ordinary).*$", "", name).strip(), "exch": exch}
    log("universe", len(out))
    return out


# ================================================================== FINRA short interest (twice a month)
def candidate_settlement_dates(n_back_days=150):
    """FINRA settlement dates: mid-month (15th or prior business day) and last business day of the month."""
    ds = set()
    d = TODAY
    for _ in range(8):
        y, m = d.year, d.month
        mid = dt.date(y, m, 15)
        while mid.weekday() >= 5:
            mid -= dt.timedelta(days=1)
        nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
        end = nxt - dt.timedelta(days=1)
        while end.weekday() >= 5:
            end -= dt.timedelta(days=1)
        for x in (mid, end, mid - dt.timedelta(days=1), end - dt.timedelta(days=1)):
            if x <= TODAY and (TODAY - x).days <= n_back_days:
                ds.add(x)
        d = dt.date(y, m, 1) - dt.timedelta(days=1)
    return sorted(ds, reverse=True)


def load_short_interest():
    periods = []
    for d in candidate_settlement_dates():
        if len(periods) >= SI_PERIODS:
            break
        r = get(f"https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d:%Y%m%d}.csv", tries=2)
        if not r:
            continue
        rows = {}
        lines = r.text.splitlines()
        head = [h.strip('"') for h in lines[0].split("|")]
        for ln in lines[1:]:
            p = [x.strip('"') for x in ln.split("|")]
            if len(p) < len(head):
                continue
            row = dict(zip(head, p))
            if row.get("marketClassCode") not in ("NYSE", "NNM", "NSC", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSE American", "IEX", "NCM", "NGS"):
                pass                                              # keep all; universe filter decides
            rows[row["symbolCode"].replace(".", "-")] = {"si": f(row.get("currentShortPositionQuantity")),
                                                          "adv": f(row.get("averageDailyVolumeQuantity")),
                                                          "dtc": f(row.get("daysToCoverQuantity")),
                                                          "name": row.get("issueName", "")}
        periods.append({"date": d.isoformat(), "rows": rows})
        log("short interest", d, len(rows))
    if not periods:
        diag("finra_si", "no short-interest file found")
    return periods


# ================================================================== FINRA Reg SHO daily short-sale volume
def load_regsho():
    days, d = [], TODAY + dt.timedelta(days=1)
    while len(days) < REGSHO_DAYS and (TODAY - d).days < 45:
        d -= dt.timedelta(days=1)
        if d.weekday() >= 5:
            continue
        r = get(f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt", tries=2)
        if not r:
            continue
        vols = {}
        for ln in r.text.splitlines()[1:]:
            p = ln.split("|")
            if len(p) >= 5 and p[1]:
                sv, tv = f(p[2]), f(p[4])
                if tv:
                    vols[p[1].replace(".", "-")] = (sv or 0, tv)
        days.append({"date": d.isoformat(), "v": vols})
    log("regsho days", len(days), days[0]["date"] if days else None)
    return days


# ================================================================== shares outstanding (SEC)
def shares_outstanding():
    if "@" not in SEC_UA:
        diag("sec", "SEC_USER_AGENT secret missing")
        return {}
    h = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip"}
    tick = get("https://www.sec.gov/files/company_tickers.json", headers=h)
    cik2t = {}
    if tick:
        for row in tick.json().values():
            cik2t.setdefault(int(row["cik_str"]), []).append(row["ticker"].upper().replace(".", "-"))
    out = {}
    q = (TODAY.month - 1) // 3 + 1
    frames = []
    y, qq = TODAY.year, q
    for _ in range(4):
        frames.append(f"CY{y}Q{qq}I")
        qq -= 1
        if qq == 0:
            y, qq = y - 1, 4
    for fr in reversed(frames):            # older first, newer overwrite
        r = get(f"https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{fr}.json", headers=h)
        if not r:
            continue
        for row in r.json().get("data", []):
            for t in cik2t.get(int(row["cik"]), []):
                out[t] = {"shares": row["val"], "asof": row.get("end", "")}
        time.sleep(0.2)
    log("shares outstanding", len(out))
    return out


# ================================================================== prices (Yahoo, threaded)
def yahoo(sym):
    r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3mo&interval=1d", tries=2, timeout=30)
    if not r:
        return sym, None
    try:
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        rows = [(c, v, hgh) for c, v, hgh in zip(q["close"], q["volume"], q["high"]) if c]
        if len(rows) < 21:
            return sym, None
        closes = [x[0] for x in rows]
        vols = [x[1] or 0 for x in rows]
        highs = [x[2] or x[0] for x in rows]
        last = closes[-1]
        hi20 = max(highs[-21:-1])
        v5 = sum(vols[-5:]) / 5
        v20 = sum(vols[-20:]) / 20
        return sym, {"price": last, "ret1d": last / closes[-2] - 1, "ret5d": last / closes[-6] - 1, "ret20d": last / closes[-21] - 1,
                     "hi20": hi20, "breakout": last >= hi20, "vol_ratio": (v5 / v20) if v20 else None,
                     "adv20": v20, "dollar_vol": v20 * last, "spark": [round(c, 4) for c in closes[-40:]]}
    except Exception:
        return sym, None


def prices(symbols):
    out = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for sym, res in ex.map(yahoo, symbols):
            if res:
                out[sym] = res
    log("prices", len(out), "of", len(symbols))
    return out


# ================================================================== Reddit (ApeWisdom)
def reddit():
    out = {}
    for flt, key in (("all-stocks", "all"), ("Shortsqueeze", "sq"), ("SqueezePlays", "sp")):
        for page in (1, 2):
            r = get(f"https://apewisdom.io/api/v1.0/filter/{flt}/page/{page}", tries=2)
            if not r:
                break
            for x in r.json().get("results", []):
                o = out.setdefault(x["ticker"].upper().replace(".", "-"), {})
                o[key] = {"m": x.get("mentions") or 0, "m24": x.get("mentions_24h_ago") or 0, "rank": x.get("rank")}
            time.sleep(0.5)
    log("reddit tickers", len(out))
    return out


# ================================================================== borrow (optional)
def borrow(sym):
    r = get(f"https://iborrowdesk.com/api/ticker/{sym}", tries=1, timeout=20)
    if not r:
        return sym, None
    try:
        js = r.json()
        d = js.get("daily") or js.get("real_time") or []
        if not d:
            return sym, None
        last = d[-1]
        prev = d[-6] if len(d) > 5 else d[0]
        return sym, {"fee": f(last.get("fee")), "avail": f(last.get("available")), "fee_prev": f(prev.get("fee")), "avail_prev": f(prev.get("available")),
                     "date": str(last.get("time") or last.get("date") or "")[:10]}
    except Exception:
        return sym, None


# ================================================================== scoring
def pct_rank(values):
    """value -> percentile 0..1 among non-null values."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return lambda v: 0
    n = len(xs)
    import bisect
    return lambda v: 0 if v is None else bisect.bisect_right(xs, v) / n


def main():
    uni = universe()
    si_periods = load_short_interest()
    if not si_periods:
        (DATA / "squeeze.json").write_text(json.dumps({"status": "error", "diag": DIAG}), encoding="utf-8")
        return
    latest = si_periods[0]["rows"]
    regsho = load_regsho()
    so = shares_outstanding()

    # candidates: exchange-listed, has short interest, some size
    cands = [s for s, r in latest.items() if s in uni and (r["si"] or 0) >= 200_000]
    log("candidates (listed, SI>=200k)", len(cands))
    px = prices(cands)
    rd = reddit()

    rows = []
    for s in cands:
        p = px.get(s)
        if not p or p["price"] < MIN_PRICE:
            continue
        sh = so.get(s, {}).get("shares")
        mcap = sh * p["price"] if sh else None
        if mcap is not None and mcap < MIN_MCAP:
            continue
        cur = latest[s]
        hist = [per["rows"].get(s, {}).get("si") for per in si_periods][::-1]          # oldest -> newest
        prev = hist[-2] if len(hist) > 1 else None
        consec = 0
        for a, b in zip(hist[::-1][1:], hist[::-1]):
            if a is not None and b is not None and b > a:
                consec += 1
            else:
                break
        sv = [d["v"].get(s) for d in regsho]
        ratios = [x[0] / x[1] for x in sv if x and x[1]]
        r1 = ratios[0] if ratios else None
        r5 = statistics.mean(ratios[:5]) if len(ratios) >= 3 else None
        r20 = statistics.mean(ratios) if len(ratios) >= 10 else None
        dtc = cur["si"] / p["adv20"] if p.get("adv20") else cur.get("dtc")
        red = rd.get(s, {})
        m_all = red.get("all", {})
        m_sq = (red.get("sq", {}).get("m") or 0) + (red.get("sp", {}).get("m") or 0)
        rows.append({
            "s": s, "n": uni[s]["name"] or cur["name"], "x": uni[s]["exch"], "p": round(p["price"], 4),
            "mcap": mcap, "sh": sh, "si": cur["si"], "si_prev": prev,
            "si_chg": (cur["si"] / prev - 1) if prev else None, "si_pct": (cur["si"] / sh) if sh else None,
            "si_val": cur["si"] * p["price"], "dtc": dtc, "dtc_f": cur.get("dtc"), "consec": consec,
            "si_hist": hist, "sv1": r1, "sv5": r5, "sv20": r20, "sv_series": [round(x, 3) for x in ratios[:20]][::-1],
            "r1": p["ret1d"], "r5": p["ret5d"], "r20": p["ret20d"], "bo": p["breakout"], "vr": p["vol_ratio"], "dv": p["dollar_vol"],
            "spark": p["spark"], "rd": m_all.get("m"), "rd24": m_all.get("m24"), "rdsq": m_sq,
        })
    log("rows", len(rows))

    # optional borrow data for the most shorted names
    top = sorted(rows, key=lambda r: -(r["si_pct"] or 0))[:150]
    bmap = {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for sym, b in ex.map(borrow, [r["s"] for r in top]):
            if b:
                bmap[sym] = b
    if not bmap:
        diag("borrow", "borrow fee source unavailable - score uses short interest, volume, price and Reddit only")
    for r in rows:
        r["b"] = bmap.get(r["s"])

    # ---- squeeze score (percentile based, 0-100)
    P = {k: pct_rank([r[k] for r in rows]) for k in ("si_pct", "dtc", "si_chg", "sv5", "vr", "r5")}
    fee_rank = pct_rank([r["b"]["fee"] for r in rows if r.get("b")])
    for r in rows:
        comp = {
            "si": 30 * P["si_pct"](r["si_pct"]),
            "dtc": 15 * P["dtc"](r["dtc"]),
            "trend": 10 * (0.6 * P["si_chg"](r["si_chg"]) + 0.4 * min(r["consec"], 4) / 4),
            "flow": 5 * P["sv5"](r["sv5"]),
            "ignite": 25 * (0.4 * (1 if r["bo"] else 0) + 0.3 * P["vr"](r["vr"]) + 0.3 * P["r5"](r["r5"])),
            "reddit": 5 * min(1, ((r["rd"] or 0) + 2 * (r["rdsq"] or 0)) / 50),
            "borrow": 10 * fee_rank(r["b"]["fee"]) if r.get("b") and r["b"].get("fee") is not None else 0,
        }
        if not bmap:                                     # no borrow data at all: rescale the other 90 points to 100
            comp = {k: v / 0.9 for k, v in comp.items()}
        r["score"] = round(sum(comp.values()), 1)
        r["comp"] = {k: round(v, 1) for k, v in comp.items()}
        badges = []
        if (r["si_pct"] or 0) >= 0.15 and r["bo"] and (r["vr"] or 0) >= 2:
            badges.append("ignite")
        if r["si_chg"] is not None and r["si_chg"] <= -0.15 and (r["r20"] or 0) >= 0.2:
            badges.append("covering")
        if r["consec"] >= 3:
            badges.append("building")
        if (r["rd"] or 0) >= 10 and (r["rd"] or 0) >= 3 * max(1, r["rd24"] or 0) or (r["rdsq"] or 0) >= 5:
            badges.append("reddit")
        b = r.get("b")
        if b and ((b.get("avail") is not None and b["avail"] <= 1000) or (b.get("fee") and b.get("fee_prev") and b["fee"] >= 1.5 * b["fee_prev"] and b["fee"] >= 10)):
            badges.append("borrow")
        r["badges"] = badges

    rows.sort(key=lambda r: -r["score"])
    res = {"status": "ok", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "si_date": si_periods[0]["date"], "si_dates": [p["date"] for p in si_periods][::-1],
           "regsho_date": regsho[0]["date"] if regsho else None, "borrow": bool(bmap),
           "count": len(rows), "rows": rows, "diag": DIAG}
    DATA.mkdir(parents=True, exist_ok=True)

    def rnd(o):
        if isinstance(o, float):
            return None if math.isnan(o) else (round(o, 4) if abs(o) < 1000 else round(o))
        if isinstance(o, dict):
            return {k: rnd(v) for k, v in o.items()}
        if isinstance(o, list):
            return [rnd(v) for v in o]
        return o
    (DATA / "squeeze.json").write_text(json.dumps(rnd(res), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log("wrote data/squeeze.json", len(rows), "rows", len(DIAG), "diag")


if __name__ == "__main__":
    main()

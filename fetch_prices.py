"""抓農業部「農產品交易行情」（批發市場每日行情）到 SQLite。

用法（純標準庫，不需安裝套件）：
    py -3.14 -I fetch_prices.py                 抓最近 30 天
    py -3.14 -I fetch_prices.py --days 400      抓最近 400 天（跨年才能比去年同期；約 30 到 60 分鐘）
    py -3.14 -I fetch_prices.py --from 2026-01-01 --to 2026-03-31
    py -3.14 -I fetch_prices.py --db data/foodsec.db

資料源：https://data.moa.gov.tw/api/v1/AgriProductsTransType/  免 API key。
限制：非會員每次請求只回第一頁 1000 筆，所以改成「每個市場 × CHUNK_DAYS 天」一個請求；
      某段仍滿 1000 筆就自動拆成單日重抓，單日還滿就記 WARN。
市場清單在 markets.txt（一行一個，對應 API 的 MarketName；不存在的市場回 0 筆無害）。
回應欄位：TransDate、TcType（N04 蔬菜、N05 水果、N06 花卉）、CropCode、CropName、MarketCode、MarketName、
Upper_Price、Middle_Price、Lower_Price、Avg_Price、Trans_Quantity。CropName「休市」列會略過。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://data.moa.gov.tw/api/v1/AgriProductsTransType/"
HERE = os.path.dirname(os.path.abspath(__file__))
CHUNK_DAYS = 2
PAGE_LIMIT = 1000
SLEEP_SECONDS = 0.3
TIMEOUT = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS PRICE_DAILY (
    TRANS_DATE   TEXT NOT NULL,   -- ISO yyyy-MM-dd
    TC_TYPE      TEXT NOT NULL,
    CROP_CODE    TEXT NOT NULL,
    CROP_NAME    TEXT,
    MARKET_CODE  TEXT NOT NULL,
    MARKET_NAME  TEXT,
    UPPER_PRICE  REAL,
    MIDDLE_PRICE REAL,
    LOWER_PRICE  REAL,
    AVG_PRICE    REAL,
    TRANS_QTY    REAL,
    PRIMARY KEY (TRANS_DATE, TC_TYPE, CROP_CODE, MARKET_CODE)
);
CREATE INDEX IF NOT EXISTS PRICE_DAILY_CROP_IDX ON PRICE_DAILY (CROP_NAME, TRANS_DATE);
CREATE TABLE IF NOT EXISTS FETCH_LOG (
    LOG_ID     INTEGER PRIMARY KEY AUTOINCREMENT,
    RANGE_FROM TEXT, RANGE_TO TEXT, MARKET_NAME TEXT,
    ROW_COUNT  INTEGER,
    FETCHED_AT TEXT,
    LOG_STATUS TEXT, LOG_NOTE TEXT
);
"""


def make_ssl_context() -> ssl.SSLContext:
    """Python 3.13 起預設 VERIFY_X509_STRICT，農業部憑證鏈缺 Subject Key Identifier 會被擋。
    只拿掉嚴格旗標，CA 與主機名驗證照常。"""
    ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


SSL_CTX = make_ssl_context()


def load_list(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]


def to_roc(d: dt.date) -> str:
    return f"{d.year - 1911}.{d.month:02d}.{d.day:02d}"


def from_roc(s: str) -> str:
    y, m, d = s.strip().split(".")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch(start: dt.date, end: dt.date, market: str | None, tries: int = 3) -> list[dict]:
    """逾時或連線錯誤自動重試，間隔遞增；API 回 ERROR 不重試。"""
    params = {"Start_time": to_roc(start), "End_time": to_roc(end)}
    if market:
        params["MarketName"] = market
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}",
                                 headers={"Accept": "application/json", "User-Agent": "foodsec/0.2"})
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
                body = resp.read()
            obj = json.loads(body.decode("utf-8-sig"))
            if obj.get("RS") != "OK":
                raise RuntimeError(f"API RS={obj.get('RS')} {obj.get('MSG', '')}")
            return obj.get("Data") or []
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as ex:
            last = ex
            if attempt < tries:
                time.sleep(3 * attempt)
    raise last if last else RuntimeError("fetch 失敗")


def fetch_market_range(start: dt.date, end: dt.date, market: str) -> tuple[list[dict], list[str]]:
    """回 (rows, warnings)。滿 1000 筆就拆單日重抓。"""
    rows = fetch(start, end, market)
    if len(rows) < PAGE_LIMIT or start == end:
        return rows, ([f"{market} {start} 單日仍滿 {PAGE_LIMIT} 筆，可能不完整"] if len(rows) >= PAGE_LIMIT else [])
    out: list[dict] = []
    warns: list[str] = []
    d = start
    while d <= end:
        time.sleep(SLEEP_SECONDS)
        day_rows = fetch(d, d, market)
        if len(day_rows) >= PAGE_LIMIT:
            warns.append(f"{market} {d} 單日仍滿 {PAGE_LIMIT} 筆，可能不完整")
        out.extend(day_rows)
        d += dt.timedelta(days=1)
    return out, warns


def upsert(con: sqlite3.Connection, rows: list[dict]) -> int:
    sql = """INSERT OR REPLACE INTO PRICE_DAILY
             (TRANS_DATE, TC_TYPE, CROP_CODE, CROP_NAME, MARKET_CODE, MARKET_NAME,
              UPPER_PRICE, MIDDLE_PRICE, LOWER_PRICE, AVG_PRICE, TRANS_QTY)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)"""
    data = []
    for r in rows:
        name = (r.get("CropName") or "").strip()
        tc = (r.get("TcType") or "").strip()
        if not tc or name == "休市":
            continue
        try:
            data.append((
                from_roc(r["TransDate"]), tc, str(r.get("CropCode") or "").strip(), name,
                str(r.get("MarketCode") or "").strip(), (r.get("MarketName") or "").strip(),
                to_float(r.get("Upper_Price")), to_float(r.get("Middle_Price")), to_float(r.get("Lower_Price")),
                to_float(r.get("Avg_Price")), to_float(r.get("Trans_Quantity")),
            ))
        except (KeyError, ValueError):
            continue
    con.executemany(sql, data)
    return len(data)


def log(con: sqlite3.Connection, start: dt.date, end: dt.date, market: str, n: int, status: str, note: str = "") -> None:
    con.execute("INSERT INTO FETCH_LOG (RANGE_FROM, RANGE_TO, MARKET_NAME, ROW_COUNT, FETCHED_AT, LOG_STATUS, LOG_NOTE) VALUES (?,?,?,?,?,?,?)",
                (start.isoformat(), end.isoformat(), market, n, dt.datetime.now().isoformat(timespec="seconds"), status, note[:300]))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)  # 導到檔案時也即時看得到進度
    ap = argparse.ArgumentParser(description="抓農業部批發市場行情到 SQLite")
    ap.add_argument("--days", type=int, default=30, help="抓最近幾天（預設 30）")
    ap.add_argument("--from", dest="date_from", help="起日 yyyy-MM-dd，與 --to 並用時忽略 --days")
    ap.add_argument("--to", dest="date_to", help="迄日 yyyy-MM-dd")
    ap.add_argument("--db", default=os.path.join(HERE, "data", "foodsec.db"))
    ap.add_argument("--markets", default=os.path.join(HERE, "markets.txt"))
    args = ap.parse_args()

    today = dt.date.today()
    if args.date_from and args.date_to:
        start = dt.date.fromisoformat(args.date_from)
        end = dt.date.fromisoformat(args.date_to)
    else:
        end = today
        start = today - dt.timedelta(days=args.days)
    if start > end:
        print("起日不可晚於迄日", file=sys.stderr)
        return 2

    markets = load_list(args.markets)
    if not markets:
        print(f"找不到市場清單 {args.markets}", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")  # 讀寫並行，產頁面時不會撞鎖
    con.executescript(SCHEMA)

    total = 0
    fails = 0
    warn_count = 0
    cur = start
    while cur <= end:
        seg_end = min(cur + dt.timedelta(days=CHUNK_DAYS - 1), end)
        seg_total = 0
        for market in markets:
            try:
                rows, warns = fetch_market_range(cur, seg_end, market)
                n = upsert(con, rows)
                log(con, cur, seg_end, market, n, "WARN" if warns else "OK", "; ".join(warns))
                seg_total += n
                warn_count += len(warns)
                for w in warns:
                    print(f"  警告：{w}", file=sys.stderr)
            except (urllib.error.URLError, RuntimeError, json.JSONDecodeError, TimeoutError, OSError) as ex:
                fails += 1
                log(con, cur, seg_end, market, 0, "FAIL", str(ex))
                print(f"  失敗：{market} {cur}~{seg_end} {ex}", file=sys.stderr)
            time.sleep(SLEEP_SECONDS)
        con.commit()
        total += seg_total
        print(f"{cur} ~ {seg_end}: {seg_total} 筆")
        cur = seg_end + dt.timedelta(days=1)

    (days, crops, mkts, dmin, dmax) = con.execute(
        "SELECT COUNT(DISTINCT TRANS_DATE), COUNT(DISTINCT CROP_CODE), COUNT(DISTINCT MARKET_CODE), MIN(TRANS_DATE), MAX(TRANS_DATE) FROM PRICE_DAILY"
    ).fetchone()
    con.close()
    print(f"本次寫入 {total} 筆，失敗 {fails} 次，截斷警告 {warn_count} 次；"
          f"資料庫現有 {days} 個交易日、{crops} 種作物、{mkts} 個市場，{dmin} ~ {dmax}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

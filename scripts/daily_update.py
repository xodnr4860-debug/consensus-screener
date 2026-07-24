"""매일 컨센서스 갱신 (연간 + 분기).

배포 서버에서 매일 밤(평일 22:00) cron으로 실행된다. 오늘 하루치만 받아서,
직전 저장값과 달라진 것만 consensus / consensus_q 에 덧붙인다. 과거를 다시 받지
않으므로 하루 약 120코인 수준으로 저렴하다.

수동 실행:
    python scripts/daily_update.py            # 오늘자 전체 갱신
    python scripts/daily_update.py --limit 3  # 3호출(30종목)만 — 점검용
    python scripts/daily_update.py --date 20260721   # 특정 날짜로
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import fnspace

ITEMS = ["E121000", "E122700", "E610550"]   # 매출액, 영업이익, 추정기관수
NOISE_THRESHOLD = 0.001                      # 0.1% 미만 변화는 잔떨림으로 무시
_QMAP = {"1Q": 1, "2Q": 2, "3Q": 3, "4Q": 4}


def _ensure_log(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
            rows INTEGER, message TEXT, finished_at TEXT NOT NULL)""")
    conn.commit()


def _changed(prev, cur) -> bool:
    """직전 저장값 대비 의미 있는 변화가 있었는지. 추정기관 수는 1명만 바뀌어도 남긴다."""
    if prev is None:
        return True
    for a, b in zip(prev[:2], cur[:2]):
        if (a is None) != (b is None):
            return True
        if a is None:
            continue
        if a == 0:
            if b != 0:
                return True
        elif abs(b - a) / abs(a) > NOISE_THRESHOLD:
            return True
    return prev[2] != cur[2]


def _latest_annual(conn) -> dict:
    rows = conn.execute("""
        SELECT c.ticker, c.fiscal_year, c.revenue, c.operating_income, c.est_count
        FROM consensus c
        JOIN (SELECT ticker, fiscal_year, MAX(snapshot_date) md FROM consensus
              GROUP BY ticker, fiscal_year) m
          ON c.ticker=m.ticker AND c.fiscal_year=m.fiscal_year AND c.snapshot_date=m.md
    """).fetchall()
    return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}


def _latest_quarterly(conn) -> dict:
    rows = conn.execute("""
        SELECT c.ticker, c.fiscal_year, c.fiscal_quarter, c.revenue, c.operating_income, c.est_count
        FROM consensus_q c
        JOIN (SELECT ticker, fiscal_year, fiscal_quarter, MAX(snapshot_date) md FROM consensus_q
              GROUP BY ticker, fiscal_year, fiscal_quarter) m
          ON c.ticker=m.ticker AND c.fiscal_year=m.fiscal_year
         AND c.fiscal_quarter=m.fiscal_quarter AND c.snapshot_date=m.md
    """).fetchall()
    return {(r[0], r[1], r[2]): (r[3], r[4], r[5]) for r in rows}


def _run_stage(conn, codes, day, *, basis, fry, toy) -> int:
    """basis 'annual'|'quarterly' 로 오늘 하루치를 받아 변화분만 저장. 저장 행 수 반환."""
    annualgb = "A" if basis == "annual" else "Q"
    table = "consensus" if basis == "annual" else "consensus_q"
    last = _latest_annual(conn) if basis == "annual" else _latest_quarterly(conn)
    kept = 0
    for batch in fnspace.batched(codes):
        for attempt in range(3):
            try:
                res = fnspace.fetch(batch, ITEMS, day, day, purpose=f"daily_{basis}",
                                    fraccyear=fry, toaccyear=toy, annualgb=annualgb)
                break
            except fnspace.CoinBudgetExceeded:
                print("코인 한도 도달 — 중단.")
                return kept
            except Exception as exc:
                if attempt == 2:
                    print(f"  배치 실패: {exc}")
                    res = None
                    break
                time.sleep(3 * (attempt + 1))
        if res is None:
            continue

        insert = []
        for ent in res.entities:
            for r in ent.get("DATA") or []:
                fy = r["FS_YEAR"]
                if basis == "annual":
                    key = (ent["CODE"], fy)
                    cols = (ent["CODE"], day, fy)
                else:
                    q = _QMAP.get(str(r.get("FS_QTR")))
                    if q is None:
                        continue
                    key = (ent["CODE"], fy, q)
                    cols = (ent["CODE"], day, fy, q)
                cur = (r.get("E121000"), r.get("E122700"), r.get("E610550"))
                if _changed(last.get(key), cur):
                    insert.append((*cols, *cur))
                    last[key] = cur
        if basis == "annual":
            conn.executemany(
                "INSERT OR REPLACE INTO consensus (ticker, snapshot_date, fiscal_year,"
                " revenue, operating_income, est_count) VALUES (?,?,?,?,?,?)", insert)
        else:
            conn.executemany(
                "INSERT OR REPLACE INTO consensus_q (ticker, snapshot_date, fiscal_year,"
                " fiscal_quarter, revenue, operating_income, est_count) VALUES (?,?,?,?,?,?,?)",
                insert)
        conn.commit()
        kept += len(insert)
    return kept


def main(limit: int | None = None, on: str | None = None) -> None:
    conn = sqlite3.connect(fnspace.DB_PATH)
    _ensure_log(conn)

    d = date.today() if on is None else date.fromisoformat(on)
    if d.weekday() >= 5:
        print(f"{d} 는 주말 — 컨센서스가 안 바뀌므로 건너뜁니다.")
        return
    day = d.strftime("%Y%m%d")

    codes = [r[0] for r in conn.execute(
        "SELECT ticker FROM company WHERE covered = 1 ORDER BY ticker").fetchall()]
    if limit:
        codes = codes[:limit * fnspace.MAX_CODES_PER_CALL]

    spent0 = fnspace.spent_this_cycle()
    print(f"[매일갱신] {day} · 대상 {len(codes)}종목", flush=True)

    ann = _run_stage(conn, codes, day, basis="annual", fry=d.year - 1, toy=d.year + 1)
    qtr = _run_stage(conn, codes, day, basis="quarterly", fry=d.year - 1, toy=d.year + 2)
    coins = fnspace.spent_this_cycle() - spent0

    conn.execute("INSERT INTO ingest_log (run_date, stage, status, rows, message,"
                 " finished_at) VALUES (?,?,?,?,?,?)",
                 (day, "daily", "ok", ann + qtr, f"annual={ann}, quarterly={qtr}, coins~{coins}",
                  datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    print(f"[매일갱신] 완료 · 연간 {ann}줄 / 분기 {qtr}줄 저장 · 소모 ~{coins}코인 · "
          f"누적 {fnspace.spent_this_cycle():,}/{fnspace.monthly_budget():,}")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="점검용: N호출(=N*10종목)만")
    ap.add_argument("--date", dest="on", default=None, help="특정 날짜 YYYY-MM-DD")
    a = ap.parse_args()
    main(a.limit, a.on)

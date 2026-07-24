"""분기 컨센서스 백필.

연간(backfill.py)과 별개로 분기 컨센서스를 받는다. annualgb=Q 로 호출하며,
FnSpace의 분기 데이터는 연도 오프셋이 있어 toaccyear를 원하는 마지막 연도 +1로
줘야 그 연도 분기가 나온다(2025~2028 요청 -> 2025·26·27 각 4분기).

분기는 12개 기간(3년×4분기)이라 연간(3개 기간)보다 데이터 행이 ~4배 많고,
코인은 행 수에 비례하므로 비용도 그만큼 크다. 그래서 --limit로 소량 체크포인트를
먼저 돌려 실제 코인을 확인한 뒤 전체를 진행한다.

    python scripts/backfill_quarterly.py --years 1 --limit 5   # 5호출 체크포인트
    python scripts/backfill_quarterly.py --years 1             # 전체
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import fnspace

ITEMS = {
    "E121000": "revenue",
    "E122700": "operating_income",
    "E610550": "est_count",
}
NOISE_THRESHOLD = 0.001
_QMAP = {"1Q": 1, "2Q": 2, "3Q": 3, "4Q": 4}


def setup(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS consensus_q (
            ticker           TEXT NOT NULL,
            snapshot_date    TEXT NOT NULL,
            fiscal_year      INTEGER NOT NULL,
            fiscal_quarter   INTEGER NOT NULL,
            revenue          REAL,
            operating_income REAL,
            est_count        INTEGER,
            PRIMARY KEY (ticker, fiscal_year, fiscal_quarter, snapshot_date)
        );
        CREATE INDEX IF NOT EXISTS idx_consensus_q_lookup
            ON consensus_q (ticker, fiscal_year, fiscal_quarter, snapshot_date DESC);
    """)
    conn.commit()


def _changed(prev, cur) -> bool:
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


def main(years: float, limit: int | None) -> None:
    conn = sqlite3.connect(fnspace.DB_PATH)
    setup(conn)

    codes = [r[0] for r in conn.execute(
        "SELECT ticker FROM company WHERE covered = 1 ORDER BY ticker").fetchall()]
    done = {r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM consensus_q").fetchall()}
    todo = [c for c in codes if c not in done]
    if limit:
        todo = todo[:limit * fnspace.MAX_CODES_PER_CALL]

    today = date.today()
    frdate = (today - timedelta(days=int(365 * years))).strftime("%Y%m%d")
    todate = today.strftime("%Y%m%d")
    # 분기 데이터 연도 오프셋: 마지막 연도 +1 까지 요청해야 당해 분기가 나온다.
    fry, toy = today.year - 1, today.year + 2

    n_calls = -(-len(todo) // fnspace.MAX_CODES_PER_CALL)
    print(f"[분기] 대상 {len(todo)}종목 (완료 {len(done)}) / 호출 {n_calls}회 / "
          f"기간 {frdate}~{todate}" + (f"  [체크포인트 {limit}호출]" if limit else ""),
          flush=True)
    if not todo:
        print("받을 종목이 없습니다.")
        return

    spent_before = fnspace.spent_this_cycle()
    kept = raw = 0
    t0 = time.time()
    for i, batch in enumerate(fnspace.batched(todo), start=1):
        for attempt in range(3):
            try:
                res = fnspace.fetch(batch, list(ITEMS), frdate, todate,
                                    purpose="backfill_q", fraccyear=fry, toaccyear=toy,
                                    annualgb="Q", enforce_budget=True)
                break
            except fnspace.CoinBudgetExceeded:
                print("코인 한도 도달 — 중단. 다시 실행하면 이어서 받습니다.")
                return
            except Exception as exc:
                if attempt == 2:
                    print(f"  [{i}/{n_calls}] 실패: {exc}", flush=True)
                    res = None
                    break
                time.sleep(3 * (attempt + 1))
        if res is None:
            continue

        rows = []
        for ent in res.entities:
            last: dict[tuple, tuple] = {}
            for r in sorted(ent.get("DATA") or [], key=lambda x: x["DT"]):
                q = _QMAP.get(str(r.get("FS_QTR")))
                if q is None:          # 'Annual' 등은 분기 테이블에 넣지 않는다
                    continue
                raw += 1
                key = (r["FS_YEAR"], q)
                cur = (r.get("E121000"), r.get("E122700"), r.get("E610550"))
                if _changed(last.get(key), cur):
                    rows.append((ent["CODE"], r["DT"], r["FS_YEAR"], q, *cur))
                    last[key] = cur
        conn.executemany(
            "INSERT OR REPLACE INTO consensus_q (ticker, snapshot_date, fiscal_year,"
            " fiscal_quarter, revenue, operating_income, est_count)"
            " VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        kept += len(rows)

        if i % 10 == 0 or i == n_calls:
            pct = i / n_calls
            eta = (time.time() - t0) / max(pct, .01) * (1 - pct) / 60
            print(f"  [{i}/{n_calls}] 저장 {kept:,}줄 / 원본 {raw:,}줄 / "
                  f"계량기 {fnspace.spent_this_cycle():,} / 남은시간 약 {eta:.0f}분",
                  flush=True)

    est_delta = fnspace.spent_this_cycle() - spent_before
    print(f"\n[분기] {'체크포인트' if limit else '전체'} 완료: {kept:,}줄 저장 "
          f"(원본 {raw:,}줄) / 이번 실행 추정소모 {est_delta:,}코인")
    print("👉 FnSpace 이용통계에서 실제 코인 델타를 확인하세요.")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=None, help="체크포인트 호출 수")
    a = ap.parse_args()
    main(a.years, a.limit)

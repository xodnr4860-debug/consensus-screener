"""커버 종목의 과거 컨센서스를 받아 저장한다.

과거분은 3개 항목만 받는다 — 리비전 계산에 쓰이는 건 매출액/영업이익뿐이고,
추정기관수는 '컨센서스 구성 변화'를 추적하는 데 필요하다. YoY 증가율·PER 등
나머지 지표는 오늘자 하루치만 있으면 되므로 일일 배치에서 따로 받는다.

저장은 '값이 바뀐 날만' 남긴다. 컨센서스는 애널리스트가 리포트를 낼 때만
움직이고 나머지 날은 같은 값의 복사본이라, 전부 쌓으면 낭비다. 다만 미세한
잔떨림(0.0001% 수준)이 매일 있어서, 의미 없는 변화는 무시한다.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import fnspace

ITEMS = {
    "E121000": "revenue",           # 매출액 (천원)
    "E122700": "operating_income",  # 영업이익 (천원)
    "E610550": "est_count",         # 추정기관 수
}

# 이보다 작은 변화는 계산상 잔떨림으로 보고 저장하지 않는다.
NOISE_THRESHOLD = 0.001   # 0.1%


def setup(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS consensus (
            ticker           TEXT NOT NULL,
            snapshot_date    TEXT NOT NULL,
            fiscal_year      INTEGER NOT NULL,
            revenue          REAL,
            operating_income REAL,
            est_count        INTEGER,
            PRIMARY KEY (ticker, fiscal_year, snapshot_date)
        );
        CREATE INDEX IF NOT EXISTS idx_consensus_lookup
            ON consensus (ticker, fiscal_year, snapshot_date DESC);
    """)
    conn.commit()


def _changed(prev: tuple | None, cur: tuple) -> bool:
    """직전 저장값 대비 의미 있는 변화가 있었는지.

    추정기관 수는 1명만 바뀌어도 컨센서스 구성이 달라진 것이므로 무조건 남긴다.
    """
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


def main(years: float = 1.0) -> None:
    conn = sqlite3.connect(fnspace.DB_PATH)
    setup(conn)

    codes = [r[0] for r in conn.execute(
        "SELECT ticker FROM company WHERE covered = 1 ORDER BY ticker").fetchall()]
    done = {r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM consensus").fetchall()}
    todo = [c for c in codes if c not in done]

    today = date.today()
    frdate = (today - timedelta(days=int(365 * years))).strftime("%Y%m%d")
    todate = today.strftime("%Y%m%d")

    est = fnspace.estimate_coins(len(todo), len(ITEMS), frdate, todate)
    n_calls = -(-len(todo) // fnspace.MAX_CODES_PER_CALL)
    print(f"백필 대상 {len(todo)}종목 (완료 {len(done)}) / 호출 {n_calls}회 / "
          f"예상 {est:,}코인 / 기간 {frdate}~{todate}", flush=True)
    if not todo:
        return

    kept = raw = 0
    t0 = time.time()
    for i, batch in enumerate(fnspace.batched(todo), start=1):
        for attempt in range(3):
            try:
                res = fnspace.fetch(batch, list(ITEMS), frdate, todate,
                                    purpose="backfill", fraccyear=today.year - 1,
                                    toaccyear=today.year + 1)
                break
            except fnspace.CoinBudgetExceeded:
                print("코인 한도 도달 — 중단합니다. 나중에 다시 실행하면 이어서 받습니다.")
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
            last: dict[int, tuple] = {}
            for r in sorted(ent.get("DATA") or [], key=lambda x: x["DT"]):
                raw += 1
                fy = r["FS_YEAR"]
                cur = (r.get("E121000"), r.get("E122700"), r.get("E610550"))
                if _changed(last.get(fy), cur):
                    rows.append((ent["CODE"], r["DT"], fy, *cur))
                    last[fy] = cur
        conn.executemany(
            "INSERT OR REPLACE INTO consensus (ticker, snapshot_date, fiscal_year,"
            " revenue, operating_income, est_count) VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        kept += len(rows)

        if i % 10 == 0 or i == n_calls:
            pct = i / n_calls
            eta = (time.time() - t0) / max(pct, .01) * (1 - pct) / 60
            print(f"  [{i}/{n_calls}] 저장 {kept:,}줄 / 원본 {raw:,}줄 "
                  f"(압축 {raw/max(kept,1):.1f}배) / 코인 {fnspace.spent_this_cycle():,} "
                  f"/ 남은시간 약 {eta:.0f}분", flush=True)

    print(f"\n완료: {kept:,}줄 저장 (원본 {raw:,}줄, {raw/max(kept,1):.1f}배 압축)")
    print(f"누적 코인 {fnspace.spent_this_cycle():,} / {fnspace.monthly_budget():,}")
    conn.close()


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 1.0)

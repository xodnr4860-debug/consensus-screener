"""커버 종목 전수조사.

전체 상장사(ETF·코넥스 제외) 6,334개를 한 번씩 훑어서 '애널리스트 추정치가
존재하는 종목' 명단을 만든다. 한 번 만들어두면 이후 매일 배치는 이 명단만
조회하므로 코인이 크게 절약된다. 분기에 한 번 정도만 다시 돌리면 된다.

추정기관수(E610550) 한 항목만, 하루치만 본다 -> 약 265코인.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core import fnspace

ITEM_EST_COUNT = "E610550"   # EPS추정기관수(지배)
TICKER_XLSX = fnspace.ROOT / "종목 리스트_20260720.xlsx"


def load_universe() -> pd.DataFrame:
    """엑셀에서 실제 사업회사만 추린다.

    FnGuide 종목 리스트에는 ETF/리츠가 섞여 있는데, 이들은 업종이 '미분류'로
    찍힌다. 코넥스는 컨센서스 커버리지가 사실상 없어 함께 제외한다.

    주의: 이 엑셀은 종목당 한 줄이 아니다. 한 종목이 여러 업종 분류에 걸치면
    그만큼 줄이 반복된다(6,334줄 -> 실제 2,648종목). 중복을 그대로 두면 같은
    종목을 몇 번씩 조회해 코인을 두 배 이상 낭비하므로 반드시 제거한다.
    """
    df = pd.read_excel(TICKER_XLSX)
    df = df.rename(columns={df.columns[7]: "sector_name", df.columns[8]: "industry"})
    df = df[(df["industry"] != "미분류") & (df["시장분류"] != "KONEX")]
    df = df.drop_duplicates(subset="종목코드", keep="first")
    return df[["종목코드", "종목명", "시장분류", "sector_name"]].reset_index(drop=True)


def setup_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS company (
            ticker    TEXT PRIMARY KEY,
            name      TEXT NOT NULL,
            market    TEXT NOT NULL,
            sector    TEXT,
            est_count INTEGER,          -- 조사 시점 추정기관 수 (NULL이면 미커버)
            covered   INTEGER NOT NULL DEFAULT 0,
            checked_at TEXT
        )""")
    conn.commit()


def main(snapshot: str | None = None) -> None:
    uni = load_universe()
    codes = uni["종목코드"].tolist()
    meta = {r["종목코드"]: r for _, r in uni.iterrows()}

    # 주말이면 직전 영업일로. 휴장일엔 데이터가 비어 돌아온다.
    d = date.today() if snapshot is None else date.fromisoformat(snapshot)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    day = d.strftime("%Y%m%d")

    conn = sqlite3.connect(fnspace.DB_PATH)
    setup_tables(conn)

    # 이미 오늘 조사한 종목은 건너뛴다. 중간에 끊겨도 이어서 돌릴 수 있다.
    done = {r[0] for r in conn.execute(
        "SELECT ticker FROM company WHERE checked_at = ?", (day,)).fetchall()}
    if done:
        codes = [c for c in codes if c not in done]
        print(f"이미 조사됨 {len(done)}종목 -> 건너뜀", flush=True)

    n_calls = (len(codes) + fnspace.MAX_CODES_PER_CALL - 1) // fnspace.MAX_CODES_PER_CALL
    est_total = fnspace.estimate_coins(len(codes), 1, day, day) if codes else 0
    print(f"남은 대상 {len(codes)}종목 / 호출 {n_calls}회 / 예상 {est_total}코인 / 기준일 {day}",
          flush=True)

    covered = 0
    for i, batch in enumerate(fnspace.batched(codes), start=1):
        for attempt in range(3):
            try:
                res = fnspace.fetch(
                    batch, [ITEM_EST_COUNT], day, day,
                    purpose="discover_universe", fraccyear=d.year, toaccyear=d.year,
                )
                break
            except Exception as exc:            # 일시적 네트워크 오류는 재시도
                if attempt == 2:
                    print(f"  [{i}/{n_calls}] 실패: {exc}", flush=True)
                    res = None
                    break
                time.sleep(2 * (attempt + 1))
        if res is None:
            continue

        found = {e["CODE"]: e for e in res.entities}
        for code in batch:
            ent = found.get(code)
            rows = (ent or {}).get("DATA") or []
            n_est = next((r.get(ITEM_EST_COUNT) for r in rows if r.get(ITEM_EST_COUNT)), None)
            m = meta[code]
            conn.execute(
                "INSERT INTO company (ticker, name, market, sector, est_count, covered,"
                " checked_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET"
                " est_count=excluded.est_count, covered=excluded.covered,"
                " checked_at=excluded.checked_at",
                (code, m["종목명"], m["시장분류"], m["sector_name"],
                 int(n_est) if n_est else None, 1 if n_est else 0, day),
            )
            if n_est:
                covered += 1
        conn.commit()

        if i % 50 == 0 or i == n_calls:
            print(f"  [{i}/{n_calls}] 진행 {i*10}종목 / 커버 {covered}개 / "
                  f"누적추정 {fnspace.spent_this_cycle()}코인", flush=True)

    dist = conn.execute(
        "SELECT CASE WHEN est_count >= 5 THEN '5명+' WHEN est_count >= 3 THEN '3-4명'"
        " WHEN est_count >= 1 THEN '1-2명' ELSE '미커버' END AS band, COUNT(*)"
        " FROM company GROUP BY band ORDER BY band DESC").fetchall()
    print("\n커버리지 분포")
    for band, cnt in dist:
        print(f"  {band:>6} : {cnt:,}종목")
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)

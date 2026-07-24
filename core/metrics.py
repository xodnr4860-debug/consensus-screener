"""저장된 컨센서스 이력 -> 스크리닝용 지표 계산(compute_metrics).

consensus/consensus_q 테이블은 '값이 바뀐 날'만 들어 있다. 따라서 특정 날짜의
컨센서스는 "그 날짜 이하에서 가장 최근 행"이다(as-of 조회). 중간 날짜가 비어
있어도 마지막 값이 그대로 유효하므로 정보 손실은 없다.

리비전(상향폭)의 정의:
    (현재 컨센서스 / N개월 전 컨센서스 - 1) x 100

여기서 반드시 함께 봐야 하는 게 추정기관 수의 증감이다. 컨센서스는 애널리스트
추정치의 평균이라, 보수적인 애널리스트가 커버를 그만두기만 해도 평균이 저절로
올라간다. 아무도 추정치를 올리지 않았는데 상향으로 잡히는 것이다. est_chg_*가
음수인 종목의 상향은 이 '구성 변화'를 의심해야 한다.

핵심 API는 compute_metrics(basis, fy, quarter) — 선택한 기준(연간/분기)과 기간의
종목별 지표를 즉석에서 만들어 반환한다. 화면(app.py)이 이걸 그대로 스크리닝한다.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from core import fnspace

# FnGuide 금액 항목의 단위는 천원. 화면·조건은 억원으로 다룬다.
THOUSAND_WON_TO_EOK = 1 / 100_000


def _pct_change(now: float | None, before: float | None) -> float | None:
    """상향폭(%). 부호가 바뀌는 경우(적자->흑자 등)는 비율이 무의미해 None."""
    if now is None or before is None or before == 0:
        return None
    if (before < 0) != (now < 0):
        return None
    return (now / before - 1) * 100 * (1 if before > 0 else -1)


def _as_of(rows: list[tuple], cutoff: str) -> tuple | None:
    """cutoff 이하에서 가장 최근 행. rows는 snapshot_date 오름차순."""
    found = None
    for r in rows:
        if r[0] <= cutoff:
            found = r
        else:
            break
    return found


# ---------------------------------------------------------------- 기준 선택형 계산

def _prev_quarter(fy: int, q: int) -> tuple[int, int]:
    return (fy, q - 1) if q > 1 else (fy - 1, 4)


def available_periods(conn: sqlite3.Connection) -> dict[str, list]:
    """UI 드롭다운용. 실제 데이터가 있는 연도/분기 목록."""
    years = [r[0] for r in conn.execute(
        "SELECT DISTINCT fiscal_year FROM consensus ORDER BY fiscal_year").fetchall()]
    quarters = []
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                    " AND name='consensus_q'").fetchone():
        quarters = [(r[0], r[1]) for r in conn.execute(
            "SELECT DISTINCT fiscal_year, fiscal_quarter FROM consensus_q"
            " ORDER BY fiscal_year, fiscal_quarter").fetchall()]
    return {"annual": years, "quarterly": quarters}


def _load_series(conn, table, ticker_filter, periods, as_of):
    """(ticker, period) -> snapshot 시계열. period는 annual이면 fiscal_year,
    quarterly면 (fiscal_year, fiscal_quarter)."""
    hist: dict = {}
    if table == "consensus":
        rows = conn.execute(
            "SELECT ticker, snapshot_date, fiscal_year, revenue, operating_income,"
            " est_count FROM consensus WHERE fiscal_year IN (%s) AND snapshot_date <= ?"
            " ORDER BY ticker, snapshot_date" % ",".join("?" * len(periods)),
            (*periods, as_of))
        for tk, sd, y, rev, op, ec in rows:
            hist.setdefault(tk, {}).setdefault(y, []).append((sd, rev, op, ec))
    else:
        clause = " OR ".join("(fiscal_year=? AND fiscal_quarter=?)" for _ in periods)
        params = [x for p in periods for x in p]
        rows = conn.execute(
            "SELECT ticker, snapshot_date, fiscal_year, fiscal_quarter, revenue,"
            " operating_income, est_count FROM consensus_q WHERE (%s) AND snapshot_date <= ?"
            " ORDER BY ticker, snapshot_date" % clause, (*params, as_of))
        for tk, sd, y, q, rev, op, ec in rows:
            hist.setdefault(tk, {}).setdefault((y, q), []).append((sd, rev, op, ec))
    return hist


def compute_metrics(conn: sqlite3.Connection, basis: str, fy: int,
                    quarter: int | None = None, as_of: str | None = None) -> list[dict]:
    """선택한 기준(연간/분기)과 기간의 종목별 지표를 즉석에서 계산한다.

    스크리닝 룰은 지표 키(rev_yoy, op_rev_3m ...)로만 판정하므로, 기준에 맞는
    지표표를 만들어 주면 같은 룰이 그대로 동작한다. 분기 기준일 때만 QoQ(직전
    분기 대비)와 YoY(전년 동분기 대비)가 채워진다.
    """
    table = "consensus" if basis == "annual" else "consensus_q"
    latest = conn.execute(f"SELECT MAX(snapshot_date) FROM {table}").fetchone()[0]
    if latest is None:
        return []
    as_of = as_of or latest
    d = date.fromisoformat(as_of)
    cut_1m = (d - timedelta(days=30)).isoformat()
    cut_3m = (d - timedelta(days=91)).isoformat()

    meta = {r[0]: r[1:] for r in conn.execute(
        "SELECT ticker, name, market, sector FROM company").fetchall()}

    if basis == "annual":
        cur_key, yoy_key, qoq_key = fy, fy - 1, None
        periods = [fy, fy - 1]
    else:
        cur_key = (fy, quarter)
        yoy_key = (fy - 1, quarter)
        qoq_key = _prev_quarter(fy, quarter)
        periods = [cur_key, yoy_key, qoq_key]

    hist = _load_series(conn, table, None, periods, as_of)

    out: list[dict] = []
    for tk, by_period in hist.items():
        cur = by_period.get(cur_key) or []
        if not cur:
            continue
        now = cur[-1]
        yoy = (by_period.get(yoy_key) or [None])[-1]
        qoq = (by_period.get(qoq_key) or [None])[-1] if qoq_key else None

        rev = now[1] * THOUSAND_WON_TO_EOK if now[1] is not None else None
        op = now[2] * THOUSAND_WON_TO_EOK if now[2] is not None else None
        m1, m3 = _as_of(cur, cut_1m), _as_of(cur, cut_3m)
        name, market, sector = meta.get(tk, (None, None, None))

        out.append({
            "ticker": tk, "name": name, "market": market, "sector": sector,
            "rev_est": rev, "op_est": op,
            "op_margin": (op / rev * 100) if rev not in (None, 0) and op is not None else None,
            "rev_yoy": _pct_change(now[1], yoy[1] if yoy else None),
            "op_yoy": _pct_change(now[2], yoy[2] if yoy else None),
            "rev_qoq": _pct_change(now[1], qoq[1] if qoq else None),
            "op_qoq": _pct_change(now[2], qoq[2] if qoq else None),
            "rev_rev_1m": _pct_change(now[1], m1[1] if m1 else None),
            "rev_rev_3m": _pct_change(now[1], m3[1] if m3 else None),
            "op_rev_1m": _pct_change(now[2], m1[2] if m1 else None),
            "op_rev_3m": _pct_change(now[2], m3[2] if m3 else None),
            "est_count": now[3],
            "est_count_3m": m3[3] if m3 else None,
            "est_chg_1m": (now[3] - m1[3]) if (m1 and now[3] is not None and m1[3] is not None) else None,
            "est_chg_3m": (now[3] - m3[3]) if (m3 and now[3] is not None and m3[3] is not None) else None,
        })
    return out


if __name__ == "__main__":
    # 간단 점검: 연간 2026 기준 지표를 계산해 상향/가짜상향 개수를 찍는다.
    conn = sqlite3.connect(fnspace.DB_PATH)
    rows = compute_metrics(conn, "annual", date.today().year)
    up = [r for r in rows if (r["op_rev_3m"] or 0) >= 10]
    fake = [r for r in up if (r["est_chg_3m"] or 0) < 0]
    print(f"연간 {date.today().year} · 전체 {len(rows):,}종목 / "
          f"영업이익 3개월 상향10%+ {len(up)} / 그중 추정기관 감소(가짜 상향 의심) {len(fake)}")

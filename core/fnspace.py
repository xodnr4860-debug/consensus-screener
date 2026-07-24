"""FnSpace(FnGuide) 컨센서스 API 클라이언트 + 코인 사용량 회계.

코인은 호출 횟수가 아니라 **데이터 양**으로 과금된다. 2026-07-20 실측 결과
두 건의 서로 다른 호출에서 아래 식이 일관되게 맞았다.

    코인 ≈ 종목수 x 영업일수 x 항목수 / COIN_DIVISOR

  · 10종목 x 1일  x 2항목 x 48회 =  1,920 units ->  44 코인  (divisor 43.6)
  · 10종목 x 750일 x 3항목 x  1회 = 22,500 units -> 930 코인  (divisor 24.2)

두 값이 정확히 일치하진 않으므로 보수적으로 작은 쪽(=비싸게 잡히는 쪽)을
쓴다. 예상보다 적게 나오면 다행이고, 많이 나와서 한도를 넘는 사고는 막는다.
실제 잔량은 FnSpace 이용통계 페이지가 정답이며, 여기 기록은 추정치다.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "screener.db"

BASE_URL = "https://www.fnspace.com/Api/Consensus3Api"

# 한 번에 보낼 수 있는 종목 수. 11개부터 "최대 요청건수 초과" 에러.
MAX_CODES_PER_CALL = 10

# 위 주석 참조. 보수적으로 잡은 값.
COIN_DIVISOR = 24.0

# 달력일 -> 영업일 환산 계수 (주말/공휴일 제외, 연 246일 기준)
BUSINESS_DAY_RATIO = 0.674


def _api_key() -> str:
    """`.env`에서 API 키를 읽는다. 키를 소스코드에 박아두지 않기 위함."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("FNSPACE_API_KEY="):
                return line.split("=", 1)[1].strip()
    key = os.environ.get("FNSPACE_API_KEY")
    if not key:
        raise RuntimeError(".env 파일에 FNSPACE_API_KEY가 없습니다.")
    return key


# ---------------------------------------------------------------- 코인 회계

def estimate_coins(n_codes: int, n_items: int, frdate: str, todate: str) -> int:
    """호출 전에 소모될 코인을 추정한다. 항상 올림 처리해 과소평가를 피한다."""
    span = (datetime.strptime(todate, "%Y%m%d") - datetime.strptime(frdate, "%Y%m%d")).days + 1
    business_days = max(1, round(span * BUSINESS_DAY_RATIO))
    units = n_codes * n_items * business_days
    return max(1, int(units / COIN_DIVISOR + 0.999))


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coin_ledger (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            called_at  TEXT NOT NULL,
            purpose    TEXT NOT NULL,
            n_codes    INTEGER NOT NULL,
            n_items    INTEGER NOT NULL,
            frdate     TEXT NOT NULL,
            todate     TEXT NOT NULL,
            est_coins  INTEGER NOT NULL,
            rows       INTEGER,
            ok         INTEGER NOT NULL
        )""")
    conn.commit()
    return conn


def spent_this_cycle(cycle_start: date | None = None) -> int:
    """이번 결제주기에 쓴 것으로 추정되는 코인 합계.

    FnSpace 주기는 매월 20일 시작(2026.07.20~08.19 확인). 인자를 주면 그날부터
    센다.
    """
    if cycle_start is None:
        today = date.today()
        cycle_start = (
            date(today.year, today.month, 20)
            if today.day >= 20
            else date(today.year - 1, 12, 20) if today.month == 1
            else date(today.year, today.month - 1, 20)
        )
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(est_coins), 0) FROM coin_ledger "
            "WHERE ok = 1 AND date(called_at) >= ?", (cycle_start.isoformat(),)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def monthly_budget() -> int:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("FNSPACE_MONTHLY_COINS="):
                return int(line.split("=", 1)[1].strip())
    return 50000


# ---------------------------------------------------------------- 호출

class CoinBudgetExceeded(RuntimeError):
    """예정된 호출이 남은 코인을 초과할 때. 실수로 한도를 태우는 걸 막는다."""


@dataclass
class Response:
    entities: list[dict[str, Any]]
    est_coins: int
    rows: int


def fetch(
    codes: Sequence[str],
    items: Sequence[str],
    frdate: str,
    todate: str,
    *,
    purpose: str,
    fraccyear: int,
    toaccyear: int,
    consolgb: str = "M",
    annualgb: str = "A",
    accdategb: str = "C",
    timeout: int = 300,
    enforce_budget: bool = True,
) -> Response:
    """컨센서스 데이터를 한 번 조회하고, 소모 코인을 장부에 남긴다.

    codes는 최대 10개. 그 이상 넣으면 API가 통째로 거절하므로 여기서 막는다.
    """
    if len(codes) > MAX_CODES_PER_CALL:
        raise ValueError(f"한 번에 최대 {MAX_CODES_PER_CALL}종목입니다 (요청 {len(codes)}개)")

    est = estimate_coins(len(codes), len(items), frdate, todate)

    if enforce_budget:
        remaining = monthly_budget() - spent_this_cycle()
        if est > remaining:
            raise CoinBudgetExceeded(
                f"이 호출은 약 {est}코인이 필요한데 남은 추정 잔량은 {remaining}코인입니다."
            )

    url = BASE_URL + "?" + urllib.parse.urlencode({
        "key": _api_key(), "format": "json",
        "code": ",".join(codes), "item": ",".join(items),
        "consolgb": consolgb, "annualgb": annualgb, "accdategb": accdategb,
        "fraccyear": fraccyear, "toaccyear": toaccyear,
        "frdate": frdate, "todate": todate,
    })

    payload = json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    ok = payload.get("success") == "true"
    entities = payload.get("dataset") or []
    rows = sum(len(e.get("DATA") or []) for e in entities)

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO coin_ledger (called_at, purpose, n_codes, n_items, frdate,"
            " todate, est_coins, rows, ok) VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), purpose, len(codes),
             len(items), frdate, todate, est if ok else 0, rows, int(ok)),
        )
        conn.commit()
    finally:
        conn.close()

    if not ok:
        raise RuntimeError(f"API 오류: {payload.get('errmsg')}")
    return Response(entities=entities, est_coins=est, rows=rows)


def batched(seq: Sequence[str], size: int = MAX_CODES_PER_CALL) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

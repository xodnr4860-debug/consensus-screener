"""컨센서스 스크리너 웹 화면.

실행:  streamlit run app.py

화면 구성
  · 사이드바 : 이번 달 코인 잔량 게이지 + 사용 내역
  · 본문     : 조건 빌더(키움 HTS 방식) -> 스크리닝 결과표
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import date

# 화면(뷰) 이름 — 버튼 네비게이션과 프로그램 전환(종목 링크)에 공용으로 쓴다.
# 무료 공개 배포 — 로그인 없이 주소만 알면 접속. 관리자 탭 없음.
SCREEN_VIEW = "🔍 조건 스크리닝"
STOCK_VIEW = "📊 종목 조회"

import pandas as pd
import streamlit as st

from core import fnspace, metrics
from core.rules import (COMPARATORS, DEFAULT_PRESET, METRICS, METRIC_BY_KEY,
                        RuleError, describe, screen, validate)

st.set_page_config(page_title="컨센서스 스크리너", page_icon="📈", layout="wide")

# 조건/그룹 사이의 AND·OR 연결어는 '클릭하면 전환되는 토글 배지 버튼'이다.
# 그룹 간·조건 간 배지 크기를 동일하게 맞추고, AND는 파랑 / OR는 주황으로 칠한다.
# 색은 컨테이너 키(connbtn_and_ / connbtn_or_)로 구분한다.
st.markdown("""
<style>
div[class*="st-key-connbtn_"] { display:flex; justify-content:center;
    margin:.15rem 0; transform:translateY(-0.35rem); }
div[class*="st-key-connbtn_"] button {
    border-radius:999px !important; font-weight:800 !important; font-size:.72rem !important;
    letter-spacing:.05em; padding:.2rem 1.1rem !important; min-height:0 !important;
    min-width:135px; white-space:nowrap;
}
div[class*="st-key-connbtn_and_"] button {
    background:rgba(59,130,246,.16) !important; color:#3b82f6 !important;
    border:1px solid rgba(59,130,246,.45) !important;
}
div[class*="st-key-connbtn_or_"] button {
    background:rgba(245,158,11,.18) !important; color:#d97706 !important;
    border:1px solid rgba(245,158,11,.5) !important;
}
div[class*="st-key-connbtn_"] button:hover { filter:brightness(1.08); }

.grp-tag {
    display:inline-block; font-size:.7rem; font-weight:700; letter-spacing:.05em;
    padding:.15rem .55rem; border-radius:6px;
    background:rgba(128,128,140,.16); color:inherit; opacity:.75;
}
/* 조건 그룹 상자에 옅은 음영 + 왼쪽 강조선.
   위쪽 여백을 넉넉히 줘서 '그룹 N' 태그가 테두리에 붙지 않게 한다. */
div[class*="st-key-grpbox"] {
    background:rgba(128,128,140,.055);
    border-left:3px solid rgba(59,130,246,.45) !important;
    border-radius:8px; padding:.9rem 1rem .4rem;
}
</style>""", unsafe_allow_html=True)


def connector_toggle(op: str, key: str) -> bool:
    """AND·OR 토글 배지 버튼을 그린다. 클릭되면 True를 반환한다.

    배지 자체가 버튼이라, 클릭할 때마다 AND↔OR가 바뀐다(별도 라디오 없음).
    """
    kind = "and" if op == "AND" else "or"
    label = "그리고 · AND" if op == "AND" else "또는 · OR"
    with st.container(key=f"connbtn_{kind}_{key}"):
        return st.button(label, key=f"cbtn_{key}", help="클릭하면 AND ↔ OR 전환")


# ---------------------------------------------------------------- 조건 빌더

def _new_id() -> str:
    """조건마다 고유 id. 위젯 key를 위치(0,1,2..)로 잡으면 중간 조건을 삭제했을 때
    아래 줄들이 위로 밀리면서 이전 줄의 위젯 값을 물려받아 값이 뒤섞인다."""
    st.session_state.setdefault("_seq", 0)
    st.session_state["_seq"] += 1
    return f"c{st.session_state['_seq']}"


def _blank_condition() -> dict:
    return {"metric": "op_rev_3m", "cmp": "gte", "value": 10.0, "_id": _new_id()}


# 그룹 모델: {"conditions": [c1, c2, ...], "ops": [op_12, op_23, ...]}.
# ops는 인접 조건 사이의 연결어(AND/OR)로, 길이는 조건 수 - 1. 그룹 사이 연결어는
# session_state.outer_ops(길이 = 그룹 수 - 1)에 따로 둔다. 모두 개별 전환된다.
_BUILDER_VER = 3


def init_state() -> None:
    if st.session_state.get("_bver") != _BUILDER_VER:
        # DEFAULT_PRESET을 화면 구조(그룹 + 개별 연결어)로 펼쳐 넣는다.
        # deepcopy 필수: 화면에서 조건을 그 자리에서 수정하므로 모듈 상수가 오염되면 안 됨.
        preset = deepcopy(DEFAULT_PRESET)
        groups, loose = [], []
        for child in preset["children"]:
            if "op" in child:
                conds = list(child["children"])
                groups.append({"conditions": conds,
                               "ops": [child["op"]] * (len(conds) - 1)})
            else:
                loose.append(child)
        if loose:
            groups.insert(0, {"conditions": loose, "ops": ["AND"] * (len(loose) - 1)})
        st.session_state.groups = groups
        st.session_state.outer_ops = [preset["op"]] * (len(groups) - 1)
        st.session_state._bver = _BUILDER_VER


def render_condition(gi: int, ci: int, cond: dict) -> None:
    """조건 한 줄:  [지표] [부등호] [숫자] [삭제]"""
    cid = cond.setdefault("_id", _new_id())
    c1, c2, c3, c4 = st.columns([5, 2, 3, 1])

    keys = [m.key for m in METRICS]
    labels = {m.key: m.label for m in METRICS}
    cond["metric"] = c1.selectbox(
        "지표", keys, index=keys.index(cond["metric"]),
        format_func=lambda k: labels[k], key=f"m_{cid}", label_visibility="collapsed")

    ops = list(COMPARATORS)
    cond["cmp"] = c2.selectbox(
        "비교", ops, index=ops.index(cond["cmp"]),
        format_func=lambda o: COMPARATORS[o][0], key=f"o_{cid}",
        label_visibility="collapsed")

    unit = METRIC_BY_KEY[cond["metric"]].unit
    cond["value"] = c3.number_input(
        unit, value=float(cond["value"]), step=1.0, format="%.2f",
        key=f"v_{cid}", label_visibility="collapsed")

    if c4.button("🗑", key=f"d_{cid}", help="이 조건 삭제"):
        g = st.session_state.groups[gi]
        if len(g["conditions"]) == 1:
            # 그룹의 마지막 조건을 지우면 그룹째 삭제(빈 그룹을 남기지 않음).
            st.session_state.groups.pop(gi)
            if st.session_state.outer_ops:
                st.session_state.outer_ops.pop(min(gi, len(st.session_state.outer_ops) - 1))
        else:
            g["conditions"].pop(ci)
            g["ops"].pop(min(ci, len(g["ops"]) - 1))  # 인접 연결어 하나 제거
        st.rerun()


def _flip(op: str) -> str:
    return "OR" if op == "AND" else "AND"


def _seq_to_tree(children: list, ops: list) -> dict:
    """조건 나열 + 개별 연결어를 룰 트리로. AND가 OR보다 먼저 묶인다(불리언 표준).

    예) A AND B OR C AND D  ->  OR( AND(A,B), AND(C,D) )
    """
    if len(children) == 1:
        return children[0]
    and_runs: list[list] = [[children[0]]]
    for i, op in enumerate(ops):
        if op == "AND":
            and_runs[-1].append(children[i + 1])
        else:
            and_runs.append([children[i + 1]])
    or_children = [r[0] if len(r) == 1 else {"op": "AND", "children": r} for r in and_runs]
    return or_children[0] if len(or_children) == 1 else {"op": "OR", "children": or_children}


def render_builder() -> dict:
    st.subheader("스크리닝 조건")
    st.caption("조건 사이·그룹 사이의 **파란/주황 배지를 각각 클릭**하면 AND(그리고) ↔ "
               "OR(또는)가 개별로 바뀝니다. AND가 OR보다 먼저 묶입니다 "
               "(예: `A 그리고 B 또는 C` = `(A 그리고 B) 또는 C`). 더 복잡한 묶음은 그룹으로.")

    for gi, group in enumerate(st.session_state.groups):
        if gi > 0:
            # 그룹 사이 연결어 — 이 자리(gi-1) 하나만 개별 전환
            if connector_toggle(st.session_state.outer_ops[gi - 1], f"outer{gi}"):
                st.session_state.outer_ops[gi - 1] = _flip(st.session_state.outer_ops[gi - 1])
                st.rerun()

        with st.container(border=True, key=f"grpbox{gi}"):
            h1, h2 = st.columns([9, 1], vertical_alignment="center")
            h1.markdown(f"<span class='grp-tag'>그룹 {gi+1} · 조건 {len(group['conditions'])}개</span>",
                        unsafe_allow_html=True)
            if h2.button("✕", key=f"gd{gi}", help="그룹 삭제"):
                st.session_state.groups.pop(gi)
                if st.session_state.outer_ops:
                    st.session_state.outer_ops.pop(min(gi, len(st.session_state.outer_ops) - 1))
                st.rerun()

            for ci, cond in enumerate(group["conditions"]):
                if ci > 0:
                    # 조건 사이 연결어 — 이 자리(ci-1) 하나만 개별 전환
                    if connector_toggle(group["ops"][ci - 1], f"g{gi}c{ci}"):
                        group["ops"][ci - 1] = _flip(group["ops"][ci - 1])
                        st.rerun()
                render_condition(gi, ci, cond)

            if st.button("＋ 조건 추가", key=f"ga{gi}"):
                group["conditions"].append(_blank_condition())
                group["ops"].append("AND")          # 새 조건 앞 연결어 기본 AND
                st.rerun()

    if st.button("＋ 그룹 추가"):
        st.session_state.groups.append({"conditions": [_blank_condition()], "ops": []})
        st.session_state.outer_ops.append("AND")    # 새 그룹 앞 연결어 기본 AND
        st.rerun()

    # 화면 관리용 _id는 룰 트리에 넣지 않는다 (저장 파일이 지저분해지므로).
    def clean(c: dict) -> dict:
        return {k: v for k, v in c.items() if k != "_id"}

    # 각 그룹을 개별 연결어(AND 우선) 규칙으로 트리화한 뒤, 그룹들을 다시 같은 규칙으로 묶는다.
    group_trees = [
        _seq_to_tree([clean(c) for c in g["conditions"]], g["ops"])
        for g in st.session_state.groups if g["conditions"]
    ]
    if not group_trees:
        return {}
    return _seq_to_tree(group_trees, st.session_state.outer_ops)


# ---------------------------------------------------------------- 데이터

@st.cache_data(ttl=600)
def get_periods() -> dict:
    conn = sqlite3.connect(fnspace.DB_PATH)
    try:
        return metrics.available_periods(conn)
    finally:
        conn.close()


@st.cache_data(ttl=300)
def compute_screen_metrics(basis: str, fy: int, quarter: int | None,
                           as_of: str) -> pd.DataFrame:
    """선택한 기준(연간/분기)·기간의 지표표. as_of는 캐시 무효화용(날짜)."""
    conn = sqlite3.connect(fnspace.DB_PATH)
    try:
        return pd.DataFrame(metrics.compute_metrics(conn, basis, fy, quarter))
    finally:
        conn.close()


def coverage_summary() -> tuple[int, int] | None:
    conn = sqlite3.connect(fnspace.DB_PATH)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "company" not in tables:
            return None
        total, cov = conn.execute(
            "SELECT COUNT(*), SUM(covered) FROM company").fetchone()
        return int(total), int(cov or 0)
    finally:
        conn.close()


@st.cache_data(ttl=300)
def covered_companies() -> pd.DataFrame:
    """종목 조회 드롭다운용 — 커버 종목 (코드, 이름, 시장)."""
    conn = sqlite3.connect(fnspace.DB_PATH)
    try:
        return pd.read_sql_query(
            "SELECT ticker, name, market FROM company WHERE covered = 1 ORDER BY name", conn)
    finally:
        conn.close()


# 컨센서스 저장값 단위: 천원 -> 억원
_TO_EOK = 1 / 100_000


@st.cache_data(ttl=300)
def ticker_timeseries(ticker: str) -> pd.DataFrame | None:
    """한 종목의 회계연도별 컨센서스 추이(일별, 억원).

    consensus 테이블은 '값이 바뀐 날'만 있으므로, 일 단위로 채워(ffill) 연속
    추이를 만든다. 반환 컬럼: fiscal_year, revenue(억원), op(억원), est_count.
    """
    conn = sqlite3.connect(fnspace.DB_PATH)
    try:
        raw = pd.read_sql_query(
            "SELECT snapshot_date, fiscal_year, revenue, operating_income, est_count"
            " FROM consensus WHERE ticker = ? ORDER BY snapshot_date", conn, params=[ticker])
    finally:
        conn.close()
    if raw.empty:
        return None

    raw["snapshot_date"] = pd.to_datetime(raw["snapshot_date"])
    idx = pd.date_range(raw["snapshot_date"].min(), raw["snapshot_date"].max(), freq="D")
    frames = []
    for fy, sub in raw.groupby("fiscal_year"):
        sub = sub.set_index("snapshot_date")
        sub = sub[~sub.index.duplicated(keep="last")].reindex(idx).ffill()
        frames.append(pd.DataFrame({
            "date": idx, "fiscal_year": fy,
            "revenue": sub["revenue"] * _TO_EOK,
            "op": sub["operating_income"] * _TO_EOK,
            "est_count": sub["est_count"],
        }))
    return pd.concat(frames, ignore_index=True)


@st.cache_data(ttl=300)
def ticker_quarterly_profile(ticker: str) -> pd.DataFrame | None:
    """분기별 '최신' 컨센서스 프로파일(억원). 각 (연도,분기)의 가장 최근 추정치를
    골라 분기 순서대로 정렬한다. 분기별 실적 예상 흐름(계절성)을 보는 용도."""
    conn = sqlite3.connect(fnspace.DB_PATH)
    try:
        raw = pd.read_sql_query(
            "SELECT snapshot_date, fiscal_year, fiscal_quarter, revenue, operating_income"
            " FROM consensus_q WHERE ticker = ? ORDER BY snapshot_date", conn, params=[ticker])
    finally:
        conn.close()
    if raw.empty:
        return None
    latest = raw.groupby(["fiscal_year", "fiscal_quarter"]).tail(1).copy()
    latest = latest.sort_values(["fiscal_year", "fiscal_quarter"])
    latest["period"] = latest["fiscal_year"].astype(str) + " " + latest["fiscal_quarter"].astype(str) + "Q"
    latest["revenue"] = latest["revenue"] * _TO_EOK
    latest["op"] = latest["operating_income"] * _TO_EOK
    return latest[["period", "fiscal_year", "fiscal_quarter", "revenue", "op"]]


# 색상 — 매출=파랑, 영업이익=초록 (라이트/다크 양쪽에서 읽히는 톤)
_C_REV, _C_OP = "#3b82f6", "#10b981"

# 확대/이동을 막을 모드바 버튼(트레이딩뷰식: 좌우 이동 + 횡축 휠줌만 남긴다).
# 세로 방향 조작을 유발하는 박스줌 계열을 제거하고, '축 리셋(화면에 맞춤)'은 남긴다.
_HIDE_BTNS = ["zoom2d", "zoomIn2d", "zoomOut2d", "select2d", "lasso2d", "autoScale2d"]


def _time_chart(s: pd.Series, title: str, color: str, unit: str = "억원") -> None:
    """시계열 라인차트. 클릭드래그=좌우 이동만, 휠=횡축 확대/축소만(세로 고정).
    더블클릭 또는 모드바 '축 리셋'으로 화면에 데이터를 다시 맞춘다(오토)."""
    import plotly.graph_objects as go
    s = s.dropna()
    fig = go.Figure(go.Scatter(
        x=s.index, y=s.values, mode="lines", line=dict(color=color, width=2),
        hovertemplate="%{x|%Y-%m-%d}<br>%{y:,.0f}" + unit + "<extra></extra>"))
    fig.update_layout(
        title=dict(text=title, font=dict(size=15)), dragmode="pan",
        height=300, margin=dict(l=10, r=10, t=44, b=10), showlegend=False,
        hovermode="x unified")
    fig.update_xaxes(fixedrange=False)   # 좌우 이동/횡축 줌 허용
    fig.update_yaxes(fixedrange=True)    # 세로 고정 — 위아래로 안 흔들림
    st.plotly_chart(fig, use_container_width=True, config={
        "scrollZoom": True, "displaylogo": False, "modeBarButtonsToRemove": _HIDE_BTNS})


def _quarter_bar(df: pd.DataFrame, ycol: str, title: str, color: str) -> None:
    """분기별 프로파일 막대. 한 화면에 다 들어오므로 확대/이동은 잠근다."""
    import plotly.graph_objects as go
    fig = go.Figure(go.Bar(
        x=df["period"], y=df[ycol], marker_color=color,
        hovertemplate="%{x}<br>%{y:,.0f}억<extra></extra>"))
    fig.update_layout(title=dict(text=title, font=dict(size=15)),
                      height=300, margin=dict(l=10, r=10, t=44, b=10), showlegend=False)
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# ---------------------------------------------------------------- 결과표

def render_results(rule: dict, hits: list[dict], total: int) -> None:
    st.success(f"**{len(hits)}종목**이 조건을 통과했습니다. (전체 {total:,}종목 중)")
    if not hits:
        return

    show = pd.DataFrame(hits)

    # 컨센서스는 애널리스트 추정치의 '평균'이라, 보수적인 애널리스트가 커버를
    # 그만두기만 해도 아무도 추정치를 올리지 않았는데 평균이 뛴다. 3개월 전 ->
    # 현재를 나란히 찍어 그 '가짜 상향'을 눈으로 바로 잡아낼 수 있게 한다.
    def coverage_cell(r) -> str:
        now, before = r.get("est_count"), r.get("est_count_3m")
        if pd.isna(now):
            return "-"
        if pd.isna(before) or before == now:
            return f"{int(now)}명"
        return f"{int(before)} → {int(now)}명 {'▼' if before > now else '▲'}"

    show["커버리지"] = show.apply(coverage_cell, axis=1)
    # 종목명 옆 '분석' 링크. 클릭하면 ?stock=코드 로 이동 -> 종목 조회 화면이 열린다.
    show["분석"] = "?stock=" + show["ticker"].astype(str)
    lead = ["name", "분석", "ticker", "커버리지"]
    skip = {"est_count", "est_count_3m", "est_chg_1m", "est_chg_3m"}
    # 전부 비어있는 지표 열은 숨긴다(예: 연간 기준일 때 QoQ 열).
    rest = [m.key for m in METRICS if m.key in show.columns and m.key not in skip
            and not show[m.key].isna().all()]

    # 숫자만 나열하면 %인지 억원인지 구분이 안 된다. 지표에 등록된 단위를 그대로
    # 셀에 붙이고, 소수점은 둘째 자리에서 반올림해 자릿수를 맞춘다.
    unit_format = {
        "%": "%.2f%%",
        "억원": "%,d억",   # 금액은 소수점 반올림 + 천단위 쉼표 (1,418,951억)
        "배": "%.2f배",
        "개": "%d개",
        "명": "%+d명",     # 증감이라 부호가 의미 있다
    }
    col_config = {
        "분석": st.column_config.LinkColumn(
            "분석", display_text="📊 보기", width="small",
            help="클릭하면 이 종목의 분석 대시보드로 이동합니다."),
        "커버리지": st.column_config.TextColumn(
            "애널리스트", help="3개월 전 → 현재 추정기관 수. ▼는 커버가 줄었다는 뜻으로, "
                              "상향폭이 실제 상향이 아니라 구성 변화일 수 있습니다.")}
    for m in METRICS:
        if m.key in rest:
            # 금액(억원)은 %d가 버림이라, 반올림은 데이터 단계에서 처리한다.
            if m.unit == "억원":
                show[m.key] = show[m.key].round().astype("Int64")
            col_config[m.label] = st.column_config.NumberColumn(
                m.label, format=unit_format.get(m.unit, "%.2f"))

    # 한 번에 ~25행까지 보이도록 표 높이를 키운다(그 이상은 표 내부 스크롤).
    n_show = min(len(show), 25)
    table_height = (n_show + 1) * 35 + 3
    st.caption("종목명 옆 **📊 보기**를 누르면 그 종목의 분석 대시보드로 이동합니다.")
    st.dataframe(
        show[lead + rest].rename(columns={
            "name": "종목명", "ticker": "코드",
            **{m.key: m.label for m in METRICS}}),
        hide_index=True, width="stretch", height=table_height, column_config=col_config)

    shrunk = show[show.get("est_chg_3m", pd.Series(dtype=float)).fillna(0) < 0]
    if len(shrunk):
        names = ", ".join(shrunk["name"].head(6).astype(str))
        st.warning(
            f"⚠️ **추정기관 감소에 의한 상향일 수 있습니다** — 통과 종목 중 "
            f"{len(shrunk)}개는 3개월 새 애널리스트가 줄었습니다 "
            f"({names}{' 외' if len(shrunk) > 6 else ''}). 보수적으로 보던 "
            f"애널리스트가 빠지면 아무도 추정치를 올리지 않아도 평균이 올라갑니다.")

    thin = int((show.get("est_count", pd.Series(dtype=float)).fillna(0) <= 2).sum())
    if thin:
        st.caption(f"참고: 통과 종목 중 {thin}개는 애널리스트 2명 이하입니다 — "
                   f"소수 의견이라 변화폭이 과장될 수 있습니다.")


# ---------------------------------------------------------------- 탭 1: 스크리닝

def period_selector() -> tuple[str, int, int | None, str]:
    """스크리닝 기준(연간/분기)과 대상 기간을 고른다. 반환: (basis, fy, quarter, 라벨)."""
    periods = get_periods()
    st.subheader("🎯 스크리닝 기준")
    c1, c2 = st.columns([1, 2])
    basis = c1.radio("구분", ["annual", "quarterly"], horizontal=True,
                     format_func=lambda b: "연간" if b == "annual" else "분기",
                     key="scr_basis")
    if basis == "annual":
        years = periods["annual"] or [date.today().year]
        default = years.index(date.today().year) if date.today().year in years else len(years) - 1
        fy = c2.selectbox("회계연도", years, index=default,
                          format_func=lambda y: f"{y}년(E)", key="scr_fy")
        return "annual", fy, None, f"{fy}년 연간"
    opts = periods["quarterly"] or [(date.today().year, 1)]
    # 기본값: 오늘 기준 직전에 해당하는 분기(대략 현재 분기)
    cur_q = (date.today().month - 1) // 3 + 1
    want = (date.today().year, cur_q)
    default = opts.index(want) if want in opts else len(opts) - 1
    pick = c2.selectbox("분기", opts, index=default,
                        format_func=lambda p: f"{p[0]}년 {p[1]}분기(E)", key="scr_q")
    return "quarterly", pick[0], pick[1], f"{pick[0]}년 {pick[1]}분기"


def tab_screening() -> None:
    cov = coverage_summary()
    if cov:
        st.caption(f"전체 {cov[0]:,}종목 중 애널리스트 커버 **{cov[1]:,}종목**이 스크리닝 대상입니다.")

    basis, fy, quarter, period_label = period_selector()
    if basis == "quarterly":
        st.caption("분기 기준에서는 **QoQ(직전 분기 대비)**, YoY(전년 동분기 대비) 증가율을 "
                   "조건으로 쓸 수 있습니다.")
    else:
        st.caption("연간 기준에서는 QoQ 조건은 적용되지 않습니다(값 없음).")

    rule = render_builder()

    st.divider()
    valid = bool(rule)
    if valid:
        try:
            validate(rule)
        except RuleError as exc:
            st.error(f"조건이 올바르지 않습니다 — {exc}")
            valid = False
    else:
        st.info("조건을 하나 이상 추가하세요.")

    # 스크리닝은 버튼을 눌렀을 때만 실행한다. 결과는 세션에 저장해 두고, 조건을
    # 편집하는 동안에는 마지막으로 '추출'한 결과가 그대로 유지된다.
    run = st.button(f"🔎 **{period_label}** 기준으로 추출", type="primary",
                    disabled=not valid, width="stretch")
    if run:
        # 서버가 가벼운 사양이라 체감 지연이 있을 수 있어 스피너로 진행을 알린다.
        with st.spinner(f"⟳ {period_label} 컨센서스로 조건에 맞는 종목을 찾는 중입니다…"):
            df = compute_screen_metrics(basis, fy, quarter, date.today().isoformat())
            if df.empty:
                st.warning("이 기간의 컨센서스 데이터가 없습니다. 다른 기준/기간을 선택하세요.")
            else:
                rows = df.to_dict("records")
                st.session_state.extracted = {
                    "rule": rule, "hits": screen(rule, rows), "total": len(rows),
                    "period": period_label}

    result = st.session_state.get("extracted")
    if not result:
        st.caption("기준을 고르고 조건을 만든 뒤 **‘추출’** 을 누르면 결과가 나옵니다.")
        return

    with st.expander(f"지금 조건 요약 · {result.get('period', '')} 기준", expanded=True):
        st.code(describe(result["rule"]), language=None)

    render_results(result["rule"], result["hits"], result["total"])
    st.download_button("조건 저장 (JSON)",
                       json.dumps(result["rule"], ensure_ascii=False, indent=2),
                       file_name="screening_rule.json", mime="application/json")


# ---------------------------------------------------------------- 탭 2: 종목 조회

def render_stock_dashboard(ticker: str, name: str, key_prefix: str = "") -> None:
    """한 종목의 컨센서스 대시보드(요약 지표 + 추이 차트). 종목 조회 탭과
    스크리닝 결과의 인라인 보기가 함께 쓴다. key_prefix로 위젯 키 충돌을 피한다."""
    with st.spinner(f"⟳ {name} 컨센서스 추이를 불러오는 중입니다… 잠시만 기다려 주세요."):
        ts = ticker_timeseries(ticker)
    if ts is None or ts.empty:
        st.warning("이 종목의 컨센서스 이력이 없습니다.")
        return

    years = sorted(ts["fiscal_year"].unique())
    default_fy = max([y for y in years if y <= date.today().year] or years)
    fy = st.radio("기준 회계연도", years, horizontal=True, index=years.index(default_fy),
                  format_func=lambda y: f"{y}년(E)", key=f"{key_prefix}fy_{ticker}")

    cur = ts[ts["fiscal_year"] == fy].set_index("date")
    prev = ts[ts["fiscal_year"] == fy - 1].set_index("date")

    def latest(s: pd.Series):
        s = s.dropna()
        return s.iloc[-1] if len(s) else None

    def chg_3m(s: pd.Series):
        s = s.dropna()
        if len(s) < 2:
            return None
        past = s[s.index <= s.index[-1] - pd.Timedelta(days=91)]
        base = past.iloc[-1] if len(past) else s.iloc[0]
        return (s.iloc[-1] / base - 1) * 100 if base else None

    rev_now, op_now = latest(cur["revenue"]), latest(cur["op"])
    est_now = latest(cur["est_count"])
    c1, c2, c3 = st.columns(3)
    c1.metric(f"{fy}년 매출 컨센서스",
              f"{rev_now:,.0f}억" if rev_now is not None else "-",
              f"{chg_3m(cur['revenue']):+.1f}% (3개월)" if chg_3m(cur['revenue']) is not None else None)
    c2.metric(f"{fy}년 영업이익 컨센서스",
              f"{op_now:,.0f}억" if op_now is not None else "-",
              f"{chg_3m(cur['op']):+.1f}% (3개월)" if chg_3m(cur['op']) is not None else None)
    c3.metric("추정기관 수", f"{int(est_now)}명" if est_now is not None else "-")

    st.caption("차트: 클릭 드래그 = 좌우 이동 · 마우스 휠 = 가로 확대/축소 · "
               "더블클릭 = 화면에 맞춤(오토)")

    # 연간 컨센서스 — 매출/영업이익을 좌우 2분할로 나란히
    st.markdown(f"#### 📊 {fy}년 연간 컨센서스 추이")
    lc, rc = st.columns(2)
    with lc:
        _time_chart(cur["revenue"], "매출액 (억원)", _C_REV)
    with rc:
        _time_chart(cur["op"], "영업이익 (억원)", _C_OP)

    # 증가율(YoY) 추이 — 매출/영업이익을 좌우 2분할 (전년 데이터가 있을 때만)
    if not prev.empty:
        rev_g = ((cur["revenue"] / prev["revenue"] - 1) * 100).dropna()
        op_g = ((cur["op"] / prev["op"] - 1) * 100).dropna()
        if not rev_g.empty or not op_g.empty:
            st.markdown(f"#### 📈 {fy}년 증가율(YoY) 컨센서스 추이 ({fy} vs {fy-1})")
            gl, gr = st.columns(2)
            with gl:
                _time_chart(rev_g, "매출 증가율 (%)", _C_REV, unit="%")
            with gr:
                _time_chart(op_g, "영업이익 증가율 (%)", _C_OP, unit="%")

    # 분기별 컨센서스 — 각 분기 최신 추정치(계절성 프로파일)를 좌우 2분할
    qp = ticker_quarterly_profile(ticker)
    if qp is not None and not qp.empty:
        st.markdown("#### 📊 분기별 컨센서스 (각 분기 최신 추정)")
        qlc, qrc = st.columns(2)
        with qlc:
            _quarter_bar(qp, "revenue", "분기 매출액 (억원)", _C_REV)
        with qrc:
            _quarter_bar(qp, "op", "분기 영업이익 (억원)", _C_OP)
    else:
        st.caption("분기 컨센서스 데이터가 없습니다.")

    # 추정기관 수 추이
    st.markdown("#### 👥 추정기관 수 추이")
    _time_chart(cur["est_count"], "추정기관 수 (명)", "#8b8b94", unit="명")


def tab_stock() -> None:
    st.caption("커버 종목을 검색하면 연간 매출·영업이익 컨센서스 추이와 분기별 컨센서스를 "
               "차트로 보여줍니다.")

    companies = covered_companies()
    if companies.empty:
        st.warning("아직 데이터가 없습니다. 백필을 먼저 실행하세요.")
        return

    label = companies["name"] + "  (" + companies["ticker"].str.lstrip("A") + ")"
    # 스크리닝 결과에서 종목을 클릭해 넘어온 경우, 그 종목을 기본 선택한다.
    jump = st.session_state.pop("stock_jump", None)
    default_idx = None
    if jump is not None and jump in set(companies["ticker"]):
        default_idx = int(companies.reset_index(drop=True).index[companies["ticker"] == jump][0])
    choice = st.selectbox("종목 검색", options=list(label), index=default_idx,
                          placeholder="종목명 또는 코드로 검색…")
    if choice is None:
        return
    ticker = companies.loc[label == choice, "ticker"].iloc[0]
    name = companies.loc[label == choice, "name"].iloc[0]
    render_stock_dashboard(ticker, name, key_prefix="tab_")


# ---------------------------------------------------------------- 메인

def main() -> None:
    init_state()
    st.title("📈 컨센서스 스크리너")

    # 결과표의 하이퍼링크(?stock=코드)로 들어오면 종목 조회 화면으로 전환하고
    # 해당 종목을 선택한다. 처리 후 쿼리파라미터는 지워 새로고침 때 재발동을 막는다.
    jumped = st.query_params.get("stock")
    if jumped:
        st.session_state.stock_jump = jumped
        st.session_state.view = STOCK_VIEW
        st.query_params.clear()

    # st.tabs는 코드로 탭 전환이 안 되므로, 버튼 기반 네비게이션으로 만든다.
    # 이래야 종목 링크가 실제로 화면을 넘길 수 있다.
    views = [SCREEN_VIEW, STOCK_VIEW]
    if st.session_state.get("view") not in views:
        st.session_state.view = SCREEN_VIEW

    nav = st.columns(len(views))
    for i, v in enumerate(views):
        active = v == st.session_state.view
        if nav[i].button(v, key=f"nav_{i}", width="stretch",
                         type="primary" if active else "secondary"):
            st.session_state.view = v
            st.rerun()

    st.divider()
    if st.session_state.view == SCREEN_VIEW:
        tab_screening()
    else:
        tab_stock()


if __name__ == "__main__":
    main()

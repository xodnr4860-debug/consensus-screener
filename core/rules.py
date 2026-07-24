"""컨센서스 스크리닝 룰 엔진.

조건은 JSON 트리로 표현한다. 두 종류의 노드가 있다.

  그룹 노드 : {"op": "AND"|"OR", "children": [...]}
  조건 노드 : {"metric": "op_rev_3m", "cmp": "gte", "value": 10}

HTS처럼 AND/OR을 무제한 중첩할 수 있고, 임계값은 사용자가 직접 넣는다.

    {"op": "AND", "children": [
        {"metric": "rev_yoy", "cmp": "gt",  "value": 0},
        {"metric": "op_yoy",  "cmp": "gt",  "value": 0},
        {"metric": "op_est",  "cmp": "gt",  "value": 0},
        {"op": "OR", "children": [
            {"metric": "op_rev_3m",  "cmp": "gte", "value": 10},
            {"metric": "rev_rev_3m", "cmp": "gte", "value": 10}]}]}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

# ---------------------------------------------------------------- 지표 정의

@dataclass(frozen=True)
class Metric:
    key: str
    label: str        # UI에 노출되는 한글명
    unit: str         # "%" | "억원" | "배"
    group: str        # UI 그룹핑용


# 스크리너가 다루는 지표 전체 목록. Streamlit 조건 빌더는 이 목록을 그대로
# 드롭다운으로 렌더링하므로, 지표를 늘리려면 여기에만 추가하면 된다.
METRICS: tuple[Metric, ...] = (
    # 추정 실적 성장성 (당해/차년도 컨센서스의 YoY)
    Metric("rev_yoy", "매출액 증가율(YoY, 추정)", "%", "성장성"),
    Metric("op_yoy", "영업이익 증가율(YoY, 추정)", "%", "성장성"),
    Metric("np_yoy", "순이익 증가율(YoY, 추정)", "%", "성장성"),
    # QoQ 증가율 — 분기 기준으로 스크리닝할 때만 값이 있다(직전 분기 대비).
    Metric("rev_qoq", "매출액 증가율(QoQ, 추정)", "%", "성장성"),
    Metric("op_qoq", "영업이익 증가율(QoQ, 추정)", "%", "성장성"),
    # 추정 실적 절대 수준
    Metric("rev_est", "추정 매출액", "억원", "규모"),
    Metric("op_est", "추정 영업이익", "억원", "규모"),
    Metric("op_margin", "추정 영업이익률", "%", "규모"),
    # 컨센서스 리비전 — 현재 컨센서스 대비 N개월 전 컨센서스의 변화율
    Metric("rev_rev_1m", "매출 컨센서스 변화(1개월)", "%", "리비전"),
    Metric("rev_rev_3m", "매출 컨센서스 변화(3개월)", "%", "리비전"),
    Metric("op_rev_1m", "영업이익 컨센서스 변화(1개월)", "%", "리비전"),
    Metric("op_rev_3m", "영업이익 컨센서스 변화(3개월)", "%", "리비전"),
    # 주가/수급
    Metric("ret_1m", "1개월 주가 수익률", "%", "주가"),
    Metric("ret_3m", "3개월 주가 수익률", "%", "주가"),
    Metric("mkt_cap", "시가총액", "억원", "주가"),
    Metric("per_fwd", "12M Fwd PER", "배", "밸류에이션"),
    # 커버리지 — 추정기관 수가 적으면 리비전 노이즈가 크므로 필터가 필요하다
    Metric("est_count", "추정기관 수", "개", "커버리지"),
    # 컨센서스는 애널리스트 추정치의 '평균'이라, 보수적인 애널리스트가 커버를
    # 중단하기만 해도 아무도 추정치를 올리지 않았는데 평균이 뛴다. 이 구성 변화를
    # 걸러내려면 추정기관 수의 증감을 리비전과 나란히 봐야 한다.
    Metric("est_chg_1m", "추정기관 수 증감(1개월)", "명", "커버리지"),
    Metric("est_chg_3m", "추정기관 수 증감(3개월)", "명", "커버리지"),
)

METRIC_BY_KEY: Mapping[str, Metric] = {m.key: m for m in METRICS}


# ---------------------------------------------------------------- 비교 연산

COMPARATORS: Mapping[str, tuple[str, Callable[[float, float], bool]]] = {
    "gt": (">", lambda a, b: a > b),
    "gte": (">=", lambda a, b: a >= b),
    "lt": ("<", lambda a, b: a < b),
    "lte": ("<=", lambda a, b: a <= b),
}


class RuleError(ValueError):
    """룰 트리가 구조적으로 잘못된 경우."""


def validate(node: Any, _path: str = "root") -> None:
    """룰 트리를 재귀 검증한다. 문제가 있으면 RuleError를 던진다.

    저장 시점과 평가 시점 양쪽에서 부르기 때문에, 손상된 프리셋이
    배치 도중에 터지는 일이 없다.
    """
    if not isinstance(node, dict):
        raise RuleError(f"{_path}: 노드는 dict여야 합니다 (got {type(node).__name__})")

    if "op" in node:
        if node["op"] not in ("AND", "OR"):
            raise RuleError(f"{_path}: op는 AND 또는 OR이어야 합니다 (got {node['op']!r})")
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise RuleError(f"{_path}: 그룹에는 자식 조건이 최소 1개 필요합니다")
        for i, child in enumerate(children):
            validate(child, f"{_path}.{node['op']}[{i}]")
        return

    if "metric" not in node:
        raise RuleError(f"{_path}: 조건 노드에 metric이 없습니다")
    if node["metric"] not in METRIC_BY_KEY:
        raise RuleError(f"{_path}: 알 수 없는 지표 {node['metric']!r}")
    if node.get("cmp") not in COMPARATORS:
        raise RuleError(f"{_path}: 알 수 없는 비교연산 {node.get('cmp')!r}")
    if not isinstance(node.get("value"), (int, float)) or isinstance(node.get("value"), bool):
        raise RuleError(f"{_path}: value는 숫자여야 합니다 (got {node.get('value')!r})")


def evaluate(node: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """종목 한 개(row)가 룰을 통과하는지 판정한다.

    row에 지표가 없거나 None이면 (= FnGuide가 값을 안 준 경우) 해당 조건은
    False로 처리한다. 데이터 결측을 통과로 봐주면 커버리지 없는 종목이
    스크리닝 결과에 섞여 들어온다.
    """
    if "op" in node:
        results = (evaluate(child, row) for child in node["children"])
        return all(results) if node["op"] == "AND" else any(results)

    actual = row.get(node["metric"])
    if actual is None:
        return False
    _, fn = COMPARATORS[node["cmp"]]
    return fn(float(actual), float(node["value"]))


def screen(rule: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """전체 상장사 중 룰을 통과한 종목만 반환한다."""
    validate(rule)
    return [row for row in rows if evaluate(rule, row)]


# ---------------------------------------------------------------- 사람이 읽는 형태

def describe(node: Mapping[str, Any], indent: int = 0) -> str:
    """룰 트리를 화면 '조건 요약'에 붙일 들여쓰기 한글 문자열로 변환한다."""
    pad = "  " * indent
    if "op" in node:
        joiner = "그리고" if node["op"] == "AND" else "또는"
        lines = [f"{pad}({joiner})"]
        lines += [describe(c, indent + 1) for c in node["children"]]
        return "\n".join(lines)

    metric = METRIC_BY_KEY[node["metric"]]
    symbol, _ = COMPARATORS[node["cmp"]]
    return f"{pad}- {metric.label} {symbol} {node['value']:g}{metric.unit}"


# 사용자가 최초에 제시한 조건을 기본 프리셋으로 넣어둔다.
# est_chg_3m >= 0 은 '추정기관이 빠져서 평균이 올라간 가짜 상향'을 걸러내는 장치다.
# 화면에서 지우면 그런 종목도 함께 보인다.
DEFAULT_PRESET: dict[str, Any] = {
    "op": "AND",
    "children": [
        {"metric": "rev_yoy", "cmp": "gt", "value": 0},
        {"metric": "op_yoy", "cmp": "gt", "value": 0},
        {"metric": "op_est", "cmp": "gt", "value": 0},
        {"metric": "est_chg_3m", "cmp": "gte", "value": 0},
        {
            "op": "OR",
            "children": [
                {"metric": "op_rev_3m", "cmp": "gte", "value": 10},
                {"metric": "rev_rev_3m", "cmp": "gte", "value": 10},
            ],
        },
    ],
}

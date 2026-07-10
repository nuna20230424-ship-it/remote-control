"""NavGraph — NavMap(정본) 위에 얹는 networkx.DiGraph 런타임 뷰.

이 모듈의 위치와 역할
--------------------
- tier1. 학습 엔진(learner)이 관측을 여기에 **채우고**, 계획 엔진(planner)이 최단경로를
  **소비하며**, REST API 가 상태/전이/커버리지를 **조회**하는 공용 그래프 계층이다.
- 영속화의 정본(source of truth)은 어디까지나 :class:`~remotectl.models.NavMap`
  (states/transitions 리스트)이다. networkx 그래프는 그로부터 재구성하는 **런타임 뷰**이며,
  두 표현은 항상 동기 상태로 유지된다(모든 변경 메서드가 양쪽을 함께 갱신한다).
- 그래프 라이브러리 의존은 이 모듈 안에 격리된다. 상위 계층(엔진/API)은 NavGraph 의
  공개 메서드(upsert_state/observe_transition/shortest_path/unexplored/coverage/save/load)만
  사용하고 networkx 를 직접 임포트하지 않는다.

설계 결정
---------
- **엣지 다중성**: 같은 (from, to) 사이에도 서로 다른 키(token)로 여러 전이가 존재할 수 있어
  ``networkx.MultiDiGraph`` 를 사용한다. 각 엣지는 key.token 을 그래프 키로 삼아 유일하다.
  같은 (from, token) 이 여러 to 로 관측되는 비결정성(R3)은 서로 다른 to 노드로 향하는
  별개 엣지로 표현된다.
- **최단경로 비용**: 홉 수(스텝 수) 최소화가 목표이므로 모든 엣지 가중치를 1 로 둔다.
  동일 홉 수의 대안 중에서는 전이 confidence 가 높은(=덜 위험한) 엣지를 선호한다
  (가중치 = 1 + (1 - confidence) * ε, ε 는 홉 우선순위를 깨지 않는 작은 값).
- **결정론**: NavMap 의 states/transitions 순서를 보존하고, 동률일 때 token 정렬 등으로
  타이브레이크해 같은 맵이면 항상 같은 경로/후보 순서를 돌려준다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import networkx as nx

from .models import (
    Button,
    KeyPress,
    NavMap,
    ScreenState,
    StateKind,
    Transition,
    _utcnow,
)

# 동일 홉 수 경로 사이에서 confidence 로 타이브레이크하기 위한 미세 가중치 계수.
# 한 홉의 기본 비용 1 에 비해 충분히 작아, 홉 수가 적은 경로가 항상 우선한다.
_CONFIDENCE_EPSILON = 1e-3

# 커버리지 계산 등에서 후보 버튼 집합을 지정하지 않았을 때 쓰는 기본 방향키 집합.
# (전 모듈 공용 정책이 아니라 이 모듈의 조회 편의를 위한 보수적 기본값.)
DEFAULT_COVERAGE_BUTTONS: list[KeyPress] = [
    KeyPress(button=Button.UP),
    KeyPress(button=Button.DOWN),
    KeyPress(button=Button.LEFT),
    KeyPress(button=Button.RIGHT),
    KeyPress(button=Button.OK),
    KeyPress(button=Button.BACK),
    KeyPress(button=Button.HOME),
]


def _edge_weight(confidence: float) -> float:
    """엣지 가중치: 홉 1 + 저신뢰 페널티(미세). 낮을수록 선호된다.

    confidence 가 1.0 이면 비용 1.0, 0.0 이면 1.0 + ε 로, 항상 홉 수 우선을 보장한다.
    """
    return 1.0 + (1.0 - confidence) * _CONFIDENCE_EPSILON


class NavGraph:
    """NavMap 을 감싸는 networkx MultiDiGraph 런타임 뷰.

    한 인스턴스는 정확히 하나의 :class:`NavMap` 을 소유하며, 모든 변경은 NavMap 과
    내부 그래프를 원자적으로 함께 갱신한다. 스레드 안전성은 보장하지 않는다(단일
    학습/실행 루프가 소유한다고 가정).
    """

    def __init__(self, navmap: Optional[NavMap] = None) -> None:
        """NavGraph 생성.

        Args:
            navmap: 감쌀 정본 맵. None 이면 빈 NavMap 을 새로 만든다.

        기존 navmap 의 states/transitions 로부터 내부 그래프를 재구성한다.
        """
        self._navmap: NavMap = navmap if navmap is not None else NavMap()
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._rebuild_graph()

    # ------------------------------------------------------------------ #
    # 내부: 그래프 재구성
    # ------------------------------------------------------------------ #

    def _rebuild_graph(self) -> None:
        """NavMap(정본)으로부터 내부 networkx 그래프를 완전히 재구성한다."""
        g: nx.MultiDiGraph = nx.MultiDiGraph()
        # 노드: 모든 상태(고립 상태 포함)를 먼저 등록.
        for state_id in self._navmap.states:
            g.add_node(state_id)
        # 엣지: 전이. 그래프 키 = key.token (같은 from,to 라도 키가 다르면 별개 엣지).
        for tr in self._navmap.transitions:
            self._add_edge_to_graph(g, tr)
        self._graph = g

    @staticmethod
    def _add_edge_to_graph(g: nx.MultiDiGraph, tr: Transition) -> None:
        """단일 Transition 을 그래프 엣지로 반영(가중치/전이 참조 포함)."""
        g.add_edge(
            tr.from_state_id,
            tr.to_state_id,
            key=tr.key.token,
            weight=_edge_weight(tr.confidence),
            transition=tr,
        )

    # ------------------------------------------------------------------ #
    # 공개 프로퍼티
    # ------------------------------------------------------------------ #

    @property
    def navmap(self) -> NavMap:
        """감싸고 있는 정본 NavMap(읽기/직렬화용)."""
        return self._navmap

    @property
    def graph(self) -> nx.MultiDiGraph:
        """내부 networkx 뷰(고급 분석/진단용). 직접 변경하지 말 것."""
        return self._graph

    # ------------------------------------------------------------------ #
    # 상태 upsert
    # ------------------------------------------------------------------ #

    def upsert_state(self, state: ScreenState) -> ScreenState:
        """상태를 맵에 삽입하거나(신규) 갱신한다(기존).

        같은 id(=signature 파생)의 상태가 이미 있으면 병합한다:
        - visit_count 는 두 값 중 큰 값(누적 방문 손실 방지)으로,
        - confidence 는 더 높은 값으로,
        - label/app_id/kind/screenshot_ref 는 비어있던 필드를 새 값으로 보완,
        - first_seen 은 더 이른 값 유지, last_seen 은 더 늦은 값으로 갱신.
        신규면 그대로 등록하고 그래프 노드를 추가한다.

        Args:
            state: 삽입/갱신할 화면 상태.

        Returns:
            맵에 최종 반영된(병합 후) ScreenState 인스턴스.
        """
        existing = self._navmap.states.get(state.id)
        if existing is None:
            # 신규 상태: 그대로 등록.
            merged = state
            self._navmap.states[state.id] = merged
            self._graph.add_node(merged.id)
        else:
            merged = self._merge_states(existing, state)
            self._navmap.states[merged.id] = merged
            # 노드는 이미 존재하지만 방어적으로 보장.
            if not self._graph.has_node(merged.id):
                self._graph.add_node(merged.id)

        # 루트 상태 힌트: 홈 상태를 처음 보면 루트로 지정(없을 때만).
        # 비-홈 상태가 먼저 관측돼도 루트가 오염되지 않도록 HOME 조건을 건다.
        if self._navmap.root_state_id is None and merged.kind is StateKind.HOME:
            self._navmap.root_state_id = merged.id

        self._touch()
        return merged

    @staticmethod
    def _merge_states(existing: ScreenState, incoming: ScreenState) -> ScreenState:
        """동일 id 상태 두 개를 보수적으로 병합한 새 ScreenState 를 만든다.

        validate_assignment 하에서 안전하도록 model_copy(update=...) 로 새 인스턴스를 만든다.
        """
        return existing.model_copy(
            update={
                "signature": existing.signature or incoming.signature,
                "label": existing.label or incoming.label,
                "kind": incoming.kind
                if existing.kind.value == "unknown" and incoming.kind.value != "unknown"
                else existing.kind,
                "app_id": existing.app_id or incoming.app_id,
                "screenshot_ref": existing.screenshot_ref or incoming.screenshot_ref,
                "confidence": max(existing.confidence, incoming.confidence),
                "visit_count": max(existing.visit_count, incoming.visit_count),
                "first_seen": min(existing.first_seen, incoming.first_seen),
                "last_seen": max(existing.last_seen, incoming.last_seen),
            }
        )

    # ------------------------------------------------------------------ #
    # 전이 관측
    # ------------------------------------------------------------------ #

    def observe_transition(
        self,
        from_state: ScreenState | str,
        key: KeyPress,
        to_state: ScreenState | str,
    ) -> Transition:
        """전이 (from, key) -> to 를 관측 기록한다.

        from_state/to_state 는 ScreenState 또는 이미 등록된 state_id 문자열 모두 허용한다.
        ScreenState 를 주면 관측 전에 upsert 하여 노드 존재를 보장한다(편의). id 문자열을
        주면 해당 상태가 이미 맵에 있어야 한다(없으면 ValueError).

        같은 (from_id, key.token, to_id) 3-튜플이 이미 있으면 observed_count 를 1 올리고
        last_observed 를 갱신한다(신규면 observed_count=1). 그래프 엣지의 가중치는
        transition.confidence 를 반영해 함께 갱신한다.

        Args:
            from_state: 입력 직전 상태(또는 id).
            key: 누른 키.
            to_state: 관측된 결과 상태(또는 id).

        Returns:
            생성 또는 갱신된 Transition.

        Raises:
            ValueError: id 문자열로 준 상태가 맵에 없을 때, 또는 key 타입이 잘못됐을 때.
        """
        if not isinstance(key, KeyPress):  # 방어: 문자열 리터럴 유입 차단
            raise ValueError("key 는 KeyPress 인스턴스여야 합니다(문자열 토큰 금지).")

        from_id = self._ensure_state(from_state, role="from_state")
        to_id = self._ensure_state(to_state, role="to_state")

        token = key.token
        existing = self._find_transition(from_id, token, to_id)
        if existing is not None:
            # validate_assignment 하에서 개별 필드 갱신은 재검증되므로 그대로 대입.
            existing.observed_count += 1
            existing.last_observed = _utcnow()
            tr = existing
        else:
            tr = Transition(
                from_state_id=from_id,
                key=key,
                to_state_id=to_id,
            )
            self._navmap.transitions.append(tr)

        # 그래프 엣지 동기화: 같은 token 엣지가 있으면 갱신, 없으면 추가.
        self._graph.add_edge(
            from_id,
            to_id,
            key=token,
            weight=_edge_weight(tr.confidence),
            transition=tr,
        )
        self._touch()
        return tr

    def _ensure_state(self, state: ScreenState | str, *, role: str) -> str:
        """ScreenState 는 upsert 후 id 반환, id 문자열은 맵 존재 검증 후 반환."""
        if isinstance(state, ScreenState):
            return self.upsert_state(state).id
        if isinstance(state, str):
            if state not in self._navmap.states:
                raise ValueError(
                    f"{role} id '{state}' 가 맵에 없습니다. 먼저 upsert_state 하세요."
                )
            return state
        raise ValueError(f"{role} 는 ScreenState 또는 state_id(str) 여야 합니다.")

    def _find_transition(
        self, from_id: str, token: str, to_id: str
    ) -> Optional[Transition]:
        """(from, token, to) 3-튜플로 기존 전이를 찾는다(없으면 None)."""
        for tr in self._navmap.transitions:
            if (
                tr.from_state_id == from_id
                and tr.to_state_id == to_id
                and tr.key.token == token
            ):
                return tr
        return None

    # ------------------------------------------------------------------ #
    # 조회
    # ------------------------------------------------------------------ #

    def get_state(self, state_id: str) -> Optional[ScreenState]:
        """상태 id 로 ScreenState 조회(없으면 None)."""
        return self._navmap.states.get(state_id)

    def outgoing(self, state_id: str) -> list[Transition]:
        """해당 상태에서 나가는 전이 목록(정본 순서 보존).

        존재하지 않는 state_id 면 빈 리스트를 돌려준다(예외 아님 — 조회 관대성).
        """
        return [t for t in self._navmap.transitions if t.from_state_id == state_id]

    def shortest_path(
        self, from_id: str, to_id: str
    ) -> Optional[list[Transition]]:
        """from_id -> to_id 최소 홉 전이 시퀀스를 반환한다.

        홉 수(스텝 수)를 우선 최소화하고, 동률이면 전이 confidence 가 높은 경로를 선호한다
        (엣지 가중치 = 1 + 저신뢰 페널티). 반환은 이어붙이면 경로가 되는 Transition 리스트.

        Args:
            from_id: 시작 상태 id.
            to_id: 목표 상태 id.

        Returns:
            - from_id == to_id 이고 그 상태가 존재하면 빈 리스트([]) (이미 목표).
            - 경로가 있으면 Transition 리스트.
            - 시작/목표 상태가 없거나 도달 불가면 None.
        """
        if from_id not in self._navmap.states or to_id not in self._navmap.states:
            return None
        if from_id == to_id:
            return []

        try:
            node_path: list[str] = nx.shortest_path(
                self._graph, source=from_id, target=to_id, weight="weight"
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

        # 노드 경로 -> 각 홉마다 최소 가중치(동률이면 token 오름차순) 엣지 선택.
        transitions: list[Transition] = []
        for a, b in zip(node_path, node_path[1:]):
            tr = self._best_edge_transition(a, b)
            if tr is None:  # 이론상 도달 불가(경로가 존재하므로 엣지도 존재)
                return None
            transitions.append(tr)
        return transitions

    def _best_edge_transition(self, a: str, b: str) -> Optional[Transition]:
        """a->b 병렬 엣지 중 가중치 최소(동률 시 token 오름차순) 전이를 고른다."""
        edge_dict = self._graph.get_edge_data(a, b)
        if not edge_dict:
            return None
        best: Optional[Transition] = None
        best_key: Optional[tuple[float, str]] = None
        for token, data in edge_dict.items():
            tr: Transition = data["transition"]
            sort_key = (data.get("weight", 1.0), token)
            if best_key is None or sort_key < best_key:
                best_key = sort_key
                best = tr
        return best

    # ------------------------------------------------------------------ #
    # 탐색 지원(학습 정책용)
    # ------------------------------------------------------------------ #

    def unexplored(
        self, from_id: str, button_set: list[KeyPress]
    ) -> list[KeyPress]:
        """from_id 에서 아직 시도하지 않은(관측 전이가 없는) 버튼 목록.

        button_set 의 각 KeyPress 에 대해, from_id 에서 그 token 으로 나가는 전이가
        한 번도 관측되지 않았으면 미탐색으로 간주한다. 학습 엔진의 "미탐색 간선 우선"
        정책(BFS)이 다음에 누를 후보를 고를 때 사용한다.

        입력 순서를 보존하고, 같은 token 중복은 한 번만 남긴다.

        Args:
            from_id: 기준 상태 id.
            button_set: 후보 버튼 집합.

        Returns:
            미탐색 KeyPress 리스트(from_id 가 맵에 없으면 button_set 전체가 미탐색).
        """
        explored_tokens = {
            t.key.token for t in self._navmap.transitions if t.from_state_id == from_id
        }
        result: list[KeyPress] = []
        seen: set[str] = set()
        for kp in button_set:
            token = kp.token
            if token in seen:
                continue
            seen.add(token)
            if token not in explored_tokens:
                result.append(kp)
        return result

    def coverage(self, button_set: list[KeyPress]) -> float:
        """탐색 커버리지(M4): 시도된 (state, button) 비율 [0.0, 1.0].

        분모 = (맵에 등록된 상태 수) * (button_set 의 유일 token 수).
        분자 = 그 중 실제로 관측 전이가 존재하는 (state, token) 조합 수.

        이는 "각 상태에서 각 후보 버튼을 최소 한 번 눌러봤는가"의 진행률로,
        학습 종료 판단(coverage_target)에 쓰인다. 상태가 없거나 button_set 이 비면
        정의 불가이므로 0.0 을 반환한다.

        Args:
            button_set: 커버리지 분모가 되는 후보 버튼 집합.

        Returns:
            0.0~1.0 커버리지 비율.
        """
        states = list(self._navmap.states)
        unique_tokens = {kp.token for kp in button_set}
        denom = len(states) * len(unique_tokens)
        if denom == 0:
            return 0.0

        # (from_id, token) 조합 중 button_set 에 포함되고 실제로 관측된 것 집계.
        explored_pairs: set[tuple[str, str]] = set()
        state_set = set(states)
        for t in self._navmap.transitions:
            token = t.key.token
            if t.from_state_id in state_set and token in unique_tokens:
                explored_pairs.add((t.from_state_id, token))

        return len(explored_pairs) / denom

    def coverage_report(self, button_set: list[KeyPress]) -> dict:
        """커버리지를 키별·상태별로 분해한 리포트.

        `coverage()` 가 단일 비율이라면, 이건 "어느 키가 어느 상태에서 아직 안 눌렸는지"를
        노출한다. 100% 가 **발견된 상태 기준**임을 감추지 않기 위한 것으로, 학습 요약과
        운영자 판단에 쓰인다(silent 100% 방지).

        Returns:
            dict: {
              overall: 전체 비율(=coverage),
              state_count, key_count,
              per_key: {token: {tried_states, ratio}},   # 각 키가 몇 개 상태에서 시도됐나
              uncovered: [{state_id, untried_tokens}],    # 상태별 미시도 키
            }
        """
        states = list(self._navmap.states)
        unique_tokens: list[str] = []
        seen: set[str] = set()
        for kp in button_set:
            if kp.token not in seen:
                seen.add(kp.token)
                unique_tokens.append(kp.token)

        state_set = set(states)
        explored: dict[str, set[str]] = {}
        for t in self._navmap.transitions:
            tok = t.key.token
            if t.from_state_id in state_set and tok in seen:
                explored.setdefault(tok, set()).add(t.from_state_id)

        n_states = len(states)
        per_key = {
            tok: {
                "tried_states": len(explored.get(tok, ())),
                "ratio": (len(explored.get(tok, ())) / n_states) if n_states else 0.0,
            }
            for tok in unique_tokens
        }
        uncovered = []
        for sid in states:
            untried = [tok for tok in unique_tokens if sid not in explored.get(tok, set())]
            if untried:
                uncovered.append({"state_id": sid, "untried_tokens": untried})

        denom = n_states * len(unique_tokens)
        covered_pairs = sum(len(v) for v in explored.values())
        return {
            "overall": (covered_pairs / denom) if denom else 0.0,
            "state_count": n_states,
            "key_count": len(unique_tokens),
            "per_key": per_key,
            "uncovered": uncovered,
        }

    # ------------------------------------------------------------------ #
    # 영속화
    # ------------------------------------------------------------------ #

    def save(self, path: str | Path) -> None:
        """정본 NavMap 을 JSON 파일로 저장한다.

        디렉터리가 없으면 생성한다. Enum 은 값, datetime 은 ISO-8601 로 직렬화된다.

        Args:
            path: 저장 경로.

        Raises:
            OSError: 파일 쓰기 실패 시.
        """
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self._navmap.dump_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "NavGraph":
        """JSON 파일에서 NavMap 을 로드해 NavGraph 를 만든다.

        알 수 없는 필드는 무시된다(스키마 진화 내성, models 의 extra="ignore").

        Args:
            path: 로드할 JSON 파일 경로.

        Returns:
            로드된 맵으로 그래프를 재구성한 NavGraph.

        Raises:
            FileNotFoundError: 파일이 없을 때.
            ValueError: JSON 파싱/검증 실패 시(pydantic ValidationError 포함).
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"맵 파일을 찾을 수 없습니다: {p}")
        raw = p.read_text(encoding="utf-8")
        navmap = NavMap.load_json(raw)
        return cls(navmap)

    # ------------------------------------------------------------------ #
    # 내부 유틸
    # ------------------------------------------------------------------ #

    def _touch(self) -> None:
        """맵 변경 시각 갱신."""
        self._navmap.updated_at = _utcnow()

    # ------------------------------------------------------------------ #
    # 진단용 표현
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        """상태(노드) 수."""
        return len(self._navmap.states)

    def __repr__(self) -> str:
        return (
            f"NavGraph(map_id={self._navmap.map_id!r}, "
            f"states={len(self._navmap.states)}, "
            f"transitions={len(self._navmap.transitions)})"
        )


__all__ = ["NavGraph", "DEFAULT_COVERAGE_BUTTONS"]

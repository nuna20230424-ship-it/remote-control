"""상태 식별(identify) — SenseResult 를 안정적인 state id 로 확정하고 맵에 반영.

역할(경계)
----------
- 이 모듈은 "지금 관찰한 화면(SenseResult)이 이미 아는 상태인가, 새 상태인가"를 판정하는
  순수 함수 계층이다. 부작용은 오직 ``resolve_state`` 가 그래프에 상태를 upsert 할 때뿐이다.
- 드라이버(전송)·센스(판정)와 무관하게, 이미 파생된 ScreenState 만 다룬다. HTTP/네트워크 없음.

식별 원리(PRD R2 — 내용 주소화 상태 정체성)
-------------------------------------------
- ScreenState.id 는 signature 로부터 결정론적으로 파생된다(models.compute_state_id).
  즉 "같은 화면 → 같은 signature → 같은 id" 가 이미 모델/센스 계층에서 보장된다.
- 따라서 상태 매칭은 복잡한 유사도 계산이 아니라 **id 동일성 조회**로 환원된다:
  이미 맵에 같은 id 의 상태가 있으면 그 상태가 "같은 화면"이다. 이 단순함이 설계 의도다.
- 이 모듈은 그 규약을 한곳에 캡슐화해, 엔진(learner/executor)이 id 파생 규칙이나
  맵 upsert 세부를 직접 알지 않고도 "관찰 → 확정 상태"를 얻게 한다(관심사 분리).

공개 함수
---------
- ``match_state(result, navmap) -> str | None``
    관찰 결과가 기존 맵의 어떤 상태와 매칭되는지 조회(부작용 없음). 매칭 시 그 state id,
    미매칭(새 화면)이면 None.
- ``resolve_state(result, graph) -> ScreenState``
    관찰 결과를 그래프에 upsert 하고, 맵에 반영된 확정 ScreenState 를 반환.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from remotectl.models import NavMap, ScreenState
from remotectl.sense.base import SenseResult

if TYPE_CHECKING:  # 런타임 임포트 회피(navmap 은 상위 tier 뷰; 순환/조기의존 방지, 덕타이핑 사용)
    from remotectl.navmap import NavGraph

__all__ = [
    "match_state",
    "resolve_state",
]


def match_state(result: SenseResult, navmap: NavMap) -> Optional[str]:
    """관찰 결과(SenseResult)가 기존 맵의 상태와 매칭되면 그 state id 를, 아니면 None 을 반환.

    부작용 없는 순수 조회다(맵을 변경하지 않는다).

    매칭 규약(PRD R2)
    -----------------
    - ScreenState.id 는 signature 파생이므로, "같은 화면"은 이미 같은 id 를 갖는다.
      따라서 매칭은 관찰 상태의 id 가 맵 states 에 이미 존재하는지 확인하는 것으로 충분하다.
    - 존재하면 그 id(=기존 상태와 동일 화면), 없으면 None(=아직 학습되지 않은 새 화면).

    Args:
        result: 센스가 판정한 관찰 결과. ``result.state.id`` 가 조회 키다.
        navmap: 조회 대상 네비게이션 맵(정본). 변경되지 않는다.

    Returns:
        매칭되는 기존 상태의 id. 매칭되는 상태가 없으면 None.

    Raises:
        TypeError: result/navmap 가 기대 타입이 아니어서 필수 속성 접근에 실패한 경우.
    """
    try:
        observed_id = result.state.id
    except AttributeError as exc:  # 잘못된 인자 타입을 명확한 오류로 승격
        raise TypeError(
            "match_state: result 는 .state.id 를 갖는 SenseResult 여야 합니다."
        ) from exc

    # 방어적 처리: id 가 비어 있으면(비정상 SenseResult) 매칭 불가로 간주한다.
    # 정상 경로에서는 센스 계층이 signature 파생 id 를 항상 채운다.
    if not observed_id:
        return None

    # 내용 주소화: 같은 화면은 같은 id → states 딕셔너리 존재 여부가 곧 매칭 여부.
    existing = navmap.get_state(observed_id)
    return existing.id if existing is not None else None


def resolve_state(result: SenseResult, graph: "NavGraph") -> ScreenState:
    """관찰 결과를 그래프에 upsert 하고, 맵에 반영된 확정 ScreenState 를 반환한다.

    ``match_state`` 가 순수 조회라면, 이 함수는 "확정 + 반영"을 담당하는 상태 변경 지점이다.
    엔진(learner/executor)은 press→capture→observe 로 얻은 SenseResult 를 이 함수에 넘겨
    "지금 확정된 현재 상태"를 얻는다. 새 화면이면 맵에 노드가 추가되고, 기존 화면이면
    방문 통계/최근관찰 등이 누적된다(구체 병합 정책은 NavGraph.upsert_state 소관).

    동작
    ----
    1. 관찰 결과의 ScreenState 를 그래프에 upsert 한다(신규 추가 또는 기존 노드 갱신).
    2. upsert 가 돌려준 "맵에 실제로 반영된" ScreenState 를 그대로 반환한다.

    id 는 signature 파생이므로 upsert 는 id 동일성으로 자동 병합된다(중복 노드 방지, R2).

    Args:
        result: 센스가 판정한 관찰 결과.
        graph: 상태 그래프 런타임 뷰(NavGraph). 이 호출로 상태가 반영된다.

    Returns:
        맵에 반영된 확정 ScreenState. 이후 전이 기록/계획의 기준 상태로 쓰인다.

    Raises:
        TypeError: result/graph 가 기대 인터페이스를 만족하지 않는 경우.
    """
    try:
        observed_state = result.state
    except AttributeError as exc:
        raise TypeError(
            "resolve_state: result 는 .state 를 갖는 SenseResult 여야 합니다."
        ) from exc

    upsert = getattr(graph, "upsert_state", None)
    if not callable(upsert):
        raise TypeError(
            "resolve_state: graph 는 upsert_state(state) 를 제공하는 NavGraph 여야 합니다."
        )

    # upsert_state 는 맵에 반영된(병합된) 확정 상태를 반환하는 것이 규약이다.
    # 방어적으로, 구현이 None 을 돌려주는 예외 상황에서는 관찰 상태를 그대로 확정으로 사용한다.
    resolved = upsert(observed_state)
    return resolved if resolved is not None else observed_state

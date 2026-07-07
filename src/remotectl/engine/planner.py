"""Planner — UC-3 계획 엔진.

이 모듈의 위치와 역할
--------------------
- tier2. 학습(UC-1)이 채워둔 :class:`~remotectl.navmap.NavGraph` 위에서, 시작 상태로부터
  목표 상태까지 **눌러야 할 버튼 시퀀스**를 뽑아낸다. 실제 드라이버/센스는 건드리지 않는
  순수 계획 계층으로, executor(UC-3 실행 루프)가 이 결과(PlanStep 시퀀스)를 받아
  실제로 실행·검증·재계획한다.
- 그래프 탐색(최단경로)의 책임은 전적으로 NavGraph 에 있다. Planner 는 그 결과를
  실행 가능한 :class:`~remotectl.models.PlanStep` 시퀀스로 **변환**하고, "경로 없음"을
  도메인 예외(:class:`PlanningError`)로 **승격**하는 얇은 계층이다(M6).

설계 결정
---------
- **도달 불가/미학습(R5)의 정규화**: NavGraph.shortest_path 는 (a) 시작/목표 상태가
  맵에 없거나 (b) 도달 불가일 때 모두 ``None`` 을 돌려준다. Planner 는 이 둘을 구분해
  명확한 사유 문자열과 함께 :class:`PlanningError` 로 올린다. 상위(executor)는 이 예외를
  잡아 ExecutionStatus 로 정규화한다.
- **from == goal**: 이미 목표에 있으므로 빈 계획([])을 돌려준다(예외 아님). 단, 그 상태가
  맵에 존재해야 한다(존재하지 않으면 미학습으로 간주해 예외).
- **PlanStep 채우기**: 각 홉(Transition)을 index/from/key/expected_to 로 채운다. 실행 후
  필드(executed/actual_to_state_id/matched/observed_confidence)는 계획 시점에는 기본값으로
  두고 executor 가 갱신한다.
"""

from __future__ import annotations

from ..models import PlanStep
from ..navmap import NavGraph


class PlanningError(RuntimeError):
    """계획을 수립할 수 없을 때 발생하는 도메인 예외.

    다음 두 경우를 승격한다(R5/M6):
    - 미학습: 시작 또는 목표 상태가 맵에 아직 존재하지 않음.
    - 도달 불가: 두 상태 모두 맵에 있으나 시작에서 목표로 가는 경로가 없음.

    상위 실행 계층(executor)은 이 예외를 잡아 적절한 ExecutionStatus 로 정규화하며,
    예외 자체를 사용자에게 전파하지 않는다.
    """


class Planner:
    """NavGraph 최단경로를 실행 가능한 PlanStep 시퀀스로 변환하는 계획기.

    상태를 갖지 않는 얇은 어댑터로, 주입된 그래프의 현재 스냅샷을 매 호출마다 조회한다.
    따라서 학습이 그래프를 갱신하면 다음 :meth:`plan` 호출부터 곧바로 반영된다.
    """

    def __init__(self, graph: NavGraph) -> None:
        """계획기를 초기화한다.

        Args:
            graph: 계획의 근거가 되는 네비게이션 그래프(정본 NavMap 의 런타임 뷰).
        """
        self._graph = graph

    def plan(self, from_state_id: str, goal_state_id: str) -> list[PlanStep]:
        """시작 상태에서 목표 상태까지의 최소 홉 계획을 수립한다.

        Args:
            from_state_id: 현재(시작) 상태 id.
            goal_state_id: 도달하고자 하는 목표 상태 id.

        Returns:
            이어 실행하면 목표에 도달하는 PlanStep 리스트(index 오름차순).
            시작과 목표가 같은 상태면 빈 리스트([])를 반환한다.

        Raises:
            PlanningError:
                - 시작/목표 상태가 맵에 없을 때(미학습).
                - 두 상태 모두 있으나 경로가 없을 때(도달 불가).
        """
        # 방어적 입력 검증: 빈 id 는 계획 불가.
        if not from_state_id or not goal_state_id:
            raise PlanningError(
                "시작/목표 상태 id 가 비어 있어 계획할 수 없습니다."
            )

        navmap = self._graph.navmap
        from_known = navmap.get_state(from_state_id) is not None
        goal_known = navmap.get_state(goal_state_id) is not None

        # 미학습(R5): 시작 또는 목표 상태가 맵에 없으면 경로 계산 자체가 불가능하다.
        if not from_known or not goal_known:
            missing: list[str] = []
            if not from_known:
                missing.append(f"시작 상태({from_state_id})")
            if not goal_known:
                missing.append(f"목표 상태({goal_state_id})")
            raise PlanningError(
                "맵에 학습되지 않은 상태가 있어 계획할 수 없습니다: "
                + ", ".join(missing)
            )

        # 이미 목표에 있음: 빈 계획.
        if from_state_id == goal_state_id:
            return []

        transitions = self._graph.shortest_path(from_state_id, goal_state_id)

        # 두 상태 모두 존재하지만 경로가 없음 -> 도달 불가.
        # (위에서 존재 및 from==goal 을 이미 배제했으므로 None 은 순수 도달 불가를 뜻한다.)
        if transitions is None:
            raise PlanningError(
                f"현재 맵에서 {from_state_id} -> {goal_state_id} 로 가는 경로가 없습니다."
            )

        # 정상적으로는 발생하지 않지만(경로가 있으면 최소 한 홉), 방어적으로 처리.
        if not transitions:
            return []

        steps: list[PlanStep] = []
        for index, tr in enumerate(transitions):
            steps.append(
                PlanStep(
                    index=index,
                    from_state_id=tr.from_state_id,
                    key=tr.key,
                    expected_to_state_id=tr.to_state_id,
                )
            )
        return steps

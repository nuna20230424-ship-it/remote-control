"""UC-3 목표기반 실행 엔진 — 계획을 실행하고, 스텝마다 관찰로 검증하며, 어긋나면 재계획한다.

역할
----
학습으로 구축한 네비게이션 맵 위에서 "넷플릭스 켜줘" 같은 자연어 목표를 받아,
목표 상태까지의 버튼 시퀀스를 스스로 실행·검증·교정하는 목표기반 에이전트다.
한 번의 :meth:`Executor.run_goal` 은 다음 단계를 밟는다:

    resolve_goal(자연어→Goal) → find_goal_state_id(맵 상태 매핑)
        → 현재 상태 관찰 → plan(최단경로) → [실행·검증·재계획 루프] → ExecutionResult

핵심 루프(계획 실행 + 검증 + 재계획, M2/M3)
--------------------------------------------
계획된 스텝을 하나씩:

    press(key) → settle → capture → observe(판정) → resolve_state(현재상태 확정)

로 실행한 뒤, "실제로 도달한 상태(actual)"가 "계획이 기대한 상태(expected)"와 같은지
검증한다(PlanStep.matched). 일치하면 다음 스텝으로 진행하고, **불일치하면**(비결정 전이·
저신뢰 판정·맵 오차 등, R3) 지금 실제로 서 있는 상태를 기준으로 목표까지 **재계획**한다.
재계획은 replan_budget 만큼만 허용한다(무한 루프 방지, M3).

결과 정규화(예외 미전파)
------------------------
이 엔진은 어떤 경우에도 예외를 밖으로 던지지 않는다. 모든 실패는 :class:`ExecutionResult`
의 status 로 정규화된다(호출자가 분기 없이 결과만 읽으면 되도록):

- SUCCESS             : 목표 상태 도달.
- FAILED_UNRESOLVED   : 목표 해석/맵 매핑 실패(미학습 앱, 알 수 없는 명령 등, R4).
- FAILED_UNREACHABLE  : 목표는 맵에 있으나 현재 상태에서 경로가 없음(계획 불가, R5).
- FAILED_BUDGET       : 재계획/스텝 예산 소진(반복 불일치로 목표에 못 닿음, M3).
- FAILED_DRIVER       : 드라이버/센스 도달 불가·오류(RemoteDriverError/ScreenSenseError).

의존 경계(M5)
-------------
이 엔진은 RemoteDriver / ScreenSense **추상**, NavGraph, identify(순수), goals(규칙),
engine.planner 에만 의존한다. MockRemoteDriver·RemoteMcpClient·MockScreenSense 등
**구체 구현체는 절대 임포트하지 않는다**(구현체 주입은 api/deps.py 의 책임).
아키텍처 임포트 검사가 이 불변식을 강제한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from remotectl.drivers.base import (
    RawScreen,
    RemoteDriver,
    RemoteDriverError,
)
from remotectl.engine.planner import Planner, PlanningError
from remotectl.goals import find_goal_state_id, resolve_goal
from remotectl.identify import resolve_state
from remotectl.models import (
    ExecutionResult,
    ExecutionStatus,
    Goal,
    PlanStep,
    ScreenState,
)
from remotectl.navmap import NavGraph

# 주의(M5): 추상 타입은 sense.base 에서 직접 임포트한다. 패키지 루트(remotectl.sense)는
# __init__ 에서 구체 구현체(MockScreenSense 등)를 재노출하므로, 그쪽을 임포트하면
# 실행기가 구현체에 전이적으로 결합된다(아키텍처 임포트 검사 위반).
from remotectl.sense.base import ScreenSense, ScreenSenseError, SenseResult

__all__ = ["Executor"]


def _utcnow() -> datetime:
    """models 와 동일한 tz-aware UTC 규약(naive 혼입 비교 버그 방지)."""
    return datetime.now(timezone.utc)


class Executor:
    """UC-3 목표기반 실행 엔진.

    사용 예:
        executor = Executor(driver, sense, graph, planner)
        result = executor.run_goal("넷플릭스 켜줘")
        if result.succeeded:
            ...

    파라미터
    --------
    - driver: 리모컨 제어 추상(press/capture/settle). 구체 구현은 주입받는다(M5).
    - sense: 화면 판정 추상(observe). 구체 구현은 주입받는다(M5).
    - graph: 계획·검증의 기준이 되는 NavGraph(맵 런타임 뷰). 실행 중 관찰은
      resolve_state 를 통해 이 맵에 반영된다(관측 누적).
    - planner: 최단경로를 PlanStep 시퀀스로 만드는 Planner. graph 와 같은 맵을 봐야 한다.
    - settle_ms: press 후 UI 안정화 대기(ms). R3(로딩/애니메이션) 대응. 0 이면 대기 안 함.
    - replan_budget: 스텝 불일치 시 허용하는 최대 재계획 횟수(무한 루프 방지, M3).
    """

    def __init__(
        self,
        driver: RemoteDriver,
        sense: ScreenSense,
        graph: NavGraph,
        planner: Planner,
        settle_ms: int = 400,
        replan_budget: int = 5,
    ) -> None:
        self.driver = driver
        self.sense = sense
        self.graph = graph
        self.planner = planner
        self.settle_ms = max(0, int(settle_ms))
        self.replan_budget = max(0, int(replan_budget))

    # --------------------------------------------------------------------- #
    # 공개 API
    # --------------------------------------------------------------------- #

    def run_goal(self, goal_text: str) -> ExecutionResult:
        """자연어 목표를 해석·계획·실행하고 정규화된 ExecutionResult 를 반환한다.

        절대로 예외를 던지지 않는다. 목표 해석 실패, 경로 없음, 예산 소진, 드라이버/센스
        오류는 모두 해당 ExecutionStatus 로 매핑되어 결과에 담긴다.

        단계
        ----
        1. resolve_goal: 자연어 → Goal(규칙 해석). 미해석이면 FAILED_UNRESOLVED.
        2. find_goal_state_id: Goal → 맵의 목표 상태 id. 미학습/미매핑이면 FAILED_UNRESOLVED.
        3. 현재 상태 관찰: capture→observe→resolve_state 로 시작 상태 확정.
           (드라이버/센스 오류면 FAILED_DRIVER.)
        4. plan: 현재→목표 최단경로. 이미 목표면 즉시 SUCCESS, 도달불가면 FAILED_UNREACHABLE.
        5. 실행·검증·재계획 루프: 스텝 실행→관찰→일치검증. 불일치 시 현재상태 기준 재계획
           (replan_budget 소진 시 FAILED_BUDGET). 목표 도달 시 SUCCESS.

        Args:
            goal_text: 사용자 자연어 목표(예: "넷플릭스 켜줘").

        Returns:
            ExecutionResult: status/goal/시작·종료 상태/실행 스텝/재계획 횟수/사유 메시지.
        """
        # --- 1. 목표 해석 ------------------------------------------------- #
        try:
            goal: Goal = resolve_goal(goal_text, self.graph.navmap)
        except Exception as exc:  # noqa: BLE001 — 해석기 예외도 결과로 정규화(계약: 예외 미전파).
            return self._fail_unresolved_raw(goal_text, f"목표 해석 중 오류: {exc}")

        if not goal.resolved:
            # goals 규칙이 이미 미해석/미매핑 사유를 resolve_note 에 담아 준다(R4).
            note = goal.resolve_note or "목표를 해석하지 못했습니다."
            return self._result(ExecutionStatus.FAILED_UNRESOLVED, goal, message=note)

        # --- 2. 목표 상태 매핑 ------------------------------------------- #
        try:
            goal_state_id = find_goal_state_id(goal, self.graph.navmap)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                ExecutionStatus.FAILED_UNRESOLVED,
                goal,
                message=f"목표 상태 매핑 중 오류: {exc}",
            )

        if not goal_state_id:
            return self._result(
                ExecutionStatus.FAILED_UNRESOLVED,
                goal,
                message=(
                    goal.resolve_note
                    or "목표에 해당하는 상태가 맵에 없습니다(미학습). 먼저 학습이 필요합니다."
                ),
            )

        # --- 3. 현재 상태 관찰(시작 상태 확정) --------------------------- #
        try:
            current: ScreenState = self._observe_current()
        except RemoteDriverError as exc:
            return self._result(
                ExecutionStatus.FAILED_DRIVER,
                goal,
                message=f"드라이버 오류(초기 관찰): {exc}",
            )
        except ScreenSenseError as exc:
            return self._result(
                ExecutionStatus.FAILED_DRIVER,
                goal,
                message=f"센스 오류(초기 관찰): {exc}",
            )

        start_state_id = current.id

        # --- 4. 초기 계획 ------------------------------------------------ #
        # 이미 목표 상태에 서 있으면 실행 없이 성공.
        if current.id == goal_state_id:
            return self._result(
                ExecutionStatus.SUCCESS,
                goal,
                start_state_id=start_state_id,
                final_state_id=current.id,
                message="이미 목표 상태에 있습니다.",
            )

        try:
            plan = self.planner.plan(current.id, goal_state_id)
        except PlanningError as exc:
            return self._result(
                ExecutionStatus.FAILED_UNREACHABLE,
                goal,
                start_state_id=start_state_id,
                final_state_id=current.id,
                message=f"경로 없음(계획 불가): {exc}",
            )

        # --- 5. 실행·검증·재계획 루프 ------------------------------------ #
        executed_steps: list[PlanStep] = []
        replans = 0
        step_index = 0  # 실행 궤적 상의 통짜 인덱스(재계획해도 단조 증가).

        while True:
            # 계획이 비었다 = 현재가 곧 목표(shortest_path 규약: from==to 이면 []).
            if not plan:
                if current.id == goal_state_id:
                    return self._result(
                        ExecutionStatus.SUCCESS,
                        goal,
                        start_state_id=start_state_id,
                        final_state_id=current.id,
                        steps=executed_steps,
                        replans=replans,
                        message="목표 상태에 도달했습니다.",
                    )
                # 계획은 비었는데 목표가 아님 = 재계획 필요(방어적).
                replans, plan, budget_exhausted = self._replan(
                    current.id, goal_state_id, replans
                )
                if budget_exhausted:
                    return self._budget_result(
                        goal, start_state_id, current.id, executed_steps, replans
                    )
                continue

            planned = plan.pop(0)

            # 계획 스텝의 from 이 현재와 어긋나면(재계획 직전 상태 변화 등) 재계획.
            if planned.from_state_id != current.id:
                replans, plan, budget_exhausted = self._replan(
                    current.id, goal_state_id, replans
                )
                if budget_exhausted:
                    return self._budget_result(
                        goal, start_state_id, current.id, executed_steps, replans
                    )
                continue

            # --- 스텝 실행 + 관찰 ---------------------------------------- #
            try:
                result = self._press_and_observe(planned.key)
            except RemoteDriverError as exc:
                # 여기까지 실행한 궤적은 보존해 진단 가능하게 한다.
                self._mark_unexecuted(planned)
                return self._result(
                    ExecutionStatus.FAILED_DRIVER,
                    goal,
                    start_state_id=start_state_id,
                    final_state_id=current.id,
                    steps=executed_steps,
                    replans=replans,
                    message=f"드라이버 오류(실행 중): {exc}",
                )
            except ScreenSenseError as exc:
                self._mark_unexecuted(planned)
                return self._result(
                    ExecutionStatus.FAILED_DRIVER,
                    goal,
                    start_state_id=start_state_id,
                    final_state_id=current.id,
                    steps=executed_steps,
                    replans=replans,
                    message=f"센스 오류(실행 중): {exc}",
                )

            # 관찰 → 맵에 반영(관측 누적) → 실제 도달 상태 확정.
            actual = resolve_state(result, self.graph)
            # 관측한 전이도 맵에 기록(실행이 곧 새 학습이기도 함).
            self.graph.observe_transition(current, planned.key, actual)

            # 실행 스텝 기록(궤적). 인덱스는 통짜로 부여.
            executed_step = self._executed_step(
                index=step_index,
                from_state_id=current.id,
                planned=planned,
                actual_to_state_id=actual.id,
                observed_confidence=result.state.confidence,
            )
            executed_steps.append(executed_step)
            step_index += 1

            # 현재 상태를 실제 도달 상태로 갱신.
            current = actual

            # --- 목표 도달 검사(계획의 기대와 무관하게, 실제 상태 기준) ---- #
            if current.id == goal_state_id:
                return self._result(
                    ExecutionStatus.SUCCESS,
                    goal,
                    start_state_id=start_state_id,
                    final_state_id=current.id,
                    steps=executed_steps,
                    replans=replans,
                    message="목표 상태에 도달했습니다.",
                )

            # --- 검증: 기대대로 전이했는가? ------------------------------ #
            if executed_step.matched:
                # 기대와 일치 → 남은 계획을 그대로 이어서 실행.
                continue

            # 불일치(R3) → 지금 실제 상태를 기준으로 목표까지 재계획.
            replans, plan, budget_exhausted = self._replan(
                current.id, goal_state_id, replans
            )
            if budget_exhausted:
                return self._budget_result(
                    goal, start_state_id, current.id, executed_steps, replans
                )
            # 재계획된 plan 으로 루프 계속.

    # --------------------------------------------------------------------- #
    # 재계획
    # --------------------------------------------------------------------- #

    def _replan(
        self, current_id: str, goal_state_id: str, replans: int
    ) -> tuple[int, list[PlanStep], bool]:
        """현재 상태를 기준으로 목표까지 재계획한다.

        재계획 예산(replan_budget)을 넘어서면 budget_exhausted=True 를 돌려 호출자가
        FAILED_BUDGET 로 마무리하게 한다. 도달 불가(PlanningError)도 재계획으로는 목표에
        닿을 수 없으므로 예산 소진과 동일하게(budget_exhausted=True) 처리한다 — 이 경우
        상위는 이미 최소 한 번 시도했으므로 예산 실패로 정규화하는 편이 결과 해석에 일관적이다.

        Args:
            current_id: 재계획의 출발이 되는 현재 상태 id.
            goal_state_id: 목표 상태 id.
            replans: 지금까지의 재계획 횟수.

        Returns:
            (갱신된 replans, 새 계획, budget_exhausted) 튜플.
            budget_exhausted=True 면 새 계획은 빈 리스트다.
        """
        if replans >= self.replan_budget:
            return replans, [], True

        replans += 1
        try:
            new_plan = self.planner.plan(current_id, goal_state_id)
        except PlanningError:
            # 현재 상태에서 목표로 갈 길이 없다 → 더 시도해도 소용없음.
            return replans, [], True
        return replans, new_plan, False

    # --------------------------------------------------------------------- #
    # 드라이버/센스 상호작용(경계)
    # --------------------------------------------------------------------- #

    def _observe_current(self) -> ScreenState:
        """현재 화면을 캡처·판정·확정한다(초기 관찰; 전이 기록 없음).

        RemoteDriverError / ScreenSenseError 는 그대로 전파(호출자가 status 로 분류).
        """
        return resolve_state(self._capture_and_observe(), self.graph)

    def _press_and_observe(self, key) -> SenseResult:
        """press → settle → capture → observe. 실행 루프의 한 스텝 관찰.

        전이 기록/상태 확정은 호출자가 담당한다. 드라이버/센스 예외는 그대로 전파.
        """
        self.driver.press(key)
        if self.settle_ms:
            self.driver.settle(self.settle_ms)
        return self._capture_and_observe()

    def _capture_and_observe(self) -> SenseResult:
        """capture → observe(판정). 드라이버/센스 예외는 그대로 전파."""
        raw: RawScreen = self.driver.capture()
        return self.sense.observe(raw)

    # --------------------------------------------------------------------- #
    # PlanStep / 결과 조립 헬퍼
    # --------------------------------------------------------------------- #

    @staticmethod
    def _executed_step(
        *,
        index: int,
        from_state_id: str,
        planned: PlanStep,
        actual_to_state_id: str,
        observed_confidence: Optional[float],
    ) -> PlanStep:
        """실행 후 채워진 PlanStep 을 만든다(계획 스텝을 변형하지 않고 새 인스턴스로).

        matched 는 계획의 expected 와 실제 도달 상태의 동일성으로 판정한다.
        """
        return PlanStep(
            index=index,
            from_state_id=from_state_id,
            key=planned.key,
            expected_to_state_id=planned.expected_to_state_id,
            executed=True,
            actual_to_state_id=actual_to_state_id,
            matched=(actual_to_state_id == planned.expected_to_state_id),
            observed_confidence=observed_confidence,
        )

    @staticmethod
    def _mark_unexecuted(planned: PlanStep) -> None:
        """드라이버 오류로 실제 실행에 실패한 계획 스텝은 궤적에 넣지 않는다(no-op 표식).

        현재는 부작용 없는 표식용 훅이다(궤적에는 executed=True 스텝만 append 하므로,
        실패 스텝은 단순히 누락한다). 장래 부분 실행 진단이 필요하면 여기서 확장한다.
        """
        return None

    def _budget_result(
        self,
        goal: Goal,
        start_state_id: Optional[str],
        final_state_id: Optional[str],
        steps: list[PlanStep],
        replans: int,
    ) -> ExecutionResult:
        """재계획/스텝 예산 소진 실패 결과를 조립한다(M3)."""
        return self._result(
            ExecutionStatus.FAILED_BUDGET,
            goal,
            start_state_id=start_state_id,
            final_state_id=final_state_id,
            steps=steps,
            replans=replans,
            message=(
                f"재계획 예산 소진(replans={replans}/{self.replan_budget}): "
                "반복된 불일치 또는 경로 상실로 목표에 도달하지 못했습니다."
            ),
        )

    def _fail_unresolved_raw(self, goal_text: str, message: str) -> ExecutionResult:
        """Goal 조차 만들지 못한 경우(해석기 예외)의 미해석 결과.

        Goal 모델은 target 필드가 필수라 유효 인스턴스를 만들 수 없을 수 있으므로,
        규칙 해석 실패를 나타내는 최소 GOTO_KIND(HOME) placeholder Goal 로 감싼다.
        (status 가 FAILED_UNRESOLVED 이므로 target 의 의미는 사용되지 않는다.)
        """
        from remotectl.models import GoalType, StateKind

        placeholder = Goal(
            raw_text=goal_text[:500] if goal_text else "(빈 목표)",
            goal_type=GoalType.GOTO_KIND,
            target_kind=StateKind.HOME,
            resolved=False,
            resolve_note=message[:500],
        )
        return self._result(
            ExecutionStatus.FAILED_UNRESOLVED, placeholder, message=message
        )

    @staticmethod
    def _result(
        status: ExecutionStatus,
        goal: Goal,
        *,
        start_state_id: Optional[str] = None,
        final_state_id: Optional[str] = None,
        steps: Optional[list[PlanStep]] = None,
        replans: int = 0,
        message: Optional[str] = None,
    ) -> ExecutionResult:
        """ExecutionResult 를 일관되게 조립한다(finished_at 자동 채움)."""
        return ExecutionResult(
            status=status,
            goal=goal,
            start_state_id=start_state_id,
            final_state_id=final_state_id,
            steps=list(steps) if steps else [],
            replans=replans,
            finished_at=_utcnow(),
            message=message[:1000] if message else None,
        )

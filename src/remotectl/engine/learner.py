"""UC-1 탐색 학습 엔진 — 버튼 입력→화면 전이를 관찰하며 네비게이션 맵을 구축한다.

역할
----
셋탑박스를 리모컨으로 "스스로 조작해 보며" 화면 상태 그래프를 채우는 능동 탐색기다.
매 스텝은 다음 결정론적 루프를 돈다:

    select_key → press → settle → capture → observe(판정) → resolve_state → observe_transition

- select_key: 현재 상태에서 "아직 눌러보지 않은 버튼(미탐색 간선)"을 우선 고르는 정책.
- press/settle/capture: RemoteDriver 경계(전송)로 실제 입력·안정화·화면 원재료 획득.
- observe: ScreenSense 경계(판정)로 RawScreen → SenseResult(정규화 signature/id 파생).
- resolve_state: identify 계층이 SenseResult 를 맵 상태로 확정(upsert, 같은 화면 자동 수렴).
- observe_transition: NavGraph 에 (from,key,to) 전이를 기록(맵의 정본을 채운다).

탐색 정책(미탐색 간선 우선 BFS 성향)
------------------------------------
목표는 "적은 스텝으로 넓은 커버리지"다. 그래서 현재 상태에서 아직 시도하지 않은 버튼이
있으면 그것부터 소진한다(지역적 완전 탐색). 미탐색 간선이 없으면(현재 상태를 다 훑었으면)
아직 미탐색 간선이 남은 이미 알려진 상태 쪽으로 "이동"하기 위해, 기존 전이 중 하나를 눌러
프런티어로 이동한다. 이로써 전역적으로는 BFS 성향(가까운 미탐색부터)의 탐색이 된다.
정지 위험(막다른 상태/사이클)을 완화하기 위해, 진행이 없으면 reset() 으로 루트 복귀한다.

의존 경계(M5)
-------------
이 엔진은 RemoteDriver / ScreenSense **추상**과 NavGraph / identify(순수) 계층에만 의존한다.
MockRemoteDriver·RemoteMcpClient·MockScreenSense 등 **구체 구현체는 절대 임포트하지 않는다**.
구현체 주입은 상위(api/deps.py)의 책임이다. (아키텍처 임포트 검사가 이를 강제한다.)

종료 조건(M4)
-------------
- step_budget 소진, 또는
- coverage_target 도달(NavGraph.coverage(button_set) >= target), 또는
- 더 탐색할 미탐색 간선이 전혀 없음(맵 포화), 또는
- 드라이버/센스 도달 불가(오류 시 안전 종료; 예외를 삼키지 않고 요약에 사유 기록).
"""

from __future__ import annotations

import time
from typing import Optional

from remotectl.drivers.base import (
    HOME_KEY,
    CaptureError,
    DriverUnavailableError,
    PressError,
    RawScreen,
    RemoteDriver,
    RemoteDriverError,
)
from remotectl.identify import resolve_state
from remotectl.models import (
    Button,
    KeyPress,
    LearningSummary,
    ScreenState,
)
from remotectl.navmap import NavGraph
# 주의(M5): 추상 타입은 sense.base 에서 직접 임포트한다. 패키지 루트(remotectl.sense)는
# __init__ 에서 MockScreenSense/DetectionMcpScreenSense(구체 구현체)를 재노출·즉시 임포트하므로,
# 그쪽을 임포트하면 학습기가 구현체에 전이적으로 결합된다(아키텍처 임포트 검사 위반).
from remotectl.sense.base import ScreenSense, ScreenSenseError

__all__ = ["Learner", "DEFAULT_BUTTON_SET"]


# --------------------------------------------------------------------------- #
# 기본 탐색 버튼 집합
# --------------------------------------------------------------------------- #
#
# 탐색에 쓸 후보 키. STB UI 탐색에서 실제로 화면 전이를 만드는 방향키/확정/뒤로가기를
# 핵심으로 둔다. 볼륨/전원/숫자 등은 화면 그래프 탐색에 노이즈이므로 기본 집합에서 제외한다.
# (사업자·단말별로 필요하면 button_set 인자로 주입해 확장/축소한다.)
DEFAULT_BUTTON_SET: list[KeyPress] = [
    KeyPress(button=Button.RIGHT),
    KeyPress(button=Button.LEFT),
    KeyPress(button=Button.DOWN),
    KeyPress(button=Button.UP),
    KeyPress(button=Button.OK),
    KeyPress(button=Button.BACK),
    KeyPress(button=Button.MENU),
]


class Learner:
    """UC-1 탐색 학습 엔진.

    사용 예:
        learner = Learner(driver, sense, graph)
        summary = learner.learn(step_budget=200, coverage_target=0.9)

    파라미터
    --------
    - driver: 리모컨 제어 추상(press/capture/reset). 구체 구현은 주입받는다(M5).
    - sense: 화면 판정 추상(observe). 구체 구현은 주입받는다(M5).
    - graph: 학습 결과를 채울 NavGraph(맵 런타임 뷰). 이 객체를 in-place 로 갱신한다.
    - button_set: 탐색 후보 키. None 이면 DEFAULT_BUTTON_SET 사용.
    - settle_ms: press 후 UI 안정화 대기(ms). R3(로딩/애니메이션) 대응.
    """

    def __init__(
        self,
        driver: RemoteDriver,
        sense: ScreenSense,
        graph: NavGraph,
        button_set: Optional[list[KeyPress]] = None,
        settle_ms: int = 400,
    ) -> None:
        self.driver = driver
        self.sense = sense
        self.graph = graph
        # None 또는 빈 리스트면 기본 집합. 리스트는 방어적으로 복사한다.
        self.button_set: list[KeyPress] = (
            list(button_set) if button_set else list(DEFAULT_BUTTON_SET)
        )
        self.settle_ms = max(0, int(settle_ms))

    # --------------------------------------------------------------------- #
    # 공개 API
    # --------------------------------------------------------------------- #

    def learn(
        self,
        step_budget: int = 200,
        coverage_target: float = 0.9,
        session_id: Optional[str] = None,
    ) -> LearningSummary:
        """탐색 학습 세션을 실행하고 종료 집계(LearningSummary)를 반환한다.

        루프: reset → 초기 관찰 → (미탐색 간선 우선) 스텝 반복 → 종료 조건 → 요약.

        - step_budget: 최대 press 시도 횟수(0 이하이면 초기 관찰만 하고 종료).
        - coverage_target: NavGraph.coverage(button_set) 가 이 값 이상이면 조기 종료(M4).
        - session_id: 세션 식별자. None 이면 시각 기반으로 자동 생성.

        예외는 던지지 않는다: 드라이버/센스 오류는 안전 종료 후 stop_reason 에 사유를 남긴다.
        (다만 어떤 관찰도 못 한 완전 실패는 그대로 예외를 전파해 상위가 인지하게 한다.)
        """
        sid = session_id or f"learn-{int(time.time() * 1000)}"
        started = _utcnow()
        steps_taken = 0
        stop_reason: Optional[str] = None

        # --- 초기화: 알려진 시작점 복귀 + 최초 화면 관찰 ------------------- #
        try:
            self.driver.reset()
            current = self._observe_current()
        except (RemoteDriverError, ScreenSenseError):
            # 초기 관찰조차 실패하면 학습을 시작할 수 없다 — 상위가 알도록 전파.
            raise

        # 루트 상태 힌트 기록(맵에 아직 root 가 없을 때만).
        if self.graph.navmap.root_state_id is None:
            self.graph.navmap.root_state_id = current.id

        target = self._clamp01(coverage_target)

        # --- 탐색 루프 --------------------------------------------------- #
        while steps_taken < step_budget:
            # 조기 종료: 커버리지 목표 달성.
            if self._coverage() >= target:
                stop_reason = f"커버리지 목표 도달({self._coverage():.3f} >= {target:.3f})"
                break

            key = self._select_key(current.id)
            if key is None:
                # 어디에도 미탐색 간선이 없음 = 맵 포화(탐색 완료).
                stop_reason = "미탐색 간선 없음(맵 포화)"
                break

            try:
                self.driver.press(key)
                if self.settle_ms:
                    self.driver.settle(self.settle_ms)
                result = self._capture_and_observe()
            except DriverUnavailableError as exc:
                stop_reason = f"드라이버 도달 불가: {exc}"
                break
            except (PressError, CaptureError, RemoteDriverError) as exc:
                # 개별 입력/캡처 실패는 안전 종료 사유로 기록(엔진 크래시 방지).
                stop_reason = f"드라이버 오류: {exc}"
                break
            except ScreenSenseError as exc:
                stop_reason = f"센스 오류: {exc}"
                break

            steps_taken += 1

            # SenseResult → 맵 상태 확정(upsert; 같은 화면 자동 수렴).
            to_state = resolve_state(result, self.graph)
            # 전이 기록: (current, key) -> to_state. self-loop(무전이)도 유효한 관측이다.
            self.graph.observe_transition(current, key, to_state)

            # 현재 상태 갱신 후 다음 스텝.
            current = to_state

            # 진행이 막힌 경우(현재도 프런티어도 아님) 대비: _select_key 가 이미
            # 프런티어 이동/포화를 처리하므로 여기서는 별도 복귀 로직이 필요없다.

        else:
            # while 정상 소진(break 없이 예산 도달).
            stop_reason = f"스텝 예산 소진({steps_taken}/{step_budget})"

        return self._summarize(
            session_id=sid,
            steps_taken=steps_taken,
            started_at=started,
            stop_reason=stop_reason,
        )

    def _select_key(self, current_state_id: str) -> Optional[KeyPress]:
        """다음에 누를 키를 고른다(미탐색 간선 우선 정책).

        우선순위
        --------
        1) 현재 상태에서 아직 시도하지 않은 버튼(미탐색 간선)이 있으면 그중 첫 번째.
           (button_set 순서를 정책 우선순위로 사용 — 방향키 먼저.)
        2) 없으면(현재 상태를 다 훑었으면), 아직 미탐색 간선이 남은 다른 상태로 이동하기
           위해 현재 상태의 알려진 전이 중 하나를 눌러 프런티어 방향으로 나아간다(BFS 성향).
        3) 어디에도 미탐색 간선이 남지 않았으면 None(탐색 완료 신호).

        반환 None 은 상위 루프의 종료 조건이 된다.
        """
        # 1) 현재 상태의 미탐색 간선.
        local_unexplored = self.graph.unexplored(current_state_id, self.button_set)
        if local_unexplored:
            return self._first_by_policy(local_unexplored)

        # 전역적으로 미탐색 간선이 하나도 없으면 포화.
        if not self._has_any_frontier():
            return None

        # 2) 프런티어로 이동: 현재 상태에서 나가는 알려진 전이 하나를 눌러 다른 상태로.
        #    (self-loop 전이는 이동에 도움이 안 되므로 제외한다.)
        for transition in self.graph.outgoing(current_state_id):
            if transition.to_state_id != current_state_id:
                return transition.key

        # 나갈 수 있는 전이가 self-loop 뿐이거나 없음 = 막다른 상태.
        # 루트로 복귀하는 편이 낫지만, 드라이버 부작용은 press 로만 내야 하므로
        # HOME 키를 눌러 (알려졌든 아니든) 홈 쪽으로 나가게 한다.
        home = HOME_KEY
        # HOME 을 이미 이 상태에서 시도해 self-loop 로 확인됐다면 더 할 게 없다.
        if any(
            t.key.token == home.token and t.to_state_id == current_state_id
            for t in self.graph.outgoing(current_state_id)
        ):
            # 이 상태에서는 어떤 키로도 벗어날 수 없다고 관측됨. 그래도 프런티어는
            # 남아있으므로(위 _has_any_frontier True), HOME 재시도로 탈출을 노린다.
            return home
        return home

    # --------------------------------------------------------------------- #
    # 내부 헬퍼
    # --------------------------------------------------------------------- #

    def _first_by_policy(self, candidates: list[KeyPress]) -> KeyPress:
        """미탐색 후보들을 button_set 정책 순서로 정렬해 첫 번째를 고른다.

        NavGraph.unexplored 의 반환 순서에 의존하지 않고, 이 엔진의 우선순위
        (button_set 순서)를 명시적으로 적용한다(방향키 우선의 결정성 확보).
        """
        order = {kp.token: i for i, kp in enumerate(self.button_set)}
        return min(candidates, key=lambda kp: order.get(kp.token, len(order)))

    def _has_any_frontier(self) -> bool:
        """맵의 어떤 알려진 상태에라도 미탐색 간선이 남아있는지."""
        for state_id in self.graph.navmap.states:
            if self.graph.unexplored(state_id, self.button_set):
                return True
        return False

    def _observe_current(self) -> ScreenState:
        """현재 화면을 캡처·판정·확정한다(초기 관찰용; 전이 기록 없음)."""
        return resolve_state(self._capture_and_observe(), self.graph)

    def _capture_and_observe(self):
        """capture → observe. (전이 기록/맵 확정은 호출자 책임.)

        반환: SenseResult. 드라이버/센스 예외는 그대로 전파(호출자가 분류).
        """
        raw: RawScreen = self.driver.capture()
        return self.sense.observe(raw)

    def _coverage(self) -> float:
        """현재 맵의 커버리지 비율(M4). NavGraph 에 위임."""
        return self.graph.coverage(self.button_set)

    def _count_unexplored_edges(self) -> int:
        """모든 알려진 상태에 걸쳐 남은 미탐색 (state,button) 간선 총수."""
        total = 0
        for state_id in self.graph.navmap.states:
            total += len(self.graph.unexplored(state_id, self.button_set))
        return total

    def _summarize(
        self,
        *,
        session_id: str,
        steps_taken: int,
        started_at,
        stop_reason: Optional[str],
    ) -> LearningSummary:
        """세션 종료 집계를 조립한다."""
        navmap = self.graph.navmap
        try:
            coverage_ratio: Optional[float] = self._clamp01(self._coverage())
        except Exception:  # noqa: BLE001 — 집계 실패가 요약 자체를 깨지 않게.
            coverage_ratio = None

        return LearningSummary(
            session_id=session_id,
            steps_taken=steps_taken,
            states_visited=len(navmap.states),
            transitions_recorded=len(navmap.transitions),
            unexplored_edges=self._count_unexplored_edges(),
            coverage_ratio=coverage_ratio,
            started_at=started_at,
            finished_at=_utcnow(),
            stop_reason=stop_reason,
        )

    @staticmethod
    def _clamp01(value: float) -> float:
        """0.0~1.0 로 클램프(Confidence 필드 검증 실패 방지)."""
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return float(value)


# 요약 타임스탬프용(모델과 동일 tz-aware UTC 규약).
def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)

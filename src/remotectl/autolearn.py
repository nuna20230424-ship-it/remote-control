"""AutoLearner — 목표 커버리지까지 학습을 자동 반복하는 오케스트레이터(별도 자동화 시스템).

Learner 를 여러 라운드 재실행하며, 매 라운드 커버리지를 측정해:
  - 목표 커버리지 도달 → 성공 종료.
  - K라운드 연속 진전(커버리지/상태/전이) 없음 → 정체 종료(미커버 키/상태 리포트).
  - 최대 라운드 상한 → 종료.

100% 는 미발견 상태·도달불가 키 때문에 항상 보장되지 않으므로(오라클 한계), 목표 커버리지를
파라미터로 받고 "무엇이 왜 안 됐는지"를 리포트로 노출한다(silent 미달 방지). 라운드마다
Learner._skipped 가 초기화되어 일시 오류 키는 다음 라운드에서 재치유를 시도한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from remotectl.engine.learner import Learner

__all__ = ["AutoLearner", "AutoLearnReport", "RoundResult"]


@dataclass(slots=True)
class RoundResult:
    """한 라운드 결과."""

    round: int
    coverage: float
    states: int
    transitions: int
    steps_taken: int
    stop_reason: str | None


@dataclass(slots=True)
class AutoLearnReport:
    """자동 학습 종료 리포트."""

    reached: bool
    target_coverage: float
    final_coverage: float
    rounds: int
    stop_cause: str  # "target" | "no_progress" | "max_rounds"
    key_set_source: str
    uncovered_key_tokens: list[str]
    uncovered_states: list[dict]
    history: list[RoundResult] = field(default_factory=list)
    store_stats: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class AutoLearner:
    """Learner 를 목표 커버리지까지 자동 반복 실행한다."""

    def __init__(self, learner: Learner):
        self.learner = learner

    def run(
        self,
        *,
        target_coverage: float = 1.0,
        step_budget: int = 200,
        max_rounds: int = 20,
        no_progress_rounds: int = 2,
    ) -> AutoLearnReport:
        """자동 학습 루프.

        Args:
            target_coverage: 목표 커버리지(0~1). 도달 시 성공 종료.
            step_budget: 라운드당 최대 스텝.
            max_rounds: 최대 라운드 수(안전 상한).
            no_progress_rounds: 연속 무진전 이때 도달하면 정체 종료.

        Returns:
            AutoLearnReport (도달 여부·최종 커버리지·미커버 키/상태·라운드 이력).
        """
        target = max(0.0, min(1.0, float(target_coverage)))
        max_rounds = max(1, int(max_rounds))
        no_progress_rounds = max(1, int(no_progress_rounds))

        graph = self.learner.graph
        button_set = self.learner.button_set
        history: list[RoundResult] = []
        reached = False
        stop_cause = "max_rounds"
        stale = 0
        prev_sig: tuple | None = None
        coverage = graph.coverage(button_set)

        for r in range(1, max_rounds + 1):
            summary = self.learner.learn(
                step_budget=step_budget, coverage_target=target, session_id=f"auto-{r}"
            )
            coverage = graph.coverage(button_set)
            states = len(graph.navmap.states)
            transitions = len(graph.navmap.transitions)
            history.append(RoundResult(
                round=r, coverage=coverage, states=states, transitions=transitions,
                steps_taken=summary.steps_taken, stop_reason=summary.stop_reason,
            ))

            if coverage >= target:
                reached = True
                stop_cause = "target"
                break

            sig = (round(coverage, 6), states, transitions)
            if sig == prev_sig:
                stale += 1
                if stale >= no_progress_rounds:
                    stop_cause = "no_progress"
                    break
            else:
                stale = 0
            prev_sig = sig

        report = graph.coverage_report(button_set)
        uncovered_tokens = sorted(
            tok for tok, pk in report["per_key"].items() if pk["ratio"] < 1.0
        )
        store_stats = None
        observer = getattr(self.learner, "observer", None)
        if observer is not None and hasattr(observer, "stats"):
            try:
                store_stats = observer.stats()
            except Exception:  # noqa: BLE001 — 통계 실패가 리포트를 깨지 않게.
                store_stats = None

        return AutoLearnReport(
            reached=reached,
            target_coverage=target,
            final_coverage=coverage,
            rounds=len(history),
            stop_cause=stop_cause,
            key_set_source=self.learner.key_set_source,
            uncovered_key_tokens=uncovered_tokens,
            uncovered_states=report["uncovered"],
            history=history,
            store_stats=store_stats,
        )

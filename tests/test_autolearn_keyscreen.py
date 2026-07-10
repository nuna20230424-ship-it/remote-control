"""동시 학습(키↔화면) · 별도 DB · 오류 자가치유 · 목표 커버리지 자동 반복 테스트.

- KeyScreenStore(SQLite): 키이벤트↔화면 매핑 + 화면 LLM 분석 + 오류 로깅.
- Learner+observer: 스텝마다 키↔화면을 동시에 별도 DB에 기록.
- 오류 자가치유: 일시 판정 실패 → REOBSERVE 재판정으로 복구(reconciled).
- PolicyErrorAnalyzer: 단계별 복구 정책.
- AutoLearner: 목표 커버리지 도달/정체 판단 + 미커버 리포트(100% 미달 비-silent).
"""

from __future__ import annotations

from remotectl.autolearn import AutoLearner
from remotectl.drivers.base import RawScreen
from remotectl.drivers.mock import MockRemoteDriver
from remotectl.engine.hooks import (
    ErrorContext,
    ErrorPhase,
    Remediation,
    StepRecord,
)
from remotectl.engine.learner import Learner
from remotectl.navmap import NavGraph
from remotectl.reconcile import PolicyErrorAnalyzer
from remotectl.sense.base import ScreenSense, SenseUnavailableError
from remotectl.sense.mock import MockScreenSense
from remotectl.store import KeyScreenStore


# --------------------------------------------------------------------------- #
# KeyScreenStore (SQLite, in-memory)
# --------------------------------------------------------------------------- #


def _rec(from_id="st_home", key="RIGHT", to_id="st_nf", reconciled=False):
    return StepRecord(
        session_id="s1", from_state_id=from_id, from_signature="sig:" + from_id,
        key_token=key, to_state_id=to_id, to_signature="sig:" + to_id,
        to_kind="app", to_app_id="netflix", analysis="넷플릭스 앱 화면",
        confidence=0.9, reconciled=reconciled,
    )


def test_store_records_screen_and_mapping():
    store = KeyScreenStore(":memory:")
    store.on_step(_rec())
    store.on_step(_rec())  # 같은 (from,key) → observed 증가, 중복 아님
    stats = store.stats()
    assert stats["mappings"] == 1
    assert stats["screens"] == 1
    m = store.mappings()[0]
    assert m["observed"] == 2
    assert m["to_app_id"] == "netflix"
    assert m["analysis"] == "넷플릭스 앱 화면"
    assert store.screens()[0]["analysis"] == "넷플릭스 앱 화면"
    store.close()


def test_store_logs_error_and_marks_mapping():
    store = KeyScreenStore(":memory:")
    store.on_step(_rec())
    from remotectl.engine.hooks import RemediationDecision
    ctx = ErrorContext(ErrorPhase.OBSERVE, "st_home", "RIGHT", "판정 실패", attempt=1)
    store.on_error(ctx, RemediationDecision(Remediation.SKIP, "포기"))
    stats = store.stats()
    assert stats["errors"] == 1
    assert stats["error_mappings"] == 1  # 해당 매핑 status=error
    store.close()


def test_store_tracks_reconciled():
    store = KeyScreenStore(":memory:")
    store.on_step(_rec(reconciled=True))
    assert store.stats()["reconciled"] == 1
    store.close()


# --------------------------------------------------------------------------- #
# PolicyErrorAnalyzer
# --------------------------------------------------------------------------- #


def test_policy_press_retries_then_stops():
    a = PolicyErrorAnalyzer(max_attempts=2)
    assert a.analyze(ErrorContext(ErrorPhase.PRESS, "s", "OK", "x", attempt=0)).action is Remediation.RETRY
    assert a.analyze(ErrorContext(ErrorPhase.PRESS, "s", "OK", "x", attempt=2)).action is Remediation.STOP


def test_policy_observe_reobserves_then_skips():
    a = PolicyErrorAnalyzer(max_attempts=2)
    assert a.analyze(ErrorContext(ErrorPhase.OBSERVE, "s", "OK", "x", attempt=0)).action is Remediation.REOBSERVE
    assert a.analyze(ErrorContext(ErrorPhase.OBSERVE, "s", "OK", "x", attempt=2)).action is Remediation.SKIP


def test_policy_low_confidence_reobserves():
    a = PolicyErrorAnalyzer(max_attempts=1)
    d = a.analyze(ErrorContext(ErrorPhase.LOW_CONFIDENCE, "s", "OK", "low", attempt=0, confidence=0.2))
    assert d.action is Remediation.REOBSERVE


# --------------------------------------------------------------------------- #
# 동시 학습 — 키↔화면을 별도 DB에 함께 기록
# --------------------------------------------------------------------------- #


def test_learn_records_keyscreen_to_store():
    store = KeyScreenStore(":memory:")
    learner = Learner(
        MockRemoteDriver(), MockScreenSense(), NavGraph(), settle_ms=0,
        observer=store, analyzer=PolicyErrorAnalyzer(),
    )
    learner.learn(step_budget=200, coverage_target=0.99)
    stats = store.stats()
    assert stats["mappings"] > 0, "키이벤트↔화면 매핑이 기록돼야 한다"
    assert stats["screens"] > 0, "화면(LLM 분석)이 기록돼야 한다"
    # 화면 분석 텍스트(라벨)가 실제로 저장됐는지.
    assert any(sc["analysis"] for sc in store.screens())
    store.close()


# --------------------------------------------------------------------------- #
# 오류 자가치유 — 일시 판정 실패 후 REOBSERVE 로 복구
# --------------------------------------------------------------------------- #


class _FlakySense(ScreenSense):
    """지정한 observe 호출 인덱스에서 한 번 SenseUnavailableError 를 던지는 래퍼."""

    def __init__(self, inner: ScreenSense, fail_on: set[int]):
        self._inner = inner
        self._fail_on = set(fail_on)
        self._calls = 0

    def observe(self, raw: RawScreen):
        self._calls += 1
        if self._calls in self._fail_on:
            raise SenseUnavailableError(f"일시 판정 실패(call {self._calls})")
        return self._inner.observe(raw)

    @property
    def backend_name(self) -> str:
        return f"flaky:{self._inner.backend_name}"


def test_self_heal_recovers_transient_sense_failure():
    store = KeyScreenStore(":memory:")
    # call #1 = 초기 관찰(성공해야 학습 시작), call #2 = 첫 스텝 판정(실패) → REOBSERVE 복구.
    sense = _FlakySense(MockScreenSense(), fail_on={2})
    learner = Learner(
        MockRemoteDriver(), sense, NavGraph(), settle_ms=0,
        observer=store, analyzer=PolicyErrorAnalyzer(max_attempts=3),
    )
    summary = learner.learn(step_budget=50, coverage_target=0.99)
    assert summary.states_visited >= 1  # 학습이 중단되지 않고 진행됨
    stats = store.stats()
    assert stats["errors"] >= 1, "오류가 기록돼야 한다"
    assert stats["reconciled"] >= 1, "자가치유로 정정된 매핑이 있어야 한다"
    store.close()


# --------------------------------------------------------------------------- #
# AutoLearner — 목표 커버리지 자동 반복 + 리포트
# --------------------------------------------------------------------------- #


def _auto(target, store=None):
    store = store or KeyScreenStore(":memory:")
    learner = Learner(
        MockRemoteDriver(), MockScreenSense(), NavGraph(), settle_ms=0,
        observer=store, analyzer=PolicyErrorAnalyzer(),
    )
    return AutoLearner(learner).run(target_coverage=target, step_budget=200, max_rounds=6), store


def test_autolearn_reaches_modest_target():
    report, store = _auto(0.5)
    assert report.reached is True
    assert report.stop_cause == "target"
    assert report.final_coverage >= 0.5
    assert report.store_stats is not None and report.store_stats["mappings"] > 0
    store.close()


def test_autolearn_reports_uncovered_when_target_unreachable():
    # mock 토폴로지상 7키 100% 는 도달 못 함 → 정체 종료 + 미커버 리포트(비-silent).
    report, store = _auto(1.0)
    assert report.reached is False
    assert report.stop_cause in ("no_progress", "max_rounds")
    assert report.final_coverage < 1.0
    assert len(report.uncovered_key_tokens) > 0, "미커버 키가 리포트에 노출돼야 한다"
    assert report.uncovered_states, "미커버 상태가 리포트에 노출돼야 한다"
    store.close()

"""LLM(VLM) 오류 인식 → 재판정 자가치유 테스트 (#1·#2·#3).

- #1 detection observe 가 verdict(normal/anomaly)를 SenseResult 에 실어 보낸다.
- #2 Learner 가 verdict==anomaly 를 "오류 인식"으로 삼아 재판정(REOBSERVE)을 유발한다.
- #3 VlmErrorAnalyzer 가 VLM 판정(anomaly/저신뢰)을 근거로 복구를 결정한다.
- 통합: 이상 판정 → 재판정 → 정상 복구 → 정상 매핑 학습(reconciled).
"""

from __future__ import annotations

import httpx

from remotectl.drivers.base import RawScreen
from remotectl.drivers.mock import MockRemoteDriver
from remotectl.engine.hooks import ErrorContext, ErrorPhase, Remediation
from remotectl.engine.learner import Learner
from remotectl.navmap import NavGraph
from remotectl.reconcile import VlmErrorAnalyzer
from remotectl.sense.base import ScreenSense, SenseResult
from remotectl.sense.detection_mcp import DetectionMcpScreenSense
from remotectl.sense.mock import MockScreenSense
from remotectl.store import KeyScreenStore


# --------------------------------------------------------------------------- #
# #1 detection observe → verdict 전달
# --------------------------------------------------------------------------- #


def _detect(verdict: str, confidence=0.9):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "verdict": verdict, "tier": "vision", "confidence": confidence,
            "description": "넷플릭스 앱 홈 화면",
        })
    client = httpx.Client(base_url="http://det.test", transport=httpx.MockTransport(handler))
    return DetectionMcpScreenSense("http://det.test", client=client)


def test_observe_carries_verdict():
    r = _detect("anomaly").observe(RawScreen(image_ref="x", image_bytes=b"png"))
    assert r.verdict == "anomaly"
    assert _detect("normal").observe(RawScreen(image_bytes=b"png")).verdict == "normal"
    # 알 수 없는 verdict 는 None 으로 정규화.
    assert _detect("weird").observe(RawScreen(image_bytes=b"png")).verdict is None


# --------------------------------------------------------------------------- #
# #3 VlmErrorAnalyzer — VLM 판정 기반 복구 결정
# --------------------------------------------------------------------------- #


def test_vlm_analyzer_anomaly_reobserves_then_skips():
    a = VlmErrorAnalyzer(max_attempts=2)
    ctx0 = ErrorContext(ErrorPhase.LLM_ANOMALY, "s", "OK", "anomaly", attempt=0, extra={"verdict": "anomaly"})
    assert a.analyze(ctx0).action is Remediation.REOBSERVE
    ctx2 = ErrorContext(ErrorPhase.LLM_ANOMALY, "s", "OK", "anomaly", attempt=2, extra={"verdict": "anomaly"})
    assert a.analyze(ctx2).action is Remediation.SKIP


def test_vlm_analyzer_low_confidence_reobserves():
    a = VlmErrorAnalyzer(max_attempts=2, confidence_threshold=0.5)
    ctx = ErrorContext(ErrorPhase.LOW_CONFIDENCE, "s", "OK", "low", attempt=0, confidence=0.2)
    assert a.analyze(ctx).action is Remediation.REOBSERVE


def test_vlm_analyzer_hard_error_follows_policy():
    a = VlmErrorAnalyzer(max_attempts=2)
    # 전송 실패는 정책 상속: 재시도 → 소진 시 중단.
    assert a.analyze(ErrorContext(ErrorPhase.PRESS, "s", "OK", "x", attempt=0)).action is Remediation.RETRY
    assert a.analyze(ErrorContext(ErrorPhase.PRESS, "s", "OK", "x", attempt=2)).action is Remediation.STOP


# --------------------------------------------------------------------------- #
# #2 + 통합 — 이상 판정 → 재판정 → 정상 복구
# --------------------------------------------------------------------------- #


class _VerdictSense(ScreenSense):
    """MockScreenSense 판정에 verdict 를 스케줄대로 붙이는 래퍼(호출 순서 기준)."""

    def __init__(self, inner: ScreenSense, verdicts: list[str]):
        self._inner = inner
        self._verdicts = verdicts
        self._i = 0

    def observe(self, raw: RawScreen) -> SenseResult:
        base = self._inner.observe(raw)
        v = self._verdicts[self._i] if self._i < len(self._verdicts) else "normal"
        self._i += 1
        return SenseResult(
            state=base.state, raw_signature=base.raw_signature,
            low_confidence=base.low_confidence, verdict=v,
        )

    @property
    def backend_name(self) -> str:
        return f"verdict:{self._inner.backend_name}"


def test_learner_recognizes_anomaly_and_recovers():
    # call#1 초기관찰=normal(학습 시작), call#2 첫 스텝=anomaly(오류 인식) → 재판정, call#3=normal(복구).
    sense = _VerdictSense(MockScreenSense(), ["normal", "anomaly", "normal"])
    store = KeyScreenStore(":memory:")
    learner = Learner(
        MockRemoteDriver(), sense, NavGraph(), settle_ms=0,
        observer=store, analyzer=VlmErrorAnalyzer(max_attempts=3),
    )
    summary = learner.learn(step_budget=50, coverage_target=0.99)
    assert summary.states_visited >= 1            # 학습이 중단되지 않고 진행
    stats = store.stats()
    assert stats["errors"] >= 1, "LLM 이상 판정이 오류로 기록돼야 한다"
    assert stats["reconciled"] >= 1, "재판정 자가치유로 정정된 매핑이 있어야 한다"
    # 첫 스텝 이후 이상이 재판정으로 정상 복구됐으므로, 최종 매핑은 error 상태로 남지 않는다.
    assert stats["mappings"] >= 1
    store.close()


def test_prefer_normal_over_anomaly_reading():
    """복구 채택 규칙: 정상 판정이 이상 판정을 이긴다(동일 신뢰도라도)."""
    learner = Learner(MockRemoteDriver(), MockScreenSense(), NavGraph(), settle_ms=0)
    base = MockScreenSense().observe(RawScreen(text_hint="screen:home"))
    anomaly = SenseResult(state=base.state, raw_signature=base.raw_signature, low_confidence=False, verdict="anomaly")
    normal = SenseResult(state=base.state, raw_signature=base.raw_signature, low_confidence=False, verdict="normal")
    assert learner._prefer(normal, anomaly) is True    # 정상 > 이상
    assert learner._prefer(anomaly, normal) is False    # 이상은 정상을 못 이김

"""오류 자가치유 정책 — 학습 스텝 오류를 분석해 복구 행동을 결정한다.

detection-mcp VLM 재사용(사용자 결정): "화면 재분석"은 REOBSERVE 행동이 트리거하는
**VLM(sense) 재판정**으로 이뤄진다. 즉 이 분석기는 "언제 어떻게 다시 볼지"의 정책을 정하고,
실제 재판정(정확한 키↔화면 재매핑)은 Learner 가 detection-mcp sense.observe 를 다시 부르며 수행한다.

정책(PolicyErrorAnalyzer)
-------------------------
- PRESS(드라이버 실패)      : 재시도(RETRY) → 소진 시 STOP(드라이버 다운으로 간주).
- CAPTURE/OBSERVE(판정 실패): 안정화 더 주고 재판정(REOBSERVE) → 소진 시 SKIP(오류 기록).
- LOW_CONFIDENCE(저신뢰)    : VLM 재판정(REOBSERVE)으로 더 정확한 읽기 시도 → 소진 시 SKIP.
- INCONSISTENT(비결정 전이) : 재판정으로 확정 시도(REOBSERVE) → 소진 시 SKIP(flaky 기록).
"""

from __future__ import annotations

import os

from remotectl.engine.hooks import (
    ErrorAnalyzer,
    ErrorContext,
    ErrorPhase,
    Remediation,
    RemediationDecision,
)

__all__ = ["PolicyErrorAnalyzer", "VlmErrorAnalyzer"]


class PolicyErrorAnalyzer(ErrorAnalyzer):
    """규칙 기반 복구 정책. attempt 가 max_attempts 이상이면 에스컬레이트(SKIP/STOP)."""

    def __init__(self, max_attempts: int = 2):
        self.max_attempts = max(1, int(max_attempts))

    @classmethod
    def from_env(cls, **overrides) -> "PolicyErrorAnalyzer":
        """REMOTECTL_RECONCILE_MAX_ATTEMPTS(기본 2)로 구성."""
        raw = overrides.pop("max_attempts", None)
        if raw is None:
            raw = os.environ.get("REMOTECTL_RECONCILE_MAX_ATTEMPTS", "2")
        try:
            max_attempts = int(raw)
        except (TypeError, ValueError):
            max_attempts = 2
        return cls(max_attempts=max_attempts, **overrides)

    def analyze(self, context: ErrorContext) -> RemediationDecision:
        exhausted = context.attempt >= self.max_attempts

        if context.phase is ErrorPhase.PRESS:
            if exhausted:
                return RemediationDecision(
                    Remediation.STOP,
                    f"press {context.attempt}회 실패 → 드라이버 다운 간주, 안전 종료",
                )
            return RemediationDecision(Remediation.RETRY, f"press 재시도({context.attempt + 1})")

        if context.phase in (ErrorPhase.CAPTURE, ErrorPhase.OBSERVE):
            if exhausted:
                return RemediationDecision(
                    Remediation.SKIP, f"판정 {context.attempt}회 실패 → 스텝 건너뜀(오류 기록)"
                )
            return RemediationDecision(
                Remediation.REOBSERVE, f"안정화 후 VLM 재판정({context.attempt + 1})"
            )

        if context.phase in (
            ErrorPhase.LOW_CONFIDENCE, ErrorPhase.INCONSISTENT, ErrorPhase.LLM_ANOMALY
        ):
            if exhausted:
                return RemediationDecision(
                    Remediation.SKIP,
                    f"{context.phase.value} 확정 실패 → 현재 판정 채택하고 flaky 기록",
                )
            return RemediationDecision(
                Remediation.REOBSERVE, f"VLM 재판정으로 화면 재매핑 시도({context.attempt + 1})"
            )

        # 알 수 없는 단계는 보수적으로 건너뜀.
        return RemediationDecision(Remediation.SKIP, "미분류 오류 → 건너뜀")


class VlmErrorAnalyzer(PolicyErrorAnalyzer):
    """LLM(VLM) 판정을 근거로 오류를 인식·복구 결정하는 분석기(#3).

    "LLM 이 판단해서 오류라고 인식하면 다시 수정 후 재시도" 를 담당한다. 오류 진단의 근거는
    detection-mcp 가 낸 판정(ErrorContext.extra['verdict'] · confidence)이다:

    - 하드 오류(전송/캡처/판정 예외)  : 정책(부모) 그대로 — 재전송/재판정/중단.
    - LLM 이상(anomaly) 판정           : REOBSERVE(VLM 재판정으로 정상 복구 시도) → 소진 시 SKIP(이상 수용).
    - LLM 저신뢰(confidence < 임계)    : REOBSERVE(더 정확한 재판정) → 소진 시 SKIP(현재 수용).
    - 비결정 전이 등                    : 정책(부모).
    """

    def __init__(self, max_attempts: int = 2, confidence_threshold: float = 0.5):
        super().__init__(max_attempts)
        self.confidence_threshold = max(0.0, min(1.0, float(confidence_threshold)))

    @classmethod
    def from_env(cls, **overrides) -> "VlmErrorAnalyzer":
        """REMOTECTL_RECONCILE_MAX_ATTEMPTS(기본 2) · REMOTECTL_RECONCILE_CONF_THRESHOLD(기본 0.5)."""
        raw = overrides.pop("max_attempts", None)
        if raw is None:
            raw = os.environ.get("REMOTECTL_RECONCILE_MAX_ATTEMPTS", "2")
        try:
            max_attempts = int(raw)
        except (TypeError, ValueError):
            max_attempts = 2
        thr = overrides.pop("confidence_threshold", None)
        if thr is None:
            thr = os.environ.get("REMOTECTL_RECONCILE_CONF_THRESHOLD", "0.5")
        try:
            threshold = float(thr)
        except (TypeError, ValueError):
            threshold = 0.5
        return cls(max_attempts=max_attempts, confidence_threshold=threshold, **overrides)

    def analyze(self, context: ErrorContext) -> RemediationDecision:
        # 하드 오류는 정책(부모)을 그대로 따른다.
        if context.phase in (ErrorPhase.PRESS, ErrorPhase.CAPTURE, ErrorPhase.OBSERVE):
            return super().analyze(context)

        exhausted = context.attempt >= self.max_attempts
        verdict = (context.extra or {}).get("verdict")

        # LLM 이 이상(anomaly)으로 판정 → 오류로 인식하고 정상 복구를 시도.
        if verdict == "anomaly" or context.phase is ErrorPhase.LLM_ANOMALY:
            if exhausted:
                return RemediationDecision(
                    Remediation.SKIP, "VLM 이상(anomaly) 지속 → 실제 이상 상태로 수용(flaky 기록)"
                )
            return RemediationDecision(
                Remediation.REOBSERVE,
                f"VLM 이 이상으로 판정 → 재판정으로 정상 복구 시도({context.attempt + 1})",
            )

        # LLM 저신뢰 → 더 정확한 재판정.
        conf = context.confidence
        if (conf is not None and conf < self.confidence_threshold) \
                or context.phase is ErrorPhase.LOW_CONFIDENCE:
            if exhausted:
                return RemediationDecision(Remediation.SKIP, "VLM 저신뢰 지속 → 현재 판정 수용")
            return RemediationDecision(
                Remediation.REOBSERVE, f"VLM 저신뢰 → 재판정으로 정확도 향상({context.attempt + 1})"
            )

        # 비결정 전이 등은 정책(부모).
        return super().analyze(context)

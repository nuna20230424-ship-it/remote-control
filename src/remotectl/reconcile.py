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

__all__ = ["PolicyErrorAnalyzer"]


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

        if context.phase in (ErrorPhase.LOW_CONFIDENCE, ErrorPhase.INCONSISTENT):
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

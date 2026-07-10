"""학습 루프 훅(관찰/오류분석) — 엔진과 구체 구현(SQLite DB · VLM 분석)을 잇는 추상.

M5(의존 경계): Learner 는 이 **추상**만 안다. SQLite KeyScreenStore(store.py)나
detection-mcp 백엔드 ErrorAnalyzer(reconcile.py) 같은 구체는 여기 계약만 구현하며,
주입은 상위(api/deps.py)의 책임이다. 이로써 "키↔화면 동시 기록"과 "오류 자가치유"를
엔진 코어를 오염시키지 않고 얹는다.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

__all__ = [
    "StepRecord",
    "StepObserver",
    "ErrorPhase",
    "ErrorContext",
    "Remediation",
    "RemediationDecision",
    "ErrorAnalyzer",
    "NullObserver",
    "NoRetryAnalyzer",
]


@dataclass(slots=True)
class StepRecord:
    """학습 한 스텝의 (from_state, key) → to_state 매핑 + LLM 화면 분석.

    "키이벤트별 화면"의 정본 레코드. from/to 는 상태 id + 서명, analysis 는 LLM(VLM)이
    낸 화면 서술/라벨, confidence 는 판정 신뢰도. reconciled 는 오류 자가치유로 정정됐는지.
    """

    session_id: str
    from_state_id: str
    from_signature: str
    key_token: str
    to_state_id: str
    to_signature: str
    to_kind: str
    to_app_id: Optional[str]
    analysis: Optional[str]
    confidence: float
    reconciled: bool = False


class ErrorPhase(str, Enum):
    """오류가 난 학습 단계."""

    PRESS = "press"          # 키 전송 실패(드라이버)
    CAPTURE = "capture"      # 화면 캡처 실패
    OBSERVE = "observe"      # 화면 판정 실패/미확정
    LOW_CONFIDENCE = "low_confidence"  # 판정은 됐으나 신뢰도 미달
    INCONSISTENT = "inconsistent"      # 같은 (from,key)가 관측마다 다른 to (비결정)


@dataclass(slots=True)
class ErrorContext:
    """오류 분석에 넘기는 맥락."""

    phase: ErrorPhase
    from_state_id: Optional[str]
    key_token: Optional[str]
    message: str
    attempt: int = 0
    confidence: Optional[float] = None
    extra: dict = field(default_factory=dict)


class Remediation(str, Enum):
    """오류 분석기가 지시하는 복구 행동."""

    RETRY = "retry"        # 같은 행동을 다시(전송 재시도 등)
    REOBSERVE = "reobserve"  # press 없이 재캡처·재판정(안정화 더 대기)
    SKIP = "skip"          # 이 스텝을 포기하고 다음으로(오류 기록)
    STOP = "stop"          # 학습을 안전 종료


@dataclass(slots=True)
class RemediationDecision:
    """오류 분석 결과: 행동 + 사람이 읽는 사유(원장 기록용)."""

    action: Remediation
    note: str = ""


class StepObserver(abc.ABC):
    """학습 스텝/오류를 받아 부수 기록(별도 DB 등)하는 관찰자."""

    @abc.abstractmethod
    def on_step(self, record: StepRecord) -> None:
        """정상 스텝 기록(키이벤트↔화면 매핑 upsert)."""

    @abc.abstractmethod
    def on_error(self, context: ErrorContext, decision: "RemediationDecision") -> None:
        """오류 + 복구 결정 기록."""


class ErrorAnalyzer(abc.ABC):
    """오류를 분석해 복구 행동을 결정하는 분석기(구체는 VLM/규칙 기반)."""

    @abc.abstractmethod
    def analyze(self, context: ErrorContext) -> RemediationDecision:
        """오류 맥락 → 복구 결정."""


class NullObserver(StepObserver):
    """아무것도 기록하지 않는 기본 관찰자(관찰자 미주입 시)."""

    def on_step(self, record: StepRecord) -> None:  # noqa: D102
        return None

    def on_error(self, context: ErrorContext, decision: RemediationDecision) -> None:  # noqa: D102
        return None


class NoRetryAnalyzer(ErrorAnalyzer):
    """자가치유 없이 항상 SKIP(오류 기록 후 다음 스텝) — 분석기 미주입 시 기본."""

    def analyze(self, context: ErrorContext) -> RemediationDecision:  # noqa: D102
        return RemediationDecision(action=Remediation.SKIP, note="분석기 미주입(기본 SKIP)")

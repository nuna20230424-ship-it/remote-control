"""ScreenSense 추상 인터페이스 — 화면 원재료(RawScreen)를 상태(ScreenState)로 판정.

역할(경계)
----------
- 드라이버(RemoteDriver)는 "무엇이 화면에 있는가"를 모른다. 그것을 아는 것이 ScreenSense 다.
- ScreenSense 는 RawScreen(픽셀/텍스트/메타)을 입력으로 받아, 정규화된 signature 와
  라벨/종류/앱ID/신뢰도를 담은 ScreenState 를 만든다. 상태 id 는 signature 파생(모델이 계산).
- 이 분리로 detection-mcp/VLM/휴리스틱 백엔드를 코어 변경 없이 교체한다(PRD G5, M5).

상태 정체성(PRD R2)
-------------------
- signature 정규화가 상태 식별의 핵심 레버다. 같은 화면은 같은 signature 로 수렴해야 한다
  (다른 관찰→같은 상태, 다른 화면→다른 상태). 정규화 책임은 센스 구현체에 있다.
- 저신뢰(confidence < threshold) 판정은 엔진이 재관찰/보류하도록 confidence 로 신호한다(R3).

계약
----
- observe(raw) -> SenseResult : 순수 함수적(부작용 없음). 같은 raw 는 같은 결과가 이상적.
- backend_name : 진단/대시보드 표기용.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from remotectl.drivers.base import RawScreen
from remotectl.models import ScreenState, StateKind, compute_state_id

__all__ = [
    "SenseResult",
    "ScreenSense",
    "ScreenSenseError",
    "SenseUnavailableError",
    "normalize_signature",
]


class ScreenSenseError(RuntimeError):
    """센스 계층 오류의 베이스. 엔진은 이 타입만 잡으면 된다."""


class SenseUnavailableError(ScreenSenseError):
    """판정 백엔드(detection-mcp/VLM)에 도달 불가."""


def normalize_signature(text: str) -> str:
    """화면 서명 정규화(상태 식별 안정화, PRD R2).

    최소 정규화: 공백 축약 + 소문자화 + 좌우 트림. 백엔드가 이미 정규화된 라벨을 주면
    거의 항등이지만, OCR/자유 텍스트가 들어와도 동일 화면이 수렴하도록 여기서 한 번 더 다듬는다.
    (더 강한 정규화 — 동의어/온톨로지 매핑 — 는 후속. PRD 비목표의 사업자별 라벨 표준화.)
    """
    return " ".join(text.split()).strip().lower()


@dataclass(slots=True)
class SenseResult:
    """observe() 산출물.

    - state: 판정된 ScreenState(id 는 signature 파생으로 이미 채워짐).
    - raw_signature: 정규화 전 원시 서명(추적/디버깅용; Observation.sensed_signature 원장).
    - low_confidence: confidence 가 임계 미만이라 재관찰 권고인지(엔진 힌트).
    """

    state: ScreenState
    raw_signature: str
    low_confidence: bool = False


class ScreenSense(abc.ABC):
    """화면 판정 계약. 구현체: MockScreenSense / DetectionMcpScreenSense(VLM)."""

    #: 이 값 미만 confidence 는 저신뢰로 표시(엔진이 재관찰 판단). 구현체가 조정 가능.
    confidence_threshold: float = 0.5

    @abc.abstractmethod
    def observe(self, raw: RawScreen) -> SenseResult:
        """RawScreen 을 판정해 SenseResult 를 반환한다.

        - 부작용 없이(순수) 동작하는 것을 지향한다.
        - 도달 불가(백엔드 다운)면 SenseUnavailableError 를 던진다.
        """

    @property
    @abc.abstractmethod
    def backend_name(self) -> str:
        """판정 백엔드 이름(예: "mock", "detection-mcp:qwen2.5vl:7b")."""

    # --- 공통 조립 헬퍼(구현체가 재사용) --------------------------------- #

    def _build_result(
        self,
        *,
        raw_signature: str,
        label: Optional[str],
        kind: StateKind,
        app_id: Optional[str],
        confidence: float,
        screenshot_ref: Optional[str],
    ) -> SenseResult:
        """정규화 → ScreenState 조립 → 저신뢰 판단을 한 곳에서.

        구현체는 백엔드 결과를 이 시그니처에 맞춰 넘기기만 하면, 정규화/ id 파생/
        임계값 처리가 일관되게 적용된다(구현체 간 편차 방지).
        """
        norm = normalize_signature(raw_signature)
        state = ScreenState(
            id=compute_state_id(norm),
            signature=norm,
            label=label,
            kind=kind,
            app_id=app_id,
            confidence=confidence,
            screenshot_ref=screenshot_ref,
        )
        return SenseResult(
            state=state,
            raw_signature=raw_signature,
            low_confidence=confidence < self.confidence_threshold,
        )

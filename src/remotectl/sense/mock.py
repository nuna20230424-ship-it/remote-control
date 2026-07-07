"""MockScreenSense — 결정적 화면 판정(개발/테스트, PRD G4/M1).

동작
----
RawScreen.text_hint 를 화면 서명(signature)으로 그대로 채택하고, RawScreen.meta 에
드라이버가 실어 보낸 label/kind/app_id 를 활용해 ScreenState 를 조립한다. 항상 고신뢰(1.0).

MockRemoteDriver 와의 정합
--------------------------
MockRemoteDriver.capture() 는 화면마다 유일한 text_hint(=SimScreen.signature)와
meta(label/kind/app_id)를 내보낸다. 따라서 이 센스와 짝을 이루면 "같은 화면→같은 state id,
다른 화면→다른 state id" 가 결정론적으로 성립하여 학습/계획/실행이 안정적으로 검증된다.

meta 가 없는 RawScreen(예: 순수 text 만 있는 경우)에도 동작하도록, meta 부재 시에는
signature 문자열에서 kind/app_id 를 값싸게 추론한다("screen:app:netflix" 같은 규약 활용).
이는 mock 편의일 뿐, 실제 판정 규칙이 아니다(실판정은 DetectionMcpScreenSense).
"""

from __future__ import annotations

from typing import Optional

from remotectl.drivers.base import RawScreen
from remotectl.models import StateKind
from remotectl.sense.base import ScreenSense, ScreenSenseError, SenseResult

__all__ = ["MockScreenSense"]


class MockScreenSense(ScreenSense):
    """결정적 mock 판정기. 실제 모델 호출 없음."""

    def __init__(self, *, confidence: float = 1.0):
        self._confidence = confidence

    @property
    def backend_name(self) -> str:
        return "mock"

    def observe(self, raw: RawScreen) -> SenseResult:
        signature = raw.text_hint
        if not signature:
            # text_hint 가 없으면 image_ref 로 폴백(그마저 없으면 판정 불가).
            signature = raw.image_ref
        if not signature:
            raise ScreenSenseError(
                "MockScreenSense: RawScreen 에 text_hint/image_ref 가 모두 없어 판정 불가."
            )

        meta = raw.meta or {}
        label = meta.get("label")
        app_id = meta.get("app_id")
        kind = self._resolve_kind(meta.get("kind"), signature)

        return self._build_result(
            raw_signature=signature,
            label=label,
            kind=kind,
            app_id=app_id,
            confidence=self._confidence,
            screenshot_ref=raw.image_ref,
        )

    # --- 내부 ------------------------------------------------------------- #

    @staticmethod
    def _resolve_kind(meta_kind: Optional[str], signature: str) -> StateKind:
        """meta.kind 우선, 없으면 signature 규약에서 값싸게 추론.

        규약: "screen:home", "screen:app:netflix", "screen:launcher:...", "screen:app:settings".
        """
        if meta_kind:
            try:
                return StateKind(meta_kind)
            except ValueError:
                pass
        sig = signature.lower()
        if "app:settings" in sig or "screen:settings" in sig:
            return StateKind.SETTINGS
        if ":app:" in sig or sig.endswith(":app"):
            return StateKind.APP
        if "home" in sig:
            return StateKind.HOME
        if "launcher" in sig or "menu" in sig:
            return StateKind.MENU
        return StateKind.UNKNOWN

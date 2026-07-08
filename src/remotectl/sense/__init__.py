"""remotectl.sense — 화면 판정 계층(ScreenSense 인터페이스와 구현체들).

경계
----
- 이 패키지는 "화면 원재료(RawScreen)가 어떤 상태(ScreenState)인가" 판정만 담당한다.
- 버튼 입력/캡처/리셋(전송)은 remotectl.drivers(RemoteDriver) 의 책임이다.
- 코어 엔진은 ScreenSense 추상 타입만 임포트한다(구현체 선택은 조립 지점에서, M5).

구현체
------
- MockScreenSense        : 결정적 mock 판정(개발/테스트, PRD G4/M1).
- DetectionMcpScreenSense: detection-mcp/VLM HTTP 어댑터(스텁; [WIRE] 지점 배선 필요).
"""

from remotectl.sense.base import (
    ScreenSense,
    ScreenSenseError,
    SenseResult,
    SenseUnavailableError,
    normalize_signature,
)
from remotectl.sense.detection_mcp import DetectionMcpScreenSense
from remotectl.sense.mock import MockScreenSense

__all__ = [
    # 인터페이스 + 데이터/예외
    "ScreenSense",
    "SenseResult",
    "ScreenSenseError",
    "SenseUnavailableError",
    "normalize_signature",
    # 구현체
    "MockScreenSense",
    "DetectionMcpScreenSense",
]

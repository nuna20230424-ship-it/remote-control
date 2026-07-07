"""remotectl.drivers — 리모컨 제어 전송 계층(RemoteDriver 인터페이스와 구현체들).

경계
----
- 이 패키지는 "버튼 입력/화면 캡처/리셋" 이라는 전송만 담당한다.
- 화면→상태 판정은 remotectl.sense(ScreenSense) 의 책임이다.
- 코어 엔진은 RemoteDriver 추상 타입만 임포트한다(구현체 선택은 조립 지점에서).

구현체
------
- MockRemoteDriver : 결정적 가짜 STB(개발/테스트, PRD G4/M1).
- RemoteMcpClient  : 사내망 remote-MCP HTTP 어댑터(스텁; [WIRE] 지점 배선 필요).
"""

from remotectl.drivers.base import (
    CaptureError,
    DriverInfo,
    DriverUnavailableError,
    PressError,
    RawScreen,
    RemoteDriver,
    RemoteDriverError,
)
from remotectl.drivers.mock import (
    MockRemoteDriver,
    MockScenario,
    SimScreen,
    default_stb_scenario,
)
from remotectl.drivers.mcp_client import DEFAULT_KEYMAP, RemoteMcpClient

__all__ = [
    # 인터페이스 + 전송 데이터/예외
    "RemoteDriver",
    "RawScreen",
    "DriverInfo",
    "RemoteDriverError",
    "DriverUnavailableError",
    "PressError",
    "CaptureError",
    # 구현체
    "MockRemoteDriver",
    "MockScenario",
    "SimScreen",
    "default_stb_scenario",
    "RemoteMcpClient",
    "DEFAULT_KEYMAP",
]

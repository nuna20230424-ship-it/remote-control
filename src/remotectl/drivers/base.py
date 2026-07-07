"""RemoteDriver 추상 인터페이스 — 리모컨 하드웨어/전송 계층의 경계.

역할
----
코어 엔진(학습/계획/실행)은 "버튼을 누르고, 화면을 캡처하고, 초기화한다"는 세 가지
최소 공통 동작만 안다. 실제로 그것이 사내망 remote-MCP HTTP 호출인지, 가짜 상태머신인지,
장래의 IR 블래스터/ADB 브리지인지는 이 인터페이스 뒤에서 흡수한다(PRD R1, M5).

경계 계약(왜 이 세 개인가)
--------------------------
- press(key): 리모컨 키 1회(반복 포함) 입력. 부작용만 있고 화면 판정은 하지 않는다.
  (화면→상태 판정은 ScreenSense 의 책임. 드라이버는 "무엇이 보이는가"를 모른다.)
- capture(): 현재 화면의 원시 캡처(RawScreen)를 가져온다. 픽셀/텍스트/메타의 원재료일 뿐,
  상태 식별(signature/label)은 하지 않는다 — 그것도 ScreenSense 의 몫.
- reset(): 알려진 시작 지점(보통 HOME)으로 되돌린다. 학습/실행 세션의 재현성 확보.
- settle(): 입력 후 UI 안정화(로딩·애니메이션) 대기(PRD R3). 기본 구현 제공.

이 분리 덕분에 "드라이버가 화면을 어떻게 얻는가"(전송)와 "화면이 어떤 상태인가"(판정)가
독립적으로 교체된다. 예: Mock 드라이버 + 실제 VLM 센스, 또는 실제 MCP 드라이버 + Mock 센스.

동기 API 인 이유
----------------
1차 릴리스 엔진은 동기(순차 탐색) 루프다. 실제 HTTP(httpx)는 동기 클라이언트로 감싼다.
장래 비동기가 필요하면 AsyncRemoteDriver 를 병렬로 도입하되, 코어 계약은 이 동기 형태를 정본으로 둔다.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional

from remotectl.models import Button, KeyPress

__all__ = [
    "RawScreen",
    "DriverInfo",
    "RemoteDriver",
    "RemoteDriverError",
    "PressError",
    "CaptureError",
    "DriverUnavailableError",
]


# --------------------------------------------------------------------------- #
# 예외 계층
# --------------------------------------------------------------------------- #


class RemoteDriverError(RuntimeError):
    """드라이버 계층의 모든 오류의 베이스.

    엔진은 이 타입만 잡아 ExecutionStatus.FAILED_DRIVER 로 매핑하면 된다
    (구체 어댑터의 예외 종류에 결합되지 않도록).
    """


class DriverUnavailableError(RemoteDriverError):
    """대상(STB/MCP)에 도달 불가(연결 거부/타임아웃/서버 미기동 등).

    remote-MCP 도달성 문제(사내망 이슈 등)는 개별 TC 버그가 아니라 이 범주로 올린다.
    """


class PressError(RemoteDriverError):
    """press() 수행 실패(전송 실패/미지원 키코드 등)."""


class CaptureError(RemoteDriverError):
    """capture() 수행 실패(캡처 미지원/이미지 획득 실패 등)."""


# --------------------------------------------------------------------------- #
# 전송 계층 데이터(코어 모델과 분리된 "원재료")
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RawScreen:
    """드라이버가 반환하는 화면 원재료(raw). 상태 판정 전 단계.

    ScreenSense 가 이것을 입력으로 받아 ScreenState(signature/label/kind) 를 만든다.
    드라이버는 가능한 재료만 채우고, 못 채우는 필드는 None 으로 둔다(어댑터별 능력 차이 허용).

    필드
    ----
    - image_ref: 스크린샷 참조(파일경로/URL/blob id). 원본 바이트를 여기 싣지 않는다.
    - image_bytes: 인메모리 캡처가 필요한 경우의 원본 바이트(옵션; 대개 None).
    - image_mime: image_bytes 의 MIME(예: "image/png"). 바이트가 있을 때만 의미.
    - text_hint: 드라이버가 값싸게 얻을 수 있는 텍스트(OSD/HDMI-CEC 상태/디버그 오버레이 등).
                 VLM 없이 저비용 선판정(PRD R6)에 활용될 수 있다.
    - meta: 어댑터별 부가 정보(해상도, 프레임 타임스탬프, 현재 앱 힌트 등). 자유 dict.
    - captured_at_ms: 캡처 시각(단조 ms). settle/타이밍 디버깅용(옵션).
    """

    image_ref: Optional[str] = None
    image_bytes: Optional[bytes] = None
    image_mime: Optional[str] = None
    text_hint: Optional[str] = None
    meta: dict = field(default_factory=dict)
    captured_at_ms: Optional[int] = None

    def has_pixels(self) -> bool:
        """VLM 판정에 넘길 실제 이미지가 있는지(ref 또는 bytes)."""
        return self.image_bytes is not None or self.image_ref is not None


@dataclass(slots=True)
class DriverInfo:
    """드라이버 자기소개(진단/대시보드 표기용).

    - name: 어댑터 이름(예: "mock", "remote-mcp").
    - target: 대상 식별(예: STB 모델/호스트). Mock 은 시나리오명.
    - endpoint: 실제 연결 대상(예: MCP base URL). Mock 은 None.
    - supports_capture: capture() 로 실제 픽셀을 얻을 수 있는지.
    - ready: 현재 사용 가능한지(연결 확인 결과). 미확인이면 None.
    """

    name: str
    target: Optional[str] = None
    endpoint: Optional[str] = None
    supports_capture: bool = False
    ready: Optional[bool] = None


# --------------------------------------------------------------------------- #
# 추상 인터페이스
# --------------------------------------------------------------------------- #


class RemoteDriver(abc.ABC):
    """리모컨 제어의 최소 공통 계약.

    구현체(MockRemoteDriver / RemoteMcpClient / 장래 어댑터)는 이 4개 추상 메서드만
    채우면 코어 엔진과 호환된다(M5: 코어 코드 변경 0줄).

    컨텍스트 매니저로 쓰면 close() 가 보장된다:
        with RemoteMcpClient.from_env() as drv:
            drv.reset(); drv.press(KeyPress(button=Button.HOME))
    """

    # --- 필수 계약 -------------------------------------------------------- #

    @abc.abstractmethod
    def press(self, key: KeyPress) -> None:
        """리모컨 키를 1회(key.repeat 만큼 반복) 입력한다.

        - 화면 판정을 하지 않는다(부작용만). 판정은 capture()+ScreenSense 로.
        - key.repeat 반복 입력은 구현체가 처리(HTTP 라면 N회 호출 또는 repeat 파라미터).
        - 실패 시 PressError(도달불가면 DriverUnavailableError) 를 던진다.
        """

    @abc.abstractmethod
    def capture(self) -> RawScreen:
        """현재 화면의 원재료(RawScreen)를 획득한다.

        - 상태 식별은 하지 않는다(ScreenSense 책임).
        - 실패 시 CaptureError(도달불가면 DriverUnavailableError) 를 던진다.
        """

    @abc.abstractmethod
    def reset(self) -> None:
        """알려진 시작 지점(보통 HOME)으로 되돌린다.

        구현 예: HOME 키 여러 번, 또는 MCP 의 전용 reset 오퍼레이션.
        세션 재현성(학습/실행 시작 상태 고정)에 쓰인다.
        """

    @abc.abstractmethod
    def info(self) -> DriverInfo:
        """드라이버 자기소개(진단/대시보드). 부작용 없이 값싸게 반환."""

    # --- 기본 제공(오버라이드 선택) --------------------------------------- #

    def settle(self, ms: int) -> None:
        """입력 후 UI 안정화 대기(PRD R3).

        기본은 단순 sleep. 실제 어댑터는 "프레임 정지 감지" 같은 능동 대기로
        오버라이드할 수 있다. ms<=0 이면 대기하지 않는다.
        """
        if ms and ms > 0:
            import time

            time.sleep(ms / 1000.0)

    def press_and_capture(self, key: KeyPress, *, settle_ms: int = 0) -> RawScreen:
        """press → settle → capture 를 한 번에. 학습/실행 루프의 공통 편의.

        엔진이 이 순서를 매번 재작성하지 않도록 여기 제공한다(계약의 일부는 아님).
        """
        self.press(key)
        if settle_ms:
            self.settle(settle_ms)
        return self.capture()

    def close(self) -> None:
        """자원 정리(HTTP 세션 종료 등). 기본은 no-op."""

    def __enter__(self) -> "RemoteDriver":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# 편의: 리셋에 흔히 쓰는 HOME 키프레스(코어에서 반복 생성 방지).
HOME_KEY = KeyPress(button=Button.HOME)

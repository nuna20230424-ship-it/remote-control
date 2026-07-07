"""MockRemoteDriver — 실제 STB 없이 학습/계획/실행을 완결시키는 결정적 상태머신.

목적(PRD G4, M1)
-----------------
사내망 remote-MCP 없이도 전체 파이프라인(학습→맵구축→목표실행)이 엔드투엔드로 돌도록,
"버튼을 누르면 화면이 결정적으로 전이하는" 가짜 STB 를 제공한다. 테스트/개발의 기준 대상.

설계
----
- 내부에 SimScreen(가짜 화면) 노드와 (screen, button_token) -> screen 전이표를 둔다.
- press() 는 현재 화면을 전이시키고, capture() 는 현재 화면을 RawScreen 으로 노출한다.
- capture() 가 내보내는 RawScreen.text_hint / image_ref 는 화면마다 유일하다.
  MockScreenSense 는 이 text_hint 를 그대로 signature 로 삼아 상태를 결정론적으로 식별한다.
  (드라이버=전송, 센스=판정 이라는 경계를 유지하면서도 결정성을 확보하는 방식)
- 정의되지 않은 (screen, button) 입력은 "제자리"(무전이)로 처리 — 실제 리모컨에서 먹히지
  않는 키를 눌러도 화면이 그대로인 상황을 모사(학습이 self-loop 를 관측하게 됨).

비결정성 주입(PRD R3/M3 재계획 견고성 테스트용)
----------------------------------------------
- flaky_transitions: {(screen, button_token): [잘못된목적지, ...]} 를 주면, 해당 전이가
  처음 N회는 잘못된 화면으로 튄다. 재계획 로직이 이를 흡수하는지 검증할 수 있다.
- 시드 기반이 아니라 "호출 횟수 기반"의 결정적 flakiness 라, 테스트가 재현 가능하다.

기본 시나리오(default_stb_scenario)
-----------------------------------
홈에서 좌우로 앱 런처를 훑고 OK 로 앱에 진입하는, 최소하지만 목표실행을 검증하기에 충분한 맵.
앱: netflix / youtube / settings. 홈에서 BACK/HOME 은 홈으로 수렴.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from remotectl.drivers.base import (
    DriverInfo,
    PressError,
    RawScreen,
    RemoteDriver,
)
from remotectl.models import KeyPress, StateKind

__all__ = ["SimScreen", "MockScenario", "MockRemoteDriver", "default_stb_scenario"]


@dataclass(slots=True)
class SimScreen:
    """가짜 화면 노드.

    - key: 시나리오 내부 식별자(전이표의 노드 이름). signature 와 같게 두면 단순하다.
    - signature: capture 시 text_hint 로 노출될 화면 서명(센스가 이걸 상태로 식별).
    - label: 사람이 읽는 라벨(대시보드/디버깅). 센스 mock 이 참조할 수도 있음.
    - kind: 상태 종류 힌트(StateKind). 센스 mock 이 참조.
    - app_id: 앱 화면이면 앱 식별자(목표 매핑 핵심 키).
    - image_ref: 가짜 스크린샷 참조(예: "mock://home"). has_pixels 를 True 로 만든다.
    """

    key: str
    signature: str
    label: str
    kind: StateKind = StateKind.UNKNOWN
    app_id: Optional[str] = None
    image_ref: Optional[str] = None


@dataclass(slots=True)
class MockScenario:
    """가짜 STB 한 대의 정의(화면들 + 전이표 + 시작점).

    - screens: screen_key -> SimScreen.
    - transitions: (screen_key, key_token) -> 목적지 screen_key.
      key_token 은 KeyPress.token 규약(예: "RIGHT", "OK", "APP_SHORTCUT:netflix").
    - start_screen / home_screen: 초기 화면 / reset 목적지.
    - flaky_transitions: (screen_key, key_token) -> [초기 N회 동안의 오배송 목적지들].
    """

    screens: dict[str, SimScreen]
    transitions: dict[tuple[str, str], str]
    start_screen: str
    home_screen: str
    flaky_transitions: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 정합성 검증: 전이 목적지/시작·홈 화면이 실제 존재하는지.
        for (src, _tok), dst in self.transitions.items():
            if src not in self.screens:
                raise ValueError(f"전이 출발 화면 미정의: {src}")
            if dst not in self.screens:
                raise ValueError(f"전이 도착 화면 미정의: {dst}")
        for key in (self.start_screen, self.home_screen):
            if key not in self.screens:
                raise ValueError(f"시작/홈 화면 미정의: {key}")
        for (src, _tok), dsts in self.flaky_transitions.items():
            for d in dsts:
                if d not in self.screens:
                    raise ValueError(f"flaky 목적지 미정의: {d}")


class MockRemoteDriver(RemoteDriver):
    """결정적 가짜 STB 드라이버. RemoteDriver 계약을 완전히 구현한다."""

    def __init__(self, scenario: Optional[MockScenario] = None, *, name: str = "mock"):
        self._scenario = scenario or default_stb_scenario()
        self._name = name
        self._current = self._scenario.start_screen
        # flaky 전이가 몇 번 튀었는지 카운트((src,tok) -> 소비된 오배송 수).
        self._flaky_used: dict[tuple[str, str], int] = {}
        self.press_count = 0  # 진단/테스트용 계측.

    # --- RemoteDriver 계약 ------------------------------------------------ #

    def press(self, key: KeyPress) -> None:
        if not isinstance(key, KeyPress):  # 방어: 엔진 계약 위반 조기 검출
            raise PressError(f"KeyPress 가 필요합니다: {type(key)!r}")
        # repeat 는 같은 전이를 반복 적용(현실의 UP*3 등). token 은 이미 repeat 를 포함하지만
        # 전이표는 단위 토큰 기준이므로 base token 으로 repeat 만큼 스텝을 밟는다.
        base = KeyPress(
            button=key.button, app_shortcut=key.app_shortcut, repeat=1
        ).token
        for _ in range(key.repeat):
            self._step(base)
            self.press_count += 1

    def capture(self) -> RawScreen:
        screen = self._scenario.screens[self._current]
        return RawScreen(
            image_ref=screen.image_ref or f"mock://{screen.key}",
            text_hint=screen.signature,
            meta={
                "screen_key": screen.key,
                "label": screen.label,
                "kind": screen.kind.value,
                "app_id": screen.app_id,
            },
        )

    def reset(self) -> None:
        self._current = self._scenario.home_screen
        self._flaky_used.clear()

    def info(self) -> DriverInfo:
        return DriverInfo(
            name=self._name,
            target=f"sim:{len(self._scenario.screens)}-screens",
            endpoint=None,
            supports_capture=True,
            ready=True,
        )

    def settle(self, ms: int) -> None:
        # Mock 은 실제로 대기하지 않는다(테스트 속도). 계약상 no-op 로 충분.
        return None

    # --- 내부 --------------------------------------------------------------#

    def _step(self, token: str) -> None:
        src = self._current
        # flaky 우선: 아직 소진되지 않은 오배송이 있으면 그쪽으로 튄다.
        flaky = self._scenario.flaky_transitions.get((src, token))
        if flaky:
            used = self._flaky_used.get((src, token), 0)
            if used < len(flaky):
                self._flaky_used[(src, token)] = used + 1
                self._current = flaky[used]
                return
        # 정상 전이. 미정의면 제자리(self-loop).
        self._current = self._scenario.transitions.get((src, token), src)

    # --- 테스트 편의 ------------------------------------------------------ #

    @property
    def current_screen_key(self) -> str:
        """현재 시뮬 화면 키(테스트 단언용). 계약 밖의 introspection."""
        return self._current

    def goto(self, screen_key: str) -> None:
        """테스트에서 특정 화면으로 순간이동(전이표 무시). 계약 밖."""
        if screen_key not in self._scenario.screens:
            raise KeyError(screen_key)
        self._current = screen_key


# --------------------------------------------------------------------------- #
# 기본 시나리오
# --------------------------------------------------------------------------- #


def default_stb_scenario() -> MockScenario:
    """1차 릴리스 테스트용 기본 STB 시나리오.

    구조(홈에서 좌우 런처 → OK 진입):
        home  --RIGHT-->  launcher_netflix  --RIGHT-->  launcher_youtube
                                                          --RIGHT--> launcher_settings
        (각 launcher 에서 OK -> 해당 app 화면; BACK -> home)
        홈에서 앱 단축키(APP_SHORTCUT:<app>)로 직행하는 지름길도 제공(리모컨 앱 버튼 모사).
    """
    screens: dict[str, SimScreen] = {
        "home": SimScreen(
            key="home",
            signature="screen:home",
            label="홈 화면",
            kind=StateKind.HOME,
            image_ref="mock://home",
        ),
        "launcher_netflix": SimScreen(
            key="launcher_netflix",
            signature="screen:launcher:netflix",
            label="런처 - 넷플릭스 선택",
            kind=StateKind.MENU,
            image_ref="mock://launcher/netflix",
        ),
        "launcher_youtube": SimScreen(
            key="launcher_youtube",
            signature="screen:launcher:youtube",
            label="런처 - 유튜브 선택",
            kind=StateKind.MENU,
            image_ref="mock://launcher/youtube",
        ),
        "launcher_settings": SimScreen(
            key="launcher_settings",
            signature="screen:launcher:settings",
            label="런처 - 설정 선택",
            kind=StateKind.MENU,
            image_ref="mock://launcher/settings",
        ),
        "app_netflix": SimScreen(
            key="app_netflix",
            signature="screen:app:netflix",
            label="넷플릭스 앱",
            kind=StateKind.APP,
            app_id="netflix",
            image_ref="mock://app/netflix",
        ),
        "app_youtube": SimScreen(
            key="app_youtube",
            signature="screen:app:youtube",
            label="유튜브 앱",
            kind=StateKind.APP,
            app_id="youtube",
            image_ref="mock://app/youtube",
        ),
        "app_settings": SimScreen(
            key="app_settings",
            signature="screen:app:settings",
            label="설정",
            kind=StateKind.SETTINGS,
            app_id="settings",
            image_ref="mock://app/settings",
        ),
    }

    t: dict[tuple[str, str], str] = {}

    # 홈 <-> 런처(좌우 이동). 홈에서 RIGHT 로 첫 런처 진입.
    t[("home", "RIGHT")] = "launcher_netflix"
    t[("launcher_netflix", "RIGHT")] = "launcher_youtube"
    t[("launcher_youtube", "RIGHT")] = "launcher_settings"
    t[("launcher_settings", "RIGHT")] = "launcher_settings"  # 끝에서 제자리
    # 좌로 되돌기
    t[("launcher_settings", "LEFT")] = "launcher_youtube"
    t[("launcher_youtube", "LEFT")] = "launcher_netflix"
    t[("launcher_netflix", "LEFT")] = "home"

    # OK 로 앱 진입
    t[("launcher_netflix", "OK")] = "app_netflix"
    t[("launcher_youtube", "OK")] = "app_youtube"
    t[("launcher_settings", "OK")] = "app_settings"

    # 앱에서 BACK -> 해당 런처, HOME -> 홈
    for app, launcher in (
        ("app_netflix", "launcher_netflix"),
        ("app_youtube", "launcher_youtube"),
        ("app_settings", "launcher_settings"),
    ):
        t[(app, "BACK")] = launcher
        t[(app, "HOME")] = "home"

    # 어디서나 HOME -> home (미지정 화면 보정). 런처에서도 HOME 지원.
    for skey in screens:
        t.setdefault((skey, "HOME"), "home")
    # 런처/홈에서 BACK -> home
    for skey in ("launcher_netflix", "launcher_youtube", "launcher_settings", "home"):
        t.setdefault((skey, "BACK"), "home")

    # 앱 단축키 지름길(리모컨 앱 버튼). 어디서나 직행.
    for skey in screens:
        t[(skey, "APP_SHORTCUT:netflix")] = "app_netflix"
        t[(skey, "APP_SHORTCUT:youtube")] = "app_youtube"
        t[(skey, "APP_SHORTCUT:settings")] = "app_settings"

    return MockScenario(
        screens=screens,
        transitions=t,
        start_screen="home",
        home_screen="home",
    )

"""드라이버/센스 계층 계약 테스트.

검증 대상(내 담당 파일 한정):
- RemoteDriver/ScreenSense 계약이 지켜지는가(추상 메서드 구현, 예외 매핑).
- MockRemoteDriver + MockScreenSense 가 결정적으로 맞물려 "같은 화면→같은 state id" 를
  보장하는가(학습/실행 엔진이 이 위에서 성립하기 위한 전제, M1/M5).
- RemoteMcpClient/DetectionMcpScreenSense 가 "가정한 MCP 계약" JSON 을 올바로 파싱하고,
  도달불가를 DriverUnavailableError/SenseUnavailableError 로 승격하는가.

httpx.MockTransport 를 써서 실제 사내망 없이 HTTP 어댑터를 검증한다.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from remotectl.drivers import (
    DriverUnavailableError,
    MockRemoteDriver,
    RawScreen,
    RemoteDriver,
    RemoteMcpClient,
    default_stb_scenario,
)
from remotectl.models import Button, KeyPress, StateKind, compute_state_id
from remotectl.sense import (
    DetectionMcpScreenSense,
    MockScreenSense,
    ScreenSense,
    SenseUnavailableError,
    normalize_signature,
)

APP_NETFLIX = KeyPress(button=Button.APP_SHORTCUT, app_shortcut="netflix")


# --------------------------------------------------------------------------- #
# Mock 드라이버 상태머신
# --------------------------------------------------------------------------- #


def test_mock_driver_is_remote_driver():
    assert isinstance(MockRemoteDriver(), RemoteDriver)


def test_mock_driver_deterministic_navigation():
    drv = MockRemoteDriver()
    drv.reset()
    assert drv.current_screen_key == "home"

    # 홈 -> RIGHT -> 넷플릭스 런처 -> OK -> 넷플릭스 앱
    drv.press(KeyPress(button=Button.RIGHT))
    assert drv.current_screen_key == "launcher_netflix"
    drv.press(KeyPress(button=Button.OK))
    assert drv.current_screen_key == "app_netflix"

    cap = drv.capture()
    assert cap.text_hint == "screen:app:netflix"
    assert cap.meta["app_id"] == "netflix"
    assert cap.has_pixels()


def test_mock_driver_app_shortcut_and_repeat():
    drv = MockRemoteDriver()
    drv.reset()
    drv.press(APP_NETFLIX)  # 어디서나 넷플릭스 직행
    assert drv.current_screen_key == "app_netflix"

    drv.reset()
    before = drv.press_count
    # RIGHT*2 == launcher_netflix -> launcher_youtube (repeat 는 스텝을 2회 밟는다)
    drv.press(KeyPress(button=Button.RIGHT, repeat=2))
    assert drv.current_screen_key == "launcher_youtube"
    assert drv.press_count - before == 2


def test_mock_driver_undefined_key_is_self_loop():
    drv = MockRemoteDriver()
    drv.reset()
    drv.press(KeyPress(button=Button.PLAY_PAUSE))  # 홈에서 정의 안 됨
    assert drv.current_screen_key == "home"


def test_mock_driver_flaky_transition_is_deterministic():
    scen = default_stb_scenario()
    # 홈에서 RIGHT 가 처음 1회는 잘못된 곳(설정 런처)으로 튀도록 주입.
    scen.flaky_transitions[("home", "RIGHT")] = ["launcher_settings"]
    drv = MockRemoteDriver(scen)

    drv.reset()
    drv.press(KeyPress(button=Button.RIGHT))
    assert drv.current_screen_key == "launcher_settings"  # 1회차: 오배송

    drv.reset()  # reset 이 flaky 카운터도 초기화
    drv.press(KeyPress(button=Button.RIGHT))
    assert drv.current_screen_key == "launcher_settings"  # reset 후 다시 1회차


# --------------------------------------------------------------------------- #
# Mock 센스 <-> Mock 드라이버 정합
# --------------------------------------------------------------------------- #


def test_mock_sense_is_screen_sense():
    assert isinstance(MockScreenSense(), ScreenSense)


def test_mock_sense_deterministic_state_identity():
    """같은 화면은 같은 state id, 다른 화면은 다른 id (PRD R2 전제)."""
    drv = MockRemoteDriver()
    sense = MockScreenSense()
    drv.reset()

    home1 = sense.observe(drv.capture()).state
    drv.press(APP_NETFLIX)
    nf = sense.observe(drv.capture()).state
    drv.reset()
    home2 = sense.observe(drv.capture()).state

    assert home1.id == home2.id  # 같은 화면 수렴
    assert home1.id != nf.id  # 다른 화면 분리
    assert home1.kind is StateKind.HOME
    assert nf.kind is StateKind.APP
    assert nf.app_id == "netflix"
    # id 는 정규화된 signature 파생이어야 함
    assert nf.id == compute_state_id(normalize_signature("screen:app:netflix"))


def test_mock_sense_confidence_and_ref():
    res = MockScreenSense().observe(
        RawScreen(image_ref="mock://x", text_hint="Screen:Home", meta={})
    )
    assert res.state.confidence == 1.0
    assert res.low_confidence is False
    assert res.state.screenshot_ref == "mock://x"
    # 정규화(소문자/트림) 적용 확인
    assert res.state.signature == "screen:home"


def test_mock_sense_infers_kind_without_meta():
    sense = MockScreenSense()
    assert sense.observe(RawScreen(text_hint="screen:app:youtube")).state.kind is StateKind.APP
    assert sense.observe(RawScreen(text_hint="screen:app:settings")).state.kind is StateKind.SETTINGS
    assert sense.observe(RawScreen(text_hint="screen:home")).state.kind is StateKind.HOME


# --------------------------------------------------------------------------- #
# RemoteMcpClient — 가정한 계약 파싱 (httpx.MockTransport)
# --------------------------------------------------------------------------- #


def _mcp_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/press":
        return httpx.Response(200, json={"ok": True})
    if path == "/reset":
        return httpx.Response(200, json={"ok": True})
    if path == "/capture":
        png = base64.b64encode(b"\x89PNG-fake").decode()
        return httpx.Response(
            200,
            json={
                "image_ref": "http://172.16.3.x/frame/1.png",
                "image_b64": png,
                "mime": "image/png",
                "text": "HOME",
                "meta": {"res": "1920x1080"},
            },
        )
    if path == "/health":
        return httpx.Response(200, json={"status": "ok", "target": "STB-model-X"})
    return httpx.Response(404, text="not wired")


def _make_mcp_client() -> RemoteMcpClient:
    transport = httpx.MockTransport(_mcp_handler)
    client = httpx.Client(base_url="http://mcp.test", transport=transport)
    return RemoteMcpClient("http://mcp.test", client=client)


def test_mcp_client_is_remote_driver():
    assert isinstance(_make_mcp_client(), RemoteDriver)


def test_mcp_client_press_capture_reset_health():
    drv = _make_mcp_client()
    drv.press(KeyPress(button=Button.HOME))  # 예외 없으면 성공
    drv.press(APP_NETFLIX)
    drv.reset()

    raw = drv.capture()
    assert raw.image_ref.endswith("1.png")
    assert raw.image_bytes == b"\x89PNG-fake"
    assert raw.image_mime == "image/png"
    assert raw.text_hint == "HOME"
    assert raw.meta["res"] == "1920x1080"

    info = drv.info()
    assert info.ready is True
    assert info.target == "STB-model-X"
    assert info.endpoint == "http://mcp.test"


def test_mcp_client_capture_feeds_detection_sense_roundtrip():
    """드라이버 RawScreen 이 detection 센스로 그대로 흘러가는 조립 계약(스모크)."""
    drv = _make_mcp_client()
    raw = drv.capture()
    # detection 센스는 별도 백엔드 필요하므로 여기서는 mock 센스로 판정 가능함만 확인.
    res = MockScreenSense().observe(raw)
    assert res.state.signature == "home"  # text "HOME" -> 정규화 "home"


def test_mcp_client_unreachable_maps_to_driver_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(base_url="http://dead", transport=httpx.MockTransport(boom))
    drv = RemoteMcpClient("http://dead", client=client)
    with pytest.raises(DriverUnavailableError):
        drv.press(KeyPress(button=Button.HOME))


def test_mcp_client_5xx_maps_to_driver_unavailable():
    def err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(err))
    drv = RemoteMcpClient("http://x", client=client)
    with pytest.raises(DriverUnavailableError):
        drv.capture()


def test_mcp_client_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("REMOTE_MCP_URL", raising=False)
    with pytest.raises(DriverUnavailableError):
        RemoteMcpClient.from_env()


# --------------------------------------------------------------------------- #
# DetectionMcpScreenSense — 가정한 계약 파싱
# --------------------------------------------------------------------------- #


def _detect_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/classify":
        return httpx.Response(
            200,
            json={
                "signature": "Screen:App:Netflix",
                "label": "넷플릭스 앱",
                "kind": "app",
                "app_id": "netflix",
                "confidence": 92,  # 0~100 스케일도 흡수되어야 함
            },
        )
    return httpx.Response(404)


def _make_detect_sense() -> DetectionMcpScreenSense:
    client = httpx.Client(
        base_url="http://det.test", transport=httpx.MockTransport(_detect_handler)
    )
    return DetectionMcpScreenSense("http://det.test", client=client)


def test_detection_sense_is_screen_sense():
    assert isinstance(_make_detect_sense(), ScreenSense)


def test_detection_sense_parses_and_normalizes():
    sense = _make_detect_sense()
    res = sense.observe(RawScreen(image_ref="http://x/1.png", image_bytes=b"png"))
    st = res.state
    assert st.signature == "screen:app:netflix"  # 정규화
    assert st.kind is StateKind.APP
    assert st.app_id == "netflix"
    assert st.confidence == pytest.approx(0.92)  # 100 스케일 축소
    assert res.low_confidence is False
    assert "detection-mcp" in sense.backend_name


def test_detection_sense_unreachable_maps_to_sense_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(base_url="http://dead", transport=httpx.MockTransport(boom))
    sense = DetectionMcpScreenSense("http://dead", client=client)
    with pytest.raises(SenseUnavailableError):
        sense.observe(RawScreen(image_ref="x"))


def test_detection_sense_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("DETECTION_MCP_URL", raising=False)
    with pytest.raises(SenseUnavailableError):
        DetectionMcpScreenSense.from_env()

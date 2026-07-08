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

import json

import httpx
import pytest

from remotectl.drivers import (
    CaptureError,
    DriverUnavailableError,
    MockRemoteDriver,
    PressError,
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
# RemoteMcpClient — 실물 ir-mcp(/send) + capture-mcp(/capture) 계약 (MockTransport)
# --------------------------------------------------------------------------- #


class _IrRecorder:
    """ir-mcp /send·/health 핸들러 + 요청 기록(코드셋/키/호출수 검증용)."""

    def __init__(self):
        self.sends: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/send":
            body = json.loads(request.content)
            self.sends.append(body)
            return httpx.Response(200, json={
                "codeset": body["codeset"],
                "resolved_codeset": body["codeset"],
                "key": body["key"],
                "backend": "itach",
                "response": "sendir,1:1,1,38029,...",
            })
        if path == "/health":
            return httpx.Response(200, json={
                "status": "ok", "service": "ir-mcp",
                "backend": "itach", "itach": "10.0.10.20:4998",
            })
        return httpx.Response(404, text="not wired")


def _make_ir_client(rec: _IrRecorder, **kw) -> RemoteMcpClient:
    client = httpx.Client(base_url="http://ir.test", transport=httpx.MockTransport(rec))
    return RemoteMcpClient("http://ir.test", client=client, codeset="ref_remote", **kw)


def test_mcp_client_is_remote_driver():
    assert isinstance(_make_ir_client(_IrRecorder()), RemoteDriver)


def test_mcp_client_send_maps_keys_and_codeset():
    rec = _IrRecorder()
    drv = _make_ir_client(rec)
    drv.press(KeyPress(button=Button.HOME))
    drv.press(KeyPress(button=Button.RIGHT))
    drv.press(APP_NETFLIX)
    assert rec.sends == [
        {"codeset": "ref_remote", "key": "HOME"},
        {"codeset": "ref_remote", "key": "DPAD_RIGHT"},  # 방향키 → DPAD_*
        {"codeset": "ref_remote", "key": "NETFLIX"},      # 앱단축 → 대문자 키명
    ]


def test_mcp_client_repeat_sends_n_times():
    rec = _IrRecorder()
    drv = _make_ir_client(rec)
    drv.press(KeyPress(button=Button.RIGHT, repeat=3))  # ir-mcp repeat 미지원 → 3회 /send
    assert rec.sends == [{"codeset": "ref_remote", "key": "DPAD_RIGHT"}] * 3


def test_mcp_client_reset_presses_home():
    rec = _IrRecorder()
    drv = _make_ir_client(rec, reset_home_presses=2)  # reset 엔드포인트 없음 → HOME N회
    drv.reset()
    assert rec.sends == [{"codeset": "ref_remote", "key": "HOME"}] * 2


def test_mcp_client_health_info():
    drv = _make_ir_client(_IrRecorder())
    info = drv.info()
    assert info.ready is True
    assert info.name == "ir-mcp"
    assert info.target == "itach"
    assert info.endpoint == "http://ir.test"


def test_mcp_client_capture_extracts_frame_from_capture_mcp(monkeypatch):
    def cap_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/capture"
        body = json.loads(request.content)
        assert body["target"] == "dut"
        return httpx.Response(200, json={
            "file_path": "/data/captures/scenario_abc.mp4", "duration": 1,
        })

    cap_client = httpx.Client(
        base_url="http://cap.test", transport=httpx.MockTransport(cap_handler)
    )
    drv = _make_ir_client(_IrRecorder(), capture_client=cap_client)
    # ffmpeg 의존을 피하려 프레임 추출만 스텁(파일경로 → 가짜 PNG 바이트).
    monkeypatch.setattr(drv, "_extract_frame", lambda p: b"\x89PNG-frame")

    raw = drv.capture()
    assert raw.image_ref == "/data/captures/scenario_abc.mp4"
    assert raw.image_bytes == b"\x89PNG-frame"
    assert raw.image_mime == "image/png"
    assert raw.meta["source"] == "capture-mcp"


def test_mcp_client_capture_without_capture_url_unavailable():
    drv = _make_ir_client(_IrRecorder())  # capture_client 없음
    with pytest.raises(DriverUnavailableError):
        drv.capture()


def test_mcp_client_capture_4xx_maps_to_capture_error():
    cap_client = httpx.Client(
        base_url="http://cap.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad target")),
    )
    drv = _make_ir_client(_IrRecorder(), capture_client=cap_client)
    with pytest.raises(CaptureError):
        drv.capture()


def test_mcp_client_unreachable_maps_to_driver_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(base_url="http://dead", transport=httpx.MockTransport(boom))
    drv = RemoteMcpClient("http://dead", client=client)
    with pytest.raises(DriverUnavailableError):
        drv.press(KeyPress(button=Button.HOME))


def test_mcp_client_4xx_send_maps_to_press_error():
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="unknown key")

    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(bad))
    drv = RemoteMcpClient("http://x", client=client)
    with pytest.raises(PressError):
        drv.press(KeyPress(button=Button.HOME))


def test_mcp_client_5xx_maps_to_driver_unavailable():
    def err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(err))
    drv = RemoteMcpClient("http://x", client=client)
    with pytest.raises(DriverUnavailableError):
        drv.press(KeyPress(button=Button.HOME))


def test_mcp_client_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("IR_MCP_URL", raising=False)
    monkeypatch.delenv("REMOTE_MCP_URL", raising=False)
    with pytest.raises(DriverUnavailableError):
        RemoteMcpClient.from_env()


# --------------------------------------------------------------------------- #
# DetectionMcpScreenSense — 실물 /check/screen + description 로컬 합성
# --------------------------------------------------------------------------- #


def _detect_handler_with(description: str, **extra):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/check/screen":
            body = json.loads(request.content)
            assert "image_base64" in body  # 이미지 바이트가 b64 로 실려 감
            return httpx.Response(200, json={
                "verdict": "normal", "tier": "vision",
                "confidence": 0.92, "description": description, **extra,
            })
        return httpx.Response(404)
    return handler


def _make_detect_sense(description: str = "넷플릭스 앱 홈 화면. 추천 콘텐츠 표시") -> DetectionMcpScreenSense:
    client = httpx.Client(
        base_url="http://det.test",
        transport=httpx.MockTransport(_detect_handler_with(description)),
    )
    return DetectionMcpScreenSense("http://det.test", client=client)


def _raw_png() -> RawScreen:
    return RawScreen(image_ref="http://x/1.png", image_bytes=b"\x89PNG-frame")


def test_detection_sense_is_screen_sense():
    assert isinstance(_make_detect_sense(), ScreenSense)


def test_detection_sense_synthesizes_state_from_description():
    sense = _make_detect_sense("넷플릭스 앱 홈 화면. 추천 콘텐츠 표시")
    st = sense.observe(_raw_png()).state
    assert st.kind is StateKind.APP        # 앱 감지 → APP
    assert st.app_id == "netflix"          # "넷플릭스" 키워드
    assert st.confidence == pytest.approx(0.92)
    assert "detection-mcp" in sense.backend_name


def test_detection_sense_kind_keywords():
    settings = _make_detect_sense("STB 환경설정 메뉴 화면").observe(_raw_png()).state
    assert settings.kind is StateKind.SETTINGS
    playback = _make_detect_sense("영상 재생 중입니다").observe(_raw_png()).state
    assert playback.kind is StateKind.PLAYBACK


def test_detection_sense_signature_stable_across_phrasing():
    """표현이 달라도 같은 의미 키워드면 같은 state id 로 수렴한다(R2 전제)."""
    a = _make_detect_sense("넷플릭스 홈").observe(_raw_png()).state
    b = _make_detect_sense("넷플릭스 홈 화면입니다").observe(_raw_png()).state
    assert a.id == b.id


def test_detection_sense_missing_description_errors():
    from remotectl.sense import ScreenSenseError

    client = httpx.Client(
        base_url="http://det.test",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"verdict": "normal", "confidence": 0.5})
        ),
    )
    sense = DetectionMcpScreenSense("http://det.test", client=client)
    with pytest.raises(ScreenSenseError):
        sense.observe(_raw_png())


def test_detection_sense_unreachable_maps_to_sense_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(base_url="http://dead", transport=httpx.MockTransport(boom))
    sense = DetectionMcpScreenSense("http://dead", client=client)
    with pytest.raises(SenseUnavailableError):
        sense.observe(_raw_png())


def test_detection_sense_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("DETECTION_MCP_URL", raising=False)
    with pytest.raises(SenseUnavailableError):
        DetectionMcpScreenSense.from_env()

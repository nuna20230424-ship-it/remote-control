"""RemoteMcpClient — 사내 STB 스택(ir-mcp + capture-mcp)으로 실제 STB 를 제어하는 RemoteDriver.

실물 계약(2026-07 stb-ai-tc-automation 조사 확정)에 맞춰 배선됨. 가정했던 단일 remote-MCP
(`/press`·`/capture`)는 실재하지 않고, 제어와 캡처가 **두 서비스**로 분리돼 있다.

============================================================================
실물 계약 (as-wired)
============================================================================
1) 리모컨 제어 = **ir-mcp** (FastAPI, base = IR_MCP_URL, 예: http://172.16.3.x:8002)
     POST /send   req {"codeset": "<codeset>", "key": "<KEY>"}
                  res {"codeset","resolved_codeset","key","backend","response"}  (성공=HTTP2xx)
     GET  /health res {"status":"ok","service":"ir-mcp","backend":"itach",...}
   - repeat 파라미터 없음 → key.repeat 만큼 /send 를 N회 호출.
   - reset 엔드포인트 없음 → HOME 키를 REMOTECTL_RESET_HOME_PRESSES 회 눌러 폴백.
   - codeset(REMOTECTL_IR_CODESET) 는 대상 단말/리모컨에 맞춰야 한다(예: ref_remote/kt_new).

2) 화면 캡처 = **capture-mcp** (FastAPI, base = CAPTURE_MCP_URL, 예: http://172.16.3.x:8001)
     POST /capture req {"target":"dut","duration_sec":<int>,"label":<str|null>}
                   res {"file_path":"/data/captures/xxx.mp4", ...}  (N초 **영상**)
   - capture-mcp 는 스틸이 아니라 MP4 를 준다 → ffmpeg 로 **마지막 프레임**을 추출해 스틸로 쓴다.
   - file_path 는 이 프로세스가 읽을 수 있어야 한다(capture-mcp 와 공유 볼륨 가정).
   - CAPTURE_MCP_URL 미설정이면 capture() 는 DriverUnavailableError(캡처 미가용).

키맵(canonical Button → ir-mcp 키명)은 아래 DEFAULT_KEYMAP 참조. 코드셋별로 없는 키는
ir-mcp 가 4xx 로 거절 → PressError 로 정규화된다.
============================================================================
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

import httpx

from remotectl.drivers.base import (
    CaptureError,
    DriverInfo,
    DriverUnavailableError,
    PressError,
    RawScreen,
    RemoteDriver,
)
from remotectl.models import Button, KeyPress

__all__ = ["RemoteMcpClient", "DEFAULT_KEYMAP"]


# --------------------------------------------------------------------------- #
# canonical Button → ir-mcp 코드셋 키명 (실물 kt_new/ref_remote 키 어휘 기준)
# --------------------------------------------------------------------------- #
# 방향키는 DPAD_*, 음량은 VOLUME_*, 숫자는 "0"~"9". 코드셋에 따라 일부 키가 없을 수 있으며,
# 그 경우 ir-mcp 가 4xx 를 반환해 PressError 로 정규화된다.
DEFAULT_KEYMAP: dict[Button, str] = {
    Button.HOME: "HOME",
    Button.BACK: "BACK",
    Button.UP: "DPAD_UP",
    Button.DOWN: "DPAD_DOWN",
    Button.LEFT: "DPAD_LEFT",
    Button.RIGHT: "DPAD_RIGHT",
    Button.OK: "OK",
    Button.MENU: "MENU",
    Button.EXIT: "EXIT",
    Button.PLAY_PAUSE: "PLAY_PAUSE",
    Button.STOP: "STOP",
    Button.REWIND: "REWIND",
    Button.FAST_FORWARD: "FAST_FORWARD",
    Button.VOL_UP: "VOLUME_UP",
    Button.VOL_DOWN: "VOLUME_DOWN",
    Button.MUTE: "MUTE",
    Button.CH_UP: "CH_UP",
    Button.CH_DOWN: "CH_DOWN",
    Button.POWER: "POWER",
    Button.NUM_0: "0",
    Button.NUM_1: "1",
    Button.NUM_2: "2",
    Button.NUM_3: "3",
    Button.NUM_4: "4",
    Button.NUM_5: "5",
    Button.NUM_6: "6",
    Button.NUM_7: "7",
    Button.NUM_8: "8",
    Button.NUM_9: "9",
}


def _env_int(name: str, default: int) -> int:
    """환경변수를 int 로 파싱(빈 값/미설정/파싱 실패 시 기본값)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


class RemoteMcpClient(RemoteDriver):
    """ir-mcp(제어) + capture-mcp(캡처) HTTP 어댑터.

    코어 엔진은 이 클래스 내부를 모르며 RemoteDriver 계약만 본다(M5). MockRemoteDriver 와
    동일하게 학습/실행 엔진에 그대로 꽂힌다.

    구성
    ----
    - base_url:     ir-mcp base URL(제어). from_env 가 IR_MCP_URL/REMOTE_MCP_URL 에서 읽는다.
    - codeset:      /send 에 넘길 코드셋(REMOTECTL_IR_CODESET). 대상 단말/리모컨과 일치해야 함.
    - capture_url:  capture-mcp base URL(옵션). 없으면 capture() 미가용.
    - capture_target: "ref" | "dut"(REMOTECTL_CAPTURE_TARGET, 기본 dut).
    - capture_duration_sec: 캡처 영상 길이 초(REMOTECTL_CAPTURE_DURATION_SEC, 기본 1).
    - reset_home_presses: reset() 시 HOME 반복 횟수(REMOTECTL_RESET_HOME_PRESSES, 기본 1).
    - keymap:       canonical Button → ir-mcp 키명.
    - token/timeout: 인증/타임아웃.
    """

    def __init__(
        self,
        base_url: str,
        *,
        codeset: str = "ref_remote",
        capture_url: Optional[str] = None,
        capture_target: str = "dut",
        capture_duration_sec: int = 1,
        reset_home_presses: int = 1,
        keymap: Optional[dict[Button, str]] = None,
        timeout: float = 10.0,
        token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
        capture_client: Optional[httpx.Client] = None,
    ):
        if not base_url:
            raise ValueError("base_url(IR_MCP_URL/REMOTE_MCP_URL) 가 필요합니다.")
        self._base_url = base_url.rstrip("/")
        self._codeset = codeset
        self._capture_url = capture_url.rstrip("/") if capture_url else None
        self._capture_target = capture_target
        self._capture_duration_sec = max(1, int(capture_duration_sec))
        self._reset_home_presses = max(0, int(reset_home_presses))
        self._keymap = dict(keymap or DEFAULT_KEYMAP)
        self._token = token

        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=timeout, headers=headers
        )
        self._owns_client = client is None

        # capture-mcp 는 지연 시간이 커(영상 녹화) 별도 timeout 을 넉넉히 준다.
        if capture_client is not None:
            self._capture_client: Optional[httpx.Client] = capture_client
            self._owns_capture_client = False
        elif self._capture_url:
            self._capture_client = httpx.Client(
                base_url=self._capture_url,
                timeout=timeout + self._capture_duration_sec + 30.0,
                headers=headers,
            )
            self._owns_capture_client = True
        else:
            self._capture_client = None
            self._owns_capture_client = False

    # --- 팩토리 ----------------------------------------------------------- #

    @classmethod
    def from_env(cls, **overrides) -> "RemoteMcpClient":
        """환경변수로부터 구성.

        - IR_MCP_URL | REMOTE_MCP_URL  : ir-mcp base URL(필수).
        - REMOTECTL_IR_CODESET         : 코드셋(기본 ref_remote).
        - CAPTURE_MCP_URL              : capture-mcp base URL(옵션; 없으면 capture 미가용).
        - REMOTECTL_CAPTURE_TARGET     : ref | dut(기본 dut).
        - REMOTECTL_CAPTURE_DURATION_SEC: 캡처 영상 초(기본 1).
        - REMOTECTL_RESET_HOME_PRESSES : reset 시 HOME 반복(기본 1).
        - REMOTE_MCP_TOKEN             : 인증 토큰(옵션).
        - REMOTE_MCP_TIMEOUT           : 타임아웃 초(기본 10).
        """
        base_url = (
            overrides.pop("base_url", None)
            or os.environ.get("IR_MCP_URL")
            or os.environ.get("REMOTE_MCP_URL", "")
        )
        if not base_url:
            raise DriverUnavailableError(
                "IR_MCP_URL/REMOTE_MCP_URL 미설정. 실 STB 배선 전이거나 환경변수 누락. "
                "개발/테스트는 MockRemoteDriver 를 사용하라."
            )
        kwargs: dict[str, object] = {
            "codeset": overrides.pop("codeset", None)
            or os.environ.get("REMOTECTL_IR_CODESET", "ref_remote"),
            "capture_url": overrides.pop("capture_url", None)
            or os.environ.get("CAPTURE_MCP_URL")
            or None,
            "capture_target": overrides.pop("capture_target", None)
            or os.environ.get("REMOTECTL_CAPTURE_TARGET", "dut"),
            "capture_duration_sec": overrides.pop("capture_duration_sec", None)
            or _env_int("REMOTECTL_CAPTURE_DURATION_SEC", 1),
            "reset_home_presses": overrides.pop("reset_home_presses", None)
            or _env_int("REMOTECTL_RESET_HOME_PRESSES", 1),
            "token": overrides.pop("token", None) or os.environ.get("REMOTE_MCP_TOKEN"),
        }
        timeout = overrides.pop("timeout", None)
        if timeout is None:
            timeout = float(os.environ.get("REMOTE_MCP_TIMEOUT", "10.0"))
        kwargs["timeout"] = timeout
        kwargs.update(overrides)
        return cls(base_url, **kwargs)  # type: ignore[arg-type]

    # --- RemoteDriver 계약 ------------------------------------------------ #

    def press(self, key: KeyPress) -> None:
        """ir-mcp POST /send 로 키를 송신한다(repeat 만큼 N회)."""
        mcp_key = self._map_key(key)
        # ir-mcp 는 repeat 파라미터가 없다 → repeat 횟수만큼 개별 /send.
        for _ in range(key.repeat):
            resp = self._request(
                self._client, "POST", "/send",
                json={"codeset": self._codeset, "key": mcp_key},
            )
            self._raise_if_not_ok(resp, PressError, op=f"send({mcp_key})")

    def capture(self) -> RawScreen:
        """capture-mcp 로 짧은 영상을 녹화하고 마지막 프레임을 스틸(PNG)로 추출한다."""
        if self._capture_client is None:
            raise DriverUnavailableError(
                "CAPTURE_MCP_URL 미설정 — 화면 캡처 미가용. capture-mcp base URL 을 배선하라."
            )
        resp = self._request(
            self._capture_client, "POST", "/capture",
            json={
                "target": self._capture_target,
                "duration_sec": self._capture_duration_sec,
                "label": "remotectl-observe",
            },
        )
        self._raise_if_not_ok(resp, CaptureError, op="capture")
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise CaptureError(f"capture 응답 JSON 파싱 실패: {e}") from e

        file_path = data.get("file_path") or data.get("path")
        if not file_path:
            raise CaptureError(
                "capture 응답에 file_path 가 없습니다. capture-mcp 계약 확인 필요."
            )

        image_bytes = self._extract_frame(file_path)
        return RawScreen(
            image_ref=file_path,
            image_bytes=image_bytes,
            image_mime="image/png",
            text_hint=None,
            meta={
                "capture_target": self._capture_target,
                "duration_sec": data.get("duration") or self._capture_duration_sec,
                "source": "capture-mcp",
            },
        )

    def reset(self) -> None:
        """ir-mcp 에 reset 오퍼레이션이 없으므로 HOME 키 반복으로 시작점(HOME) 복귀."""
        home_key = self._keymap.get(Button.HOME, "HOME")
        for _ in range(self._reset_home_presses):
            resp = self._request(
                self._client, "POST", "/send",
                json={"codeset": self._codeset, "key": home_key},
            )
            self._raise_if_not_ok(resp, PressError, op="reset(HOME)")

    def info(self) -> DriverInfo:
        ready: Optional[bool] = None
        target: Optional[str] = None
        try:
            resp = self._request(self._client, "GET", "/health")
            if resp.status_code == 200:
                body = resp.json()
                ready = body.get("status") == "ok"
                # ir-mcp health 는 backend 별 디바이스 정보를 함께 준다(itach/adb_target 등).
                target = (
                    body.get("backend")
                    or body.get("adb_target")
                    or body.get("itach")
                )
            else:
                ready = False
        except DriverUnavailableError:
            ready = False
        except Exception:  # noqa: BLE001  (health 는 부가 정보라 실패해도 info 는 반환)
            ready = None
        return DriverInfo(
            name="ir-mcp",
            target=target,
            endpoint=self._base_url,
            supports_capture=self._capture_client is not None,
            ready=ready,
        )

    def available_keys(self) -> list[KeyPress]:
        """ir-mcp GET /codesets/{codeset} 로 대상 코드셋의 실제 키 전체를 KeyPress 로 보고한다.

        커버리지 정본이 "고정 7키"가 아니라 **이 단말 리모컨이 실제로 갖는 키 집합**이 되게 한다.
        ir-mcp 키명을 키맵 역매핑으로 canonical Button 에 대응시키고, 매핑에 없는 키(NETFLIX 등
        앱/특수키)는 APP_SHORTCUT 로 표현한다. 도달 불가/미확정이면 빈 리스트(학습기가 폴백).
        """
        try:
            resp = self._request(
                self._client, "GET", f"/codesets/{self._codeset}"
            )
        except DriverUnavailableError:
            return []
        if resp.status_code >= 400:
            return []
        try:
            keys = resp.json().get("keys") or []
        except Exception:  # noqa: BLE001
            return []

        inverse = {name: button for button, name in self._keymap.items()}
        out: list[KeyPress] = []
        for name in keys:
            button = inverse.get(name)
            if button is not None:
                out.append(KeyPress(button=button))
            else:
                # 키맵에 없는 키(앱 단축/특수키)는 앱 단축으로 표현 → _map_key 가 대문자 키명으로 송신.
                out.append(KeyPress(button=Button.APP_SHORTCUT, app_shortcut=name.lower()))
        return out

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        if self._owns_capture_client and self._capture_client is not None:
            self._capture_client.close()

    # --- 내부 헬퍼 -------------------------------------------------------- #

    def _map_key(self, key: KeyPress) -> str:
        """canonical KeyPress → ir-mcp 키명.

        APP_SHORTCUT 는 앱 식별자를 대문자 키명으로 변환한다(예: "netflix" → "NETFLIX").
        코드셋에 해당 앱 키가 있어야 실제 송신된다.
        """
        if key.button is Button.APP_SHORTCUT:
            if not key.app_shortcut:
                raise PressError("APP_SHORTCUT 인데 app_shortcut 값이 없습니다.")
            return key.app_shortcut.strip().upper()
        mapped = self._keymap.get(key.button)
        if mapped is None:
            raise PressError(f"키맵에 없는 버튼: {key.button}.")
        return mapped

    def _extract_frame(self, file_path: str) -> bytes:
        """ffmpeg 로 영상 파일의 마지막 프레임을 PNG 바이트로 추출한다.

        press 직후 화면이 안정된 마지막 프레임을 쓴다(-sseof). ffmpeg 미설치/경로 접근 불가/
        추출 실패는 CaptureError 로 정규화한다.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise CaptureError(
                "ffmpeg 를 찾을 수 없습니다 — capture-mcp 영상에서 프레임 추출 불가. "
                "ffmpeg 설치 필요."
            )
        # -sseof -0.2 : 끝에서 0.2초 지점부터, -frames:v 1 : 한 프레임, PNG 로 stdout.
        cmd = [
            ffmpeg, "-nostdin", "-loglevel", "error",
            "-sseof", "-0.2", "-i", file_path,
            "-frames:v", "1", "-f", "image2", "-c:v", "png", "pipe:1",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30.0)
        except FileNotFoundError as e:
            raise CaptureError(f"ffmpeg 실행 실패: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise CaptureError(f"ffmpeg 프레임 추출 타임아웃: {e}") from e
        if proc.returncode != 0 or not proc.stdout:
            err = proc.stderr.decode("utf-8", "replace")[:200] if proc.stderr else ""
            raise CaptureError(
                f"프레임 추출 실패(rc={proc.returncode}, path={file_path}): {err}. "
                "파일 접근성(공유 볼륨) 확인 필요."
            )
        return proc.stdout

    def _request(
        self, client: httpx.Client, method: str, path: str, **kwargs
    ) -> httpx.Response:
        """HTTP 요청 1회. 연결/타임아웃 실패는 DriverUnavailableError 로 승격."""
        try:
            return client.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise DriverUnavailableError(f"MCP 타임아웃({path}): {e}") from e
        except httpx.HTTPError as e:  # ConnectError/네트워크 등
            raise DriverUnavailableError(
                f"MCP 도달 불가({path}): {e}. 사내망/엔드포인트/포트 확인 필요."
            ) from e

    @staticmethod
    def _raise_if_not_ok(resp: httpx.Response, exc_type, *, op: str) -> None:
        """응답 성공 판정(ir-mcp/capture-mcp 는 성공을 HTTP 2xx 로 표현)."""
        if resp.status_code >= 500:
            raise DriverUnavailableError(
                f"MCP {op} 서버 오류 {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise exc_type(f"MCP {op} 실패 {resp.status_code}: {resp.text[:200]}")

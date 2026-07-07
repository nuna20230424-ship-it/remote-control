"""RemoteMcpClient — 사내망 remote-MCP(HTTP)로 실제 STB 를 제어하는 RemoteDriver 어댑터.

상태: 스텁(계약 골격). 실제 엔드포인트가 미확정이므로(PRD R1) HTTP 요청 형태를
"가정한 MCP 계약"으로 명시해 두고, 실물이 확정되면 아래 [WIRE] 표식 지점만 채우면 된다.
코어 엔진은 이 클래스의 내부를 모르며, RemoteDriver 계약만 본다(M5).

============================================================================
가정한 MCP 계약 (assumed contract) — 실물 확정 시 이 표를 정본과 대조하라
============================================================================
전송: HTTP/JSON, base URL = 환경변수 REMOTE_MCP_URL (예: http://172.16.3.xxx:PORT).
      (detection-mcp 가 172.16.3.136:8103 인 것으로 보아 remote-MCP 도 172.16.3.x 대역으로 추정.)

가정 오퍼레이션(operation):
  1) press      — 리모컨 키 입력
       POST {base}/press
       req : {"key": "<CANONICAL_KEY>", "repeat": <int>, "app": "<app_id|null>"}
       res : {"ok": true}                                  (본문 형식 미확정)
  2) capture    — 현재 화면 캡처
       GET  {base}/capture   (or POST; 미확정)
       res : {"image_ref": "<url|path>", "image_b64": "<...|null>",
              "mime": "image/png", "text": "<osd/debug|null>", "meta": {...}}
  3) reset      — 시작 지점(HOME)으로 복귀
       POST {base}/reset
       res : {"ok": true}
  4) health     — 도달성/준비 확인(옵션)
       GET  {base}/health
       res : {"status": "ok", "target": "<stb model/host>"}

키코드 매핑(canonical Button -> MCP 키 문자열):
  아래 _DEFAULT_KEYMAP 은 "추정"이다. 실물 remote-MCP 가 기대하는 키 문자열
  (예: "KEY_HOME", "0x0A", "netflix" 등)에 맞게 [WIRE-KEYMAP] 에서 교체하라.

미확정/사용자 확인 필요 사항([WIRE] 지점):
  - [WIRE-URL]     : REMOTE_MCP_URL 실제 host:port/경로 프리픽스.
  - [WIRE-PATHS]   : 각 오퍼레이션의 실제 method + path.
  - [WIRE-KEYMAP]  : canonical Button -> 실제 키코드 문자열.
  - [WIRE-PRESS]   : press 요청 바디 스키마(단발 vs repeat 파라미터 vs N회 호출).
  - [WIRE-CAPTURE] : capture 응답 파싱(이미지가 URL 인가 base64 인가, 필드명).
  - [WIRE-RESET]   : reset 이 별도 오퍼레이션인가, 아니면 HOME 다중 press 로 대체인가.
  - [WIRE-AUTH]    : 인증 헤더/토큰 필요 여부(REMOTE_MCP_TOKEN 예약).
  - [WIRE-ERRORS]  : 실패 응답 형태(HTTP status vs 바디 ok=false) → 예외 매핑.
============================================================================
"""

from __future__ import annotations

import base64
import os
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
# [WIRE-KEYMAP] canonical Button -> remote-MCP 가 기대하는 키 문자열 (추정)
# --------------------------------------------------------------------------- #
# 실물이 "KEY_HOME" / "0x..." / 소문자 등 무엇을 원하는지 확인 후 값을 교체하라.
# 여기서는 우선 canonical 심볼 그대로 보낸다(가장 무해한 기본값).
DEFAULT_KEYMAP: dict[Button, str] = {b: b.value for b in Button}


class RemoteMcpClient(RemoteDriver):
    """remote-MCP HTTP 어댑터(스텁).

    엔드포인트 확정 전이라 실제 요청/응답 파싱은 [WIRE] 지점에 TODO 로 표시되어 있고,
    지금은 호출 형태만 갖춘 채 명확한 예외로 "미배선" 을 알린다. 배선 후에는 Mock 과
    동일하게 RemoteDriver 로서 엔진에 그대로 꽂힌다.

    구성
    ----
    - base_url: REMOTE_MCP_URL. from_env() 로 환경변수에서 읽는다.
    - keymap: canonical Button -> 실제 키 문자열([WIRE-KEYMAP]).
    - timeout: HTTP 타임아웃(초). 도달불가/지연은 DriverUnavailableError 로 승격.
    - token: 인증 토큰(REMOTE_MCP_TOKEN, [WIRE-AUTH]). 없으면 헤더 미첨부.
    """

    def __init__(
        self,
        base_url: str,
        *,
        keymap: Optional[dict[Button, str]] = None,
        timeout: float = 5.0,
        token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ):
        if not base_url:
            raise ValueError("base_url(REMOTE_MCP_URL) 가 필요합니다.")
        self._base_url = base_url.rstrip("/")
        self._keymap = dict(keymap or DEFAULT_KEYMAP)
        self._token = token
        # 주입된 client(테스트에서 httpx.MockTransport 를 꽂기 위함)가 있으면 재사용.
        headers = {"Content-Type": "application/json"}
        if token:  # [WIRE-AUTH] 실제 헤더 이름/스킴 확인 필요(Bearer? X-Token?).
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=timeout, headers=headers
        )
        self._owns_client = client is None

    # --- 팩토리 ----------------------------------------------------------- #

    @classmethod
    def from_env(cls, **overrides) -> "RemoteMcpClient":
        """환경변수로부터 구성. [WIRE-URL]

        - REMOTE_MCP_URL   : base URL(필수).
        - REMOTE_MCP_TOKEN : 인증 토큰(옵션).
        - REMOTE_MCP_TIMEOUT: 타임아웃 초(옵션, 기본 5).
        """
        base_url = overrides.pop("base_url", None) or os.environ.get("REMOTE_MCP_URL", "")
        if not base_url:
            raise DriverUnavailableError(
                "REMOTE_MCP_URL 미설정. 실 remote-MCP 배선 전이거나 환경변수 누락. "
                "개발/테스트는 MockRemoteDriver 를 사용하라."
            )
        token = overrides.pop("token", None) or os.environ.get("REMOTE_MCP_TOKEN")
        timeout = overrides.pop("timeout", None)
        if timeout is None:
            timeout = float(os.environ.get("REMOTE_MCP_TIMEOUT", "5.0"))
        return cls(base_url, token=token, timeout=timeout, **overrides)

    # --- RemoteDriver 계약 ------------------------------------------------ #

    def press(self, key: KeyPress) -> None:
        mcp_key = self._map_key(key)
        # [WIRE-PRESS] 요청 바디/경로/메서드를 실물 계약에 맞게 교체.
        payload = {
            "key": mcp_key,
            "repeat": key.repeat,
            "app": key.app_shortcut,  # APP_SHORTCUT 일 때만 값이 있음.
        }
        # [WIRE-PATHS] 실제 press 경로/메서드 확인.
        resp = self._request("POST", "/press", json=payload)
        self._raise_if_not_ok(resp, PressError, op="press")

    def capture(self) -> RawScreen:
        # [WIRE-PATHS] 실제 capture 경로/메서드(GET vs POST) 확인.
        resp = self._request("GET", "/capture")
        self._raise_if_not_ok(resp, CaptureError, op="capture")
        # [WIRE-CAPTURE] 응답 필드명/이미지 표현(URL vs base64) 을 실물에 맞게 파싱.
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise CaptureError(f"capture 응답 JSON 파싱 실패: {e}") from e

        image_bytes: Optional[bytes] = None
        b64 = data.get("image_b64")
        if b64:
            try:
                image_bytes = base64.b64decode(b64)
            except Exception as e:  # noqa: BLE001
                raise CaptureError(f"image_b64 디코드 실패: {e}") from e

        return RawScreen(
            image_ref=data.get("image_ref"),
            image_bytes=image_bytes,
            image_mime=data.get("mime"),
            text_hint=data.get("text"),
            meta=data.get("meta") or {},
        )

    def reset(self) -> None:
        # [WIRE-RESET] 전용 reset 오퍼레이션이 없으면 HOME 다중 press 로 폴백하도록 교체.
        resp = self._request("POST", "/reset")
        self._raise_if_not_ok(resp, PressError, op="reset")

    def info(self) -> DriverInfo:
        ready: Optional[bool] = None
        target: Optional[str] = None
        # [WIRE-PATHS] health 오퍼레이션이 없으면 이 블록을 제거하거나 대체하라.
        try:
            resp = self._request("GET", "/health")
            if resp.status_code == 200:
                body = resp.json()
                ready = body.get("status") == "ok"
                target = body.get("target")
            else:
                ready = False
        except DriverUnavailableError:
            ready = False
        except Exception:  # noqa: BLE001  (health 는 부가 정보라 실패해도 info 는 반환)
            ready = None
        return DriverInfo(
            name="remote-mcp",
            target=target,
            endpoint=self._base_url,
            supports_capture=True,
            ready=ready,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- 내부 헬퍼 -------------------------------------------------------- #

    def _map_key(self, key: KeyPress) -> str:
        """canonical KeyPress -> remote-MCP 키 문자열. [WIRE-KEYMAP]

        APP_SHORTCUT 는 app_shortcut 식별자를 그대로 키로 쓴다(실물이 앱 실행을 어떻게
        받는지에 따라 press 바디의 "app" 필드로 옮겨야 할 수도 있음 → [WIRE-PRESS]).
        """
        if key.button is Button.APP_SHORTCUT:
            return key.app_shortcut or ""
        mapped = self._keymap.get(key.button)
        if mapped is None:
            raise PressError(f"키맵에 없는 버튼: {key.button}. [WIRE-KEYMAP] 확인 필요.")
        return mapped

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """HTTP 요청 1회. 연결/타임아웃 실패는 DriverUnavailableError 로 승격. [WIRE-ERRORS]"""
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise DriverUnavailableError(
                f"remote-MCP 타임아웃({self._base_url}{path}): {e}"
            ) from e
        except httpx.HTTPError as e:  # ConnectError/네트워크 등
            raise DriverUnavailableError(
                f"remote-MCP 도달 불가({self._base_url}{path}): {e}. "
                "사내망/엔드포인트/포트 확인 필요."
            ) from e

    @staticmethod
    def _raise_if_not_ok(resp: httpx.Response, exc_type, *, op: str) -> None:
        """응답 성공 판정. [WIRE-ERRORS]

        지금은 HTTP status 만 본다. 실물이 200 + {"ok": false} 형태로 실패를 표현하면
        여기서 바디의 ok 필드도 함께 검사하도록 교체하라.
        """
        if resp.status_code >= 500:
            # 서버측 오류/미기동은 도달성 문제로 취급(진단 일관성).
            raise DriverUnavailableError(
                f"remote-MCP {op} 서버 오류 {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise exc_type(
                f"remote-MCP {op} 실패 {resp.status_code}: {resp.text[:200]}"
            )
        # [WIRE-ERRORS] 필요 시:
        #   body = resp.json(); if not body.get("ok", True): raise exc_type(...)

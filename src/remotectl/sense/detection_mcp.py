"""DetectionMcpScreenSense — detection-mcp/VLM 백엔드로 화면을 판정하는 ScreenSense 어댑터.

상태: 스텁(계약 골격). detection-mcp 의 정확한 오퍼레이션이 미확정이므로(PRD 아웃오브스코프:
실 detection-mcp/VLM 실연동은 후속) HTTP 호출 형태를 "가정한 계약" 으로 명시하고,
실물 확정 시 아래 [WIRE] 지점만 채우면 MockScreenSense 와 동일하게 엔진에 꽂힌다.

메모리상 근거(사용자 프로젝트 기준):
  - detection-mcp 는 사내망 172.16.3.136:8103 로 관측됨(도달성 이슈가 error verdict 주원인).
  - 로컬 VLM 판정기: qwen2.5vl:7b + 보정 프롬프트 조합이 실측 96%. num_ctx 16k 필수.
  - 최대 레버는 "프롬프트"(모델 크기보다). 32b 는 대상 Mac 에서 비현실적.
  → 이 어댑터는 detection-mcp(HTTP)를 우선 호출하고, 프롬프트/모델은 서버 설정 또는
    아래 요청 파라미터로 넘기는 형태를 가정한다.

============================================================================
가정한 detection-mcp 계약 (assumed) — 실물 확정 시 대조
============================================================================
전송: HTTP/JSON, base URL = 환경변수 DETECTION_MCP_URL (예: http://172.16.3.136:8103).
가정 오퍼레이션:
  classify — 화면 이미지를 상태로 판정
    POST {base}/classify   ([WIRE-PATHS])
    req : {"image_ref": "<url|path|null>", "image_b64": "<...|null>",
           "text_hint": "<osd|null>", "prompt": "<판정 보정 프롬프트|null>",
           "model": "qwen2.5vl:7b"}                              ([WIRE-REQ])
    res : {"signature": "<정규화된 화면 서명>", "label": "<사람 라벨>",
           "kind": "home|app|menu|settings|playback|live_tv|dialog|loading|unknown",
           "app_id": "<netflix|null>", "confidence": 0.0~1.0}    ([WIRE-RES])

미확정/사용자 확인 필요([WIRE] 지점):
  - [WIRE-URL]   : DETECTION_MCP_URL host:port/경로.
  - [WIRE-PATHS] : classify 오퍼레이션의 method + path.
  - [WIRE-REQ]   : 이미지 전달 방식(URL vs base64), 프롬프트/모델 파라미터명.
  - [WIRE-RES]   : 응답 필드명, kind 라벨 체계 매핑, confidence 스케일.
  - [WIRE-PROMPT]: 보정 프롬프트 본문(실측상 최대 레버). 여기 CALIBRATION_PROMPT 에 채움.
  - [WIRE-AUTH]  : 인증 필요 여부(DETECTION_MCP_TOKEN 예약).
============================================================================
"""

from __future__ import annotations

import base64
import os
from typing import Optional

import httpx

from remotectl.drivers.base import RawScreen
from remotectl.models import StateKind
from remotectl.sense.base import (
    ScreenSense,
    ScreenSenseError,
    SenseResult,
    SenseUnavailableError,
)

__all__ = ["DetectionMcpScreenSense", "CALIBRATION_PROMPT"]


# [WIRE-PROMPT] 판정 보정 프롬프트(실측상 정확도의 최대 레버). 실제 문구는 튜닝 후 확정.
CALIBRATION_PROMPT = (
    "이 셋탑박스 화면이 어떤 상태인지 판정하라. "
    "홈/앱/런처메뉴/설정/재생/라이브TV/다이얼로그/로딩 중 하나로 kind 를 정하고, "
    "앱 화면이면 app_id(netflix/youtube/...)를 채워라. "
    "동일 화면은 동일 signature 로 수렴하도록 안정적인 서명을 생성하라."
)


class DetectionMcpScreenSense(ScreenSense):
    """detection-mcp/VLM HTTP 어댑터(스텁).

    실물 배선 전이라 응답 파싱은 [WIRE-RES] 에 TODO 로 표시. 배선 후에는 ScreenSense 로서
    엔진에 그대로 꽂힌다(코어 변경 0줄, M5).
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str = "qwen2.5vl:7b",
        prompt: Optional[str] = None,
        timeout: float = 30.0,  # VLM 판정 지연 고려(PRD R6).
        confidence_threshold: float = 0.5,
        token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ):
        if not base_url:
            raise ValueError("base_url(DETECTION_MCP_URL) 가 필요합니다.")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._prompt = prompt or CALIBRATION_PROMPT
        self.confidence_threshold = confidence_threshold
        headers = {"Content-Type": "application/json"}
        if token:  # [WIRE-AUTH]
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=timeout, headers=headers
        )
        self._owns_client = client is None

    @classmethod
    def from_env(cls, **overrides) -> "DetectionMcpScreenSense":
        """환경변수 구성. [WIRE-URL]

        - DETECTION_MCP_URL    : base URL(필수).
        - DETECTION_MCP_MODEL  : VLM 모델명(옵션, 기본 qwen2.5vl:7b).
        - DETECTION_MCP_TOKEN  : 인증 토큰(옵션).
        - DETECTION_MCP_TIMEOUT: 타임아웃 초(옵션, 기본 30).
        """
        base_url = overrides.pop("base_url", None) or os.environ.get(
            "DETECTION_MCP_URL", ""
        )
        if not base_url:
            raise SenseUnavailableError(
                "DETECTION_MCP_URL 미설정. VLM 배선 전이거나 환경변수 누락. "
                "개발/테스트는 MockScreenSense 를 사용하라."
            )
        model = overrides.pop("model", None) or os.environ.get(
            "DETECTION_MCP_MODEL", "qwen2.5vl:7b"
        )
        token = overrides.pop("token", None) or os.environ.get("DETECTION_MCP_TOKEN")
        timeout = overrides.pop("timeout", None)
        if timeout is None:
            timeout = float(os.environ.get("DETECTION_MCP_TIMEOUT", "30.0"))
        return cls(base_url, model=model, token=token, timeout=timeout, **overrides)

    @property
    def backend_name(self) -> str:
        return f"detection-mcp:{self._model}"

    def observe(self, raw: RawScreen) -> SenseResult:
        # [WIRE-REQ] 이미지 전달 방식/파라미터명을 실물 계약에 맞게 교체.
        image_b64: Optional[str] = None
        if raw.image_bytes is not None:
            image_b64 = base64.b64encode(raw.image_bytes).decode("ascii")

        payload = {
            "image_ref": raw.image_ref,
            "image_b64": image_b64,
            "text_hint": raw.text_hint,
            "prompt": self._prompt,
            "model": self._model,
        }

        # [WIRE-PATHS] 실제 classify 경로/메서드 확인.
        try:
            resp = self._client.post("/classify", json=payload)
        except httpx.TimeoutException as e:
            raise SenseUnavailableError(
                f"detection-mcp 타임아웃({self._base_url}): {e}"
            ) from e
        except httpx.HTTPError as e:
            raise SenseUnavailableError(
                f"detection-mcp 도달 불가({self._base_url}): {e}. 172.16.3.x 도달성 확인 필요."
            ) from e

        if resp.status_code >= 500:
            raise SenseUnavailableError(
                f"detection-mcp 서버 오류 {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise ScreenSenseError(
                f"detection-mcp classify 실패 {resp.status_code}: {resp.text[:200]}"
            )

        # [WIRE-RES] 응답 필드명/kind 라벨 매핑/confidence 스케일을 실물에 맞게 파싱.
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise ScreenSenseError(f"classify 응답 JSON 파싱 실패: {e}") from e

        raw_signature = data.get("signature")
        if not raw_signature:
            raise ScreenSenseError(
                "classify 응답에 signature 가 없습니다. [WIRE-RES] 필드명 확인."
            )

        kind = self._map_kind(data.get("kind"))
        confidence = self._coerce_confidence(data.get("confidence"))

        return self._build_result(
            raw_signature=raw_signature,
            label=data.get("label"),
            kind=kind,
            app_id=data.get("app_id"),
            confidence=confidence,
            screenshot_ref=raw.image_ref,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "DetectionMcpScreenSense":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- 내부 ------------------------------------------------------------- #

    @staticmethod
    def _map_kind(value: Optional[str]) -> StateKind:
        """백엔드 kind 라벨 -> StateKind. [WIRE-RES]

        실물 라벨 체계가 다르면 여기서 별칭 매핑을 확장하라(예: "설정"->settings).
        """
        if not value:
            return StateKind.UNKNOWN
        try:
            return StateKind(value)
        except ValueError:
            return StateKind.UNKNOWN

    @staticmethod
    def _coerce_confidence(value) -> float:
        """confidence 를 0.0~1.0 로 정규화(스케일/타입 방어). [WIRE-RES]"""
        try:
            c = float(value)
        except (TypeError, ValueError):
            return 0.0
        if c > 1.0:  # 0~100 스케일로 오면 축소.
            c = c / 100.0
        return max(0.0, min(1.0, c))

"""DetectionMcpScreenSense — detection-mcp(VLM) 로 화면을 판정하는 ScreenSense 어댑터.

실물 계약(2026-07 stb-ai-tc-automation 조사 확정)에 맞춰 배선됨.

핵심: detection-mcp 는 **상태 분류기가 아니라 QA 정합성 판정기**다. `POST /check/screen` 은
baseline 대비 normal/anomaly verdict 와 함께 화면을 서술하는 `description`(VLM/OCR 산출)을
돌려준다. 네비게이션 맵에 필요한 화면 상태 식별(signature/kind/app_id)은 서버가 주지 않으므로,
이 어댑터가 **description 을 로컬에서 합성**해 SenseResult 로 만든다(사용자 결정, 2026-07).

============================================================================
실물 계약 (as-wired)
============================================================================
전송: HTTP/JSON, base = DETECTION_MCP_URL (실측 http://172.16.3.136:8103).
  POST /check/screen
    req {"scenario":"<라우팅용 id>", "image_base64":"<png/jpg b64>",
         "prefer_vision": true, "expected": null, ...}
    res {"verdict":"normal|anomaly|no_baseline", "tier":"...",
         "confidence":0.0~1.0, "best_score":0.0~1.0, "description":"<화면 서술>", ...}

로컬 합성(description → 상태):
  - signature : description 에서 추출한 의미 토큰(kind + app_id + 키워드 집합)으로 구성해
                _build_result 가 정규화·해시한다. 자유문장 원문 해시보다 표현 흔들림에 강하다.
                (안정성의 최대 레버는 detection-mcp 서버측 vision 프롬프트다 — 사내 메모리 근거.)
  - kind      : 한/영 키워드 규칙 매칭(_KIND_KEYWORDS).
  - app_id    : 알려진 앱 키워드 매칭(_APP_KEYWORDS).
  - confidence: 응답 confidence(없으면 best_score).

미확정/후속:
  - [TUNE] _KIND_KEYWORDS/_APP_KEYWORDS 어휘와 서버측 vision 프롬프트는 실측 튜닝 대상.
  - scenario 는 라우팅용 자리표시(REMOTECTL_DETECTION_SCENARIO, 기본 "screen_state_probe").
    상태 분류 전용 baseline 이 없으면 prefer_vision 경로의 description 에 의존한다.
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

__all__ = ["DetectionMcpScreenSense"]


# --------------------------------------------------------------------------- #
# description(한/영 자유 서술) → StateKind 키워드 규칙. [TUNE]
# 우선순위: 앞에 올수록 우선(구체 상태 → 일반 상태). 앱은 별도(_APP_KEYWORDS)로도 판단.
# --------------------------------------------------------------------------- #
_KIND_KEYWORDS: list[tuple[StateKind, tuple[str, ...]]] = [
    (StateKind.LOADING, ("로딩", "loading", "버퍼", "buffering", "잠시만")),
    (StateKind.DIALOG, ("팝업", "다이얼로그", "dialog", "popup", "알림", "확인 창", "오류", "error")),
    (StateKind.PLAYBACK, ("재생", "playback", "playing", "영상 재생", "시청 중")),
    (StateKind.LIVE_TV, ("실시간", "라이브", "live tv", "live-tv", "채널", "편성표", "epg")),
    (StateKind.SETTINGS, ("설정", "settings", "환경설정", "preferences", "구성")),
    (StateKind.MENU, ("메뉴", "menu", "런처", "launcher", "목록", "리스트")),
    (StateKind.HOME, ("홈", "home", "메인 화면", "대시보드", "dashboard")),
]

# 알려진 앱 키워드 → app_id. [TUNE]
_APP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "netflix": ("넷플릭스", "netflix"),
    "youtube": ("유튜브", "youtube"),
    "tving": ("티빙", "tving"),
    "wavve": ("웨이브", "wavve"),
    "disneyplus": ("디즈니", "disney"),
    "coupangplay": ("쿠팡플레이", "coupang play", "coupangplay"),
}


class DetectionMcpScreenSense(ScreenSense):
    """detection-mcp(/check/screen) HTTP 어댑터 + description 로컬 합성.

    배선 후 MockScreenSense 와 동일하게 ScreenSense 로서 엔진에 그대로 꽂힌다(코어 변경 0줄, M5).
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str = "qwen2.5vl:7b",
        scenario: str = "screen_state_probe",
        prefer_vision: bool = True,
        timeout: float = 240.0,  # VLM 판정 지연 고려(실 클라이언트도 240s).
        confidence_threshold: float = 0.5,
        token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ):
        if not base_url:
            raise ValueError("base_url(DETECTION_MCP_URL) 가 필요합니다.")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._scenario = scenario
        self._prefer_vision = prefer_vision
        self.confidence_threshold = confidence_threshold
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=timeout, headers=headers
        )
        self._owns_client = client is None

    @classmethod
    def from_env(cls, **overrides) -> "DetectionMcpScreenSense":
        """환경변수 구성.

        - DETECTION_MCP_URL      : base URL(필수).
        - DETECTION_MCP_MODEL    : VLM 모델명(옵션, 기본 qwen2.5vl:7b; backend_name 표기용).
        - REMOTECTL_DETECTION_SCENARIO: /check/screen 라우팅 scenario(기본 screen_state_probe).
        - DETECTION_MCP_TOKEN    : 인증 토큰(옵션).
        - DETECTION_MCP_TIMEOUT  : 타임아웃 초(옵션, 기본 240).
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
        scenario = overrides.pop("scenario", None) or os.environ.get(
            "REMOTECTL_DETECTION_SCENARIO", "screen_state_probe"
        )
        token = overrides.pop("token", None) or os.environ.get("DETECTION_MCP_TOKEN")
        timeout = overrides.pop("timeout", None)
        if timeout is None:
            timeout = float(os.environ.get("DETECTION_MCP_TIMEOUT", "240.0"))
        return cls(
            base_url, model=model, scenario=scenario, token=token,
            timeout=timeout, **overrides,
        )

    @property
    def backend_name(self) -> str:
        return f"detection-mcp:{self._model}"

    def observe(self, raw: RawScreen) -> SenseResult:
        image_b64 = self._encode_image(raw)
        payload = {
            "scenario": self._scenario,
            "image_base64": image_b64,
            "prefer_vision": self._prefer_vision,
        }
        try:
            resp = self._client.post("/check/screen", json=payload)
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
                f"detection-mcp /check/screen 실패 {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise ScreenSenseError(f"/check/screen 응답 JSON 파싱 실패: {e}") from e

        description = (data.get("description") or "").strip()
        if not description:
            # description 이 없으면 상태 식별이 불가하다 — verdict 만으로는 화면을 구분할 수 없음.
            raise ScreenSenseError(
                "/check/screen 응답에 description 이 없습니다. prefer_vision 경로/서버 설정 확인 필요."
            )

        kind, app_id = self._classify(description)
        confidence = self._coerce_confidence(
            data.get("confidence") if data.get("confidence") is not None
            else data.get("best_score")
        )
        signature = self._signature_source(kind, app_id, description)

        return self._build_result(
            raw_signature=signature,
            label=description[:120],
            kind=kind,
            app_id=app_id,
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

    # --- 내부: description → 상태 합성 [TUNE] ------------------------------ #

    @staticmethod
    def _encode_image(raw: RawScreen) -> str:
        """RawScreen 의 이미지 바이트를 base64 로 인코딩. 바이트가 없으면 불가."""
        if raw.image_bytes is None:
            raise ScreenSenseError(
                "detection-mcp 는 image_base64 가 필요합니다(RawScreen.image_bytes 없음). "
                "드라이버 capture() 가 프레임 바이트를 채우는지 확인하라."
            )
        return base64.b64encode(raw.image_bytes).decode("ascii")

    @staticmethod
    def _classify(description: str) -> tuple[StateKind, Optional[str]]:
        """description 텍스트에서 (kind, app_id) 를 키워드 규칙으로 추출한다."""
        low = description.lower()

        app_id: Optional[str] = None
        for aid, kws in _APP_KEYWORDS.items():
            if any(kw in low for kw in kws):
                app_id = aid
                break

        kind = StateKind.UNKNOWN
        for candidate, kws in _KIND_KEYWORDS:
            if any(kw in low for kw in kws):
                kind = candidate
                break

        # 앱이 감지됐고 특별한 상태(재생/설정 등)가 아니면 APP 화면으로 본다.
        if app_id and kind in (StateKind.UNKNOWN, StateKind.HOME, StateKind.MENU):
            kind = StateKind.APP
        return kind, app_id

    @staticmethod
    def _signature_source(kind: StateKind, app_id: Optional[str], description: str) -> str:
        """상태 서명 원천 문자열.

        표현 흔들림에 강하도록 (kind + app_id + 매칭된 앱/상태 키워드 집합)으로 구성한다.
        어느 키워드에도 안 걸린 미지 화면은 원문 요약으로 폴백(최소한의 구분).
        """
        low = description.lower()
        tokens: list[str] = [f"kind={kind.value}"]
        if app_id:
            tokens.append(f"app={app_id}")
        matched: list[str] = []
        for _, kws in _KIND_KEYWORDS:
            for kw in kws:
                if kw in low:
                    matched.append(kw)
        for kws in _APP_KEYWORDS.values():
            for kw in kws:
                if kw in low:
                    matched.append(kw)
        if matched:
            tokens.append("kw=" + "+".join(sorted(set(matched))))
        else:
            # 키워드 미매칭 → 원문 앞부분으로 최소 구분(정규화는 _build_result 가 수행).
            tokens.append("desc=" + " ".join(description.split())[:80])
        return "|".join(tokens)

    @staticmethod
    def _coerce_confidence(value) -> float:
        """confidence 를 0.0~1.0 로 정규화(스케일/타입 방어)."""
        try:
            c = float(value)
        except (TypeError, ValueError):
            return 0.0
        if c > 1.0:  # 0~100 스케일로 오면 축소.
            c = c / 100.0
        return max(0.0, min(1.0, c))

"""config — 런타임 설정(Settings) 정본.

이 모듈의 위치와 역할
--------------------
- 물리적으로는 tier0(순수 데이터: Pydantic 모델만 의존, I/O 최소)이지만, 논리적으로는
  tier3(조립)에서만 소비된다. 즉 :mod:`remotectl.api.deps` 만 이 값을 읽어 구체 구현체를
  고른다(M5: 유일 wiring 지점의 입력). 엔진/맵/센스/드라이버 코어는 Settings 를 모른다.
- 환경변수(``REMOTECTL_*``) 로부터 설정을 읽어, "어떤 드라이버/센스 백엔드를 쓸지, 맵을
  어디에 저장할지, 안정화 대기/예산은 얼마인지" 같은 배선 파라미터를 한곳에 모은다.

설계 원칙
---------
- 순수/무해: 이 모듈을 임포트하는 것만으로는 어떤 네트워크/파일 I/O 도 일어나지 않는다.
  실제 드라이버/센스 생성(도달성 확인 등)은 deps 에서 지연 수행한다.
- 기본값은 "Mock 로 즉시 엔드투엔드가 도는" 개발 친화값이다(driver=mock, sense=mock).
- 알 수 없는 값에 관대하지 않다: driver_backend/sense_backend 는 허용된 값만 받는다
  (오타로 인한 조용한 오배선 방지). 검증 실패는 명확한 ValueError 로 조기에 드러난다.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["Settings", "load_settings"]


# 허용 백엔드 어휘(문자열 리터럴 오타 방지용 상수).
DriverBackend = Literal["mock", "mcp"]
SenseBackend = Literal["mock", "detection"]


class Settings(BaseModel):
    """환경변수(``REMOTECTL_*``) 기반 런타임 설정.

    deps(:mod:`remotectl.api.deps`) 가 이 값으로 구체 구현체를 선택·조립한다.

    필드
    ----
    - driver_backend: 리모컨 드라이버 선택. "mock"(가짜 STB) | "mcp"(사내망 remote-MCP).
    - sense_backend: 화면 판정 백엔드. "mock"(결정적) | "detection"(detection-mcp/VLM).
    - remote_mcp_url: remote-MCP base URL(mcp 백엔드일 때 사용). 비면 from_env 가 환경변수 참조.
    - detection_mcp_url: detection-mcp base URL(detection 백엔드일 때 사용).
    - map_store_path: 네비게이션 맵 JSON 영속화 경로(로드/저장 공통).
    - settle_ms: press 후 UI 안정화 대기(ms). 학습/실행 엔진에 주입된다(R3).
    - learn_step_budget: UC-1 학습 세션의 기본 최대 스텝 예산.
    - exec_replan_budget: UC-3 실행의 기본 재계획 예산(M3).
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    driver_backend: DriverBackend = "mock"
    sense_backend: SenseBackend = "mock"
    remote_mcp_url: str = ""
    detection_mcp_url: str = ""
    map_store_path: str = "./data/navmap.json"
    settle_ms: int = Field(default=400, ge=0)
    learn_step_budget: int = Field(default=200, ge=0)
    exec_replan_budget: int = Field(default=5, ge=0)

    @field_validator("driver_backend", mode="before")
    @classmethod
    def _norm_driver(cls, v: object) -> object:
        """소문자/공백 정규화(환경변수 표기 흔들림 흡수)."""
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("sense_backend", mode="before")
    @classmethod
    def _norm_sense(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v


def _env_int(name: str, default: int) -> int:
    """환경변수를 int 로 파싱(빈 값/미설정/파싱 실패 시 기본값).

    잘못된 숫자 표기가 앱 기동 자체를 막지 않도록, 파싱 실패는 조용히 기본값으로 폴백한다
    (Settings 필드 검증이 최종 방어선).
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def load_settings() -> Settings:
    """환경변수에서 Settings 를 로드한다(미설정 항목은 기본값).

    읽는 환경변수(모두 옵션)
    ------------------------
    - REMOTECTL_DRIVER_BACKEND : "mock" | "mcp"
    - REMOTECTL_SENSE_BACKEND  : "mock" | "detection"
    - REMOTE_MCP_URL           : remote-MCP base URL
    - DETECTION_MCP_URL        : detection-mcp base URL
    - REMOTECTL_MAP_STORE_PATH : 맵 JSON 경로
    - REMOTECTL_SETTLE_MS      : 안정화 대기(ms)
    - REMOTECTL_LEARN_STEP_BUDGET : 학습 스텝 예산
    - REMOTECTL_EXEC_REPLAN_BUDGET: 실행 재계획 예산

    Returns:
        환경변수를 반영한 Settings 인스턴스.

    Raises:
        ValueError: driver_backend/sense_backend 가 허용 값이 아닐 때(Pydantic ValidationError).
    """
    return Settings(
        driver_backend=os.environ.get("REMOTECTL_DRIVER_BACKEND", "mock"),
        sense_backend=os.environ.get("REMOTECTL_SENSE_BACKEND", "mock"),
        # remote-MCP/detection-MCP URL 은 각 어댑터의 from_env 규약(REMOTE_MCP_URL 등)과
        # 이름을 맞춰 두어, deps 가 명시 URL 을 주지 않아도 어댑터가 환경변수로 폴백할 수 있다.
        remote_mcp_url=os.environ.get("REMOTE_MCP_URL", ""),
        detection_mcp_url=os.environ.get("DETECTION_MCP_URL", ""),
        map_store_path=os.environ.get("REMOTECTL_MAP_STORE_PATH", "./data/navmap.json"),
        settle_ms=_env_int("REMOTECTL_SETTLE_MS", 400),
        learn_step_budget=_env_int("REMOTECTL_LEARN_STEP_BUDGET", 200),
        exec_replan_budget=_env_int("REMOTECTL_EXEC_REPLAN_BUDGET", 5),
    )

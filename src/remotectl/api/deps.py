"""api.deps — 설정(Settings) → 구체 구현체 조립. 유일한 wiring 지점(M5).

핵심 불변식(M5)
---------------
learner/executor 등 코어 엔진은 RemoteDriver/ScreenSense **추상**만 안다. 어떤 구체
드라이버/센스를 쓸지 고르는 결정은 오직 이 모듈에서만 일어난다. 즉:

- MockRemoteDriver / RemoteMcpClient / MockScreenSense / DetectionMcpScreenSense 같은
  **구체 구현체를 임포트하는 곳은 이 파일뿐**이다.
- 코어가 구현체를 임포트하면 아키텍처 임포트 검사가 이를 위반으로 잡는다.

역할
----
- make_driver(s): Settings.driver_backend 에 따라 RemoteDriver 구현체를 생성.
- make_sense(s):  Settings.sense_backend 에 따라 ScreenSense 구현체를 생성.
- load_graph(s):  Settings.map_store_path 에서 NavGraph 를 로드(없으면 빈 그래프).

설계 메모
---------
- 구체 구현체 임포트는 함수 안에서 지연(lazy) 수행한다. 이 모듈을 단순히 임포트하는 것만으로
  httpx 클라이언트 생성이나 도달성 확인 같은 부작용이 일어나지 않게 하기 위함이다.
- 실 백엔드(mcp/detection)는 from_env 규약을 따르되, Settings 에 명시 URL 이 있으면
  그것을 우선 넘긴다(환경변수와 Settings 간 일관성).
"""

from __future__ import annotations

from pathlib import Path

from remotectl.config import Settings
from remotectl.drivers.base import RemoteDriver
from remotectl.engine.hooks import ErrorAnalyzer, StepObserver
from remotectl.navmap import NavGraph
from remotectl.sense.base import ScreenSense

__all__ = ["make_driver", "make_sense", "load_graph", "make_store", "make_analyzer"]


def make_driver(s: Settings) -> RemoteDriver:
    """Settings.driver_backend 에 따라 리모컨 드라이버 구현체를 생성한다(M5 wiring).

    - "mock": :class:`MockRemoteDriver` (기본 시나리오, 실 STB 불필요).
    - "mcp":  :class:`RemoteMcpClient` (사내망 remote-MCP HTTP 어댑터). Settings.remote_mcp_url
      이 있으면 base_url 로 넘기고, 없으면 어댑터의 from_env(REMOTE_MCP_URL) 규약에 맡긴다.

    Args:
        s: 런타임 설정.

    Returns:
        RemoteDriver 추상을 만족하는 구현체 인스턴스.

    Raises:
        ValueError: driver_backend 값이 알 수 없을 때.
        DriverUnavailableError: mcp 백엔드인데 URL 이 전혀 없을 때(from_env 규약).
    """
    backend = s.driver_backend
    if backend == "mock":
        # 지연 임포트: 구체 구현체 결합을 이 함수 스코프로 국한(M5).
        from remotectl.drivers.mock import MockRemoteDriver

        return MockRemoteDriver()

    if backend == "mcp":
        from remotectl.drivers.mcp_client import RemoteMcpClient

        # Settings 에 명시 URL 이 있으면 우선 사용(없으면 from_env 가 환경변수로 폴백).
        overrides: dict[str, object] = {}
        if s.remote_mcp_url:
            overrides["base_url"] = s.remote_mcp_url
        return RemoteMcpClient.from_env(**overrides)

    raise ValueError(
        f"알 수 없는 driver_backend: {backend!r} (허용: 'mock' | 'mcp')"
    )


def make_sense(s: Settings) -> ScreenSense:
    """Settings.sense_backend 에 따라 화면 판정 센스 구현체를 생성한다(M5 wiring).

    - "mock":      :class:`MockScreenSense` (결정적 판정, 실 VLM 불필요).
    - "detection": :class:`DetectionMcpScreenSense` (detection-mcp/VLM HTTP 어댑터).
      Settings.detection_mcp_url 이 있으면 base_url 로 넘기고, 없으면 from_env 규약에 맡긴다.

    Args:
        s: 런타임 설정.

    Returns:
        ScreenSense 추상을 만족하는 구현체 인스턴스.

    Raises:
        ValueError: sense_backend 값이 알 수 없을 때.
        SenseUnavailableError: detection 백엔드인데 URL 이 전혀 없을 때(from_env 규약).
    """
    backend = s.sense_backend
    if backend == "mock":
        from remotectl.sense.mock import MockScreenSense

        return MockScreenSense()

    if backend == "detection":
        from remotectl.sense.detection_mcp import DetectionMcpScreenSense

        overrides: dict[str, object] = {}
        if s.detection_mcp_url:
            overrides["base_url"] = s.detection_mcp_url
        return DetectionMcpScreenSense.from_env(**overrides)

    raise ValueError(
        f"알 수 없는 sense_backend: {backend!r} (허용: 'mock' | 'detection')"
    )


def load_graph(s: Settings) -> NavGraph:
    """Settings.map_store_path 에서 NavGraph 를 로드한다.

    파일이 없으면(아직 학습 전) 빈 NavGraph 를 새로 만들어 돌려준다 — 첫 학습이 여기에
    상태/전이를 채우고, 이후 :meth:`NavGraph.save` 로 같은 경로에 영속화한다.

    Args:
        s: 런타임 설정(map_store_path 사용).

    Returns:
        로드된(또는 새로 만든 빈) NavGraph.

    Raises:
        ValueError: 파일이 존재하지만 JSON 파싱/검증에 실패한 경우(손상된 맵).
    """
    path = Path(s.map_store_path)
    if not path.exists():
        # 미학습 상태: 빈 그래프로 시작(학습 후 save 로 이 경로에 생성됨).
        return NavGraph()
    return NavGraph.load(path)


def make_store(s: Settings) -> StepObserver:
    """키이벤트↔화면 매핑 별도 DB(SQLite) 관찰자를 생성한다(동시 기록용).

    Settings.keyscreen_db_path 에 SQLite 파일을 두고, 학습 루프에 관찰자로 주입되어
    매 스텝의 (from,key)→to 매핑 + LLM 화면 분석을 누적한다.
    """
    from remotectl.store import KeyScreenStore

    return KeyScreenStore.from_env(path=s.keyscreen_db_path)


def make_analyzer(s: Settings) -> ErrorAnalyzer:
    """오류 자가치유 분석기를 생성한다.

    - sense=detection: :class:`VlmErrorAnalyzer` — LLM(VLM) 이상/저신뢰 판정을 오류로 인식해
      재판정(REOBSERVE)으로 정상 복구를 시도한다("LLM 이 판단해 오류 인식 → 수정 후 재시도").
    - sense=mock: :class:`PolicyErrorAnalyzer` — VLM 판정이 없으므로 규칙 기반.
    """
    if s.sense_backend == "detection":
        from remotectl.reconcile import VlmErrorAnalyzer

        return VlmErrorAnalyzer.from_env()

    from remotectl.reconcile import PolicyErrorAnalyzer

    return PolicyErrorAnalyzer.from_env()

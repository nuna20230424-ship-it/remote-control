"""remotectl MCP 서버 — 다른 AI 에이전트가 학습/맵/목표실행을 '도구'로 호출하게 노출한다.

엔진(Learner/Planner/Executor)을 인프로세스로 재사용하며, 어떤 STB/백엔드를 쓸지는 전부
환경변수(``REMOTECTL_*``)로 결정된다(개발 기본은 mock). REST 서버(:mod:`remotectl.api.app`)와
**동일한 컨텍스트 팩토리(deps)와 맵 저장 경로**를 공유하므로, 같은
``REMOTECTL_MAP_STORE_PATH`` 를 가리키면 REST/CLI/MCP 어느 경로로 학습해도 맵을 공유한다.

노출 도구
--------
- ``remote_learn``       : UC-1 탐색 학습(맵 구축/저장).
- ``remote_inspect_map`` : UC-2 맵 조회(states/transitions/coverage).
- ``remote_find_path``   : 두 상태 간 최단 버튼 경로.
- ``remote_run_goal``    : UC-3 자연어 목표 실행(결과 status 로 성공/실패 판단).

실행
----
    python -m remotectl.mcp_server        # stdio 트랜스포트(오케스트레이터가 자식 프로세스로 기동)

오케스트레이터 등록 예(기존 사내 MCP 8종과 동일 방식)::

    {"mcpServers": {"remotectl": {
        "command": "python", "args": ["-m", "remotectl.mcp_server"],
        "env": {"REMOTECTL_DRIVER_BACKEND": "mcp", "REMOTE_MCP_URL": "http://172.16.x.x:PORT",
                "REMOTECTL_SENSE_BACKEND": "detection",
                "REMOTECTL_MAP_STORE_PATH": "/shared/navmap.json"}}}}
"""

from __future__ import annotations

from typing import Any, Optional

from remotectl.api import deps
from remotectl.api.app import _map_view, _path_view  # 표준 직렬화 재사용(중복 방지)
from remotectl.config import load_settings
from remotectl.engine.executor import Executor
from remotectl.engine.learner import Learner
from remotectl.engine.planner import Planner

__all__ = [
    "remote_learn",
    "remote_inspect_map",
    "remote_find_path",
    "remote_run_goal",
    "TOOLS",
    "build_server",
    "main",
]


def _ctx():
    """``load_settings()`` → deps 로 ``(settings, driver, sense, graph)`` 를 조립한다.

    REST 앱과 동일한 유일 wiring 지점(deps)만 경유하므로 백엔드 선택/맵 로드 규약이 일치한다.
    호출마다 맵을 파일에서 로드하므로, 외부에서 맵을 갱신해도 다음 호출에 반영된다.
    """
    s = load_settings()
    return s, deps.make_driver(s), deps.make_sense(s), deps.load_graph(s)


def remote_learn(
    step_budget: Optional[int] = None, coverage_target: float = 0.9
) -> dict[str, Any]:
    """STB를 탐색하며 버튼→화면 전이를 학습해 네비게이션 맵을 구축/저장한다.

    목표 실행(:func:`remote_run_goal`) 전에 맵이 비어 있으면 먼저 호출하라. 학습 요약
    (LearningSummary: 방문 상태 수·발견 전이 수·커버리지 등)을 dict 로 돌려주고, 맵은
    ``REMOTECTL_MAP_STORE_PATH`` 에 저장된다.

    Args:
        step_budget: 최대 스텝 예산. None 이면 설정값(``REMOTECTL_LEARN_STEP_BUDGET``).
        coverage_target: 커버리지 목표(0~1). 도달 시 조기 종료.
    """
    s, driver, sense, graph = _ctx()
    summary = Learner(driver, sense, graph, settle_ms=s.settle_ms).learn(
        step_budget=step_budget if step_budget is not None else s.learn_step_budget,
        coverage_target=coverage_target,
    )
    graph.save(s.map_store_path)
    return summary.model_dump(mode="json")


def remote_inspect_map() -> dict[str, Any]:
    """학습된 네비게이션 맵을 조회한다(states/transitions/coverage/root). 학습 전이면 빈 맵."""
    _, _, _, graph = _ctx()
    return _map_view(graph)


def remote_find_path(from_state: str, to_state: str) -> dict[str, Any]:
    """두 상태 id 사이 최단 버튼 경로(PlanStep 열)를 반환한다.

    도달불가/미학습은 예외 없이 ``reachable=False`` 로 정규화된다.
    """
    _, _, _, graph = _ctx()
    return _path_view(graph, from_state, to_state)


def remote_run_goal(text: str) -> dict[str, Any]:
    """자연어 목표(예: '넷플릭스 켜줘')를 맵 기반으로 계획·실행한다.

    Executor 는 예외를 던지지 않고 모든 실패를 ``status`` 로 정규화하므로 항상 결과 dict 를
    돌려준다(성공 여부는 ``status`` 필드; 미학습이면 ``failed_unresolved``/``failed_unreachable``
    → 먼저 :func:`remote_learn` 을 호출하라).
    """
    s, driver, sense, graph = _ctx()
    result = Executor(
        driver,
        sense,
        graph,
        Planner(graph),
        settle_ms=s.settle_ms,
        replan_budget=s.exec_replan_budget,
    ).run_goal(text)
    graph.save(s.map_store_path)
    return result.model_dump(mode="json")


# 공개 도구 목록(테스트/문서/등록에서 단일 출처로 참조).
TOOLS = [remote_learn, remote_inspect_map, remote_find_path, remote_run_goal]


def build_server():
    """FastMCP 서버를 만들어 :data:`TOOLS` 를 등록해 돌려준다.

    ``mcp`` 는 선택 의존성이다(``pip install 'remotectl[mcp]'``). 미설치 시 명확한 오류.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:  # 선택 의존성 미설치
        raise RuntimeError(
            "mcp 패키지가 필요합니다. 설치: pip install 'remotectl[mcp]'"
        ) from exc

    server = FastMCP("remotectl")
    for fn in TOOLS:
        server.tool()(fn)
    return server


def main() -> None:
    """stdio 트랜스포트로 MCP 서버를 기동한다(``python -m remotectl.mcp_server``)."""
    build_server().run()


if __name__ == "__main__":
    main()

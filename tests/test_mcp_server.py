"""remotectl.mcp_server 스모크 — 도구가 엔진을 인프로세스로 올바로 감싸는지 검증한다.

- 도구 함수는 mock 백엔드(기본값)로 실 STB 없이 완결된다.
- ``mcp`` 미설치 환경에서는 도구 함수 자체는 여전히 동작하므로(패키지 불필요), 서버 조립
  테스트만 importorskip 로 건너뛴다.
"""

from __future__ import annotations

import pytest

from remotectl import mcp_server


@pytest.fixture(autouse=True)
def _mock_env(tmp_path, monkeypatch):
    """맵 저장을 격리된 tmp 경로로 돌리고 백엔드를 mock 으로 고정한다."""
    monkeypatch.setenv("REMOTECTL_DRIVER_BACKEND", "mock")
    monkeypatch.setenv("REMOTECTL_SENSE_BACKEND", "mock")
    monkeypatch.setenv("REMOTECTL_MAP_STORE_PATH", str(tmp_path / "navmap.json"))


def test_learn_inspect_goal_roundtrip():
    """learn → 맵 조회 → 목표 실행이 mock 만으로 엔드투엔드 완결된다(같은 맵 경로 공유)."""
    # 완전 학습(0.99)으로 mock 시나리오의 앱 상태(netflix 등)까지 발견 — 목표 해석 전제.
    summary = mcp_server.remote_learn(step_budget=200, coverage_target=0.99)
    assert summary["states_visited"] >= 1
    assert summary["transitions_recorded"] >= 1

    view = mcp_server.remote_inspect_map()
    assert view["state_count"] >= 1
    assert view["transition_count"] >= 1

    result = mcp_server.remote_run_goal("넷플릭스 켜줘")
    # 성공 경로: mock 기본 시나리오에서 netflix 도달.
    assert result["status"] == "success"
    assert result["final_state_id"]


def test_run_goal_on_empty_map_is_normalized():
    """학습 전(빈 맵) 목표 실행은 예외 없이 실패 status 로 정규화된다."""
    result = mcp_server.remote_run_goal("넷플릭스 켜줘")
    assert result["status"].startswith("failed_")


def test_find_path_unreachable_normalized():
    """존재하지 않는 상태 간 경로 질의는 reachable=False 로 정규화된다(예외 없음)."""
    view = mcp_server.remote_find_path("st_does_not_exist", "st_also_missing")
    assert view["reachable"] is False


def test_build_server_registers_all_tools():
    """FastMCP 서버 조립 시 도구가 모두 등록된다(mcp 설치 시에만)."""
    pytest.importorskip("mcp")
    server = mcp_server.build_server()
    assert server is not None
    names = {fn.__name__ for fn in mcp_server.TOOLS}
    assert {
        "remote_learn", "remote_inspect_map", "remote_find_path",
        "remote_run_goal", "remote_autolearn", "remote_keyscreen",
    } <= names

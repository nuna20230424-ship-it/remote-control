"""FastAPI REST + 대시보드 스모크 테스트 (Mock 백엔드).

검증 범위(QA 트랙):
- create_app(Settings) 가 mock 드라이버/센스로 앱을 조립한다(M5 wiring 은 deps 경유).
- 주요 엔드포인트(/health, /learn, /map, /map/path, /goal, /) 가 정상 응답한다.
- 전체 UC(학습→맵→목표실행)가 REST 위에서 엔드투엔드로 돈다.

TestClient(=Starlette 인프로세스 클라이언트)로 실제 네트워크 없이 앱을 구동한다.
"""

from __future__ import annotations

import pytest

from remotectl.api.app import create_app
from remotectl.config import Settings

# TestClient 임포트(설치 환경에 따라 fastapi 또는 starlette 경유).
try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - fallback
    from starlette.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    """mock 백엔드 + 임시 맵 저장 경로로 조립한 앱의 TestClient."""
    settings = Settings(
        driver_backend="mock",
        sense_backend="mock",
        map_store_path=str(tmp_path / "navmap.json"),
        settle_ms=0,
        learn_step_budget=200,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health_reports_mock_backends(client):
    """/health 가 드라이버/센스 가용성을 보고한다."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["driver"]["ready"] is True
    assert body["sense"]["backend_name"] == "mock"


def test_dashboard_serves_html(client):
    """GET / 이 자족 대시보드 HTML 을 서빙한다(R7)."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<" in resp.text  # HTML 마크업


def test_learn_then_map_then_goal_e2e(client):
    """REST 위에서 학습→맵조회→목표실행 전 파이프라인이 완결된다."""
    # UC-1 학습
    learn = client.post("/learn", json={"step_budget": 200, "coverage_target": 0.99})
    assert learn.status_code == 200
    summary = learn.json()
    assert summary["states_visited"] == 7
    assert summary["transitions_recorded"] > 0

    # UC-2 맵 조회
    mp = client.get("/map").json()
    assert mp["state_count"] == 7
    assert mp["transition_count"] > 0
    assert mp["coverage"] is not None
    # 전이 뷰가 token 편의 필드를 노출한다.
    assert all("token" in t for t in mp["transitions"])

    # UC-3 목표 실행
    goal = client.post("/goal", json={"text": "넷플릭스 켜줘"})
    assert goal.status_code == 200
    result = goal.json()
    assert result["status"] == "success", result.get("message")
    assert result["button_sequence"], "성공 실행은 키 시퀀스를 담아야 한다."


def test_map_path_endpoint(client):
    """학습 후 /map/path 가 두 상태 간 경로(또는 도달불가)를 정규화해 반환한다."""
    client.post("/learn", json={"step_budget": 200, "coverage_target": 0.99})
    mp = client.get("/map").json()
    root = mp["root_state_id"]
    netflix = next(s["id"] for s in mp["states"] if s.get("app_id") == "netflix")

    resp = client.get("/map/path", params={"from": root, "to": netflix})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert body["hops"] >= 1
    assert [s["key"]["button"] for s in body["steps"]] == ["RIGHT", "OK"]


def test_map_path_unreachable_is_normalized(client):
    """존재하지 않는 목표 상태로의 경로 요청은 reachable=False 로 정규화된다."""
    client.post("/learn", json={"step_budget": 50, "coverage_target": 0.99})
    mp = client.get("/map").json()
    root = mp["root_state_id"]
    resp = client.get("/map/path", params={"from": root, "to": "st_notarealstate0"})
    assert resp.status_code == 200
    assert resp.json()["reachable"] is False


def test_goal_unresolved_returns_200_with_status(client):
    """미해석 목표도 200 으로 status=failed_unresolved 를 돌려준다(예외 미전파)."""
    client.post("/learn", json={"step_budget": 200, "coverage_target": 0.99})
    resp = client.post("/goal", json={"text": "도무지 알 수 없는 명령 zzz"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed_unresolved"

"""핵심 파이프라인 엔드투엔드 검증 (Mock 드라이버/센스 기반).

검증 범위(QA 트랙):
- UC-1 학습: MockRemoteDriver + MockScreenSense 로 탐색해 네비게이션 맵을 구축한다.
- UC-2 맵/식별: 같은 화면→같은 state id 로 수렴하고, JSON 왕복 저장/로드가 무결하다.
- UC-3 계획/실행: 학습한 맵 위에서 목표(넷플릭스/유튜브/설정)까지 경로를 계획·실행한다.
- 견고성(R3/M3): 비결정 전이(flaky)를 재계획으로 흡수해 목표에 도달한다.
- 실패 정규화(R4/R5/M6): 미해석·미학습·도달불가가 예외가 아니라 상태값으로 정규화된다.

이 테스트들은 구체 구현체(Mock*)를 직접 조립하되, 엔진(Learner/Planner/Executor)에는
오직 추상(RemoteDriver/ScreenSense)만 주입한다. 실 STB/VLM 없이 전 구간이 완결됨을 근거로 한다.
"""

from __future__ import annotations

import pytest

from remotectl.drivers.mock import MockRemoteDriver, MockScenario, default_stb_scenario
from remotectl.engine.executor import Executor
from remotectl.engine.learner import Learner
from remotectl.engine.planner import Planner, PlanningError
from remotectl.goals import find_goal_state_id, resolve_goal
from remotectl.identify import match_state, resolve_state
from remotectl.models import (
    Button,
    ExecutionStatus,
    KeyPress,
    NavMap,
    ScreenState,
    StateKind,
)
from remotectl.navmap import NavGraph
from remotectl.sense.mock import MockScreenSense


# --------------------------------------------------------------------------- #
# 픽스처
# --------------------------------------------------------------------------- #


def _fresh_stack():
    """새 Mock 드라이버/센스/빈 그래프 스택."""
    return MockRemoteDriver(), MockScreenSense(), NavGraph()


@pytest.fixture()
def learned_graph() -> NavGraph:
    """기본 시나리오를 충분히 학습한 그래프(여러 테스트가 공유)."""
    driver, sense, graph = _fresh_stack()
    Learner(driver, sense, graph, settle_ms=0).learn(
        step_budget=200, coverage_target=0.99
    )
    return graph


# --------------------------------------------------------------------------- #
# UC-1 학습
# --------------------------------------------------------------------------- #


def test_learning_discovers_all_screens(learned_graph: NavGraph):
    """학습이 기본 시나리오의 7개 화면을 모두 발견하고 전이를 기록한다."""
    navmap = learned_graph.navmap
    assert navmap.state_count() == 7, "기본 시나리오의 화면 7개가 모두 학습돼야 한다."
    assert navmap.transition_count() > 0, "적어도 하나의 전이가 기록돼야 한다."

    app_ids = {s.app_id for s in navmap.states.values() if s.app_id}
    assert app_ids == {"netflix", "youtube", "settings"}, (
        f"3개 앱 상태가 모두 학습돼야 한다. got={app_ids}"
    )

    kinds = {s.kind for s in navmap.states.values()}
    assert StateKind.HOME in kinds
    assert StateKind.APP in kinds


def test_learning_summary_fields_are_consistent():
    """LearningSummary 집계 필드가 실제 맵과 일치한다."""
    driver, sense, graph = _fresh_stack()
    summary = Learner(driver, sense, graph, settle_ms=0).learn(
        step_budget=200, coverage_target=0.99, session_id="qa-1"
    )
    assert summary.session_id == "qa-1"
    assert summary.states_visited == graph.navmap.state_count()
    assert summary.transitions_recorded == graph.navmap.transition_count()
    assert summary.steps_taken > 0
    assert summary.coverage_ratio is not None and summary.coverage_ratio > 0.0
    assert summary.stop_reason  # 종료 사유가 반드시 기록된다.


def test_learning_stops_on_coverage_target():
    """낮은 커버리지 목표를 주면 예산 소진 전에 조기 종료한다."""
    driver, sense, graph = _fresh_stack()
    summary = Learner(driver, sense, graph, settle_ms=0).learn(
        step_budget=500, coverage_target=0.05
    )
    assert summary.steps_taken < 500, "낮은 목표면 예산을 다 쓰기 전에 멈춰야 한다."
    assert "커버리지" in (summary.stop_reason or "")


# --------------------------------------------------------------------------- #
# UC-2 맵 / 식별 (R2)
# --------------------------------------------------------------------------- #


def test_state_identity_is_deterministic():
    """같은 화면(같은 signature)은 같은 state id 로 수렴한다(R2)."""
    driver, sense, _ = _fresh_stack()
    driver.reset()
    r1 = sense.observe(driver.capture())
    # 나갔다가 돌아오기: RIGHT -> LEFT (home -> launcher_netflix -> home)
    driver.press(KeyPress(button=Button.RIGHT))
    driver.press(KeyPress(button=Button.LEFT))
    r2 = sense.observe(driver.capture())
    assert driver.current_screen_key == "home"
    assert r1.state.id == r2.state.id, "같은 홈 화면은 같은 id 여야 한다."


def test_resolve_and_match_state_roundtrip():
    """resolve_state 는 upsert 후 확정 상태를, match_state 는 그 id 를 재조회한다."""
    driver, sense, graph = _fresh_stack()
    driver.reset()
    result = sense.observe(driver.capture())
    resolved = resolve_state(result, graph)
    assert graph.get_state(resolved.id) is not None
    assert match_state(result, graph.navmap) == resolved.id


def test_navmap_json_roundtrip_is_lossless(learned_graph: NavGraph, tmp_path):
    """맵 저장→로드 왕복 후 상태/전이 수가 보존된다(영속화 무결성)."""
    path = tmp_path / "navmap.json"
    learned_graph.save(path)
    reloaded = NavGraph.load(path)
    assert reloaded.navmap.state_count() == learned_graph.navmap.state_count()
    assert reloaded.navmap.transition_count() == learned_graph.navmap.transition_count()
    # 순수 모델 왕복도 확인.
    dumped = learned_graph.navmap.dump_json()
    parsed = NavMap.load_json(dumped)
    assert parsed.state_count() == learned_graph.navmap.state_count()


def test_navmap_extra_fields_ignored_on_load():
    """알 수 없는 필드가 있는 JSON 도 로드된다(스키마 진화 내성, extra=ignore)."""
    nav = NavMap()
    st = ScreenState(signature="screen:home", kind=StateKind.HOME)
    nav.states[st.id] = st
    data = nav.model_dump(mode="json")
    data["unknown_future_field"] = {"x": 1}
    data["states"][st.id]["another_unknown"] = 42
    reloaded = NavMap.model_validate(data)
    assert reloaded.state_count() == 1


# --------------------------------------------------------------------------- #
# 계획(Planner) — 그래프 최단경로
# --------------------------------------------------------------------------- #


def test_planner_shortest_path_home_to_netflix(learned_graph: NavGraph):
    """홈에서 넷플릭스 앱까지 최소 홉 계획(RIGHT -> OK)이 나온다."""
    home_id = learned_graph.navmap.root_state_id
    goal = resolve_goal("넷플릭스", learned_graph.navmap)
    netflix_id = find_goal_state_id(goal, learned_graph.navmap)
    assert netflix_id is not None

    steps = Planner(learned_graph).plan(home_id, netflix_id)
    assert [s.key.token for s in steps] == ["RIGHT", "OK"], (
        "홈→넷플릭스 최단경로는 RIGHT, OK 여야 한다."
    )
    # 각 스텝의 from/expected 가 이어져 경로를 이룬다.
    assert steps[0].from_state_id == home_id
    assert steps[-1].expected_to_state_id == netflix_id


def test_planner_same_state_returns_empty(learned_graph: NavGraph):
    """시작==목표면 빈 계획([])을 반환한다(예외 아님)."""
    home_id = learned_graph.navmap.root_state_id
    assert Planner(learned_graph).plan(home_id, home_id) == []


def test_planner_unlearned_state_raises(learned_graph: NavGraph):
    """맵에 없는 상태를 목표로 주면 PlanningError(미학습)."""
    home_id = learned_graph.navmap.root_state_id
    with pytest.raises(PlanningError):
        Planner(learned_graph).plan(home_id, "st_deadbeefdeadbeef")


def test_planner_unreachable_raises():
    """두 상태 모두 있으나 경로가 없으면 PlanningError(도달불가, R5)."""
    graph = NavGraph()
    a = graph.upsert_state(ScreenState(signature="screen:home", kind=StateKind.HOME))
    b = graph.upsert_state(
        ScreenState(signature="screen:app:netflix", app_id="netflix", kind=StateKind.APP)
    )
    with pytest.raises(PlanningError):
        Planner(graph).plan(a.id, b.id)


# --------------------------------------------------------------------------- #
# UC-3 목표 해석 + 실행
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected_app",
    [
        ("넷플릭스 켜줘", "netflix"),
        ("유튜브 틀어", "youtube"),
        ("넷플 보여줘", "netflix"),
    ],
)
def test_goal_resolution_maps_app(learned_graph: NavGraph, text: str, expected_app: str):
    """자연어 명령이 규칙으로 올바른 앱 상태에 매핑된다(R4)."""
    goal = resolve_goal(text, learned_graph.navmap)
    assert goal.resolved, f"'{text}' 는 학습된 맵에서 해석돼야 한다."
    assert goal.target_app_id == expected_app
    sid = find_goal_state_id(goal, learned_graph.navmap)
    assert sid is not None and learned_graph.navmap.get_state(sid).app_id == expected_app


@pytest.mark.parametrize(
    "text,expected_app",
    [
        ("넷플릭스 켜줘", "netflix"),
        ("유튜브 틀어줘", "youtube"),
        ("설정 열어", "settings"),
    ],
)
def test_executor_reaches_goal(learned_graph: NavGraph, text: str, expected_app: str):
    """학습한 맵 위에서 목표를 실행하면 해당 앱 상태에 도달한다(UC-3)."""
    driver, sense, _ = _fresh_stack()
    executor = Executor(driver, sense, learned_graph, Planner(learned_graph), settle_ms=0)
    result = executor.run_goal(text)
    assert result.status is ExecutionStatus.SUCCESS, (
        f"'{text}' 실행 결과: {result.status} / {result.message}"
    )
    assert result.succeeded
    final = learned_graph.navmap.get_state(result.final_state_id)
    assert final is not None and final.app_id == expected_app
    assert result.button_sequence, "성공 실행은 최소 한 번 이상 키를 눌러야 한다."


def test_executor_already_at_goal_is_success(learned_graph: NavGraph):
    """이미 홈에 있고 목표가 홈이면 입력 없이 SUCCESS."""
    driver, sense, _ = _fresh_stack()
    driver.reset()  # home
    executor = Executor(driver, sense, learned_graph, Planner(learned_graph), settle_ms=0)
    result = executor.run_goal("홈으로 가")
    assert result.status is ExecutionStatus.SUCCESS
    assert result.button_sequence == [], "이미 목표면 키 입력이 없어야 한다."


def test_executor_replans_around_flaky_transition(learned_graph: NavGraph):
    """비결정 전이(flaky)를 재계획으로 흡수해 목표에 도달한다(R3/M3)."""
    scenario = default_stb_scenario()
    flaky = MockScenario(
        screens=scenario.screens,
        transitions=scenario.transitions,
        start_screen="home",
        home_screen="home",
        # home 에서 RIGHT 첫 시도는 엉뚱하게 launcher_settings 로 튄다.
        flaky_transitions={("home", "RIGHT"): ["launcher_settings"]},
    )
    driver = MockRemoteDriver(flaky)
    sense = MockScreenSense()
    executor = Executor(
        driver, sense, learned_graph, Planner(learned_graph), settle_ms=0, replan_budget=5
    )
    result = executor.run_goal("넷플릭스 켜줘")
    assert result.status is ExecutionStatus.SUCCESS, (
        f"재계획으로 목표 도달해야 한다: {result.message}"
    )
    assert result.replans >= 1, "flaky 전이가 최소 1회 재계획을 유발해야 한다."
    final = learned_graph.navmap.get_state(result.final_state_id)
    assert final.app_id == "netflix"


def test_executor_unresolved_goal_is_normalized(learned_graph: NavGraph):
    """알 수 없는 명령은 예외가 아니라 FAILED_UNRESOLVED 로 정규화된다(R4)."""
    driver, sense, _ = _fresh_stack()
    executor = Executor(driver, sense, learned_graph, Planner(learned_graph), settle_ms=0)
    result = executor.run_goal("이건 도무지 알 수 없는 명령 xyz")
    assert result.status is ExecutionStatus.FAILED_UNRESOLVED
    assert result.message  # 사유가 채워진다.


def test_executor_unlearned_app_is_normalized():
    """학습 안 된 앱 목표는 FAILED_UNRESOLVED(미학습)로 정규화된다(R4)."""
    driver, sense, empty_graph = _fresh_stack()  # 빈 그래프(미학습)
    executor = Executor(driver, sense, empty_graph, Planner(empty_graph), settle_ms=0)
    result = executor.run_goal("넷플릭스 켜줘")
    assert result.status is ExecutionStatus.FAILED_UNRESOLVED


def test_executor_never_raises(learned_graph: NavGraph):
    """어떤 입력에도 run_goal 은 예외를 던지지 않고 결과로 정규화한다."""
    driver, sense, _ = _fresh_stack()
    executor = Executor(driver, sense, learned_graph, Planner(learned_graph), settle_ms=0)
    for text in ["", "   ", "!!!", "넷플릭스", "goto st_notarealid00000000"]:
        result = executor.run_goal(text)
        assert result.status in set(ExecutionStatus)  # 항상 유효한 status


# --------------------------------------------------------------------------- #
# 아키텍처 불변식(M5) — 엔진이 구체 구현체를 임포트하지 않는다
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "module_name",
    ["remotectl.engine.learner", "remotectl.engine.executor", "remotectl.engine.planner"],
)
def test_engine_does_not_import_concrete_impls(module_name: str):
    """learner/executor/planner 가 구체 드라이버/센스 모듈을 실제로 임포트하지 않는다(M5).

    주석/docstring 에 심볼 이름이 언급되는 것은 허용한다(그것들은 '임포트 금지'를 설명하는
    문장이다). 여기서는 AST 로 실제 import 문만 검사해, 구현체 모듈 결합만 위반으로 잡는다.
    """
    import ast
    import importlib

    module = importlib.import_module(module_name)
    with open(module.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    forbidden_modules = {
        "remotectl.drivers.mock",
        "remotectl.drivers.mcp_client",
        "remotectl.sense.mock",
        "remotectl.sense.detection_mcp",
    }
    forbidden_names = {
        "MockRemoteDriver",
        "RemoteMcpClient",
        "MockScreenSense",
        "DetectionMcpScreenSense",
    }

    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)
            for alias in node.names:
                imported_names.add(alias.name)

    bad_mod = imported_modules & forbidden_modules
    bad_name = imported_names & forbidden_names
    assert not bad_mod, f"{module_name} 가 구현체 모듈을 임포트함(M5 위반): {bad_mod}"
    assert not bad_name, f"{module_name} 가 구현체 심볼을 임포트함(M5 위반): {bad_name}"

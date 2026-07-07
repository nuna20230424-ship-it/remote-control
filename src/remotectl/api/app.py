"""api.app — 최상위 조립·서빙 계층(tier3).

역할
----
이 모듈은 remotectl 의 "지붕"이다. 아래 두 진입점을 제공한다.

1. FastAPI 앱(``create_app`` / 모듈 전역 ``app``): REST API + 자족 대시보드 서빙.
   uvicorn 진입점은 ``remotectl.api.app:app`` 이다.
2. CLI(``main``): 서브커맨드 learn / inspect / goal / serve.
   pyproject [project.scripts] 의 콘솔 스크립트 진입점이다.

조립 원칙(M5)
-------------
구체 구현체 선택(드라이버/센스)은 오직 :mod:`remotectl.api.deps` 에서만 일어난다.
이 모듈은 deps.make_driver/make_sense/load_graph 로 부품을 받아 엔진(Learner/Planner/
Executor)을 조립할 뿐, MockRemoteDriver·RemoteMcpClient 등을 직접 임포트하지 않는다.

FastAPI 지연 임포트
-------------------
FastAPI/uvicorn 은 서빙에만 필요하다. CLI 의 learn/inspect/goal 및 테스트는 이들 없이도
동작해야 하므로, FastAPI 관련 임포트는 ``create_app`` / ``serve`` 함수 안에서 지연 수행한다.
모듈 전역 ``app`` 은 PEP 562 ``__getattr__`` 로 **접근 시점에** 생성한다 — 그래서
``import remotectl.api.app`` 자체는 FastAPI 부재 환경에서도 실패하지 않고, uvicorn 이
``app`` 을 조회하는 순간에만 앱이 만들어진다.

대시보드(R7)
------------
``api/static/index.html`` 은 외부 CDN/자산 의존이 0 인 자족 페이지다. GET / 이 이 파일을
그대로 서빙하고, 파일이 없으면 최소 폴백 HTML 을 돌려준다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from remotectl.config import Settings, load_settings
from remotectl.engine.executor import Executor
from remotectl.engine.learner import Learner
from remotectl.engine.planner import Planner, PlanningError
from remotectl.navmap import DEFAULT_COVERAGE_BUTTONS, NavGraph

from remotectl.api import deps

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # 타입 체커 전용(런타임 임포트 회피 — FastAPI 부재 환경 보호).
    from fastapi import FastAPI

__all__ = ["create_app", "main"]


# --------------------------------------------------------------------------- #
# REST 요청 모델(모듈 스코프) — FastAPI 가 요청 바디로 인식하려면 함수 지역이 아닌
# 모듈 전역 타입이어야 안전하다(일부 버전은 지역 정의를 쿼리 파라미터로 오인).
# pydantic 은 하드 의존이라 FastAPI 부재 환경에서도 이 정의는 무해하다.
# --------------------------------------------------------------------------- #


class LearnRequest(BaseModel):
    """POST /learn 요청 바디."""

    step_budget: Optional[int] = Field(default=None, ge=0, description="최대 스텝 예산(미지정 시 설정값).")
    coverage_target: float = Field(default=0.9, ge=0.0, le=1.0, description="커버리지 목표(0~1).")


class GoalRequest(BaseModel):
    """POST /goal 요청 바디."""

    text: str = Field(min_length=1, max_length=500, description="자연어 목표(예: '넷플릭스 켜줘').")


# 대시보드 정적 파일 위치(이 모듈 옆의 static/index.html).
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"

# FastAPI 부재 시(순수 CLI 사용) 최소 폴백 대시보드.
_FALLBACK_HTML = (
    "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
    "<title>STB 리모컨 학습 에이전트</title></head><body style='font-family:sans-serif'>"
    "<h1>STB 리모컨 학습 에이전트</h1>"
    "<p>대시보드 정적 파일(index.html)을 찾을 수 없습니다.</p>"
    "<p>REST API 는 정상 동작합니다: <code>GET /map</code>, <code>POST /goal</code>, "
    "<code>POST /learn</code>.</p></body></html>"
)


# --------------------------------------------------------------------------- #
# 조립 헬퍼 — 매 요청/명령마다 최신 맵을 반영하기 위해 부품을 조립한다.
# --------------------------------------------------------------------------- #


def _transition_view(tr) -> dict[str, Any]:
    """Transition 을 대시보드/JSON 친화 dict 로 변환(key.token 을 편의 필드로 노출)."""
    return {
        "from_state_id": tr.from_state_id,
        "to_state_id": tr.to_state_id,
        "token": tr.key.token,
        "key": {"button": tr.key.button.value, "app_shortcut": tr.key.app_shortcut, "repeat": tr.key.repeat},
        "observed_count": tr.observed_count,
        "success_count": tr.success_count,
        "confidence": tr.confidence,
    }


def _map_view(graph: NavGraph) -> dict[str, Any]:
    """NavGraph 를 /map 응답 dict 로 직렬화(states/transitions/coverage/root).

    ScreenState 는 Pydantic model_dump(mode="json") 로, Transition 은 대시보드가 쓰기 쉬운
    평탄한 형태(_transition_view)로 낸다. coverage 는 기본 버튼 집합 기준 진행률(M4).
    """
    navmap = graph.navmap
    states = [s.model_dump(mode="json") for s in navmap.states.values()]
    transitions = [_transition_view(t) for t in navmap.transitions]
    try:
        coverage = graph.coverage(DEFAULT_COVERAGE_BUTTONS)
    except Exception:  # noqa: BLE001 — 커버리지 실패가 맵 조회를 깨지 않게.
        coverage = None
    return {
        "map_id": navmap.map_id,
        "root_state_id": navmap.root_state_id,
        "states": states,
        "transitions": transitions,
        "state_count": len(states),
        "transition_count": len(transitions),
        "coverage": coverage,
    }


def _path_view(graph: NavGraph, from_id: str, to_id: str) -> dict[str, Any]:
    """/map/path 응답: from→to 최단경로를 PlanStep 열로 변환하고 도달 가능성을 정규화.

    Planner 의 PlanningError(미학습/도달불가)를 잡아 reachable=False + 사유로 정규화한다.
    """
    planner = Planner(graph)
    try:
        steps = planner.plan(from_id, to_id)
    except PlanningError as exc:
        return {"reachable": False, "steps": [], "message": str(exc)}
    return {
        "reachable": True,
        "steps": [s.model_dump(mode="json") for s in steps],
        "hops": len(steps),
    }


# --------------------------------------------------------------------------- #
# FastAPI 앱 팩토리
# --------------------------------------------------------------------------- #


def create_app(settings: Optional[Settings] = None) -> "FastAPI":
    """FastAPI 앱을 조립한다(REST + 대시보드).

    Settings → deps 로 driver/sense/graph 를 만들고 app.state 에 보관한다. 맵은 학습/실행이
    갱신하며, 명시적으로 저장을 요청하면 settings.map_store_path 에 영속화한다.

    등록 라우트
    -----------
    - GET  /health           : 드라이버 DriverInfo + 센스 backend_name(가용성 진단).
    - POST /learn            : UC-1 학습 세션 실행 → LearningSummary(맵도 저장).
    - GET  /map              : UC-2 맵 조회(states/transitions/coverage).
    - GET  /map/path?from=&to= : 두 상태 최단경로(PlanStep 열, 도달불가 정규화).
    - POST /goal             : UC-3 목표 실행 → ExecutionResult(맵도 저장).
    - GET  /                 : 자족 대시보드 HTML(외부 CDN 0, R7).

    Args:
        settings: 런타임 설정. None 이면 load_settings() 로 환경변수에서 로드한다.

    Returns:
        구성된 FastAPI 인스턴스.

    Raises:
        RuntimeError: FastAPI 가 설치되어 있지 않을 때(서빙 의존성 누락).
    """
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.responses import HTMLResponse, JSONResponse, Response
    except ModuleNotFoundError as exc:  # 서빙 의존성 미설치
        raise RuntimeError(
            "FastAPI 가 설치되어 있지 않습니다. 서빙에는 'pip install fastapi \"uvicorn[standard]\"' "
            "가 필요합니다. CLI 의 learn/inspect/goal 은 FastAPI 없이 동작합니다."
        ) from exc

    s = settings or load_settings()

    # 부품 조립(유일 wiring 지점 deps 경유, M5). 드라이버/센스는 앱 수명 동안 재사용한다.
    driver = deps.make_driver(s)
    sense = deps.make_sense(s)
    graph = deps.load_graph(s)

    app = FastAPI(
        title="STB 리모컨 학습 에이전트",
        version="0.1.0",
        description="버튼 입력→화면 전이 학습(UC-1) · 네비게이션 맵(UC-2) · 목표기반 실행(UC-3)",
    )
    # 조립 부품을 app.state 에 보관(라우트가 참조).
    app.state.settings = s
    app.state.driver = driver
    app.state.sense = sense
    app.state.graph = graph

    # --- 내부: 맵 저장(실패해도 요청은 성공 처리) ------------------------- #

    def _persist() -> None:
        try:
            graph.save(s.map_store_path)
        except Exception:  # noqa: BLE001 — 저장 실패가 학습/실행 결과 반환을 막지 않게.
            pass

    # --- 라우트 ------------------------------------------------------------ #

    @app.get("/health")
    def health() -> JSONResponse:
        """드라이버/센스 가용성 진단(대시보드 상태 표시등)."""
        try:
            info = driver.info()
            driver_view = {
                "name": info.name,
                "target": info.target,
                "endpoint": info.endpoint,
                "supports_capture": info.supports_capture,
                "ready": info.ready,
            }
        except Exception as exc:  # noqa: BLE001 — 진단 엔드포인트는 항상 응답해야 함.
            driver_view = {"name": "?", "ready": False, "error": str(exc)}
        return JSONResponse(
            {
                "status": "ok",
                "driver": driver_view,
                "sense": {"backend_name": sense.backend_name},
                "map_store_path": s.map_store_path,
            }
        )

    @app.post("/learn")
    def learn(req: LearnRequest) -> JSONResponse:
        """UC-1 탐색 학습 세션을 실행하고 LearningSummary 를 반환한다(맵 저장 포함)."""
        budget = req.step_budget if req.step_budget is not None else s.learn_step_budget
        learner = Learner(driver, sense, graph, settle_ms=s.settle_ms)
        try:
            summary = learner.learn(step_budget=budget, coverage_target=req.coverage_target)
        except Exception as exc:  # noqa: BLE001 — 초기 관찰 실패 등은 502 로 정규화.
            raise HTTPException(status_code=502, detail=f"학습 실패: {exc}") from exc
        _persist()
        return JSONResponse(summary.model_dump(mode="json"))

    @app.get("/map")
    def get_map() -> JSONResponse:
        """UC-2 네비게이션 맵 조회(states/transitions/coverage)."""
        return JSONResponse(_map_view(graph))

    @app.get("/map/path")
    def get_path(
        from_: str = Query(alias="from", min_length=1),
        to: str = Query(min_length=1),
    ) -> JSONResponse:
        """두 상태 간 최단경로(PlanStep 열). 도달불가/미학습은 reachable=False 로 정규화."""
        return JSONResponse(_path_view(graph, from_, to))

    @app.post("/goal")
    def run_goal(req: GoalRequest) -> JSONResponse:
        """UC-3 자연어 목표를 실행하고 ExecutionResult 를 반환한다(맵 저장 포함).

        Executor 는 예외를 던지지 않고 모든 실패를 status 로 정규화하므로, 항상 200 으로
        결과를 돌려준다(성공 여부는 status 필드로 판단).
        """
        executor = Executor(
            driver, sense, graph, Planner(graph),
            settle_ms=s.settle_ms, replan_budget=s.exec_replan_budget,
        )
        result = executor.run_goal(req.text)
        _persist()
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        """자족 대시보드(R7: 외부 CDN 0). static/index.html 을 그대로 서빙."""
        if _INDEX_HTML.exists():
            return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))
        return HTMLResponse(_FALLBACK_HTML)

    # index.html 이 상대경로(./app.js, ./styles.css)로 로드하는 정적 자산을 서빙한다.
    # 루트(/) StaticFiles 마운트는 위 API 라우트를 가리므로 명시 파일 라우트로만 노출.
    def _serve_asset(filename: str, media_type: str) -> Response:
        target = _STATIC_DIR / filename
        if not target.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        return Response(content=target.read_text(encoding="utf-8"), media_type=media_type)

    @app.get("/app.js", response_class=Response)
    def dashboard_js() -> Response:
        """대시보드 로직(app.js)을 동일 출처로 서빙."""
        return _serve_asset("app.js", "text/javascript")

    @app.get("/styles.css", response_class=Response)
    def dashboard_css() -> Response:
        """대시보드 스타일(styles.css)을 동일 출처로 서빙."""
        return _serve_asset("styles.css", "text/css")

    return app


# --------------------------------------------------------------------------- #
# 모듈 전역 app (PEP 562 지연 생성) — uvicorn remotectl.api.app:app 진입점.
# --------------------------------------------------------------------------- #
#
# 여기서 곧바로 create_app() 을 호출하지 않는다: FastAPI 미설치 환경(순수 CLI/테스트)에서도
# 이 모듈을 임포트할 수 있어야 하기 때문. uvicorn 이 module.app 을 조회하는 그 순간에만
# create_app() 이 실행되어 앱이 만들어진다.

_app_singleton: Optional["FastAPI"] = None


def __getattr__(name: str) -> Any:
    """모듈 속성 지연 해석(PEP 562). ``app`` 접근 시 FastAPI 앱을 1회 생성해 캐시한다."""
    if name == "app":
        global _app_singleton
        if _app_singleton is None:
            _app_singleton = create_app()
        return _app_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cmd_learn(s: Settings, args: argparse.Namespace) -> int:
    """CLI learn: 학습 세션 실행 후 요약을 출력하고 맵을 저장한다."""
    driver = deps.make_driver(s)
    sense = deps.make_sense(s)
    graph = deps.load_graph(s)
    budget = args.steps if args.steps is not None else s.learn_step_budget
    learner = Learner(driver, sense, graph, settle_ms=s.settle_ms)
    try:
        summary = learner.learn(step_budget=budget, coverage_target=args.coverage)
    except Exception as exc:  # noqa: BLE001
        print(f"학습 실패: {exc}", file=sys.stderr)
        return 1
    finally:
        _safe_close(driver)
        _safe_close(sense)
    try:
        graph.save(s.map_store_path)
    except Exception as exc:  # noqa: BLE001
        print(f"경고: 맵 저장 실패({s.map_store_path}): {exc}", file=sys.stderr)
    print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print(
        f"\n학습 완료: 상태 {summary.states_visited} · 전이 {summary.transitions_recorded} "
        f"· 커버리지 {summary.coverage_ratio} → 저장: {s.map_store_path}",
        file=sys.stderr,
    )
    return 0


def _cmd_inspect(s: Settings, args: argparse.Namespace) -> int:
    """CLI inspect: 저장된 맵의 상태/전이/커버리지를 요약 출력한다."""
    graph = deps.load_graph(s)
    view = _map_view(graph)
    print(json.dumps(view, ensure_ascii=False, indent=2))
    print(
        f"\n맵({s.map_store_path}): 상태 {view['state_count']} · 전이 {view['transition_count']} "
        f"· 커버리지 {view['coverage']}",
        file=sys.stderr,
    )
    return 0


def _cmd_goal(s: Settings, args: argparse.Namespace) -> int:
    """CLI goal: 자연어 목표를 실행하고 ExecutionResult 를 출력한다(맵 저장 포함)."""
    driver = deps.make_driver(s)
    sense = deps.make_sense(s)
    graph = deps.load_graph(s)
    executor = Executor(
        driver, sense, graph, Planner(graph),
        settle_ms=s.settle_ms, replan_budget=s.exec_replan_budget,
    )
    try:
        result = executor.run_goal(args.text)
    finally:
        _safe_close(driver)
        _safe_close(sense)
    try:
        graph.save(s.map_store_path)
    except Exception as exc:  # noqa: BLE001
        print(f"경고: 맵 저장 실패({s.map_store_path}): {exc}", file=sys.stderr)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    seq = "  →  ".join(result.button_sequence) or "(입력 없음)"
    print(
        f"\n결과: {result.status.value} · 재계획 {result.replans}회 · 키열: {seq}",
        file=sys.stderr,
    )
    # 성공이면 0, 그 외(미해석/도달불가/예산/드라이버 오류)는 비영(진단 스크립트 친화).
    return 0 if result.succeeded else 2


def _cmd_serve(s: Settings, args: argparse.Namespace) -> int:
    """CLI serve: uvicorn 으로 REST + 대시보드를 기동한다(FastAPI/uvicorn 필요)."""
    try:
        import uvicorn
    except ModuleNotFoundError:
        print(
            "uvicorn 이 설치되어 있지 않습니다. 'pip install \"uvicorn[standard]\" fastapi' 후 재시도하세요.",
            file=sys.stderr,
        )
        return 1
    # settings 를 반영한 앱을 만들어 넘긴다(문자열 임포트 대신 인스턴스 전달).
    app = create_app(s)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _safe_close(obj: Any) -> None:
    """close() 가 있으면 조용히 호출(HTTP 세션 정리). 없거나 실패해도 무시."""
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _build_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서 구성(learn/inspect/goal/serve)."""
    parser = argparse.ArgumentParser(
        prog="remotectl",
        description="STB 리모컨 학습 에이전트 CLI (학습·조회·목표실행·서빙)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_learn = sub.add_parser("learn", help="UC-1 탐색 학습 세션 실행")
    p_learn.add_argument("--steps", type=int, default=None, help="최대 스텝 예산(기본: 설정값)")
    p_learn.add_argument("--coverage", type=float, default=0.9, help="커버리지 목표(0~1)")

    sub.add_parser("inspect", help="UC-2 저장된 맵 요약 조회")

    p_goal = sub.add_parser("goal", help="UC-3 자연어 목표 실행")
    p_goal.add_argument("text", help='자연어 목표 (예: "넷플릭스 켜줘")')

    p_serve = sub.add_parser("serve", help="REST API + 대시보드 서빙(uvicorn)")
    p_serve.add_argument("--host", default="127.0.0.1", help="바인드 호스트")
    p_serve.add_argument("--port", type=int, default=8099, help="포트(기본 8099)")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 진입점. 서브커맨드를 파싱해 실행하고 프로세스 종료 코드를 반환한다.

    서브커맨드
    ----------
    - learn --steps N --coverage C : 탐색 학습 → LearningSummary(맵 저장).
    - inspect                      : 저장된 맵 요약(states/transitions/coverage).
    - goal "<텍스트>"              : 목표 실행 → ExecutionResult. 성공 0, 실패 2.
    - serve --host H --port P      : REST + 대시보드 기동(FastAPI/uvicorn 필요).

    Args:
        argv: 인자 리스트(None 이면 sys.argv[1:] 사용).

    Returns:
        프로세스 종료 코드(0=성공). 잘못된 사용/설정은 비영 값.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        s = load_settings()
    except Exception as exc:  # noqa: BLE001 — 설정 검증 실패를 사용자 오류로 정규화.
        print(f"설정 로드 실패: {exc}", file=sys.stderr)
        return 1

    dispatch = {
        "learn": _cmd_learn,
        "inspect": _cmd_inspect,
        "goal": _cmd_goal,
        "serve": _cmd_serve,
    }
    handler = dispatch.get(args.command)
    if handler is None:  # argparse(required=True) 로 도달 불가하지만 방어적으로.
        parser.print_help(sys.stderr)
        return 1
    return handler(s, args)


if __name__ == "__main__":
    raise SystemExit(main())

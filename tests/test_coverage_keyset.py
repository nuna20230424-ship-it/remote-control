"""커버리지 키집합 재정렬(#1) 테스트.

핵심: 커버리지 분모가 "고정 7키"가 아니라 **실 리모컨 키 집합**(드라이버 보고)이 되고,
키별 커버리지/미시도 키가 리포트로 노출되는지 검증한다.

- RemoteMcpClient.available_keys(): ir-mcp GET /codesets/{codeset} → KeyPress 역매핑.
- Learner: 명시 button_set 없으면 driver.available_keys()(POWER 등 제외)를 채택, 없으면 기본셋 폴백.
- LearningSummary: key_set_source / key_coverage / uncovered_key_tokens.
- NavGraph.coverage_report(): 키별·상태별 미시도 노출(발견 기준 100% 를 감추지 않음).
"""

from __future__ import annotations

import httpx

from remotectl.drivers import MockRemoteDriver, RemoteMcpClient
from remotectl.engine.learner import DEFAULT_BUTTON_SET, Learner
from remotectl.models import Button, KeyPress
from remotectl.navmap import NavGraph
from remotectl.sense import MockScreenSense


# --------------------------------------------------------------------------- #
# RemoteMcpClient.available_keys — 실 코드셋 키 → KeyPress
# --------------------------------------------------------------------------- #


def _codeset_client(keys: list[str]) -> RemoteMcpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/codesets/ref_remote":
            return httpx.Response(200, json={"codeset": "ref_remote", "keys": keys})
        return httpx.Response(404)

    client = httpx.Client(base_url="http://ir.test", transport=httpx.MockTransport(handler))
    return RemoteMcpClient("http://ir.test", client=client, codeset="ref_remote")


def test_mcp_available_keys_maps_codeset_to_keypress():
    drv = _codeset_client(["DPAD_UP", "OK", "VOLUME_UP", "0", "NETFLIX"])
    keys = drv.available_keys()
    # 키맵에 있는 것은 canonical Button, 없는 것(NETFLIX)은 APP_SHORTCUT 로.
    by_token = {k.token for k in keys}
    assert KeyPress(button=Button.UP).token in by_token       # DPAD_UP → UP
    assert KeyPress(button=Button.OK).token in by_token
    assert KeyPress(button=Button.VOL_UP).token in by_token   # VOLUME_UP → VOL_UP
    assert KeyPress(button=Button.NUM_0).token in by_token    # "0" → NUM_0
    nf = [k for k in keys if k.button is Button.APP_SHORTCUT]
    assert len(nf) == 1 and nf[0].app_shortcut == "netflix"


def test_mcp_available_keys_empty_on_unreachable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(base_url="http://dead", transport=httpx.MockTransport(boom))
    drv = RemoteMcpClient("http://dead", client=client)
    assert drv.available_keys() == []  # 미보고 → 학습기가 기본셋 폴백


# --------------------------------------------------------------------------- #
# Learner — 드라이버 실 키집합 채택 / 폴백 / 안전 제외
# --------------------------------------------------------------------------- #


class _KeyReportingDriver(MockRemoteDriver):
    """available_keys 를 보고하는 mock(실 리모컨 키집합 시뮬레이션)."""

    def available_keys(self) -> list[KeyPress]:
        return [
            KeyPress(button=Button.RIGHT),
            KeyPress(button=Button.OK),
            KeyPress(button=Button.VOL_UP),
            KeyPress(button=Button.POWER),  # 안전 제외 대상
            KeyPress(button=Button.APP_SHORTCUT, app_shortcut="netflix"),
        ]


def test_learner_adopts_driver_keyset_excluding_power():
    driver = _KeyReportingDriver()
    learner = Learner(driver, MockScreenSense(), NavGraph(), settle_ms=0)
    assert learner.key_set_source == "driver"
    tokens = {kp.token for kp in learner.button_set}
    # POWER 는 안전 제외, 나머지 실 키는 커버리지 대상에 포함.
    assert not any("POWER" in t for t in tokens)
    assert KeyPress(button=Button.VOL_UP).token in tokens
    assert KeyPress(button=Button.APP_SHORTCUT, app_shortcut="netflix").token in tokens


def test_learner_falls_back_to_default_when_driver_silent():
    # 기본 MockRemoteDriver 는 available_keys()=[] (미보고) → 기본 탐색셋 폴백.
    learner = Learner(MockRemoteDriver(), MockScreenSense(), NavGraph(), settle_ms=0)
    assert learner.key_set_source == "default"
    assert {k.token for k in learner.button_set} == {k.token for k in DEFAULT_BUTTON_SET}


def test_learner_explicit_button_set_wins():
    learner = Learner(
        _KeyReportingDriver(), MockScreenSense(), NavGraph(),
        button_set=[KeyPress(button=Button.OK)], settle_ms=0,
    )
    assert learner.key_set_source == "explicit"
    assert {k.token for k in learner.button_set} == {KeyPress(button=Button.OK).token}


# --------------------------------------------------------------------------- #
# 커버리지 리포트 — 발견 기준 100% 를 감추지 않음
# --------------------------------------------------------------------------- #


def test_summary_reports_key_coverage_over_real_keyset():
    driver = _KeyReportingDriver()
    summary = Learner(driver, MockScreenSense(), NavGraph(), settle_ms=0).learn(
        step_budget=300, coverage_target=1.0
    )
    assert summary.key_set_source == "driver"
    # 커버리지 분모가 실 키집합(POWER 제외 4키) 기준 — key_coverage 에 그 키들이 잡힌다.
    assert summary.key_coverage, "키별 커버리지가 비어선 안 된다"
    assert not any("POWER" in t for t in summary.key_coverage)
    # 포화까지 탐색하면 발견 상태 기준 100% 도달 → 미시도 키 없음.
    assert summary.uncovered_key_tokens == []
    assert summary.coverage_ratio == 1.0


def test_low_target_leaves_uncovered_keys_visible():
    driver = _KeyReportingDriver()
    summary = Learner(driver, MockScreenSense(), NavGraph(), settle_ms=0).learn(
        step_budget=1, coverage_target=1.0  # 1스텝만 → 대부분 미시도
    )
    # 조기 종료 시 미시도 키가 숨지 않고 그대로 노출된다(silent 100% 방지).
    assert summary.coverage_ratio is not None and summary.coverage_ratio < 1.0
    assert len(summary.uncovered_key_tokens) > 0


def test_coverage_report_structure():
    driver = _KeyReportingDriver()
    graph = NavGraph()
    Learner(driver, MockScreenSense(), graph, settle_ms=0).learn(
        step_budget=50, coverage_target=1.0
    )
    report = graph.coverage_report([KeyPress(button=Button.RIGHT), KeyPress(button=Button.OK)])
    assert set(report) == {"overall", "state_count", "key_count", "per_key", "uncovered"}
    assert report["key_count"] == 2
    for tok, pk in report["per_key"].items():
        assert set(pk) == {"tried_states", "ratio"}

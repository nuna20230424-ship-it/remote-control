"use strict";
/*
 * remotectl 운영자 대시보드 — 클라이언트 로직 (바닐라 JS, 의존성 0).
 *
 * 역할
 * ----
 * - REST 호출(fetch)·클라이언트 상태 관리·DOM 렌더링을 담당한다.
 * - models.py 정본 필드를 "그대로" 소비한다. 임의 필드를 신설하지 않는다.
 *   computed 필드(ExecutionResult.button_sequence / step_count)는 서버가 계산한 값을
 *   표시만 하고 클라이언트에서 재계산하지 않는다.
 *
 * 소비 계약(§DESIGN 4 / api.app 라우트)
 * -------------------------------------
 *   GET  /health              -> {status, driver:{name,target,endpoint,supports_capture,ready}, sense:{backend_name}, map_store_path}
 *   GET  /map                 -> {map_id, root_state_id, states:[ScreenState], transitions:[{from_state_id,to_state_id,token,key:{button,app_shortcut,repeat},observed_count,success_count,confidence}], state_count, transition_count, coverage}
 *   POST /learn {step_budget, coverage_target} -> LearningSummary
 *   GET  /map/path?from=&to=  -> {reachable, steps:[PlanStep], hops} | {reachable:false, steps:[], message}
 *   POST /goal {text}         -> ExecutionResult
 *
 * DOM 계약
 * --------
 * index.html(§DESIGN 2)이 제공하는 시맨틱 컨테이너 id 를 소비한다:
 *   #statusbar #learn-panel #goal-panel #map-canvas #exec-trace #obs-log
 * 각 패널 안의 세부 컨트롤/슬롯은 이 파일이 (없으면) 만들어 붙인다 — 그래서 index.html 의
 * 정확한 내부 마크업에 강결합하지 않고, 문서화된 상위 컨테이너 id 에만 의존한다.
 * (기존 id 가 이미 있으면 그대로 재사용한다: getEl 은 조회 우선.)
 */

(function () {
  // ---------------------------------------------------------------------- //
  // 0. 상수 / 유틸
  // ---------------------------------------------------------------------- //

  var NS = "http://www.w3.org/2000/svg";
  var REDUCED_MOTION =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // StateKind -> 색 힌트 토큰(§DESIGN 1.5). 값은 CSS 커스텀 프로퍼티명(styles.css 소유).
  var KIND_COLOR = {
    home: "--signal",
    app: "--good",
    menu: "--ink-dim",
    settings: "--ink-dim",
    playback: "--warn",
    live_tv: "--good",
    dialog: "--warn",
    loading: "--warn",
    unknown: "--line",
  };
  var KIND_LABEL = {
    home: "홈",
    app: "앱",
    menu: "메뉴",
    settings: "설정",
    playback: "재생",
    live_tv: "라이브",
    dialog: "대화상자",
    loading: "로딩",
    unknown: "미상",
  };
  // ExecutionStatus -> {pill 클래스(good/warn/crit), 사람이 읽는 라벨}.
  var STATUS_INFO = {
    success: { cls: "good", text: "성공" },
    failed_unreachable: { cls: "crit", text: "도달불가" },
    failed_unresolved: { cls: "warn", text: "미해석" },
    failed_budget: { cls: "warn", text: "예산소진" },
    failed_driver: { cls: "crit", text: "드라이버오류" },
  };

  var OBS_RING_MAX = 200; // 관측 로그 링 버퍼 최대 행수.
  var LOW_CONF = 0.5; // 저신뢰 임계(warn).

  function cssVar(name) {
    // 색 토큰을 실제 값으로 해석(SVG fill/stroke 는 var() 미지원 브라우저 대비 직접 값 사용).
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v || "").trim() || "#7C929B";
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function num(n) {
    return n == null || isNaN(n) ? 0 : n;
  }
  function pct(x) {
    return x == null ? "—" : Math.round(x * 100) + "%";
  }
  function shortId(id) {
    if (!id) return "—";
    return id.length > 12 ? id.slice(0, 12) + "…" : id;
  }
  function nowClock() {
    var d = new Date();
    function p(n) {
      return (n < 10 ? "0" : "") + n;
    }
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }
  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        if (k === "class") e.className = attrs[k];
        else if (k === "text") e.textContent = attrs[k];
        else if (k === "html") e.innerHTML = attrs[k];
        else if (k.indexOf("on") === 0 && typeof attrs[k] === "function")
          e.addEventListener(k.slice(2), attrs[k]);
        else e.setAttribute(k, attrs[k]);
      }
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return e;
  }
  function svgEl(tag, attrs) {
    var e = document.createElementNS(NS, tag);
    if (attrs)
      for (var k in attrs)
        if (Object.prototype.hasOwnProperty.call(attrs, k)) e.setAttribute(k, attrs[k]);
    return e;
  }
  // 컨테이너 조회 우선; 없으면 지정 부모(기본 body)에 만들어 붙인다.
  function getEl(id, tag, parent) {
    var e = document.getElementById(id);
    if (!e) {
      e = document.createElement(tag || "div");
      e.id = id;
      (parent || document.body).appendChild(e);
    }
    return e;
  }
  // 부모 안의 하위 슬롯(자식 요소) 조회 우선; 없으면 만들어 붙인다.
  function slot(parent, id, tag, attrs) {
    var e = document.getElementById(id);
    if (!e) {
      e = el(tag || "div", attrs || {});
      e.id = id;
      parent.appendChild(e);
    }
    return e;
  }

  // ---------------------------------------------------------------------- //
  // 1. fetch 래퍼 (JSON in/out, 에러 정규화)
  // ---------------------------------------------------------------------- //
  function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try {
          data = text ? JSON.parse(text) : null;
        } catch (_) {
          data = text;
        }
        if (!resp.ok) {
          var detail =
            data && data.detail
              ? typeof data.detail === "string"
                ? data.detail
                : JSON.stringify(data.detail)
              : typeof data === "string" && data
              ? data
              : resp.statusText;
          throw new Error(detail || "HTTP " + resp.status);
        }
        return data;
      });
    });
  }

  // ---------------------------------------------------------------------- //
  // 2. 상태 스토어
  // ---------------------------------------------------------------------- //
  var store = {
    health: null, // GET /health 응답
    map: null, // GET /map 응답
    lastExecution: null, // 직전 ExecutionResult
    lastSummary: null, // 직전 LearningSummary
    obs: [], // 관측 로그 링 버퍼(신규가 앞)
    learning: false, // 학습 요청 진행 중(라이브 인디케이터)
    currentPath: null, // 하이라이트할 상태 id 시퀀스(맵 signal 강조)
  };

  function pushObs(row) {
    // row: {t, kind:'LEARN'|'EXEC', seq, from, key, to, signature, confidence}
    store.obs.unshift(row);
    if (store.obs.length > OBS_RING_MAX) store.obs.length = OBS_RING_MAX;
  }

  // ---------------------------------------------------------------------- //
  // 3. Toast (사람이 읽는 fetch 오류 / 작업 결과) — §DESIGN 3
  // ---------------------------------------------------------------------- //
  var toastHost = null;
  function toast(msg, kind) {
    if (!toastHost) {
      toastHost = el("div", { id: "toast-host", class: "toast-host" });
      document.body.appendChild(toastHost);
    }
    var t = el("div", { class: "toast toast-" + (kind || "info"), role: "status" }, [
      el("span", { class: "toast-dot" }),
      el("span", { text: msg }),
    ]);
    toastHost.appendChild(t);
    var life = REDUCED_MOTION ? 6000 : 5000;
    setTimeout(function () {
      t.classList.add("toast-out");
      setTimeout(function () {
        if (t.parentNode) t.parentNode.removeChild(t);
      }, REDUCED_MOTION ? 0 : 200);
    }, life);
  }

  // 재사용 UI 헬퍼 --------------------------------------------------------- //
  function pill(text, cls) {
    return el("span", { class: "pill pill-" + (cls || "line"), text: text });
  }
  function keyToken(token) {
    return el("span", { class: "key-token", text: token });
  }
  function meter(value, warnLow) {
    // 0..1 신뢰도 막대. <0.5 면 warn.
    var v = Math.max(0, Math.min(1, num(value)));
    var fill = el("span", { class: "meter-fill" });
    fill.style.width = Math.round(v * 100) + "%";
    var bar = el("span", { class: "meter" + (warnLow && v < LOW_CONF ? " meter-warn" : "") }, [fill]);
    return bar;
  }
  function emptyState(msg) {
    return el("div", { class: "empty-state", text: msg });
  }

  // ---------------------------------------------------------------------- //
  // 4. 조작 레일 마크업 보장 (학습/목표 패널 내부 컨트롤 슬롯)
  // ---------------------------------------------------------------------- //
  function ensureLearnPanel() {
    var panel = getEl("learn-panel", "section");
    if (document.getElementById("learn-steps")) return; // 이미 index.html 이 제공.
    panel.classList.add("panel");
    panel.appendChild(el("h2", { class: "panel-title", text: "UC-1 · 학습" }));
    var grid = el("div", { class: "field-row" });
    var f1 = el("div", { class: "field" }, [
      el("label", { for: "learn-steps", text: "STEP BUDGET" }),
      el("input", { type: "number", id: "learn-steps", value: "200", min: "0" }),
    ]);
    var f2 = el("div", { class: "field" }, [
      el("label", { for: "learn-cov", text: "COVERAGE TARGET" }),
      el("input", { type: "number", id: "learn-cov", value: "0.9", min: "0", max: "1", step: "0.05" }),
    ]);
    grid.appendChild(f1);
    grid.appendChild(f2);
    panel.appendChild(grid);
    var actions = el("div", { class: "btn-row" }, [
      el("button", { id: "btn-learn", class: "btn btn-primary", text: "학습 시작" }),
      el("button", { id: "btn-learn-stop", class: "btn btn-secondary", text: "중지", disabled: "" }),
    ]);
    panel.appendChild(actions);
    panel.appendChild(el("div", { id: "learn-progress" }));
    panel.appendChild(el("div", { id: "learn-summary", class: "summary-slot" }));
  }

  function ensureGoalPanel() {
    var panel = getEl("goal-panel", "section");
    if (document.getElementById("goal-text")) return;
    panel.classList.add("panel");
    panel.appendChild(el("h2", { class: "panel-title", text: "UC-3 · 목표 실행" }));
    panel.appendChild(el("label", { for: "goal-text", text: "자연어 목표" }));
    panel.appendChild(
      el("input", { type: "text", id: "goal-text", placeholder: "예: 넷플릭스 켜줘" })
    );
    var chips = el("div", { class: "quick-chips", id: "goal-chips" });
    [
      ["넷플릭스 켜줘", "넷플릭스"],
      ["유튜브 켜줘", "유튜브"],
      ["설정 열어줘", "설정"],
      ["홈으로", "홈으로"],
    ].forEach(function (pair) {
      chips.appendChild(
        el("button", {
          type: "button",
          class: "chip chip-action",
          text: pair[1],
          "data-goal": pair[0],
        })
      );
    });
    panel.appendChild(chips);
    panel.appendChild(
      el("div", { class: "btn-row" }, [
        el("button", { id: "btn-goal", class: "btn btn-primary", text: "실행" }),
        el("button", { id: "btn-path-preview", class: "btn btn-ghost", text: "경로만 보기" }),
      ])
    );
    panel.appendChild(el("div", { id: "goal-result", class: "summary-slot" }));
  }

  // ---------------------------------------------------------------------- //
  // 5. 상태 바 렌더 (#statusbar) — §DESIGN 2.A
  // ---------------------------------------------------------------------- //
  function renderStatusbar() {
    var bar = getEl("statusbar", "header");
    if (!bar.classList.contains("statusbar")) bar.classList.add("statusbar");

    var h = store.health || {};
    var d = h.driver || {};
    var s = h.sense || {};
    var map = store.map || {};

    // 드라이버 가용성 점: ready=true good / false crit / 미확인 dim.
    var dReady = d.ready;
    var dCls = dReady === true ? "good" : dReady === false ? "crit" : "dim";
    var dReadyText = dReady === true ? "가용" : dReady === false ? "도달불가" : "미확인";
    var driverEndpoint = d.endpoint ? " · " + d.endpoint : d.target ? " · " + d.target : "";

    var unexplored = store.lastSummary ? num(store.lastSummary.unexplored_edges) : null;
    var cov = map.coverage;

    bar.innerHTML = "";

    // 제품 마크
    var brand = el("div", { class: "brand" }, [
      el("span", { class: "brand-mark", text: "remotectl" }),
      el("span", { class: "brand-sub", text: "STB 리모컨 학습 에이전트" }),
    ]);

    // 백엔드 칩 2개
    var backends = el("div", { class: "sb-backends" }, [
      el("span", { class: "backend-chip", title: "드라이버: " + (d.name || "?") + driverEndpoint }, [
        el("span", { class: "dot dot-" + dCls }),
        el("span", { class: "backend-role", text: "DRIVER" }),
        el("span", { class: "backend-name", text: (d.name || "?") + " · " + dReadyText }),
      ]),
      el("span", { class: "backend-chip", title: "센스: " + (s.backend_name || "?") }, [
        el("span", { class: "dot dot-" + (s.backend_name ? "good" : "dim") }),
        el("span", { class: "backend-role", text: "SENSE" }),
        el("span", { class: "backend-name", text: s.backend_name || "?" }),
      ]),
    ]);

    // 카운터
    function counter(label, value) {
      return el("div", { class: "sb-counter" }, [
        el("span", { class: "sb-count-n", text: String(value) }),
        el("span", { class: "sb-count-k", text: label }),
      ]);
    }
    var counters = el("div", { class: "sb-counters" }, [
      counter("상태", num(map.state_count)),
      counter("전이", num(map.transition_count)),
    ]);

    // 커버리지 진행 막대 + mono %
    var covWrap = el("div", { class: "sb-coverage" }, [
      el("span", { class: "sb-count-k", text: "커버리지" }),
      (function () {
        var fill = el("span", { class: "meter-fill" });
        fill.style.width = Math.round(num(cov) * 100) + "%";
        return el("span", { class: "meter meter-signal" }, [fill]);
      })(),
      el("span", { class: "mono sb-cov-val", text: pct(cov) }),
    ]);

    // 미탐색 배지
    var unexpBadge = el(
      "span",
      { class: "badge " + (unexplored ? "badge-warn" : "badge-mute") },
      ["미탐색 " + (unexplored == null ? "—" : unexplored)]
    );

    // 학습 라이브 인디케이터
    var live = el(
      "div",
      { class: "live-indicator" + (store.learning ? " live-on" : ""), "aria-live": "polite" },
      [
        el("span", { class: "live-dot" }),
        el("span", { class: "mono", text: store.learning ? "LEARNING…" : "IDLE" }),
      ]
    );

    bar.appendChild(brand);
    bar.appendChild(backends);
    bar.appendChild(counters);
    bar.appendChild(covWrap);
    bar.appendChild(unexpBadge);
    bar.appendChild(live);
  }

  // ---------------------------------------------------------------------- //
  // 6. 학습 요약 렌더 (#learn-summary) — LearningSummary
  // ---------------------------------------------------------------------- //
  function renderLearnSummary() {
    var host = getEl("learn-summary", "div", document.getElementById("learn-panel") || document.body);
    host.innerHTML = "";
    var s = store.lastSummary;
    if (!s) {
      host.appendChild(emptyState("학습을 시작하면 요약이 여기 표시됩니다."));
      return;
    }
    // coverage_ratio 링
    var ring = ringSvg(s.coverage_ratio);
    var stats = el("div", { class: "summary-metrics" }, [
      metricBox("스텝", s.steps_taken),
      metricBox("상태", s.states_visited),
      metricBox("전이", s.transitions_recorded),
      metricBox("미탐색", s.unexplored_edges),
    ]);
    host.appendChild(
      el("div", { class: "summary-head" }, [
        ring,
        el("div", {}, [
          el("div", { class: "summary-title", text: "학습 세션 요약" }),
          el("div", { class: "mono summary-sub", text: "session " + shortId(s.session_id) }),
        ]),
      ])
    );
    host.appendChild(stats);
    host.appendChild(
      el("div", { class: "summary-note" }, [
        el("span", { class: "label-cap", text: "종료 사유 " }),
        el("span", { text: s.stop_reason || "-" }),
      ])
    );
  }
  function metricBox(k, v) {
    return el("div", { class: "metric" }, [
      el("span", { class: "metric-n mono", text: String(num(v)) }),
      el("span", { class: "metric-k", text: k }),
    ]);
  }
  function ringSvg(ratio) {
    var r = ratio == null ? 0 : Math.max(0, Math.min(1, ratio));
    var size = 52,
      stroke = 6,
      rad = (size - stroke) / 2,
      c = 2 * Math.PI * rad;
    var svg = svgEl("svg", { width: size, height: size, class: "ring", viewBox: "0 0 " + size + " " + size });
    var track = svgEl("circle", {
      cx: size / 2,
      cy: size / 2,
      r: rad,
      fill: "none",
      stroke: cssVar("--signal-soft"),
      "stroke-width": stroke,
    });
    var arc = svgEl("circle", {
      cx: size / 2,
      cy: size / 2,
      r: rad,
      fill: "none",
      stroke: cssVar("--signal"),
      "stroke-width": stroke,
      "stroke-linecap": "round",
      "stroke-dasharray": c,
      "stroke-dashoffset": c * (1 - r),
      transform: "rotate(-90 " + size / 2 + " " + size / 2 + ")",
    });
    var txt = svgEl("text", {
      x: size / 2,
      y: size / 2,
      "text-anchor": "middle",
      "dominant-baseline": "central",
      class: "ring-label",
    });
    txt.textContent = ratio == null ? "—" : Math.round(r * 100) + "%";
    svg.appendChild(track);
    svg.appendChild(arc);
    svg.appendChild(txt);
    return svg;
  }

  // ---------------------------------------------------------------------- //
  // 7. 목표 결과 렌더 (#goal-result) — ExecutionResult
  // ---------------------------------------------------------------------- //
  function renderGoalResult() {
    var host = getEl("goal-result", "div", document.getElementById("goal-panel") || document.body);
    host.innerHTML = "";
    var r = store.lastExecution;
    if (!r) {
      host.appendChild(emptyState("목표를 실행하면 결과가 여기 표시됩니다."));
      return;
    }
    var info = STATUS_INFO[r.status] || { cls: "warn", text: r.status };

    // 헤더: status pill + replans 배지
    var head = el("div", { class: "result-head" }, [
      el("span", { class: "pill pill-" + info.cls }, [info.text + " · " + r.status]),
      el(
        "span",
        { class: "badge " + (num(r.replans) > 0 ? "badge-warn" : "badge-mute") },
        ["재계획 " + num(r.replans)]
      ),
      el("span", { class: "badge badge-mute" }, ["스텝 " + num(r.step_count)]),
    ]);
    host.appendChild(head);

    // start -> final
    host.appendChild(
      el("div", { class: "mono result-route" }, [
        shortId(r.start_state_id) + "  →  " + shortId(r.final_state_id),
      ])
    );

    // button_sequence (서버 computed, 표시만)
    var seq = r.button_sequence || [];
    if (seq.length) {
      var chips = el("div", { class: "token-strip" });
      seq.forEach(function (tk, i) {
        chips.appendChild(keyToken(tk));
        if (i < seq.length - 1) chips.appendChild(el("span", { class: "token-arrow", text: "→" }));
      });
      host.appendChild(chips);
    }

    // 목표 해석 사유(미해석/미매핑) — resolve_note 를 crit/warn 박스로
    var goal = r.goal || {};
    if (goal.resolve_note && r.status !== "success") {
      host.appendChild(
        el("div", { class: "notice notice-" + (info.cls === "crit" ? "crit" : "warn") }, [
          el("span", { class: "label-cap", text: "사유 " }),
          el("span", { text: goal.resolve_note }),
        ])
      );
    }
    if (r.message) {
      host.appendChild(el("div", { class: "result-msg", text: r.message }));
    }
  }

  // ---------------------------------------------------------------------- //
  // 8. 실행 스텝 트레이스 (#exec-trace) — ExecutionResult.steps[PlanStep]
  // ---------------------------------------------------------------------- //
  function renderExecTrace() {
    var host = getEl("exec-trace", "section");
    if (!host.classList.contains("panel")) host.classList.add("panel");
    host.innerHTML = "";
    host.appendChild(el("h2", { class: "panel-title", text: "실행 스텝 트레이스" }));

    var r = store.lastExecution;
    if (!r || !(r.steps && r.steps.length)) {
      host.appendChild(emptyState("실행 이력이 없습니다."));
      return;
    }
    var info = STATUS_INFO[r.status] || { cls: "warn", text: r.status };
    var dur = "";
    if (r.started_at && r.finished_at) {
      var ms = new Date(r.finished_at) - new Date(r.started_at);
      if (!isNaN(ms) && ms >= 0) dur = (ms / 1000).toFixed(2) + "s";
    }
    host.appendChild(
      el("div", { class: "trace-head" }, [
        el("span", { class: "pill pill-" + info.cls, text: info.text }),
        el(
          "span",
          { class: "badge " + (num(r.replans) > 0 ? "badge-warn" : "badge-mute") },
          ["재계획 " + num(r.replans)]
        ),
        dur ? el("span", { class: "mono trace-dur", text: dur }) : null,
      ])
    );

    var wrap = el("div", { class: "table-scroll" });
    var table = el("table", { class: "trace-table" });
    table.appendChild(
      el("thead", {}, [
        el("tr", {}, [
          el("th", { text: "#" }),
          el("th", { text: "FROM" }),
          el("th", { text: "KEY" }),
          el("th", { text: "EXPECTED" }),
          el("th", { text: "ACTUAL" }),
          el("th", { text: "일치" }),
          el("th", { text: "신뢰도" }),
        ]),
      ])
    );
    var tbody = el("tbody");
    r.steps.forEach(function (step) {
      // matched: true=good / false=crit / null(미실행)=line
      var stripe =
        step.matched === true ? "matched" : step.matched === false ? "mismatch" : "pending";
      var matchTxt =
        step.matched === true ? "일치" : step.matched === false ? "불일치" : "—";
      var tok = step.key ? step.key.token || step.key.button : "";
      // key.token 은 서버 편의 필드가 없을 수 있어(PlanStep.key 는 KeyPress model_dump) 재구성.
      if (step.key && !step.key.token) tok = keyTokenString(step.key);
      var confCell = el("td");
      if (step.observed_confidence != null) {
        confCell.appendChild(meter(step.observed_confidence, true));
        confCell.appendChild(el("span", { class: "mono conf-num", text: num(step.observed_confidence).toFixed(2) }));
      } else {
        confCell.textContent = "—";
      }
      var tr = el("tr", { class: "trace-row trace-" + stripe }, [
        el("td", { class: "mono", text: String(step.index) }),
        el("td", { class: "mono", text: shortId(step.from_state_id) }),
        el("td", {}, [keyToken(tok)]),
        el("td", { class: "mono", text: shortId(step.expected_to_state_id) }),
        el("td", { class: "mono", text: shortId(step.actual_to_state_id) }),
        el("td", {}, [el("span", { class: "match-tag match-" + stripe, text: matchTxt })]),
        confCell,
      ]);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);
  }

  // KeyPress model_dump({button, app_shortcut, repeat}) -> token 문자열(서버 token 부재 시 폴백).
  function keyTokenString(key) {
    if (!key) return "";
    var base = key.button;
    if (key.button === "APP_SHORTCUT" && key.app_shortcut) base = base + ":" + key.app_shortcut;
    return key.repeat && key.repeat > 1 ? base + "*" + key.repeat : base;
  }

  // ---------------------------------------------------------------------- //
  // 9. 관측 로그 (#obs-log) — 클라이언트 파생 원장(링 버퍼)
  // ---------------------------------------------------------------------- //
  var obsFilter = "ALL"; // ALL | LEARN | EXEC | LOW
  function renderObsLog() {
    var host = getEl("obs-log", "section");
    if (!host.classList.contains("panel")) host.classList.add("panel");
    host.innerHTML = "";

    var head = el("div", { class: "obs-head" }, [
      el("h2", { class: "panel-title", text: "관측 로그" }),
    ]);
    var filters = el("div", { class: "obs-filters" });
    [
      ["ALL", "전체"],
      ["LEARN", "학습"],
      ["EXEC", "실행"],
      ["LOW", "저신뢰"],
    ].forEach(function (f) {
      filters.appendChild(
        el("button", {
          type: "button",
          class: "chip chip-filter" + (obsFilter === f[0] ? " chip-on" : ""),
          text: f[1],
          onclick: function () {
            obsFilter = f[0];
            renderObsLog();
          },
        })
      );
    });
    filters.appendChild(
      el("button", {
        type: "button",
        class: "chip chip-ghost",
        text: "지우기",
        onclick: function () {
          store.obs = [];
          renderObsLog();
        },
      })
    );
    head.appendChild(filters);
    host.appendChild(head);

    var rows = store.obs.filter(function (o) {
      if (obsFilter === "ALL") return true;
      if (obsFilter === "LOW") return num(o.confidence) < LOW_CONF;
      return o.kind === obsFilter;
    });

    if (!rows.length) {
      host.appendChild(emptyState("관측 로그가 비어 있습니다."));
      return;
    }

    var wrap = el("div", { class: "table-scroll obs-scroll" });
    var table = el("table", { class: "obs-table" });
    table.appendChild(
      el("thead", {}, [
        el("tr", {}, [
          el("th", { text: "시각" }),
          el("th", { text: "종류" }),
          el("th", { text: "SEQ" }),
          el("th", { text: "FROM→KEY→TO" }),
          el("th", { text: "SIGNATURE" }),
          el("th", { text: "신뢰도" }),
        ]),
      ])
    );
    var tbody = el("tbody");
    rows.forEach(function (o) {
      var low = num(o.confidence) < LOW_CONF;
      var causal =
        shortId(o.from) + "  →  " + (o.key || "—") + "  →  " + shortId(o.to);
      tbody.appendChild(
        el("tr", { class: "obs-row" }, [
          el("td", { class: "mono", text: o.t }),
          el("td", {}, [el("span", { class: "tag tag-" + (o.kind === "EXEC" ? "exec" : "learn"), text: o.kind })]),
          el("td", { class: "mono", text: o.seq == null ? "—" : String(o.seq) }),
          el("td", { class: "mono", text: causal }),
          el("td", { class: "mono obs-sig", title: o.signature || "", text: o.signature || "—" }),
          el("td", { class: "mono" + (low ? " conf-low" : "") }, [
            o.confidence == null ? "—" : num(o.confidence).toFixed(2),
          ]),
        ])
      );
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);
  }

  // ---------------------------------------------------------------------- //
  // 10. 맵 렌더 (#map-canvas) — SVG, root 상단 고정, 자체 계층 배치
  // ---------------------------------------------------------------------- //
  var mapView = { zoom: 1, showLabels: true, lowOnly: false, query: "" };

  function ensureMapPanel() {
    var panel = getEl("map-canvas", "section");
    if (!panel.classList.contains("panel")) panel.classList.add("panel");
    if (document.getElementById("map-toolbar")) return panel;
    panel.appendChild(el("h2", { class: "panel-title", text: "UC-2 · 네비게이션 맵" }));
    var toolbar = el("div", { class: "map-toolbar", id: "map-toolbar" }, [
      el("button", { type: "button", class: "chip", id: "map-zoom-in", text: "＋" }),
      el("button", { type: "button", class: "chip", id: "map-zoom-out", text: "－" }),
      el("button", { type: "button", class: "chip", id: "map-zoom-reset", text: "리셋" }),
      el("button", { type: "button", class: "chip chip-on", id: "map-labels", text: "라벨" }),
      el("button", { type: "button", class: "chip", id: "map-lowonly", text: "저신뢰만" }),
      el("input", { type: "text", class: "map-search", id: "map-search", placeholder: "라벨/앱 검색" }),
      el("button", { type: "button", class: "chip", id: "map-refresh", text: "새로고침" }),
    ]);
    panel.appendChild(toolbar);
    var canvas = el("div", { class: "map-canvas-wrap", id: "map-svg-wrap" });
    panel.appendChild(canvas);
    panel.appendChild(el("div", { id: "map-popover", class: "map-popover", hidden: "" }));

    // 툴바 이벤트
    toolbar.querySelector("#map-zoom-in").addEventListener("click", function () {
      mapView.zoom = Math.min(2.5, mapView.zoom * 1.2);
      renderMap();
    });
    toolbar.querySelector("#map-zoom-out").addEventListener("click", function () {
      mapView.zoom = Math.max(0.4, mapView.zoom / 1.2);
      renderMap();
    });
    toolbar.querySelector("#map-zoom-reset").addEventListener("click", function () {
      mapView.zoom = 1;
      renderMap();
    });
    var lblBtn = toolbar.querySelector("#map-labels");
    lblBtn.addEventListener("click", function () {
      mapView.showLabels = !mapView.showLabels;
      lblBtn.classList.toggle("chip-on", mapView.showLabels);
      renderMap();
    });
    var lowBtn = toolbar.querySelector("#map-lowonly");
    lowBtn.addEventListener("click", function () {
      mapView.lowOnly = !mapView.lowOnly;
      lowBtn.classList.toggle("chip-on", mapView.lowOnly);
      renderMap();
    });
    toolbar.querySelector("#map-search").addEventListener("input", function (e) {
      mapView.query = e.target.value.trim().toLowerCase();
      renderMap();
    });
    toolbar.querySelector("#map-refresh").addEventListener("click", refreshMap);
    return panel;
  }

  // root 를 최상단으로 하는 BFS 레이어 배치(간단·결정론적). networkx 없이 순수 계산.
  function layoutLayers(states, transitions, rootId) {
    var idset = {};
    states.forEach(function (s) {
      idset[s.id] = true;
    });
    var adj = {};
    states.forEach(function (s) {
      adj[s.id] = [];
    });
    transitions.forEach(function (t) {
      if (adj[t.from_state_id] && idset[t.to_state_id] && t.from_state_id !== t.to_state_id)
        adj[t.from_state_id].push(t.to_state_id);
    });
    var start = rootId && idset[rootId] ? rootId : states.length ? states[0].id : null;
    var depth = {};
    if (start) {
      var q = [start];
      depth[start] = 0;
      while (q.length) {
        var cur = q.shift();
        (adj[cur] || []).forEach(function (nx) {
          if (depth[nx] == null) {
            depth[nx] = depth[cur] + 1;
            q.push(nx);
          }
        });
      }
    }
    // 미연결 노드는 최하단 레이어로.
    var maxD = 0;
    states.forEach(function (s) {
      if (depth[s.id] != null && depth[s.id] > maxD) maxD = depth[s.id];
    });
    states.forEach(function (s) {
      if (depth[s.id] == null) depth[s.id] = maxD + 1;
    });
    // 레이어별 그룹핑(입력 순서 보존 → 결정론적).
    var layers = {};
    states.forEach(function (s) {
      (layers[depth[s.id]] = layers[depth[s.id]] || []).push(s);
    });
    return layers;
  }

  function renderMap() {
    ensureMapPanel();
    var wrap = getEl("map-svg-wrap", "div", document.getElementById("map-canvas"));
    wrap.innerHTML = "";
    var map = store.map || {};
    var states = (map.states || []).slice();
    var transitions = map.transitions || [];

    // 검색/필터
    var visible = states.filter(function (s) {
      if (mapView.lowOnly && num(s.confidence) >= LOW_CONF) return false;
      if (mapView.query) {
        var hay = ((s.label || "") + " " + (s.app_id || "") + " " + (s.kind || "")).toLowerCase();
        if (hay.indexOf(mapView.query) < 0) return false;
      }
      return true;
    });

    if (!states.length) {
      wrap.appendChild(
        emptyState("아직 학습된 화면이 없습니다. 좌측에서 학습을 시작하세요.")
      );
      return;
    }
    if (!visible.length) {
      wrap.appendChild(emptyState("필터/검색 조건에 맞는 상태가 없습니다."));
      return;
    }

    var layers = layoutLayers(visible, transitions, map.root_state_id);
    var depths = Object.keys(layers)
      .map(Number)
      .sort(function (a, b) {
        return a - b;
      });
    var maxRow = 0;
    depths.forEach(function (d) {
      if (layers[d].length > maxRow) maxRow = layers[d].length;
    });

    var nodeR = 22;
    var colGap = 150 * mapView.zoom;
    var rowGap = 120 * mapView.zoom;
    var padX = 70,
      padY = 60;
    var W = Math.max(560, padX * 2 + Math.max(1, maxRow - 1) * colGap);
    var H = Math.max(300, padY * 2 + Math.max(1, depths.length - 1) * rowGap);

    var pos = {};
    depths.forEach(function (d, di) {
      var row = layers[d];
      var y = padY + di * rowGap;
      row.forEach(function (s, ri) {
        var span = row.length;
        var x = span === 1 ? W / 2 : padX + (ri * (W - 2 * padX)) / (span - 1);
        pos[s.id] = { x: x, y: y, st: s };
      });
    });

    var svg = svgEl("svg", {
      viewBox: "0 0 " + W + " " + H,
      class: "map-svg",
      role: "img",
      "aria-label": "네비게이션 맵",
    });
    svg.style.minWidth = Math.min(W, 1200) + "px";

    // 화살표 마커(기본/신호 경로).
    var defs = svgEl("defs");
    ["arrow", "arrow-signal", "arrow-warn"].forEach(function (mid) {
      var color =
        mid === "arrow-signal" ? cssVar("--signal") : mid === "arrow-warn" ? cssVar("--warn") : cssVar("--line");
      var marker = svgEl("marker", {
        id: mid,
        viewBox: "0 0 10 10",
        refX: "9",
        refY: "5",
        markerWidth: "7",
        markerHeight: "7",
        orient: "auto-start-reverse",
      });
      var path = svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: color });
      marker.appendChild(path);
      defs.appendChild(marker);
    });
    svg.appendChild(defs);

    // 현재 하이라이트 경로(연속 상태쌍 집합).
    var pathEdges = {};
    if (store.currentPath && store.currentPath.length > 1) {
      for (var i = 0; i < store.currentPath.length - 1; i++)
        pathEdges[store.currentPath[i] + "|" + store.currentPath[i + 1]] = true;
    }

    // 엣지
    var edgeLayer = svgEl("g", { class: "edge-layer" });
    transitions.forEach(function (t) {
      var a = pos[t.from_state_id],
        b = pos[t.to_state_id];
      if (!a || !b) return;
      var token = t.token || keyTokenString(t.key);
      if (t.from_state_id === t.to_state_id) {
        // self-loop: 노드 위쪽 작은 호.
        var loop = svgEl("path", {
          d:
            "M" +
            (a.x - 6) +
            "," +
            (a.y - nodeR) +
            " C" +
            (a.x - 26) +
            "," +
            (a.y - nodeR - 34) +
            " " +
            (a.x + 26) +
            "," +
            (a.y - nodeR - 34) +
            " " +
            (a.x + 6) +
            "," +
            (a.y - nodeR),
          fill: "none",
          stroke: cssVar("--line"),
          "stroke-width": "1.2",
          "marker-end": "url(#arrow)",
        });
        edgeLayer.appendChild(loop);
        return;
      }
      var dx = b.x - a.x,
        dy = b.y - a.y,
        len = Math.hypot(dx, dy) || 1;
      var x1 = a.x + (dx / len) * nodeR,
        y1 = a.y + (dy / len) * nodeR;
      var x2 = b.x - (dx / len) * nodeR,
        y2 = b.y - (dy / len) * nodeR;

      // 두께=observed_count(1..6px), 색=success/observed 비율.
      var oc = num(t.observed_count);
      var thickness = Math.max(1.2, Math.min(6, 1 + oc * 0.8));
      var ratio = oc > 0 ? num(t.success_count) / oc : 0;
      var onPath = pathEdges[t.from_state_id + "|" + t.to_state_id];
      var color, marker;
      if (onPath) {
        color = cssVar("--signal");
        marker = "url(#arrow-signal)";
      } else if (oc > 0 && ratio < 0.5) {
        // 성공률 낮은 전이(관측 대비 성공 절반 미만)는 warn 으로 표시.
        color = cssVar("--warn");
        marker = "url(#arrow-warn)";
      } else {
        color = cssVar("--line");
        marker = "url(#arrow)";
      }
      var line = svgEl("line", {
        x1: x1,
        y1: y1,
        x2: x2,
        y2: y2,
        stroke: color,
        "stroke-width": onPath ? thickness + 1 : thickness,
        "marker-end": marker,
        class: "edge" + (onPath ? " edge-signal" : ""),
      });
      edgeLayer.appendChild(line);

      if (mapView.showLabels) {
        var lbl = svgEl("text", {
          x: (x1 + x2) / 2,
          y: (y1 + y2) / 2 - 4,
          "text-anchor": "middle",
          class: "edge-label",
        });
        lbl.textContent = token;
        edgeLayer.appendChild(lbl);
      }
    });
    svg.appendChild(edgeLayer);

    // 노드
    var nodeLayer = svgEl("g", { class: "node-layer" });
    var animate = !REDUCED_MOTION;
    visible.forEach(function (s, idx) {
      var p = pos[s.id];
      if (!p) return;
      var isRoot = s.id === map.root_state_id;
      var low = num(s.confidence) < LOW_CONF;
      var kindColor = cssVar(KIND_COLOR[s.kind] || "--line");

      var g = svgEl("g", { class: "map-node" + (animate ? " node-enter" : ""), tabindex: "0", role: "button" });
      g.setAttribute("aria-label", (s.label || s.kind || s.id) + " 상태");

      // 저신뢰 warn 링
      if (low) {
        g.appendChild(
          svgEl("circle", {
            cx: p.x,
            cy: p.y,
            r: nodeR + 4,
            fill: "none",
            stroke: cssVar("--warn"),
            "stroke-width": "1.5",
            "stroke-dasharray": "3 3",
          })
        );
      }
      // 본체
      g.appendChild(
        svgEl("circle", {
          cx: p.x,
          cy: p.y,
          r: nodeR,
          fill: cssVar("--panel-2"),
          stroke: isRoot ? cssVar("--signal") : kindColor,
          "stroke-width": isRoot ? "2.4" : "1.6",
        })
      );
      // StateKind 색 힌트 칩(상단 작은 점)
      g.appendChild(
        svgEl("circle", { cx: p.x, cy: p.y - nodeR - 6, r: 4, fill: kindColor })
      );
      // 신뢰도 막대(노드 하단 4px)
      var barW = 34,
        conf = Math.max(0, Math.min(1, num(s.confidence)));
      g.appendChild(
        svgEl("rect", {
          x: p.x - barW / 2,
          y: p.y + nodeR + 4,
          width: barW,
          height: 4,
          rx: 2,
          fill: cssVar("--line"),
        })
      );
      g.appendChild(
        svgEl("rect", {
          x: p.x - barW / 2,
          y: p.y + nodeR + 4,
          width: barW * conf,
          height: 4,
          rx: 2,
          fill: low ? cssVar("--warn") : cssVar("--signal"),
        })
      );
      // visit_count 배지
      if (num(s.visit_count) > 0) {
        g.appendChild(svgEl("circle", { cx: p.x + nodeR - 2, cy: p.y - nodeR + 2, r: 9, fill: cssVar("--signal-soft"), stroke: cssVar("--signal"), "stroke-width": "1" }));
        var vt = svgEl("text", { x: p.x + nodeR - 2, y: p.y - nodeR + 2, "text-anchor": "middle", "dominant-baseline": "central", class: "visit-badge" });
        vt.textContent = String(s.visit_count);
        g.appendChild(vt);
      }
      // 라벨
      if (mapView.showLabels) {
        var t = svgEl("text", {
          x: p.x,
          y: p.y + nodeR + 22,
          "text-anchor": "middle",
          class: "node-label" + (isRoot ? " node-label-root" : ""),
        });
        t.textContent = s.label || KIND_LABEL[s.kind] || s.kind || shortId(s.id);
        g.appendChild(t);
      }

      function showPopover() {
        showNodePopover(s, p);
      }
      g.addEventListener("click", showPopover);
      g.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          showPopover();
        }
      });
      if (animate) g.style.animationDelay = Math.min(idx * 12, 240) + "ms";
      nodeLayer.appendChild(g);
    });
    svg.appendChild(nodeLayer);
    wrap.appendChild(svg);
  }

  function showNodePopover(s, p) {
    var pop = getEl("map-popover", "div", document.getElementById("map-canvas"));
    pop.hidden = false;
    pop.innerHTML = "";
    pop.appendChild(
      el("div", { class: "pop-head" }, [
        el("span", { class: "pill pill-line", text: KIND_LABEL[s.kind] || s.kind }),
        el("span", { class: "pop-title", text: s.label || KIND_LABEL[s.kind] || "(무명)" }),
        el("button", { type: "button", class: "pop-close", text: "✕", onclick: function () { pop.hidden = true; } }),
      ])
    );
    function kv(k, v) {
      return el("div", { class: "kv" }, [
        el("span", { class: "kv-k label-cap", text: k }),
        el("span", { class: "kv-v mono", text: v == null || v === "" ? "—" : String(v) }),
      ]);
    }
    pop.appendChild(kv("ID", s.id));
    pop.appendChild(kv("SIGNATURE", s.signature));
    pop.appendChild(kv("APP", s.app_id));
    pop.appendChild(kv("CONFIDENCE", num(s.confidence).toFixed(2)));
    pop.appendChild(kv("VISITS", s.visit_count));
    pop.appendChild(kv("FIRST SEEN", s.first_seen));
    pop.appendChild(kv("LAST SEEN", s.last_seen));
    // path 미리보기 채우기 편의: from/to 인풋이 있으면 to 를 이 노드로.
    var toInput = document.getElementById("path-to");
    if (toInput) toInput.value = s.id;
  }

  // ---------------------------------------------------------------------- //
  // 11. API 오케스트레이션
  // ---------------------------------------------------------------------- //
  function refreshHealth() {
    return api("GET", "/health")
      .then(function (h) {
        store.health = h;
        renderStatusbar();
      })
      .catch(function (e) {
        store.health = { driver: { ready: false, name: "?" }, sense: {} };
        renderStatusbar();
        toast("상태 조회 실패: " + e.message, "crit");
      });
  }

  function refreshMap() {
    return api("GET", "/map")
      .then(function (m) {
        store.map = m;
        renderStatusbar();
        renderMap();
      })
      .catch(function (e) {
        toast("맵 조회 실패: " + e.message, "crit");
      });
  }

  function setLearning(on) {
    store.learning = on;
    var btn = document.getElementById("btn-learn");
    if (btn) {
      btn.disabled = on;
      btn.classList.toggle("is-loading", on);
      btn.textContent = on ? "학습 중…" : "학습 시작";
    }
    renderStatusbar();
  }

  function runLearn() {
    var stepsEl = document.getElementById("learn-steps");
    var covEl = document.getElementById("learn-cov");
    var steps = stepsEl ? parseInt(stepsEl.value, 10) : 200;
    if (isNaN(steps)) steps = 200;
    var cov = covEl ? parseFloat(covEl.value) : 0.9;
    if (isNaN(cov)) cov = 0.9;
    cov = Math.max(0, Math.min(1, cov));

    setLearning(true);
    return api("POST", "/learn", { step_budget: steps, coverage_target: cov })
      .then(function (summary) {
        store.lastSummary = summary;
        // 관측 로그에 요약 파생 이벤트 1건(집계 원장).
        pushObs({
          t: nowClock(),
          kind: "LEARN",
          seq: summary.steps_taken,
          from: null,
          key: null,
          to: summary.states_visited + "개 상태",
          signature: "세션 " + shortId(summary.session_id) + " · " + (summary.stop_reason || ""),
          confidence: summary.coverage_ratio,
        });
        renderLearnSummary();
        renderObsLog();
        toast(
          "학습 완료 · 상태 " +
            num(summary.states_visited) +
            " · 전이 " +
            num(summary.transitions_recorded) +
            " · 커버리지 " +
            pct(summary.coverage_ratio),
          "good"
        );
        return refreshMap();
      })
      .catch(function (e) {
        toast("학습 실패: " + e.message, "crit");
      })
      .then(function () {
        setLearning(false);
      });
  }

  function runGoal(text) {
    var input = document.getElementById("goal-text");
    var goalText = (text != null ? text : input ? input.value : "").trim();
    if (!goalText) {
      toast("목표 문장을 입력하세요.", "warn");
      return Promise.resolve();
    }
    var btn = document.getElementById("btn-goal");
    if (btn) {
      btn.disabled = true;
      btn.classList.add("is-loading");
    }
    return api("POST", "/goal", { text: goalText })
      .then(function (result) {
        store.lastExecution = result;
        // 하이라이트 경로: 스텝의 from/actual(또는 expected) 상태 열.
        var path = [];
        (result.steps || []).forEach(function (st, i) {
          if (i === 0 && st.from_state_id) path.push(st.from_state_id);
          var to = st.actual_to_state_id || st.expected_to_state_id;
          if (to) path.push(to);
        });
        store.currentPath = path.length > 1 ? path : null;
        // 관측 로그 파생: 실행된 스텝을 EXEC 이벤트로.
        (result.steps || []).forEach(function (st) {
          if (!st.executed) return;
          pushObs({
            t: nowClock(),
            kind: "EXEC",
            seq: st.index,
            from: st.from_state_id,
            key: st.key ? st.key.token || keyTokenString(st.key) : "",
            to: st.actual_to_state_id,
            signature: st.matched === false ? "MISMATCH(expected " + shortId(st.expected_to_state_id) + ")" : "matched",
            confidence: st.observed_confidence,
          });
        });
        renderGoalResult();
        renderExecTrace();
        renderObsLog();
        var info = STATUS_INFO[result.status] || { cls: "warn", text: result.status };
        toast(
          "목표 실행: " +
            info.text +
            " · 키열 " +
            (result.button_sequence || []).join(" → ") +
            (num(result.replans) ? " · 재계획 " + result.replans : ""),
          info.cls
        );
        return refreshMap();
      })
      .catch(function (e) {
        toast("목표 실행 실패: " + e.message, "crit");
      })
      .then(function () {
        if (btn) {
          btn.disabled = false;
          btn.classList.remove("is-loading");
        }
      });
  }

  // 경로만 보기: goal-text 는 안 쓰고 path-from/path-to 또는 (미학습 시) 안내.
  function previewPath() {
    var fromEl = document.getElementById("path-from");
    var toEl = document.getElementById("path-to");
    var map = store.map || {};
    var from = fromEl ? fromEl.value.trim() : "";
    var to = toEl ? toEl.value.trim() : "";
    if (!from && map.root_state_id) from = map.root_state_id;
    if (!from || !to) {
      toast("경로 미리보기: from/to 상태 id 가 필요합니다(맵 노드를 클릭해 to 를 채울 수 있습니다).", "warn");
      return Promise.resolve();
    }
    return api(
      "GET",
      "/map/path?from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to)
    )
      .then(function (r) {
        if (!r.reachable) {
          store.currentPath = null;
          renderMap();
          toast("경로 없음: " + (r.message || "도달 불가"), "warn");
          return;
        }
        var steps = r.steps || [];
        var path = [];
        steps.forEach(function (st, i) {
          if (i === 0 && st.from_state_id) path.push(st.from_state_id);
          if (st.expected_to_state_id) path.push(st.expected_to_state_id);
        });
        if (!path.length && from) path.push(from);
        store.currentPath = path.length > 1 ? path : null;
        renderMap();
        var seq = steps
          .map(function (st) {
            return st.key ? st.key.token || keyTokenString(st.key) : "";
          })
          .join(" → ");
        toast(
          steps.length ? "경로 " + steps.length + "홉 · " + seq : "이미 목표 상태입니다.",
          "good"
        );
      })
      .catch(function (e) {
        toast("경로 조회 실패: " + e.message, "crit");
      });
  }

  // ---------------------------------------------------------------------- //
  // 12. 이벤트 배선 + 초기 부팅
  // ---------------------------------------------------------------------- //
  function bind() {
    ensureLearnPanel();
    ensureGoalPanel();
    ensureMapPanel();

    var bl = document.getElementById("btn-learn");
    if (bl) bl.addEventListener("click", function () { runLearn(); });

    var bg = document.getElementById("btn-goal");
    if (bg) bg.addEventListener("click", function () { runGoal(); });

    var bp = document.getElementById("btn-path-preview");
    if (bp) bp.addEventListener("click", function () { previewPath(); });

    var gt = document.getElementById("goal-text");
    if (gt)
      gt.addEventListener("keydown", function (e) {
        if (e.key === "Enter") runGoal();
      });

    // 빠른 칩(위임)
    var chips = document.getElementById("goal-chips");
    if (chips)
      chips.addEventListener("click", function (e) {
        var b = e.target.closest ? e.target.closest("[data-goal]") : null;
        if (b) {
          var t = b.getAttribute("data-goal");
          if (gt) gt.value = t;
          runGoal(t);
        }
      });

    // 별도 경로 조회 패널(index.html 이 제공하면 재사용)
    var bpath = document.getElementById("btn-path");
    if (bpath) bpath.addEventListener("click", function () { previewPath(); });
  }

  function boot() {
    // 초기 요약/결과/트레이스/로그 엠프티 스테이트 그림.
    renderStatusbar();
    renderLearnSummary();
    renderGoalResult();
    renderExecTrace();
    renderObsLog();
    // 데이터 로드(독립 호출 병렬).
    refreshHealth();
    refreshMap();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      bind();
      boot();
    });
  } else {
    bind();
    boot();
  }

  // 진단/테스트를 위한 최소 노출(전역 오염 최소화).
  window.remotectl = {
    refreshMap: refreshMap,
    refreshHealth: refreshHealth,
    runLearn: runLearn,
    runGoal: runGoal,
    store: store,
  };
})();

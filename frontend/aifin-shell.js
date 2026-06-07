(function () {
  "use strict";

  var routeToView = {
    "/": "home",
    "/quantflow": "home",
    "/quantflow/": "home",
    "/factor-lab": "factor-lab",
    "/my-strategy": "my-strategy",
    "/strategy": "strategy",
    "/charts": "strategy",
    "/backtest": "backtest",
    "/knowledge": "knowledge",
    "/editor": "editor",
    "/workspace": "editor",
    "/analyst": "analyst"
  };

  var viewToRoute = {
    home: "/quantflow/",
    "factor-lab": "/quantflow/factor-lab",
    "my-strategy": "/quantflow/my-strategy",
    strategy: "/quantflow/strategy",
    backtest: "/quantflow/backtest",
    knowledge: "/quantflow/knowledge",
    editor: "/quantflow/editor",
    analyst: "/quantflow/analyst"
  };

  var shell = document.getElementById("aifin-shell");
  var views = Array.prototype.slice.call(document.querySelectorAll("[data-view]"));
  var routeButtons = Array.prototype.slice.call(document.querySelectorAll("button[data-route]"));
  var isRouting = false;
  var reduceMotionQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  /* ─── Typewriter ─── */
  var typewriterEl = document.getElementById("typewriterText");
  var typewriterPrompts = [
    "对比价值因子与成长因子的IC衰减曲线...",
    "筛选流动性充足的中小盘量价因子...",
    "构建多因子Alpha模型并回测近三年表现...",
    "评估因子拥挤度对策略容量的影响..."
  ];
  var twIndex = 0;
  var twCharIndex = 0;
  var twIsDeleting = false;
  var twTimer = null;

  function typewriterTick() {
    if (!typewriterEl) return;

    var currentPrompt = typewriterPrompts[twIndex];

    if (!twIsDeleting) {
      twCharIndex++;
      typewriterEl.textContent = currentPrompt.slice(0, twCharIndex);

      if (twCharIndex >= currentPrompt.length) {
        twTimer = setTimeout(function () {
          twIsDeleting = true;
          typewriterTick();
        }, 2200);
        return;
      }
      twTimer = setTimeout(typewriterTick, 60);
    } else {
      twCharIndex--;
      typewriterEl.textContent = currentPrompt.slice(0, twCharIndex);

      if (twCharIndex <= 0) {
        twIsDeleting = false;
        twIndex = (twIndex + 1) % typewriterPrompts.length;
        twTimer = setTimeout(typewriterTick, 500);
        return;
      }
      twTimer = setTimeout(typewriterTick, 30);
    }
  }

  function startTypewriter() {
    if (reduceMotionQuery && reduceMotionQuery.matches) {
      if (typewriterEl) {
        typewriterEl.textContent = typewriterPrompts[0];
      }
      return;
    }

    if (twTimer) return;
    twCharIndex = 0;
    twIsDeleting = false;
    typewriterTick();
  }

  function stopTypewriter() {
    if (twTimer) {
      clearTimeout(twTimer);
      twTimer = null;
    }
  }

  /* ─── Routing ─── */
  function normalizeRoute(pathname) {
    if (routeToView[pathname]) {
      return pathname;
    }

    var trimmed = pathname.replace(/\/+$/, "");
    if (routeToView[trimmed]) {
      return trimmed;
    }

    if (trimmed.indexOf("/quantflow") === 0) {
      var inner = trimmed.replace(/^\/quantflow/, "") || "/";
      if (routeToView[inner]) {
        return inner;
      }
    }

    return "/";
  }

  function setActiveRoute(route, shouldPush) {
    var normalized = normalizeRoute(route);
    var viewName = routeToView[normalized] || "home";

    views.forEach(function (view) {
      view.classList.toggle("is-active", view.getAttribute("data-view") === viewName);
    });

    routeButtons.forEach(function (button) {
      var target = normalizeRoute(button.getAttribute("data-route") || "/");
      var targetView = routeToView[target];
      var isActive = targetView === viewName && viewName !== "home";
      // Keep the strategy parent active for strategy subpages.
      if ((viewName === "backtest" || viewName === "my-strategy") && targetView === "strategy") {
        isActive = true;
      }
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-current", isActive ? "page" : "false");
    });

    lazyLoadIframe(viewName);

    if (shouldPush && window.history && window.history.pushState) {
      var nextRoute = viewToRoute[viewName] || "/";
      window.history.pushState({ view: viewName }, "", nextRoute);
    }

    if (shell) {
      shell.setAttribute("data-active-view", viewName);
    }

    if (viewName === "home") {
      startTypewriter();
    } else {
      stopTypewriter();
    }

    if (viewName === "backtest") {
      renderBacktestTable();
    }

    if (viewName === "factor-lab") {
      renderFactorLab();
    }

    if (viewName === "analyst") {
      renderTradingAgentProcess();
    }
  }

  function getViewForRoute(route) {
    return routeToView[normalizeRoute(route)] || "home";
  }

  function getActiveView() {
    if (shell && shell.getAttribute("data-active-view")) {
      return shell.getAttribute("data-active-view");
    }

    var active = document.querySelector(".view.is-active");
    return active ? active.getAttribute("data-view") : "home";
  }

  function lazyLoadIframe(viewName) {
    var placeholder = "<!doctype html><html><head><meta charset=\"utf-8\"><style>html,body{margin:0;width:100%;height:100%;background:#fafafa;}</style></head><body></body></html>";
    var frames = Array.prototype.slice.call(document.querySelectorAll("iframe[data-src]"));

    frames.forEach(function (frame) {
      var view = frame.closest("[data-view]");
      var isCurrent = view && view.getAttribute("data-view") === viewName;
      var dataSrc = frame.getAttribute("data-src");

      if (isCurrent && dataSrc) {
        frame.removeAttribute("srcdoc");
        if (frame.getAttribute("src") !== dataSrc) {
          frame.setAttribute("src", dataSrc);
        }
        return;
      }

      if (frame.getAttribute("src")) {
        frame.removeAttribute("src");
      }
      frame.setAttribute("srcdoc", placeholder);
    });
  }

  /* ─── Event Listeners ─── */
  routeButtons.forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.preventDefault();
      navigateWithTransition(button.getAttribute("data-route") || "/");
    });
  });

  window.addEventListener("popstate", function () {
    setActiveRoute(window.location.pathname, false);
  });

  window.addEventListener("message", function (event) {
    if (event.origin !== window.location.origin) {
      return;
    }

    var data = event.data || {};
    if (data.type !== "aifin:navigate" || typeof data.route !== "string") {
      return;
    }

    if (!routeToView[normalizeRoute(data.route)]) {
      return;
    }

    navigateWithTransition(data.route);
  });

  function navigateWithTransition(route) {
    var nextView = getViewForRoute(route);

    if (isRouting || nextView === getActiveView()) {
      setActiveRoute(route, true);
      return;
    }

    var reduceMotion = reduceMotionQuery && reduceMotionQuery.matches;
    if (!shell || reduceMotion) {
      setActiveRoute(route, true);
      return;
    }

    isRouting = true;

    var scanline = document.createElement("div");
    scanline.className = "route-scanline";
    scanline.setAttribute("aria-hidden", "true");
    shell.appendChild(scanline);

    window.setTimeout(function () {
      setActiveRoute(route, true);
    }, 120);

    window.setTimeout(function () {
      scanline.remove();
      isRouting = false;
    }, 400);
  }

  /* ─── Dropdown Nav ─── */
  var dropdowns = Array.prototype.slice.call(document.querySelectorAll(".nav-dropdown"));
  dropdowns.forEach(function (dropdown) {
    var menuButtons = Array.prototype.slice.call(dropdown.querySelectorAll(".nav-dropdown-menu button[data-route]"));
    menuButtons.forEach(function (btn) {
      btn.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        navigateWithTransition(btn.getAttribute("data-route") || "/");
      });
    });
  });

  /* ─── Backtest Mock Data ─── */
  var backtestMockData = [
    { name: "小市值策略", tag: "Code", time: "2026-05-26 18:43:07", runs: 0, backtests: 2 },
    { name: "双均线策略", tag: "Code", time: "2026-05-25 18:47:27", runs: 0, backtests: 0 },
    { name: "银行股轮动策略", tag: "Code", time: "2026-05-25 18:47:27", runs: 0, backtests: 0 },
    { name: "低估价值选股策略", tag: "Code", time: "2026-05-25 18:47:27", runs: 0, backtests: 0 },
    { name: "Dual_Thrust策略—股指期货", tag: "Code", time: "2026-05-25 18:47:27", runs: 0, backtests: 0 }
  ];

  var backtestPageSize = 10;
  var backtestRendered = false;

  function renderBacktestPagination() {
    var pagination = document.getElementById("backtestPagination");
    if (!pagination) return;

    var total = backtestMockData.length;
    var totalPages = Math.max(1, Math.ceil(total / backtestPageSize));
    var currentPage = 1;

    pagination.innerHTML =
      '<div class="pagination-state">' +
      '<button type="button" disabled aria-label="上一页">&#8249;</button>' +
      '<span>第 ' + currentPage + ' 页</span>' +
      '<span class="pagination-divider"></span>' +
      '<span>共 ' + totalPages + ' 页</span>' +
      '<button type="button" disabled aria-label="下一页">&#8250;</button>' +
      '</div>' +
      '<span class="pagination-summary">每页 ' + backtestPageSize + ' 行 · 共 ' + total + ' 条</span>';
  }

  function renderBacktestTable() {
    if (backtestRendered) return;
    var tbody = document.getElementById("backtestTableBody");
    if (!tbody) return;

    var html = "";
    backtestMockData.forEach(function (item) {
      html += '<tr>';
      html += '<td class="col-check"><input type="checkbox"></td>';
      html += '<td class="col-name"><span class="file-icon" aria-hidden="true"></span>' + item.name + '</td>';
      html += '<td class="col-tag"><span class="tag-code">' + item.tag + '</span></td>';
      html += '<td class="col-time">' + item.time + '</td>';
      html += '<td class="col-run">' + item.runs + '</td>';
      html += '<td class="col-bt">' + (item.backtests > 0 ? '<a href="#">' + item.backtests + '</a>' : '0') + '</td>';
      html += '</tr>';
    });
    tbody.innerHTML = html;
    renderBacktestPagination();
    backtestRendered = true;
  }

  /* ─── Factor Lab Task Matrix ─── */
  var factorLabState = {
    page: 1,
    pageSize: 10,
    selectedTaskKey: "3Y|daily|T+5",
    selectedSchemeId: "",
    rankMetric: "overall",
    startMonth: "2025-01",
    endMonth: "2025-05",
    chartMetrics: {
      overall: true,
      upPrecision: true,
      upRecall: true,
      downPrecision: true,
      downRecall: true
    }
  };

  var factorTargets = ["3Y", "5Y", "7Y", "10Y"];
  var factorTargetLabels = {
    "3Y": "3Y国债活跃",
    "5Y": "5Y国债活跃",
    "7Y": "7Y国债活跃",
    "10Y": "10Y国债活跃"
  };
  var factorTaskColumns = [
    { id: "dailyT1", label: "T+1", frequency: "daily", horizon: "T+1" },
    { id: "dailyT5", label: "T+5", frequency: "daily", horizon: "T+5" },
    { id: "weekly", label: "周度", frequency: "weekly", horizon: "NEXT_MONDAY" }
  ];
  var factorStatusLabels = {
    active: "运行中",
    paused: "暂停",
    running: "运行中",
    complete: "已完成",
    archived: "已归档"
  };
  var factorTrendMetrics = [
    { id: "overall", label: "整体准确率", color: "#15623f" },
    { id: "upPrecision", label: "上涨准确率", color: "#2f7ba1" },
    { id: "upRecall", label: "上涨召回率", color: "#b98728" },
    { id: "downPrecision", label: "下跌准确率", color: "#d62828" },
    { id: "downRecall", label: "下跌召回率", color: "#6f5aa8" }
  ];
  var factorSchemeNamePool = [
    "F-v22 term-micro × MTL-v07",
    "F-v22 term-micro × LGBM-0526",
    "F-v21 momentum × XGB-v18",
    "F-v20 macro-lite × Ridge-ens",
    "F-v23 liquidity × CatBoost-v03",
    "F-v19 carry-slope × RF-v11"
  ];

  var factorDailyBaseRows = [
    { month: "2025-01", samples: 18, actualDist: "10/6/2", predictedDist: "13/5/0", overall: 38.9, correct: 7, upPrecision: 46.2, upRecall: 60.0, downPrecision: 20.0, downRecall: 16.7 },
    { month: "2025-02", samples: 18, actualDist: "14/4/0", predictedDist: "13/5/0", overall: 50.0, correct: 9, upPrecision: 69.2, upRecall: 64.3, downPrecision: 0.0, downRecall: 0.0 },
    { month: "2025-03", samples: 21, actualDist: "8/13/0", predictedDist: "5/16/0", overall: 76.2, correct: 16, upPrecision: 80.0, upRecall: 50.0, downPrecision: 75.0, downRecall: 92.3 },
    { month: "2025-04", samples: 21, actualDist: "10/11/0", predictedDist: "11/10/0", overall: 85.7, correct: 18, upPrecision: 81.8, upRecall: 90.0, downPrecision: 90.0, downRecall: 81.8 },
    { month: "2025-05", samples: 19, actualDist: "10/8/1", predictedDist: "11/8/0", overall: 73.7, correct: 14, upPrecision: 72.7, upRecall: 80.0, downPrecision: 75.0, downRecall: 75.0 }
  ];
  var factorWeeklyBaseRows = [
    { month: "2025-01", samples: 4, actualDist: "2/2/0", predictedDist: "3/1/0", overall: 50.0, correct: 2, upPrecision: 66.7, upRecall: 100.0, downPrecision: 0.0, downRecall: 0.0 },
    { month: "2025-02", samples: 4, actualDist: "3/1/0", predictedDist: "2/2/0", overall: 75.0, correct: 3, upPrecision: 100.0, upRecall: 66.7, downPrecision: 50.0, downRecall: 100.0 },
    { month: "2025-03", samples: 5, actualDist: "2/3/0", predictedDist: "2/3/0", overall: 80.0, correct: 4, upPrecision: 100.0, upRecall: 100.0, downPrecision: 66.7, downRecall: 66.7 },
    { month: "2025-04", samples: 4, actualDist: "2/2/0", predictedDist: "2/2/0", overall: 75.0, correct: 3, upPrecision: 100.0, upRecall: 100.0, downPrecision: 50.0, downRecall: 50.0 },
    { month: "2025-05", samples: 4, actualDist: "2/1/1", predictedDist: "2/2/0", overall: 50.0, correct: 2, upPrecision: 50.0, upRecall: 50.0, downPrecision: 50.0, downRecall: 100.0 }
  ];
  var factorDailyRows = [
    { day: "05/06", predicted: "涨", actual: "涨", correct: true },
    { day: "05/07", predicted: "跌", actual: "跌", correct: true },
    { day: "05/08", predicted: "涨", actual: "跌", correct: false },
    { day: "05/11", predicted: "跌", actual: "跌", correct: true },
    { day: "05/12", predicted: "涨", actual: "涨", correct: true },
    { day: "05/13", predicted: "涨", actual: "涨", correct: true },
    { day: "05/14", predicted: "跌", actual: "涨", correct: false },
    { day: "05/15", predicted: "涨", actual: "涨", correct: true },
    { day: "05/18", predicted: "涨", actual: "涨", correct: true },
    { day: "05/19", predicted: "涨", actual: "涨", correct: true },
    { day: "05/20", predicted: "跌", actual: "涨", correct: false },
    { day: "05/21", predicted: "跌", actual: "跌", correct: true },
    { day: "05/22", predicted: "涨", actual: "涨", correct: true },
    { day: "05/25", predicted: "涨", actual: "涨", correct: true },
    { day: "05/26", predicted: "涨", actual: "跌", correct: false },
    { day: "05/27", predicted: "跌", actual: "跌", correct: true },
    { day: "05/28", predicted: "涨", actual: "涨", correct: true },
    { day: "05/29", predicted: "待验证", actual: "待验证", correct: null }
  ];
  var factorWeeklyRows = [
    { day: "05/05", predicted: "涨", actual: "涨", correct: true },
    { day: "05/12", predicted: "跌", actual: "涨", correct: false },
    { day: "05/19", predicted: "涨", actual: "涨", correct: true },
    { day: "05/26", predicted: "跌", actual: "跌", correct: true }
  ];
  var factorLabBound = false;
  var factorLabRemoteLoaded = false;
  var factorLabRemoteLoading = false;
  var factorLabRefreshTimer = null;
  var factorLabApiError = "";
  var factorLabDataMode = "mock";

  function clampPercent(value) {
    return Math.max(0, Math.min(100, Number(value) || 0));
  }

  function formatPercent(value) {
    if (value === null || value === undefined || value === "") return "--";
    var numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    return numeric.toFixed(1) + "%";
  }

  function getMetricClass(value) {
    if (value === null || value === undefined || value === "") return "";
    if (!Number.isFinite(Number(value))) return "";
    if (value >= 62) return "metric-good";
    if (value >= 55) return "metric-warn";
    return "metric-bad";
  }

  function getMetricLabel(metricId) {
    var metric = factorTrendMetrics.filter(function (item) {
      return item.id === metricId;
    })[0];
    return metric ? metric.label : "整体准确率";
  }

  function getTaskKey(target, column) {
    return target + "|" + column.frequency + "|" + column.horizon;
  }

  function getTargetDisplayName(target) {
    return factorTargetLabels[target] || target;
  }

  function mergeTargetLabels(labels) {
    Object.keys(labels || {}).forEach(function (target) {
      if (labels[target]) factorTargetLabels[target] = String(labels[target]);
    });
  }

  function getTaskByKey(taskKey) {
    var parts = String(taskKey || "").split("|");
    var column = factorTaskColumns.filter(function (item) {
      return item.frequency === parts[1] && item.horizon === parts[2];
    })[0] || factorTaskColumns[0];
    return {
      key: taskKey,
      target: parts[0] || "3Y",
      targetLabel: getTargetDisplayName(parts[0] || "3Y"),
      frequency: column.frequency,
      horizon: column.horizon,
      label: getTargetDisplayName(parts[0] || "3Y") + " · " + column.label,
      columnLabel: column.label
    };
  }

  function makeMonthRows(baseRows, offset) {
    return baseRows.map(function (row, index) {
      var wave = (index % 2 === 0 ? 1 : -1) * 1.4;
      var overall = clampPercent(row.overall + offset + wave);
      var samples = Number(row.samples) || 0;
      var actualCounts = normalizeDist(row.actualDist);
      var predictedCounts = normalizeDist(row.predictedDist);
      return {
        month: row.month,
        samples: samples,
        actualDist: row.actualDist,
        predictedDist: row.predictedDist,
        actualCounts: actualCounts,
        predictedCounts: predictedCounts,
        overall: overall,
        correct: Math.max(0, Math.min(samples, Math.round(samples * overall / 100))),
        upPrecision: clampPercent(row.upPrecision + offset * 0.8 + wave),
        upRecall: clampPercent(row.upRecall + offset * 0.6 - wave),
        downPrecision: clampPercent(row.downPrecision + offset * 0.7 + wave),
        downRecall: clampPercent(row.downRecall + offset * 0.5 - wave)
      };
    });
  }

  function createTaskSchemes(target, column, count, targetIndex, columnIndex) {
    var schemes = [];
    var baseRows = column.frequency === "weekly" ? factorWeeklyBaseRows : factorDailyBaseRows;
    var baseline = targetIndex * 2.5 + (column.id === "dailyT5" ? 4.2 : (column.id === "weekly" ? 2.8 : 0));
    for (var i = 0; i < count; i++) {
      var name = factorSchemeNamePool[i % factorSchemeNamePool.length];
      var status = i === 0 ? "running" : (i === count - 1 && count > 3 ? "archived" : "complete");
      var qualityStep = status === "archived" ? 0.4 : i * 1.9;
      schemes.push({
        id: target.toLowerCase() + "-" + column.id + "-s" + (i + 1),
        taskKey: getTaskKey(target, column),
        name: name,
        status: status,
        latestRun: status === "archived" ? "05-21" : "05-29",
        monthlyRows: makeMonthRows(baseRows, baseline + qualityStep - (count - 3) * 0.7)
      });
    }
    return schemes;
  }

  var factorTaskSchemes = {};
  factorTargets.forEach(function (target, targetIndex) {
    factorTaskColumns.forEach(function (column, columnIndex) {
      var count = 3 + ((targetIndex + columnIndex) % 3);
      if (target === "3Y" && column.id === "dailyT5") count = 5;
      factorTaskSchemes[getTaskKey(target, column)] = createTaskSchemes(target, column, count, targetIndex, columnIndex);
    });
  });

  function initEmptyTaskSchemes() {
    var result = {};
    factorTargets.forEach(function (target) {
      factorTaskColumns.forEach(function (column) {
        result[getTaskKey(target, column)] = [];
      });
    });
    return result;
  }

  function directionText(value) {
    if (value === 1) return "涨";
    if (value === -1) return "跌";
    if (value === 0) return "平";
    return "待验证";
  }

  function normalizeDirection(value) {
    var numeric = Number(value);
    return numeric === 1 || numeric === -1 || numeric === 0 ? numeric : null;
  }

  function normalizeDist(dist) {
    if (typeof dist === "string") {
      var parts = dist.split("/").map(function (item) {
        return Number(item) || 0;
      });
      return { up: parts[0] || 0, down: parts[1] || 0, flat: parts[2] || 0 };
    }
    dist = dist || {};
    return {
      up: Number(dist.up || 0),
      down: Number(dist.down || 0),
      flat: Number(dist.flat || 0)
    };
  }

  function distText(dist) {
    dist = normalizeDist(dist);
    return [dist.up || 0, dist.down || 0, dist.flat || 0].join("/");
  }

  function metricOrNull(value) {
    return value === null || value === undefined ? null : Number(value);
  }

  function rowFromMetric(metric) {
    var overall = metric.accuracy === null || metric.accuracy === undefined ? metric.overall : metric.accuracy;
    var actualCounts = normalizeDist(metric.actual_dist);
    var predictedCounts = normalizeDist(metric.predicted_dist);
    return {
      month: metric.month,
      samples: Number(metric.total || metric.samples || 0),
      actualDist: distText(actualCounts),
      predictedDist: distText(predictedCounts),
      actualCounts: actualCounts,
      predictedCounts: predictedCounts,
      overall: metricOrNull(overall),
      correct: Number(metric.correct || 0),
      upPrecision: metricOrNull(metric.up_precision),
      upRecall: metricOrNull(metric.up_recall),
      downPrecision: metricOrNull(metric.down_precision),
      downRecall: metricOrNull(metric.down_recall)
    };
  }

  function dailyRowsByMonth(rows) {
    var grouped = {};
    (rows || []).forEach(function (row) {
      var month = String(row.predict_date || row.target_date || "").slice(0, 7);
      if (!month) return;
      if (!grouped[month]) grouped[month] = [];
      var predictedDirection = normalizeDirection(row.predicted_direction);
      var actualDirection = normalizeDirection(row.actual_direction);
      grouped[month].push({
        day: String(row.predict_date || row.target_date).slice(5, 10).replace("-", "/"),
        predicted: directionText(predictedDirection),
        actual: directionText(actualDirection),
        predictedDirection: predictedDirection,
        actualDirection: actualDirection,
        correct: actualDirection === null || predictedDirection === null ? null : predictedDirection === actualDirection
      });
    });
    return grouped;
  }

  function appendPendingMonths(monthlyRows, groupedDailyRows) {
    var known = {};
    monthlyRows.forEach(function (row) {
      known[row.month] = true;
    });
    Object.keys(groupedDailyRows).sort().forEach(function (month) {
      if (known[month]) return;
      monthlyRows.push({
        month: month,
        samples: 0,
        actualDist: "0/0/0",
        predictedDist: "0/0/0",
        actualCounts: { up: 0, down: 0, flat: 0 },
        predictedCounts: { up: 0, down: 0, flat: 0 },
        overall: null,
        correct: 0,
        upPrecision: null,
        upRecall: null,
        downPrecision: null,
        downRecall: null
      });
    });
    monthlyRows.sort(function (a, b) {
      return a.month.localeCompare(b.month);
    });
    return monthlyRows;
  }

  function columnForHorizon(horizon, frequency) {
    if (
      String(frequency || "").toLowerCase() === "weekly" ||
      String(horizon) === "NEXT_MONDAY" ||
      Number(horizon) === 6
    ) {
      return factorTaskColumns[2];
    }
    if (Number(horizon) === 1) return factorTaskColumns[0];
    if (Number(horizon) === 5) return factorTaskColumns[1];
    return null;
  }

  function normalizeBackendSchemeStatus(status) {
    if (status === "active") return "active";
    if (status === "paused") return "paused";
    if (status === "archived") return "archived";
    return status || "complete";
  }

  function fetchJson(url) {
    return fetch(url, { cache: "no-store", headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) {
        throw new Error("HTTP " + response.status + " " + url);
      }
      return response.json();
    });
  }

  function finishFactorLabDataLoad(tasks, mode) {
    var isInitialLoad = !factorLabRemoteLoaded;
    factorTaskSchemes = tasks;
    factorLabRemoteLoaded = true;
    factorLabRemoteLoading = false;
    factorLabApiError = "";
    factorLabDataMode = mode;
    var availableTasks = Object.keys(factorTaskSchemes).filter(function (key) {
      return factorTaskSchemes[key].length > 0;
    });
    if (availableTasks.length && !factorTaskSchemes[factorLabState.selectedTaskKey]) {
      factorLabState.selectedTaskKey = availableTasks[0];
    }
    syncFactorMonthRange();
    var months = getFactorAvailableMonths();
    if (months.length && isInitialLoad) {
      factorLabState.endMonth = months[months.length - 1];
    }
    renderFactorLab();
  }

  function buildBacktestTaskSchemes(payload) {
    mergeTargetLabels(payload.target_labels);
    var tasks = initEmptyTaskSchemes();
    (payload.schemes || []).forEach(function (scheme) {
      var column = columnForHorizon(scheme.horizon, scheme.frequency);
      if (!column) return;
      if (scheme.target_label) factorTargetLabels[scheme.tenor] = String(scheme.target_label);
      var taskKey = getTaskKey(scheme.tenor, column);
      if (!tasks[taskKey]) tasks[taskKey] = [];
      var groupedDailyRows = dailyRowsByMonth(scheme.daily_rows || []);
      var monthlyRows = (scheme.monthly_metrics || []).map(rowFromMetric);
      monthlyRows = appendPendingMonths(monthlyRows, groupedDailyRows);
      var latestRun = scheme.latest_run && scheme.latest_run.date
        ? scheme.latest_run.date.slice(5)
        : (scheme.end_date ? scheme.end_date.slice(5) : "--");
      tasks[taskKey].push({
        id: scheme.id,
        taskKey: taskKey,
        name: scheme.name,
        status: normalizeBackendSchemeStatus(scheme.status),
        latestRun: latestRun,
        monthlyRows: monthlyRows,
        dailyRowsByMonth: groupedDailyRows
      });
    });
    return tasks;
  }

  function loadLiveFactorLabData() {
    return fetchJson("/api/schemes")
      .then(function (payload) {
        mergeTargetLabels(payload.target_labels);
        var schemes = payload.schemes || [];
        var tasks = initEmptyTaskSchemes();
        var requests = [];
        schemes.forEach(function (scheme) {
          mergeTargetLabels(scheme.target_labels);
          var column = columnForHorizon(scheme.horizon, scheme.frequency);
          if (!column) return;
          (scheme.tenors || []).forEach(function (tenor) {
            requests.push(
              fetchJson("/api/metrics/" + encodeURIComponent(scheme.scheme_id) + "?tenor=" + encodeURIComponent(tenor))
                .then(function (metrics) {
                  if (metrics.target_label) factorTargetLabels[tenor] = String(metrics.target_label);
                  var groupedDailyRows = dailyRowsByMonth(metrics.daily_rows || []);
                  var monthlyRows = (metrics.monthly_metrics || []).map(rowFromMetric);
                  monthlyRows = appendPendingMonths(monthlyRows, groupedDailyRows);
                  var taskKey = getTaskKey(tenor, column);
                  if (!tasks[taskKey]) tasks[taskKey] = [];
                  tasks[taskKey].push({
                    id: scheme.scheme_id,
                    taskKey: taskKey,
                    name: scheme.name,
                    status: normalizeBackendSchemeStatus(scheme.status),
                    latestRun: scheme.last_run ? scheme.last_run.date.slice(5) : "--",
                    monthlyRows: monthlyRows,
                    dailyRowsByMonth: groupedDailyRows
                  });
                })
            );
          });
        });
        return Promise.all(requests).then(function () {
          finishFactorLabDataLoad(tasks, "live");
        });
      });
  }

  function loadFactorLabData(options) {
    var force = options && options.force === true;
    if ((!force && factorLabRemoteLoaded) || factorLabRemoteLoading || !window.fetch) return;
    factorLabRemoteLoading = true;
    fetchJson("/api/backtests/factor-lab")
      .then(function (payload) {
        if (payload && payload.schemes && payload.schemes.length) {
          finishFactorLabDataLoad(buildBacktestTaskSchemes(payload), "backtest");
          return null;
        }
        return loadLiveFactorLabData();
      })
      .catch(function () {
        return loadLiveFactorLabData();
      })
      .catch(function (error) {
        factorLabApiError = error.message || "API unavailable";
        factorLabRemoteLoaded = true;
        factorLabRemoteLoading = false;
        factorLabDataMode = "mock";
        renderFactorLab();
      });
  }

  function startFactorLabAutoRefresh() {
    if (factorLabRefreshTimer || !window.setInterval) return;
    factorLabRefreshTimer = window.setInterval(function () {
      if (getActiveView() !== "factor-lab") return;
      loadFactorLabData({ force: true });
    }, 60000);
  }

  function getSchemesForTask(taskKey) {
    var schemes = factorTaskSchemes[taskKey] || [];
    return schemes.slice();
  }

  function getSelectedTaskSchemes() {
    return getSchemesForTask(factorLabState.selectedTaskKey);
  }

  function getSelectedScheme() {
    var allSchemes = factorTaskSchemes[factorLabState.selectedTaskKey] || [];
    return allSchemes.filter(function (scheme) {
      return scheme.id === factorLabState.selectedSchemeId;
    })[0] || null;
  }

  function getVisibleRowsForScheme(scheme) {
    if (!scheme) return [];
    return scheme.monthlyRows.filter(function (row) {
      return row.month >= factorLabState.startMonth && row.month <= factorLabState.endMonth;
    });
  }

  function getVisibleDailyRowsForScheme(scheme) {
    if (!scheme || !scheme.dailyRowsByMonth) return [];
    var rows = [];
    Object.keys(scheme.dailyRowsByMonth).forEach(function (month) {
      if (month < factorLabState.startMonth || month > factorLabState.endMonth) return;
      rows = rows.concat(scheme.dailyRowsByMonth[month] || []);
    });
    return rows.filter(function (row) {
      return normalizeDirection(row.predictedDirection) !== null && normalizeDirection(row.actualDirection) !== null;
    });
  }

  function metricFromSampleRows(rows) {
    var samples = rows.length;
    var correct = rows.reduce(function (sum, row) {
      return sum + (row.predictedDirection === row.actualDirection ? 1 : 0);
    }, 0);
    var predUp = rows.reduce(function (sum, row) { return sum + (row.predictedDirection === 1 ? 1 : 0); }, 0);
    var predDown = rows.reduce(function (sum, row) { return sum + (row.predictedDirection === -1 ? 1 : 0); }, 0);
    var actualUp = rows.reduce(function (sum, row) { return sum + (row.actualDirection === 1 ? 1 : 0); }, 0);
    var actualDown = rows.reduce(function (sum, row) { return sum + (row.actualDirection === -1 ? 1 : 0); }, 0);
    var upTp = rows.reduce(function (sum, row) {
      return sum + (row.predictedDirection === 1 && row.actualDirection === 1 ? 1 : 0);
    }, 0);
    var downTp = rows.reduce(function (sum, row) {
      return sum + (row.predictedDirection === -1 && row.actualDirection === -1 ? 1 : 0);
    }, 0);
    return {
      samples: samples,
      correct: correct,
      overall: samples ? correct / samples * 100 : null,
      upPrecision: predUp ? upTp / predUp * 100 : null,
      upRecall: actualUp ? upTp / actualUp * 100 : null,
      downPrecision: predDown ? downTp / predDown * 100 : null,
      downRecall: actualDown ? downTp / actualDown * 100 : null
    };
  }

  function inferTruePositive(primaryMetric, primaryDenominator, fallbackMetric, fallbackDenominator) {
    var primary = metricOrNull(primaryMetric);
    if (primary !== null && primaryDenominator > 0) {
      return Math.max(0, Math.min(primaryDenominator, Math.round(primary / 100 * primaryDenominator)));
    }
    var fallback = metricOrNull(fallbackMetric);
    if (fallback !== null && fallbackDenominator > 0) {
      return Math.max(0, Math.min(fallbackDenominator, Math.round(fallback / 100 * fallbackDenominator)));
    }
    return 0;
  }

  function metricFromMonthlyRows(rows) {
    var samples = rows.reduce(function (sum, row) { return sum + row.samples; }, 0);
    var correct = rows.reduce(function (sum, row) { return sum + row.correct; }, 0);
    var predUp = 0;
    var predDown = 0;
    var actualUp = 0;
    var actualDown = 0;
    var upTp = 0;
    var downTp = 0;
    rows.forEach(function (row) {
      var actualCounts = normalizeDist(row.actualCounts || row.actualDist);
      var predictedCounts = normalizeDist(row.predictedCounts || row.predictedDist);
      predUp += predictedCounts.up;
      predDown += predictedCounts.down;
      actualUp += actualCounts.up;
      actualDown += actualCounts.down;
      upTp += inferTruePositive(row.upRecall, actualCounts.up, row.upPrecision, predictedCounts.up);
      downTp += inferTruePositive(row.downRecall, actualCounts.down, row.downPrecision, predictedCounts.down);
    });
    return {
      samples: samples,
      correct: correct,
      overall: samples ? correct / samples * 100 : null,
      upPrecision: predUp ? upTp / predUp * 100 : null,
      upRecall: actualUp ? upTp / actualUp * 100 : null,
      downPrecision: predDown ? downTp / predDown * 100 : null,
      downRecall: actualDown ? downTp / actualDown * 100 : null
    };
  }

  function aggregateScheme(scheme) {
    var dailyRows = getVisibleDailyRowsForScheme(scheme);
    if (dailyRows.length) return metricFromSampleRows(dailyRows);
    return metricFromMonthlyRows(getVisibleRowsForScheme(scheme));
  }

  function sortSchemesByMetric(schemes) {
    return schemes.slice().sort(function (a, b) {
      var aMetric = aggregateScheme(a)[factorLabState.rankMetric] || 0;
      var bMetric = aggregateScheme(b)[factorLabState.rankMetric] || 0;
      return bMetric - aMetric;
    });
  }

  function ensureSelectedScheme() {
    var schemes = sortSchemesByMetric(getSelectedTaskSchemes());
    if (!schemes.length) {
      factorLabState.selectedSchemeId = "";
      return;
    }
    var stillVisible = schemes.some(function (scheme) {
      return scheme.id === factorLabState.selectedSchemeId;
    });
    if (!stillVisible) {
      factorLabState.selectedSchemeId = schemes[0].id;
    }
  }

  function renderTaskOverview() {
    var body = document.getElementById("factorTaskMatrixBody");
    var range = document.getElementById("factorOverviewRange");
    var metricBadge = document.getElementById("factorOverviewMetric");
    if (!body) return;
    if (range) range.textContent = factorLabState.startMonth + " 至 " + factorLabState.endMonth;
    if (metricBadge) metricBadge.textContent = "指标：" + getMetricLabel(factorLabState.rankMetric);

    var html = factorTargets.map(function (target) {
      var cells = factorTaskColumns.map(function (column) {
        var key = getTaskKey(target, column);
        var schemes = getSchemesForTask(key);
        var best = sortSchemesByMetric(schemes)[0];
        var metric = best ? aggregateScheme(best) : null;
        var selectedClass = key === factorLabState.selectedTaskKey ? " is-selected" : "";
        var value = metric ? formatPercent(metric[factorLabState.rankMetric]) : "--";
        return '<td><button type="button" class="factor-task-cell' + selectedClass + '" data-factor-task-key="' + escapeHtml(key) + '">' +
          '<span class="factor-task-top">' + value + '</span>' +
          '<span class="factor-task-count">' + schemes.length + ' 个方案</span>' +
          '</button></td>';
      }).join("");
      return '<tr><td>' + escapeHtml(getTargetDisplayName(target)) + '</td>' + cells + '</tr>';
    }).join("");
    body.innerHTML = html;
  }

  function renderSchemeRanking() {
    var body = document.getElementById("factorSchemeRankingBody");
    var title = document.getElementById("factorRankingTitle");
    var meta = document.getElementById("factorRankingMeta");
    if (!body) return;

    var task = getTaskByKey(factorLabState.selectedTaskKey);
    var schemes = sortSchemesByMetric(getSelectedTaskSchemes());
    if (title) title.textContent = task.label + " 候选方案排行";
    if (meta) {
      if (factorLabRemoteLoading) {
        meta.textContent = "正在读取本机方案数据。";
      } else if (factorLabApiError) {
        meta.textContent = "API暂不可用，当前显示本地备用数据。";
      } else if (factorLabDataMode === "backtest") {
        meta.textContent = "该任务格子下共有 " + schemes.length + " 个历史回测方案。";
      } else {
        meta.textContent = "该任务格子下共有 " + schemes.length + " 个候选方案。";
      }
    }

    if (!schemes.length) {
      body.innerHTML = '<tr><td colspan="8" class="factor-empty-cell">该任务格子下暂无方案</td></tr>';
      return;
    }

    body.innerHTML = schemes.map(function (scheme, index) {
      var metric = aggregateScheme(scheme);
      var selectedClass = scheme.id === factorLabState.selectedSchemeId ? " class=\"is-selected\"" : "";
      return '<tr' + selectedClass + ' data-factor-scheme-id="' + escapeHtml(scheme.id) + '">' +
        '<td>' + (index + 1) + '</td>' +
        '<td><strong>' + escapeHtml(scheme.name) + '</strong></td>' +
        '<td class="' + getMetricClass(metric.overall) + '">' + formatPercent(metric.overall) + '（' + metric.correct + '/' + metric.samples + '）</td>' +
        '<td>' + metric.samples + '</td>' +
        '<td class="' + getMetricClass(metric.upPrecision) + '">' + formatPercent(metric.upPrecision) + '</td>' +
        '<td class="' + getMetricClass(metric.downPrecision) + '">' + formatPercent(metric.downPrecision) + '</td>' +
        '<td>' + escapeHtml(scheme.latestRun) + '</td>' +
        '<td><span class="factor-status-pill is-' + escapeHtml(scheme.status) + '">' + escapeHtml(factorStatusLabels[scheme.status] || scheme.status) + '</span></td>' +
        '</tr>';
    }).join("");
  }

  function renderFactorPagination() {
    var pagination = document.getElementById("factorPaginationTop");
    if (!pagination) return;

    var rows = getVisibleFactorMonthRows();
    var totalRows = rows.length;
    var totalPages = Math.max(1, Math.ceil(totalRows / factorLabState.pageSize));
    if (factorLabState.page > totalPages) factorLabState.page = totalPages;

    var firstRow = totalRows === 0 ? 0 : ((factorLabState.page - 1) * factorLabState.pageSize + 1);
    var html = '<span>显示 ' + firstRow + '-';
    html += Math.min(totalRows, factorLabState.page * factorLabState.pageSize) + ' / ' + totalRows + ' 个月</span>';
    html += '<button type="button" data-factor-page="prev"' + (factorLabState.page === 1 ? " disabled" : "") + '>上一页</button>';
    for (var i = 1; i <= totalPages; i++) {
      html += '<button type="button" class="' + (i === factorLabState.page ? "is-active" : "") + '" data-factor-page="' + i + '">' + i + '</button>';
    }
    html += '<button type="button" data-factor-page="next"' + (factorLabState.page === totalPages ? " disabled" : "") + '>下一页</button>';
    pagination.innerHTML = html;
  }

  function getVisibleFactorMonthRows() {
    return getVisibleRowsForScheme(getSelectedScheme());
  }

  function getFactorAvailableMonths() {
    var months = factorDailyBaseRows.concat(factorWeeklyBaseRows).reduce(function (result, row) {
      if (result.indexOf(row.month) === -1) result.push(row.month);
      return result;
    }, []);
    Object.keys(factorTaskSchemes).forEach(function (key) {
      factorTaskSchemes[key].forEach(function (scheme) {
        scheme.monthlyRows.forEach(function (row) {
          if (months.indexOf(row.month) === -1) months.push(row.month);
        });
      });
    });
    return months.sort();
  }

  function syncFactorMonthRange() {
    var months = getFactorAvailableMonths();
    if (!months.length) return;
    if (months.indexOf(factorLabState.startMonth) === -1) factorLabState.startMonth = months[0];
    if (months.indexOf(factorLabState.endMonth) === -1) factorLabState.endMonth = months[months.length - 1];
    if (factorLabState.endMonth < factorLabState.startMonth) factorLabState.endMonth = factorLabState.startMonth;
  }

  function renderFactorMonthSelects() {
    var startMonthSelect = document.getElementById("factorStartMonth");
    var endMonthSelect = document.getElementById("factorEndMonth");
    if (!startMonthSelect || !endMonthSelect) return;

    syncFactorMonthRange();
    var months = getFactorAvailableMonths();
    startMonthSelect.innerHTML = months.map(function (month) {
      return '<option value="' + escapeHtml(month) + '"' + (month === factorLabState.startMonth ? " selected" : "") + '>' + escapeHtml(month) + '</option>';
    }).join("");
    endMonthSelect.innerHTML = months.map(function (month) {
      var disabled = month < factorLabState.startMonth ? " disabled" : "";
      return '<option value="' + escapeHtml(month) + '"' + (month === factorLabState.endMonth ? " selected" : "") + disabled + '>' + escapeHtml(month) + '</option>';
    }).join("");
    startMonthSelect.value = factorLabState.startMonth;
    endMonthSelect.value = factorLabState.endMonth;
  }

  function updateFactorFilterUi() {
    Array.prototype.slice.call(document.querySelectorAll("[data-factor-rank-metric]")).forEach(function (button) {
      button.classList.toggle("is-active", button.getAttribute("data-factor-rank-metric") === factorLabState.rankMetric);
    });
    renderFactorMonthSelects();
  }

  function updateFactorLabSummary() {
    var task = getTaskByKey(factorLabState.selectedTaskKey);
    var scheme = getSelectedScheme();
    var summaryValues = Array.prototype.slice.call(document.querySelectorAll(".factor-lab-summary strong"));
    if (summaryValues.length < 3) return;
    summaryValues[0].textContent = task.label;
    summaryValues[1].textContent = getSchemesForTask(factorLabState.selectedTaskKey).length;
    summaryValues[2].textContent = scheme ? scheme.name.split(" ")[0] : "--";
  }

  function updateFactorTrendToggles() {
    Array.prototype.slice.call(document.querySelectorAll("[data-factor-chart-metric]")).forEach(function (button) {
      var metricId = button.getAttribute("data-factor-chart-metric");
      var active = factorLabState.chartMetrics[metricId] !== false;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function renderFactorTrendChart() {
    var host = document.getElementById("factorTrendChart");
    if (!host) return;

    updateFactorTrendToggles();
    var rows = getVisibleFactorMonthRows();
    var metrics = factorTrendMetrics.filter(function (metric) {
      return factorLabState.chartMetrics[metric.id] !== false;
    });
    if (!rows.length || !metrics.length) {
      host.innerHTML = '<div class="factor-trend-empty">暂无可展示的趋势指标</div>';
      return;
    }

    var width = 960;
    var height = 300;
    var left = 58;
    var right = 24;
    var top = 24;
    var bottom = 42;
    var plotWidth = width - left - right;
    var plotHeight = height - top - bottom;
    var x = function (index) {
      return rows.length === 1 ? left + plotWidth / 2 : left + plotWidth * index / (rows.length - 1);
    };
    var y = function (value) {
      return top + (100 - Number(value || 0)) / 100 * plotHeight;
    };

    var svg = '<svg viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="月度指标趋势折线图">';
    [0, 25, 50, 75, 100].forEach(function (tick) {
      var tickY = y(tick);
      svg += '<line class="factor-trend-grid" x1="' + left + '" y1="' + tickY.toFixed(1) + '" x2="' + (width - right) + '" y2="' + tickY.toFixed(1) + '"></line>';
      svg += '<text class="factor-trend-axis" x="' + (left - 12) + '" y="' + (tickY + 4).toFixed(1) + '" text-anchor="end">' + tick + '%</text>';
    });
    rows.forEach(function (row, index) {
      svg += '<text class="factor-trend-axis" x="' + x(index).toFixed(1) + '" y="' + (height - 14) + '" text-anchor="middle">' + escapeHtml(row.month) + '</text>';
    });
    metrics.forEach(function (metric) {
      var points = rows.map(function (row, index) {
        return [x(index), y(row[metric.id]), row[metric.id], row.month];
      });
      var path = points.map(function (point, index) {
        return (index === 0 ? "M" : "L") + point[0].toFixed(1) + " " + point[1].toFixed(1);
      }).join(" ");
      svg += '<path class="factor-trend-line" d="' + path + '" stroke="' + metric.color + '"></path>';
      points.forEach(function (point) {
        svg += '<g><title>' + escapeHtml(point[3] + " · " + metric.label + " " + formatPercent(point[2])) + '</title>';
        svg += '<circle class="factor-trend-point" cx="' + point[0].toFixed(1) + '" cy="' + point[1].toFixed(1) + '" r="4.5" fill="' + metric.color + '"></circle></g>';
      });
    });
    svg += '</svg>';
    host.innerHTML = svg;
  }

  function renderFactorDetail() {
    var tbody = document.getElementById("factorMonthlyTableBody");
    var title = document.getElementById("factorDetailTitle");
    var meta = document.getElementById("factorDetailMeta");
    if (!tbody) return;

    var task = getTaskByKey(factorLabState.selectedTaskKey);
    var scheme = getSelectedScheme();
    if (title) title.textContent = "选中方案详情：" + task.label;
    if (meta) meta.textContent = scheme ? scheme.name : "该任务格子下暂无可查看方案。";

    var start = (factorLabState.page - 1) * factorLabState.pageSize;
    var visibleRows = getVisibleFactorMonthRows();
    var pageRows = visibleRows.slice(start, start + factorLabState.pageSize);
    var calendarIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="18" rx="2"></rect><path d="M16 2v4M8 2v4M3 10h18"></path></svg>';

    var html = "";
    pageRows.forEach(function (row) {
      html += '<tr>';
      html += '<td>' + escapeHtml(row.month) + '</td>';
      html += '<td><strong>' + row.samples + '</strong></td>';
      html += '<td>' + escapeHtml(row.actualDist) + '</td>';
      html += '<td>' + escapeHtml(row.predictedDist) + '</td>';
      html += '<td class="' + getMetricClass(row.overall) + '">' + formatPercent(row.overall) + '（' + row.correct + '/' + row.samples + '）</td>';
      html += '<td class="' + getMetricClass(row.upPrecision) + '">' + formatPercent(row.upPrecision) + '</td>';
      html += '<td class="' + getMetricClass(row.upRecall) + '">' + formatPercent(row.upRecall) + '</td>';
      html += '<td class="' + getMetricClass(row.downPrecision) + '">' + formatPercent(row.downPrecision) + '</td>';
      html += '<td class="' + getMetricClass(row.downRecall) + '">' + formatPercent(row.downRecall) + '</td>';
      html += '<td><button type="button" class="factor-calendar-link" data-factor-calendar-month="' + escapeHtml(row.month) + '" aria-label="查看' + escapeHtml(row.month) + '每日明细">' + calendarIcon + '</button></td>';
      html += '</tr>';
    });
    if (!html) html = '<tr><td colspan="10" class="factor-empty-cell">当前方案暂无月度数据</td></tr>';
    tbody.innerHTML = html;
    renderFactorPagination();
    renderFactorTrendChart();
  }

  function renderFactorLab() {
    bindFactorLabEvents();
    loadFactorLabData();
    updateFactorFilterUi();
    ensureSelectedScheme();
    renderTaskOverview();
    renderSchemeRanking();
    updateFactorLabSummary();
    renderFactorDetail();
  }

  function renderFactorDailyRows(month) {
    var body = document.getElementById("factorDailyTableBody");
    var title = document.getElementById("factorCalendarTitle");
    var meta = document.getElementById("factorCalendarMeta");
    if (!body || !title || !meta) return;

    var task = getTaskByKey(factorLabState.selectedTaskKey);
    var scheme = getSelectedScheme();
    var isWeekly = task.frequency === "weekly";
    title.textContent = month + (isWeekly ? " 周度验证表" : " 每日验证表");
    meta.textContent = (scheme ? scheme.name : "--") + " · " + task.label;

    var html = "";
    var monthLabel = month.slice(5, 7);
    var rows = scheme && scheme.dailyRowsByMonth && scheme.dailyRowsByMonth[month]
      ? scheme.dailyRowsByMonth[month]
      : (isWeekly ? factorWeeklyRows : factorDailyRows);
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="4" class="factor-empty-cell">当前月份暂无每日明细</td></tr>';
      return;
    }
    rows.forEach(function (row) {
      var predictedClass = row.predicted === "涨" ? "direction-up" : (row.predicted === "跌" ? "direction-down" : "");
      var actualClass = row.actual === "涨" ? "direction-up" : (row.actual === "跌" ? "direction-down" : "");
      var displayDay = row.day.replace(/^\d{2}/, monthLabel);
      var result = row.correct === null
        ? '<span class="factor-result-dot" style="background:#bfc5c0;">?</span>'
        : '<span class="factor-result-dot ' + (row.correct ? "is-correct" : "is-wrong") + '">' + (row.correct ? "✓" : "×") + '</span>';
      html += '<tr>';
      html += '<td class="mono">' + escapeHtml(displayDay) + '</td>';
      html += '<td class="' + predictedClass + '">' + escapeHtml(row.predicted) + '</td>';
      html += '<td class="' + actualClass + '">' + escapeHtml(row.actual) + '</td>';
      html += '<td>' + result + '</td>';
      html += '</tr>';
    });
    body.innerHTML = html;
  }

  function openFactorCalendar(month) {
    var drawer = document.getElementById("factorCalendarDrawer");
    if (!drawer) return;
    renderFactorDailyRows(month);
    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
  }

  function closeFactorCalendar() {
    var drawer = document.getElementById("factorCalendarDrawer");
    if (!drawer) return;
    drawer.classList.remove("is-open");
    drawer.setAttribute("aria-hidden", "true");
  }

  function bindFactorLabEvents() {
    if (factorLabBound) return;
    factorLabBound = true;

    var pageHost = document.querySelector('[data-view="factor-lab"]');
    if (pageHost) {
      pageHost.addEventListener("click", function (event) {
        var taskButton = event.target.closest("[data-factor-task-key]");
        if (taskButton) {
          factorLabState.selectedTaskKey = taskButton.getAttribute("data-factor-task-key");
          factorLabState.selectedSchemeId = "";
          factorLabState.page = 1;
          closeFactorCalendar();
          renderFactorLab();
          return;
        }

        var schemeRow = event.target.closest("[data-factor-scheme-id]");
        if (schemeRow) {
          factorLabState.selectedSchemeId = schemeRow.getAttribute("data-factor-scheme-id");
          factorLabState.page = 1;
          closeFactorCalendar();
          renderFactorLab();
          return;
        }

        var rankButton = event.target.closest("[data-factor-rank-metric]");
        if (rankButton) {
          factorLabState.rankMetric = rankButton.getAttribute("data-factor-rank-metric") || "overall";
          factorLabState.selectedSchemeId = "";
          factorLabState.page = 1;
          closeFactorCalendar();
          renderFactorLab();
          return;
        }

        var pageButton = event.target.closest("[data-factor-page]");
        if (pageButton && !pageButton.disabled) {
          var action = pageButton.getAttribute("data-factor-page");
          var totalPages = Math.max(1, Math.ceil(getVisibleFactorMonthRows().length / factorLabState.pageSize));
          if (action === "prev") {
            factorLabState.page = Math.max(1, factorLabState.page - 1);
          } else if (action === "next") {
            factorLabState.page = Math.min(totalPages, factorLabState.page + 1);
          } else {
            factorLabState.page = Number(action) || 1;
          }
          renderFactorLab();
          return;
        }

        var calendarButton = event.target.closest("[data-factor-calendar-month]");
        if (calendarButton) {
          openFactorCalendar(calendarButton.getAttribute("data-factor-calendar-month"));
          return;
        }

        var chartMetricButton = event.target.closest("[data-factor-chart-metric]");
        if (chartMetricButton) {
          var metricId = chartMetricButton.getAttribute("data-factor-chart-metric");
          factorLabState.chartMetrics[metricId] = factorLabState.chartMetrics[metricId] === false;
          renderFactorTrendChart();
        }
      });

      Array.prototype.slice.call(pageHost.querySelectorAll("[data-factor-calendar-close]")).forEach(function (button) {
        button.addEventListener("click", closeFactorCalendar);
      });
    }

    var startMonthInput = document.getElementById("factorStartMonth");
    var endMonthInput = document.getElementById("factorEndMonth");
    if (startMonthInput && !startMonthInput.dataset.factorBound) {
      startMonthInput.dataset.factorBound = "true";
      startMonthInput.addEventListener("change", function () {
        factorLabState.startMonth = startMonthInput.value || factorLabState.startMonth;
        if (factorLabState.startMonth > factorLabState.endMonth) {
          factorLabState.endMonth = factorLabState.startMonth;
          if (endMonthInput) endMonthInput.value = factorLabState.endMonth;
        }
        factorLabState.selectedSchemeId = "";
        factorLabState.page = 1;
        closeFactorCalendar();
        renderFactorLab();
      });
    }

    if (endMonthInput && !endMonthInput.dataset.factorBound) {
      endMonthInput.dataset.factorBound = "true";
      endMonthInput.addEventListener("change", function () {
        factorLabState.endMonth = endMonthInput.value || factorLabState.endMonth;
        if (factorLabState.endMonth < factorLabState.startMonth) {
          factorLabState.endMonth = factorLabState.startMonth;
          endMonthInput.value = factorLabState.endMonth;
        }
        factorLabState.selectedSchemeId = "";
        factorLabState.page = 1;
        closeFactorCalendar();
        renderFactorLab();
      });
    }
  }

  /* ─── TradingAgents Demo ─── */
  var tradingAgentDemoRun = {
    meta: {
      ticker: "CNC.T0",
      date: "2026-05-26",
      reportId: "RPT-DEMO-001"
    },
    stages: [
      {
        id: "analysts",
        title: "01 分析师团队",
        summary: "Market、Sentiment、News、Fundamentals 四类分析师串行形成基础研究材料。",
        nodes: [
          {
            id: "market",
            title: "Market Analyst",
            role: "技术分析",
            summary: "读取价格、成交量和技术指标，判断短中期趋势是否支持建仓。",
            bullets: [
              "价格处于 26.50 - 29.50 CNY 观察区间内，短线反弹仍未完成有效突破。",
              "成交量回升但不连续，说明主动买盘还没有形成稳定共识。",
              "波动率抬升，止损与仓位上限需要前置约束。"
            ],
            duration: 620
          },
          {
            id: "sentiment",
            title: "Sentiment Analyst",
            role: "情绪分析",
            summary: "聚合市场讨论与新闻情绪，识别短线拥挤度和反身性风险。",
            bullets: [
              "情绪修复主要来自估值回补预期，并非趋势性乐观。",
              "负面讨论集中在现金流质量和商誉减值余波。",
              "情绪因子短期偏中性，暂不支持激进加仓。"
            ],
            duration: 620
          },
          {
            id: "news",
            title: "News Analyst",
            role: "新闻分析",
            summary: "检查公司事件、行业动态和宏观约束，寻找可能改变方向的催化因素。",
            bullets: [
              "近期新闻没有出现足以重估盈利中枢的强催化。",
              "政策与行业流动性环境边际稳定，但风险偏好修复仍偏慢。",
              "下一阶段关注 Q2 财报和管理层现金流指引。"
            ],
            duration: 620
          },
          {
            id: "fundamentals",
            title: "Fundamentals Analyst",
            role: "基本面分析",
            summary: "评估盈利质量、自由现金流、估值和资产负债表风险。",
            bullets: [
              "低估值提供安全边际，账面价值折价仍具备吸引力。",
              "Q1 盈利修复需要更多季度验证，营运资本贡献不可线性外推。",
              "商誉和现金流质量是限制估值扩张的主要因素。"
            ],
            duration: 680
          }
        ]
      },
      {
        id: "research",
        title: "02 投资辩论",
        summary: "Bull/Bear 围绕买入与回避展开对抗，Research Manager 将争议压缩成投资计划。",
        nodes: [
          {
            id: "bull",
            title: "Bull Researcher",
            role: "多头研究员",
            summary: "主张低估值和盈利修复构成中期配置机会。",
            bullets: [
              "当前估值隐含较悲观预期，账面价值折价提供保护。",
              "自由现金流收益率改善，若延续可驱动估值修复。",
              "反弹初期不宜等到所有确认信号出现后才配置。"
            ],
            duration: 700
          },
          {
            id: "bear",
            title: "Bear Researcher",
            role: "空头研究员",
            summary: "强调现金流可持续性、商誉和技术形态仍构成下行约束。",
            bullets: [
              "盈利改善仍可能由一次性因素驱动，可持续性不足。",
              "技术面三次高点下移，上方抛压没有完全释放。",
              "流动性不足会放大回撤，估值折价可能长期存在。"
            ],
            duration: 700
          },
          {
            id: "research-manager",
            title: "Research Manager",
            role: "研究经理",
            summary: "综合多空辩论，输出 Hold / 中性持有的投资计划。",
            bullets: [
              "多头逻辑成立但催化不足，空头风险尚未解除。",
              "建议维持中低仓位，等待财报验证后再调整方向。",
              "将 26.50 - 29.50 CNY 设为关键观察区间。"
            ],
            duration: 760
          }
        ]
      },
      {
        id: "trading",
        title: "03 交易方案",
        summary: "Trader 将 Research Manager 的投资计划转化为可执行的交易方案。",
        nodes: [
          {
            id: "trader",
            title: "Trader",
            role: "交易执行",
            summary: "把 Hold 观点转成仓位、触发条件和执行约束。",
            bullets: [
              "建议仓位维持在 2.5% - 3.5%，避免主动提高风险暴露。",
              "放量突破区间上沿后再逐步加仓，不提前追涨。",
              "跌破关键支撑后降至防御仓位并重新评估。"
            ],
            duration: 680
          }
        ]
      },
      {
        id: "risk",
        title: "04 风险委员会",
        summary: "Aggressive、Conservative、Neutral 三类风控观点轮转，Portfolio Manager 形成终裁。",
        nodes: [
          {
            id: "aggressive",
            title: "Aggressive Analyst",
            role: "进取风控",
            summary: "认为安全边际足够，可以保留向上弹性。",
            bullets: [
              "估值折价充分，若基本面继续修复，收益弹性高于损失空间。",
              "建议保留核心仓位，不因短线波动过早撤出。",
              "可用条件单控制回撤，而不是完全放弃机会。"
            ],
            duration: 620
          },
          {
            id: "conservative",
            title: "Conservative Analyst",
            role: "保守风控",
            summary: "强调本金保护，建议控制仓位和回撤阈值。",
            bullets: [
              "现金流质量和商誉风险未完全出清，不能用低估值掩盖质量问题。",
              "流动性不足会导致止损执行成本上升。",
              "仓位上限应低于常规配置，直到财报验证。"
            ],
            duration: 620
          },
          {
            id: "neutral",
            title: "Neutral Analyst",
            role: "中性风控",
            summary: "在收益弹性和下行保护之间给出折中建议。",
            bullets: [
              "维持中低仓位，既保留修复参与权，也限制尾部风险。",
              "把财报和成交量作为二次决策触发器。",
              "避免一次性调仓，采用分段验证方式。"
            ],
            duration: 620
          },
          {
            id: "portfolio-manager",
            title: "Portfolio Manager",
            role: "组合经理",
            summary: "给出最终 Hold 裁决和仓位控制建议。",
            bullets: [
              "最终评级：Hold，中性持有。",
              "建议仓位：2.5% - 3.5%，等待 Q2 财报确认盈利质量。",
              "若突破 29.50 CNY 且成交量放大，可重新评估加仓；若跌破支撑，降至防御仓位。"
            ],
            duration: 760
          }
        ]
      }
    ],
    report: {
      rating: "Hold",
      position: "2.5%-3.5%",
      risk: "Medium",
      confidence: "58%",
      executive_summary: "维持 CNC.T0 中性持有评级，建议中低仓位配置。当前价格处于基本面修复与结构性风险并存的均衡区，等待后续财报验证盈利恢复的可持续性。",
      investment_thesis: "低估值提供安全边际，但反弹成交量不足、三次高点下移，说明上行需要更明确的基本面催化。",
      raw_markdown: "## 最终交易报告\n\n**Rating**: Hold\n\n**Executive Summary**: 维持 CNC.T0 中性持有评级，建议中低仓位配置。当前价格处于基本面修复与结构性风险并存的均衡区，等待后续财报验证盈利恢复的可持续性。\n\n**Investment Thesis**: 低估值提供安全边际，但反弹成交量不足、三次高点下移，说明上行需要更明确的基本面催化。\n\n**Risk Assessment**: 主要风险为营运资本贡献不可持续、商誉减值余波和流动性不足导致的估值折价。\n\n**Position Guidance**:\n- 建议仓位：2.5% - 3.5%\n- 持有周期：中期，持有至 Q2 财报验证\n- 观察区间：26.50 - 29.50 CNY\n- 触发条件：放量突破上沿可逐步加仓；跌破关键支撑则降至防御仓位。"
    }
  };

  var agentDemoRendered = false;
  var agentRunTimer = null;
  var agentActivityTimer = null;
  var agentStepIndex = -1;
  var agentRunStarted = false;
  var agentRunCompleted = false;
  var agentRunSource = "后端实时运行";
  var agentLiveLogs = [];

  function getAgentNodes() {
    var nodes = [];
    tradingAgentDemoRun.stages.forEach(function (stage) {
      stage.nodes.forEach(function (node) {
        node.stageId = stage.id;
        nodes.push(node);
      });
    });
    return nodes;
  }

  function getAgentNodeById(nodeId) {
    return getAgentNodes().filter(function (node) {
      return node.id === nodeId;
    })[0] || null;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function formatInlineMarkdown(value) {
    return escapeHtml(value)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function renderMarkdownDocument(markdown) {
    var lines = String(markdown || "").split(/\r?\n/);
    var html = "";
    var inList = false;

    function closeList() {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
    }

    lines.forEach(function (rawLine) {
      var line = rawLine.trim();
      if (!line) {
        closeList();
        return;
      }

      var heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        closeList();
        html += "<h" + heading[1].length + ">" + formatInlineMarkdown(heading[2]) + "</h" + heading[1].length + ">";
        return;
      }

      var bullet = line.match(/^[-*]\s+(.+)$/);
      if (bullet) {
        if (!inList) {
          html += "<ul>";
          inList = true;
        }
        html += "<li>" + formatInlineMarkdown(bullet[1]) + "</li>";
        return;
      }

      closeList();
      html += "<p>" + formatInlineMarkdown(line) + "</p>";
    });

    closeList();
    return html || "<p>暂无报告内容。</p>";
  }

  function buildReportMarkdown(report) {
    if (report && report.raw_markdown) return report.raw_markdown;
    report = report || {};
    var parts = ["## 最终交易报告"];
    if (report.rating) parts.push("**Rating**: " + report.rating);
    if (report.executive_summary) parts.push("**Executive Summary**: " + report.executive_summary);
    if (report.investment_thesis) parts.push("**Investment Thesis**: " + report.investment_thesis);
    if (report.risk_assessment) parts.push("**Risk Assessment**: " + report.risk_assessment);
    if (report.position) parts.push("**Position Guidance**: " + report.position);
    return parts.join("\n\n");
  }

  function getAgentStageState(stage) {
    var doneCount = stage.nodes.filter(function (node) { return node.state === "done"; }).length;
    var hasRunning = stage.nodes.some(function (node) { return node.state === "running"; });
    if (doneCount === stage.nodes.length) return "done";
    if (hasRunning || doneCount > 0) return "running";
    return "waiting";
  }

  function resetAgentDemoState() {
    getAgentNodes().forEach(function (node) {
      node.state = "waiting";
    });
    agentStepIndex = -1;
    agentRunStarted = false;
    agentRunCompleted = false;
    if (agentRunTimer) {
      window.clearTimeout(agentRunTimer);
      agentRunTimer = null;
    }
    if (agentActivityTimer) {
      window.clearInterval(agentActivityTimer);
      agentActivityTimer = null;
    }
    agentLiveLogs = [];
    setAgentLiveState("等待开始分析", "点击开始后，系统会实时展示当前分析师、运行日志和最终报告。");
    renderAgentLiveLog();
  }

  function renderAgentMiniTimeline() {
    var timeline = document.getElementById("agentMiniTimeline");
    if (!timeline) return;

    var html = "";
    getAgentNodes().forEach(function (node) {
      html += '<span class="agent-mini-dot is-' + (node.state || "waiting") + '" title="' + escapeHtml(node.title) + '"></span>';
    });
    timeline.innerHTML = html;
  }

  function renderAgentStageList() {
    var stageList = document.getElementById("agentStageList");
    if (!stageList) return;

    var html = "";
    tradingAgentDemoRun.stages.forEach(function (stage) {
      var stageState = getAgentStageState(stage);
      var doneCount = stage.nodes.filter(function (node) { return node.state === "done"; }).length;
      html += '<section class="agent-stage-card is-' + stageState + '">';
      html += '<div class="agent-stage-top"><strong>' + escapeHtml(stage.title) + '</strong><span>' + doneCount + '/' + stage.nodes.length + '</span></div>';
      html += '<p class="agent-stage-summary">' + escapeHtml(stage.summary) + '</p>';
      html += '<div class="agent-node-list">';
      stage.nodes.forEach(function (node) {
        var state = node.state || "waiting";
        html += '<button type="button" class="agent-node is-' + state + '" data-agent-id="' + escapeHtml(node.id) + '">';
        html += '<span class="agent-node-state" aria-hidden="true"></span>';
        html += '<span class="agent-node-title">' + escapeHtml(node.title) + '</span>';
        html += '<span class="agent-node-role">' + escapeHtml(node.role) + '</span>';
        html += '</button>';
      });
      html += '</div></section>';
    });
    stageList.innerHTML = html;
  }

  function renderAgentPipeline() {
    var pipeline = document.getElementById("agentPipeline");
    if (!pipeline) return;

    var labels = {
      market: "MKT",
      sentiment: "SNT",
      news: "NEW",
      fundamentals: "FND",
      bull: "BULL",
      bear: "BEAR",
      "research-manager": "RM",
      trader: "TRD",
      aggressive: "AGR",
      conservative: "CON",
      neutral: "NEU",
      "portfolio-manager": "PM"
    };

    var html = "";
    tradingAgentDemoRun.stages.forEach(function (stage) {
      var stageState = getAgentStageState(stage);
      var doneCount = stage.nodes.filter(function (node) { return node.state === "done"; }).length;
      html += '<section class="agent-flow-stage is-' + stageState + '">';
      html += '<header><strong>' + escapeHtml(stage.title.replace(/^\d+\s*/, "")) + '</strong><span>' + doneCount + '/' + stage.nodes.length + '</span></header>';
      html += '<div class="agent-flow-track">';
      stage.nodes.forEach(function (node) {
        var state = node.state || "waiting";
        html += '<button type="button" class="agent-pipeline-node is-' + state + '" data-agent-id="' + escapeHtml(node.id) + '" title="' + escapeHtml(node.title + " · " + node.role) + '">';
        html += '<span class="pipeline-code">' + escapeHtml(labels[node.id] || node.title.slice(0, 3)) + '</span>';
        html += '<span class="pipeline-name">' + escapeHtml(node.role) + '</span>';
        html += '</button>';
      });
      html += '</div></section>';
    });
    pipeline.innerHTML = html;
  }

  function getActiveAgentNode() {
    var nodes = getAgentNodes();
    return nodes.filter(function (node) { return node.state === "running"; })[0] ||
      nodes.filter(function (node) { return node.state !== "done"; })[0] ||
      nodes[nodes.length - 1] ||
      null;
  }

  function setAgentLiveState(title, subtitle) {
    var titleEl = document.getElementById("agentLiveTitle");
    var subtitleEl = document.getElementById("agentLiveSubtitle");
    if (titleEl) titleEl.textContent = title;
    if (subtitleEl) subtitleEl.textContent = subtitle;
  }

  function renderAgentLiveLog() {
    var logEl = document.getElementById("agentLiveLog");
    if (!logEl) return;
    var html = "";
    if (!agentLiveLogs.length) {
      logEl.innerHTML = '<div class="agent-log-entry is-empty"><strong>IDLE</strong><span>等待提交分析任务。</span></div>';
      logEl.scrollTop = logEl.scrollHeight;
      return;
    }
    agentLiveLogs.slice(-5).forEach(function (item) {
      html += '<div class="agent-log-entry"><strong>' + escapeHtml(item.label) + '</strong><span>' + escapeHtml(item.text) + '</span></div>';
    });
    logEl.innerHTML = html;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function pushAgentLiveLog(label, text) {
    agentLiveLogs.push({ label: label, text: text });
    if (agentLiveLogs.length > 8) agentLiveLogs = agentLiveLogs.slice(agentLiveLogs.length - 8);
    renderAgentLiveLog();
  }

  function markAgentRunning(nodeId) {
    var node = getAgentNodeById(nodeId);
    if (!node || node.state === "done") return;
    getAgentNodes().forEach(function (item) {
      if (item.state === "running" && item.id !== nodeId) item.state = "waiting";
    });
    node.state = "running";
    setAgentLiveState(node.title + " 正在分析", node.summary || "正在等待该节点返回分析结果。");
    refreshAgentDemoUi();
  }

  function markNextPendingRunning() {
    var next = getAgentNodes().filter(function (node) { return node.state !== "done"; })[0];
    if (next) markAgentRunning(next.id);
  }

  function updateAgentProgress() {
    var nodes = getAgentNodes();
    var doneCount = nodes.filter(function (node) { return node.state === "done"; }).length;
    var hasRunning = nodes.some(function (node) { return node.state === "running"; });
    var visualCount = doneCount + (hasRunning ? 0.5 : 0);
    var percent = nodes.length ? Math.round((visualCount / nodes.length) * 100) : 0;
    percent = Math.max(0, Math.min(100, percent));
    var progressText = document.getElementById("agentProgressText");
    var progressBar = document.getElementById("agentProgressBar");

    if (progressText) {
      progressText.textContent = doneCount + " / " + nodes.length + " · " + percent + "%";
    }
    if (progressBar) {
      progressBar.style.width = percent + "%";
    }
    var overallProgress = document.getElementById("agentOverallProgress");
    if (overallProgress) {
      overallProgress.style.width = percent + "%";
      if (overallProgress.parentElement) {
        overallProgress.parentElement.setAttribute("aria-valuenow", String(percent));
      }
    }
    var overallProgressText = document.getElementById("agentOverallProgressText");
    if (overallProgressText) {
      overallProgressText.textContent = percent + "%";
    }
  }

  function renderAgentReportDocument(report) {
    var doc = document.getElementById("agentReportDocument");
    if (!doc) return;
    doc.innerHTML = renderMarkdownDocument(buildReportMarkdown(report || tradingAgentDemoRun.report));
  }

  function updateAgentReportState(state, message) {
    var panel = document.getElementById("agentReportPanel");
    var status = document.getElementById("agentReportStatus");
    var placeholder = document.getElementById("agentReportPlaceholder");
    var content = document.getElementById("agentReportContent");
    var startBtn = document.getElementById("agentStartBtn");
    var tickerInput = document.getElementById("agentTickerInput");
    var dateInput = document.getElementById("agentTradeDate");
    var meta = document.getElementById("agentReportMeta");

    if (meta) {
      meta.textContent = "标的：" + ((tickerInput && tickerInput.value) || tradingAgentDemoRun.meta.ticker) +
        " · 日期：" + ((dateInput && dateInput.value) || tradingAgentDemoRun.meta.date) + " · " + agentRunSource;
    }

    if (panel) {
      panel.classList.toggle("is-pending", state !== "done" && state !== "error");
      panel.classList.toggle("is-running", state === "running");
      panel.classList.toggle("is-complete", state === "done");
      panel.classList.toggle("is-error", state === "error");
    }

    if (status) {
      status.classList.toggle("is-running", state === "running");
      status.classList.toggle("is-done", state === "done");
      status.classList.toggle("is-error", state === "error");
      status.textContent = state === "done" ? "已生成" : (state === "running" ? "分析中" : (state === "error" ? "运行失败" : "等待运行"));
    }

    if (placeholder) {
      placeholder.hidden = state === "done";
    }
    if (content) {
      content.hidden = state !== "done";
    }

    if (startBtn) {
      startBtn.disabled = state === "running";
      startBtn.innerHTML = state === "running" ? '<span aria-hidden="true">●</span> 分析中' : '<span aria-hidden="true">▶</span> 开始分析';
    }

    if (state === "done") {
      var report = tradingAgentDemoRun.report;
      var rating = document.getElementById("agentMetricRating");
      var position = document.getElementById("agentMetricPosition");
      var risk = document.getElementById("agentMetricRisk");
      var confidence = document.getElementById("agentMetricConfidence");
      if (rating) rating.textContent = report.rating || "--";
      if (position) position.textContent = report.position || "--";
      if (risk) risk.textContent = report.risk || "--";
      if (confidence) confidence.textContent = report.confidence || "--";
      setAgentLiveState("Portfolio Manager 已生成最终报告", "报告已根据分析师团队、投资辩论、交易方案和风险委员会输出直接渲染。");
      renderAgentReportDocument(report);
    } else if (state === "error") {
      setAgentLiveState("后端运行失败", message || "TradingAgents 后端暂时不可用，可重新分析或等待后端恢复。");
      pushAgentLiveLog("ERROR", message || "TradingAgents run failed.");
    } else {
      ["agentMetricRating", "agentMetricPosition", "agentMetricRisk", "agentMetricConfidence"].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.textContent = "--";
      });
    }
  }

  function renderTradingAgentProcess() {
    var stageList = document.getElementById("agentStageList");
    if (!stageList) return;

    if (!agentDemoRendered) {
      resetAgentDemoState();
      bindAgentDemoEvents();
      agentDemoRendered = true;
    }

    renderAgentMiniTimeline();
    renderAgentPipeline();
    renderAgentStageList();
    updateAgentProgress();
    updateAgentReportState(agentRunStarted ? "running" : (agentRunCompleted ? "done" : "idle"));
  }

  function refreshAgentDemoUi() {
    renderAgentMiniTimeline();
    renderAgentPipeline();
    renderAgentStageList();
    updateAgentProgress();
  }

  function completeTradingAgentDemo() {
    agentRunTimer = null;
    agentRunStarted = false;
    agentRunCompleted = true;
    if (agentActivityTimer) {
      window.clearInterval(agentActivityTimer);
      agentActivityTimer = null;
    }
    getAgentNodes().forEach(function (node) {
      node.state = "done";
    });
    refreshAgentDemoUi();
    updateAgentReportState("done");
  }

  function startAgentActivityTicker() {
    if (agentActivityTimer) {
      window.clearInterval(agentActivityTimer);
    }

    var phrases = [
      "读取输入参数并准备上下文窗口。",
      "等待模型与数据工具返回，当前节点仍在工作。",
      "归并上游观点，提取可交易证据。",
      "检查风险约束，准备传递给下一位分析师。"
    ];
    var tick = 0;

    agentActivityTimer = window.setInterval(function () {
      if (!agentRunStarted || agentRunCompleted) return;
      var active = getActiveAgentNode();
      if (!active) return;
      var text = phrases[tick % phrases.length];
      tick += 1;
      setAgentLiveState(active.title + " 正在分析", active.summary || text);
      pushAgentLiveLog(active.title, text);
    }, 2600);
  }

  function advanceAgentStep() {
    var nodes = getAgentNodes();

    if (agentStepIndex >= 0 && nodes[agentStepIndex]) {
      nodes[agentStepIndex].state = "done";
      pushAgentLiveLog(nodes[agentStepIndex].title, "演示节点已完成，结果进入下一环节。");
    }

    agentStepIndex++;
    if (agentStepIndex >= nodes.length) {
      completeTradingAgentDemo();
      return;
    }

    nodes[agentStepIndex].state = "running";
    setAgentLiveState(nodes[agentStepIndex].title + " 正在分析", nodes[agentStepIndex].summary || "正在模拟该节点输出。");
    pushAgentLiveLog(nodes[agentStepIndex].title, "演示模式正在生成分析内容。");
    refreshAgentDemoUi();

    agentRunTimer = window.setTimeout(advanceAgentStep, nodes[agentStepIndex].duration || 650);
  }

  function startTradingAgentDemo() {
    resetAgentDemoState();
    agentRunStarted = true;
    agentRunSource = "后端实时运行";
    updateAgentReportState("running");
    refreshAgentDemoUi();
    markAgentRunning("market");
    pushAgentLiveLog("RUN", "TradingAgents 运行已提交，正在建立后端分析流。");
    startAgentActivityTicker();

    if (reduceMotionQuery && reduceMotionQuery.matches) {
      completeTradingAgentDemo();
      return;
    }

    // Try real backend SSE first, fall back to mock on failure
    startTradingAgentSSE().catch(function (err) {
      console.warn("[TradingAgents] Backend unavailable, using mock mode:", err.message || err);
      agentRunSource = "演示模式";
      pushAgentLiveLog("FALLBACK", "后端暂不可用，已切换到前端演示流程。");
      advanceAgentStep();
    });
  }

  function startTradingAgentSSE() {
    var tickerInput = document.getElementById("agentTickerInput");
    var dateInput = document.getElementById("agentTradeDate");
    var activeAsset = document.querySelector('.agent-segmented [data-agent-asset].is-active');
    var ticker = (tickerInput && tickerInput.value) || tradingAgentDemoRun.meta.ticker;
    var tradeDate = (dateInput && dateInput.value) || tradingAgentDemoRun.meta.date;
    var assetType = (activeAsset && activeAsset.getAttribute("data-agent-asset")) || "stock";

    return fetch("/api/trading-agents/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json", "uid": "demo" },
      body: JSON.stringify({ ticker: ticker, trade_date: tradeDate, asset_type: assetType })
    }).then(function (res) {
      if (!res.ok) throw new Error("Backend error " + res.status);
      return res.json();
    }).then(function (data) {
      var runId = data.run_id;
      var evtSource = new EventSource("/api/trading-agents/runs/" + runId + "/events");

      evtSource.onmessage = function (event) {
        try {
          var msg = JSON.parse(event.data);
          handleAgentSSEEvent(msg);
          if (msg.type === "run_completed" || msg.type === "run_error") {
            evtSource.close();
          }
        } catch (e) {
          console.error("[TradingAgents] SSE parse error:", e);
        }
      };

      evtSource.onerror = function () {
        evtSource.close();
        // If run hasn't completed yet, show error state
        if (agentRunStarted && !agentRunCompleted) {
          agentRunStarted = false;
          if (agentActivityTimer) {
            window.clearInterval(agentActivityTimer);
            agentActivityTimer = null;
          }
          updateAgentReportState("error", "SSE 连接中断，未收到完整分析结果。");
        }
      };
    });
  }

  function handleAgentSSEEvent(msg) {
    if (msg.type === "run_started") {
      agentRunSource = "后端实时运行";
      markAgentRunning("market");
      pushAgentLiveLog("RUN", "后端已开始执行 TradingAgents 图流程。");
    } else if (msg.type === "node_started") {
      var node = getAgentNodeById(msg.node_id);
      if (node) {
        markAgentRunning(msg.node_id);
        pushAgentLiveLog(node.title, "节点已启动，正在生成分析内容。");
      }
    } else if (msg.type === "node_completed") {
      var node = getAgentNodeById(msg.node_id);
      if (node) {
        node.state = "done";
        if (msg.content) node.summary = msg.content;
        if (msg.full_content) node.fullContent = msg.full_content;
        if (msg.bullets && msg.bullets.length > 0) node.bullets = msg.bullets;
        pushAgentLiveLog(node.title, msg.content || "节点已完成，结果已进入后续流程。");
        refreshAgentDemoUi();
        markNextPendingRunning();
      }
    } else if (msg.type === "run_completed") {
      if (msg.report) {
        tradingAgentDemoRun.report = msg.report;
      }
      // Mark any remaining nodes as done
      getAgentNodes().forEach(function (n) { n.state = "done"; });
      completeTradingAgentDemo();
    } else if (msg.type === "run_error") {
      agentRunStarted = false;
      if (agentActivityTimer) {
        window.clearInterval(agentActivityTimer);
        agentActivityTimer = null;
      }
      updateAgentReportState("error", msg.message || "TradingAgents run failed.");
      console.error("[TradingAgents] Run error:", msg.code, msg.message);
    }
  }

  function openAgentDrawer(nodeId) {
    var node = getAgentNodeById(nodeId);
    var drawer = document.getElementById("agentDetailDrawer");
    if (!node || !drawer) return;

    var stage = tradingAgentDemoRun.stages.filter(function (item) {
      return item.id === node.stageId;
    })[0];

    var stageEl = document.getElementById("agentDrawerStage");
    var titleEl = document.getElementById("agentDrawerTitle");
    var subtitleEl = document.getElementById("agentDrawerSubtitle");
    var bodyEl = document.getElementById("agentDrawerBody");

    if (stageEl) stageEl.textContent = stage ? stage.title : "AGENT DETAIL";
    if (titleEl) titleEl.textContent = node.title;
    if (subtitleEl) subtitleEl.textContent = "";
    if (bodyEl) {
      var html = '<article><strong>节点产出摘要</strong>';
      // Only show bullets if node has been completed (has real data from backend)
      if (node.state === "done" && node.fullContent) {
        if (node.bullets && node.bullets.length > 0) {
          html += '<ul>';
          node.bullets.forEach(function (bullet) {
            html += '<li>' + escapeHtml(bullet) + '</li>';
          });
          html += '</ul>';
        }
        // Show more button for full content rendered as markdown
        var full = node.fullContent;
        var renderMd = (typeof marked !== "undefined") ? function(s) { return marked.parse(s); } : function(s) { return '<pre style="white-space:pre-wrap;">' + escapeHtml(s) + '</pre>'; };
        var previewLen = 300;
        if (full.length > previewLen) {
          html += '<div class="agent-drawer-expand">';
          html += '<div class="agent-drawer-full-content" style="display:none;max-height:400px;overflow-y:auto;margin-top:8px;padding:12px;background:#f9f9f7;border-radius:8px;font-size:13px;line-height:1.6;">' + renderMd(full) + '</div>';
          html += '<button class="agent-show-more-btn" style="margin-top:8px;padding:4px 12px;font-size:12px;border:1px solid #ddd;border-radius:4px;background:#fff;cursor:pointer;" onclick="(function(btn){var c=btn.previousElementSibling;if(c.style.display===\'none\'){c.style.display=\'block\';btn.textContent=\'收起\';}else{c.style.display=\'none\';btn.textContent=\'展开全文\';}})(this)">展开全文</button>';
          html += '</div>';
        } else {
          html += '<div style="max-height:400px;overflow-y:auto;margin-top:8px;padding:12px;background:#f9f9f7;border-radius:8px;font-size:13px;line-height:1.6;">' + renderMd(full) + '</div>';
        }
      } else {
        html += '<p style="color:#888;">等待节点运行后生成摘要</p>';
      }
      html += '</article>';
      html += '<article><strong>当前状态</strong><ul>';
      html += '<li>' + (node.state === "done" ? "已完成，内容已进入后续节点。" : (node.state === "running" ? "正在运行，等待该节点完成输出。" : "等待上游节点完成后启动。")) + '</li>';
      html += '</ul></article>';
      bodyEl.innerHTML = html;
    }

    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
  }

  function closeAgentDrawer() {
    var drawer = document.getElementById("agentDetailDrawer");
    if (!drawer) return;
    drawer.classList.remove("is-open");
    drawer.setAttribute("aria-hidden", "true");
  }

  function bindAgentDemoEvents() {
    var startBtn = document.getElementById("agentStartBtn");
    var restartBtn = document.getElementById("agentRestartBtn");
    var decisionBtn = document.getElementById("agentDecisionBtn");
    var debateBtn = document.getElementById("agentDebateBtn");
    var stageList = document.getElementById("agentStageList");
    var pipeline = document.getElementById("agentPipeline");
    var drawerCloseButtons = Array.prototype.slice.call(document.querySelectorAll("[data-agent-drawer-close]"));
    var segmentedButtons = Array.prototype.slice.call(document.querySelectorAll(".agent-segmented button"));

    if (startBtn && !startBtn.dataset.agentBound) {
      startBtn.dataset.agentBound = "true";
      startBtn.addEventListener("click", startTradingAgentDemo);
    }

    if (restartBtn && !restartBtn.dataset.agentBound) {
      restartBtn.dataset.agentBound = "true";
      restartBtn.addEventListener("click", startTradingAgentDemo);
    }

    if (decisionBtn && !decisionBtn.dataset.agentBound) {
      decisionBtn.dataset.agentBound = "true";
      decisionBtn.addEventListener("click", function () {
        openAgentDrawer("portfolio-manager");
      });
    }

    if (debateBtn && !debateBtn.dataset.agentBound) {
      debateBtn.dataset.agentBound = "true";
      debateBtn.addEventListener("click", function () {
        openAgentDrawer("research-manager");
      });
    }

    if (stageList && !stageList.dataset.agentBound) {
      stageList.dataset.agentBound = "true";
      stageList.addEventListener("click", function (event) {
        var button = event.target.closest(".agent-node[data-agent-id]");
        if (!button) return;
        openAgentDrawer(button.getAttribute("data-agent-id"));
      });
    }

    if (pipeline && !pipeline.dataset.agentBound) {
      pipeline.dataset.agentBound = "true";
      pipeline.addEventListener("click", function (event) {
        var button = event.target.closest(".agent-pipeline-node[data-agent-id]");
        if (!button) return;
        openAgentDrawer(button.getAttribute("data-agent-id"));
      });
    }

    drawerCloseButtons.forEach(function (button) {
      if (button.dataset.agentBound) return;
      button.dataset.agentBound = "true";
      button.addEventListener("click", closeAgentDrawer);
    });

    segmentedButtons.forEach(function (button) {
      if (button.dataset.agentBound) return;
      button.dataset.agentBound = "true";
      button.addEventListener("click", function () {
        var group = button.closest(".agent-segmented");
        if (!group) return;
        Array.prototype.slice.call(group.querySelectorAll("button")).forEach(function (item) {
          item.classList.toggle("is-active", item === button);
        });
      });
    });
  }

  window.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeFactorCalendar();
      closeAgentDrawer();
    }
  });

  /* ─── Init ─── */
  setActiveRoute("/factor-lab", false);
  startFactorLabAutoRefresh();
})();

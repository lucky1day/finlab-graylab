(function () {
  "use strict";

  var routeToView = {
    "/": "factor-lab",
    "/quantflow": "factor-lab",
    "/quantflow/": "factor-lab",
    "/factor-lab": "factor-lab"
  };

  var viewToRoute = {
    "factor-lab": "/"
  };

  var shell = document.getElementById("aifin-shell");
  var views = Array.prototype.slice.call(document.querySelectorAll("[data-view]"));
  var routeButtons = Array.prototype.slice.call(document.querySelectorAll("button[data-route]"));
  var isRouting = false;
  var reduceMotionQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

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
    var viewName = routeToView[normalized] || "factor-lab";

    views.forEach(function (view) {
      view.classList.toggle("is-active", view.getAttribute("data-view") === viewName);
    });

    routeButtons.forEach(function (button) {
      var target = normalizeRoute(button.getAttribute("data-route") || "/");
      var targetView = routeToView[target];
      var isActive = targetView === viewName;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-current", isActive ? "page" : "false");
    });

    if (shouldPush && window.history && window.history.pushState) {
      var nextRoute = viewToRoute[viewName] || "/";
      window.history.pushState({ view: viewName }, "", nextRoute);
    }

    if (shell) {
      shell.setAttribute("data-active-view", viewName);
    }

    if (viewName === "factor-lab") {
      renderFactorLab();
    }
  }

  function getViewForRoute(route) {
    return routeToView[normalizeRoute(route)] || "factor-lab";
  }

  function getActiveView() {
    if (shell && shell.getAttribute("data-active-view")) {
      return shell.getAttribute("data-active-view");
    }

    var active = document.querySelector(".view.is-active");
    return active ? active.getAttribute("data-view") : "factor-lab";
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

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  /* ─── Factor Lab Task Matrix ─── */
  var factorLabState = {
    page: 1,
    pageSize: 10,
    selectedTaskKey: "3Y|daily|T+5",
    selectedSchemeId: "",
    rankMetric: "overall",
    rankDirection: "desc",
    compareMetric: "overall",
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
    archived: "已归档",
    validated: "已验证",
    retired: "已退役",
    failed: "失败",
    success: "成功"
  };
  var lifecycleAlertLabels = {
    latest_run_failed: "最近运行失败",
    missing_predictions: "无已批准预测"
  };
  var factorTrendMetrics = [
    { id: "overall", label: "整体准确率", color: "#15623f" },
    { id: "upPrecision", label: "上涨准确率", color: "#2f7ba1" },
    { id: "upRecall", label: "上涨召回率", color: "#b98728" },
    { id: "downPrecision", label: "下跌准确率", color: "#d62828" },
    { id: "downRecall", label: "下跌召回率", color: "#6f5aa8" }
  ];
  var factorCompareMetrics = [
    { id: "overall", label: "整体准确率", source: "overall" },
    { id: "up", label: "上涨准确率", source: "upPrecision" },
    { id: "down", label: "下跌准确率", source: "downPrecision" }
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
  var factorCompareData = null;
  var factorCompareLoading = false;
  var factorCompareError = "";
  var factorCompareRequestKey = "";
  var factorCompareLoadedKey = "";
  var factorLifecycleData = null;
  var factorLifecycleLoading = false;
  var factorLifecycleLoaded = false;
  var factorLifecycleError = "";

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

  function getCompareMetric(metricId) {
    return factorCompareMetrics.filter(function (item) {
      return item.id === metricId;
    })[0] || factorCompareMetrics[0];
  }

  function getCompareMetricSource(metricId) {
    return getCompareMetric(metricId).source;
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

  function getSchemeDisplayName(scheme) {
    return String((scheme && (scheme.display_name || scheme.name || scheme.scheme_name)) || "--");
  }

  function isWeeklyHorizon(frequency, horizon) {
    return (
      String(frequency || "").toLowerCase() === "weekly" ||
      String(horizon) === "NEXT_MONDAY" ||
      Number(horizon) === 6
    );
  }

  function detailGroupMonth(row, frequency, horizon) {
    var sourceDate = isWeeklyHorizon(frequency, horizon)
      ? (row.feature_date || row.predict_date || row.target_date || "")
      : (row.predict_date || row.target_date || "");
    return String(sourceDate).slice(0, 7);
  }

  function detailDisplayDay(row) {
    return String(row.predict_date || row.target_date || row.feature_date || "").slice(5, 10).replace("-", "/");
  }

  function dailyRowsByMonth(rows, frequency, horizon) {
    var grouped = {};
    (rows || []).forEach(function (row) {
      var month = detailGroupMonth(row, frequency, horizon);
      if (!month) return;
      if (!grouped[month]) grouped[month] = [];
      var predictedDirection = normalizeDirection(row.predicted_direction);
      var actualDirection = normalizeDirection(row.actual_direction);
      var isCorrect = row.is_correct;
      if (isCorrect === undefined || isCorrect === null) {
        isCorrect = actualDirection === null || predictedDirection === null ? null : predictedDirection === actualDirection;
      }
      grouped[month].push({
        day: detailDisplayDay(row),
        predictDate: row.predict_date || "",
        targetDate: row.target_date || "",
        runId: row.run_id || null,
        schemeVersion: row.scheme_version || "",
        inputArtifactHash: row.input_artifact_hash || "",
        confidence: row.confidence === null || row.confidence === undefined ? null : Number(row.confidence),
        predicted: directionText(predictedDirection),
        actual: directionText(actualDirection),
        predictedDirection: predictedDirection,
        actualDirection: actualDirection,
        correct: isCorrect === null ? null : Boolean(isCorrect)
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
    if (isWeeklyHorizon(frequency, horizon)) {
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
      var groupedDailyRows = dailyRowsByMonth(scheme.daily_rows || [], scheme.frequency, scheme.horizon);
      var monthlyRows = (scheme.monthly_metrics || []).map(rowFromMetric);
      monthlyRows = appendPendingMonths(monthlyRows, groupedDailyRows);
      var latestRun = scheme.latest_run && scheme.latest_run.date
        ? scheme.latest_run.date.slice(5)
        : (scheme.end_date ? scheme.end_date.slice(5) : "--");
      tasks[taskKey].push({
        id: scheme.id,
        taskKey: taskKey,
        name: getSchemeDisplayName(scheme),
        schemeName: scheme.scheme_name || scheme.name || scheme.scheme_id || "",
        benchmarkLabel: scheme.benchmark_label || scheme.benchmark_id || "",
        dataSourceLabel: scheme.data_source_label || scheme.data_source || "",
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
                  var groupedDailyRows = dailyRowsByMonth(metrics.daily_rows || [], scheme.frequency, scheme.horizon);
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

  function rankingMetricValue(scheme, metricId) {
    var metric = aggregateScheme(scheme);
    if (metricId === "samples") return metric.samples;
    if (metricId === "correct") return metric.correct;
    return metric[metricId];
  }

  function isLowSampleMetric(metric) {
    var samples = Number(metric && metric.samples || 0);
    return samples > 0 && samples < 30;
  }

  function sortRankingSchemes(schemes, metricId, direction) {
    var sortMetric = metricId || "overall";
    var sortDirection = direction === "asc" ? "asc" : "desc";
    var directionFactor = sortDirection === "asc" ? 1 : -1;
    return schemes.slice().map(function (scheme, index) {
      return { scheme: scheme, index: index, value: rankingMetricValue(scheme, sortMetric) };
    }).sort(function (a, b) {
      var aValue = a.value === null || a.value === undefined || a.value === "" ? null : Number(a.value);
      var bValue = b.value === null || b.value === undefined || b.value === "" ? null : Number(b.value);
      if (aValue === null && bValue === null) return a.index - b.index;
      if (aValue === null) return 1;
      if (bValue === null) return -1;
      if (aValue === bValue) return a.index - b.index;
      return aValue < bValue ? -1 * directionFactor : directionFactor;
    }).map(function (item) {
      return item.scheme;
    });
  }

  function sortSchemesByMetric(schemes) {
    return sortRankingSchemes(schemes, factorLabState.rankMetric, factorLabState.rankDirection);
  }

  function latestSchemeVersion(scheme) {
    if (!scheme || !scheme.dailyRowsByMonth) return "";
    var latest = "";
    Object.keys(scheme.dailyRowsByMonth).forEach(function (month) {
      (scheme.dailyRowsByMonth[month] || []).forEach(function (row) {
        if (row.schemeVersion) latest = row.schemeVersion;
      });
    });
    return latest;
  }

  function emptyCompareCell(metricId) {
    return {
      value: null,
      samples: 0,
      correct: 0,
      overall: null,
      up_precision: null,
      down_precision: null,
      className: ""
    };
  }

  function compareCellFromMetric(metric, metricId) {
    var source = getCompareMetricSource(metricId);
    var value = metric[source];
    return {
      value: value === undefined ? null : value,
      samples: Number(metric.samples || 0),
      correct: Number(metric.correct || 0),
      overall: metric.overall === undefined ? null : metric.overall,
      up_precision: metric.upPrecision === undefined ? null : metric.upPrecision,
      down_precision: metric.downPrecision === undefined ? null : metric.downPrecision,
      className: getMetricClass(value)
    };
  }

  function compareCellFromApi(cell, metricId) {
    cell = cell || {};
    var value = cell.value;
    if (value === undefined) {
      if (metricId === "up") value = cell.up_precision;
      else if (metricId === "down") value = cell.down_precision;
      else value = cell.overall;
    }
    return {
      value: value === undefined ? null : value,
      samples: Number(cell.samples || 0),
      correct: Number(cell.correct || 0),
      overall: cell.overall === undefined ? null : cell.overall,
      up_precision: cell.up_precision === undefined ? null : cell.up_precision,
      down_precision: cell.down_precision === undefined ? null : cell.down_precision,
      className: getMetricClass(value)
    };
  }

  function buildLocalCompareMatrix(tasks, frequency, metricId) {
    var tenors = [];
    var schemes = {};
    Object.keys(tasks || {}).forEach(function (taskKey) {
      var task = getTaskByKey(taskKey);
      if (frequency && task.frequency !== frequency) return;
      if (tenors.indexOf(task.target) === -1) tenors.push(task.target);
      (tasks[taskKey] || []).forEach(function (scheme) {
        if (!schemes[scheme.id]) {
          schemes[scheme.id] = {
            scheme_id: scheme.id,
            name: scheme.name,
            status: scheme.status,
            frequency: task.frequency,
            cells: {}
          };
        }
        schemes[scheme.id].cells[task.target] = compareCellFromMetric(aggregateScheme(scheme), metricId);
      });
    });
    tenors.sort(function (a, b) {
      return _compareTenorSortKey(a).localeCompare(_compareTenorSortKey(b));
    });
    return {
      frequency: frequency || "",
      metric: metricId,
      tenors: tenors,
      target_labels: tenors.reduce(function (labels, tenor) {
        labels[tenor] = getTargetDisplayName(tenor);
        return labels;
      }, {}),
      schemes: Object.keys(schemes).map(function (schemeId) {
        var scheme = schemes[schemeId];
        tenors.forEach(function (tenor) {
          if (!scheme.cells[tenor]) scheme.cells[tenor] = emptyCompareCell(metricId);
        });
        return scheme;
      })
    };
  }

  function _compareTenorSortKey(tenor) {
    var numeric = parseInt(String(tenor).replace("Y", ""), 10);
    return (Number.isFinite(numeric) ? String(100 + numeric) : "999") + "|" + tenor;
  }

  function normalizeCompareData(payload, metricId) {
    payload = payload || {};
    var tenors = (payload.tenors || []).slice();
    return {
      frequency: payload.frequency || "",
      metric: metricId,
      tenors: tenors,
      target_labels: payload.target_labels || {},
      schemes: (payload.schemes || []).map(function (scheme) {
        var cells = {};
        tenors.forEach(function (tenor) {
          cells[tenor] = compareCellFromApi(scheme.cells && scheme.cells[tenor], metricId);
        });
        return {
          scheme_id: scheme.scheme_id,
          name: scheme.name || scheme.scheme_id,
          status: scheme.status || "active",
          frequency: scheme.frequency || payload.frequency || "",
          cells: cells
        };
      })
    };
  }

  function normalizeLifecycleCards(items) {
    return (items || []).map(function (item) {
      var status = item.version_status || item.registry_status || "active";
      var alerts = item.alerts || [];
      var latestRun = item.latest_run || {};
      var alertText = alerts.map(function (alert) {
        return lifecycleAlertLabels[alert] || alert;
      }).join(" / ");
      return {
        schemeId: item.scheme_id,
        name: item.name || item.scheme_id,
        schemeVersion: item.scheme_version || "--",
        versionStatus: status,
        statusLabel: factorStatusLabels[status] || status,
        statusClass: "is-" + status,
        latestRunStatus: latestRun.status || "--",
        latestRunDate: latestRun.predict_date || latestRun.date || "--",
        recordsWritten: latestRun.records_written === null || latestRun.records_written === undefined ? "--" : String(latestRun.records_written),
        recentSuccessRate: item.recent_success_rate,
        latestPredictionDate: item.latest_prediction_date || "--",
        hasAlert: alerts.length > 0,
        alertText: alertText
      };
    });
  }

  function buildLocalLifecycleCards(tasks) {
    var schemes = {};
    Object.keys(tasks || {}).forEach(function (taskKey) {
      (tasks[taskKey] || []).forEach(function (scheme) {
        if (schemes[scheme.id]) return;
        schemes[scheme.id] = {
          scheme_id: scheme.id,
          name: scheme.name,
          scheme_version: latestSchemeVersion(scheme) || "",
          version_status: scheme.status || "active",
          latest_run: {
            status: scheme.status || "--",
            predict_date: scheme.latestRun || "--",
            records_written: null
          },
          recent_success_rate: null,
          latest_prediction_date: scheme.latestRun || null,
          alerts: []
        };
      });
    });
    return normalizeLifecycleCards(Object.keys(schemes).map(function (schemeId) {
      return schemes[schemeId];
    }));
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

  function currentCompareFrequency() {
    return getTaskByKey(factorLabState.selectedTaskKey).frequency;
  }

  function currentCompareRequestKey() {
    return [
      currentCompareFrequency(),
      factorLabState.startMonth,
      factorLabState.endMonth,
      factorLabState.compareMetric
    ].join("|");
  }

  function compareApiUrl() {
    var params = [
      "frequency=" + encodeURIComponent(currentCompareFrequency()),
      "start_month=" + encodeURIComponent(factorLabState.startMonth),
      "end_month=" + encodeURIComponent(factorLabState.endMonth),
      "metric=" + encodeURIComponent(factorLabState.compareMetric)
    ];
    return "/api/metrics/compare?" + params.join("&");
  }

  function loadMetricsCompareData(options) {
    if (!window.fetch) return;
    var force = options && options.force === true;
    var key = currentCompareRequestKey();
    if (!force && (factorCompareLoading || factorCompareLoadedKey === key || factorCompareRequestKey === key)) return;
    factorCompareLoading = true;
    factorCompareError = "";
    factorCompareRequestKey = key;
    fetchJson(compareApiUrl())
      .then(function (payload) {
        factorCompareData = payload;
        factorCompareLoadedKey = key;
        factorCompareLoading = false;
        factorCompareError = "";
        renderCompareHeatmap();
      })
      .catch(function (error) {
        factorCompareLoading = false;
        factorCompareError = error.message || "compare API unavailable";
        renderCompareHeatmap();
      });
  }

  function getCompareDataForRender() {
    if (factorCompareData && factorCompareLoadedKey === currentCompareRequestKey()) {
      return normalizeCompareData(factorCompareData, factorLabState.compareMetric);
    }
    return buildLocalCompareMatrix(factorTaskSchemes, currentCompareFrequency(), factorLabState.compareMetric);
  }

  function updateCompareMetricToggles() {
    Array.prototype.slice.call(document.querySelectorAll("[data-factor-compare-metric]")).forEach(function (button) {
      var metricId = button.getAttribute("data-factor-compare-metric");
      var active = metricId === factorLabState.compareMetric;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function renderCompareHeatmap() {
    var host = document.getElementById("factorCompareHeatmap");
    var meta = document.getElementById("factorCompareMeta");
    if (!host) return;
    updateCompareMetricToggles();
    var data = getCompareDataForRender();
    var metric = getCompareMetric(factorLabState.compareMetric);
    if (meta) {
      if (factorCompareLoading) {
        meta.textContent = "正在读取横向对比数据。";
      } else if (factorCompareError) {
        meta.textContent = "API暂不可用，当前显示本地备用矩阵。";
      } else {
        meta.textContent = currentCompareFrequency() + " · " + metric.label + " · " + data.schemes.length + " 个方案";
      }
    }
    if (!data.schemes.length || !data.tenors.length) {
      host.innerHTML = '<div class="factor-trend-empty">暂无可对比的方案数据</div>';
      return;
    }
    var html = '<table class="factor-compare-table"><thead><tr><th>方案</th>';
    data.tenors.forEach(function (tenor) {
      var label = data.target_labels && data.target_labels[tenor] ? data.target_labels[tenor] : getTargetDisplayName(tenor);
      html += '<th>' + escapeHtml(label) + '</th>';
    });
    html += '</tr></thead><tbody>';
    data.schemes.forEach(function (scheme) {
      html += '<tr><td><strong>' + escapeHtml(scheme.name || scheme.scheme_id) + '</strong><span class="factor-status-pill is-' + escapeHtml(scheme.status || "active") + '">' + escapeHtml(factorStatusLabels[scheme.status] || scheme.status || "active") + '</span></td>';
      data.tenors.forEach(function (tenor) {
        var cell = scheme.cells && scheme.cells[tenor] ? scheme.cells[tenor] : emptyCompareCell(factorLabState.compareMetric);
        var className = cell.className || getMetricClass(cell.value);
        html += '<td class="factor-compare-cell ' + className + '">';
        html += '<strong>' + formatPercent(cell.value) + '</strong>';
        html += '<span>' + Number(cell.samples || 0) + ' 样本</span>';
        html += '</td>';
      });
      html += '</tr>';
    });
    html += '</tbody></table>';
    host.innerHTML = html;
  }

  function loadSchemeLifecycleData() {
    if (!window.fetch || factorLifecycleLoading || factorLifecycleLoaded) return;
    factorLifecycleLoading = true;
    factorLifecycleError = "";
    fetchJson("/api/schemes/lifecycle")
      .then(function (payload) {
        factorLifecycleData = payload;
        factorLifecycleLoaded = true;
        factorLifecycleLoading = false;
        factorLifecycleError = "";
        renderLifecycleOverview();
      })
      .catch(function (error) {
        factorLifecycleLoading = false;
        factorLifecycleLoaded = true;
        factorLifecycleError = error.message || "lifecycle API unavailable";
        renderLifecycleOverview();
      });
  }

  function getLifecycleCardsForRender() {
    if (factorLifecycleData && factorLifecycleData.schemes) {
      return normalizeLifecycleCards(factorLifecycleData.schemes);
    }
    return buildLocalLifecycleCards(factorTaskSchemes);
  }

  function renderLifecycleOverview() {
    var host = document.getElementById("factorLifecycleCards");
    var meta = document.getElementById("factorLifecycleMeta");
    if (!host) return;
    var cards = getLifecycleCardsForRender();
    if (meta) {
      if (factorLifecycleLoading) {
        meta.textContent = "正在读取方案健康概览。";
      } else if (factorLifecycleError) {
        meta.textContent = "API暂不可用，当前显示本地备用概览。";
      } else {
        meta.textContent = cards.length + " 个在册方案";
      }
    }
    if (!cards.length) {
      host.innerHTML = '<div class="factor-trend-empty">暂无方案生命周期数据</div>';
      return;
    }
    host.innerHTML = cards.map(function (card) {
      var alertHtml = card.hasAlert ? '<span class="factor-lifecycle-alert">' + escapeHtml(card.alertText) + '</span>' : "";
      return '<article class="factor-lifecycle-card' + (card.hasAlert ? " has-alert" : "") + '">' +
        '<div class="factor-lifecycle-card-head"><strong>' + escapeHtml(card.name) + '</strong><span class="factor-status-pill ' + escapeHtml(card.statusClass) + '">' + escapeHtml(card.statusLabel) + '</span></div>' +
        '<div class="factor-lifecycle-version">' + escapeHtml(card.schemeVersion) + '</div>' +
        '<dl>' +
        '<div><dt>最近运行</dt><dd>' + escapeHtml(card.latestRunStatus) + ' · ' + escapeHtml(card.latestRunDate) + '</dd></div>' +
        '<div><dt>成功率</dt><dd>' + formatPercent(card.recentSuccessRate) + '</dd></div>' +
        '<div><dt>已写记录</dt><dd>' + escapeHtml(card.recordsWritten) + '</dd></div>' +
        '<div><dt>最新预测</dt><dd>' + escapeHtml(card.latestPredictionDate) + '</dd></div>' +
        '</dl>' + alertHtml +
        '</article>';
    }).join("");
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
      var version = latestSchemeVersion(scheme);
      var versionHtml = version ? '<span class="factor-scheme-version">' + escapeHtml(version) + '</span>' : "";
      var lowSampleHtml = isLowSampleMetric(metric) ? '<span class="factor-sample-badge">样本不足</span>' : "";
      var barWidth = clampPercent(metric.overall);
      return '<tr' + selectedClass + ' data-factor-scheme-id="' + escapeHtml(scheme.id) + '">' +
        '<td>' + (index + 1) + '</td>' +
        '<td><strong>' + escapeHtml(scheme.name) + '</strong>' + versionHtml + '</td>' +
        '<td class="' + getMetricClass(metric.overall) + '"><div class="factor-score-cell"><span>' + formatPercent(metric.overall) + '（' + metric.correct + '/' + metric.samples + '）</span><span class="factor-score-bar" aria-hidden="true"><span style="width:' + barWidth.toFixed(1) + '%"></span></span></div></td>' +
        '<td><span class="factor-sample-count">' + metric.samples + '</span>' + lowSampleHtml + '</td>' +
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
    Array.prototype.slice.call(document.querySelectorAll("[data-factor-rank-sort]")).forEach(function (button) {
      var metricId = button.getAttribute("data-factor-rank-sort");
      var active = metricId === factorLabState.rankMetric;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-sort", active ? factorLabState.rankDirection : "none");
      button.setAttribute("data-sort-direction", active ? factorLabState.rankDirection.toUpperCase() : "");
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
    summaryValues[2].textContent = scheme ? getSchemeDisplayName(scheme) : "--";
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
    loadSchemeLifecycleData();
    renderLifecycleOverview();
    renderTaskOverview();
    loadMetricsCompareData();
    renderCompareHeatmap();
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

  function openFactorCalendar(month, trigger) {
    var drawer = document.getElementById("factorCalendarDrawer");
    if (!drawer) return;
    renderFactorDailyRows(month);
    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
    positionFactorCalendarPanel(trigger);
  }

  function positionFactorCalendarPanel(trigger) {
    var drawer = document.getElementById("factorCalendarDrawer");
    var panel = drawer ? drawer.querySelector(".factor-calendar-panel") : null;
    if (!drawer || !panel) return;

    if (trigger) {
      Array.prototype.slice.call(document.querySelectorAll(".factor-calendar-link.is-active")).forEach(function (button) {
        button.classList.remove("is-active");
      });
      trigger.classList.add("is-active");
    }

    var afterPaint = window.requestAnimationFrame || function (callback) { window.setTimeout(callback, 0); };
    afterPaint(function () {
      var margin = 16;
      var gap = 12;
      var drawerRect = drawer.getBoundingClientRect();
      var panelRect = panel.getBoundingClientRect();
      var viewportWidth = window.innerWidth || document.documentElement.clientWidth;
      var viewportHeight = window.innerHeight || document.documentElement.clientHeight;
      var panelWidth = panelRect.width;
      var panelHeight = panelRect.height;
      var left = Math.max(margin, (viewportWidth - panelWidth) / 2);
      var top = margin;

      if (trigger && viewportWidth >= 760) {
        var triggerRect = trigger.getBoundingClientRect();
        var rightSide = triggerRect.right + gap;
        var leftSide = triggerRect.left - panelWidth - gap;

        left = rightSide + panelWidth <= viewportWidth - margin ? rightSide : leftSide;
        left = Math.min(Math.max(left, margin), Math.max(margin, viewportWidth - panelWidth - margin));

        top = triggerRect.top + triggerRect.height / 2 - panelHeight / 2;
        top = Math.min(Math.max(top, margin), Math.max(margin, viewportHeight - panelHeight - margin));
      }

      panel.style.right = "auto";
      panel.style.left = Math.round(left - drawerRect.left) + "px";
      panel.style.top = Math.round(top - drawerRect.top) + "px";

      var title = document.getElementById("factorCalendarTitle");
      if (!title || typeof title.focus !== "function") return;
      title.setAttribute("tabindex", "-1");
      try {
        title.focus({ preventScroll: true });
      } catch (error) {
        title.focus();
      }
    });
  }

  function closeFactorCalendar() {
    var drawer = document.getElementById("factorCalendarDrawer");
    if (!drawer) return;
    drawer.classList.remove("is-open");
    drawer.setAttribute("aria-hidden", "true");
    Array.prototype.slice.call(document.querySelectorAll(".factor-calendar-link.is-active")).forEach(function (button) {
      button.classList.remove("is-active");
    });
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
          factorLabState.rankDirection = "desc";
          factorLabState.selectedSchemeId = "";
          factorLabState.page = 1;
          closeFactorCalendar();
          renderFactorLab();
          return;
        }

        var rankSortButton = event.target.closest("[data-factor-rank-sort]");
        if (rankSortButton) {
          var sortMetric = rankSortButton.getAttribute("data-factor-rank-sort") || "overall";
          if (factorLabState.rankMetric === sortMetric) {
            factorLabState.rankDirection = factorLabState.rankDirection === "asc" ? "desc" : "asc";
          } else {
            factorLabState.rankMetric = sortMetric;
            factorLabState.rankDirection = "desc";
          }
          factorLabState.selectedSchemeId = "";
          factorLabState.page = 1;
          closeFactorCalendar();
          renderFactorLab();
          return;
        }

        var compareMetricButton = event.target.closest("[data-factor-compare-metric]");
        if (compareMetricButton) {
          factorLabState.compareMetric = compareMetricButton.getAttribute("data-factor-compare-metric") || "overall";
          factorCompareData = null;
          factorCompareLoadedKey = "";
          factorCompareRequestKey = "";
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
          openFactorCalendar(calendarButton.getAttribute("data-factor-calendar-month"), calendarButton);
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

  window.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeFactorCalendar();
    }
  });

  window.__factorLabTestHooks = {
    aggregateScheme: aggregateScheme,
    buildLocalCompareMatrix: buildLocalCompareMatrix,
    isLowSampleMetric: isLowSampleMetric,
    normalizeLifecycleCards: normalizeLifecycleCards,
    sortRankingSchemes: sortRankingSchemes
  };

  /* ─── Init ─── */
  setActiveRoute("/factor-lab", false);
  startFactorLabAutoRefresh();
})();

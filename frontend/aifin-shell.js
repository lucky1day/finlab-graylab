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
    startMonth: "2025-01",
    endMonth: "2025-05",
    endMonthPinned: false,
    dataSource: "all",
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
  var DEFAULT_SCHEME_DEPLOYMENT_DATE = "2026/06/01";
  var SCHEME_DEPLOYMENT_DATE_OVERRIDES = {
    weekly_5y_direct_0529: "2026/06/10"
  };

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

  function formatDeploymentDate(value) {
    var text = String(value || DEFAULT_SCHEME_DEPLOYMENT_DATE).trim();
    if (!text) return DEFAULT_SCHEME_DEPLOYMENT_DATE;
    var isoMatch = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (isoMatch) return isoMatch[1] + "/" + isoMatch[2] + "/" + isoMatch[3];
    var slashMatch = text.match(/^(\d{4})\/(\d{2})\/(\d{2})/);
    if (slashMatch) return slashMatch[1] + "/" + slashMatch[2] + "/" + slashMatch[3];
    return text;
  }

  function normalizeIsoDate(value) {
    var match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    return match ? match[1] + "-" + match[2] + "-" + match[3] : "";
  }

  function isWeeklyTask(task) {
    return task && String(task.frequency || "").toLowerCase() === "weekly";
  }

  function liveDividerLabels(scheme, task) {
    var dividerLabel = normalizeIsoDate(scheme && scheme.liveSinceDate);
    var metricSinceLabel = normalizeIsoDate(scheme && scheme.liveMetricSinceDate);
    if (isWeeklyTask(task)) {
      metricSinceLabel = metricSinceLabel || dividerLabel;
    }
    return {
      dividerLabel: dividerLabel,
      metricSinceLabel: metricSinceLabel
    };
  }

  function liveDividerText(scheme, task) {
    var labels = liveDividerLabels(scheme, task);
    var dividerText = labels.dividerLabel ? "实盘发出起点 " + labels.dividerLabel : "实盘起点";
    if (labels.metricSinceLabel && labels.metricSinceLabel !== labels.dividerLabel) {
      dividerText += " · 统计起点 " + labels.metricSinceLabel;
    }
    return dividerText;
  }

  function getSchemeDeploymentDate(scheme) {
    var schemeId = String((scheme && scheme.scheme_id) || "").trim();
    if (schemeId && SCHEME_DEPLOYMENT_DATE_OVERRIDES[schemeId]) {
      return formatDeploymentDate(SCHEME_DEPLOYMENT_DATE_OVERRIDES[schemeId]);
    }
    return formatDeploymentDate(
      scheme && (
        scheme.deploymentDate ||
        scheme.deployedAt ||
        scheme.deployed_at ||
        scheme.deployment_date
      )
    );
  }

  function getSchemeRemark(scheme) {
    return String((scheme && (scheme.remark || scheme.note || scheme.notes)) || "").trim();
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
        deploymentDate: DEFAULT_SCHEME_DEPLOYMENT_DATE,
        remark: "",
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
    if (value === null || value === undefined || value === "") return null;
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
    var sourceDate = row.predict_date || row.target_date || row.feature_date || "";
    return String(sourceDate).slice(0, 7);
  }

  function detailDisplayDay(row, frequency, horizon) {
    var sourceDate = row.predict_date || row.target_date || row.feature_date || "";
    return String(sourceDate).slice(5, 10).replace("-", "/");
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
        day: detailDisplayDay(row, frequency, horizon),
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

  function hasPopulatedTasks(tasks) {
    return Object.keys(tasks || {}).some(function (key) {
      return (tasks[key] || []).length > 0;
    });
  }

  function finishFactorLabDataLoad(tasks, mode) {
    factorTaskSchemes = tasks;
    factorLabRemoteLoaded = true;
    factorLabRemoteLoading = false;
    factorLabApiError = "";
    factorLabDataMode = mode;
    var availableTasks = Object.keys(factorTaskSchemes).filter(function (key) {
      return factorTaskSchemes[key].length > 0;
    });
    if (
      availableTasks.length &&
      (!factorTaskSchemes[factorLabState.selectedTaskKey] || !factorTaskSchemes[factorLabState.selectedTaskKey].length)
    ) {
      factorLabState.selectedTaskKey = availableTasks[0];
    }
    syncFactorMonthRange();
    var months = getFactorAvailableMonths();
    if (months.length && !factorLabState.endMonthPinned) {
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
        schemeId: scheme.scheme_id || scheme.scheme_name || scheme.name || "",
        schemeName: scheme.scheme_name || scheme.name || scheme.scheme_id || "",
        benchmarkLabel: scheme.benchmark_label || scheme.benchmark_id || "",
        dataSourceLabel: scheme.data_source_label || scheme.data_source || "",
        status: normalizeBackendSchemeStatus(scheme.status),
        latestRun: latestRun,
        deploymentDate: getSchemeDeploymentDate(scheme),
        remark: getSchemeRemark(scheme),
        monthlyRows: monthlyRows,
        dailyRowsByMonth: groupedDailyRows
      });
    });
    return tasks;
  }

  function liveMetricKey(schemeId, tenor) {
    return schemeId + "|" + tenor;
  }

  function buildLiveTaskSchemes(payload, metricsByKey) {
    mergeTargetLabels(payload.target_labels);
    var schemes = payload.schemes || [];
    var tasks = initEmptyTaskSchemes();
    schemes.forEach(function (scheme) {
      mergeTargetLabels(scheme.target_labels);
      var column = columnForHorizon(scheme.horizon, scheme.frequency);
      if (!column) return;
      (scheme.tenors || []).forEach(function (tenor) {
        var metrics = metricsByKey[liveMetricKey(scheme.scheme_id, tenor)] || {};
        if (metrics.target_label) factorTargetLabels[tenor] = String(metrics.target_label);
        var groupedDailyRows = dailyRowsByMonth(metrics.daily_rows || [], scheme.frequency, scheme.horizon);
        var monthlyRows = (metrics.monthly_metrics || []).map(rowFromMetric);
        monthlyRows = appendPendingMonths(monthlyRows, groupedDailyRows);
        var liveSinceDate = "";
        var liveMetricSinceDate = "";
        if (metrics.daily_rows && metrics.daily_rows.length) {
          var dates = metrics.daily_rows.map(function (r) { return r.predict_date || ""; }).sort();
          liveSinceDate = dates[0] || "";
          var metricDates = metrics.daily_rows.map(function (r) {
            return r.predict_date || r.target_date || "";
          }).sort();
          liveMetricSinceDate = metricDates[0] || liveSinceDate;
        }
        var taskKey = getTaskKey(tenor, column);
        if (!tasks[taskKey]) tasks[taskKey] = [];
        tasks[taskKey].push({
          id: scheme.scheme_id,
          schemeId: scheme.scheme_id,
          taskKey: taskKey,
          tenor: tenor,
          column: column.id,
          name: scheme.name,
          status: normalizeBackendSchemeStatus(scheme.status),
          latestRun: scheme.last_run ? scheme.last_run.date.slice(5) : "--",
          deploymentDate: getSchemeDeploymentDate(scheme),
          remark: getSchemeRemark(scheme),
          monthlyRows: monthlyRows,
          dailyRowsByMonth: groupedDailyRows,
          liveSinceDate: liveSinceDate,
          liveMetricSinceDate: liveMetricSinceDate
        });
      });
    });
    return tasks;
  }

  function fetchLiveFactorLabTasks() {
    return fetchJson("/api/schemes")
      .then(function (payload) {
        var schemes = payload.schemes || [];
        var metricsByKey = {};
        var requests = [];
        schemes.forEach(function (scheme) {
          var column = columnForHorizon(scheme.horizon, scheme.frequency);
          if (!column) return;
          (scheme.tenors || []).forEach(function (tenor) {
            requests.push(
              fetchJson("/api/metrics/" + encodeURIComponent(scheme.scheme_id) + "?tenor=" + encodeURIComponent(tenor))
                .then(function (metrics) {
                  metricsByKey[liveMetricKey(scheme.scheme_id, tenor)] = metrics;
                })
                .catch(function (error) {
                  error.isLiveMetricError = true;
                  throw error;
                })
            );
          });
        });
        return Promise.all(requests).then(function () {
          return buildLiveTaskSchemes(payload, metricsByKey);
        });
      });
  }

  function loadBacktestFactorLabData() {
    return fetchJson("/api/backtests/factor-lab").then(function (payload) {
      if (payload && payload.schemes && payload.schemes.length) {
        finishFactorLabDataLoad(buildBacktestTaskSchemes(payload), "backtest");
        return true;
      }
      finishFactorLabDataLoad(initEmptyTaskSchemes(), "backtest");
      return false;
    });
  }

  function failFactorLabDataLoad(error) {
    factorTaskSchemes = initEmptyTaskSchemes();
    factorLabApiError = error.message || "API unavailable";
    factorLabRemoteLoaded = true;
    factorLabRemoteLoading = false;
    factorLabDataMode = "live-error";
    renderFactorLab();
    return false;
  }

  function loadFactorLabData(options) {
    var force = options && options.force === true;
    if ((!force && factorLabRemoteLoaded) || factorLabRemoteLoading || !window.fetch) return;
    factorLabRemoteLoading = true;
    // 同时拉取实时和回测，合并展示
    return Promise.all([
      fetchLiveFactorLabTasks().catch(function () { return null; }),
      loadBacktestFactorLabDataSilent().catch(function () { return null; })
    ]).then(function (results) {
      var liveTasks = results[0];
      var backtestTasks = results[1];
      var hasLive = liveTasks && hasPopulatedTasks(liveTasks);
      var hasBacktest = backtestTasks && hasPopulatedTasks(backtestTasks);
      if (!hasLive && !hasBacktest) {
        return failFactorLabDataLoad({ message: "实时和回测接口均暂不可用" });
      }
      var mergedTasks = mergeFactorLabTasks(backtestTasks, liveTasks);
      var mode = "";
      if (hasLive && hasBacktest) mode = "merged";
      else if (hasLive) mode = "live";
      else mode = "backtest";
      finishFactorLabDataLoad(mergedTasks, mode);
      return true;
    });
  }

  function loadBacktestFactorLabDataSilent() {
    return fetchJson("/api/backtests/factor-lab").then(function (payload) {
      if (payload && payload.schemes && payload.schemes.length) {
        return buildBacktestTaskSchemes(payload);
      }
      return null;  // no backtest data, not an error
    });
  }

  function mergeFactorLabTasks(backtestTasks, liveTasks) {
    // 同一方案(scheme_id)的回测和实盘合并为一条记录,月度行加 _source 标记
    var merged = initEmptyTaskSchemes();

    // 先走回测(做底盘),再合实盘
    if (backtestTasks) {
      Object.keys(backtestTasks).forEach(function (taskKey) {
        if (!merged[taskKey]) merged[taskKey] = [];
        (backtestTasks[taskKey] || []).forEach(function (btScheme) {
          var btRows = (btScheme.monthlyRows || []).map(function (r) {
            var row = {};
            Object.keys(r).forEach(function (k) { row[k] = r[k]; });
            row._source = "backtest";
            return row;
          });
          var btDaily = {};
          (btScheme.dailyRowsByMonth ? Object.keys(btScheme.dailyRowsByMonth) : []).forEach(function (m) {
            btDaily[m] = (btScheme.dailyRowsByMonth[m] || []).map(function (dr) {
              var d = {}; Object.keys(dr).forEach(function (k) { d[k] = dr[k]; });
              d._source = "backtest";
              return d;
            });
          });
          var btMonths = btRows.map(function (r) { return r.month; }).sort();
          merged[taskKey].push({
            id: btScheme.schemeId || btScheme.id,
            schemeId: btScheme.schemeId || "",
            taskKey: taskKey,
            name: btScheme.name,
            status: btScheme.status,
            latestRun: btScheme.latestRun,
            deploymentDate: btScheme.deploymentDate || getSchemeDeploymentDate(btScheme),
            remark: btScheme.remark || "",
            monthlyRows: btRows,
            dailyRowsByMonth: btDaily,
            benchmarkLabel: btScheme.benchmarkLabel || "",
            dataSourceLabel: btScheme.dataSourceLabel || "",
            backtestStartMonth: btMonths.length ? btMonths[0] : "",
            backtestEndMonth: btMonths.length ? btMonths[btMonths.length - 1] : "",
            liveSinceDate: "",
            liveMetricSinceDate: ""
          });
        });
      });
    }

    // 合入实盘:同 scheme_id 的合并月度数据,否则追加新条目
    if (liveTasks) {
      Object.keys(liveTasks).forEach(function (taskKey) {
        if (!merged[taskKey]) merged[taskKey] = [];
        (liveTasks[taskKey] || []).forEach(function (liveScheme) {
          var liveSchemaId = liveScheme.schemeId || liveScheme.id || "";
          var matched = false;
          for (var i = 0; i < merged[taskKey].length; i++) {
            var mScheme = merged[taskKey][i];
            if (mScheme.schemeId && mScheme.schemeId === liveSchemaId) {
              // 同方案:实盘行覆盖回测，补足独有月份
              var btOnlyMonths = {};
              mScheme.monthlyRows.forEach(function (r) {
                if (r._source === "backtest") btOnlyMonths[r.month] = r;
              });
              (liveScheme.monthlyRows || []).forEach(function (lr) {
                var lrCopy = {}; Object.keys(lr).forEach(function (k) { lrCopy[k] = lr[k]; });
                lrCopy._source = "live";
                if (btOnlyMonths[lr.month]) {
                  // 同月实盘覆盖回测
                  for (var j = 0; j < mScheme.monthlyRows.length; j++) {
                    if (mScheme.monthlyRows[j].month === lr.month) {
                      mScheme.monthlyRows[j] = lrCopy;
                      break;
                    }
                  }
                } else {
                  mScheme.monthlyRows.push(lrCopy);
                }
              });
              mScheme.monthlyRows.sort(function (a, b) { return (a.month || "").localeCompare(b.month || ""); });
              // daily 同理
              (liveScheme.dailyRowsByMonth ? Object.keys(liveScheme.dailyRowsByMonth) : []).forEach(function (m) {
                if (!mScheme.dailyRowsByMonth) mScheme.dailyRowsByMonth = {};
                mScheme.dailyRowsByMonth[m] = (liveScheme.dailyRowsByMonth[m] || []).map(function (dr) {
                  var d = {}; Object.keys(dr).forEach(function (k) { d[k] = dr[k]; });
                  d._source = "live";
                  return d;
                });
              });
              mScheme.liveSinceDate = liveScheme.liveSinceDate || "";
              mScheme.liveMetricSinceDate = liveScheme.liveMetricSinceDate || liveScheme.liveSinceDate || "";
              mScheme.deploymentDate = liveScheme.deploymentDate || mScheme.deploymentDate || DEFAULT_SCHEME_DEPLOYMENT_DATE;
              mScheme.remark = liveScheme.remark || mScheme.remark || "";
              matched = true;
              break;
            }
          }
          if (!matched) {
            // 仅有实盘,无回测
            var lrOnlyRows = (liveScheme.monthlyRows || []).map(function (r) {
              var row = {}; Object.keys(r).forEach(function (k) { row[k] = r[k]; });
              row._source = "live";
              return row;
            });
            merged[taskKey].push({
              id: liveSchemaId,
              schemeId: liveSchemaId,
              taskKey: taskKey,
              name: liveScheme.name,
              status: liveScheme.status,
              latestRun: liveScheme.latestRun,
              deploymentDate: liveScheme.deploymentDate || DEFAULT_SCHEME_DEPLOYMENT_DATE,
              remark: liveScheme.remark || "",
              monthlyRows: lrOnlyRows,
              dailyRowsByMonth: liveScheme.dailyRowsByMonth || {},
              liveSinceDate: liveScheme.liveSinceDate || "",
              liveMetricSinceDate: liveScheme.liveMetricSinceDate || liveScheme.liveSinceDate || "",
              backtestStartMonth: "",
              backtestEndMonth: ""
            });
          }
        });
      });
    }

    return merged;
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
    var src = factorLabState.dataSource;
    return scheme.monthlyRows.filter(function (row) {
      if (row.month < factorLabState.startMonth || row.month > factorLabState.endMonth) return false;
      if (src === "all") return true;
      return row._source === src;
    });
  }

  function getVisibleDailyRowsForScheme(scheme) {
    if (!scheme || !scheme.dailyRowsByMonth) return [];
    var src = factorLabState.dataSource;
    var rows = [];
    Object.keys(scheme.dailyRowsByMonth).forEach(function (month) {
      if (month < factorLabState.startMonth || month > factorLabState.endMonth) return;
      (scheme.dailyRowsByMonth[month] || []).forEach(function (dr) {
        if (src === "all" || dr._source === src) rows.push(dr);
      });
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

  function renderSchemeRankingRow(scheme, index, metric) {
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
      '<td class="mono">' + escapeHtml(getSchemeDeploymentDate(scheme)) + '</td>' +
      '<td class="factor-remark-cell">' + escapeHtml(getSchemeRemark(scheme)) + '</td>' +
      '</tr>';
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
        meta.textContent = "实时API暂不可用，请检查服务或迁移状态。";
      } else if (factorLabDataMode === "backtest") {
        meta.textContent = "该任务格子下共有 " + schemes.length + " 个历史回测方案。";
      } else if (factorLabDataMode === "merged") {
        meta.textContent = "该任务格子下共有 " + schemes.length + " 个候选方案。";
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
      return renderSchemeRankingRow(scheme, index, metric);
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
    var src = factorLabState.dataSource;
    var months = [];
    // 远程数据已加载时跳过 mock base rows（mock 行无 _source，会污染口径过滤）
    if (!factorLabRemoteLoaded) {
      months = factorDailyBaseRows.concat(factorWeeklyBaseRows).reduce(function (result, row) {
        if (src !== "all" && row._source && row._source !== src) return result;
        if (result.indexOf(row.month) === -1) result.push(row.month);
        return result;
      }, []);
    }
    Object.keys(factorTaskSchemes).forEach(function (key) {
      factorTaskSchemes[key].forEach(function (scheme) {
        scheme.monthlyRows.forEach(function (row) {
          if (src !== "all" && row._source && row._source !== src) return;
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

  function _resetMonthRangeForSource() {
    var months = getFactorAvailableMonths();
    if (!months.length) return;
    factorLabState.startMonth = months[0];
    factorLabState.endMonth = months[months.length - 1];
    factorLabState.endMonthPinned = false;
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
    var srcSelect = document.getElementById("factorDataSource");
    if (srcSelect) srcSelect.value = factorLabState.dataSource;
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

  function getTrendContainerWidth(host) {
    var rect = host && host.getBoundingClientRect ? host.getBoundingClientRect() : null;
    var rectWidth = rect && rect.width ? rect.width : 0;
    return Math.max(960, Math.round(host && host.clientWidth || rectWidth || 0));
  }

  function buildTrendChartLayout(rowCount, containerWidth) {
    var left = 58;
    var right = 24;
    var top = 24;
    var bottom = 46;
    var height = 304;
    var pointGap = 52;
    var minLabelGap = 96;
    var baseWidth = Math.max(960, Number(containerWidth) || 0);
    var width = Math.max(baseWidth, left + right + Math.max(1, rowCount - 1) * pointGap);
    var plotWidth = width - left - right;
    var actualPointGap = rowCount <= 1 ? plotWidth : plotWidth / (rowCount - 1);
    var labelStep = Math.max(1, Math.ceil(minLabelGap / Math.max(1, actualPointGap)));
    if (rowCount > 36) labelStep = Math.max(labelStep, 3);
    return {
      width: Math.round(width),
      height: height,
      left: left,
      right: right,
      top: top,
      bottom: bottom,
      plotWidth: plotWidth,
      plotHeight: height - top - bottom,
      labelStep: labelStep,
      isScrollable: width > baseWidth + 1
    };
  }

  function shouldShowTrendMonthLabel(index, rowCount, labelStep) {
    return index === 0 || index === rowCount - 1 || index % labelStep === 0;
  }

  function trendMonthLabelAnchor(index, rowCount) {
    if (index === 0) return "start";
    if (index === rowCount - 1) return "end";
    return "middle";
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

    var layout = buildTrendChartLayout(rows.length, getTrendContainerWidth(host));
    var width = layout.width;
    var height = layout.height;
    var left = layout.left;
    var right = layout.right;
    var top = layout.top;
    var bottom = layout.bottom;
    var plotWidth = width - left - right;
    var plotHeight = height - top - bottom;
    var trendDividerColor = "#155C3E";
    var x = function (index) {
      return rows.length === 1 ? left + plotWidth / 2 : left + plotWidth * index / (rows.length - 1);
    };
    var y = function (value) {
      return top + (100 - Number(value || 0)) / 100 * plotHeight;
    };

    var svg = '<svg width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="月度指标趋势折线图">';
    [0, 25, 50, 75, 100].forEach(function (tick) {
      var tickY = y(tick);
      svg += '<line class="factor-trend-grid" x1="' + left + '" y1="' + tickY.toFixed(1) + '" x2="' + (width - right) + '" y2="' + tickY.toFixed(1) + '"></line>';
      svg += '<text class="factor-trend-axis" x="' + (left - 12) + '" y="' + (tickY + 4).toFixed(1) + '" text-anchor="end">' + tick + '%</text>';
    });
    var labelStep = layout.labelStep;
    rows.forEach(function (row, index) {
      // 横轴标签防溢出：月份过多时跳隔显示，点位仍完整保留。
      if (!shouldShowTrendMonthLabel(index, rows.length, labelStep)) {
        svg += '<line class="factor-trend-tick" x1="' + x(index).toFixed(1) + '" y1="' + (height - bottom) + '" x2="' + x(index).toFixed(1) + '" y2="' + (height - bottom + 6) + '" stroke="rgba(93,101,111,0.3)"></line>';
        return;
      }
      svg += '<text class="factor-trend-axis" x="' + x(index).toFixed(1) + '" y="' + (height - 14) + '" text-anchor="' + trendMonthLabelAnchor(index, rows.length) + '">' + escapeHtml(row.month) + '</text>';
    });
    // 实盘分隔虚线（仅"全部"口径，找到第一个 live 月份）
    if (factorLabState.dataSource === "all") {
      for (var di = 0; di < rows.length; di++) {
        if (rows[di]._source === "live") {
          var dx = x(di);
          svg += '<line class="factor-trend-divider" x1="' + dx.toFixed(1) + '" y1="' + top + '" x2="' + dx.toFixed(1) + '" y2="' + (height - bottom) + '" stroke-dasharray="6 4" stroke="' + trendDividerColor + '" stroke-width="1.5"></line>';
          svg += '<text class="factor-trend-divider-label" x="' + dx.toFixed(1) + '" y="' + (top - 6) + '" text-anchor="middle" fill="' + trendDividerColor + '">实盘</text>';
          break;
        }
      }
    }
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
    host.innerHTML = '<div class="factor-trend-scroll" role="region" aria-label="月度指标趋势横向滚动区域">' + svg + '</div>';
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
    // 跨页检测回测->实盘分界：找到本页之前的最后一个 _source
    var prevSource = "";
    for (var ri = 0; ri < start && ri < visibleRows.length; ri++) {
      if (visibleRows[ri]._source) prevSource = visibleRows[ri]._source;
    }
    pageRows.forEach(function (row) {
      // 在"全部"口径下，回测到实盘的分界处插入分隔行
      if (factorLabState.dataSource === "all" && prevSource === "backtest" && row._source === "live") {
        html += '<tr class="factor-live-divider"><td colspan="10">&#9660; ' + escapeHtml(liveDividerText(scheme, task)) + '</td></tr>';
      }
      prevSource = row._source || prevSource;
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
    var dateHeader = document.getElementById("factorDailyDateHeader");
    var note = document.getElementById("factorCalendarNote");
    title.textContent = month + (isWeekly ? " 周度验证表" : " 每日验证表");
    meta.textContent = (scheme ? scheme.name : "--") + " · " + task.label;
    if (dateHeader) dateHeader.textContent = isWeekly ? "预测周" : "预测日";
    if (note) note.textContent = isWeekly ? "表内可继续滚动查看该月全部周度预测。" : "表内可继续滚动查看该月全部预测日。";

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
        factorLabState.endMonthPinned = true;
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

    var dataSourceSelect = document.getElementById("factorDataSource");
    if (dataSourceSelect && !dataSourceSelect.dataset.factorBound) {
      dataSourceSelect.dataset.factorBound = "true";
      dataSourceSelect.addEventListener("change", function () {
        factorLabState.dataSource = dataSourceSelect.value || "all";
        factorLabState.selectedSchemeId = "";
        factorLabState.page = 1;
        _resetMonthRangeForSource();
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
    getFactorLabState: function () {
      return {
        apiError: factorLabApiError,
        dataMode: factorLabDataMode,
        endMonth: factorLabState.endMonth,
        selectedTaskKey: factorLabState.selectedTaskKey,
        startMonth: factorLabState.startMonth
      };
    },
    getSelectedScheme: getSelectedScheme,
    getSchemeDeploymentDate: getSchemeDeploymentDate,
    getSchemeRemark: getSchemeRemark,
    isLowSampleMetric: isLowSampleMetric,
    liveDividerTextForTest: liveDividerText,
    loadFactorLabData: loadFactorLabData,
    trendChartLayoutForTest: buildTrendChartLayout,
    trendMonthLabelVisibleForTest: shouldShowTrendMonthLabel,
    renderSchemeRankingRowForTest: renderSchemeRankingRow,
    sortRankingSchemes: sortRankingSchemes
  };

  /* ─── Init ─── */
  setActiveRoute("/factor-lab", false);
  startFactorLabAutoRefresh();
})();

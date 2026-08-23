(function () {
  "use strict";

  var routeToView = {
    "/": "factor-lab",
    "/quantflow": "factor-lab",
    "/quantflow/": "factor-lab",
    "/factor-lab": "factor-lab"
  };
  var PUBLIC_BASE_PATH = "/bond-factor-lab";
  var FACTOR_LAB_HISTORY_START_DATE = "2025-01-01";

  var viewToRoute = {
    "factor-lab": "/"
  };

  var shell = document.getElementById("aifin-shell");
  var views = Array.prototype.slice.call(document.querySelectorAll("[data-view]"));
  var routeButtons = Array.prototype.slice.call(document.querySelectorAll("button[data-route]"));

  /* ─── Routing ─── */
  function publicBasePath() {
    var pathname = (window.location && window.location.pathname) || "/";
    if (pathname === PUBLIC_BASE_PATH || pathname.indexOf(PUBLIC_BASE_PATH + "/") === 0) {
      return PUBLIC_BASE_PATH;
    }
    return "";
  }

  function stripPublicBasePath(pathname) {
    var basePath = publicBasePath();
    if (basePath && (pathname === basePath || pathname.indexOf(basePath + "/") === 0)) {
      return pathname.slice(basePath.length) || "/";
    }
    return pathname;
  }

  function routeUrl(route) {
    var basePath = publicBasePath();
    if (!basePath) {
      return route;
    }
    if (route === "/") {
      return basePath + "/";
    }
    return basePath + route;
  }

  function apiUrl(path) {
    var basePath = publicBasePath();
    if (!basePath || path.indexOf(basePath + "/") === 0) {
      return path;
    }
    return basePath + path;
  }

  function normalizeRoute(pathname) {
    pathname = stripPublicBasePath(pathname);
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
      window.history.pushState({ view: viewName }, "", routeUrl(nextRoute));
    }

    if (shell) {
      shell.setAttribute("data-active-view", viewName);
    }

    if (viewName === "factor-lab") {
      renderFactorLab();
    }
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
    setActiveRoute(route, true);
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
    selectedTaskKey: "3Y|T+5",
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

  var factorTargets = ["1Y", "3Y", "5Y", "7Y", "10Y"];
  var factorDefaultTargetLabels = {
    "1Y": "1Y国债活跃",
    "3Y": "3Y国债活跃",
    "5Y": "5Y国债活跃",
    "7Y": "7Y国债活跃",
    "10Y": "10Y国债活跃"
  };
  var factorTargetLabels = Object.assign(Object.create(null), factorDefaultTargetLabels);
  var factorTaskColumns = [
    { id: "dailyT1", label: "T+1", taskType: "T+1", frequency: "daily", horizon: "T+1" },
    { id: "dailyT5", label: "T+5", taskType: "T+5", frequency: "daily", horizon: "T+5" },
    { id: "weeklyPoint", label: "周收盘", taskType: "weekly_point", frequency: "weekly", horizon: "NEXT_WEEK_FRIDAY" },
    { id: "weeklyAverage", label: "周平均", taskType: "weekly_average", frequency: "weekly", horizon: "NEXT_WEEK_AVERAGE" },
    { id: "monthly", label: "月中收", taskType: "monthly", frequency: "monthly", horizon: "MONTHLY" }
  ];
  var factorTrendMetrics = [
    { id: "overall", label: "整体准确率", color: "#15623f" },
    { id: "upPrecision", label: "上涨准确率", color: "#2f7ba1" },
    { id: "upRecall", label: "上涨召回率", color: "#b98728" },
    { id: "downPrecision", label: "下跌准确率", color: "#d62828" },
    { id: "downRecall", label: "下跌召回率", color: "#6f5aa8" }
  ];

  var factorLabBound = false;
  var factorRemarkTrigger = null;
  var factorLabRemoteLoaded = false;
  var factorLabRemoteLoading = false;
  var factorLabApiError = "";
  var factorLabDataMode = "loading";
  var factorLabDrawerSelection = null;
  var FACTOR_LAB_HEALTHY_REFRESH_MS = 60000;
  var FACTOR_LAB_RETRY_DELAYS_MS = [1000, 2000, 5000, 10000, 30000];
  var factorLabRuntimeState = {
    loadSeq: 0,
    controller: null,
    capability: "unknown",
    committedViewModel: null,
    stale: false,
    consecutiveFailures: 0,
    nextRefreshAt: 0,
    refreshTimer: null,
    visibilityBound: false,
    aggregateCache: new Map(),
    committedAt: 0
  };
  window.__factorLabReady = null;

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
    if (value === null || value === undefined || value === "") return "metric-empty";
    var numeric = Number(value);
    if (!Number.isFinite(numeric)) return "metric-empty";
    return numeric >= 60 ? "metric-high" : "metric-low";
  }

  function getMetricLabel(metricId) {
    var metric = factorTrendMetrics.filter(function (item) {
      return item.id === metricId;
    })[0];
    return metric ? metric.label : "整体准确率";
  }

  function formatDeploymentDate(value) {
    var text = String(value || "").trim();
    if (!text) return "";
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
    return task && (
      String(task.frequency || "").toLowerCase() === "weekly" ||
      task.taskType === "weekly_point" ||
      task.taskType === "weekly_average"
    );
  }

  function isWeeklyAverageTask(task) {
    return task && task.taskType === "weekly_average";
  }

  function liveBacktestCutoffTargetDate(liveScheme) {
    var targetDates = [];
    var dailyRows = liveScheme && liveScheme.dailyRowsByMonth || {};
    Object.keys(dailyRows).forEach(function (month) {
      (dailyRows[month] || []).forEach(function (row) {
        var targetDate = normalizeIsoDate(
          row && (row.targetDate || row.target_date || "")
        );
        if (targetDate) targetDates.push(targetDate);
      });
    });
    (liveScheme && liveScheme.phaseRanges || []).forEach(function (range) {
      var targetDate = normalizeIsoDate(
        range && (range.start_target_date || range.startTargetDate || "")
      );
      if (targetDate) targetDates.push(targetDate);
    });
    targetDates.sort();
    return targetDates.length ? targetDates[0] : "";
  }

  function updateBacktestMonthRange(scheme) {
    var months = (scheme.monthlyRows || []).filter(function (row) {
      return row._source === "backtest" && row.month;
    }).map(function (row) {
      return row.month;
    }).sort();
    scheme.backtestStartMonth = months.length ? months[0] : "";
    scheme.backtestEndMonth = months.length ? months[months.length - 1] : "";
  }

  function trimBacktestAtLiveStart(scheme, liveScheme) {
    var cutoffTargetDate = liveBacktestCutoffTargetDate(liveScheme);
    if (!cutoffTargetDate) return;
    var dailyByMonth = scheme.dailyRowsByMonth || {};
    Object.keys(dailyByMonth).forEach(function (month) {
      dailyByMonth[month] = (dailyByMonth[month] || []).filter(function (row) {
        if (row._source !== "backtest") return true;
        var targetDate = normalizeIsoDate(
          row && (row.targetDate || row.target_date || "")
        );
        return Boolean(targetDate) && targetDate < cutoffTargetDate;
      });
      if (!dailyByMonth[month].length) delete dailyByMonth[month];
    });
    var backtestRowsByMonth = {};
    Object.keys(dailyByMonth).forEach(function (month) {
      var backtestRows = (dailyByMonth[month] || []).filter(function (row) {
        return row._source === "backtest";
      });
      if (backtestRows.length) backtestRowsByMonth[month] = backtestRows;
    });
    var backtestMonthlyRows = monthlyRowsFromGroupedDetails(backtestRowsByMonth).map(
      function (row) {
        row._source = "backtest";
        return row;
      }
    );
    scheme.monthlyRows = (scheme.monthlyRows || []).filter(function (row) {
      return row._source !== "backtest";
    }).concat(backtestMonthlyRows).sort(function (left, right) {
      return String(left.month || "").localeCompare(String(right.month || ""));
    });
    updateBacktestMonthRange(scheme);
  }

  function liveDividerText(scheme) {
    var deploymentValue = scheme && (
      scheme.deploymentIsoDate ||
      scheme.deployedAt ||
      scheme.deployed_at ||
      scheme.deployment_date ||
      scheme.deploymentDate
    );
    var deploymentDate = normalizeIsoDate(
      String(deploymentValue || "").replace(/\//g, "-")
    );
    var candidates = [];
    var dailyRows = scheme && scheme.dailyRowsByMonth || {};
    Object.keys(dailyRows).forEach(function (month) {
      (dailyRows[month] || []).forEach(function (row) {
        if (!row || row._source !== "live") return;
        var predictDate = normalizeIsoDate(row.predictDate || row.predict_date);
        var targetDate = normalizeIsoDate(row.targetDate || row.target_date);
        if (deploymentDate && predictDate > deploymentDate && targetDate) {
          candidates.push({ predictDate: predictDate, targetDate: targetDate });
        }
      });
    });
    candidates.sort(function (left, right) {
      if (left.predictDate !== right.predictDate) {
        return left.predictDate < right.predictDate ? -1 : 1;
      }
      if (left.targetDate !== right.targetDate) {
        return left.targetDate < right.targetDate ? -1 : 1;
      }
      return 0;
    });
    var targetStart = candidates.length ? candidates[0].targetDate : "";
    return targetStart
      ? "实盘预测目标区间：" + targetStart + "开始"
      : "实盘预测目标区间：待产生";
  }

  function getSchemeDeploymentDate(scheme) {
    return formatDeploymentDate(
      scheme && (
        scheme.deploymentDate ||
        scheme.deployedAt ||
        scheme.deployed_at ||
        scheme.deployment_date
      )
    );
  }

  function requireSchemeDeploymentDate(scheme, context) {
    var value = getSchemeDeploymentDate(scheme);
    if (value) return value;
    var id = scheme && (scheme.schemeId || scheme.scheme_id || scheme.id || scheme.base_scheme_id || scheme.name);
    throw new Error((context || "scheme") + " missing deployed_at for " + (id || "unknown"));
  }

  function getSchemeRemark(scheme) {
    return String((scheme && (
      scheme.remark || scheme.note || scheme.notes || scheme.description
    )) || "").trim();
  }

  function getTaskKey(target, column) {
    return target + "|" + column.taskType;
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
      return item.taskType === parts[1];
    })[0] || factorTaskColumns[0];
    return {
      key: taskKey,
      target: parts[0] || "3Y",
      targetLabel: getTargetDisplayName(parts[0] || "3Y"),
      frequency: column.frequency,
      horizon: column.horizon,
      taskType: column.taskType,
      label: getTargetDisplayName(parts[0] || "3Y") + " · " + column.label,
      columnLabel: column.label
    };
  }

  var factorTaskSchemes = initEmptyTaskSchemes();

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

  function getDirectionClass(value) {
    if (value === "涨") return "direction-up";
    if (value === "跌") return "direction-down";
    return "direction-neutral";
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

  function numberOrNull(value) {
    if (value === null || value === undefined) return null;
    var number = Number(value);
    return isFinite(number) ? number : null;
  }

  function requireMetricSamples(row, context) {
    var value = numberOrNull(row && row.metricSamples);
    if (value !== null) return value;
    throw new Error(context + " requires metricSamples");
  }

  function getSchemeDisplayName(scheme) {
    return String((scheme && (scheme.display_name || scheme.name || scheme.scheme_name)) || "--");
  }

  function detailGroupMonth(row, frequency, horizon) {
    var sourceDate = row.target_date || "";
    return String(sourceDate).slice(0, 7);
  }

  function detailDisplayDay(row, frequency, horizon) {
    var sourceDate = row.target_date || "";
    return String(sourceDate).slice(5, 10).replace("-", "/");
  }

  function shortDateLabel(value, fallback) {
    var normalized = normalizeIsoDate(value);
    if (normalized) return normalized.slice(5).replace("-", "/");
    return fallback || "--";
  }

  function dateCellHtml(value, fallback) {
    var label = shortDateLabel(value, fallback);
    var title = normalizeIsoDate(value) || label;
    return '<td class="mono" title="' + escapeHtml(title) + '">' + escapeHtml(label) + '</td>';
  }

  function dailyRowsByMonth(rows, frequency, horizon) {
    var grouped = {};
    (rows || []).forEach(function (row) {
      if (!isFactorLabDisplayRow(row)) return;
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
        featureDate: row.feature_date || "",
        targetDate: row.target_date || "",
        predictionPhase: row.prediction_phase || "",
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

  function isFactorLabDisplayRow(row) {
    var predictDate = normalizeIsoDate(
      row && (row.predictDate || row.predict_date || "")
    );
    return Boolean(predictDate) &&
      predictDate >= FACTOR_LAB_HISTORY_START_DATE;
  }

  function directionCounts(rows, key, metricOnly) {
    var counts = { up: 0, down: 0, flat: 0 };
    (rows || []).forEach(function (row) {
      var predicted = normalizeDirection(row.predictedDirection);
      var value = normalizeDirection(row[key]);
      if (value === null) return;
      if (metricOnly && predicted !== 1 && predicted !== -1) return;
      if (value === 1) counts.up += 1;
      else if (value === -1) counts.down += 1;
      else if (value === 0) counts.flat += 1;
    });
    return counts;
  }

  function metricRowFromSampleRows(month, rows) {
    var metric = metricFromSampleRows(rows);
    var actualCounts = directionCounts(rows, "actualDirection", false);
    var predictedCounts = directionCounts(rows, "predictedDirection", false);
    var metricActualCounts = directionCounts(rows, "actualDirection", true);
    var metricPredictedCounts = directionCounts(rows, "predictedDirection", true);
    return {
      month: month,
      samples: metric.samples,
      metricSamples: metric.metricSamples,
      actualDist: distText(actualCounts),
      predictedDist: distText(predictedCounts),
      actualCounts: actualCounts,
      predictedCounts: predictedCounts,
      metricActualCounts: metricActualCounts,
      metricPredictedCounts: metricPredictedCounts,
      overall: metric.overall,
      correct: metric.correct,
      upPrecision: metric.upPrecision,
      upRecall: metric.upRecall,
      downPrecision: metric.downPrecision,
      downRecall: metric.downRecall
    };
  }

  function monthlyRowsFromGroupedDetails(groupedDailyRows) {
    return Object.keys(groupedDailyRows || {}).sort().map(function (month) {
      var rows = (groupedDailyRows[month] || []).filter(function (row) {
        return normalizeDirection(row.predictedDirection) !== null && normalizeDirection(row.actualDirection) !== null;
      });
      return metricRowFromSampleRows(month, rows);
    });
  }

  function columnForTaskType(taskType) {
    return factorTaskColumns.filter(function (column) {
      return column.taskType === taskType;
    })[0] || null;
  }

  function columnForScheme(scheme) {
    var context = "scheme " + ((scheme && (scheme.scheme_id || scheme.base_scheme_id || scheme.name)) || "unknown");
    var taskType = String((scheme && scheme.task_type) || "").trim();
    if (!taskType) {
      throw new Error(context + " missing task_type");
    }
    var column = columnForTaskType(taskType);
    if (!column) {
      throw new Error(context + " invalid task_type: " + taskType);
    }
    return column;
  }

  function normalizeBackendSchemeStatus(status) {
    if (status === "active") return "active";
    if (status === "paused") return "paused";
    if (status === "archived") return "archived";
    return status || "complete";
  }

  var DASHBOARD_SCHEMA_VERSION = "factor-lab-dashboard-v1";
  var DASHBOARD_ROW_FIELDS = [
    "predict_date",
    "feature_date",
    "target_date",
    "prediction_phase",
    "predicted_direction",
    "actual_direction"
  ];
  var DASHBOARD_TOP_FIELDS = [
    "schema_version",
    "snapshot_id",
    "generated_at",
    "display_until",
    "stale",
    "snapshot_age_ms",
    "row_fields",
    "target_labels",
    "schemes"
  ];
  var DASHBOARD_SCHEME_FIELDS = [
    "scheme_id",
    "base_scheme_id",
    "name",
    "description",
    "horizon",
    "task_type",
    "frequency",
    "target_tenor",
    "target_label",
    "status",
    "deployed_at",
    "owner",
    "signal_status",
    "signal_failure_category",
    "live_rows",
    "backtest"
  ];
  var DASHBOARD_BACKTEST_FIELDS = [
    "benchmark_id",
    "benchmark_label",
    "data_source",
    "data_source_label",
    "latest_run_date",
    "rows"
  ];
  var DASHBOARD_TASK_TYPES = ["T+1", "T+5", "weekly_point", "weekly_average", "monthly"];
  var DASHBOARD_LIVE_PHASES = ["gray_live", "scheduled_live"];
  var DASHBOARD_SIGNAL_STATUSES = ["missing", "not_due", "present"];
  var DASHBOARD_SNAPSHOT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  var DASHBOARD_GENERATED_AT_PATTERN = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/;

  function dashboardDataError(message) {
    return new Error("dashboard schema error: " + message);
  }

  function isDashboardObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function requireExactDashboardFields(value, expected, context) {
    if (!isDashboardObject(value)) {
      throw dashboardDataError(context + " must be an object");
    }
    var actual = Object.keys(value).sort();
    var required = expected.slice().sort();
    if (actual.length !== required.length || actual.some(function (field, index) {
      return field !== required[index];
    })) {
      throw dashboardDataError(context + " fields must match v1 exactly");
    }
  }

  function requireDashboardString(value, context, allowEmpty) {
    if (typeof value !== "string" || (!allowEmpty && !value) ||
        (value && (isDashboardBoundaryCharacter(value.charCodeAt(0)) ||
          isDashboardBoundaryCharacter(value.charCodeAt(value.length - 1))))) {
      throw dashboardDataError(context + " must be a canonical string");
    }
    return value;
  }

  function isDashboardBoundaryCharacter(codepoint) {
    return codepoint <= 0x20 ||
      (codepoint >= 0x7f && codepoint <= 0x9f) ||
      codepoint === 0x85 || codepoint === 0xa0 || codepoint === 0x1680 ||
      (codepoint >= 0x2000 && codepoint <= 0x200a) ||
      codepoint === 0x2028 || codepoint === 0x2029 || codepoint === 0x202f ||
      codepoint === 0x205f || codepoint === 0x3000 || codepoint === 0xfeff;
  }

  function compareUnicodeCodePoints(left, right) {
    var leftPoints = Array.from(String(left));
    var rightPoints = Array.from(String(right));
    var length = Math.min(leftPoints.length, rightPoints.length);
    for (var index = 0; index < length; index += 1) {
      var leftPoint = leftPoints[index].codePointAt(0);
      var rightPoint = rightPoints[index].codePointAt(0);
      if (leftPoint !== rightPoint) return leftPoint < rightPoint ? -1 : 1;
    }
    if (leftPoints.length === rightPoints.length) return 0;
    return leftPoints.length < rightPoints.length ? -1 : 1;
  }

  function requireDashboardInteger(value, context, minimum) {
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
      throw dashboardDataError(context + " must be an integer >= " + minimum);
    }
    return value;
  }

  function requireDashboardIsoDate(value, context) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      throw dashboardDataError(context + " must be an ISO date");
    }
    var year = Number(value.slice(0, 4));
    var month = Number(value.slice(5, 7));
    var day = Number(value.slice(8, 10));
    var daysInMonth = month >= 1 && month <= 12
      ? new Date(Date.UTC(year, month, 0)).getUTCDate()
      : 0;
    if (year < 1 || day < 1 || day > daysInMonth) {
      throw dashboardDataError(context + " must be an ISO date");
    }
    return value;
  }

  function requireDashboardAwareDateTime(value, context) {
    requireDashboardString(value, context, false);
    var match = value.match(DASHBOARD_GENERATED_AT_PATTERN);
    if (!match) throw dashboardDataError(context + " must be an aware ISO datetime");
    requireDashboardIsoDate(match[1], context);
    if (Number(match[2]) > 23 || Number(match[3]) > 59 || Number(match[4]) > 59) {
      throw dashboardDataError(context + " must be an aware ISO datetime");
    }
    if (match[5] !== "Z") {
      var offset = match[5].slice(1).split(":");
      if (Number(offset[0]) > 23 || Number(offset[1]) > 59) {
        throw dashboardDataError(context + " must be an aware ISO datetime");
      }
    }
    return value;
  }

  function requireDashboardDirection(value, context, allowNull) {
    if (allowNull && value === null) return null;
    if (typeof value !== "number" || !Number.isInteger(value) || (value !== -1 && value !== 0 && value !== 1)) {
      throw dashboardDataError(context + " has invalid direction");
    }
    return value;
  }

  function decodeDashboardRows(rows, source, context) {
    if (!Array.isArray(rows)) throw dashboardDataError(context + " must be an array");
    var seenPoints = Object.create(null);
    var lastSortKey = "";
    return rows.map(function (row, index) {
      var rowContext = context + "[" + index + "]";
      if (!Array.isArray(row) || row.length !== DASHBOARD_ROW_FIELDS.length) {
        throw dashboardDataError(rowContext + " must have width " + DASHBOARD_ROW_FIELDS.length);
      }
      var predictDate = requireDashboardIsoDate(row[0], rowContext + ".predict_date");
      var featureDate = requireDashboardIsoDate(row[1], rowContext + ".feature_date");
      var targetDate = requireDashboardIsoDate(row[2], rowContext + ".target_date");
      var predictionPhase = row[3];
      if (source === "live") {
        if (DASHBOARD_LIVE_PHASES.indexOf(predictionPhase) === -1) {
          throw dashboardDataError(rowContext + " has invalid live prediction_phase");
        }
      } else if (predictionPhase !== null) {
        throw dashboardDataError(rowContext + " backtest prediction_phase must be null");
      }
      var predictedDirection = requireDashboardDirection(
        row[4], rowContext + ".predicted_direction", false
      );
      var actualDirection = requireDashboardDirection(
        row[5], rowContext + ".actual_direction", source === "live"
      );
      if (seenPoints[targetDate]) {
        throw dashboardDataError(context + " has duplicate canonical target_date " + targetDate);
      }
      seenPoints[targetDate] = true;
      var sortKey = targetDate + "\u0000" + predictDate;
      if (lastSortKey && sortKey < lastSortKey) {
        throw dashboardDataError(context + " is not canonically sorted");
      }
      lastSortKey = sortKey;
      return {
        predictDate: predictDate,
        featureDate: featureDate,
        targetDate: targetDate,
        predictionPhase: predictionPhase,
        predictedDirection: predictedDirection,
        actualDirection: actualDirection,
        source: source
      };
    });
  }

  function decodeDashboardPayload(payload) {
    requireExactDashboardFields(payload, DASHBOARD_TOP_FIELDS, "payload");
    if (payload.schema_version !== DASHBOARD_SCHEMA_VERSION) {
      throw dashboardDataError("unsupported schema_version");
    }
    if (!Array.isArray(payload.row_fields) ||
        payload.row_fields.length !== DASHBOARD_ROW_FIELDS.length ||
        payload.row_fields.some(function (field, index) {
          return field !== DASHBOARD_ROW_FIELDS[index];
        }) || new Set(payload.row_fields).size !== payload.row_fields.length) {
      throw dashboardDataError("row_fields must match v1 exactly");
    }
    var snapshotId = requireDashboardString(payload.snapshot_id, "snapshot_id", false);
    if (!DASHBOARD_SNAPSHOT_ID_PATTERN.test(snapshotId)) {
      throw dashboardDataError("snapshot_id must be canonical");
    }
    var generatedAt = requireDashboardAwareDateTime(payload.generated_at, "generated_at");
    var displayUntil = requireDashboardIsoDate(payload.display_until, "display_until");
    if (typeof payload.stale !== "boolean") throw dashboardDataError("stale must be boolean");
    var snapshotAgeMs = requireDashboardInteger(payload.snapshot_age_ms, "snapshot_age_ms", 0);

    if (!isDashboardObject(payload.target_labels)) {
      throw dashboardDataError("target_labels must be an object");
    }
    var targetLabels = Object.create(null);
    Object.keys(payload.target_labels).sort(compareUnicodeCodePoints).forEach(function (target) {
      var canonicalTarget = requireDashboardString(target, "target_labels target", false);
      targetLabels[canonicalTarget] = requireDashboardString(
        payload.target_labels[target], "target_labels label", false
      );
    });

    if (!Array.isArray(payload.schemes)) throw dashboardDataError("schemes must be an array");
    var previousSchemeId = "";
    var seenSchemeIds = Object.create(null);
    var schemes = payload.schemes.map(function (scheme, index) {
      var context = "schemes[" + index + "]";
      requireExactDashboardFields(scheme, DASHBOARD_SCHEME_FIELDS, context);
      var schemeId = requireDashboardString(scheme.scheme_id, context + ".scheme_id", false);
      var baseSchemeId = requireDashboardString(
        scheme.base_scheme_id, context + ".base_scheme_id", false
      );
      var targetTenor = requireDashboardString(scheme.target_tenor, context + ".target_tenor", false);
      var horizon = requireDashboardInteger(scheme.horizon, context + ".horizon", 1);
      if (schemeId !== baseSchemeId + "__h" + horizon + "__" + targetTenor) {
        throw dashboardDataError(context + " composite identity is invalid");
      }
      if (seenSchemeIds[schemeId]) throw dashboardDataError("duplicate scheme_id " + schemeId);
      if (previousSchemeId && compareUnicodeCodePoints(schemeId, previousSchemeId) < 0) {
        throw dashboardDataError("schemes must be sorted by scheme_id");
      }
      seenSchemeIds[schemeId] = true;
      previousSchemeId = schemeId;
      if (DASHBOARD_TASK_TYPES.indexOf(scheme.task_type) === -1) {
        throw dashboardDataError(context + ".task_type is invalid");
      }
      if (scheme.status !== "active") throw dashboardDataError(context + ".status must be active");
      if (DASHBOARD_SIGNAL_STATUSES.indexOf(scheme.signal_status) === -1) {
        throw dashboardDataError(context + ".signal_status is invalid");
      }
      var signalFailureCategory = scheme.signal_failure_category;
      if (scheme.signal_status === "missing") {
        signalFailureCategory = requireDashboardString(
          signalFailureCategory, context + ".signal_failure_category", false
        );
      } else if (signalFailureCategory !== null) {
        throw dashboardDataError(
          context + ".signal_failure_category must be null when signal is available"
        );
      }
      var targetLabel = requireDashboardString(scheme.target_label, context + ".target_label", false);
      if (!Object.prototype.hasOwnProperty.call(targetLabels, targetTenor) ||
          targetLabels[targetTenor] !== targetLabel) {
        throw dashboardDataError(context + " target identity is invalid");
      }
      if (typeof scheme.description !== "string") {
        throw dashboardDataError(context + ".description must be a string");
      }
      var decodedScheme = {
        schemeId: schemeId,
        baseSchemeId: baseSchemeId,
        name: requireDashboardString(scheme.name, context + ".name", false),
        owner: requireDashboardString(scheme.owner, context + ".owner", true),
        description: scheme.description,
        horizon: horizon,
        taskType: scheme.task_type,
        frequency: requireDashboardString(scheme.frequency, context + ".frequency", false),
        targetTenor: targetTenor,
        targetLabel: targetLabel,
        status: scheme.status,
        deployedAt: requireDashboardIsoDate(scheme.deployed_at, context + ".deployed_at"),
        signalStatus: scheme.signal_status,
        signalFailureCategory: signalFailureCategory,
        liveRows: decodeDashboardRows(scheme.live_rows, "live", context + ".live_rows"),
        backtest: null
      };
      if (scheme.backtest !== null) {
        requireExactDashboardFields(scheme.backtest, DASHBOARD_BACKTEST_FIELDS, context + ".backtest");
        decodedScheme.backtest = {
          benchmarkId: requireDashboardString(
            scheme.backtest.benchmark_id, context + ".backtest.benchmark_id", false
          ),
          benchmarkLabel: requireDashboardString(
            scheme.backtest.benchmark_label, context + ".backtest.benchmark_label", false
          ),
          dataSource: requireDashboardString(
            scheme.backtest.data_source, context + ".backtest.data_source", false
          ),
          dataSourceLabel: requireDashboardString(
            scheme.backtest.data_source_label, context + ".backtest.data_source_label", false
          ),
          latestRunDate: requireDashboardIsoDate(
            scheme.backtest.latest_run_date, context + ".backtest.latest_run_date"
          ),
          rows: decodeDashboardRows(
            scheme.backtest.rows, "backtest", context + ".backtest.rows"
          )
        };
      }
      return decodedScheme;
    });

    return {
      schemaVersion: payload.schema_version,
      snapshotId: snapshotId,
      generatedAt: generatedAt,
      displayUntil: displayUntil,
      stale: payload.stale,
      snapshotAgeMs: snapshotAgeMs,
      targetLabels: targetLabels,
      schemes: schemes
    };
  }

  function dashboardDetailRow(row) {
    var actualDirection = row.actualDirection;
    return {
      day: String(row.targetDate).slice(5).replace("-", "/"),
      predictDate: row.predictDate,
      featureDate: row.featureDate,
      targetDate: row.targetDate,
      predictionPhase: row.predictionPhase || "",
      runId: null,
      schemeVersion: "",
      inputArtifactHash: "",
      confidence: null,
      predicted: directionText(row.predictedDirection),
      actual: directionText(actualDirection),
      predictedDirection: row.predictedDirection,
      actualDirection: actualDirection,
      correct: actualDirection === null ? null : row.predictedDirection === actualDirection,
      _source: row.source
    };
  }

  function groupDashboardDetails(rows) {
    var grouped = {};
    rows.forEach(function (row) {
      var month = row.targetDate.slice(0, 7);
      if (!grouped[month]) grouped[month] = [];
      grouped[month].push(dashboardDetailRow(row));
    });
    return grouped;
  }

  function dashboardMonthlyRows(grouped, source) {
    return monthlyRowsFromGroupedDetails(grouped).map(function (row) {
      row._source = source;
      return row;
    });
  }

  function deriveDashboardPhaseRanges(rows) {
    return DASHBOARD_LIVE_PHASES.map(function (phase) {
      var phaseRows = rows.filter(function (row) { return row.predictionPhase === phase; });
      if (!phaseRows.length) return null;
      var predictDates = phaseRows.map(function (row) { return row.predictDate; }).sort();
      var targetDates = phaseRows.map(function (row) { return row.targetDate; }).sort();
      return {
        prediction_phase: phase,
        start_predict_date: predictDates[0],
        end_predict_date: predictDates[predictDates.length - 1],
        start_target_date: targetDates[0],
        end_target_date: targetDates[targetDates.length - 1],
        rows: phaseRows.length
      };
    }).filter(Boolean);
  }

  function factorLabVisiblePhaseRanges(phaseRanges, visibleDailyRows) {
    var derivedByPhase = Object.create(null);
    deriveDashboardPhaseRanges(
      visibleDailyRows.map(function (row) {
        return {
          predictionPhase: row.prediction_phase || "",
          predictDate: row.predict_date || "",
          targetDate: row.target_date || ""
        };
      })
    ).forEach(function (range) {
      derivedByPhase[range.prediction_phase] = range;
    });
    (phaseRanges || []).forEach(function (range) {
      var phase = String(range.prediction_phase || "");
      var startPredictDate = normalizeIsoDate(range.start_predict_date);
      var endPredictDate = normalizeIsoDate(range.end_predict_date);
      if (DASHBOARD_LIVE_PHASES.indexOf(phase) === -1 ||
          !startPredictDate ||
          !endPredictDate ||
          endPredictDate < FACTOR_LAB_HISTORY_START_DATE ||
          derivedByPhase[phase]) {
        return;
      }
      if (startPredictDate >= FACTOR_LAB_HISTORY_START_DATE) {
        derivedByPhase[phase] = range;
      }
    });
    return DASHBOARD_LIVE_PHASES.map(function (phase) {
      return derivedByPhase[phase] || null;
    }).filter(Boolean);
  }

  function appendDashboardGroupedRows(destination, grouped) {
    Object.keys(grouped).sort().forEach(function (month) {
      if (!destination[month]) destination[month] = [];
      destination[month] = destination[month].concat(grouped[month]);
    });
  }

  function buildFactorLabViewModel(decoded) {
    var tasks = initEmptyTaskSchemes();
    decoded.schemes.forEach(function (scheme) {
      var column = columnForTaskType(scheme.taskType);
      if (!column) throw dashboardDataError("decoded task_type is invalid");
      var taskKey = getTaskKey(scheme.targetTenor, column);
      if (!tasks[taskKey]) tasks[taskKey] = [];

      var liveRows = scheme.liveRows.filter(isFactorLabDisplayRow);
      var backtestRows = scheme.backtest
        ? scheme.backtest.rows.filter(isFactorLabDisplayRow)
        : [];
      var liveGrouped = groupDashboardDetails(liveRows);
      var livePredictDates = liveRows.map(function (row) { return row.predictDate; }).sort();
      var phaseRanges = deriveDashboardPhaseRanges(liveRows);
      var cutoffTargetDate = liveBacktestCutoffTargetDate({
        dailyRowsByMonth: liveGrouped,
        phaseRanges: phaseRanges
      });
      if (cutoffTargetDate) {
        backtestRows = backtestRows.filter(function (row) {
          return row.targetDate < cutoffTargetDate;
        });
      }

      var backtestGrouped = groupDashboardDetails(backtestRows);
      var dailyRowsByMonth = {};
      appendDashboardGroupedRows(dailyRowsByMonth, backtestGrouped);
      appendDashboardGroupedRows(dailyRowsByMonth, liveGrouped);
      Object.keys(dailyRowsByMonth).forEach(function (month) {
        dailyRowsByMonth[month].sort(function (a, b) {
          var sourceRank = { backtest: 0, live: 1 };
          return sourceRank[a._source] - sourceRank[b._source] ||
            a.targetDate.localeCompare(b.targetDate) ||
            a.predictDate.localeCompare(b.predictDate);
        });
      });

      var monthlyRows = dashboardMonthlyRows(backtestGrouped, "backtest")
        .concat(dashboardMonthlyRows(liveGrouped, "live"));
      monthlyRows.sort(function (a, b) {
        var sourceRank = { backtest: 0, live: 1 };
        return a.month.localeCompare(b.month) || sourceRank[a._source] - sourceRank[b._source];
      });
      var backtestMonths = Object.keys(backtestGrouped).sort();
      tasks[taskKey].push({
        id: scheme.schemeId,
        schemeId: scheme.schemeId,
        schemeName: scheme.baseSchemeId,
        taskKey: taskKey,
        targetTenor: scheme.targetTenor,
        column: column.id,
        name: scheme.name,
        owner: scheme.owner || "",
        description: scheme.description,
        status: scheme.status,
        signalStatus: scheme.signalStatus,
        signalFailureCategory: scheme.signalFailureCategory,
        latestRun: livePredictDates.length
          ? livePredictDates[livePredictDates.length - 1].slice(5)
          : (scheme.backtest ? scheme.backtest.latestRunDate.slice(5) : "--"),
        deploymentDate: formatDeploymentDate(scheme.deployedAt),
        remark: scheme.description,
        monthlyRows: monthlyRows,
        dailyRowsByMonth: dailyRowsByMonth,
        liveSinceDate: livePredictDates.length ? livePredictDates[0] : "",
        liveMetricSinceDate: livePredictDates.length ? livePredictDates[0] : "",
        phaseRanges: phaseRanges,
        benchmarkLabel: scheme.backtest ? scheme.backtest.benchmarkLabel : "",
        dataSourceLabel: scheme.backtest ? scheme.backtest.dataSourceLabel : "",
        backtestLatestRunDate: scheme.backtest ? scheme.backtest.latestRunDate : "",
        backtestStartMonth: backtestMonths.length ? backtestMonths[0] : "",
        backtestEndMonth: backtestMonths.length ? backtestMonths[backtestMonths.length - 1] : ""
      });
    });
    Object.keys(tasks).forEach(function (taskKey) {
      tasks[taskKey].sort(function (a, b) {
        return compareUnicodeCodePoints(a.schemeId, b.schemeId);
      });
    });
    return {
      snapshotId: decoded.snapshotId,
      generatedAt: decoded.generatedAt,
      displayUntil: decoded.displayUntil,
      stale: decoded.stale,
      snapshotAgeMs: decoded.snapshotAgeMs,
      targetLabels: decoded.targetLabels,
      tasks: tasks
    };
  }

  function fetchJson(url, options) {
    var requestUrl = apiUrl(url);
    var requestOptions = {
      cache: "no-store",
      headers: { Accept: "application/json" }
    };
    if (options && options.signal) requestOptions.signal = options.signal;
    return fetch(requestUrl, requestOptions).then(function (response) {
      if (response.ok) return response.json();
      var bodyPromise = typeof response.json === "function"
        ? response.json().catch(function () { return null; })
        : Promise.resolve(null);
      return bodyPromise.then(function (body) {
        var error = new Error("HTTP " + response.status + " " + requestUrl);
        error.name = "FactorLabHttpError";
        error.status = response.status;
        error.body = body;
        error.requestUrl = requestUrl;
        throw error;
      });
    });
  }

  function hasPopulatedTasks(tasks) {
    return Object.keys(tasks || {}).some(function (key) {
      return (tasks[key] || []).length > 0;
    });
  }

  function finishFactorLabDataLoad(tasks, mode, targetLabels) {
    validateFactorLabTasksForCommit(tasks);
    var nextTargetLabels = Object.assign(Object.create(null), factorDefaultTargetLabels);
    Object.keys(targetLabels || {}).forEach(function (target) {
      if (targetLabels[target]) nextTargetLabels[target] = String(targetLabels[target]);
    });
    factorTargetLabels = nextTargetLabels;
    factorTaskSchemes = tasks;
    factorLabRemoteLoaded = true;
    factorLabRemoteLoading = false;
    factorLabApiError = "";
    factorLabDataMode = mode;
    factorLabRuntimeState.aggregateCache.clear();
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

  function validateFactorLabTasksForCommit(tasks) {
    Object.keys(tasks || {}).forEach(function (taskKey) {
      (tasks[taskKey] || []).forEach(function (scheme) {
        requireSchemeDeploymentDate(scheme, "dashboard scheme");
        var detailRows = 0;
        Object.keys(scheme.dailyRowsByMonth || {}).forEach(function (month) {
          detailRows += (scheme.dailyRowsByMonth[month] || []).length;
        });
        if ((scheme.monthlyRows || []).length && detailRows === 0) {
          throw new Error("scheme " + (scheme.schemeId || scheme.id || "") +
            " has monthly metrics but no detail rows");
        }
      });
    });
  }

  function buildBacktestTaskSchemes(payload) {
    mergeTargetLabels(payload.target_labels);
    var tasks = initEmptyTaskSchemes();
    (payload.schemes || []).forEach(function (scheme) {
      var column = columnForScheme(scheme);
      var targetTenor = scheme.target_tenor || "";
      if (!targetTenor) return;
      if (scheme.target_label) factorTargetLabels[targetTenor] = String(scheme.target_label);
      var taskKey = getTaskKey(targetTenor, column);
      if (!tasks[taskKey]) tasks[taskKey] = [];
      var groupedDailyRows = dailyRowsByMonth(scheme.daily_rows || [], scheme.frequency, scheme.horizon);
      if ((!scheme.daily_rows || !scheme.daily_rows.length) && scheme.monthly_metrics && scheme.monthly_metrics.length) {
        throw new Error("backtest scheme " + (scheme.scheme_id || "") + " has monthly_metrics but no detail rows");
      }
      var monthlyRows = monthlyRowsFromGroupedDetails(groupedDailyRows);
      var latestRun = scheme.latest_run && scheme.latest_run.date
        ? scheme.latest_run.date.slice(5)
        : (scheme.end_date ? scheme.end_date.slice(5) : "--");
      tasks[taskKey].push({
        id: scheme.id,
        taskKey: taskKey,
        name: String(scheme.scheme_name || getSchemeDisplayName(scheme)),
        schemeId: scheme.scheme_id || "",
        schemeName: scheme.base_scheme_id || scheme.scheme_name || scheme.name || scheme.scheme_id || "",
        benchmarkLabel: scheme.benchmark_label || scheme.benchmark_id || "",
        dataSourceLabel: scheme.data_source_label || scheme.data_source || "",
        status: normalizeBackendSchemeStatus(scheme.status),
        latestRun: latestRun,
        deploymentDate: requireSchemeDeploymentDate(scheme, "backtest scheme"),
        remark: getSchemeRemark(scheme),
        monthlyRows: monthlyRows,
        dailyRowsByMonth: groupedDailyRows
      });
    });
    return tasks;
  }

  function latestRunFromMetricRows(rows) {
    var dates = (rows || []).map(function (row) {
      return row.predict_date || row.predictDate || row.target_date || row.targetDate || "";
    }).filter(Boolean).sort();
    return dates.length ? String(dates[dates.length - 1]).slice(5) : "--";
  }

  function buildLiveTaskSchemes(payload, metricsByKey) {
    if (payload && !Array.isArray(payload)) mergeTargetLabels(payload.target_labels);
    var schemes = Array.isArray(payload) ? payload : (payload.schemes || []);
    var tasks = initEmptyTaskSchemes();
    schemes.forEach(function (scheme) {
      var column = columnForScheme(scheme);
      var targetTenor = scheme.target_tenor || "";
      if (!targetTenor) return;
      var metrics = metricsByKey[scheme.scheme_id] || {};
      if (metrics.target_label) factorTargetLabels[targetTenor] = String(metrics.target_label);
      var visibleDailyRows = (metrics.daily_rows || []).filter(
        isFactorLabDisplayRow
      );
      var groupedDailyRows = dailyRowsByMonth(
        visibleDailyRows,
        scheme.frequency,
        scheme.horizon
      );
      if ((!metrics.daily_rows || !metrics.daily_rows.length) && metrics.monthly_metrics && metrics.monthly_metrics.length) {
        throw new Error("live scheme " + (scheme.scheme_id || "") + " has monthly_metrics but no detail rows");
      }
      var monthlyRows = monthlyRowsFromGroupedDetails(groupedDailyRows);
      var liveSinceDate = "";
      var liveMetricSinceDate = "";
      if (visibleDailyRows.length) {
        var dates = visibleDailyRows.map(function (r) {
          return r.predict_date || "";
        }).filter(Boolean).sort();
        liveSinceDate = dates[0] || "";
        var metricDates = visibleDailyRows.map(function (r) {
          return r.predict_date || r.target_date || "";
        }).filter(Boolean).sort();
        liveMetricSinceDate = metricDates[0] || liveSinceDate;
      }
      var phaseRanges = factorLabVisiblePhaseRanges(
        metrics.phase_ranges || [],
        visibleDailyRows
      );
      var taskKey = getTaskKey(targetTenor, column);
      if (!tasks[taskKey]) tasks[taskKey] = [];
      tasks[taskKey].push({
        id: scheme.scheme_id,
        schemeId: scheme.scheme_id,
        taskKey: taskKey,
        targetTenor: targetTenor,
        column: column.id,
        name: scheme.name,
        status: normalizeBackendSchemeStatus(scheme.status),
        latestRun: latestRunFromMetricRows(visibleDailyRows),
        deploymentDate: requireSchemeDeploymentDate(scheme, "live scheme"),
        remark: getSchemeRemark(scheme),
        monthlyRows: monthlyRows,
        dailyRowsByMonth: groupedDailyRows,
        liveSinceDate: liveSinceDate,
        liveMetricSinceDate: liveMetricSinceDate,
        phaseRanges: phaseRanges
      });
    });
    return tasks;
  }

  function factorLabNow() {
    return window.performance && typeof window.performance.now === "function"
      ? window.performance.now()
      : Date.now();
  }

  function clearFactorLabRefreshTimer() {
    if (factorLabRuntimeState.refreshTimer !== null && window.clearTimeout) {
      window.clearTimeout(factorLabRuntimeState.refreshTimer);
    }
    factorLabRuntimeState.refreshTimer = null;
    factorLabRuntimeState.nextRefreshAt = 0;
  }

  function scheduleFactorLabRefresh(delayMs) {
    clearFactorLabRefreshTimer();
    if (!window.setTimeout || document.visibilityState === "hidden") return;
    factorLabRuntimeState.nextRefreshAt = factorLabNow() + delayMs;
    factorLabRuntimeState.refreshTimer = window.setTimeout(function () {
      factorLabRuntimeState.refreshTimer = null;
      factorLabRuntimeState.nextRefreshAt = 0;
      if (document.visibilityState === "hidden" || getActiveView() !== "factor-lab") return;
      loadFactorLabData({ force: true });
    }, delayMs);
  }

  function isDashboardUnsupported(error) {
    if (!error || error.name !== "FactorLabHttpError") return false;
    if (error.status === 404) return true;
    return error.status === 501 && error.body && error.body.detail &&
      error.body.detail.code === "dashboard_not_supported";
  }

  function wrapLegacyAttempt(promise) {
    return promise.then(
      function (value) { return { ok: true, value: value }; },
      function (error) { return { ok: false, error: error }; }
    );
  }

  function isFactorLabAbortError(error) {
    return Boolean(error && error.name === "AbortError");
  }

  function createLegacyRequestGroup(parentSignal) {
    var childController = window.AbortController ? new window.AbortController() : null;
    var firstError = null;
    var onParentAbort = function () {
      if (childController && !childController.signal.aborted) {
        childController.abort();
      }
    };
    if (childController && parentSignal) {
      if (parentSignal.aborted) onParentAbort();
      else if (parentSignal.addEventListener) parentSignal.addEventListener("abort", onParentAbort);
    }
    return {
      signal: childController ? childController.signal : parentSignal,
      fail: function (error) {
        if (isFactorLabAbortError(error) || firstError) return;
        firstError = error;
        if (childController && !childController.signal.aborted) childController.abort();
      },
      firstError: function () { return firstError; },
      cleanup: function () {
        if (childController && parentSignal && parentSignal.removeEventListener) {
          parentSignal.removeEventListener("abort", onParentAbort);
        }
      }
    };
  }

  function fetchLegacyFactorLabCandidate(signal, seq) {
    var responses = Object.create(null);
    var requestGroup = createLegacyRequestGroup(signal);
    var groupSignal = requestGroup.signal;
    var liveAttempt = wrapLegacyAttempt(
      fetchJson("/api/schemes", { signal: groupSignal }).then(function (schemesPayload) {
        var schemes = Array.isArray(schemesPayload)
          ? schemesPayload
          : ((schemesPayload && schemesPayload.schemes) || []);
        var metricRequests = schemes.map(function (scheme) {
          columnForScheme(scheme);
          if (!scheme.scheme_id || !scheme.target_tenor) return Promise.resolve(null);
          var metricsPath = "/api/metrics/" + encodeURIComponent(scheme.scheme_id);
          return fetchJson(metricsPath, { signal: groupSignal }).then(function (metrics) {
            return { path: metricsPath, payload: metrics };
          });
        });
        return Promise.all(metricRequests).then(function (metrics) {
          return { schemes: schemesPayload, metrics: metrics.filter(Boolean) };
        });
      }).catch(function (error) {
        requestGroup.fail(error);
        throw error;
      })
    );
    var backtestAttempt = wrapLegacyAttempt(
      fetchJson("/api/backtests/factor-lab", { signal: groupSignal }).catch(function (error) {
        requestGroup.fail(error);
        throw error;
      })
    );

    var result = Promise.all([liveAttempt, backtestAttempt]).then(function (attempts) {
      if (seq !== factorLabRuntimeState.loadSeq) return null;
      var live = attempts[0];
      var backtest = attempts[1];
      if (requestGroup.firstError()) throw requestGroup.firstError();
      if (live.ok) {
        responses["/api/schemes"] = live.value.schemes;
        live.value.metrics.forEach(function (metric) {
          responses[metric.path] = metric.payload;
        });
      }
      if (backtest.ok) responses["/api/backtests/factor-lab"] = backtest.value;
      if (!live.ok && !backtest.ok) throw live.error || backtest.error;
      if (!live.ok && isFailClosedDataError(live.error)) throw live.error;
      if (!backtest.ok && isFailClosedDataError(backtest.error)) throw backtest.error;
      var viewModel = buildLegacyFactorLabViewModelForTest(responses);
      return {
        tasks: viewModel.tasks,
        targetLabels: viewModel.targetLabels,
        snapshotId: "legacy-" + seq,
        snapshotAgeMs: 0,
        stale: false,
        source: "legacy",
        mode: viewModel.mode
      };
    });
    return result.then(
      function (candidate) {
        requestGroup.cleanup();
        return candidate;
      },
      function (error) {
        requestGroup.cleanup();
        throw error;
      }
    );
  }

  function dashboardFactorLabCandidate(payload) {
    var decoded = decodeDashboardPayload(payload);
    var viewModel = buildFactorLabViewModel(decoded);
    return {
      tasks: viewModel.tasks,
      targetLabels: viewModel.targetLabels,
      snapshotId: viewModel.snapshotId,
      snapshotAgeMs: viewModel.snapshotAgeMs,
      stale: false,
      source: "dashboard",
      mode: "dashboard"
    };
  }

  function cloneFactorLabUiState() {
    var clone = {};
    Object.keys(factorLabState).forEach(function (key) {
      clone[key] = key === "chartMetrics"
        ? Object.assign({}, factorLabState.chartMetrics)
        : factorLabState[key];
    });
    return clone;
  }

  function restoreFactorLabUiState(snapshot) {
    Object.keys(factorLabState).forEach(function (key) { delete factorLabState[key]; });
    Object.keys(snapshot).forEach(function (key) { factorLabState[key] = snapshot[key]; });
  }

  function hasDrawerSelection(selection) {
    if (!selection) return false;
    var schemes = factorTaskSchemes[selection.taskKey] || [];
    return schemes.some(function (scheme) {
      return scheme.id === selection.schemeId && scheme.dailyRowsByMonth &&
        (scheme.dailyRowsByMonth[selection.month] || []).length > 0;
    });
  }

  function countFactorLabRows(tasks) {
    var counts = { schemes: 0, liveRows: 0, backtestRows: 0 };
    Object.keys(tasks || {}).forEach(function (taskKey) {
      (tasks[taskKey] || []).forEach(function (scheme) {
        counts.schemes += 1;
        Object.keys(scheme.dailyRowsByMonth || {}).forEach(function (month) {
          (scheme.dailyRowsByMonth[month] || []).forEach(function (row) {
            if (row._source === "live") counts.liveRows += 1;
            else if (row._source === "backtest") counts.backtestRows += 1;
          });
        });
      });
    });
    return counts;
  }

  function commitFactorLabCandidate(candidate, seq) {
    if (!candidate || seq !== factorLabRuntimeState.loadSeq) return false;
    validateFactorLabTasksForCommit(candidate.tasks);
    var nextTargetLabels = Object.assign(Object.create(null), factorDefaultTargetLabels);
    Object.keys(candidate.targetLabels || {}).forEach(function (target) {
      if (candidate.targetLabels[target]) nextTargetLabels[target] = String(candidate.targetLabels[target]);
    });

    var previous = {
      tasks: factorTaskSchemes,
      labels: factorTargetLabels,
      remoteLoaded: factorLabRemoteLoaded,
      remoteLoading: factorLabRemoteLoading,
      apiError: factorLabApiError,
      dataMode: factorLabDataMode,
      committed: factorLabRuntimeState.committedViewModel,
      stale: factorLabRuntimeState.stale,
      committedAt: factorLabRuntimeState.committedAt,
      aggregateCache: factorLabRuntimeState.aggregateCache,
      ui: cloneFactorLabUiState(),
      drawer: factorLabDrawerSelection
    };
    var nextCache = previous.committed && previous.committed.snapshotId === candidate.snapshotId
      ? previous.aggregateCache
      : new Map();
    try {
      factorTaskSchemes = candidate.tasks;
      factorTargetLabels = nextTargetLabels;
      factorLabRemoteLoaded = true;
      factorLabRemoteLoading = false;
      factorLabApiError = "";
      factorLabDataMode = candidate.mode;
      factorLabRuntimeState.committedViewModel = candidate;
      factorLabRuntimeState.stale = false;
      factorLabRuntimeState.committedAt = factorLabNow();
      factorLabRuntimeState.aggregateCache = nextCache;

      var availableTasks = Object.keys(factorTaskSchemes).filter(function (key) {
        return factorTaskSchemes[key].length > 0;
      });
      if (availableTasks.length &&
          (!factorTaskSchemes[factorLabState.selectedTaskKey] ||
            !factorTaskSchemes[factorLabState.selectedTaskKey].length)) {
        factorLabState.selectedTaskKey = availableTasks[0];
      }
      syncFactorMonthRange();
      var months = getFactorAvailableMonths();
      if (months.length && !factorLabState.endMonthPinned) {
        factorLabState.endMonth = months[months.length - 1];
      }
      renderFactorLab();
      if (hasDrawerSelection(previous.drawer)) {
        factorLabState.selectedTaskKey = previous.drawer.taskKey;
        factorLabState.selectedSchemeId = previous.drawer.schemeId;
        openFactorCalendar(previous.drawer.month, null);
      } else if (previous.drawer) {
        closeFactorCalendar();
      }
    } catch (error) {
      factorTaskSchemes = previous.tasks;
      factorTargetLabels = previous.labels;
      factorLabRemoteLoaded = previous.remoteLoaded;
      factorLabRemoteLoading = previous.remoteLoading;
      factorLabApiError = previous.apiError;
      factorLabDataMode = previous.dataMode;
      factorLabRuntimeState.committedViewModel = previous.committed;
      factorLabRuntimeState.stale = previous.stale;
      factorLabRuntimeState.committedAt = previous.committedAt;
      factorLabRuntimeState.aggregateCache = previous.aggregateCache;
      factorLabDrawerSelection = previous.drawer;
      restoreFactorLabUiState(previous.ui);
      throw error;
    }

    factorLabRuntimeState.consecutiveFailures = 0;
    scheduleFactorLabRefresh(FACTOR_LAB_HEALTHY_REFRESH_MS);
    var rowCounts = countFactorLabRows(candidate.tasks);
    var readyFrame = window.requestAnimationFrame || function (callback) {
      return window.setTimeout(callback, 0);
    };
    readyFrame(function () {
      if (seq !== factorLabRuntimeState.loadSeq ||
          factorLabRuntimeState.committedViewModel !== candidate) return;
      window.__factorLabReady = {
        seq: seq,
        snapshotId: candidate.snapshotId,
        committedAt: factorLabNow(),
        stale: false,
        source: candidate.source,
        mode: candidate.source,
        schemeCount: rowCounts.schemes,
        liveRowCount: rowCounts.liveRows,
        backtestRowCount: rowCounts.backtestRows
      };
    });
    return true;
  }

  function failFactorLabDataLoad(error, seq) {
    if (seq !== undefined && seq !== factorLabRuntimeState.loadSeq) return false;
    factorLabApiError = (error && error.message) || "API unavailable";
    factorLabRemoteLoaded = true;
    factorLabRemoteLoading = false;
    factorLabRuntimeState.controller = null;
    factorLabRuntimeState.consecutiveFailures += 1;
    factorTaskSchemes = initEmptyTaskSchemes();
    factorTargetLabels = Object.assign(Object.create(null), factorDefaultTargetLabels);
    factorLabRuntimeState.committedViewModel = null;
    factorLabRuntimeState.stale = false;
    factorLabRuntimeState.committedAt = 0;
    factorLabRuntimeState.aggregateCache = new Map();
    factorLabDataMode = "error";
    closeFactorCalendar();
    renderFactorLab();
    var delayIndex = Math.min(
      factorLabRuntimeState.consecutiveFailures - 1,
      FACTOR_LAB_RETRY_DELAYS_MS.length - 1
    );
    scheduleFactorLabRefresh(FACTOR_LAB_RETRY_DELAYS_MS[delayIndex]);
    return false;
  }

  function loadFactorLabData(options) {
    var force = options && options.force === true;
    if (!window.fetch) return Promise.resolve(false);
    if (!force && (factorLabRemoteLoaded || factorLabRemoteLoading)) {
      return Promise.resolve(false);
    }

    clearFactorLabRefreshTimer();
    var seq = factorLabRuntimeState.loadSeq + 1;
    factorLabRuntimeState.loadSeq = seq;
    window.__factorLabReady = null;
    if (factorLabRuntimeState.controller &&
        typeof factorLabRuntimeState.controller.abort === "function") {
      factorLabRuntimeState.controller.abort("superseded");
    }
    var controller = window.AbortController ? new window.AbortController() : null;
    factorLabRuntimeState.controller = controller;
    factorLabRemoteLoading = true;
    renderFactorLabDataStatus();
    var signal = controller ? controller.signal : null;

    var candidatePromise;
    if (factorLabRuntimeState.capability === "legacy") {
      candidatePromise = fetchLegacyFactorLabCandidate(signal, seq);
    } else {
      candidatePromise = fetchJson("/api/factor-lab/dashboard", { signal: signal })
        .then(function (payload) {
          if (seq !== factorLabRuntimeState.loadSeq) return null;
          var candidate = dashboardFactorLabCandidate(payload);
          if (seq === factorLabRuntimeState.loadSeq) {
            factorLabRuntimeState.capability = "dashboard";
          }
          return candidate;
        })
        .catch(function (error) {
          if (seq !== factorLabRuntimeState.loadSeq) throw error;
          if (factorLabRuntimeState.capability === "unknown" && isDashboardUnsupported(error)) {
            factorLabRuntimeState.capability = "legacy";
            return fetchLegacyFactorLabCandidate(signal, seq);
          }
          throw error;
        });
    }

    return candidatePromise.then(function (candidate) {
      if (seq !== factorLabRuntimeState.loadSeq || !candidate) return false;
      var committed = commitFactorLabCandidate(candidate, seq);
      if (seq === factorLabRuntimeState.loadSeq) factorLabRuntimeState.controller = null;
      return committed;
    }).catch(function (error) {
      if (seq !== factorLabRuntimeState.loadSeq) return false;
      factorLabRuntimeState.controller = null;
      return failFactorLabDataLoad(error, seq);
    });
  }

  function isFailClosedDataError(error) {
    var message = String((error && error.message) || error || "");
    return message.indexOf("missing deployed_at") !== -1 ||
      message.indexOf("task_type") !== -1 ||
      message.indexOf("has monthly_metrics but no detail rows") !== -1 ||
      message.indexOf("requires metric_") !== -1 ||
      message.indexOf("requires metricSamples") !== -1;
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
            deploymentDate: requireSchemeDeploymentDate(btScheme, "backtest scheme"),
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
              trimBacktestAtLiveStart(mScheme, liveScheme);
              var btOnlyMonths = {};
              mScheme.monthlyRows.forEach(function (r) {
                if (r._source === "backtest") btOnlyMonths[r.month] = r;
              });
              (liveScheme.monthlyRows || []).forEach(function (lr) {
                var lrCopy = {}; Object.keys(lr).forEach(function (k) { lrCopy[k] = lr[k]; });
                lrCopy._source = "live";
                if (btOnlyMonths[lr.month]) {
                  // 同月既有回测也有实盘:保留回测行,追加实盘行（两行展示）
                  mScheme.monthlyRows.push(lrCopy);
                } else {
                  mScheme.monthlyRows.push(lrCopy);
                }
              });
              mScheme.monthlyRows.sort(function (a, b) { return (a.month || "").localeCompare(b.month || ""); });
              // daily 同理
              (liveScheme.dailyRowsByMonth ? Object.keys(liveScheme.dailyRowsByMonth) : []).forEach(function (m) {
                if (!mScheme.dailyRowsByMonth) mScheme.dailyRowsByMonth = {};
                var existingRows = mScheme.dailyRowsByMonth[m] || [];
                var liveRows = (liveScheme.dailyRowsByMonth[m] || []).map(function (dr) {
                  var d = {}; Object.keys(dr).forEach(function (k) { d[k] = dr[k]; });
                  d._source = "live";
                  return d;
                });
                mScheme.dailyRowsByMonth[m] = existingRows.concat(liveRows);
              });
              mScheme.liveSinceDate = liveScheme.liveSinceDate || "";
              mScheme.liveMetricSinceDate = liveScheme.liveMetricSinceDate || liveScheme.liveSinceDate || "";
              mScheme.phaseRanges = liveScheme.phaseRanges || [];
              mScheme.latestRun = liveScheme.latestRun || mScheme.latestRun || "--";
              mScheme.deploymentDate = requireSchemeDeploymentDate(
                { schemeId: liveSchemaId, deploymentDate: liveScheme.deploymentDate || mScheme.deploymentDate },
                "merged scheme"
              );
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
            var lrOnlyDaily = {};
            Object.keys(liveScheme.dailyRowsByMonth || {}).forEach(function (month) {
              lrOnlyDaily[month] = (liveScheme.dailyRowsByMonth[month] || []).map(function (dr) {
                var dailyRow = {};
                Object.keys(dr).forEach(function (key) { dailyRow[key] = dr[key]; });
                dailyRow._source = "live";
                return dailyRow;
              });
            });
            merged[taskKey].push({
              id: liveSchemaId,
              schemeId: liveSchemaId,
              taskKey: taskKey,
              name: liveScheme.name,
              status: liveScheme.status,
              latestRun: liveScheme.latestRun,
              deploymentDate: requireSchemeDeploymentDate(liveScheme, "live scheme"),
              remark: liveScheme.remark || "",
              monthlyRows: lrOnlyRows,
              dailyRowsByMonth: lrOnlyDaily,
              liveSinceDate: liveScheme.liveSinceDate || "",
              liveMetricSinceDate: liveScheme.liveMetricSinceDate || liveScheme.liveSinceDate || "",
              phaseRanges: liveScheme.phaseRanges || [],
              backtestStartMonth: "",
              backtestEndMonth: ""
            });
          }
        });
      });
    }

    return merged;
  }

  function buildLegacyFactorLabViewModelForTest(responses) {
    responses = responses || {};
    var originalLabels = factorTargetLabels;
    factorTargetLabels = Object.assign({}, originalLabels);
    try {
      var liveTasks = null;
      var schemesPayload = responses["/api/schemes"];
      if (schemesPayload) {
        var schemes = Array.isArray(schemesPayload)
          ? schemesPayload
          : (schemesPayload.schemes || []);
        var metricsByKey = {};
        schemes.forEach(function (scheme) {
          if (!scheme.scheme_id) return;
          var metricsPath = "/api/metrics/" + encodeURIComponent(scheme.scheme_id);
          if (responses[metricsPath]) metricsByKey[scheme.scheme_id] = responses[metricsPath];
        });
        liveTasks = buildLiveTaskSchemes(schemesPayload, metricsByKey);
      }

      var backtestTasks = null;
      var backtestPayload = responses["/api/backtests/factor-lab"];
      if (backtestPayload && backtestPayload.schemes && backtestPayload.schemes.length) {
        backtestTasks = buildBacktestTaskSchemes(backtestPayload);
      }
      var hasLive = liveTasks && hasPopulatedTasks(liveTasks);
      var hasBacktest = backtestTasks && hasPopulatedTasks(backtestTasks);
      if (!hasLive && !hasBacktest) {
        throw new Error("实时和回测接口均暂不可用");
      }
      return {
        tasks: mergeFactorLabTasks(backtestTasks, liveTasks),
        mode: hasLive && hasBacktest ? "merged" : (hasLive ? "live" : "backtest"),
        targetLabels: Object.assign({}, factorTargetLabels)
      };
    } finally {
      factorTargetLabels = originalLabels;
    }
  }

  function loadLegacyFixtureForTest(responses) {
    try {
      var viewModel = buildLegacyFactorLabViewModelForTest(responses);
      finishFactorLabDataLoad(viewModel.tasks, viewModel.mode, viewModel.targetLabels);
      return true;
    } catch (error) {
      return failFactorLabDataLoad(error);
    }
  }

  function startFactorLabAutoRefresh() {
    if (factorLabRuntimeState.visibilityBound) return;
    factorLabRuntimeState.visibilityBound = true;
    if (!document.addEventListener) return;
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") {
        clearFactorLabRefreshTimer();
        return;
      }
      if (getActiveView() === "factor-lab" && !factorLabRemoteLoading) {
        loadFactorLabData({ force: true });
      }
    });
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

  function getVisibleRawDailyRowsForScheme(scheme) {
    if (!scheme || !scheme.dailyRowsByMonth) return [];
    var src = factorLabState.dataSource;
    var rows = [];
    Object.keys(scheme.dailyRowsByMonth).forEach(function (month) {
      if (month < factorLabState.startMonth || month > factorLabState.endMonth) return;
      (scheme.dailyRowsByMonth[month] || []).forEach(function (dr) {
        if (src === "all" || dr._source === src) rows.push(dr);
      });
    });
    return rows;
  }

  function getVisibleDailyRowsForScheme(scheme) {
    return getVisibleRawDailyRowsForScheme(scheme).filter(function (row) {
      return normalizeDirection(row.predictedDirection) !== null && normalizeDirection(row.actualDirection) !== null;
    });
  }

  function metricFromSampleRows(rows) {
    var samples = rows.length;
    var metricRows = rows.filter(function (row) {
      return row.predictedDirection === 1 || row.predictedDirection === -1;
    });
    var metricSamples = metricRows.length;
    var correct = metricRows.reduce(function (sum, row) {
      return sum + (row.predictedDirection === row.actualDirection ? 1 : 0);
    }, 0);
    var predUp = metricRows.reduce(function (sum, row) { return sum + (row.predictedDirection === 1 ? 1 : 0); }, 0);
    var predDown = metricRows.reduce(function (sum, row) { return sum + (row.predictedDirection === -1 ? 1 : 0); }, 0);
    var actualUp = metricRows.reduce(function (sum, row) { return sum + (row.actualDirection === 1 ? 1 : 0); }, 0);
    var actualDown = metricRows.reduce(function (sum, row) { return sum + (row.actualDirection === -1 ? 1 : 0); }, 0);
    var upTp = metricRows.reduce(function (sum, row) {
      return sum + (row.predictedDirection === 1 && row.actualDirection === 1 ? 1 : 0);
    }, 0);
    var downTp = metricRows.reduce(function (sum, row) {
      return sum + (row.predictedDirection === -1 && row.actualDirection === -1 ? 1 : 0);
    }, 0);
    return {
      samples: samples,
      metricSamples: metricSamples,
      correct: correct,
      overall: metricSamples ? correct / metricSamples * 100 : null,
      upPrecision: predUp ? upTp / predUp * 100 : null,
      upRecall: actualUp ? upTp / actualUp * 100 : null,
      downPrecision: predDown ? downTp / predDown * 100 : null,
      downRecall: actualDown ? downTp / actualDown * 100 : null
    };
  }

  function aggregateScheme(scheme) {
    var committed = factorLabRuntimeState.committedViewModel;
    var cacheKey = [
      committed ? committed.snapshotId : "uncommitted",
      (scheme && (scheme.schemeId || scheme.id)) || "",
      factorLabState.startMonth + ":" + factorLabState.endMonth,
      factorLabState.dataSource
    ].join("\u0000");
    if (factorLabRuntimeState.aggregateCache.has(cacheKey)) {
      return factorLabRuntimeState.aggregateCache.get(cacheKey);
    }
    var rawRows = getVisibleRawDailyRowsForScheme(scheme);
    if (!rawRows.length) {
      var hasAnyDetailRows = Object.keys(scheme.dailyRowsByMonth || {}).some(function (month) {
        return (scheme.dailyRowsByMonth[month] || []).length > 0;
      });
      if (!hasAnyDetailRows && (scheme.monthlyRows || []).length) {
        throw new Error("scheme " + ((scheme && scheme.schemeId) || scheme.id || "") +
          " has monthly metrics but no detail rows");
      }
      var emptyMetric = metricFromSampleRows([]);
      factorLabRuntimeState.aggregateCache.set(cacheKey, emptyMetric);
      return emptyMetric;
    }
    var dailyRows = getVisibleDailyRowsForScheme(scheme);
    var metric = metricFromSampleRows(dailyRows);
    factorLabRuntimeState.aggregateCache.set(cacheKey, metric);
    return metric;
  }

  function rankingMetricValue(scheme, metricId) {
    var metric = aggregateScheme(scheme);
    if (metricId === "samples") return metric.samples;
    if (metricId === "correct") return metric.correct;
    return metric[metricId];
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

  function renderFactorLabDataStatus() {
    renderFactorLabSchemeTotal();
    var status = document.getElementById("factorDataStatus");
    var text = document.getElementById("factorDataStatusText");
    if (!status || !text) return;
    ["is-loading", "is-fresh", "is-error"].forEach(function (className) {
      status.classList.remove(className);
    });
    if (factorLabRemoteLoading) {
      status.classList.add("is-loading");
      text.textContent = "数据刷新中";
    } else if (factorLabApiError) {
      status.classList.add("is-error");
      text.textContent = "数据不可用";
    } else if (factorLabRuntimeState.committedViewModel) {
      status.classList.add("is-fresh");
      text.textContent = "数据已就绪";
    } else {
      status.classList.add("is-loading");
      text.textContent = "Loading";
    }
    status.setAttribute("data-data-state", factorLabDataMode);
    status.setAttribute("title", factorLabApiError || text.textContent);
  }

  function factorLabSchemeCountsAvailable(dataState) {
    var state = dataState || {};
    return Boolean(state.remoteLoaded && !state.remoteLoading && !state.apiError &&
      state.dataMode !== "loading" && state.dataMode !== "error");
  }

  function currentFactorLabDataState() {
    return {
      remoteLoaded: factorLabRemoteLoaded,
      remoteLoading: factorLabRemoteLoading,
      apiError: factorLabApiError,
      dataMode: factorLabDataMode
    };
  }

  function factorLabSchemeTotalLabel(tasks, dataState) {
    return "方案总数：" + (factorLabSchemeCountsAvailable(dataState)
      ? countFactorLabRows(tasks).schemes
      : "--");
  }

  function renderFactorLabSchemeTotal() {
    var badge = document.getElementById("factorOverviewSchemeCount");
    if (!badge) return;
    badge.textContent = factorLabSchemeTotalLabel(
      factorTaskSchemes,
      currentFactorLabDataState()
    );
  }

  function renderTaskOverview() {
    var body = document.getElementById("factorTaskMatrixBody");
    var range = document.getElementById("factorOverviewRange");
    var metricBadge = document.getElementById("factorOverviewMetric");
    if (!body) return;
    renderFactorLabSchemeTotal();
    if (range) range.textContent = factorLabState.startMonth + " 至 " + factorLabState.endMonth;
    if (metricBadge) metricBadge.textContent = "指标：" + getMetricLabel(factorLabState.rankMetric);
    var schemeCountsAvailable = factorLabSchemeCountsAvailable(currentFactorLabDataState());

    var html = factorTargets.map(function (target) {
      var cells = factorTaskColumns.map(function (column) {
        var key = getTaskKey(target, column);
        var schemes = getSchemesForTask(key);
        var best = sortSchemesByMetric(schemes)[0];
        var metric = best ? aggregateScheme(best) : null;
        var metricValue = metric ? metric[factorLabState.rankMetric] : null;
        var metricClass = ["overall", "upPrecision", "downPrecision"].indexOf(
          factorLabState.rankMetric
        ) !== -1
          ? " " + getMetricClass(metricValue)
          : "";
        var selectedClass = key === factorLabState.selectedTaskKey ? " is-selected" : "";
        var value = formatPercent(metricValue);
        var schemeCount = schemeCountsAvailable ? schemes.length : "--";
        return '<td><button type="button" class="factor-task-cell' + selectedClass + '" data-factor-task-key="' + escapeHtml(key) + '">' +
          '<span class="factor-task-top' + metricClass + '">' + value + '</span>' +
          '<span class="factor-task-count">' + schemeCount + ' 个方案</span>' +
          '</button></td>';
      }).join("");
      return '<tr><td>' + escapeHtml(getTargetDisplayName(target)) + '</td>' + cells + '</tr>';
    }).join("");
    body.innerHTML = html;
  }

  function renderSchemeRankingRow(scheme, index, metric) {
    var selectedClass = scheme.id === factorLabState.selectedSchemeId ? " class=\"is-selected\"" : "";
    var barWidth = clampPercent(metric.overall);
    var metricSamples = requireMetricSamples(metric, "ranking metric");
    var deploymentDate = requireSchemeDeploymentDate(scheme, "ranking scheme");
    var schemeName = escapeHtml(scheme.name);
    var remark = getSchemeRemark(scheme);
    var remarkControl = remark
      ? '<button type="button" class="factor-remark-detail" data-factor-remark-open="' + escapeHtml(scheme.id) + '" aria-controls="factorRemarkPopover" aria-expanded="false"><span aria-hidden="true">ⓘ</span><span>详情</span></button>'
      : '<span class="factor-remark-empty">--</span>';
    return '<tr' + selectedClass + ' data-factor-scheme-id="' + escapeHtml(scheme.id) + '">' +
      '<td>' + (index + 1) + '</td>' +
      '<td><strong class="factor-scheme-name" title="' + schemeName + '">' + schemeName + '</strong></td>' +
      '<td class="' + getMetricClass(metric.overall) + '"><div class="factor-score-cell"><span>' + formatPercent(metric.overall) + '（' + metric.correct + '/' + metricSamples + '）</span><span class="factor-score-bar" aria-hidden="true"><span style="width:' + barWidth.toFixed(1) + '%"></span></span></div></td>' +
      '<td><span class="factor-sample-count">' + metric.samples + '</span></td>' +
      '<td class="' + getMetricClass(metric.upPrecision) + '">' + formatPercent(metric.upPrecision) + '</td>' +
      '<td class="' + getMetricClass(metric.downPrecision) + '">' + formatPercent(metric.downPrecision) + '</td>' +
      '<td class="mono">' + escapeHtml(deploymentDate) + '</td>' +
      '<td class="mono">' + escapeHtml(scheme.owner || "--") + '</td>' +
      '<td class="factor-remark-cell">' + remarkControl + '</td>' +
      '</tr>';
  }

  function placeFactorRemarkPopover(trigger) {
    var popover = document.getElementById("factorRemarkPopover");
    if (!popover || !trigger) return;
    var margin = 12;
    var gap = 8;
    var triggerRect = trigger.getBoundingClientRect();
    var width = popover.offsetWidth;
    var height = popover.offsetHeight;
    var left = Math.min(
      Math.max(margin, triggerRect.right - width),
      window.innerWidth - width - margin
    );
    var top = triggerRect.bottom + gap;
    if (top + height > window.innerHeight - margin) {
      top = triggerRect.top - height - gap;
    }
    top = Math.min(
      Math.max(margin, top),
      Math.max(margin, window.innerHeight - height - margin)
    );
    popover.style.left = Math.round(left) + "px";
    popover.style.top = Math.round(top) + "px";
  }

  function closeFactorRemark(restoreFocus) {
    var popover = document.getElementById("factorRemarkPopover");
    var body = document.getElementById("factorRemarkBody");
    var trigger = factorRemarkTrigger;
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    factorRemarkTrigger = null;
    if (popover) {
      popover.hidden = true;
      popover.setAttribute("aria-hidden", "true");
      popover.style.removeProperty("left");
      popover.style.removeProperty("top");
    }
    if (body) body.textContent = "";
    if (restoreFocus && trigger && trigger.isConnected && typeof trigger.focus === "function") {
      window.requestAnimationFrame(function () {
        if (!trigger.isConnected) return;
        try {
          trigger.focus({ preventScroll: true });
        } catch (error) {
          trigger.focus();
        }
      });
    }
  }

  function openFactorRemark(trigger) {
    var schemeId = trigger && trigger.getAttribute("data-factor-remark-open");
    var scheme = getSelectedTaskSchemes().find(function (candidate) {
      return candidate.id === schemeId;
    });
    var remark = getSchemeRemark(scheme);
    var popover = document.getElementById("factorRemarkPopover");
    var body = document.getElementById("factorRemarkBody");
    if (!trigger || !remark || !popover || !body) return;

    closeFactorRemark(false);
    factorRemarkTrigger = trigger;
    body.textContent = remark;
    popover.hidden = false;
    popover.setAttribute("aria-hidden", "false");
    trigger.setAttribute("aria-expanded", "true");
    placeFactorRemarkPopover(trigger);

    var closeButton = popover.querySelector("[data-factor-remark-close]");
    if (closeButton && typeof closeButton.focus === "function") {
      closeButton.focus({ preventScroll: true });
    }
  }

  function renderSchemeRanking() {
    var body = document.getElementById("factorSchemeRankingBody");
    var title = document.getElementById("factorRankingTitle");
    if (!body) return;

    var task = getTaskByKey(factorLabState.selectedTaskKey);
    var schemes = sortSchemesByMetric(getSelectedTaskSchemes());
    if (title) title.textContent = task.label + " 候选方案排行";

    if (!schemes.length) {
      body.innerHTML = '<tr><td colspan="9" class="factor-empty-cell">该任务格子下暂无方案</td></tr>';
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
      var ariaDirection = factorLabState.rankDirection === "asc" ? "ascending" : "descending";
      button.setAttribute("aria-sort", active ? ariaDirection : "none");
    });
    var srcSelect = document.getElementById("factorDataSource");
    if (srcSelect) srcSelect.value = factorLabState.dataSource;
    renderFactorMonthSelects();
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
    var height = 244;
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
    if (!tbody) return;

    var task = getTaskByKey(factorLabState.selectedTaskKey);
    var scheme = getSelectedScheme();
    if (title) title.textContent = scheme ? "选中方案详情：" + scheme.name : "选中方案详情";

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
      var rowMetricSamples = requireMetricSamples(row, "monthly row");
      html += '<td class="' + getMetricClass(row.overall) + '">' + formatPercent(row.overall) + '（' + row.correct + '/' + rowMetricSamples + '）</td>';
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
    renderFactorLabDataStatus();
    updateFactorFilterUi();
    ensureSelectedScheme();
    renderTaskOverview();
    renderSchemeRanking();
    renderFactorDetail();
  }

  function renderDailyResult(row) {
    if (row.correct !== true && row.correct !== false) {
      return '<span class="factor-result-dot is-neutral">?</span>';
    }
    return '<span class="factor-result-dot ' + (row.correct ? "is-correct" : "is-wrong") + '">' + (row.correct ? "✓" : "×") + '</span>';
  }

  function renderFactorDailyRows(month) {
    var body = document.getElementById("factorDailyTableBody");
    var title = document.getElementById("factorCalendarTitle");
    var meta = document.getElementById("factorCalendarMeta");
    if (!body || !title || !meta) return;

    var task = getTaskByKey(factorLabState.selectedTaskKey);
    var scheme = getSelectedScheme();
    var isWeekly = isWeeklyTask(task);
    var isWeeklyAverage = isWeeklyAverageTask(task);
    var dateHeader = document.getElementById("factorDailyDateHeader");
    var note = document.getElementById("factorCalendarNote");
    title.textContent = month + (isWeekly ? " 周度验证表" : " 每日验证表");
    meta.textContent = (scheme ? scheme.name : "--") + " · " + task.label;
    if (dateHeader) dateHeader.textContent = isWeeklyAverage ? "目标周" : "目标日";
    if (note) note.textContent = isWeekly
      ? (
          isWeeklyAverage
            ? "表内可继续滚动查看该月全部周度预测；目标周按该周最后可验证交易日标记。"
            : "表内可继续滚动查看该月全部周度预测；目标日为下周最后一个交易日。"
        )
      : "表内可继续滚动查看该月全部交易日的预测。";

    var html = "";
    var monthLabel = month.slice(5, 7);
    var src = factorLabState.dataSource;
    var rows = scheme && scheme.dailyRowsByMonth && scheme.dailyRowsByMonth[month]
      ? scheme.dailyRowsByMonth[month].filter(function (r) {
          return src === "all" || r._source === src;
        })
      : [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="5" class="factor-empty-cell">当前月份暂无每日明细</td></tr>';
      return;
    }
    rows.forEach(function (row) {
      var predictedClass = getDirectionClass(row.predicted);
      var actualClass = getDirectionClass(row.actual);
      var displayDay = row.day.replace(/^\d{2}/, monthLabel);
      var result = renderDailyResult(row);
      html += '<tr>';
      html += dateCellHtml(row.predictDate, "--");
      html += dateCellHtml(row.targetDate, displayDay);
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
    var scheme = getSelectedScheme();
    factorLabDrawerSelection = scheme ? {
      taskKey: factorLabState.selectedTaskKey,
      schemeId: scheme.id,
      month: month
    } : null;
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
    factorLabDrawerSelection = null;
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
        var remarkButton = event.target.closest("[data-factor-remark-open]");
        if (remarkButton) {
          event.preventDefault();
          event.stopPropagation();
          openFactorRemark(remarkButton);
          return;
        }

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

    document.addEventListener("click", function (event) {
      var remarkCloseButton = event.target.closest("[data-factor-remark-close]");
      if (remarkCloseButton) {
        event.preventDefault();
        event.stopPropagation();
        closeFactorRemark(true);
        return;
      }
      var popover = document.getElementById("factorRemarkPopover");
      if (!popover || popover.hidden) return;
      if (popover.contains(event.target) || event.target.closest("[data-factor-remark-open]")) return;
      closeFactorRemark(true);
    });

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
      closeFactorRemark(true);
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
        dataSource: factorLabState.dataSource,
        selectedTaskKey: factorLabState.selectedTaskKey,
        startMonth: factorLabState.startMonth
      };
    },
    getFactorLabRuntimeStateForTest: function () {
      return {
        loadSeq: factorLabRuntimeState.loadSeq,
        capability: factorLabRuntimeState.capability,
        stale: factorLabRuntimeState.stale,
        consecutiveFailures: factorLabRuntimeState.consecutiveFailures,
        nextRefreshAt: factorLabRuntimeState.nextRefreshAt,
        snapshotId: factorLabRuntimeState.committedViewModel
          ? factorLabRuntimeState.committedViewModel.snapshotId
          : null,
        source: factorLabRuntimeState.committedViewModel
          ? factorLabRuntimeState.committedViewModel.source
          : null,
        aggregateCacheSize: factorLabRuntimeState.aggregateCache.size,
        hasController: Boolean(factorLabRuntimeState.controller),
        drawerSelection: factorLabDrawerSelection
      };
    },
    setFactorLabStateForTest: function (patch) {
      Object.keys(patch || {}).forEach(function (key) {
        if (Object.prototype.hasOwnProperty.call(factorLabState, key)) {
          factorLabState[key] = patch[key];
        }
      });
    },
    getSelectedScheme: getSelectedScheme,
    getSchemeDeploymentDate: getSchemeDeploymentDate,
    requireSchemeDeploymentDate: requireSchemeDeploymentDate,
    getSchemeRemark: getSchemeRemark,
    liveDividerTextForTest: liveDividerText,
    loadFactorLabData: loadFactorLabData,
    decodeDashboardPayload: decodeDashboardPayload,
    buildFactorLabViewModel: buildFactorLabViewModel,
    buildLegacyFactorLabViewModelForTest: buildLegacyFactorLabViewModelForTest,
    loadLegacyFixtureForTest: loadLegacyFixtureForTest,
    apiUrlForTest: apiUrl,
    normalizeRouteForTest: normalizeRoute,
    routeUrlForTest: routeUrl,
    getMetricClassForTest: getMetricClass,
    getDirectionClassForTest: getDirectionClass,
    renderDailyResultForTest: renderDailyResult,
    renderFactorDailyRowsForTest: renderFactorDailyRows,
    openFactorCalendarForTest: openFactorCalendar,
    closeFactorCalendarForTest: closeFactorCalendar,
    renderTaskOverviewForTest: renderTaskOverview,
    renderFactorTrendChartForTest: renderFactorTrendChart,
    trendChartLayoutForTest: buildTrendChartLayout,
    trendMonthLabelVisibleForTest: shouldShowTrendMonthLabel,
    renderSchemeRankingRowForTest: renderSchemeRankingRow,
    sortRankingSchemes: sortRankingSchemes,
    factorLabSchemeTotalLabelForTest: factorLabSchemeTotalLabel,
    getTaskSchemesForTest: function () {
      return factorTaskSchemes;
    },
    getFactorTargetLabelsForTest: function () {
      return Object.assign({}, factorTargetLabels);
    },
    getTaskSchemeCountsForTest: function () {
      var counts = {};
      Object.keys(factorTaskSchemes || {}).forEach(function (key) {
        counts[key] = (factorTaskSchemes[key] || []).length;
      });
      return counts;
    }
  };

  /* ─── Init ─── */
  setActiveRoute("/factor-lab", false);
  startFactorLabAutoRefresh();
})();

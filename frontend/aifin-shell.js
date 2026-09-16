(function () {
  "use strict";

  var routeToView = {
    "/": "factor-lab",
    "/factor-lab": "factor-lab"
  };
  var PUBLIC_BASE_PATH = "/bond-factor-lab";

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
    { id: "monthly", label: "月中收", taskType: "monthly", frequency: "monthly", horizon: "MONTHLY" },
    { id: "monthlyAverage", label: "月均", taskType: "monthly_average", frequency: "monthly", horizon: "NEXT_MID_BUCKET_AVERAGE" },
    { id: "quarterlyAverage", label: "季均", taskType: "quarterly_average", frequency: "quarterly", horizon: "NEXT_CALENDAR_QUARTER_AVERAGE" },
    { id: "annualAverage", label: "年均", taskType: "annual_average", frequency: "annual", horizon: "NEXT_SPRING_FESTIVAL_YEAR_AVERAGE" }
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
  var FACTOR_LAB_HEALTHY_REFRESH_MS = 300000;
  var FACTOR_LAB_REQUEST_TIMEOUT_MS = 6000;
  var FACTOR_LAB_MAX_RETRY_DELAY_MS = 2147483647;
  var FACTOR_LAB_RETRY_DELAYS_MS = [10000, 30000, 60000, 300000];
  var factorLabRuntimeState = {
    authenticated: false,
    loadSeq: 0,
    controller: null,
    committedViewModel: null,
    consecutiveFailures: 0,
    refreshTimer: null,
    visibilityBound: false,
    aggregateCache: new Map(),
    detailCache: new Map(),
    detailSeq: 0,
    detailController: null,
    lastSuccessfulAt: 0,
    lastSuccessfulGeneratedAt: ""
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

  function formatFactorLabGeneratedAt(value) {
    var text = String(value || "");
    return text.length >= 16 ? text.slice(0, 16).replace("T", " ") : "最近成功时间未知";
  }

  function normalizeIsoDate(value) {
    var match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    return match ? match[1] + "-" + match[2] + "-" + match[3] : "";
  }

  function monthlyAverageTargetMonth(targetDate) {
    var value = requireDashboardIsoDate(targetDate, "monthly_average target_date");
    var year = Number(value.slice(0, 4));
    var month = Number(value.slice(5, 7)) + 1;
    if (month === 13) {
      year += 1;
      month = 1;
    }
    return String(year).padStart(4, "0") + "-" + String(month).padStart(2, "0");
  }

  function targetDisplayMonth(targetDate, taskType) {
    var value = requireDashboardIsoDate(targetDate, "target_date");
    if (taskType === "monthly_average") {
      return monthlyAverageTargetMonth(value);
    }
    var targetMonth = value.slice(0, 7);
    if (taskType === "quarterly_average") {
      formatQuarterlyAverageTargetQuarter(targetMonth);
    } else if (taskType === "annual_average") {
      formatAnnualAverageTargetYear(targetMonth);
    }
    return targetMonth;
  }

  function formatMonthlyAveragePredictDate(predictDate) {
    var value = requireDashboardIsoDate(
      predictDate,
      "monthly_average predict_date"
    );
    return value.slice(5, 7) + "/" + value.slice(8, 10);
  }

  function formatMonthlyAverageTargetMonth(targetMonth) {
    var value = String(targetMonth || "").trim();
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(value)) {
      throw dashboardDataError(
        "monthly_average target month must use YYYY-MM"
      );
    }
    return value.slice(0, 4) + "/" + value.slice(5, 7);
  }

  function formatQuarterlyAveragePredictDate(predictDate) {
    var value = requireDashboardIsoDate(
      predictDate,
      "quarterly_average predict_date"
    );
    return value.slice(5, 7) + "/" + value.slice(8, 10);
  }

  function formatQuarterlyAverageTargetQuarter(targetMonth) {
    var value = String(targetMonth || "").trim();
    var match = value.match(/^(\d{4})-(01|04|07|10)$/);
    if (!match) {
      throw dashboardDataError(
        "quarterly_average quarter target month must use a quarter-start YYYY-MM"
      );
    }
    return match[1] + "/Q" + String((Number(match[2]) - 1) / 3 + 1);
  }

  function formatAnnualAveragePredictDate(predictDate) {
    var value = requireDashboardIsoDate(
      predictDate,
      "annual_average predict_date"
    );
    return value.slice(5, 7) + "/" + value.slice(8, 10);
  }

  function formatAnnualAverageTargetYear(targetMonth) {
    var value = String(targetMonth || "").trim();
    var match = value.match(/^(\d{4})-(0[1-9]|1[0-2])$/);
    if (!match) {
      throw dashboardDataError(
        "annual_average target year key must use YYYY-MM"
      );
    }
    return match[1];
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

  function isMonthlyAverageTask(task) {
    return task && task.taskType === "monthly_average";
  }

  function isQuarterlyAverageTask(task) {
    return task && task.taskType === "quarterly_average";
  }

  function isAnnualAverageTask(task) {
    return task && task.taskType === "annual_average";
  }

  function formatFactorPeriodLabel(task, month) {
    if (isQuarterlyAverageTask(task)) {
      return formatQuarterlyAverageTargetQuarter(month);
    }
    if (isAnnualAverageTask(task)) {
      return formatAnnualAverageTargetYear(month);
    }
    return month;
  }

  function liveDividerText() {
    var committed = factorLabRuntimeState.committedViewModel;
    var targetStart = committed && committed.liveTargetStartDate || "";
    return targetStart
      ? "实盘口径：target_date ≥ " + targetStart
      : "实盘预测目标区间：待产生";
  }

  function getSchemeDeploymentDate(scheme) {
    return formatDeploymentDate(scheme && scheme.deploymentDate);
  }

  function requireSchemeDeploymentDate(scheme, context) {
    var value = getSchemeDeploymentDate(scheme);
    if (value) return value;
    var id = scheme && (scheme.schemeId || scheme.name);
    throw new Error((context || "scheme") + " missing deployed_at for " + (id || "unknown"));
  }

  function getSchemeRemark(scheme) {
    return String((scheme && scheme.remark) || "").trim();
  }

  function getTaskKey(target, column) {
    return target + "|" + column.taskType;
  }

  function getTargetDisplayName(target) {
    return factorTargetLabels[target] || target;
  }

  function getTaskByKey(taskKey) {
    var parts = String(taskKey || "").split("|");
    var column = factorTaskColumns.filter(function (item) {
      return item.taskType === parts[1];
    })[0] || factorTaskColumns[0];
    return {
      frequency: column.frequency,
      taskType: column.taskType,
      label: getTargetDisplayName(parts[0] || "3Y") + " · " + column.label
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

  function columnForTaskType(taskType) {
    return factorTaskColumns.filter(function (column) {
      return column.taskType === taskType;
    })[0] || null;
  }

  var DASHBOARD_SCHEMA_VERSION = "factor-lab-dashboard-v6";
  var DASHBOARD_DETAIL_ROW_FIELDS = [
    "source",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
    "actual_direction"
  ];
  var DASHBOARD_TOP_FIELDS = [
    "schema_version",
    "representation",
    "snapshot_id",
    "generated_at",
    "display_until",
    "live_target_start_date",
    "monthly_row_fields",
    "target_labels",
    "schemes"
  ];
  var DASHBOARD_DETAIL_TOP_FIELDS = [
    "schema_version",
    "representation",
    "snapshot_id",
    "generated_at",
    "display_until",
    "live_target_start_date",
    "scheme_id",
    "month",
    "source",
    "row_fields",
    "rows"
  ];
  var DASHBOARD_MONTHLY_ROW_FIELDS = [
    "month",
    "source",
    "samples",
    "metric_samples",
    "correct",
    "predicted_up",
    "predicted_down",
    "predicted_flat",
    "actual_up",
    "actual_down",
    "actual_flat",
    "up_true_positive",
    "down_true_positive"
  ];
  var DASHBOARD_SCHEME_FIELDS = [
    "scheme_id",
    "base_scheme_id",
    "name",
    "owner",
    "is_production",
    "description",
    "horizon",
    "task_type",
    "frequency",
    "target_tenor",
    "target_label",
    "status",
    "deployed_at",
    "monthly_rows",
    "backtest"
  ];
  var DASHBOARD_BACKTEST_FIELDS = [
    "benchmark_id",
    "benchmark_label",
    "data_source",
    "data_source_label",
    "latest_run_date"
  ];
  var DASHBOARD_TASK_TYPES = ["T+1", "T+5", "weekly_point", "weekly_average", "monthly", "monthly_average", "quarterly_average", "annual_average"];
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
      throw dashboardDataError(context + " fields must match v6 exactly");
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

  function requireDashboardOwner(value, context) {
    requireDashboardString(value, context, false);
    if (Array.from(value).length > 64 || value !== value.trim() ||
        /[<>\n\r]|[\p{C}]/u.test(value) ||
        ["--", "unknown", "待定"].indexOf(value.toLowerCase()) !== -1) {
      throw dashboardDataError(context + " must be a valid owner");
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

  function decodeDashboardDetailRows(rows, requestedSource, liveTargetStartDate, context) {
    if (!Array.isArray(rows)) throw dashboardDataError(context + " must be an array");
    var seenPoints = Object.create(null);
    var lastSortKey = "";
    return rows.map(function (row, index) {
      var rowContext = context + "[" + index + "]";
      if (!Array.isArray(row) || row.length !== DASHBOARD_DETAIL_ROW_FIELDS.length) {
        throw dashboardDataError(rowContext + " must have width " + DASHBOARD_DETAIL_ROW_FIELDS.length);
      }
      var source = row[0];
      if (["backtest", "live"].indexOf(source) === -1 ||
          (requestedSource !== "all" && source !== requestedSource)) {
        throw dashboardDataError(rowContext + " has invalid source");
      }
      var predictDate = requireDashboardIsoDate(row[1], rowContext + ".predict_date");
      var featureDate = requireDashboardIsoDate(row[2], rowContext + ".feature_date");
      var targetDate = requireDashboardIsoDate(row[3], rowContext + ".target_date");
      var expectedSource = targetDate >= liveTargetStartDate ? "live" : "backtest";
      if (source !== expectedSource) {
        throw dashboardDataError(rowContext + ".source does not match target_date policy");
      }
      var predictedDirection = requireDashboardDirection(
        row[4], rowContext + ".predicted_direction", false
      );
      var actualDirection = requireDashboardDirection(
        row[5], rowContext + ".actual_direction", true
      );
      var pointKey = source + "\u0000" + targetDate;
      if (seenPoints[pointKey]) {
        throw dashboardDataError(context + " has duplicate canonical source/target_date " + pointKey);
      }
      seenPoints[pointKey] = true;
      var sourceRank = source === "backtest" ? "0" : "1";
      var sortKey = sourceRank + "\u0000" + targetDate + "\u0000" + predictDate;
      if (lastSortKey && sortKey < lastSortKey) {
        throw dashboardDataError(context + " is not canonically sorted");
      }
      lastSortKey = sortKey;
      return {
        predictDate: predictDate,
        featureDate: featureDate,
        targetDate: targetDate,
        predictedDirection: predictedDirection,
        actualDirection: actualDirection,
        source: source
      };
    });
  }

  function metricFromDashboardCounts(counts) {
    return {
      samples: counts.samples,
      metricSamples: counts.metricSamples,
      correct: counts.correct,
      overall: counts.metricSamples ? counts.correct / counts.metricSamples * 100 : null,
      upPrecision: counts.predictedUp ? counts.upTruePositive / counts.predictedUp * 100 : null,
      upRecall: counts.actualUp ? counts.upTruePositive / counts.actualUp * 100 : null,
      downPrecision: counts.predictedDown ? counts.downTruePositive / counts.predictedDown * 100 : null,
      downRecall: counts.actualDown ? counts.downTruePositive / counts.actualDown * 100 : null
    };
  }

  function dashboardLiveDisplayStartMonth(liveTargetStartDate, taskType) {
    var startMonth = liveTargetStartDate.slice(0, 7);
    if (taskType !== "monthly_average") return startMonth;
    var year = Number(startMonth.slice(0, 4));
    var month = Number(startMonth.slice(5, 7)) + 1;
    if (month === 13) {
      year += 1;
      month = 1;
    }
    return String(year).padStart(4, "0") + "-" + String(month).padStart(2, "0");
  }

  function decodeDashboardMonthlyRows(rows, taskType, liveTargetStartDate, context) {
    if (!Array.isArray(rows)) throw dashboardDataError(context + " must be an array");
    var seen = Object.create(null);
    var lastSortKey = "";
    return rows.map(function (row, index) {
      var rowContext = context + "[" + index + "]";
      if (!Array.isArray(row) || row.length !== DASHBOARD_MONTHLY_ROW_FIELDS.length) {
        throw dashboardDataError(rowContext + " must have width " + DASHBOARD_MONTHLY_ROW_FIELDS.length);
      }
      var month = requireDashboardMonth(row[0], rowContext + ".month");
      var source = row[1];
      if (["backtest", "live"].indexOf(source) === -1) {
        throw dashboardDataError(rowContext + " has invalid source");
      }
      var expectedSource = month >= dashboardLiveDisplayStartMonth(
        liveTargetStartDate, taskType
      ) ? "live" : "backtest";
      if (source !== expectedSource) {
        throw dashboardDataError(rowContext + ".source does not match target_date policy");
      }
      var key = month + "\u0000" + source;
      if (seen[key]) throw dashboardDataError(context + " has duplicate month/source " + key);
      seen[key] = true;
      var sortKey = month + "\u0000" + (source === "backtest" ? "0" : "1");
      if (lastSortKey && sortKey < lastSortKey) {
        throw dashboardDataError(context + " is not canonically sorted");
      }
      lastSortKey = sortKey;
      var values = row.slice(2).map(function (value, valueIndex) {
        return requireDashboardInteger(
          value, rowContext + "." + DASHBOARD_MONTHLY_ROW_FIELDS[valueIndex + 2], 0
        );
      });
      var counts = {
        samples: values[0],
        metricSamples: values[1],
        correct: values[2],
        predictedUp: values[3],
        predictedDown: values[4],
        predictedFlat: values[5],
        actualUp: values[6],
        actualDown: values[7],
        actualFlat: values[8],
        upTruePositive: values[9],
        downTruePositive: values[10]
      };
      if (counts.metricSamples > counts.samples || counts.correct > counts.metricSamples ||
          counts.predictedUp + counts.predictedDown + counts.predictedFlat !== counts.samples ||
          counts.actualUp + counts.actualDown + counts.actualFlat !== counts.samples) {
        throw dashboardDataError(rowContext + " counts are inconsistent");
      }
      return Object.assign({
        month: month,
        _source: source,
        actualDist: counts.actualUp + "/" + counts.actualDown + "/" + counts.actualFlat,
        predictedDist: counts.predictedUp + "/" + counts.predictedDown + "/" + counts.predictedFlat
      }, counts, metricFromDashboardCounts(counts));
    });
  }

  function requireDashboardMonth(value, context) {
    if (typeof value !== "string" || !/^\d{4}-(0[1-9]|1[0-2])$/.test(value)) {
      throw dashboardDataError(context + " must be YYYY-MM");
    }
    return value;
  }

  function decodeDashboardPayload(payload) {
    requireExactDashboardFields(payload, DASHBOARD_TOP_FIELDS, "payload");
    if (payload.schema_version !== DASHBOARD_SCHEMA_VERSION) {
      throw dashboardDataError("unsupported schema_version");
    }
    if (payload.representation !== "summary") {
      throw dashboardDataError("payload representation must be summary");
    }
    if (!Array.isArray(payload.monthly_row_fields) ||
        payload.monthly_row_fields.length !== DASHBOARD_MONTHLY_ROW_FIELDS.length ||
        payload.monthly_row_fields.some(function (field, index) {
          return field !== DASHBOARD_MONTHLY_ROW_FIELDS[index];
        }) || new Set(payload.monthly_row_fields).size !== payload.monthly_row_fields.length) {
      throw dashboardDataError("monthly_row_fields must match v6 exactly");
    }
    var snapshotId = requireDashboardString(payload.snapshot_id, "snapshot_id", false);
    if (!DASHBOARD_SNAPSHOT_ID_PATTERN.test(snapshotId)) {
      throw dashboardDataError("snapshot_id must be canonical");
    }
    var generatedAt = requireDashboardAwareDateTime(
      payload.generated_at, "generated_at"
    );
    var displayUntil = requireDashboardIsoDate(payload.display_until, "display_until");
    var liveTargetStartDate = requireDashboardIsoDate(
      payload.live_target_start_date, "live_target_start_date"
    );

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
      var taskType = scheme.task_type;
      if (DASHBOARD_TASK_TYPES.indexOf(taskType) === -1) {
        throw dashboardDataError(context + ".task_type is invalid");
      }
      requireDashboardString(scheme.frequency, context + ".frequency", false);
      if (scheme.status !== "active") throw dashboardDataError(context + ".status must be active");
      var targetLabel = requireDashboardString(scheme.target_label, context + ".target_label", false);
      if (!Object.prototype.hasOwnProperty.call(targetLabels, targetTenor) ||
          targetLabels[targetTenor] !== targetLabel) {
        throw dashboardDataError(context + " target identity is invalid");
      }
      if (typeof scheme.description !== "string") {
        throw dashboardDataError(context + ".description must be a string");
      }
      if (typeof scheme.is_production !== "boolean") {
        throw dashboardDataError(context + ".is_production must be a boolean");
      }
      var decodedScheme = {
        schemeId: schemeId,
        name: requireDashboardString(scheme.name, context + ".name", false),
        owner: requireDashboardOwner(scheme.owner, context + ".owner"),
        isProduction: scheme.is_production,
        description: scheme.description,
        taskType: taskType,
        status: scheme.status,
        targetTenor: targetTenor,
        deployedAt: requireDashboardIsoDate(scheme.deployed_at, context + ".deployed_at"),
        monthlyRows: decodeDashboardMonthlyRows(
          scheme.monthly_rows,
          taskType,
          liveTargetStartDate,
          context + ".monthly_rows"
        ),
        backtest: null
      };
      if (scheme.backtest !== null) {
        requireExactDashboardFields(scheme.backtest, DASHBOARD_BACKTEST_FIELDS, context + ".backtest");
        requireDashboardString(
          scheme.backtest.benchmark_id, context + ".backtest.benchmark_id", false
        );
        requireDashboardString(
          scheme.backtest.data_source, context + ".backtest.data_source", false
        );
        requireDashboardIsoDate(
          scheme.backtest.latest_run_date, context + ".backtest.latest_run_date"
        );
        decodedScheme.backtest = {
          benchmarkLabel: requireDashboardString(
            scheme.backtest.benchmark_label, context + ".backtest.benchmark_label", false
          ),
          dataSourceLabel: requireDashboardString(
            scheme.backtest.data_source_label, context + ".backtest.data_source_label", false
          )
        };
      }
      return decodedScheme;
    });

    return {
      snapshotId: snapshotId,
      generatedAt: generatedAt,
      displayUntil: displayUntil,
      liveTargetStartDate: liveTargetStartDate,
      targetLabels: targetLabels,
      schemes: schemes
    };
  }

  function decodeDashboardDetailPayload(payload, expected) {
    requireExactDashboardFields(payload, DASHBOARD_DETAIL_TOP_FIELDS, "detail payload");
    if (payload.schema_version !== DASHBOARD_SCHEMA_VERSION || payload.representation !== "detail") {
      throw dashboardDataError("detail payload must be dashboard v6 detail");
    }
    if (!Array.isArray(payload.row_fields) ||
        payload.row_fields.length !== DASHBOARD_DETAIL_ROW_FIELDS.length ||
        payload.row_fields.some(function (field, index) {
          return field !== DASHBOARD_DETAIL_ROW_FIELDS[index];
        }) || new Set(payload.row_fields).size !== payload.row_fields.length) {
      throw dashboardDataError("detail row_fields must match v6 exactly");
    }
    var snapshotId = requireDashboardString(payload.snapshot_id, "detail snapshot_id", false);
    if (!DASHBOARD_SNAPSHOT_ID_PATTERN.test(snapshotId)) {
      throw dashboardDataError("detail snapshot_id must be canonical");
    }
    requireDashboardAwareDateTime(payload.generated_at, "detail generated_at");
    requireDashboardIsoDate(payload.display_until, "detail display_until");
    var liveTargetStartDate = requireDashboardIsoDate(
      payload.live_target_start_date, "detail live_target_start_date"
    );
    if (payload.scheme_id !== expected.schemeId || payload.month !== expected.month ||
        payload.source !== expected.source ||
        liveTargetStartDate !== expected.liveTargetStartDate) {
      throw dashboardDataError("detail response identity does not match request");
    }
    return decodeDashboardDetailRows(
      payload.rows,
      expected.source,
      liveTargetStartDate,
      "detail rows"
    );
  }

  function dashboardDetailRow(row, taskType) {
    var actualDirection = row.actualDirection;
    return {
      day: String(row.targetDate).slice(5).replace("-", "/"),
      predictDate: row.predictDate,
      featureDate: row.featureDate,
      targetDate: row.targetDate,
      targetMonth: targetDisplayMonth(row.targetDate, taskType),
      runId: null,
      schemeVersion: "",
      inputArtifactHash: "",
      predicted: directionText(row.predictedDirection),
      actual: directionText(actualDirection),
      predictedDirection: row.predictedDirection,
      actualDirection: actualDirection,
      correct: actualDirection === null ? null : row.predictedDirection === actualDirection,
      _source: row.source
    };
  }

  function buildFactorLabViewModel(decoded) {
    var tasks = initEmptyTaskSchemes();
    decoded.schemes.forEach(function (scheme) {
      var column = columnForTaskType(scheme.taskType);
      if (!column) throw dashboardDataError("decoded task_type is invalid");
      var taskKey = getTaskKey(scheme.targetTenor, column);
      if (!tasks[taskKey]) tasks[taskKey] = [];

      tasks[taskKey].push({
        id: scheme.schemeId,
        schemeId: scheme.schemeId,
        taskType: scheme.taskType,
        taskKey: taskKey,
        targetTenor: scheme.targetTenor,
        column: column.id,
        name: scheme.name,
        owner: scheme.owner,
        isProduction: scheme.isProduction,
        description: scheme.description,
        status: scheme.status,
        deploymentDate: formatDeploymentDate(scheme.deployedAt),
        remark: scheme.description,
        monthlyRows: scheme.monthlyRows,
        benchmarkLabel: scheme.backtest ? scheme.backtest.benchmarkLabel : "",
        dataSourceLabel: scheme.backtest ? scheme.backtest.dataSourceLabel : ""
      });
    });
    Object.keys(tasks).forEach(function (taskKey) {
      tasks[taskKey].sort(function (a, b) {
        return compareUnicodeCodePoints(a.schemeId, b.schemeId);
      });
    });
    return {
      snapshotId: decoded.snapshotId,
      displayUntil: decoded.displayUntil,
      liveTargetStartDate: decoded.liveTargetStartDate,
      targetLabels: decoded.targetLabels,
      tasks: tasks
    };
  }

  if (!window.BondFactorLabHttp ||
      typeof window.BondFactorLabHttp.createJsonClient !== "function") {
    throw new Error("factor-lab-http.js must load before aifin-shell.js");
  }
  var fetchJson = window.BondFactorLabHttp.createJsonClient({
    fetch: function (url, options) { return window.fetch(url, options); },
    resolveUrl: apiUrl,
    AbortController: window.AbortController,
    setTimeout: function (callback, delay) { return window.setTimeout(callback, delay); },
    clearTimeout: function (timer) { return window.clearTimeout(timer); },
    defaultTimeoutMs: function () { return FACTOR_LAB_REQUEST_TIMEOUT_MS; },
    maxRetryDelayMs: FACTOR_LAB_MAX_RETRY_DELAY_MS,
    onUnauthorized: function () {
      if (window.CustomEvent) {
        window.dispatchEvent(new CustomEvent("bfl:auth-required"));
      }
    }
  });

  function validateFactorLabTasksForCommit(tasks) {
    Object.keys(tasks || {}).forEach(function (taskKey) {
      (tasks[taskKey] || []).forEach(function (scheme) {
        requireSchemeDeploymentDate(scheme, "dashboard scheme");
        requireDashboardOwner(scheme.owner, "dashboard scheme owner");
      });
    });
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
  }

  function scheduleFactorLabRefresh(delayMs) {
    clearFactorLabRefreshTimer();
    if (!factorLabRuntimeState.authenticated || !window.setTimeout || document.visibilityState === "hidden") return;
    factorLabRuntimeState.refreshTimer = window.setTimeout(function () {
      factorLabRuntimeState.refreshTimer = null;
      if (document.visibilityState === "hidden" || getActiveView() !== "factor-lab") return;
      loadFactorLabData({ force: true });
    }, delayMs);
  }

  function dashboardFactorLabCandidate(payload) {
    var decoded = decodeDashboardPayload(payload);
    var viewModel = buildFactorLabViewModel(decoded);
    return {
      tasks: viewModel.tasks,
      targetLabels: viewModel.targetLabels,
      snapshotId: viewModel.snapshotId,
      generatedAt: decoded.generatedAt,
      displayUntil: viewModel.displayUntil,
      liveTargetStartDate: viewModel.liveTargetStartDate,
      source: "dashboard"
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
      return scheme.id === selection.schemeId && scheme.monthlyRows.some(function (row) {
        return row.month === selection.month &&
          (factorLabState.dataSource === "all" || row._source === factorLabState.dataSource);
      });
    });
  }

  function countFactorLabRows(tasks) {
    var counts = { schemes: 0, liveRows: 0, backtestRows: 0 };
    Object.keys(tasks || {}).forEach(function (taskKey) {
      (tasks[taskKey] || []).forEach(function (scheme) {
        counts.schemes += 1;
        (scheme.monthlyRows || []).forEach(function (row) {
          if (row._source === "live") counts.liveRows += row.samples;
          else if (row._source === "backtest") counts.backtestRows += row.samples;
        });
      });
    });
    return counts;
  }

  function commitFactorLabCandidate(candidate, seq) {
    if (!candidate || seq !== factorLabRuntimeState.loadSeq) return false;
    validateFactorLabTasksForCommit(candidate.tasks);
    factorLabRuntimeState.detailSeq += 1;
    if (factorLabRuntimeState.detailController &&
        typeof factorLabRuntimeState.detailController.abort === "function") {
      factorLabRuntimeState.detailController.abort("summary-refreshed");
    }
    factorLabRuntimeState.detailController = null;
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
      aggregateCache: factorLabRuntimeState.aggregateCache,
      detailCache: factorLabRuntimeState.detailCache,
      lastSuccessfulAt: factorLabRuntimeState.lastSuccessfulAt,
      lastSuccessfulGeneratedAt: factorLabRuntimeState.lastSuccessfulGeneratedAt,
      ui: cloneFactorLabUiState(),
      drawer: factorLabDrawerSelection
    };
    try {
      factorTaskSchemes = candidate.tasks;
      factorTargetLabels = nextTargetLabels;
      factorLabRemoteLoaded = true;
      factorLabRemoteLoading = false;
      factorLabApiError = "";
      factorLabDataMode = "fresh";
      factorLabRuntimeState.committedViewModel = candidate;
      factorLabRuntimeState.aggregateCache = new Map();
      factorLabRuntimeState.detailCache = new Map();
      factorLabRuntimeState.lastSuccessfulAt = factorLabNow();
      factorLabRuntimeState.lastSuccessfulGeneratedAt = candidate.generatedAt;

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
      factorLabRuntimeState.aggregateCache = previous.aggregateCache;
      factorLabRuntimeState.detailCache = previous.detailCache;
      factorLabRuntimeState.lastSuccessfulAt = previous.lastSuccessfulAt;
      factorLabRuntimeState.lastSuccessfulGeneratedAt = previous.lastSuccessfulGeneratedAt;
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
    if (factorLabRuntimeState.committedViewModel) {
      factorLabDataMode = "stale";
      renderFactorLabDataStatus();
    } else {
      factorTaskSchemes = initEmptyTaskSchemes();
      factorTargetLabels = Object.assign(Object.create(null), factorDefaultTargetLabels);
      factorLabRuntimeState.aggregateCache = new Map();
      factorLabRuntimeState.detailCache = new Map();
      factorLabRuntimeState.detailSeq += 1;
      if (factorLabRuntimeState.detailController &&
          typeof factorLabRuntimeState.detailController.abort === "function") {
        factorLabRuntimeState.detailController.abort("summary-failed");
      }
      factorLabRuntimeState.detailController = null;
      factorLabDataMode = "error";
      closeFactorCalendar();
      renderFactorLab();
    }
    var delayIndex = Math.min(
      factorLabRuntimeState.consecutiveFailures - 1,
      FACTOR_LAB_RETRY_DELAYS_MS.length - 1
    );
    var retryDelayMs = FACTOR_LAB_RETRY_DELAYS_MS[delayIndex];
    if (error && error.retryAfterAt === Infinity) return false;
    if (error && Number.isFinite(error.retryAfterAt)) {
      retryDelayMs = Math.max(retryDelayMs, error.retryAfterAt - Date.now());
    }
    scheduleFactorLabRefresh(retryDelayMs);
    return false;
  }

  function loadFactorLabData(options) {
    var force = options && options.force === true;
    if (!factorLabRuntimeState.authenticated || !window.fetch) return Promise.resolve(false);
    if (factorLabRemoteLoading) return Promise.resolve(false);
    if (!force && factorLabRemoteLoaded) {
      if (factorLabRuntimeState.refreshTimer === null &&
          factorLabRuntimeState.committedViewModel &&
          document.visibilityState !== "hidden" && getActiveView() === "factor-lab") {
        var snapshotAge = factorLabNow() - factorLabRuntimeState.lastSuccessfulAt;
        scheduleFactorLabRefresh(Math.max(
          0, FACTOR_LAB_HEALTHY_REFRESH_MS - snapshotAge
        ));
      }
      return Promise.resolve(false);
    }

    clearFactorLabRefreshTimer();
    var seq = factorLabRuntimeState.loadSeq + 1;
    factorLabRuntimeState.loadSeq = seq;
    if (!factorLabRuntimeState.committedViewModel) window.__factorLabReady = null;
    if (factorLabRuntimeState.controller &&
        typeof factorLabRuntimeState.controller.abort === "function") {
      factorLabRuntimeState.controller.abort("superseded");
    }
    var controller = window.AbortController ? new window.AbortController() : null;
    factorLabRuntimeState.controller = controller;
    factorLabRemoteLoading = true;
    factorLabDataMode = factorLabRuntimeState.committedViewModel
      ? "refreshing"
      : "loading";
    renderFactorLabDataStatus();
    var signal = controller ? controller.signal : null;

    var candidatePromise = fetchJson(
      "/api/factor-lab/dashboard",
      {
        signal: signal,
        timeoutMs: FACTOR_LAB_REQUEST_TIMEOUT_MS
      }
    ).then(function (payload) {
      if (seq !== factorLabRuntimeState.loadSeq) return null;
      return dashboardFactorLabCandidate(payload);
    });

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
        var snapshotAge = factorLabRuntimeState.lastSuccessfulAt
          ? factorLabNow() - factorLabRuntimeState.lastSuccessfulAt
          : FACTOR_LAB_HEALTHY_REFRESH_MS;
        if (factorLabRuntimeState.consecutiveFailures > 0 ||
            snapshotAge >= FACTOR_LAB_HEALTHY_REFRESH_MS) {
          loadFactorLabData({ force: true });
        } else {
          scheduleFactorLabRefresh(
            FACTOR_LAB_HEALTHY_REFRESH_MS - snapshotAge
          );
        }
      }
    });
  }

  function startAuthenticatedFactorLab() {
    if (factorLabRuntimeState.authenticated) return Promise.resolve(false);
    factorLabRuntimeState.authenticated = true;
    setActiveRoute("/factor-lab", false);
    startFactorLabAutoRefresh();
    return loadFactorLabData({ force: true });
  }

  function clearAuthenticatedFactorLab() {
    factorLabRuntimeState.authenticated = false;
    clearFactorLabRefreshTimer();
    factorLabRuntimeState.loadSeq += 1;
    factorLabRuntimeState.detailSeq += 1;
    if (factorLabRuntimeState.controller &&
        typeof factorLabRuntimeState.controller.abort === "function") {
      factorLabRuntimeState.controller.abort("authentication-ended");
    }
    if (factorLabRuntimeState.detailController &&
        typeof factorLabRuntimeState.detailController.abort === "function") {
      factorLabRuntimeState.detailController.abort("authentication-ended");
    }
    factorLabRuntimeState.controller = null;
    factorLabRuntimeState.detailController = null;
    factorLabRuntimeState.committedViewModel = null;
    factorLabRuntimeState.aggregateCache = new Map();
    factorLabRuntimeState.detailCache = new Map();
    factorLabRuntimeState.lastSuccessfulAt = 0;
    factorLabRuntimeState.lastSuccessfulGeneratedAt = "";
    factorTaskSchemes = initEmptyTaskSchemes();
    factorTargetLabels = Object.assign(Object.create(null), factorDefaultTargetLabels);
    factorLabRemoteLoaded = false;
    factorLabRemoteLoading = false;
    factorLabApiError = "";
    factorLabDataMode = "loading";
    factorLabDrawerSelection = null;
    window.__factorLabReady = null;
    closeFactorRemark(false);
    closeFactorCalendar();
    [
      "factorTaskMatrixBody",
      "factorSchemeRankingBody",
      "factorMonthlyTableBody",
      "factorDailyTableBody",
      "factorTrendChart"
    ].forEach(function (id) {
      var element = document.getElementById(id);
      if (element) element.textContent = "";
    });
  }

  window.BondFactorLabDashboard = Object.freeze({
    start: startAuthenticatedFactorLab,
    stop: clearAuthenticatedFactorLab
  });
  if (window.__BFL_ENABLE_TEST_HOOKS__ === true) {
    window.__BFL_TEST_HOOKS__ = Object.freeze({
      fetchJson: fetchJson,
      load: loadFactorLabData,
      start: startAuthenticatedFactorLab,
      stop: clearAuthenticatedFactorLab,
      setRequestTimeoutMs: function (timeoutMs) {
        FACTOR_LAB_REQUEST_TIMEOUT_MS = timeoutMs;
      },
      setRetryDelaysMs: function (delays) {
        FACTOR_LAB_RETRY_DELAYS_MS = delays.slice();
      },
      state: function () {
        return {
          authenticated: factorLabRuntimeState.authenticated,
          loadSeq: factorLabRuntimeState.loadSeq,
          remoteLoaded: factorLabRemoteLoaded,
          remoteLoading: factorLabRemoteLoading,
          dataMode: factorLabDataMode,
          apiError: factorLabApiError,
          snapshotId: factorLabRuntimeState.committedViewModel
            ? factorLabRuntimeState.committedViewModel.snapshotId
            : null,
          consecutiveFailures: factorLabRuntimeState.consecutiveFailures,
          hasRefreshTimer: factorLabRuntimeState.refreshTimer !== null
        };
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
    var counts = {
      samples: 0,
      metricSamples: 0,
      correct: 0,
      predictedUp: 0,
      predictedDown: 0,
      predictedFlat: 0,
      actualUp: 0,
      actualDown: 0,
      actualFlat: 0,
      upTruePositive: 0,
      downTruePositive: 0
    };
    getVisibleRowsForScheme(scheme).forEach(function (row) {
      Object.keys(counts).forEach(function (key) { counts[key] += row[key]; });
    });
    var metric = metricFromDashboardCounts(counts);
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
    ["is-loading", "is-fresh", "is-stale", "is-error"].forEach(function (className) {
      status.classList.remove(className);
    });
    if (factorLabRemoteLoading) {
      status.classList.add("is-loading");
      text.textContent = "数据刷新中";
    } else if (factorLabApiError && factorLabRuntimeState.committedViewModel) {
      status.classList.add("is-stale");
      text.textContent = "刷新失败，显示 " + formatFactorLabGeneratedAt(
        factorLabRuntimeState.lastSuccessfulGeneratedAt
      ) + " 数据";
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
    status.setAttribute(
      "title",
      factorLabApiError && factorLabRuntimeState.committedViewModel
        ? text.textContent + "；" + factorLabApiError
        : factorLabApiError || text.textContent
    );
  }

  function factorLabSchemeCountsAvailable(dataState) {
    var state = dataState || {};
    return Boolean(state.hasCommitted ||
      (state.remoteLoaded && !state.remoteLoading && !state.apiError &&
        state.dataMode !== "loading" && state.dataMode !== "error"));
  }

  function currentFactorLabDataState() {
    return {
      remoteLoaded: factorLabRemoteLoaded,
      remoteLoading: factorLabRemoteLoading,
      apiError: factorLabApiError,
      dataMode: factorLabDataMode,
      hasCommitted: Boolean(factorLabRuntimeState.committedViewModel)
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
    var productionMarker = scheme.isProduction
      ? '<span class="factor-production-marker" role="img" aria-label="生产方案"></span>'
      : '';
    var remark = getSchemeRemark(scheme);
    var remarkControl = remark
      ? '<button type="button" class="factor-remark-detail" data-factor-remark-open="' + escapeHtml(scheme.id) + '" aria-controls="factorRemarkPopover" aria-expanded="false"><span aria-hidden="true">ⓘ</span><span>详情</span></button>'
      : '<span class="factor-remark-empty">--</span>';
    return '<tr' + selectedClass + ' data-factor-scheme-id="' + escapeHtml(scheme.id) + '">' +
      '<td>' + (index + 1) + '</td>' +
      '<td><div class="factor-scheme-heading"><strong class="factor-scheme-name" title="' + schemeName + '">' + schemeName + '</strong>' + productionMarker + '</div></td>' +
      '<td class="' + getMetricClass(metric.overall) + '"><div class="factor-score-cell"><span>' + formatPercent(metric.overall) + '（' + metric.correct + '/' + metricSamples + '）</span><span class="factor-score-bar" aria-hidden="true"><span style="width:' + barWidth.toFixed(1) + '%"></span></span></div></td>' +
      '<td><span class="factor-sample-count">' + metric.samples + '</span></td>' +
      '<td class="' + getMetricClass(metric.upPrecision) + '">' + formatPercent(metric.upPrecision) + '</td>' +
      '<td class="' + getMetricClass(metric.downPrecision) + '">' + formatPercent(metric.downPrecision) + '</td>' +
      '<td class="mono">' + escapeHtml(deploymentDate) + '</td>' +
      '<td>' + escapeHtml(requireDashboardOwner(scheme.owner, "ranking scheme owner")) + '</td>' +
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
    if (src === "live") {
      var committed = factorLabRuntimeState.committedViewModel;
      var task = getTaskByKey(factorLabState.selectedTaskKey);
      return factorMonthRange(
        committed && committed.liveTargetStartDate
          ? dashboardLiveDisplayStartMonth(
            committed.liveTargetStartDate,
            task.taskType
          )
          : "",
        committed && committed.displayUntil ? committed.displayUntil.slice(0, 7) : ""
      );
    }
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

  function factorMonthRange(startMonth, endMonth) {
    if (!/^\d{4}-\d{2}$/.test(startMonth) || !/^\d{4}-\d{2}$/.test(endMonth) ||
        endMonth < startMonth) return [];
    var months = [];
    var year = Number(startMonth.slice(0, 4));
    var month = Number(startMonth.slice(5, 7));
    var endYear = Number(endMonth.slice(0, 4));
    var endMonthNumber = Number(endMonth.slice(5, 7));
    while (year < endYear || (year === endYear && month <= endMonthNumber)) {
      months.push(String(year).padStart(4, "0") + "-" + String(month).padStart(2, "0"));
      month += 1;
      if (month === 13) {
        year += 1;
        month = 1;
      }
    }
    return months;
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
    var task = getTaskByKey(factorLabState.selectedTaskKey);
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
      svg += '<text class="factor-trend-axis" x="' + x(index).toFixed(1) + '" y="' + (height - 14) + '" text-anchor="' + trendMonthLabelAnchor(index, rows.length) + '">' + escapeHtml(formatFactorPeriodLabel(task, row.month)) + '</text>';
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
        return [
          x(index),
          y(row[metric.id]),
          row[metric.id],
          formatFactorPeriodLabel(task, row.month)
        ];
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

  function factorDetailPresentation(task, month) {
    if (isMonthlyAverageTask(task)) {
      return {
        title: month + " 月度平均预测明细",
        dateHeader: "目标月",
        note: "",
        emptyText: "当前月份暂无预测明细",
        buttonLabel: "打开月度平均预测明细"
      };
    }
    if (isQuarterlyAverageTask(task)) {
      return {
        title: formatQuarterlyAverageTargetQuarter(month) + " 季度平均预测明细",
        dateHeader: "目标季度",
        note: "",
        emptyText: "当前季度暂无预测明细",
        buttonLabel: "打开季度平均预测明细"
      };
    }
    if (isAnnualAverageTask(task)) {
      return {
        title: formatAnnualAverageTargetYear(month) + " 年度平均预测明细",
        dateHeader: "目标年度",
        note: "",
        emptyText: "当前年度暂无预测明细",
        buttonLabel: "打开年度平均预测明细"
      };
    }
    var weekly = isWeeklyTask(task);
    var weeklyAverage = isWeeklyAverageTask(task);
    return {
      title: month + (weekly ? " 周度验证表" : " 每日验证表"),
      dateHeader: weeklyAverage ? "目标周" : "目标日",
      note: weekly
        ? (
            weeklyAverage
              ? "表内可继续滚动查看该月全部周度预测；目标周按该周最后可验证交易日标记。"
              : "表内可继续滚动查看该月全部周度预测；目标日为下周最后一个交易日。"
          )
        : "表内可继续滚动查看该月全部交易日的预测。",
      emptyText: "当前月份暂无每日明细",
      buttonLabel: "查看" + month + "每日明细"
    };
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
        html += '<tr class="factor-live-divider"><td colspan="10">&#9660; ' + escapeHtml(liveDividerText()) + '</td></tr>';
      }
      prevSource = row._source || prevSource;
      html += '<tr>';
      html += '<td>' + escapeHtml(formatFactorPeriodLabel(task, row.month)) + '</td>';
      html += '<td><strong>' + row.samples + '</strong></td>';
      html += '<td>' + escapeHtml(row.actualDist) + '</td>';
      html += '<td>' + escapeHtml(row.predictedDist) + '</td>';
      var rowMetricSamples = requireMetricSamples(row, "monthly row");
      html += '<td class="' + getMetricClass(row.overall) + '">' + formatPercent(row.overall) + '（' + row.correct + '/' + rowMetricSamples + '）</td>';
      html += '<td class="' + getMetricClass(row.upPrecision) + '">' + formatPercent(row.upPrecision) + '</td>';
      html += '<td class="' + getMetricClass(row.upRecall) + '">' + formatPercent(row.upRecall) + '</td>';
      html += '<td class="' + getMetricClass(row.downPrecision) + '">' + formatPercent(row.downPrecision) + '</td>';
      html += '<td class="' + getMetricClass(row.downRecall) + '">' + formatPercent(row.downRecall) + '</td>';
      var buttonLabel = factorDetailPresentation(task, row.month).buttonLabel;
      html += '<td><button type="button" class="factor-calendar-link" data-factor-calendar-month="' + escapeHtml(row.month) + '" aria-label="' + escapeHtml(buttonLabel) + '">' + calendarIcon + '</button></td>';
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

  function renderFactorDailyRows(month, detailState) {
    var body = document.getElementById("factorDailyTableBody");
    var title = document.getElementById("factorCalendarTitle");
    var meta = document.getElementById("factorCalendarMeta");
    if (!body || !title || !meta) return;

    var task = getTaskByKey(factorLabState.selectedTaskKey);
    var scheme = getSelectedScheme();
    var isMonthlyAverage = isMonthlyAverageTask(task);
    var isQuarterlyAverage = isQuarterlyAverageTask(task);
    var isAnnualAverage = isAnnualAverageTask(task);
    var presentation = factorDetailPresentation(task, month);
    var dateHeader = document.getElementById("factorDailyDateHeader");
    var note = document.getElementById("factorCalendarNote");
    title.textContent = presentation.title;
    meta.textContent = (scheme ? scheme.name : "--") + " · " + task.label;
    if (dateHeader) dateHeader.textContent = presentation.dateHeader;
    if (note) {
      note.textContent = presentation.note;
      note.hidden = !presentation.note;
    }

    var html = "";
    var monthLabel = month.slice(5, 7);
    if (!detailState || detailState.status === "loading") {
      body.innerHTML = '<tr><td colspan="5" class="factor-empty-cell">正在加载预测明细…</td></tr>';
      return;
    }
    if (detailState.status === "error") {
      body.innerHTML = '<tr><td colspan="5" class="factor-empty-cell">明细加载失败，' +
        '<button type="button" class="factor-calendar-retry" data-factor-calendar-retry>重新加载</button></td></tr>';
      return;
    }
    var rows = detailState.rows || [];
    if (!rows.length) {
      var emptyText = isMonthlyAverage || isQuarterlyAverage || isAnnualAverage
        ? presentation.emptyText
        : "当前月份暂无每日明细";
      body.innerHTML = '<tr><td colspan="5" class="factor-empty-cell">' +
        escapeHtml(emptyText) + '</td></tr>';
      return;
    }
    rows.forEach(function (row) {
      var predictedClass = getDirectionClass(row.predicted);
      var actualClass = getDirectionClass(row.actual);
      var displayDay = row.day.replace(/^\d{2}/, monthLabel);
      var result = renderDailyResult(row);
      html += '<tr>';
      if (isMonthlyAverage) {
        html += '<td class="mono">' + escapeHtml(formatMonthlyAveragePredictDate(row.predictDate)) + '</td>';
        html += '<td class="mono">' + escapeHtml(formatMonthlyAverageTargetMonth(row.targetMonth || month)) + '</td>';
      } else if (isQuarterlyAverage) {
        html += '<td class="mono">' + escapeHtml(formatQuarterlyAveragePredictDate(row.predictDate)) + '</td>';
        html += '<td class="mono">' + escapeHtml(formatQuarterlyAverageTargetQuarter(row.targetMonth || month)) + '</td>';
      } else if (isAnnualAverage) {
        html += '<td class="mono">' + escapeHtml(formatAnnualAveragePredictDate(row.predictDate)) + '</td>';
        html += '<td class="mono">' + escapeHtml(formatAnnualAverageTargetYear(row.targetMonth || month)) + '</td>';
      } else {
        html += dateCellHtml(row.predictDate, "--");
        html += dateCellHtml(row.targetDate, displayDay);
      }
      html += '<td class="' + predictedClass + '">' + escapeHtml(row.predicted) + '</td>';
      html += '<td class="' + actualClass + '">' + escapeHtml(row.actual) + '</td>';
      html += '<td>' + result + '</td>';
      html += '</tr>';
    });
    body.innerHTML = html;
  }

  function factorDetailCacheKey(snapshotId, schemeId, month, source) {
    return [snapshotId, schemeId, month, source].join("\u0000");
  }

  function loadFactorCalendarDetail(scheme, month, source, force) {
    var committed = factorLabRuntimeState.committedViewModel;
    if (!committed || !scheme) return Promise.reject(new Error("dashboard summary is unavailable"));
    var key = factorDetailCacheKey(committed.snapshotId, scheme.schemeId, month, source);
    var cache = factorLabRuntimeState.detailCache;
    if (force) cache.delete(key);
    if (cache.has(key)) {
      return cache.get(key);
    }
    var query = "?scheme-id=" + encodeURIComponent(scheme.schemeId) +
      "&month=" + encodeURIComponent(month) +
      "&source=" + encodeURIComponent(source);
    var controller = window.AbortController ? new window.AbortController() : null;
    factorLabRuntimeState.detailController = controller;
    var promise = fetchJson(
      "/api/factor-lab/dashboard" + query,
      {
        signal: controller ? controller.signal : null,
        timeoutMs: FACTOR_LAB_REQUEST_TIMEOUT_MS
      }
    ).then(function (payload) {
      return decodeDashboardDetailPayload(payload, {
        schemeId: scheme.schemeId,
        month: month,
        source: source,
        liveTargetStartDate: committed.liveTargetStartDate
      }).map(function (row) {
        return dashboardDetailRow(row, scheme.taskType);
      });
    }).catch(function (error) {
      if (cache.get(key) === promise) cache.delete(key);
      throw error;
    });
    cache.set(key, promise);
    return promise;
  }

  function openFactorCalendar(month, trigger, options) {
    var drawer = document.getElementById("factorCalendarDrawer");
    if (!drawer) return;
    var scheme = getSelectedScheme();
    factorLabDrawerSelection = scheme ? {
      taskKey: factorLabState.selectedTaskKey,
      schemeId: scheme.id,
      month: month,
      source: factorLabState.dataSource
    } : null;
    renderFactorDailyRows(month, { status: "loading" });
    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
    positionFactorCalendarPanel(trigger);
    if (!scheme) return;
    var detailSeq = factorLabRuntimeState.detailSeq + 1;
    var summarySeq = factorLabRuntimeState.loadSeq;
    factorLabRuntimeState.detailSeq = detailSeq;
    loadFactorCalendarDetail(
      scheme,
      month,
      factorLabState.dataSource,
      options && options.force === true
    ).then(function (rows) {
      var selection = factorLabDrawerSelection;
      if (detailSeq !== factorLabRuntimeState.detailSeq ||
          summarySeq !== factorLabRuntimeState.loadSeq || !selection ||
          selection.schemeId !== scheme.id || selection.month !== month ||
          selection.source !== factorLabState.dataSource) return;
      renderFactorDailyRows(month, { status: "ready", rows: rows });
    }).catch(function () {
      if (detailSeq !== factorLabRuntimeState.detailSeq ||
          summarySeq !== factorLabRuntimeState.loadSeq || !factorLabDrawerSelection) return;
      renderFactorDailyRows(month, { status: "error" });
    });
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
    factorLabRuntimeState.detailSeq += 1;
    if (factorLabRuntimeState.detailController &&
        typeof factorLabRuntimeState.detailController.abort === "function") {
      factorLabRuntimeState.detailController.abort("drawer-closed");
    }
    factorLabRuntimeState.detailController = null;
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

        var calendarRetry = event.target.closest("[data-factor-calendar-retry]");
        if (calendarRetry && factorLabDrawerSelection) {
          var retry = factorLabDrawerSelection;
          factorLabState.selectedTaskKey = retry.taskKey;
          factorLabState.selectedSchemeId = retry.schemeId;
          openFactorCalendar(retry.month, null, { force: true });
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

  /* ─── Init ─── */
  clearAuthenticatedFactorLab();
})();

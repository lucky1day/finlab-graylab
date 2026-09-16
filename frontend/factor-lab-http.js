(function (root, factory) {
  "use strict";

  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.BondFactorLabHttp = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var HTTP_DATE_PATTERN = /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} GMT$/;

  function createJsonClient(config) {
    config = config || {};
    var maxRetryDelayMs = Number(config.maxRetryDelayMs);
    if (!(maxRetryDelayMs > 0)) maxRetryDelayMs = 2147483647;

    return function fetchJson(url, options) {
      options = options || {};
      var requestUrl = config.resolveUrl ? config.resolveUrl(url) : url;
      var configuredHeaders = options.headers || {};
      var requestOptions = {
        cache: "no-store",
        credentials: options.credentials || "same-origin",
        method: options.method || "GET",
        headers: Object.assign({ Accept: "application/json" }, configuredHeaders)
      };
      if (Object.prototype.hasOwnProperty.call(options, "body")) {
        requestOptions.headers["Content-Type"] = "application/json";
        requestOptions.body = JSON.stringify(options.body);
      }
      var externalSignal = options.signal || null;
      var AbortControllerType = config.AbortController;
      var controller = AbortControllerType ? new AbortControllerType() : null;
      var signal = controller ? controller.signal : externalSignal;
      if (signal) requestOptions.signal = signal;
      var timeoutMs = Number(options.timeoutMs);
      if (!(timeoutMs > 0)) {
        timeoutMs = Number(
          typeof config.defaultTimeoutMs === "function"
            ? config.defaultTimeoutMs()
            : config.defaultTimeoutMs
        );
      }
      if (!(timeoutMs > 0)) {
        throw new Error("defaultTimeoutMs must be greater than zero");
      }
      var timeoutId = null;
      var removeExternalAbortListener = null;
      var timeoutError = new Error("Request timed out after " + timeoutMs + "ms");
      timeoutError.name = "FactorLabTimeoutError";
      timeoutError.code = "request_timeout";
      timeoutError.timeoutMs = timeoutMs;
      var responseError = null;

      var cancellationPromise = new Promise(function (_resolve, reject) {
        if (externalSignal) {
          var abortFromExternalSignal = function () {
            var reason = externalSignal.reason || "external-abort";
            if (controller && !controller.signal.aborted) controller.abort(reason);
            var abortError = new Error("Request was cancelled");
            abortError.name = "AbortError";
            abortError.code = "request_aborted";
            abortError.reason = reason;
            if (responseError) copyResponseMetadata(abortError, responseError);
            reject(abortError);
          };
          if (externalSignal.aborted) {
            abortFromExternalSignal();
            return;
          }
          if (typeof externalSignal.addEventListener === "function") {
            externalSignal.addEventListener("abort", abortFromExternalSignal, { once: true });
            removeExternalAbortListener = function () {
              externalSignal.removeEventListener("abort", abortFromExternalSignal);
            };
          }
        }
        if (config.setTimeout) {
          timeoutId = config.setTimeout(function () {
            if (responseError) copyResponseMetadata(timeoutError, responseError);
            reject(timeoutError);
            if (controller && !controller.signal.aborted) controller.abort("request-timeout");
          }, timeoutMs);
        }
      });

      var responsePromise = Promise.resolve().then(function () {
        return config.fetch(requestUrl, requestOptions);
      }).then(function (response) {
        if (response.ok) return response.json();
        var error = new Error("HTTP " + response.status + " " + requestUrl);
        error.name = "FactorLabHttpError";
        error.status = response.status;
        error.requestUrl = requestUrl;
        error.requestId = response.headers && typeof response.headers.get === "function"
          ? response.headers.get("X-Request-ID")
          : null;
        attachRetryAfter(error, response, maxRetryDelayMs);
        responseError = error;
        if (response.status === 401 && options.handleUnauthorized !== false &&
            !(signal && signal.aborted) && config.onUnauthorized) {
          config.onUnauthorized();
        }
        var bodyPromise = typeof response.json === "function"
          ? response.json().catch(function () { return null; })
          : Promise.resolve(null);
        return bodyPromise.then(function (body) {
          error.body = body;
          throw error;
        });
      });

      return Promise.race([responsePromise, cancellationPromise]).then(function (payload) {
        cleanup();
        return payload;
      }, function (error) {
        cleanup();
        throw error;
      });

      function cleanup() {
        if (timeoutId !== null && config.clearTimeout) config.clearTimeout(timeoutId);
        if (removeExternalAbortListener) removeExternalAbortListener();
      }
    };
  }

  function copyResponseMetadata(target, source) {
    ["status", "requestId", "retryAfter", "retryAfterAt", "requestUrl"].forEach(
      function (field) {
        if (source[field] !== undefined) target[field] = source[field];
      }
    );
  }

  function attachRetryAfter(error, response, maxRetryDelayMs) {
    var retryAfter = response.headers && typeof response.headers.get === "function"
      ? response.headers.get("Retry-After")
      : null;
    if (retryAfter === null || retryAfter === undefined) return;

    var retryAfterValue = String(retryAfter).trim();
    var retryAfterAt = null;
    error.retryAfter = retryAfterValue;
    if (/^\d+$/.test(retryAfterValue)) {
      var retryAfterSeconds = Number(retryAfterValue);
      if (Number.isSafeInteger(retryAfterSeconds) &&
          retryAfterSeconds <= Math.floor(maxRetryDelayMs / 1000)) {
        retryAfterAt = Date.now() + retryAfterSeconds * 1000;
      } else {
        retryAfterAt = Infinity;
      }
    } else if (HTTP_DATE_PATTERN.test(retryAfterValue)) {
      var parsedRetryAfter = Date.parse(retryAfterValue);
      if (Number.isFinite(parsedRetryAfter) &&
          new Date(parsedRetryAfter).toUTCString() === retryAfterValue) {
        retryAfterAt = parsedRetryAfter - Date.now() <= maxRetryDelayMs
          ? parsedRetryAfter
          : Infinity;
      }
    }
    if (retryAfterAt === Infinity) {
      error.retryAfterAt = Infinity;
    } else if (Number.isFinite(retryAfterAt)) {
      error.retryAfterAt = Math.max(Date.now(), retryAfterAt);
    }
  }

  return Object.freeze({
    createJsonClient: createJsonClient
  });
});

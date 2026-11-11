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

  function createJsonClient(config) {
    config = config || {};

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
        responseError = error;
        if (response.status === 401 && options.handleUnauthorized !== false &&
            !(signal && signal.aborted) &&
            (!options.isCurrent || options.isCurrent()) && config.onUnauthorized) {
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
    ["status", "requestId", "requestUrl"].forEach(
      function (field) {
        if (source[field] !== undefined) target[field] = source[field];
      }
    );
  }

  return Object.freeze({
    createJsonClient: createJsonClient
  });
});

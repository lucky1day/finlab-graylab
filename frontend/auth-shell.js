(function () {
  "use strict";

  var state = {
    user: null,
    accountMenuOpen: false,
    userMoreMenuId: null,
    userMoreMenuButton: null,
    users: [],
    identitySeq: 0,
    activeRequests: new Set()
  };

  var authGate = document.getElementById("authGate");
  var shell = document.getElementById("aifin-shell");
  var loadingView = document.getElementById("authLoading");
  var loginView = document.getElementById("authLogin");
  var usersView = document.getElementById("authUsersView");
  var factorView = document.querySelector('[data-view="factor-lab"]');

  function publicBasePath() {
    var pathname = window.location.pathname || "/";
    return pathname === "/bond-factor-lab" || pathname.indexOf("/bond-factor-lab/") === 0
      ? "/bond-factor-lab"
      : "";
  }

  function apiUrl(path) {
    return publicBasePath() + path;
  }

  function errorMessage(errorCode) {
    var messages = {
      invalid_credentials: "用户名或密码错误",
      invalid_current_password: "当前密码错误",
      invalid_password: "密码不得少于6位，其中至少包含大写字母、小写字母和数字",
      invalid_profile: "用户姓名或机构名称超出允许长度",
      invalid_username: "用户名格式不正确",
      username_taken: "该用户名已被使用",
      protected_admin: "受保护初始管理员不允许执行此操作",
      cannot_modify_self: "管理员不能对自己的角色、状态或密码执行此操作",
      last_active_admin: "必须保留至少一个正常状态的管理员",
      forbidden: "没有权限执行此操作",
      user_not_found: "用户不存在"
    };
    if (errorCode === "request_outcome_unknown") {
      return "请求结果待确认，请刷新或重新登录后核验";
    }
    return messages[errorCode] || "操作失败，请稍后重试";
  }

  function emitError(host, message) {
    if (!host) return;
    host.textContent = message || "";
    host.hidden = !message;
  }

  if (!window.BondFactorLabHttp) {
    throw new Error("factor-lab-http.js must load before auth-shell.js");
  }

  var authHttpClient = window.BondFactorLabHttp.createJsonClient({
    fetch: function (url, options) { return window.fetch(url, options); },
    resolveUrl: apiUrl,
    AbortController: window.AbortController,
    setTimeout: function (callback, delay) { return window.setTimeout(callback, delay); },
    clearTimeout: function (token) { window.clearTimeout(token); },
    defaultTimeoutMs: 6000,
    onUnauthorized: function () { showLogin(); }
  });

  function abortIdentityRequests(reason) {
    state.activeRequests.forEach(function (controller) {
      if (!controller.signal.aborted) controller.abort(reason);
    });
    state.activeRequests.clear();
  }

  function advanceIdentity(reason) {
    state.identitySeq += 1;
    abortIdentityRequests(reason);
  }

  function validUser(value) {
    return value && typeof value === "object" &&
      Number.isInteger(value.id) && value.id > 0 &&
      typeof value.username === "string" && value.username.length > 0 &&
      (value.role === "admin" || value.role === "user") &&
      (value.status === "active" || value.status === "disabled") &&
      typeof value.is_protected_admin === "boolean" &&
      typeof value.must_change_password === "boolean" &&
      typeof value.created_at === "string";
  }

  function validateAuthPayload(path, method, payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("invalid_auth_payload");
    }
    if (path === "/api/admin/users" && method === "GET") {
      if (!Array.isArray(payload.users) || !payload.users.every(validUser)) {
        throw new Error("invalid_auth_payload");
      }
      return payload;
    }
    if (path === "/api/auth/logout" || path === "/api/auth/change-password") {
      return payload;
    }
    if (!validUser(payload.user)) throw new Error("invalid_auth_payload");
    return payload;
  }

  function apiRequest(path, options) {
    var requestOptions = options || {};
    var identitySeq = state.identitySeq;
    var controller = new window.AbortController();
    state.activeRequests.add(controller);
    var clientOptions = {
      method: requestOptions.method || "GET",
      signal: controller.signal,
      handleUnauthorized: path !== "/api/auth/login"
    };
    if (Object.prototype.hasOwnProperty.call(requestOptions, "body")) {
      clientOptions.body = requestOptions.body;
    }
    return authHttpClient(path, clientOptions).then(function (payload) {
      if (identitySeq !== state.identitySeq) {
        var stale = new Error("stale identity response");
        stale.name = "AbortError";
        stale.code = "request_aborted";
        throw stale;
      }
      return validateAuthPayload(path, clientOptions.method, payload);
    }).catch(function (error) {
      if (error && error.code === "request_timeout" &&
          (requestOptions.method || "GET") !== "GET") {
        error.errorCode = "request_outcome_unknown";
      } else if (error && error.body && error.body.error_code) {
        error.errorCode = error.body.error_code;
      } else if (error && !error.errorCode) {
        error.errorCode = "request_failed";
      }
      throw error;
    }).finally(function () {
      state.activeRequests.delete(controller);
    });
  }

  function stopDashboard() {
    if (window.BondFactorLabDashboard) {
      window.BondFactorLabDashboard.stop();
    }
  }

  function startDashboard() {
    if (window.BondFactorLabDashboard) {
      window.BondFactorLabDashboard.start();
    }
  }

  function clearAuthDialogs() {
    document.querySelectorAll(".auth-dialog[open]").forEach(function (dialog) {
      var form = dialog.querySelector("form");
      if (form) form.reset();
      emitError(dialog.querySelector(".auth-error"), "");
      dialog.close();
      delete dialog.returnFocusButton;
    });
  }

  function showGate(view) {
    stopDashboard();
    clearAuthDialogs();
    shell.hidden = true;
    shell.setAttribute("aria-hidden", "true");
    authGate.hidden = false;
    loadingView.hidden = view !== loadingView;
    loginView.hidden = view !== loginView;
    closeAccountMenu(false);
    closeUserMoreMenu(false);
  }

  function showLogin(message) {
    advanceIdentity("identity-changed");
    state.user = null;
    state.users = [];
    var usersBody = document.getElementById("authUsersBody");
    if (usersBody) usersBody.textContent = "";
    var usersCount = document.getElementById("authUsersCount");
    if (usersCount) usersCount.textContent = "0 个账户";
    showGate(loginView);
    var form = document.getElementById("authLoginForm");
    if (form) form.reset();
    emitError(document.getElementById("authLoginError"), message || "");
    window.setTimeout(function () {
      var username = document.getElementById("authUsername");
      if (username) username.focus();
    }, 0);
  }

  function showAuthenticated(user) {
    if (!validUser(user)) throw new Error("invalid_auth_payload");
    advanceIdentity("identity-changed");
    state.user = user;
    authGate.hidden = true;
    shell.hidden = false;
    shell.setAttribute("aria-hidden", "false");
    document.getElementById("authAccountName").textContent = user.username;
    document.getElementById("authUsersNav").hidden = user.role !== "admin";
    showFactorLab();
  }

  function showFactorLab() {
    if (!state.user) return;
    closeUserMoreMenu(false);
    usersView.hidden = true;
    factorView.hidden = false;
    document.getElementById("factorDataStatus").hidden = false;
    document.getElementById("factorLabNav").classList.add("is-active");
    document.getElementById("authUsersNav").classList.remove("is-active");
    startDashboard();
  }

  function showUsers() {
    if (!state.user || state.user.role !== "admin") return;
    stopDashboard();
    factorView.hidden = true;
    usersView.hidden = false;
    document.getElementById("factorDataStatus").hidden = true;
    document.getElementById("factorLabNav").classList.remove("is-active");
    document.getElementById("authUsersNav").classList.add("is-active");
    loadUsers();
  }

  function submitPasswordForm(form) {
    var currentPassword = form.elements.currentPassword.value;
    var newPassword = form.elements.newPassword.value;
    var confirmPassword = form.elements.confirmPassword.value;
    var errorHost = form.querySelector(".auth-error");
    if (newPassword !== confirmPassword) {
      emitError(errorHost, "两次输入的新密码不一致");
      return Promise.reject(new Error("password_confirmation_mismatch"));
    }
    emitError(errorHost, "");
    var saveState = beginDialogSave(form, "修改中…");
    if (!saveState) return Promise.resolve();
    return apiRequest("/api/auth/change-password", {
      method: "POST",
      body: {
        current_password: currentPassword,
        new_password: newPassword
      }
    }).then(function () {
      form.reset();
      var dialog = form.closest("dialog");
      if (dialog) delete dialog.returnFocusButton;
      if (dialog && dialog.open) dialog.close();
      showLogin("密码已修改，请使用新密码重新登录");
    }).catch(function (error) {
      if (error.message === "password_confirmation_mismatch") throw error;
      emitError(errorHost, errorMessage(error.errorCode));
      throw error;
    }).finally(function () {
      endDialogSave(saveState);
    });
  }

  function closeAccountMenu(returnFocus) {
    var button = document.getElementById("authAccountButton");
    var menu = document.getElementById("authAccountMenu");
    state.accountMenuOpen = false;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (returnFocus) button.focus();
  }

  function openAccountMenu() {
    var button = document.getElementById("authAccountButton");
    var menu = document.getElementById("authAccountMenu");
    state.accountMenuOpen = true;
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    var first = menu.querySelector('[role="menuitem"]');
    if (first) first.focus();
  }

  function formatCreatedAt(value) {
    var match = String(value || "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
    return match ? match[1] + " " + match[2] : String(value || "");
  }

  function actionButton(label, action, userId, disabled) {
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.dataset.userAction = action;
    button.dataset.userId = String(userId);
    button.disabled = Boolean(disabled);
    return button;
  }

  function closeUserMoreMenu(returnFocus) {
    var menu = document.getElementById("authUserMoreMenu");
    var button = state.userMoreMenuButton;
    menu.hidden = true;
    menu.classList.remove("is-open");
    if (button) button.setAttribute("aria-expanded", "false");
    state.userMoreMenuId = null;
    state.userMoreMenuButton = null;
    if (returnFocus && button && document.body.contains(button)) button.focus();
  }

  function openUserMoreMenu(button, userId) {
    var menu = document.getElementById("authUserMoreMenu");
    closeUserMoreMenu(false);
    state.userMoreMenuId = userId;
    state.userMoreMenuButton = button;
    button.setAttribute("aria-expanded", "true");
    menu.hidden = false;
    var rect = button.getBoundingClientRect();
    var menuWidth = menu.offsetWidth;
    var menuHeight = menu.offsetHeight;
    var left = Math.min(
      window.innerWidth - menuWidth - 12,
      Math.max(12, rect.right - menuWidth)
    );
    var top = rect.bottom + 8;
    if (top + menuHeight > window.innerHeight - 12) {
      top = Math.max(12, rect.top - menuHeight - 8);
    }
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top) + "px";
    window.requestAnimationFrame(function () {
      if (state.userMoreMenuButton !== button) return;
      menu.classList.add("is-open");
      var first = menu.querySelector('[role="menuitem"]');
      if (first) first.focus();
    });
  }

  function renderUsers() {
    closeUserMoreMenu(false);
    var body = document.getElementById("authUsersBody");
    body.textContent = "";
    document.getElementById("authUsersCount").textContent = state.users.length + " 个账户";
    state.users.forEach(function (user) {
      var row = document.createElement("tr");
      var identity = document.createElement("td");
      var identityInner = document.createElement("div");
      identityInner.className = "auth-user-identity";
      var username = document.createElement("strong");
      username.textContent = user.username;
      identityInner.appendChild(username);
      identity.appendChild(identityInner);
      row.appendChild(identity);

      [user.full_name, user.organization_name].forEach(function (value) {
        var profileCell = document.createElement("td");
        profileCell.className = value ? "auth-user-profile-value" : "auth-user-profile-value is-empty";
        profileCell.textContent = value || "未填写";
        row.appendChild(profileCell);
      });

      var roleCell = document.createElement("td");
      var roleBadge = document.createElement("span");
      roleBadge.className = "auth-user-badge " + (user.role === "admin" ? "is-admin" : "is-user");
      roleBadge.textContent = user.role === "admin" ? "管理员" : "普通用户";
      roleCell.appendChild(roleBadge);
      row.appendChild(roleCell);

      var statusCell = document.createElement("td");
      var statusBadge = document.createElement("span");
      statusBadge.className = "auth-user-status " + (user.status === "active" ? "is-active" : "is-disabled");
      statusBadge.textContent = user.status === "active" ? "正常" : "已停用";
      statusCell.appendChild(statusBadge);
      row.appendChild(statusCell);

      var createdCell = document.createElement("td");
      createdCell.className = "auth-user-created";
      createdCell.textContent = formatCreatedAt(user.created_at);
      row.appendChild(createdCell);
      var actions = document.createElement("td");
      actions.className = "auth-user-actions";
      var actionGroup = document.createElement("div");
      actionGroup.className = "auth-user-action-group";
      actionGroup.appendChild(actionButton("编辑", "edit", user.id, false));
      var moreButton = document.createElement("button");
      moreButton.type = "button";
      moreButton.className = "auth-user-more-button";
      moreButton.dataset.userMore = String(user.id);
      moreButton.setAttribute("aria-haspopup", "menu");
      moreButton.setAttribute("aria-expanded", "false");
      moreButton.setAttribute("aria-controls", "authUserMoreMenu");
      moreButton.innerHTML = "更多<span aria-hidden=\"true\"></span>";
      actionGroup.appendChild(moreButton);
      actions.appendChild(actionGroup);
      row.appendChild(actions);
      body.appendChild(row);
    });
  }

  function loadUsers() {
    emitError(document.getElementById("authUsersError"), "");
    return apiRequest("/api/admin/users").then(function (payload) {
      state.users = payload.users;
      renderUsers();
    }).catch(function (error) {
      if (error.code === "request_aborted" || error.status === 401) return;
      emitError(document.getElementById("authUsersError"), errorMessage(error.errorCode));
    });
  }

  function findUser(userId) {
    return state.users.filter(function (user) { return user.id === userId; })[0] || null;
  }

  function showDialog(dialog) {
    dialog.classList.remove("is-closing");
    dialog.querySelectorAll("[data-password-toggle]").forEach(function (button) {
      var input = document.getElementById(button.dataset.passwordToggle);
      if (input) input.type = "password";
      button.textContent = "显示";
      button.setAttribute("aria-label", "显示密码");
    });
    dialog.showModal();
    window.requestAnimationFrame(function () {
      var firstField = dialog.querySelector(
        'input:not([type="hidden"]):not(:disabled), select:not(:disabled)'
      );
      if (firstField) firstField.focus();
    });
  }

  function closeDialog(dialog) {
    if (!dialog || !dialog.open || dialog.classList.contains("is-closing")) return;
    var returnFocusButton = dialog.returnFocusButton;
    dialog.classList.add("is-closing");
    window.setTimeout(function () {
      if (dialog.open) dialog.close();
      dialog.classList.remove("is-closing");
      delete dialog.returnFocusButton;
      if (returnFocusButton && document.body.contains(returnFocusButton)) {
        returnFocusButton.focus();
      }
    }, 150);
  }

  function beginDialogSave(form, pendingLabel) {
    var dialog = form.closest("dialog");
    if (!dialog || dialog.dataset.saving === "true") return null;
    var submit = form.querySelector('[type="submit"]');
    var saveState = {
      dialog: dialog,
      submit: submit,
      originalLabel: submit.textContent
    };
    dialog.dataset.saving = "true";
    dialog.setAttribute("aria-busy", "true");
    submit.disabled = true;
    submit.textContent = pendingLabel;
    dialog.querySelectorAll("[data-dialog-close]").forEach(function (button) {
      button.disabled = true;
    });
    return saveState;
  }

  function endDialogSave(saveState) {
    var dialog = saveState.dialog;
    saveState.submit.disabled = false;
    saveState.submit.textContent = saveState.originalLabel;
    delete dialog.dataset.saving;
    dialog.removeAttribute("aria-busy");
    dialog.querySelectorAll("[data-dialog-close]").forEach(function (button) {
      button.disabled = false;
    });
  }

  function openProfileDialog(user) {
    var dialog = document.getElementById("authProfileDialog");
    var form = document.getElementById("authProfileForm");
    form.reset();
    form.dataset.profileMode = "self";
    form.elements.userId.value = "";
    form.elements.fullName.value = user.full_name || "";
    form.elements.organizationName.value = user.organization_name || "";
    document.getElementById("authProfileTitle").textContent = "个人资料";
    emitError(form.querySelector(".auth-error"), "");
    showDialog(dialog);
  }

  function setEditControl(form, name, disabled) {
    form.elements[name].disabled = Boolean(disabled);
    form.elements[name].closest("label").classList.toggle("is-disabled", Boolean(disabled));
  }

  function openUserEditDialog(user) {
    var dialog = document.getElementById("authUserEditDialog");
    var form = document.getElementById("authUserEditForm");
    var protectedAccount = Boolean(user.is_protected_admin);
    var ownAccount = Boolean(state.user && user.id === state.user.id);
    form.reset();
    form.elements.userId.value = String(user.id);
    form.elements.username.value = user.username;
    form.elements.fullName.value = user.full_name || "";
    form.elements.organizationName.value = user.organization_name || "";
    form.elements.role.value = user.role;
    form.elements.status.value = user.status;
    setEditControl(form, "username", protectedAccount);
    setEditControl(form, "role", protectedAccount || ownAccount);
    setEditControl(form, "status", protectedAccount || ownAccount);
    document.getElementById("authUserEditTitle").textContent = "编辑用户 · " + user.username;
    document.getElementById("authUserEditSubtitle").textContent =
      "维护账户资料与访问权限。";
    document.getElementById("authUserEditNotice").textContent = protectedAccount
      ? "受保护管理员仅允许修改用户姓名和机构名称。"
      : ownAccount
        ? "当前账户不能修改自己的角色或状态。"
        : "修改用户名、角色或状态后，该用户需要重新登录。";
    emitError(form.querySelector(".auth-error"), "");
    showDialog(dialog);
  }

  function openUserPasswordDialog(user, returnFocusButton) {
    if (state.user && user.id === state.user.id) {
      var ownDialog = document.getElementById("authChangePasswordDialog");
      ownDialog.querySelector("form").reset();
      emitError(ownDialog.querySelector(".auth-error"), "");
      ownDialog.returnFocusButton = returnFocusButton;
      showDialog(ownDialog);
      return;
    }
    var dialog = document.getElementById("authResetPasswordDialog");
    var form = document.getElementById("authResetPasswordForm");
    form.reset();
    form.elements.userId.value = String(user.id);
    document.getElementById("authResetPasswordTitle").textContent =
      "重置密码 · " + user.username;
    emitError(form.querySelector(".auth-error"), "");
    dialog.returnFocusButton = returnFocusButton;
    showDialog(dialog);
  }

  document.getElementById("authLoginForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    var submit = document.getElementById("authLoginSubmit");
    emitError(document.getElementById("authLoginError"), "");
    submit.disabled = true;
    submit.textContent = "登录中…";
    apiRequest("/api/auth/login", {
      method: "POST",
      body: {
        username: form.elements.username.value,
        password: form.elements.password.value
      }
    }).then(function (payload) {
      form.reset();
      showAuthenticated(payload.user);
    }).catch(function (error) {
      emitError(document.getElementById("authLoginError"), errorMessage(error.errorCode));
    }).finally(function () {
      submit.disabled = false;
      submit.textContent = "登录";
    });
  });

  document.getElementById("authChangePasswordForm").addEventListener("submit", function (event) {
    event.preventDefault();
    submitPasswordForm(event.currentTarget).catch(function () {});
  });

  document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = document.getElementById(button.dataset.passwordToggle);
      var reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.textContent = reveal ? "隐藏" : "显示";
      button.setAttribute("aria-label", reveal ? "隐藏密码" : "显示密码");
    });
  });

  document.getElementById("authAccountButton").addEventListener("click", function () {
    if (state.accountMenuOpen) closeAccountMenu(false);
    else openAccountMenu();
  });

  document.getElementById("authProfileButton").addEventListener("click", function () {
    closeAccountMenu(false);
    if (state.user) openProfileDialog(state.user);
  });

  document.getElementById("authChangePasswordButton").addEventListener("click", function () {
    closeAccountMenu(false);
    var dialog = document.getElementById("authChangePasswordDialog");
    dialog.querySelector("form").reset();
    delete dialog.returnFocusButton;
    showDialog(dialog);
  });

  document.getElementById("authLogoutButton").addEventListener("click", function () {
    closeAccountMenu(false);
    showLogin();
    apiRequest("/api/auth/logout", { method: "POST", body: {} })
      .then(function () {})
      .catch(function (error) {
        if (error.status !== 401) emitError(document.getElementById("authLoginError"), errorMessage(error.errorCode));
      });
  });

  document.getElementById("factorLabNav").addEventListener("click", showFactorLab);
  document.getElementById("authUsersNav").addEventListener("click", showUsers);
  document.getElementById("authCreateUserButton").addEventListener("click", function () {
    var dialog = document.getElementById("authCreateUserDialog");
    dialog.querySelector("form").reset();
    emitError(dialog.querySelector(".auth-error"), "");
    showDialog(dialog);
  });

  document.getElementById("authCreateUserForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    apiRequest("/api/admin/users", {
      method: "POST",
      body: {
        username: form.elements.username.value,
        full_name: form.elements.fullName.value || null,
        organization_name: form.elements.organizationName.value || null,
        initial_password: form.elements.initialPassword.value,
        role: form.elements.role.value
      }
    }).then(function () {
      form.reset();
      form.closest("dialog").close();
      loadUsers();
    }).catch(function (error) {
      emitError(form.querySelector(".auth-error"), errorMessage(error.errorCode));
    });
  });

  document.getElementById("authUsersBody").addEventListener("click", function (event) {
    var moreButton = event.target.closest("[data-user-more]");
    if (moreButton) {
      var moreUserId = Number(moreButton.dataset.userMore);
      if (state.userMoreMenuId === moreUserId) closeUserMoreMenu(true);
      else openUserMoreMenu(moreButton, moreUserId);
      return;
    }
    var button = event.target.closest("[data-user-action]");
    if (!button || button.disabled) return;
    var userId = Number(button.dataset.userId);
    var user = findUser(userId);
    if (!user) return;
    if (button.dataset.userAction === "edit") openUserEditDialog(user);
  });

  document.getElementById("authUserEditForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    var userId = Number(form.elements.userId.value);
    var original = findUser(userId);
    var dialog = form.closest("dialog");
    emitError(form.querySelector(".auth-error"), "");
    var saveState = beginDialogSave(form, "保存中…");
    if (!saveState) return;
    apiRequest("/api/admin/users/edit", {
      method: "POST",
      body: {
        user_id: userId,
        username: form.elements.username.value,
        full_name: form.elements.fullName.value || null,
        organization_name: form.elements.organizationName.value || null,
        role: form.elements.role.value,
        status: form.elements.status.value
      }
    }).then(function (payload) {
      endDialogSave(saveState);
      saveState = null;
      var selfSessionRevoked = Boolean(
        original && state.user && original.id === state.user.id
        && original.username !== payload.user.username
      );
      form.reset();
      closeDialog(dialog);
      if (selfSessionRevoked) {
        window.setTimeout(function () {
          showLogin("账户信息已更新，请重新登录");
        }, 150);
        return null;
      }
      if (state.user && payload.user.id === state.user.id) state.user = payload.user;
      loadUsers();
      return null;
    }).catch(function (error) {
      emitError(form.querySelector(".auth-error"), errorMessage(error.errorCode));
    }).finally(function () {
      if (saveState) endDialogSave(saveState);
    });
  });

  document.getElementById("authUserPasswordAction").addEventListener("click", function () {
    var user = findUser(state.userMoreMenuId);
    var returnFocusButton = state.userMoreMenuButton;
    closeUserMoreMenu(false);
    if (user) openUserPasswordDialog(user, returnFocusButton);
  });

  document.getElementById("authResetPasswordForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    var newPassword = form.elements.newPassword.value;
    var errorHost = form.querySelector(".auth-error");
    if (newPassword !== form.elements.confirmPassword.value) {
      emitError(errorHost, "两次输入的新密码不一致");
      return;
    }
    emitError(errorHost, "");
    var saveState = beginDialogSave(form, "重置中…");
    if (!saveState) return;
    var userId = Number(form.elements.userId.value);
    apiRequest("/api/admin/users/reset-password", {
      method: "POST",
      body: {
        user_id: userId,
        new_password: newPassword
      }
    }).then(function () {
      endDialogSave(saveState);
      saveState = null;
      form.reset();
      closeDialog(form.closest("dialog"));
      loadUsers();
    }).catch(function (error) {
      emitError(errorHost, errorMessage(error.errorCode));
    }).finally(function () {
      if (saveState) endDialogSave(saveState);
    });
  });

  document.getElementById("authProfileForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    var body = {
      full_name: form.elements.fullName.value || null,
      organization_name: form.elements.organizationName.value || null
    };
    apiRequest("/api/auth/update-profile", { method: "POST", body: body }).then(function (payload) {
      state.user = payload.user;
      form.reset();
      closeDialog(form.closest("dialog"));
    }).catch(function (error) {
      emitError(form.querySelector(".auth-error"), errorMessage(error.errorCode));
    });
  });

  document.querySelectorAll("[data-dialog-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      var dialog = button.closest("dialog");
      if (dialog.dataset.saving !== "true") closeDialog(dialog);
    });
  });

  document.querySelectorAll(".auth-dialog").forEach(function (dialog) {
    dialog.addEventListener("cancel", function (event) {
      if (dialog.dataset.saving === "true") {
        event.preventDefault();
      } else if (dialog.returnFocusButton) {
        event.preventDefault();
        closeDialog(dialog);
      }
    });
  });

  document.addEventListener("click", function (event) {
    var menu = document.getElementById("authAccountMenu");
    var button = document.getElementById("authAccountButton");
    if (state.accountMenuOpen && !menu.contains(event.target) && !button.contains(event.target)) {
      closeAccountMenu(false);
    }
    var userMenu = document.getElementById("authUserMoreMenu");
    if (
      state.userMoreMenuId !== null
      && !userMenu.contains(event.target)
      && (!state.userMoreMenuButton || !state.userMoreMenuButton.contains(event.target))
    ) {
      closeUserMoreMenu(false);
    }
  });

  window.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && state.accountMenuOpen) {
      closeAccountMenu(true);
    }
    if (event.key === "Escape" && state.userMoreMenuId !== null) {
      closeUserMoreMenu(true);
    }
  });

  window.addEventListener("resize", function () { closeUserMoreMenu(false); });
  window.addEventListener("scroll", function () { closeUserMoreMenu(false); }, true);

  window.addEventListener("bfl:auth-required", function () {
    showLogin();
  });

  if (window.__BFL_ENABLE_AUTH_TEST_HOOKS__ === true) {
    window.__BFL_AUTH_TEST_HOOKS__ = Object.freeze({
      apiRequest: apiRequest,
      showLogin: showLogin,
      showAuthenticated: showAuthenticated,
      state: function () {
        return {
          user: state.user,
          users: state.users.slice(),
          identitySeq: state.identitySeq,
          activeRequests: state.activeRequests.size
        };
      }
    });
  }

  showGate(loadingView);
  apiRequest("/api/auth/me").then(function (payload) {
    showAuthenticated(payload.user);
  }).catch(function (error) {
    if (error.status !== 401) showLogin("暂时无法验证登录状态，请稍后重试");
  });
})();

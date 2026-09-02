(function () {
  "use strict";

  var state = {
    user: null,
    expiresAt: "",
    accountMenuOpen: false,
    users: []
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
    return messages[errorCode] || "操作失败，请稍后重试";
  }

  function emitError(host, message) {
    if (!host) return;
    host.textContent = message || "";
    host.hidden = !message;
  }

  function apiRequest(path, options) {
    var requestOptions = options || {};
    var fetchOptions = {
      method: requestOptions.method || "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    };
    if (Object.prototype.hasOwnProperty.call(requestOptions, "body")) {
      fetchOptions.headers["Content-Type"] = "application/json";
      fetchOptions.body = JSON.stringify(requestOptions.body);
    }
    return fetch(apiUrl(path), fetchOptions).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (response.ok) return body;
        var error = new Error(body.error_code || "request_failed");
        error.errorCode = body.error_code || "request_failed";
        error.status = response.status;
        if (response.status === 401 && path !== "/api/auth/login") {
          showLogin();
        }
        throw error;
      });
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
    });
    document.getElementById("authResetPasswordTarget").textContent = "";
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
  }

  function showLogin(message) {
    state.user = null;
    state.expiresAt = "";
    state.users = [];
    showGate(loginView);
    var form = document.getElementById("authLoginForm");
    if (form) form.reset();
    emitError(document.getElementById("authLoginError"), message || "");
    window.setTimeout(function () {
      var username = document.getElementById("authUsername");
      if (username) username.focus();
    }, 0);
  }

  function showAuthenticated(user, expiresAt) {
    state.user = user;
    state.expiresAt = expiresAt || "";
    authGate.hidden = true;
    shell.hidden = false;
    shell.setAttribute("aria-hidden", "false");
    document.getElementById("authAccountName").textContent = user.username;
    document.getElementById("authUsersNav").hidden = user.role !== "admin";
    showFactorLab();
  }

  function showFactorLab() {
    if (!state.user) return;
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
    return apiRequest("/api/auth/change-password", {
      method: "POST",
      body: {
        current_password: currentPassword,
        new_password: newPassword
      }
    }).then(function () {
      form.reset();
      var dialog = form.closest("dialog");
      if (dialog && dialog.open) dialog.close();
      showLogin("密码已修改，请使用新密码重新登录");
    }).catch(function (error) {
      if (error.message === "password_confirmation_mismatch") throw error;
      emitError(errorHost, errorMessage(error.errorCode));
      throw error;
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

  function renderUsers() {
    var body = document.getElementById("authUsersBody");
    body.textContent = "";
    document.getElementById("authUsersCount").textContent = state.users.length + " 个账户";
    state.users.forEach(function (user) {
      var row = document.createElement("tr");
      var identity = document.createElement("td");
      var identityInner = document.createElement("div");
      identityInner.className = "auth-user-identity";
      var avatar = document.createElement("span");
      avatar.className = "auth-user-avatar";
      avatar.textContent = user.username.slice(0, 1).toUpperCase();
      var username = document.createElement("strong");
      username.textContent = user.username;
      identityInner.appendChild(avatar);
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
      actions.appendChild(actionButton("编辑资料", "profile", user.id, false));
      actions.appendChild(actionButton("改用户名", "username", user.id, user.is_protected_admin));
      actions.appendChild(actionButton(user.role === "admin" ? "设为普通用户" : "设为管理员", "role", user.id, user.is_protected_admin));
      actions.appendChild(actionButton("重置密码", "password", user.id, user.is_protected_admin || user.id === state.user.id));
      actions.appendChild(actionButton(user.status === "active" ? "停用" : "恢复", "status", user.id, user.is_protected_admin || user.id === state.user.id));
      row.appendChild(actions);
      body.appendChild(row);
    });
  }

  function loadUsers() {
    emitError(document.getElementById("authUsersError"), "");
    return apiRequest("/api/admin/users").then(function (payload) {
      state.users = payload.users || [];
      renderUsers();
    }).catch(function (error) {
      emitError(document.getElementById("authUsersError"), errorMessage(error.errorCode));
    });
  }

  function findUser(userId) {
    return state.users.filter(function (user) { return user.id === userId; })[0] || null;
  }

  function updateUser(path, body) {
    return apiRequest(path, { method: "POST", body: body }).then(function () {
      return loadUsers();
    }).catch(function (error) {
      emitError(document.getElementById("authUsersError"), errorMessage(error.errorCode));
    });
  }

  function openProfileDialog(user, mode) {
    var dialog = document.getElementById("authProfileDialog");
    var form = document.getElementById("authProfileForm");
    form.reset();
    form.dataset.profileMode = mode;
    form.elements.userId.value = mode === "admin" ? String(user.id) : "";
    form.elements.fullName.value = user.full_name || "";
    form.elements.organizationName.value = user.organization_name || "";
    document.getElementById("authProfileTitle").textContent =
      mode === "admin" ? "编辑“" + user.username + "”的资料" : "个人资料";
    emitError(form.querySelector(".auth-error"), "");
    dialog.showModal();
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
      showAuthenticated(payload.user, payload.expires_at);
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
    if (state.user) openProfileDialog(state.user, "self");
  });

  document.getElementById("authChangePasswordButton").addEventListener("click", function () {
    closeAccountMenu(false);
    var dialog = document.getElementById("authChangePasswordDialog");
    dialog.querySelector("form").reset();
    dialog.showModal();
  });

  document.getElementById("authLogoutButton").addEventListener("click", function () {
    closeAccountMenu(false);
    apiRequest("/api/auth/logout", { method: "POST", body: {} })
      .then(function () { showLogin(); })
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
    dialog.showModal();
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
    var button = event.target.closest("[data-user-action]");
    if (!button || button.disabled) return;
    var userId = Number(button.dataset.userId);
    var user = findUser(userId);
    if (!user) return;
    var action = button.dataset.userAction;
    if (action === "profile") {
      openProfileDialog(user, "admin");
    } else if (action === "username") {
      var username = window.prompt("输入新的用户名", user.username);
      if (username && username !== user.username) {
        updateUser("/api/admin/users/change-username", { user_id: userId, username: username });
      }
    } else if (action === "role") {
      var role = user.role === "admin" ? "user" : "admin";
      if (window.confirm("确认将“" + user.username + "”调整为" + (role === "admin" ? "管理员" : "普通用户") + "？该用户的全部会话将被撤销。")) {
        updateUser("/api/admin/users/change-role", { user_id: userId, role: role });
      }
    } else if (action === "status") {
      var status = user.status === "active" ? "disabled" : "active";
      if (window.confirm("确认" + (status === "disabled" ? "停用" : "恢复") + "用户“" + user.username + "”？")) {
        updateUser("/api/admin/users/change-status", { user_id: userId, status: status });
      }
    } else if (action === "password") {
      var dialog = document.getElementById("authResetPasswordDialog");
      dialog.querySelector("form").reset();
      dialog.querySelector('[name="userId"]').value = String(userId);
      document.getElementById("authResetPasswordTarget").textContent = "为用户“" + user.username + "”设置新密码；其全部会话将被撤销。";
      emitError(dialog.querySelector(".auth-error"), "");
      dialog.showModal();
    }
  });

  document.getElementById("authResetPasswordForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    apiRequest("/api/admin/users/reset-password", {
      method: "POST",
      body: {
        user_id: Number(form.elements.userId.value),
        new_password: form.elements.newPassword.value
      }
    }).then(function () {
      form.reset();
      form.closest("dialog").close();
      loadUsers();
    }).catch(function (error) {
      emitError(form.querySelector(".auth-error"), errorMessage(error.errorCode));
    });
  });

  document.getElementById("authProfileForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.currentTarget;
    var adminMode = form.dataset.profileMode === "admin";
    var body = {
      full_name: form.elements.fullName.value || null,
      organization_name: form.elements.organizationName.value || null
    };
    if (adminMode) body.user_id = Number(form.elements.userId.value);
    apiRequest(
      adminMode ? "/api/admin/users/update-profile" : "/api/auth/update-profile",
      { method: "POST", body: body }
    ).then(function (payload) {
      if (adminMode) {
        if (state.user && payload.user.id === state.user.id) {
          state.user = payload.user;
        }
        loadUsers();
      } else {
        state.user = payload.user;
      }
      form.reset();
      form.closest("dialog").close();
    }).catch(function (error) {
      emitError(form.querySelector(".auth-error"), errorMessage(error.errorCode));
    });
  });

  document.querySelectorAll("[data-dialog-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      button.closest("dialog").close();
    });
  });

  document.addEventListener("click", function (event) {
    var menu = document.getElementById("authAccountMenu");
    var button = document.getElementById("authAccountButton");
    if (state.accountMenuOpen && !menu.contains(event.target) && !button.contains(event.target)) {
      closeAccountMenu(false);
    }
  });

  window.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && state.accountMenuOpen) {
      closeAccountMenu(true);
    }
  });

  window.addEventListener("bfl:auth-required", function () {
    showLogin();
  });

  showGate(loadingView);
  apiRequest("/api/auth/me").then(function (payload) {
    showAuthenticated(payload.user, payload.expires_at);
  }).catch(function (error) {
    if (error.status !== 401) showLogin("暂时无法验证登录状态，请稍后重试");
  });
})();

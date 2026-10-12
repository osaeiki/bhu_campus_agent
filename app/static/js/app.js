/* 北华校园助手前端逻辑
   登录态与会话使用 sessionStorage（每个标签页独立）：
   这样可以在不同标签页分别登录 学生/教师/管理员，互不影响。 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const TOKEN_KEY = "bhu_token";
  const USER_KEY = "bhu_user";
  const SESSION_KEY = "bhu_session";

  let token = sessionStorage.getItem(TOKEN_KEY) || "";
  let user = null;
  let sessionId = sessionStorage.getItem(SESSION_KEY) || newId();
  let busy = false;
  let notifTimer = null;
  let lastNotifId = 0;
  let notifLoadedOnce = false;

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "s-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* 简易 Markdown 渲染：加粗 / 行内代码 / 列表 / 换行 */
  function renderMd(text) {
    let html = esc(text);
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/^[-*] (.+)$/gm, "• $1");
    html = html.replace(/^\d+\. (.+)$/gm, "$&");
    html = html.replace(/\n/g, "<br>");
    return html;
  }

  async function api(path, options) {
    const opts = options || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (token) opts.headers["Authorization"] = "Bearer " + token;
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) {
      const detail = data && data.detail ? data.detail : ("请求失败 " + resp.status);
      throw new Error(detail);
    }
    return data;
  }

  /* ---------- 登录 ---------- */
  async function doLogin(username, password) {
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    token = data.token;
    user = data.user;
    sessionStorage.setItem(TOKEN_KEY, token);
    sessionStorage.setItem(USER_KEY, JSON.stringify(user));
    resetChat(); // 每个账号登录后都从全新的会话开始，避免看到上一个账号的聊天
    enterChat();
  }

  function logout() {
    token = "";
    user = null;
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(USER_KEY);
    sessionStorage.removeItem(SESSION_KEY);
    sessionId = newId();
    $("#messages").innerHTML = "";
    $("#chat-view").classList.add("hidden");
    $("#login-view").classList.remove("hidden");
    stopNotifyPolling();
  }

  /* 重置会话：新会话ID + 清空聊天区 */
  function resetChat() {
    sessionId = newId();
    sessionStorage.setItem(SESSION_KEY, sessionId);
    $("#messages").innerHTML = "";
  }

  /* ---------- 视图切换 ---------- */
  function enterChat() {
    $("#login-view").classList.add("hidden");
    $("#chat-view").classList.remove("hidden");
    $("#user-name").textContent = user.name;
    $("#user-role").textContent = roleCn(user.role);
    $("#user-ext").textContent = [user.class_name, user.department].filter(Boolean).join(" · ");
    $("#user-avatar").textContent = user.name ? user.name[0] : "访";
    $("#btn-admin").classList.toggle("hidden", user.role !== "admin");
    refreshStatus();
    loadHistory();
    startNotifyPolling();
  }

  /* ---------- 主动推送：审批结果 / 人工回复，5 秒轮询 ---------- */
  const NOTIF_TYPE_CN = {
    leave_result: "请假审批",
    ticket_reply: "人工回复",
    repair_update: "报修进度",
    booking_result: "场地预约",
    todo_leave: "待办",
    todo_repair: "待办",
    todo_booking: "待办",
    todo_ticket: "待办",
    system: "系统",
  };

  function startNotifyPolling() {
    stopNotifyPolling();
    fetchNotifications(true);
    notifTimer = setInterval(() => fetchNotifications(false), 5000);
  }
  function stopNotifyPolling() {
    if (notifTimer) { clearInterval(notifTimer); notifTimer = null; }
    hideTicketBanner();
    hideNotifPanel();
    notifLoadedOnce = false;
    lastNotifId = 0;
  }

  function maxNotifId(items) {
    return items.reduce((m, x) => Math.max(m, x.id || 0), 0);
  }

  async function fetchNotifications(isFirst) {
    if (!token || !user) return;
    try {
      const d = await api("/api/notifications?unread=1&limit=20");
      updateNotifBadge(d.count || 0);
      renderNotifBanner(d.items || []);
      if (isFirst || !notifLoadedOnce) {
        notifLoadedOnce = true;
        lastNotifId = maxNotifId(d.items || []);
      } else {
        const fresh = (d.items || []).filter((x) => x.id > lastNotifId);
        lastNotifId = Math.max(lastNotifId, maxNotifId(d.items || []));
        fresh.reverse().forEach(pushToast);
      }
    } catch (e) { /* 轮询失败静默，下个周期重试 */ }
  }

  function updateNotifBadge(count) {
    const badge = $("#notif-badge");
    if (!badge) return;
    badge.textContent = count > 99 ? "99+" : String(count);
    badge.classList.toggle("hidden", count <= 0);
  }

  /* 顶部横幅：显示最新一条未读通知（原来只用于人工工单，现在覆盖所有推送类型） */
  function renderNotifBanner(items) {
    const b = $("#ticket-banner");
    if (!b) return;
    if (!items.length) { hideTicketBanner(); return; }
    const it = items[0];
    const label = NOTIF_TYPE_CN[it.type] || "通知";
    b.innerHTML =
      // 文本整体包一层，避免 flex 把每个子节点拆成独立列导致文字被挤成竖排
      "<span class='notif-text'>" +
      "<span class='notif-type'>" + esc(label) + "</span>" +
      "<b>" + esc(it.title) + "</b><span class='notif-body'>" + esc(it.content || "") + "</span>" +
      "</span>" +
      "<button id='btn-ticket-dismiss' class='act-btn'>知道了</button>";
    b.classList.remove("hidden");
    const btn = $("#btn-ticket-dismiss");
    if (btn) {
      btn.onclick = async () => {
        for (const x of items) {
          try { await api("/api/notifications/" + x.id + "/read", { method: "POST" }); } catch (e) {}
        }
        hideTicketBanner();
        fetchNotifications(false);
        if (!$("#notif-panel").classList.contains("hidden")) loadNotifList();
      };
    }
  }
  function hideTicketBanner() {
    const b = $("#ticket-banner");
    if (b) b.classList.add("hidden");
  }

  /* 右上角气泡 + 在聊天区插入一条系统消息，保证用户切走页面回来也能看到 */
  function pushToast(n) {
    const wrap = $("#toast-wrap");
    if (!wrap) return;
    const el = document.createElement("div");
    el.className = "toast" + (n.type && n.type.indexOf("todo_") === 0 ? " warn" : "");
    el.innerHTML =
      "<div class='tt'>📢 " + esc(n.title) + "</div><div class='tc'>" + esc(n.content || "") + "</div>";
    wrap.appendChild(el);
    setTimeout(() => el.remove(), 12000);

    const card = document.createElement("div");
    card.className = "msg notif";
    card.innerHTML = "📢 <b>[" + esc(NOTIF_TYPE_CN[n.type] || "通知") + "] " + esc(n.title) + "</b><br>" + esc(n.content || "");
    $("#messages").appendChild(card);
    scrollBottom();
  }

  /* 消息中心面板 */
  function hideNotifPanel() {
    const p = $("#notif-panel");
    if (p) p.classList.add("hidden");
  }
  async function toggleNotifPanel() {
    const p = $("#notif-panel");
    if (!p) return;
    if (p.classList.contains("hidden")) { p.classList.remove("hidden"); await loadNotifList(); }
    else hideNotifPanel();
  }
  async function loadNotifList() {
    const box = $("#notif-list");
    if (!box) return;
    try {
      const d = await api("/api/notifications?limit=30");
      const items = d.items || [];
      if (!items.length) {
        box.innerHTML = "<div class='notif-empty'>还没有通知。教师批假、管理员回复工单时会主动推送给你。</div>";
        return;
      }
      box.innerHTML = items
        .map(
          (n) =>
            "<div class='notif-item " + (n.read_at ? "" : "unread") + "' data-id='" + n.id + "'>" +
            "<div class='t'><span class='notif-type'>" + esc(NOTIF_TYPE_CN[n.type] || "通知") + "</span>" + esc(n.title) + "</div>" +
            "<div class='c'>" + esc(n.content || "") + "</div>" +
            "<div class='d'>" + esc((n.created_at || "").slice(5, 16)) + (n.read_at ? " · 已读" : " · 未读，点击标记已读") + "</div>" +
            "</div>"
        )
        .join("");
      box.querySelectorAll(".notif-item").forEach((el) => {
        el.onclick = async () => {
          const id = el.getAttribute("data-id");
          try { await api("/api/notifications/" + id + "/read", { method: "POST" }); } catch (e) {}
          await loadNotifList();
          fetchNotifications(false);
        };
      });
    } catch (e) {
      box.innerHTML = "<div class='notif-empty'>加载失败：" + esc(e.message) + "</div>";
    }
  }

  /* 从服务端恢复当前会话的历史消息（仅当前用户自己的会话） */
  async function loadHistory() {
    try {
      const data = await api("/api/history/" + encodeURIComponent(sessionId));
      const items = (data && data.items) || [];
      if (items.length) {
        items.forEach((m) => {
          if (m.role === "user") {
            addMessage("user", esc(m.content || ""));
          } else if (m.role === "assistant") {
            let tools = [];
            try { tools = m.tool_calls ? JSON.parse(m.tool_calls) : []; } catch (e) { tools = []; }
            addMessage("assistant", renderMd(m.content || ""), tools);
          }
        });
        scrollBottom();
        return;
      }
    } catch (e) {
      // 会话不存在或不属于当前用户 → 新建
    }
    if (!$("#messages").children.length) {
      addWelcome();
    }
  }

  function roleCn(r) {
    return { admin: "管理员", teacher: "教师", student: "学生", visitor: "访客" }[r] || r;
  }

  /* ---------- 消息渲染 ---------- */
  function addWelcome() {
    const tips = [
      "北校区第三教学楼有什么特点？",
      "帮我查一下课表",
      "帮我请 9 月 5 号一天的假，事由：回家办事",
      "三教报告厅明晚 7-9 点能预约吗？",
      "北校区有哪些食堂？",
      "帮我查一下我的成绩",
    ];
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    wrap.innerHTML =
      "你好，我是<b>北华校园助手</b>，北华大学北校区的本地校园 Agent。我可以：<br>" +
      "• 解答校园问题（教学楼、食堂、宿舍、交通、办事流程）<br>" +
      "• 查询课表 / 成绩<br>" +
      "• 办理请假、报修、场地预约并查询进度<br>" +
      "• 需要联网时通过外部 MCP 浏览器自动化检索公开信息<br><br>" +
      "试试下面的问题：";
    $("#messages").appendChild(wrap);
    const chips = document.createElement("div");
    chips.style.marginTop = "8px";
    tips.forEach((t) => {
      const c = document.createElement("span");
      c.className = "source-chip";
      c.textContent = t;
      c.style.cursor = "pointer";
      c.onclick = () => { $("#chat-input").value = t; $("#btn-send").click(); };
      chips.appendChild(c);
    });
    wrap.appendChild(chips);
  }

  function addMessage(role, html, toolNames) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.innerHTML = html;
    if (toolNames && toolNames.length) {
      const t = document.createElement("div");
      t.className = "msg-tools";
      t.textContent = "⚙ 调用工具：" + toolNames.join("、");
      div.appendChild(t);
    }
    if (role === "assistant") {
      addActions(div);
    }
    $("#messages").appendChild(div);
    scrollBottom();
    return div;
  }

  function addActions(msgDiv) {
    const box = document.createElement("div");
    box.className = "msg-actions";
    const up = document.createElement("button");
    up.className = "act-btn";
    up.textContent = "👍 有帮助";
    const down = document.createElement("button");
    down.className = "act-btn";
    down.textContent = "👎 没帮助";
    up.onclick = () => sendFeedback(5, "", box);
    down.onclick = () => showFeedbackPanel(box);
    box.appendChild(up);
    box.appendChild(down);
    msgDiv.appendChild(box);
  }

  function showFeedbackPanel(box) {
    if (box.querySelector(".feedback-panel")) return;
    const panel = document.createElement("div");
    panel.className = "feedback-panel";
    const input = document.createElement("input");
    input.placeholder = "说说哪里不对（可选）";
    const okBtn = document.createElement("button");
    okBtn.className = "btn primary mini";
    okBtn.textContent = "提交";
    okBtn.onclick = () => sendFeedback(1, input.value, box);
    panel.appendChild(input);
    panel.appendChild(okBtn);
    box.appendChild(panel);
  }

  async function sendFeedback(rating, comment, box) {
    try {
      await api("/api/feedback", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, rating, comment: comment || "" }),
      });
      box.innerHTML = '<div class="feedback-ok">✓ 感谢反馈</div>';
    } catch (e) {
      box.innerHTML = '<div class="feedback-ok" style="color:var(--red)">提交失败：' + esc(e.message) + "</div>";
    }
  }

  function scrollBottom() {
    const m = $("#messages");
    m.scrollTop = m.scrollHeight;
  }

  function setStatus(text) {
    const el = $("#tool-status");
    if (text) {
      el.textContent = text;
      el.classList.remove("hidden");
    } else {
      el.classList.add("hidden");
    }
  }

  async function refreshStatus() {
    try {
      const data = await api("/api/system/status");
      const items = data.mcp || [];
      if (!items.length) {
        $("#mcp-status").textContent = "外部 MCP：未配置";
      } else {
        const parts = items.map((s) => s.name + ":" + (s.status === "online" ? "在线" : "离线"));
        $("#mcp-status").textContent = "外部 MCP：" + parts.join("，");
      }
    } catch (e) {
      $("#mcp-status").textContent = "外部 MCP：获取失败";
    }
  }

  /* ---------- 发送消息（SSE） ---------- */
  async function send() {
    if (busy) return;
    const input = $("#chat-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    busy = true;
    input.disabled = true;
    $("#btn-send").disabled = true;

    addMessage("user", esc(text));
    setStatus("思考中…");
    const pending = document.createElement("div");
    pending.className = "msg assistant typing";
    pending.textContent = "正在思考…";
    $("#messages").appendChild(pending);
    scrollBottom();

    const usedTools = [];
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      if (!resp.ok || !resp.body) {
        let detail = "请求失败 " + resp.status;
        try { const j = await resp.json(); detail = j.detail || detail; } catch (e) {}
        throw new Error(detail);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buf = "";
      let answer = "";
      let done = false;
      while (true) {
        const { value, done: streamDone } = await reader.read();
        if (streamDone) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (!raw.startsWith("data:")) continue;
          const ev = JSON.parse(raw.slice(5).trim());
          if (ev.type === "tool_start") {
            setStatus("正在调用工具：" + ev.name + " …");
          } else if (ev.type === "tool_done") {
            usedTools.push(ev.name);
            setStatus("工具 " + ev.name + " 完成：" + (ev.summary || ""));
          } else if (ev.type === "intent") {
            setStatus("识别意图：" + (ev.label || ev.intent) + (ev.confidence ? "（" + Math.round(ev.confidence * 100) + "%）" : "") + "…");
          } else if (ev.type === "done") {
            answer = ev.answer || "";
            sessionId = ev.session_id || sessionId;
            sessionStorage.setItem(SESSION_KEY, sessionId);
            done = true;
          } else if (ev.type === "error") {
            throw new Error(ev.message || "未知错误");
          }
        }
      }
      if (!done && !answer) {
        throw new Error("连接中断，请重试");
      }
      pending.remove();
      addMessage("assistant", renderMd(answer), usedTools);
    } catch (e) {
      pending.remove();
      addMessage("assistant", "<b>出错了：</b>" + esc(e.message));
    } finally {
      busy = false;
      input.disabled = false;
      $("#btn-send").disabled = false;
      setStatus("");
      input.focus();
    }
  }

  /* ---------- 事件绑定 ---------- */
  function bind() {
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      $("#login-error").classList.add("hidden");
      try {
        await doLogin($("#login-username").value.trim(), $("#login-password").value);
      } catch (err) {
        $("#login-error").textContent = err.message;
        $("#login-error").classList.remove("hidden");
      }
    });
    $("#btn-send").addEventListener("click", send);
    $("#chat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
    $("#chat-input").addEventListener("input", () => {
      $("#chat-input").style.height = "auto";
      $("#chat-input").style.height = Math.min($("#chat-input").scrollHeight, 140) + "px";
    });
    $("#btn-new-chat").addEventListener("click", () => {
      resetChat();
      addWelcome();
    });
    $("#btn-notif").addEventListener("click", toggleNotifPanel);
    $("#btn-notif-refresh").addEventListener("click", () => { loadNotifList(); fetchNotifications(false); });
    $("#btn-notif-readall").addEventListener("click", async () => {
      try { await api("/api/notifications/read_all", { method: "POST" }); } catch (e) {}
      hideTicketBanner();
      updateNotifBadge(0);
      loadNotifList();
    });
    document.addEventListener("click", (e) => {
      const panel = $("#notif-panel");
      if (!panel || panel.classList.contains("hidden")) return;
      if (panel.contains(e.target) || e.target.closest("#btn-notif")) return;
      hideNotifPanel();
    });
    $("#btn-logout").addEventListener("click", logout);
    $("#btn-admin").addEventListener("click", () => { window.location.href = "/admin"; });
  }

  async function init() {
    bind();
    if (!token) return;
    try {
      user = await api("/api/me");
      sessionStorage.setItem(USER_KEY, JSON.stringify(user));
      enterChat();
    } catch (e) {
      logout();
    }
  }

  init();
})();

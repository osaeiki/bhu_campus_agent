/* 北华校园助手 · 管理后台逻辑（登录态使用 sessionStorage，与主页面同标签页共享） */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const TOKEN_KEY = "bhu_token";
  let token = sessionStorage.getItem(TOKEN_KEY) || "";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function trunc(s, n) {
    s = String(s == null ? "" : s);
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  async function api(path, options) {
    const opts = options || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (token) opts.headers["Authorization"] = "Bearer " + token;
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) throw new Error((data && data.detail) ? data.detail : "请求失败 " + resp.status);
    return data;
  }

  function badge(status) {
    const map = {
      pending: ["orange", "待审批"],
      approved: ["green", "已批准"],
      rejected: ["red", "已驳回"],
      processing: ["blue", "处理中"],
      done: ["green", "已完成"],
      closed: ["gray", "已关闭"],
      open: ["orange", "待处理"],
      online: ["green", "在线"],
      offline: ["red", "离线"],
      unknown: ["gray", "未知"],
      enabled: ["green", "启用"],
      disabled: ["gray", "停用"],
    };
    const [cls, label] = map[status] || ["gray", status];
    return '<span class="badge ' + cls + '">' + esc(label) + "</span>";
  }

  function tab(name) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    if (name === "overview") loadStats();
    if (name === "knowledge") loadKnowledge();
    if (name === "users") loadUsers();
    if (name === "mcp") loadMcp();
    if (name === "applications") loadApplications();
    if (name === "tickets") loadTickets();
    if (name === "logs") loadLogs();
    if (name === "feedback") loadFeedback();
  }

  function emptyBox(msg) {
    return '<div class="card hint">' + esc(msg) + "</div>";
  }

  /* ---------- 概览 ---------- */
  async function loadStats() {
    try {
      const s = await api("/api/admin/stats");
      const cards = [
        ["总提问数", s.total_queries],
        ["今日提问", s.today_queries],
        ["用户数", s.user_count],
        ["知识文档", s.doc_count],
        ["待办审批", s.pending_applications],
        ["待人工工单", s.open_tickets],
      ];
      $("#stat-cards").innerHTML = cards
        .map(([l, n]) => '<div class="stat-card"><div class="num">' + n + '</div><div class="label">' + l + "</div></div>")
        .join("");
      $("#daily-table").innerHTML =
        "<tr><th>日期</th><th>提问数</th></tr>" +
        (s.daily.length ? s.daily.map((d) => "<tr><td>" + esc(d.date) + "</td><td>" + d.count + "</td></tr>").join("") : "<tr><td colspan='2'>暂无数据</td></tr>");
      $("#tools-table").innerHTML =
        "<tr><th>工具</th><th>次数</th></tr>" +
        (s.top_tools.length ? s.top_tools.map((t) => "<tr><td>" + esc(t.name) + "</td><td>" + t.count + "</td></tr>").join("") : "<tr><td colspan='2'>暂无数据</td></tr>");
      const intentCn = { small_talk: "闲聊", kb_qa: "校园问答", query_course: "查课表", query_score: "查成绩", leave: "请假", repair: "报修", venue_booking: "场地预约", campus_route: "校内路线", maps_navigation: "真实导航", weather: "天气", poi_search: "地点搜索", web_search: "联网搜索", teacher_manage: "教师管理", human_service: "转人工", broad: "综合" };
      $("#intent-table-wrap").innerHTML =
        "<table class='table'><tr><th>意图</th><th>次数</th></tr>" +
        (s.top_intents.length ? s.top_intents.map((t) => "<tr><td>" + esc(intentCn[t.name] || t.name) + "</td><td>" + t.count + "</td></tr>").join("") : "<tr><td colspan='2'>暂无数据</td></tr>") +
        "</table>";
    } catch (e) {
      $("#stat-cards").innerHTML = emptyBox("加载失败：" + e.message);
    }
  }

  /* ---------- 知识库 ---------- */
  let kbEditingId = null;

  async function loadKnowledge() {
    const kw = $("#kb-search").value.trim();
    const dt = $("#kb-type").value;
    try {
      const data = await api(
        "/api/admin/knowledge?keyword=" + encodeURIComponent(kw) +
        "&doc_type=" + encodeURIComponent(dt) + "&limit=100"
      );
      const typeCn = { text: "文档", pdf: "PDF", table: "表格" };
      const rows = data.items
        .map(
          (d) =>
            "<tr><td>#" + d.id + "</td><td><b>" + esc(d.title) + "</b><br><span class='meta'>分类：" + esc(d.category) +
            " · 类型：" + esc(typeCn[d.doc_type] || d.doc_type || "文档") +
            (d.source_file ? " · 文件：" + esc(trunc(d.source_file, 30)) : " · 来源：" + esc(trunc(d.source, 30))) +
            "</span><br><span class='meta kb-chunk'>" + esc(trunc(d.content, 140)) + "</span></td><td>" +
            badge(d.enabled ? "enabled" : "disabled") + "</td><td><div class='row-actions'>" +
            "<button class='mini-btn' onclick='window._kbEdit(" + d.id + ")'>编辑</button>" +
            "<button class='mini-btn' onclick='window._kbToggle(" + d.id + "," + d.enabled + ")'>" + (d.enabled ? "停用" : "启用") + "</button>" +
            "<button class='mini-btn red' onclick='window._kbDel(" + d.id + ")'>删除</button></div></td></tr>"
        )
        .join("");
      $("#kb-table-wrap").innerHTML =
        '<div class="card"><table class="table"><tr><th>ID</th><th>文档</th><th>状态</th><th>操作</th></tr>' + rows + "</table></div>" +
        '<div class="hint">共 ' + data.total + " 条</div>";
      const byType = data.by_type || {};
      const idx = data.index || {};
      $("#kb-index-info").textContent =
        "索引状态：文档 " + (idx.docs || 0) + " 篇 · 知识块 " + (idx.chunks || 0) +
        " · 向量后端 " + (idx.backend || "-") + "（维度 " + (idx.dim || 0) + "）" +
        " · 类型分布 文档 " + (byType.text || 0) + " / PDF " + (byType.pdf || 0) + " / 表格 " + (byType.table || 0);
    } catch (e) {
      $("#kb-table-wrap").innerHTML = emptyBox("加载失败：" + e.message);
    }
  }

  window._kbEdit = function (id) {
    // 列表是分页的，编辑时按 id 单独取全文，避免文档多时找不到
    api("/api/admin/knowledge/" + id).then((d) => {
      kbEditingId = id;
      $("#kb-form-title").textContent = "编辑文档 #" + id;
      $("#kb-title").value = d.title;
      $("#kb-category").value = d.category;
      $("#kb-source").value = d.source || "";
      $("#kb-content").value = d.content;
      $("#kb-form").classList.remove("hidden");
    }).catch((e) => alert(e.message));
  };
  window._kbToggle = async function (id, enabled) {
    try { await api("/api/admin/knowledge/" + id, { method: "PUT", body: JSON.stringify({ enabled: enabled ? 0 : 1 }) }); loadKnowledge(); } catch (e) { alert(e.message); }
  };
  window._kbDel = async function (id) {
    if (!confirm("确定删除文档 #" + id + "？")) return;
    try { await api("/api/admin/knowledge/" + id, { method: "DELETE" }); loadKnowledge(); } catch (e) { alert(e.message); }
  };

  $("#btn-kb-search").addEventListener("click", loadKnowledge);
  $("#kb-type").addEventListener("change", loadKnowledge);
  $("#kb-search").addEventListener("keydown", (e) => { if (e.key === "Enter") loadKnowledge(); });
  $("#btn-kb-rebuild").addEventListener("click", async () => {
    try {
      const r = await api("/api/admin/knowledge/rebuild", { method: "POST" });
      alert("重建完成，分块数：" + r.chunks + "，向量后端：" + (r.index ? r.index.backend : "-"));
      loadKnowledge();
    } catch (e) { alert(e.message); }
  });
  $("#btn-kb-rescan").addEventListener("click", async () => {
    try {
      const r = await api("/api/admin/knowledge/rescan", { method: "POST" });
      const s = r.stats || {};
      alert("扫描完成：文件 " + s.files + " 个，新增 " + s.added + "，更新 " + s.updated +
        "，清理 " + s.removed + "，入库知识块 " + s.chunks + "。\n当前分块总数：" + ((r.index || {}).chunks || 0));
      loadKnowledge();
    } catch (e) { alert(e.message); }
  });
  $("#btn-kb-upload").addEventListener("click", () => $("#kb-file").click());
  $("#kb-file").addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const resp = await fetch("/api/admin/knowledge/upload", {
        method: "POST",
        headers: { Authorization: "Bearer " + token },
        body: fd,
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error((data && data.detail) || ("上传失败 " + resp.status));
      alert("上传成功：" + data.file + "，解析出 " + data.chunks + " 个知识块，已入库并重建索引。");
      loadKnowledge();
    } catch (err) {
      alert(err.message);
    } finally {
      e.target.value = "";
    }
  });
  $("#btn-kb-test").addEventListener("click", async () => {
    const q = $("#kb-test-q").value.trim();
    if (!q) { alert("请输入检索内容"); return; }
    const mode = $("#kb-test-mode").value;
    try {
      const d = await api("/api/admin/knowledge/search?q=" + encodeURIComponent(q) + "&mode=" + mode + "&top_k=5");
      const typeCn = { text: "文档", pdf: "PDF", table: "表格" };
      const rows = (d.fused || []).map((r, i) =>
        "<tr><td>" + (i + 1) + "</td><td><b>" + esc(r.title) + "</b><br><span class='meta'>" +
        esc(typeCn[r.doc_type] || r.doc_type) + (r.source_file ? " · " + esc(r.source_file) : "") + "</span></td>" +
        "<td><span class='meta'>融合 " + r.score + "<br>BM25 " + (r.bm25 || 0) + (r.bm25_rank ? " (#" + r.bm25_rank + ")" : "") +
        "<br>向量 " + (r.vector || 0) + (r.vector_rank ? " (#" + r.vector_rank + ")" : "") + "</span></td>" +
        "<td class='meta'>" + esc(trunc(r.chunk, 180)) + "</td></tr>"
      ).join("");
      $("#kb-test-result").innerHTML =
        "<div class='hint'>查询改写：" + esc((d.expanded_tokens || []).join(" / ")) +
        " · 向量后端：" + esc((d.meta || {}).backend || "-") + " · 模式：" + esc(d.mode || mode) + "</div>" +
        "<table class='table'><tr><th>#</th><th>命中知识块</th><th>得分</th><th>内容节选</th></tr>" + rows + "</table>";
    } catch (e) {
      $("#kb-test-result").innerHTML = emptyBox("检索失败：" + e.message);
    }
  });
  $("#btn-kb-add").addEventListener("click", () => {
    kbEditingId = null;
    $("#kb-form-title").textContent = "新增文档";
    $("#kb-title").value = ""; $("#kb-category").value = ""; $("#kb-source").value = ""; $("#kb-content").value = "";
    $("#kb-form").classList.remove("hidden");
  });
  $("#btn-kb-cancel").addEventListener("click", () => $("#kb-form").classList.add("hidden"));
  $("#btn-kb-save").addEventListener("click", async () => {
    const body = {
      title: $("#kb-title").value.trim(),
      category: $("#kb-category").value.trim() || "通用",
      content: $("#kb-content").value.trim(),
      source: $("#kb-source").value.trim(),
    };
    if (!body.title || !body.content) { alert("标题和内容不能为空"); return; }
    try {
      if (kbEditingId) await api("/api/admin/knowledge/" + kbEditingId, { method: "PUT", body: JSON.stringify(body) });
      else await api("/api/admin/knowledge", { method: "POST", body: JSON.stringify(body) });
      $("#kb-form").classList.add("hidden");
      loadKnowledge();
    } catch (e) { alert(e.message); }
  });

  /* ---------- 用户 ---------- */
  async function loadUsers() {
    try {
      const data = await api("/api/admin/users");
      const rows = data.items
        .map(
          (u) =>
            "<tr><td>#" + u.id + "</td><td><b>" + esc(u.username) + "</b><br><span class='meta'>" + esc(u.name) + "</span></td><td>" +
            badgeRole(u.role) + "</td><td><span class='meta'>" + esc(u.student_no || "-") + "<br>" + esc(u.class_name || "-") + "</span></td><td>" +
            badge(u.enabled ? "enabled" : "disabled") + "</td><td><div class='row-actions'>" +
            "<select class='input' style='min-width:90px' onchange='window._userRole(" + u.id + ",this.value)'>" +
            roleOptions(u.role) + "</select>" +
            "<button class='mini-btn' onclick='window._userEnable(" + u.id + "," + u.enabled + ")'>" + (u.enabled ? "停用" : "启用") + "</button>" +
            "<button class='mini-btn red' onclick='window._userDel(" + u.id + ")'>删除</button></div></td></tr>"
        )
        .join("");
      $("#users-table-wrap").innerHTML =
        '<div class="card"><table class="table"><tr><th>ID</th><th>账号</th><th>角色</th><th>学号/班级</th><th>状态</th><th>操作</th></tr>' + rows + "</table></div>";
    } catch (e) {
      $("#users-table-wrap").innerHTML = emptyBox("加载失败：" + e.message);
    }
  }
  function badgeRole(r) {
    return badge({ admin: "blue", teacher: "green", student: "orange", visitor: "gray" }[r] || "gray") && ("<span class='badge " + ({ admin: "blue", teacher: "green", student: "orange", visitor: "gray" }[r] || "gray") + "'>" + ({ admin: "管理员", teacher: "教师", student: "学生", visitor: "访客" }[r] || r) + "</span>");
  }
  function roleOptions(role) {
    return ["student", "teacher", "admin", "visitor"]
      .map((r) => '<option value="' + r + '"' + (r === role ? " selected" : "") + ">" + { student: "学生", teacher: "教师", admin: "管理员", visitor: "访客" }[r] + "</option>")
      .join("");
  }
  window._userRole = async function (id, role) {
    try { await api("/api/admin/users/" + id, { method: "PUT", body: JSON.stringify({ role }) }); loadUsers(); } catch (e) { alert(e.message); }
  };
  window._userEnable = async function (id, enabled) {
    try { await api("/api/admin/users/" + id, { method: "PUT", body: JSON.stringify({ enabled: enabled ? 0 : 1 }) }); loadUsers(); } catch (e) { alert(e.message); }
  };
  window._userDel = async function (id) {
    if (!confirm("确定删除用户 #" + id + "？")) return;
    try { await api("/api/admin/users/" + id, { method: "DELETE" }); loadUsers(); } catch (e) { alert(e.message); }
  };
  $("#btn-user-add").addEventListener("click", () => $("#user-form").classList.toggle("hidden"));
  $("#btn-user-cancel").addEventListener("click", () => $("#user-form").classList.add("hidden"));
  $("#btn-user-save").addEventListener("click", async () => {
    const body = {
      username: $("#u-username").value.trim(),
      password: $("#u-password").value,
      role: $("#u-role").value,
      name: $("#u-name").value.trim(),
      student_no: $("#u-student-no").value.trim(),
      department: $("#u-department").value.trim(),
      class_name: $("#u-class").value.trim(),
    };
    if (!body.username || !body.password) { alert("账号和密码不能为空"); return; }
    try {
      await api("/api/admin/users", { method: "POST", body: JSON.stringify(body) });
      $("#user-form").classList.add("hidden");
      ["u-username", "u-password", "u-name", "u-student-no", "u-department", "u-class"].forEach((id) => ($("#" + id).value = ""));
      loadUsers();
    } catch (e) { alert(e.message); }
  });

  /* ---------- MCP ---------- */
  async function loadMcp() {
    try {
      const data = await api("/api/admin/mcp");
      const rows = data.items
        .map(
          (m) =>
            "<tr><td><b>" + esc(m.name) + "</b><br><span class='meta'>" + esc(m.command) + " " + esc((m.args || []).join(" ")) + "</span></td><td>" +
            badge(m.status || "unknown") + "<br><span class='meta'>" + esc(trunc(m.last_error || "", 80)) + "</span></td><td>" +
            (m.tools ? m.tools.length : 0) + " 个工具</td><td>" +
            badge(m.enabled ? "enabled" : "disabled") + "</td><td><div class='row-actions'>" +
            "<button class='mini-btn' onclick='window._mcpTest(" + m.id + ")'>测试</button>" +
            "<button class='mini-btn' onclick='window._mcpToggle(" + m.id + ")'>" + (m.enabled ? "停用" : "启用") + "</button>" +
            "<button class='mini-btn red' onclick='window._mcpDel(" + m.id + ")'>删除</button></div></td></tr>"
        )
        .join("");
      $("#mcp-table-wrap").innerHTML =
        '<div class="card"><table class="table"><tr><th>名称</th><th>状态</th><th>工具</th><th>启用</th><th>操作</th></tr>' +
        (rows || emptyBox("暂无 MCP 服务")) + "</table></div>";
    } catch (e) {
      $("#mcp-table-wrap").innerHTML = emptyBox("加载失败：" + e.message);
    }
  }
  window._mcpTest = async function (id) {
    try { const r = await api("/api/admin/mcp/" + id + "/test", { method: "POST" }); alert("状态：" + r.status + (r.last_error ? "\n错误：" + r.last_error : "") + "\n工具数：" + (r.tools || []).length); loadMcp(); } catch (e) { alert(e.message); }
  };
  window._mcpToggle = async function (id) {
    try { await api("/api/admin/mcp/" + id + "/toggle", { method: "POST" }); loadMcp(); } catch (e) { alert(e.message); }
  };
  window._mcpDel = async function (id) {
    if (!confirm("删除该 MCP 服务？")) return;
    try { await api("/api/admin/mcp/" + id, { method: "DELETE" }); loadMcp(); } catch (e) { alert(e.message); }
  };
  $("#btn-mcp-add").addEventListener("click", () => $("#mcp-form").classList.toggle("hidden"));
  $("#btn-mcp-cancel").addEventListener("click", () => $("#mcp-form").classList.add("hidden"));
  $("#btn-mcp-refresh").addEventListener("click", loadMcp);
  $("#btn-mcp-save").addEventListener("click", async () => {
    let args = [];
    let env = {};
    try { args = JSON.parse($("#mcp-args").value || "[]"); } catch (e) { alert("参数 JSON 格式错误"); return; }
    try { env = JSON.parse($("#mcp-env").value || "{}"); } catch (e) { alert("环境变量 JSON 格式错误"); return; }
    const body = {
      name: $("#mcp-name").value.trim(),
      command: $("#mcp-command").value.trim(),
      args, env, transport: $("#mcp-transport").value, enabled: true,
    };
    if (!body.name || !body.command) { alert("名称和命令不能为空"); return; }
    try {
      await api("/api/admin/mcp", { method: "POST", body: JSON.stringify(body) });
      $("#mcp-form").classList.add("hidden");
      ["mcp-name", "mcp-command", "mcp-args", "mcp-env"].forEach((id) => ($("#" + id).value = ""));
      setTimeout(loadMcp, 3000);
    } catch (e) { alert(e.message); }
  });

  /* ---------- 申请审批 ---------- */
  async function loadApplications() {
    try {
      const d = await api("/api/admin/applications");
      $("#app-leaves").innerHTML = '<div class="card"><table class="table"><tr><th>ID</th><th>申请人</th><th>类型/时间</th><th>事由</th><th>状态</th><th>操作</th></tr>' +
        d.leaves.map((x) =>
          "<tr><td>#" + x.id + "</td><td>" + esc(x.user_name || x.username) + "</td><td>" + esc(x.leave_type) + "<br><span class='meta'>" + esc(x.start_date + " ~ " + x.end_date) + "</span></td><td>" + esc(trunc(x.reason, 60)) + "</td><td>" + badge(x.status) + "</td><td><div class='row-actions'>" +
          (x.status === "pending" ? "<button class='mini-btn green' onclick='window._leaveDec(" + x.id + ",true)'>批准</button><button class='mini-btn red' onclick='window._leaveDec(" + x.id + ",false)'>驳回</button>" : '<span class="meta">已处理</span>') +
          "</div></td></tr>"
        ).join("") + "</table></div>";
      $("#app-repairs").innerHTML = '<div class="card"><table class="table"><tr><th>ID</th><th>报修人</th><th>地点/类别</th><th>描述</th><th>状态</th><th>操作</th></tr>' +
        d.repairs.map((x) =>
          "<tr><td>#" + x.id + "</td><td>" + esc(x.user_name || x.username) + "</td><td>" + esc(x.location) + "<br><span class='meta'>" + esc(x.category) + "</span></td><td>" + esc(trunc(x.description, 60)) + "<br><span class='meta'>" + esc(trunc(x.handler_note || "", 40)) + "</span></td><td>" + badge(x.status) + "</td><td><div class='row-actions'>" +
          "<select class='input' style='min-width:100px' onchange='window._repairUpd(" + x.id + ",this.value)'>" +
          ["pending", "processing", "done", "closed"].map((s) => '<option value="' + s + '"' + (s === x.status ? " selected" : "") + ">" + { pending: "待处理", processing: "处理中", done: "已完成", closed: "已关闭" }[s] + "</option>").join("") +
          "</select></div></td></tr>"
        ).join("") + "</table></div>";
      $("#app-bookings").innerHTML = '<div class="card"><table class="table"><tr><th>ID</th><th>预约人</th><th>场地/时间</th><th>用途</th><th>状态</th><th>操作</th></tr>' +
        d.bookings.map((x) =>
          "<tr><td>#" + x.id + "</td><td>" + esc(x.user_name) + "</td><td>" + esc(x.venue_name) + "<br><span class='meta'>" + esc(x.book_date + " " + x.start_time + "-" + x.end_time) + "</span></td><td>" + esc(trunc(x.purpose || "", 50)) + "</td><td>" + badge(x.status) + "</td><td><div class='row-actions'>" +
          (x.status === "pending" ? "<button class='mini-btn green' onclick='window._bookDec(" + x.id + ",true)'>批准</button><button class='mini-btn red' onclick='window._bookDec(" + x.id + ",false)'>驳回</button>" : '<span class="meta">已处理</span>') +
          "</div></td></tr>"
        ).join("") + "</table></div>";
    } catch (e) {
      ["app-leaves", "app-repairs", "app-bookings"].forEach((id) => ($("#" + id).innerHTML = emptyBox("加载失败：" + e.message)));
    }
  }
  window._leaveDec = async function (id, approve) {
    try { await api("/api/admin/leaves/" + id + "/decision", { method: "POST", body: JSON.stringify({ approve }) }); loadApplications(); } catch (e) { alert(e.message); }
  };
  window._repairUpd = async function (id, status) {
    const note = prompt("处理说明（可选）：");
    if (note === null) { loadApplications(); return; }
    try { await api("/api/admin/repairs/" + id + "/update", { method: "POST", body: JSON.stringify({ status, note }) }); loadApplications(); } catch (e) { alert(e.message); }
  };
  window._bookDec = async function (id, approve) {
    try { await api("/api/admin/bookings/" + id + "/decision", { method: "POST", body: JSON.stringify({ approve }) }); loadApplications(); } catch (e) { alert(e.message); }
  };
  document.querySelectorAll(".sub-tab").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".sub-tab").forEach((x) => x.classList.toggle("active", x === b));
      const name = b.dataset.sub;
      ["app-leaves", "app-repairs", "app-bookings"].forEach((id) => $("#" + id).classList.toggle("active", id === "app-" + name));
    })
  );

  /* ---------- 工单 ---------- */
  async function loadTickets() {
    try {
      const d = await api("/api/admin/tickets");
      if (!d.items.length) { $("#tickets-wrap").innerHTML = emptyBox("暂无人工工单"); return; }
      $("#tickets-wrap").innerHTML = d.items
        .map(
          (t) =>
            '<div class="card"><div class="meta">工单 #' + t.id + " · " + esc(t.user_name || t.username) + " · " + esc(t.created_at) + " · " + badge(t.status) + "</div>" +
            "<div style='margin:8px 0'><b>" + esc(t.question) + "</b></div>" +
            (t.agent_context ? '<div class="meta">上下文：' + esc(trunc(t.agent_context, 200)) + "</div>" : "") +
            (t.reply ? '<div style="margin-top:6px;color:#127a54">回复：' + esc(t.reply) + "</div>" : "") +
            (t.status === "open" ? '<div class="reply-box"><input id="ticket-reply-' + t.id + '" placeholder="回复内容"><button class="mini-btn green" onclick="window._replyTicket(' + t.id + ')">回复并关闭</button></div>' : "") +
            "</div>"
        )
        .join("");
    } catch (e) {
      $("#tickets-wrap").innerHTML = emptyBox("加载失败：" + e.message);
    }
  }
  window._replyTicket = async function (id) {
    const reply = $("#ticket-reply-" + id).value.trim();
    if (!reply) { alert("请输入回复"); return; }
    try { await api("/api/admin/tickets/" + id + "/reply", { method: "POST", body: JSON.stringify({ reply }) }); loadTickets(); } catch (e) { alert(e.message); }
  };

  /* ---------- 日志与反馈 ---------- */
  async function loadLogs() {
    try {
      const d = await api("/api/admin/logs?limit=100");
      if (!d.items.length) { $("#logs-wrap").innerHTML = emptyBox("暂无日志"); return; }
      $("#logs-wrap").innerHTML =
        '<div class="card"><table class="table"><tr><th>时间</th><th>用户</th><th>意图</th><th>提问</th><th>工具</th><th>回答摘要</th><th>IP</th></tr>' +
        d.items
          .map(
            (x) =>
              "<tr><td class='meta'>" + esc(x.created_at) + "</td><td>" + esc(x.username) + " <span class='meta'>(" + esc(x.role) + ")</span></td><td>" + esc(x.intent || "-") + "</td>" +
              '<td class="log-query">' + esc(trunc(x.query, 60)) + "</td><td class='meta'>" + esc(trunc(x.tools || "", 60)) + '</td><td class="log-answer">' + esc(trunc(x.answer, 80)) + "</td><td class='meta'>" + esc(x.ip || "-") + "</td></tr>"
          )
          .join("") +
        "</table></div>";
    } catch (e) {
      $("#logs-wrap").innerHTML = emptyBox("加载失败：" + e.message);
    }
  }

  async function loadFeedback() {
    try {
      const d = await api("/api/admin/logs?limit=1").catch(() => null);
      // 反馈走独立查询接口：复用 feedback 表
      const r = await fetch("/api/admin/feedback", { headers: { Authorization: "Bearer " + token } });
      if (!r.ok) throw new Error("无权限");
      const fb = await r.json();
      if (!fb.items.length) { $("#feedback-wrap").innerHTML = emptyBox("暂无反馈"); return; }
      $("#feedback-wrap").innerHTML =
        '<div class="card"><table class="table"><tr><th>时间</th><th>用户</th><th>评分</th><th>留言</th></tr>' +
        fb.items
          .map(
            (x) =>
              "<tr><td class='meta'>" + esc(x.created_at) + "</td><td>" + esc(x.username || "-") + "</td><td>" + "★".repeat(x.rating) + "☆".repeat(5 - x.rating) + "</td><td>" + esc(x.comment || "-") + "</td></tr>"
          )
          .join("") +
        "</table></div>";
    } catch (e) {
      $("#feedback-wrap").innerHTML = emptyBox("加载失败：" + e.message);
    }
  }

  /* ---------- 初始化 ---------- */
  document.querySelectorAll("#admin-nav .tab").forEach((b) => b.addEventListener("click", () => tab(b.dataset.tab)));
  $("#btn-admin-logout").addEventListener("click", () => {
    sessionStorage.removeItem("bhu_token");
    sessionStorage.removeItem("bhu_user");
    window.location.href = "/";
  });

  /* ---------- 主动推送：管理员 / 教师端消息通知 ---------- */
  const NOTIF_TYPE_CN = {
    leave_result: "请假审批", ticket_reply: "人工回复", repair_update: "报修进度",
    booking_result: "场地预约", todo_leave: "待审批", todo_repair: "待处理",
    todo_booking: "待审批", todo_ticket: "新工单", system: "系统",
  };
  let notifTimer = null;
  let lastNotifId = 0;
  let notifLoadedOnce = false;

  function maxId(items) { return items.reduce((m, x) => Math.max(m, x.id || 0), 0); }

  async function pollNotifs(isFirst) {
    try {
      const d = await api("/api/notifications?unread=1&limit=20");
      const items = d.items || [];
      const badge = $("#admin-notif-badge");
      badge.textContent = d.count > 99 ? "99+" : String(d.count);
      badge.classList.toggle("hidden", !d.count);
      if (isFirst || !notifLoadedOnce) {
        notifLoadedOnce = true;
        lastNotifId = maxId(items);
      } else {
        const fresh = items.filter((x) => x.id > lastNotifId);
        lastNotifId = Math.max(lastNotifId, maxId(items));
        fresh.reverse().forEach((n) => {
          toast(n.title, n.content || "");
          const active = document.querySelector(".tab.active");
          if (active && n.type && n.type.indexOf("todo_leave") === 0 && active.dataset.tab === "applications") loadApplications();
          if (active && n.type === "todo_ticket" && active.dataset.tab === "tickets") loadTickets();
        });
      }
    } catch (e) { /* 轮询失败静默 */ }
  }

  function toast(title, content) {
    const el = document.createElement("div");
    el.className = "admin-toast";
    el.innerHTML = "<div class='tt'>📢 " + esc(title) + "</div><div class='tc'>" + esc(trunc(content, 120)) + "</div>";
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 12000);
  }

  async function loadNotifList() {
    const box = $("#admin-notif-list");
    try {
      const d = await api("/api/notifications?limit=30");
      const items = d.items || [];
      if (!items.length) {
        box.innerHTML = "<div class='notif-empty'>还没有通知。学生提交请假、报修、场地预约或人工工单时会推送给管理员。</div>";
        return;
      }
      box.innerHTML = items.map((n) =>
        "<div class='notif-item " + (n.read_at ? "" : "unread") + "' data-id='" + n.id + "'>" +
        "<div class='t'><span class='notif-type'>" + esc(NOTIF_TYPE_CN[n.type] || "通知") + "</span>" + esc(n.title) + "</div>" +
        "<div class='c'>" + esc(n.content || "") + "</div>" +
        "<div class='d'>" + esc((n.created_at || "").slice(5, 16)) + (n.read_at ? " · 已读" : " · 未读") + "</div></div>"
      ).join("");
      box.querySelectorAll(".notif-item").forEach((el) => {
        el.onclick = async () => {
          try { await api("/api/notifications/" + el.getAttribute("data-id") + "/read", { method: "POST" }); } catch (e) {}
          await loadNotifList();
          pollNotifs(false);
        };
      });
    } catch (e) {
      box.innerHTML = "<div class='notif-empty'>加载失败：" + esc(e.message) + "</div>";
    }
  }

  $("#admin-notif-bell").addEventListener("click", async () => {
    const p = $("#admin-notif-panel");
    p.classList.toggle("hidden");
    if (!p.classList.contains("hidden")) await loadNotifList();
  });
  $("#admin-notif-readall").addEventListener("click", async () => {
    try { await api("/api/notifications/read_all", { method: "POST" }); } catch (e) {}
    $("#admin-notif-badge").classList.add("hidden");
    await loadNotifList();
  });
  document.addEventListener("click", (e) => {
    const p = $("#admin-notif-panel");
    if (!p || p.classList.contains("hidden")) return;
    if (p.contains(e.target) || e.target.closest("#admin-notif-bell")) return;
    p.classList.add("hidden");
  });

  async function init() {
    try {
      const me = await api("/api/me");
      if (me.role !== "admin") { window.location.href = "/"; return; }
      tab("overview");
      pollNotifs(true);
      notifTimer = setInterval(() => pollNotifs(false), 5000);
    } catch (e) {
      window.location.href = "/";
    }
  }
  init();
})();

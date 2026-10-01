/* Aplicacao do cliente: autenticacao, projetos, capturas, reguas dinamicas,
   vetores, historico, usuarios, exportacao e traducao (pt/en/es/ca). */
(function () {
  "use strict";

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const T = (k, f) => window.I18N.t(k, f);
  const api = {
    async req(method, url, body) {
      const opt = { method, headers: {}, credentials: "same-origin" };
      if (body !== undefined) {
        opt.headers["Content-Type"] = "application/json";
        opt.body = JSON.stringify(body);
      }
      const r = await fetch(url, opt);
      const txt = await r.text();
      let data = null;
      try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = { detail: txt }; }
      if (!r.ok) throw Object.assign(new Error((data && data.detail) || r.statusText), {
        status: r.status, data });
      return data;
    },
    get(u) { return this.req("GET", u); },
    post(u, b) { return this.req("POST", u, b === undefined ? {} : b); },
    patch(u, b) { return this.req("PATCH", u, b); },
    del(u) { return this.req("DELETE", u); }
  };

  const S = {
    user: null, project: null, projects: [], captures: [], capture: null,
    measurements: [], vectors: [], exports: [], cloud: null,
    tool: null, pending: [], live: false, busy: false, fields: {}
  };
  let painter = null;

  // ------------------------------------------------------------------ utils
  function toast(msg, kind) {
    let box = $(".toast");
    if (!box) { box = document.createElement("div"); box.className = "toast"; document.body.appendChild(box); }
    const d = document.createElement("div");
    d.className = kind || "";
    d.textContent = msg;
    box.appendChild(d);
    setTimeout(() => d.style.opacity = "0", 3200);
    setTimeout(() => d.remove(), 3800);
  }
  const m2cm = (v) => (v * 100).toFixed(1);
  const fmtM = (v, d) => (v === undefined || v === null || isNaN(v)) ? "—" : (+v).toFixed(d === undefined ? 2 : d) + " m";
  const fmtN = (v) => (v === undefined || v === null || isNaN(v)) ? "—" : Math.round(v).toLocaleString();
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function errMsg(e) {
    const map = { credenciais_invalidas: "bad_login", email_ja_registado: "email_exists",
                  password_too_short: "pw_short" };
    const k = map[e && e.data && e.data.detail];
    return k ? T("auth." + k) : ((e && (e.data && e.data.detail || e.message)) || T("msg.error"));
  }
  const snap = (p) => [Math.round(p[0]*100)/100, Math.round(p[1]*100)/100, Math.round(p[2]*100)/100];
  const dist3 = (a, b) => Math.hypot(b[0]-a[0], b[1]-a[1], b[2]-a[2]);

  // -------------------------------------------------------------- autenticacao
  function showGate() { $("#gate").classList.remove("hidden"); $("#app").classList.add("hidden"); }
  function showApp() { $("#gate").classList.add("hidden"); $("#app").classList.remove("hidden"); }

  async function afterLogin(user) {
    S.user = user;
    $("#userName").textContent = user.name || user.email;
    $("#userRole").textContent = user.role === "admin" ? "Administrador" : "Usuario";
    $("#userAvatar").textContent = (user.name || user.email).trim().charAt(0).toUpperCase();
    const lang = user.lang || window.I18N.lang;
    if (lang && lang !== window.I18N.lang) await window.I18N.use(lang);
    showApp();
    await loadProjects();
    await loadFields();
    if (user.role === "admin") loadUsers(); else $("#viewAdmin").classList.add("hidden");
  }

  async function boot() {
    await window.I18N.init();
    $$(".gate-tabs .tab").forEach((b) => b.addEventListener("click", () => {
      $$(".gate-tabs .tab").forEach((x) => x.classList.toggle("is-active", x === b));
      $("#formLogin").classList.toggle("hidden", b.dataset.tab !== "login");
      $("#formRegister").classList.toggle("hidden", b.dataset.tab !== "register");
      $("#gateMsg").textContent = "";
    }));

    $("#formLogin").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        const r = await api.post("/api/auth/login",
          { email: f.email.value.trim(), password: f.password.value });
        await afterLogin(r.user);
      } catch (err) { $("#gateMsg").textContent = errMsg(err); }
    });
    $("#formRegister").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        const r = await api.post("/api/auth/register",
          { email: f.email.value.trim(), name: f.name.value.trim(),
            password: f.password.value, lang: window.I18N.lang });
        await afterLogin(r.user);
      } catch (err) { $("#gateMsg").textContent = errMsg(err); }
    });

    try {
      const me = await api.get("/api/auth/me");
      await afterLogin(me.user);
    } catch (e) { showGate(); }

    try {
      const h = await api.get("/api/health");
      const cap = h.capabilities || {};
      $("#chipMode").textContent = (cap.mode === "csi_mapper" ? "DSP CSI" : "modo basico")
        + (cap.open3d ? " + Open3D" : " + voxels");
    } catch (e) { /* segue */ }
  }

  // ------------------------------------------------------------- admin
  async function loadUsers() {
    try {
      const r = await api.get("/api/users");
      $("#usersBody").innerHTML = r.users.map((u) => `
        <tr data-id="${u.id}">
          <td>${esc(u.name)}</td><td>${esc(u.email)}</td>
          <td><select class="u-role">
            <option value="user"${u.role === "user" ? " selected" : ""}>Usuario</option>
            <option value="admin"${u.role === "admin" ? " selected" : ""}>Administrador</option>
          </select></td>
          <td><input type="checkbox" class="u-active"${u.active ? " checked" : ""}></td>
          <td>${u.id === S.user.id ? "—" : ""}</td>
          <td>${u.last_login ? new Date(u.last_login * 1000).toLocaleString() : "—"}</td>
          <td><button class="mini del u-del" title="Excluir">✕</button></td>
        </tr>`).join("");
      $$("#usersBody tr").forEach((tr) => {
        const id = +tr.dataset.id;
        const save = async () => {
          try {
            await api.patch("/api/users/" + id,
              { role: $(".u-role", tr).value, active: $(".u-active", tr).checked ? 1 : 0 });
            toast(T("users.saved"), "ok");
          } catch (e) { toast(errMsg(e), "err"); }
        };
        $(".u-role", tr).addEventListener("change", save);
        $(".u-active", tr).addEventListener("change", save);
        $(".u-del", tr).addEventListener("click", async () => {
          if (!confirm("Excluir usuario?")) return;
          try { await api.del("/api/users/" + id); await loadUsers(); toast(T("msg.deleted"), "ok"); }
          catch (e) { toast(errMsg(e), "err"); }
        });
      });
    } catch (e) { $("#usersMsg").textContent = T("users.only_admin"); }
  }

  // ------------------------------------------------------------ navegacao
  function bindNav() {
    $$(".mtab").forEach((b) => b.addEventListener("click", async () => {
      $$(".mtab").forEach((x) => x.classList.toggle("is-active", x === b));
      const v = b.dataset.view;
      $("#viewMap").classList.toggle("hidden", v !== "map");
      $("#viewHistory").classList.toggle("hidden", v !== "history");
      $("#viewAdmin").classList.toggle("hidden", v !== "admin");
      if (v === "history") loadHistory();
      if (v === "admin") loadUsers();
    }));
    $("#btnLogout").addEventListener("click", async () => {
      try { await api.post("/api/auth/logout"); } catch (e) { /* segue */ }
      S.user = null; showGate();
    });
    document.addEventListener("langchange", async () => {
      renderAll();
      if (S.user) {
        try { await api.patch("/api/auth/me", { lang: window.I18N.lang }); } catch (e) { /* segue */ }
      }
    });
  }

  // ------------------------------------------------------------- projetos
  async function loadProjects(q) {
    const r = await api.get("/api/projects" + (q ? "?q=" + encodeURIComponent(q) : ""));
    S.projects = r.projects;
    renderProjects();
    if (!S.project && S.projects.length) await openProject(S.projects[0].id);
  }

  function renderProjects() {
    const ul = $("#projectList");
    if (!S.projects.length) {
      ul.innerHTML = `<li class="hint">${T("proj.empty")}</li>`;
      return;
    }
    ul.innerHTML = S.projects.map((p) => `
      <li data-id="${p.id}" class="${S.project && S.project.id === p.id ? "is-active" : ""}">
        <div class="grow">
          <b>${esc(p.name)}</b>
          <small>${p.room_x}×${p.room_y}×${p.room_z} m · ${p.captures} ${T("hist.captures")} ·
            ${p.measurements} ${T("hist.measures")}</small>
        </div>
      </li>`).join("");
    $$("#projectList li[data-id]").forEach((li) =>
      li.addEventListener("click", () => openProject(+li.dataset.id)));
  }

  async function openProject(id) {
    S.project = S.projects.find((p) => p.id === id) || (await api.get("/api/projects/" + id)).project;
    S.capture = null; S.measurements = []; S.vectors = []; S.cloud = null;
    renderProjects();
    painter = painter || new window.CSIGL.Painter($("#gl"), {
      onPick: onStagePick, pickMode: false });
    await refreshProject();
    applyViewUI();
  }

  async function refreshProject() {
    if (!S.project) return;
    const p = S.project;
    const [caps, meas, vecs, exps] = await Promise.all([
      api.get(`/api/projects/${p.id}/captures`),
      api.get(`/api/projects/${p.id}/measurements`),
      api.get(`/api/projects/${p.id}/vectors`),
      api.get(`/api/projects/${p.id}/exports`)
    ]);
    S.captures = caps.captures; S.measurements = meas.measurements;
    S.vectors = vecs.vectors; S.exports = exps.exports;
    renderCaptures(); renderMeasurements(); renderVectors(); renderExports();
    if (S.captures.length && !S.capture) await loadCapture(S.captures[0].id);
    else if (!S.captures.length) { clearStage(); }
  }

  function renderCaptures() {
    $("#capCount").textContent = S.captures.length;
    const ul = $("#captureList");
    if (!S.captures.length) { ul.innerHTML = `<li class="hint">${T("cap.none")}</li>`; return; }
    ul.innerHTML = S.captures.map((c) => `
      <li data-id="${c.id}" class="${S.capture && S.capture.id === c.id ? "is-active" : ""}">
        <span class="dot-kind ${c.kind === "hardware" ? "hw" : ""}"></span>
        <div class="grow"><b>${esc(c.label || ("#" + c.id))}</b>
        <small>${new Date(c.created_at * 1000).toLocaleString()} · ${fmtN(c.n_points)} pts ·
          ${fmtN(c.n_triangles)} tri</small></div>
        <button class="mini del" title="Excluir">✕</button>
      </li>`).join("");
    $$("#captureList li[data-id]").forEach((li) => {
      li.addEventListener("click", (e) => {
        if (e.target.classList.contains("del")) return;
        loadCapture(+li.dataset.id);
      });
      $(".del", li).addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(T("cap.confirm_delete"))) return;
        try { await api.del("/api/captures/" + li.dataset.id); await refreshProject(); }
        catch (err) { toast(errMsg(err), "err"); }
      });
    });
  }

  function clearStage() {
    S.capture = null; S.cloud = null;
    if (painter) { painter.setPoints(new Float32Array(0)); painter.setMesh(null); painter.setLayer("traj", null); }
    $("#emptyStage").classList.remove("hidden");
    ["mPoints","mTris","mVol","mCov","mErr","mSnr","mMethod","mDur"].forEach((k) => $("#" + k).textContent = "—");
    $("#chipMode").textContent = "—";
  }

  const ANIM = new Set(["mPoints","mTris","mVol","mCov","mErr"]);
  function setMetric(id, value) {
    const el = $("#" + id);
    el.textContent = value;
    if (ANIM.has(id)) { el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump"); }
  }

  async function loadCapture(id) {
    const r = await api.get("/api/captures/" + id);
    const c = r.capture;
    S.capture = c;
    // nuvem -> Float32Array [x,y,z,r,g,b,a]
    const pts = c.points || [];
    let rmax = 0.001;
    for (const p of pts) rmax = Math.max(rmax, Math.hypot(p[0], p[1], p[2]));
    const flat = new Float32Array(pts.length * 7);
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      const col = window.CSIGL.distColor(Math.hypot(p[0], p[1], p[2]) / rmax);
      flat[i*7] = p[0]; flat[i*7+1] = p[1]; flat[i*7+2] = p[2];
      flat[i*7+3] = col[0]; flat[i*7+4] = col[1]; flat[i*7+5] = col[2]; flat[i*7+6] = 1;
    }
    S.cloud = flat;
    painter.setPoints(flat);
    painter.setMesh(c.mesh);
    painter.setLayer("traj", new Float32Array((c.traj || []).flat()));

    const m = c.metrics || {};
    setMetric("mPoints", fmtN(m.n_points));
    setMetric("mTris", fmtN(m.n_triangles));
    setMetric("mVol", (m.volume_voxel_m3 || 0).toFixed(2) + " m³");
    setMetric("mCov", (((m.coverage_25cm || 0) * 100).toFixed(0)) + "%");
    setMetric("mErr", fmtM(m.err_cloud_mean_m || m.err_surface_mean_m, 2));
    setMetric("mSnr", (c.calib && c.calib.snr_db ? c.calib.snr_db.toFixed(1) : "—") + " dB");
    setMetric("mMethod", c.method || "—");
    setMetric("mDur", (m.duration_s || 0).toFixed(1) + " s");
    $("#chipMode").textContent = S.capture.kind === "hardware" ? "hardware" : "simulacao";
    $("#emptyStage").classList.toggle("hidden", true);
    const room = [S.project.room_x, S.project.room_y, S.project.room_z];
    buildAux(room);
    painter.applyView(painter.state.cam === "livre" ? "iso" : "iso", room);
    painter.reset(room);
    renderCaptures();
    redrawAnnotations();
  }

  function buildAux(room) {
    const a = window.CSIGL.buildAux(room);
    painter.setLayer("grid", a.grid);
    painter.setLayer("room", a.room);
    painter.setLayer("axes", a.axes);
  }

  // --------------------------------------------------------- captura (mapa)
  function bindCapture() {
    $("#btnMap").addEventListener("click", runCapture);
    $("#btnCancel").addEventListener("click", () => { S.busy = false; toast("Cancelado"); });

    const seg = $("#viewSeg");
    $$("button", seg).forEach((b) => b.addEventListener("click", () => {
      $$("button", seg).forEach((x) => x.classList.toggle("is-active", x === b));
      painter.applyView(b.dataset.view, [S.project.room_x, S.project.room_y, S.project.room_z]);
    }));
    $("#btnReset").addEventListener("click", () =>
      painter.reset([S.project.room_x, S.project.room_y, S.project.room_z]));
    $("#btnWalk").addEventListener("click", () => {
      const on = !painter.state.walk;
      painter.setWalk(on);
      $("#btnWalk").classList.toggle("primary", on);
    });
    const st = $("#styleSeg");
    $$("button", st).forEach((b) => b.addEventListener("click", () => {
      $$("button", st).forEach((x) => x.classList.toggle("is-active", x === b));
      painter.setMode(b.dataset.style);
    }));
    $("#ptSize").addEventListener("input", (e) => painter.setPointSize(+e.target.value));

    const toggles = { chkGrid: "grid", chkAxes: "axes", chkRouter: "router",
                      chkTraj: "traj", chkRoom: "room" };
    Object.entries(toggles).forEach(([id, k]) =>
      $("#" + id).addEventListener("change", (e) => painter.show(k, e.target.checked)));
    $("#chkShowMeas").addEventListener("change", (e) => { painter.state.showMeas = e.target.checked; });
    $("#chkShowVec").addEventListener("change", (e) => { painter.state.showVec = e.target.checked; });

    $("#projSearch").addEventListener("input", (e) => debounce(() => loadProjects(e.target.value), 320)());
    $("#btnNewProject").addEventListener("click", newProject);
  }

  let _dt = null;
  function debounce(fn, ms) { return () => { clearTimeout(_dt); _dt = setTimeout(fn, ms); }; }

  async function newProject() {
    const name = prompt(T("proj.name"), "Sala " + new Date().toLocaleDateString());
    if (!name) return;
    const room = prompt("Comodo largura,profundidade,altura (m)", "4,5,2.8") || "4,5,2.8";
    const [x, y, z] = room.split(",").map((v) => parseFloat(v) || 0);
    try {
      const r = await api.post("/api/projects",
        { name, room_x: x || 4, room_y: y || 5, room_z: z || 2.8 });
      await loadProjects(); await openProject(r.project.id);
      toast(T("proj.created"), "ok");
    } catch (e) { toast(errMsg(e), "err"); }
  }

  async function runCapture() {
    if (!S.project) return toast(T("cap.select_project"), "err");
    if (S.busy) return;
    S.busy = true;
    $("#btnMap").disabled = true; $("#btnCancel").disabled = false;
    $("#progressWrap").classList.add("on");
    S.live = true;
    const phases = [["phase_calib", 8], ["phase_scan", 26], ["phase_geom", 62], ["phase_recon", 86]];
    let idx = 0, pct = 0;
    const timer = setInterval(() => {
      pct = Math.min(96, pct + Math.random() * 5 + 1.4);
      const want = phases[Math.min(idx, phases.length - 1)];
      if (pct > want[1] && idx < phases.length - 1) idx++;
      const ph = phases[idx];
      $("#pgPct").textContent = Math.round(pct) + "%";
      $("#pgPhase").textContent = T("cap." + ph[0]);
      $("#pgMsg").textContent = T("cap.running");
      $("#pgFill").parentElement.style.background =
        `conic-gradient(var(--acc) ${pct*3.6}deg, #1a2735 0deg)`;
      $("#hudStatus").textContent = T("cap." + ph[0]);
    }, 420);

    try {
      const r = await api.post(`/api/projects/${S.project.id}/captures/run`,
        { label: T("cap.title") + " " + new Date().toLocaleTimeString() });
      clearInterval(timer);
      $("#pgPct").textContent = "100%"; $("#pgFill").parentElement.style.background =
        "conic-gradient(var(--acc) 360deg, #1a2735 0deg)";
      $("#pgPhase").textContent = T("cap.done");
      $("#pgMsg").textContent = (r.method || "");
      await loadProjects(); await openProject(S.project.id);
      await loadCapture(r.capture_id);
      $("#hudStatus").textContent = T("cap.done");
      toast(T("cap.saved"), "ok");
      setTimeout(() => $("#progressWrap").classList.remove("on"), 900);
    } catch (e) {
      clearInterval(timer);
      $("#progressWrap").classList.remove("on");
      toast(T("cap.error") + ": " + errMsg(e), "err");
    } finally {
      S.busy = false; S.live = false;
      $("#btnMap").disabled = false; $("#btnCancel").disabled = true;
    }
  }

  // ------------------------------------------------------- reguas dinâmicas
  function bindTools() {
    $$("#toolGrid .tool").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.measure)));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { cancelTool(); }
      if (e.key === "Enter" && S.tool === "vector" && S.pending.length >= 2) finishVector();
    });
  }

  function setTool(t) {
    S.tool = (S.tool === t) ? null : t;
    S.pending = [];
    painter.setMeasMode(!!S.tool);
    $$("#toolGrid .tool").forEach((b) => b.classList.toggle("is-active", b.dataset.measure === S.tool));
    const hint = { width: "meas.width", depth: "meas.depth", height: "meas.height",
                   distance: "meas.distance", vector: "vec.new" }[S.tool];
    $("#hudStatus").textContent = S.tool ? T(hint) : T("hud.ready");
    $("#rulerLive").classList.toggle("hidden", S.tool !== "vector");
    if (S.tool) $("#gl").classList.add("picking"); else $("#gl").classList.remove("picking");
  }
  function cancelTool() {
    S.pending = [];
    if (S.tool === "vector") { S.tool = null; setTool(null); }
    $("#gl").classList.remove("picking");
    $("#hudStatus").textContent = T("hud.ready");
    if (painter) painter.setLayer("vecLive", null);
  }

  function onStagePick(e) {
    if (!S.tool || !S.cloud) return;
    const p = painter.pickPoint(e.clientX, e.clientY, S.cloud);
    if (!p) return;
    const point = $("#chkSnap").checked ? snap(p) : [p[0], p[1], p[2]];
    if (S.tool === "vector") {
      S.pending.push(point);
      $("#hudStatus").textContent = T("hud.vector") + " (" + S.pending.length + ")";
      updateVecLive();
      return;
    }
    S.pending.push(point);
    if (S.pending.length === 1) {
      $("#hudStatus").textContent = T("meas.point1");
      painter.setLayer("measLive", { pts: [], color: [1, 0.82, 0.4, 1] });
      return;
    }
    saveMeasurement(S.tool, S.pending[0], S.pending[1]);
    S.pending = [];
    $("#hudStatus").textContent = T("meas." + ({ width: "width", depth: "depth",
      height: "height", distance: "distance" }[S.tool]));
  }

  function updateVecLive() {
    if (!painter) return;
    const pts = S.pending.flat();
    painter.setLayer("vecLive", pts.length >= 6
      ? { pts, color: [0.72, 0.55, 1, 0.95] } : null);
  }

  async function saveMeasurement(kind, p1, p2) {
    try {
      await api.post(`/api/projects/${S.project.id}/measurements`,
        { kind, p1, p2, capture_id: S.capture ? S.capture.id : null });
      const r = await api.get(`/api/projects/${S.project.id}/measurements`);
      S.measurements = r.measurements;
      renderMeasurements(); redrawAnnotations();
      toast(T("meas.saved"), "ok");
    } catch (e) { toast(errMsg(e), "err"); }
  }

  async function finishVector() {
    if (S.pending.length < 2) return toast(T("vec.min"), "err");
    try {
      await api.post(`/api/projects/${S.project.id}/vectors`,
        { points: S.pending, name: "Vetor " + (S.vectors.length + 1),
          capture_id: S.capture ? S.capture.id : null });
      S.pending = [];
      const r = await api.get(`/api/projects/${S.project.id}/vectors`);
      S.vectors = r.vectors;
      renderVectors(); redrawAnnotations(); updateVecLive();
      toast(T("vec.saved"), "ok");
      setTool(null);
    } catch (e) { toast(errMsg(e), "err"); }
  }

  const COLORS = { width: [0.19, 0.65, 0.88, 1], depth: [0.1, 0.72, 0.47, 1],
                   height: [1, 0.82, 0.4, 1], distance: [0.88, 0.34, 0.42, 1] };

  function redrawAnnotations() {
    if (!painter) return;
    const meas = S.measurements.filter((m) => m.visible).map((m) => ({
      pts: [].concat(m.p1, m.p2), color: COLORS[m.kind] || COLORS.distance }));
    painter.setLayer("meas", meas);
    const vecs = S.vectors.filter((v) => v.visible).map((v) => ({
      pts: v.points.flat(), count: v.points.length, closed: v.closed,
      color: [1, 0.82, 0.4, 0.95] }));
    painter.setLayer("vec", vecs);
  }

  // ------------------------------------------------------------- listagens
  function renderMeasurements() {
    $("#measCount").textContent = S.measurements.length;
    const ul = $("#measList");
    if (!S.measurements.length) { ul.innerHTML = `<li class="hint">${T("meas.none")}</li>`; return; }
    const KIND = { width: "meas.kind_width", depth: "meas.kind_depth",
                   height: "meas.kind_height", distance: "meas.kind_distance" };
    ul.innerHTML = S.measurements.map((m) => `
      <li data-id="${m.id}">
        <div class="grow"><b>${T(KIND[m.kind] || "meas.kind_distance")}</b>
          <small>${m.p1.map((v) => v.toFixed(2)).join(", ")} → ${m.p2.map((v) => v.toFixed(2)).join(", ")}</small></div>
        <span class="val">${m2cm(m.value_m)} cm</span>
        <button class="mini mv" title="Mostrar/ocultar">👁</button>
        <button class="mini del" title="Excluir">✕</button>
      </li>`).join("");
    $$("#measList li[data-id]").forEach((li) => {
      const id = +li.dataset.id, m = S.measurements.find((x) => x.id === id);
      $(".del", li).addEventListener("click", async () => {
        if (!confirm(T("meas.confirm_delete"))) return;
        await api.del("/api/measurements/" + id);
        S.measurements = (await api.get(`/api/projects/${S.project.id}/measurements`)).measurements;
        renderMeasurements(); redrawAnnotations();
      });
      $(".mv", li).addEventListener("click", async () => {
        await api.patch("/api/measurements/" + id, { visible: m.visible ? 0 : 1 });
        m.visible = !m.visible; li.style.opacity = m.visible ? "1" : ".45";
        redrawAnnotations();
      });
    });
  }

  function renderVectors() {
    $("#vecCount").textContent = S.vectors.length;
    const ul = $("#vecList");
    if (!S.vectors.length) { ul.innerHTML = `<li class="hint">${T("vec.none")}</li>`; return; }
    ul.innerHTML = S.vectors.map((v) => `
      <li data-id="${v.id}">
        <div class="grow"><b>${esc(v.name)}</b>
          <small>${v.n_points} pts · ${v.n_segments} ${T("vec.segments")}${v.closed ? " · " + T("vec.closed") : ""}
            ${v.area_m2 ? " · " + v.area_m2.toFixed(2) + " m²" : ""}</small></div>
        <span class="val">${m2cm(v.total_m)} cm</span>
        <button class="mini del" title="Excluir">✕</button>
      </li>`).join("");
    $$("#vecList li[data-id]").forEach((li) => {
      $(".del", li).addEventListener("click", async () => {
        if (!confirm(T("vec.confirm_delete"))) return;
        await api.del("/api/vectors/" + li.dataset.id);
        S.vectors = (await api.get(`/api/projects/${S.project.id}/vectors`)).vectors;
        renderVectors(); redrawAnnotations();
      });
    });
  }

  // ------------------------------------------------------------- historico
  async function loadHistory() {
    const r = await api.get("/api/history");
    const box = $("#histCards");
    if (!r.areas.length) { box.innerHTML = `<p class="hint">${T("hist.empty")}</p>`; }
    else box.innerHTML = r.areas.map((a) => `
      <article class="card" data-id="${a.id}">
        <h4>${esc(a.name)}</h4>
        <p>${a.room_x}×${a.room_y}×${a.room_z} m · ${(a.triangles || 0).toLocaleString()} tri</p>
        <div class="kv">
          <div>${T("hist.captures")}<b>${a.captures}</b></div>
          <div>${T("hist.measures")}<b>${a.measurements}</b></div>
          <div>${T("hist.vectors")}<b>${a.vectors}</b></div>
        </div>
        <small class="hint">${new Date(a.updated_at * 1000).toLocaleString()} · ${T("hist.open")}</small>
      </article>`).join("");
    $$("#histCards .card").forEach((c) => c.addEventListener("click", async () => {
      await loadProjects();
      await openProject(+c.dataset.id);
      $$(".mtab").forEach((x) => x.classList.toggle("is-active", x.dataset.view === "map"));
      $("#viewMap").classList.remove("hidden");
      $("#viewHistory").classList.add("hidden");
    }));
    const tl = $("#histTimeline");
    tl.innerHTML = (r.events || []).map((e) => `
      <li><span class="t">${new Date(e.ts * 1000).toLocaleString()}</span>
      <span>${esc(e.message)}</span></li>`).join("") || `<li class="hint">${T("hist.empty")}</li>`;
  }

  // -------------------------------------------------------------- exportacao
  async function loadFields() {
    S.fields = await api.get("/api/export/fields");
    buildFields();
  }
  function buildFields() {
    const scope = $("#expScope").value;
    const reg = S.fields[scope === "capture_summary" ? "vectors" : scope] || {};
    const box = $("#expFields");
    const keys = Object.keys(reg);
    box.innerHTML = keys.map((k) => `
      <label><input type="checkbox" value="${k}" checked>${esc(reg[k].label || k)}</label>`).join("");
  }
  function buildFormats() {
    const scope = $("#expScope").value;
    const fmt = $("#expFormat");
    let list = ["csv", "json"];
    if (S.fields.formats) list = S.fields.formats.slice();
    if (scope === "cloud") list = ["csv", "json", "xyz"];
    if (scope === "mesh") list = S.fields.mesh_formats || ["obj", "ply", "stl"];
    fmt.innerHTML = list.map((f) => `<option value="${f}">${f.toUpperCase()}</option>`).join("");
  }
  function bindExport() {
    $("#expScope").addEventListener("change", () => { buildFormats(); buildFields(); });
    $("#btnExport").addEventListener("click", async () => {
      if (!S.project) return toast(T("cap.select_project"), "err");
      const scope = $("#expScope").value, format = $("#expFormat").value;
      const fields = $$("#expFields input:checked").map((i) => i.value);
      if ((scope === "cloud" || scope === "mesh") && !S.capture)
        return toast(T("exp.need_capture"), "err");
      try {
        const r = await api.post(`/api/projects/${S.project.id}/export`,
          { scope, format, fields, capture_id: S.capture ? S.capture.id : null });
        toast(T("exp.ready") + ": " + r.filename, "ok");
        S.exports = (await api.get(`/api/projects/${S.project.id}/exports`)).exports;
        renderExports();
      } catch (e) { toast(errMsg(e), "err"); }
    });
  }
  function renderExports() {
    const ul = $("#expList");
    if (!S.exports.length) { ul.innerHTML = `<li class="hint">${T("exp.none")}</li>`; return; }
    ul.innerHTML = S.exports.map((e) => `
      <li><div class="grow"><b>${esc(e.filename)}</b>
        <small>${e.fmt.toUpperCase()} · ${(e.size/1024).toFixed(1)} kB ·
        ${new Date(e.created_at * 1000).toLocaleTimeString()}</small></div>
        <a class="mini" href="${e.url}" title="${T("exp.download")}">⤓</a></li>`).join("");
  }

  // ---------------------------------------------------------------- render
  function applyViewUI() {
    if (!painter || !S.project) return;
    buildAux([S.project.room_x, S.project.room_y, S.project.room_z]);
    painter.reset([S.project.room_x, S.project.room_y, S.project.room_z]);
  }
  function renderAll() {
    renderProjects(); renderCaptures(); renderMeasurements(); renderVectors();
    renderExports(); buildFormats(); buildFields();
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindNav(); bindCapture(); bindTools(); bindExport();
    buildFormats();
    setInterval(() => { if (painter) $("#chipFps").textContent = Math.round(painter.fps) + " fps"; }, 700);
    boot().catch((e) => { $("#gateMsg").textContent = errMsg(e); showGate(); });
  });
})();

/* Local EEG collection UI. The server owns every experimental transition. */
"use strict";
(() => {
  const $ = (selector) => document.querySelector(selector);
  const clientId = crypto.randomUUID(); // A fresh page never inherits control of a recording.
  const terminalStatuses = new Set(["completed", "aborted", "interrupted", "save_failed"]);
  const conditionLabels = {attention: "专心视频", relax: "放松视频"};
  const statusLabels = {recording: "正式采集中", active: "正式采集中", saving: "保存中", completed: "已完成", aborted: "已主动中止", interrupted: "已中断", save_failed: "保存失败"};
  const deviceLabels = {disconnected: "已断开", disconnected_error: "已断开", connecting: "连接中", connected: "已连接", scanning: "搜索中", searching: "搜索中", reconnecting: "重连中", error: "连接异常", simulation: "模拟数据流", idle: "等待连接"};
  const dataLabels = {waiting: "等待脑电数据", no_data: "暂无数据", normal: "正常接收", receiving: "正常接收", insufficient: "数据不足", insufficient_data: "数据不足", interrupted: "数据已中断", stale: "数据已中断", healthy: "正常接收"};
  let state = null;
  let latestStateTime = -Infinity;
  let shownPendingFailure = "";
  let renderedKey = "";
  let dismissedSession = null;
  let draft = {subject_id: "", round: "", audio_confirmed: false, confirm_retry: false};
  let preflight = null;
  let startRequestId = crypto.randomUUID();
  let ratingRequestId = crypto.randomUUID();
  let currentVideo = null;
  const reportedMediaErrors = new WeakSet();
  let currentVideoTrial = null;
  let suppressMedia = false;
  let mediaQueue = Promise.resolve();
  let probing = false;
  let probeComplete = false;
  let probeDone = 0;
  let probeTotal = 0;
  let probeErrors = [];
  let socket = null;
  let reconnectTimer = null;
  let connected = false;
  let heartbeatBusy = false;
  let syncBusy = false;
  let starting = false;
  let fetchingPreflight = false;
  let unloading = false;

  function esc(value) { return String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
  function activeSession() { return !!state?.session && !terminalStatuses.has(state.session.status); }
  function controller() { return activeSession() && state.session.controller_id === clientId; }
  function isVideoStage(stage) { return ["transition", "attention", "relax", "video_buffering", "video_preparing", "video"].includes(stage); }
  function setText(selector, value) { const node = $(selector); if (node) node.textContent = value ?? ""; }
  function notice(message, kind = "error") { const node = $("#message"); node.textContent = message; node.className = `notice ${kind}`; node.hidden = !message; }
  function clearNotice() { notice(""); }
  function prettyDate(value) { if (!value) return "时间未记录"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("zh-CN", {hour12:false}); }
  function pct(value) { return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)}<small>%</small>` : "—"; }
  function array(value) { return Array.isArray(value) ? value : []; }
  function trialCount(session = state?.session) { return array(session?.trials).length; }
  function errorText(error) { return typeof error === "string" ? error : error?.message || error?.error || JSON.stringify(error); }

  async function api(action, payload = {}, options = {}) {
    const response = await fetch(`/api/${action}`, {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({client_id:clientId,...payload}), ...options
    });
    let data;
    try { data = await response.json(); } catch { throw new Error(`本地服务返回异常（HTTP ${response.status}）`); }
    if (data.state) acceptState(data.state);
    if (!response.ok || data.ok === false) throw new Error(errorText(data.error || data.message || `操作失败（HTTP ${response.status}）`));
    return data;
  }

  function acceptState(next) {
    if (!next || typeof next !== "object") return;
    if (typeof next.state_monotonic_ns === "number") {
      if (next.state_monotonic_ns < latestStateTime) return;
      latestStateTime = next.state_monotonic_ns;
    }
    state = next;
    if (state.session?.pending_failure && activeSession()) {
      if (currentVideo && !currentVideo.paused) currentVideo.pause();
      const failureKey = `${state.session.session_id}:${state.session.pending_failure}`;
      if (shownPendingFailure !== failureKey) {
        shownPendingFailure = failureKey;
        notice(`采集已停止：${state.session.pending_failure}。正在处理已有记录。`);
      }
    } else if (shownPendingFailure) { shownPendingFailure = ""; clearNotice(); }
    render();
    if (!probing && !probeComplete && array(state.media_catalog).length && !activeSession()) void probeCatalog();
  }

  function render() {
    if (!state) return;
    const session = state.session;
    const active = activeSession();
    document.body.classList.toggle("experiment-active", active);
    const terminal = session && terminalStatuses.has(session.status) && dismissedSession !== session.session_id;
    const readonly = active && !controller();
    const mode = state.mode === "simulation";
    $("#mode-badge").className = `badge${mode ? " simulation" : ""}`;
    setText("#mode-badge", mode ? "模拟模式 · 非真实 EEG" : "真实设备模式");
    $("#simulation-notice").hidden = !mode;
    $("#identity").hidden = !(active || terminal);
    if (active || terminal) $("#identity").innerHTML = `<strong>被试 ${esc(session.subject_id)}</strong> Round ${esc(session.round)}`;
    const view = readonly ? "readonly" : terminal ? "terminal" : active ? session.status === "saving" ? "saving" : session.stage === "baseline" ? "baseline" : session.stage === "questionnaire" ? "questionnaire" : isVideoStage(session.stage) ? "video" : "transition" : "registration";
    let step = view === "registration" ? "registration" : view === "baseline" ? "baseline" : view === "terminal" || view === "saving" ? "complete" : "trial";
    if (readonly) step = session.stage === "baseline" ? "baseline" : "trial";
    const stepNames = ["registration", "baseline", "trial", "complete"];
    document.querySelectorAll("[data-step]").forEach((node) => { node.className = node.dataset.step === step ? "active" : stepNames.indexOf(node.dataset.step) < stepNames.indexOf(step) ? "done" : ""; });
    const headings = {
      registration:["开始一次新的采集","登记被试与轮次，检查设备与素材后开始。"],
      baseline:["正在进行基线采集","采集已开始，请按照下方引导完成基线。"],
      video:["观看本轮实验视频","请完整观看视频，结束后填写对刚才体验的评分。"],
      questionnaire:["记录刚才的体验","请根据刚刚结束的这段视频作答，两题均需填写。"],
      terminal:[session?.status === "completed" ? "本轮采集已结束" : "本轮采集未完成","采集记录与保存状态如下。"],
      readonly:["采集正在另一页面进行","此页面仅显示状态，不能接管或推进当前实验。"],
      saving:["正在保存与校验数据","请保持页面打开，等待本轮保存结果。"],
      transition:["准备下一个实验步骤","正在等待服务端确认。"]
    };
    setText("#page-title", headings[view][0]);
    setText("#page-description", headings[view][1]);
    const key = `${view}:${active || terminal ? session.session_id : ""}:${view === "video" || view === "questionnaire" ? session.current_trial?.trial_id : ""}`;
    if (key !== renderedKey) {
      detachVideo();
      renderedKey = key;
      if (view === "registration") renderRegistration();
      else if (view === "baseline") renderBaseline();
      else if (view === "video") renderVideo();
      else if (view === "questionnaire") renderQuestionnaire();
      else if (view === "terminal") renderTerminal();
      else renderWaiting(view);
    }
    renderQuality();
    if (view === "registration") updateRegistration();
    if (view === "baseline") updateBaseline();
    if (view === "video") updateVideoStatus();
    if (view === "readonly") setText("#readonly-stage", stageName(session.stage));
    $("#abort-button").hidden = !controller() || session.status === "saving" || !!session.pending_failure;
    if (session?.pending_failure && $("#rating-submit")) $("#rating-submit").disabled = true;
    $("#readonly-note").hidden = !readonly;
  }

  function stageName(stage) { return ({baseline:"30 秒基线",attention:"专心视频播放中",relax:"放松视频播放中",transition:"视频准备中",questionnaire:"填写量表",video_buffering:"视频缓冲中"})[stage] || stage || "等待中"; }

  function renderQuality() {
    const q = state.quality || {};
    const d = state.device || {};
    const map = state.config?.channel_mapping || {};
    const interrupted = ["stale", "interrupted", "no_data", "waiting"].includes(q.data_status) || !connected || !["connected", "simulation"].includes(d.status);
    const recent = q.last_received_age_seconds;
    const hasRecent = typeof recent === "number" && Number.isFinite(recent);
    setText("#device-status", deviceLabels[d.status] || d.status || "等待连接");
    setText("#data-status", dataLabels[q.data_status] || q.data_status || "暂无数据");
    setText("#device-detail", d.detail || "");
    const good = ["connected", "simulation"].includes(d.status) && !interrupted;
    $("#device-icon").className = `status-icon${good ? " good" : ""}`;
    setText("#device-icon", good ? "●" : "○");
    $("#live-dot").className = good ? "live" : "";
    setText("#recording-status", state.session?.pending_failure && activeSession() ? "已停止 · 正在收尾" : activeSession() ? statusLabels[state.session.status] || "正式采集中" : state.session && dismissedSession !== state.session.session_id ? statusLabels[state.session.status] || state.session.status : good ? "预览中 · 未落盘" : "尚未开始");
    const channelName = (index) => {
      if (!map.verified) return `通道 ${index}`;
      const value = map[`channel_${index}`];
      return ({left:"左通道",right:"右通道",左:"左通道",右:"右通道"})[value] || value || `通道 ${index}`;
    };
    setText("#channel-0-label", channelName(0));
    setText("#channel-1-label", channelName(1));
    setText("#mapping-note", `通道饱和率按配置阈值估计 · ${map.verified ? "左右映射已确认" : "左右待确认"}`);
    $("#saturation-0").innerHTML = interrupted ? "—" : pct(q.saturation_0_pct);
    $("#saturation-1").innerHTML = interrupted ? "—" : pct(q.saturation_1_pct);
    $("#loss-rate").innerHTML = interrupted || q.unknown_gap ? "—" : pct(q.loss_pct);
    const win = q.actual_window_seconds;
    setText("#window-label", typeof win === "number" && win > 0 ? `实际窗口 ${win.toFixed(1)} 秒${win < 4.9 ? " · 数据不足" : ""}` : "最近 5 秒滑动窗口 · 暂无数据");
    setText("#last-received", hasRecent ? `最近接收：${recent < 1 ? "不足 1" : recent.toFixed(1)} 秒前` : "暂无有效数据");
    $("#gap-notice").hidden = !q.unknown_gap;
  }

  function renderRegistration() {
    $("#main-content").innerHTML = `<section class="card"><div class="card-body">
      <div class="section-title"><span class="section-number">01 /</span><h2>被试登记</h2></div><p class="subtitle">共 3 轮，每轮 4 个 trial：2 个专心、2 个放松；随机决定首个条件，之后交替。</p>
      <form id="registration-form"><div class="form-grid">
        <label class="field"><span>被试编号<span class="required">*</span></span><input id="subject-id" type="text" required maxlength="80" autocomplete="off" placeholder="例如：001（保留前导零）" value="${esc(draft.subject_id)}"></label>
        <label class="field"><span>轮次 · Round<span class="required">*</span></span><select id="round" required><option value="" disabled ${!draft.round ? "selected" : ""}>请选择</option>${[1,2,3].map((n)=>`<option value="${n}" ${String(draft.round)===String(n)?"selected":""}>第 ${n} 轮</option>`).join("")}</select></label>
      </div><div class="form-actions"><p class="form-hint">登记不会创建数据文件。<br>正式数据从基线开始保存。</p><button id="preflight-button" type="submit" class="button secondary">检查本轮计划 <span aria-hidden="true">→</span></button></div></form>
      <div class="registration-divider"></div><div class="section-title"><span class="section-number">02 /</span><h2>本轮计划与准备</h2></div><p class="subtitle">30 秒基线 → 视频与量表 × 4 → 保存校验</p><p id="plan-rule" class="subtitle">同一被试本轮的顺序确定后保持不变，重复检查或重新采集不会重新随机。</p><div id="trial-plan" class="empty-plan">完成上方登记后，查看本轮视频与顺序。</div>
      <div id="previous-attempts"></div><div class="checklist"><div id="media-check" class="check-item pending"></div><div id="device-check" class="check-item pending"></div></div><div style="margin-top:15px"><button class="button secondary" id="connect-button">连接设备并预览</button></div><div id="readiness-errors"></div>
      <label class="checkbox-row"><input type="checkbox" id="audio-confirmed" ${draft.audio_confirmed ? "checked" : ""}><span>已确认电脑的声音输出、音量及视频原有音轨可听见。</span></label>
    </div><div class="start-footer"><p>开始后连续保存两路原始 EEG。<br>请保持页面打开，直至采集结束。</p><button id="start-button" class="button" disabled>开始 30 秒基线采集 <span aria-hidden="true">→</span></button></div></section>
    <details class="commissioning" id="commissioning"><summary>实验负责人 · 设备联调与参数核对</summary><div class="commissioning-body"><p>连接后如未收到数据，可点击“发送小写 b”。正常接收后，点击上方“开始 30 秒基线采集”。左右耳未知时仍按通道 0/1 保存；参数确认状态会随数据记录，不影响原始数据采集。参数可在 <span class="code-path">config/settings.json</span> 中核对，以下命令只在未采集时可用。</p><div class="command-row"><button class="button quiet" data-command="b">发送小写 b</button><button class="button quiet" data-command="S">发送 S</button><button class="button quiet" data-command="R">发送 R</button><button class="button quiet" data-command="I">发送 I</button></div><p style="margin:12px 0 0">以上字节来自参考脚本；发送命令不会自动修改参数确认状态。不要同时运行原始蓝牙脚本。</p></div></details>`;
    $("#registration-form").addEventListener("submit", (event) => { event.preventDefault(); void runPreflight(); });
    ["#subject-id", "#round"].forEach((selector) => $(selector).addEventListener("input", () => {
      draft.subject_id = $("#subject-id").value; draft.round = $("#round").value;
      preflight = null; draft.confirm_retry = false; startRequestId = crypto.randomUUID(); clearNotice(); updateRegistration();
    }));
    $("#audio-confirmed").addEventListener("change", (event) => { draft.audio_confirmed = event.target.checked; updateRegistration(); });
    $("#start-button").addEventListener("click", () => void startSession());
    $("#connect-button").addEventListener("click", async (event) => {
      const button = event.currentTarget; button.disabled = true; clearNotice();
      try { await api("connect"); } catch (error) { notice(error.message); } finally { if (button.isConnected) button.disabled = false; }
    });
    document.querySelectorAll("[data-command]").forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true; clearNotice();
      try { await api("command", {command:button.dataset.command}); notice(button.dataset.command === "b" ? "已发送小写 b；显示正常接收并完成准备后，请点击“开始 30 秒基线采集”。" : `已发送命令 ${button.dataset.command}，请观察设备数据。`, ""); }
      catch (error) { notice(error.message); } finally { if (button.isConnected) button.disabled = false; }
    }));
  }

  function checkMarkup(ok, text) { return `<span class="check-icon" aria-hidden="true">${ok ? "✓" : "○"}</span><span>${esc(text)}</span>`; }
  function updateRegistration() {
    if (!$("#start-button")) return;
    const plan = array(preflight?.plan);
    const firstCondition = preflight?.randomization?.first_condition;
    setText("#plan-rule", firstCondition && conditionLabels[firstCondition] ? `本次从${conditionLabels[firstCondition]}开始，之后交替；同一被试本轮重复检查或重新采集均沿用此顺序。` : "同一被试本轮的顺序确定后保持不变，重复检查或重新采集不会重新随机。");
    const planNode = $("#trial-plan");
    if (plan.length) {
      planNode.className = "plan";
      planNode.innerHTML = plan.map((trial,index) => `<div class="plan-item ${trial.condition === "relax" ? "relax" : ""}"><span class="plan-index">${index+1}</span><div><h3>${esc(conditionLabels[trial.condition] || trial.condition)} → 量表 ${index+1}</h3><p>${esc(trial.video?.path || trial.video?.video_id)}</p>${Number.isFinite(trial.video?.duration_seconds) ? `<small>完整播放 · ${formatDuration(trial.video.duration_seconds)}</small>` : "<small>完整播放 · 时长由浏览器读取</small>"}</div></div>`).join("");
    } else { planNode.className = "empty-plan"; planNode.textContent = "完成上方登记后，查看本轮视频与顺序。"; }
    const attempts = array(preflight?.previous_attempts);
    const completed = attempts.filter((a) => a.status === "completed");
    const attemptsNode = $("#previous-attempts");
    if (attempts.length) {
      const key = attempts.map(a=>`${a.session_id}:${a.status}`).join("|");
      if (attemptsNode.dataset.key !== key) {
        attemptsNode.dataset.key = key;
        attemptsNode.innerHTML = `<div class="attempts">此被试本轮已有 ${attempts.length} 次记录：<ul>${attempts.map((a)=>`<li>${esc(prettyDate(a.started_at_utc))} · ${esc(statusLabels[a.status] || a.status)}</li>`).join("")}</ul></div>${completed.length ? `<label class="checkbox-row"><input type="checkbox" id="confirm-retry" ${draft.confirm_retry ? "checked" : ""}><span>已知本轮有完成记录，确认重采并保留之前的全部记录。</span></label>` : ""}`;
        $("#confirm-retry")?.addEventListener("change", (event) => { draft.confirm_retry = event.target.checked; updateRegistration(); });
      }
    } else { attemptsNode.innerHTML = ""; delete attemptsNode.dataset.key; }
    const mediaOk = probeComplete && probeErrors.length === 0;
    $("#media-check").className = `check-item ${mediaOk ? "ok" : "pending"}`;
    $("#media-check").innerHTML = checkMarkup(mediaOk, mediaOk ? `完整视频清单已检查 · ${probeTotal} 个视频可读取时长及首帧` : probing ? `正在检查视频兼容性 ${probeDone} / ${probeTotal}…` : probeErrors.length ? "视频检查未通过，请查看下方文件提示" : "等待视频清单检查");
    const errors = [...array(state.readiness_errors),...array(preflight?.errors),...array(state.preflight_errors),...probeErrors].map(errorText);
    const uniqueErrors = [...new Set(errors)];
    const deviceOk = !array(state.readiness_errors).length && ["normal", "receiving", "healthy", "insufficient", "insufficient_data"].includes(state.quality?.data_status);
    $("#device-check").className = `check-item ${deviceOk ? "ok" : "pending"}`;
    $("#device-check").innerHTML = checkMarkup(deviceOk, deviceOk ? "设备与记录器已就绪，预览数据不写入正式文件" : "等待设备、近期有效 EEG 与正式采集配置检查");
    $("#readiness-errors").innerHTML = uniqueErrors.length ? `<div class="notice caution compact"><strong>开始前需完成</strong><ul>${uniqueErrors.map((error)=>`<li>${esc(error)}</li>`).join("")}</ul></div>` : "";
    $("#start-button").disabled = starting || fetchingPreflight || !connected || !preflight?.plan_id || !plan.length || uniqueErrors.length > 0 || !mediaOk || !draft.audio_confirmed || (completed.length > 0 && !draft.confirm_retry);
    $("#start-button").textContent = starting ? "正在建立采集记录…" : "开始 30 秒基线采集 →";
    $("#preflight-button").disabled = fetchingPreflight || starting;
    $("#subject-id").disabled = starting;
    $("#round").disabled = starting;
  }

  function formatDuration(value) { const seconds = Math.round(value); return `${Math.floor(seconds/60)} 分 ${String(seconds%60).padStart(2,"0")} 秒`; }

  async function runPreflight() {
    if (fetchingPreflight) return;
    const form = $("#registration-form");
    if (!form?.reportValidity()) return;
    draft.subject_id = $("#subject-id").value.trim(); draft.round = $("#round").value;
    if (!draft.subject_id) { notice("请填写被试编号。"); return; }
    fetchingPreflight = true; clearNotice(); updateRegistration();
    const fingerprint = `${draft.subject_id}|${draft.round}`;
    try {
      const result = await api("preflight", {subject_id:draft.subject_id,round:Number(draft.round)});
      if (fingerprint !== `${draft.subject_id}|${draft.round}`) return;
      preflight = result; preloadRound(array(result.plan)); startRequestId = crypto.randomUUID();
    } catch (error) { preflight = null; notice(error.message); }
    finally { fetchingPreflight = false; updateRegistration(); }
  }

  async function synchronizeClock() {
    if (syncBusy) return;
    syncBusy = true;
    try {
      const before = performance.now();
      const response = await fetch("/api/clock", {cache:"no-store"});
      if (!response.ok) throw new Error("本地时钟同步失败");
      const clock = await response.json();
      const after = performance.now();
      await api("sync", {client_midpoint_ms:(before+after)/2,server_monotonic_ns:clock.server_monotonic_ns,rtt_ms:after-before});
    } finally { syncBusy = false; }
  }

  async function startSession() {
    if (starting || $("#start-button")?.disabled) return;
    const payload = {subject_id:draft.subject_id,round:Number(draft.round),plan_id:preflight.plan_id,request_id:startRequestId,confirm_retry:draft.confirm_retry,audio_confirmed:draft.audio_confirmed};
    starting = true; clearNotice(); updateRegistration();
    try {
      await synchronizeClock();
      await api("start", payload);
      dismissedSession = null;
    } catch (error) { notice(error.message); }
    finally { starting = false; if (!activeSession()) updateRegistration(); }
  }

  function renderBaseline() {
    const instruction = state.config?.baseline_instruction || "基线引导尚未配置，请联系实验负责人。";
    $("#main-content").innerHTML = `<section class="card phase-card"><div class="phase-header"><div><h2>基线采集中</h2><p class="subtitle">本轮仅进行一次起始基线</p></div><span class="phase-badge">正在连续记录</span></div><div class="baseline-body"><div class="baseline-caption">BASELINE / 剩余时间</div><div class="countdown"><span id="baseline-seconds">30</span><small>秒</small></div><p class="baseline-instruction">${esc(instruction)}</p><p class="phase-footnote">计时结束后将自动准备第一个视频，请保持此页面打开。</p></div><div class="baseline-progress"><span id="baseline-progress"></span></div></section>`;
  }
  function updateBaseline() {
    const duration = Number(state.config?.baseline_seconds) || 30;
    const remaining = Math.max(0,Number(state.session.baseline_remaining_seconds) || 0);
    setText("#baseline-seconds", Math.ceil(remaining));
    const bar = $("#baseline-progress"); if (bar) bar.style.width = `${Math.max(0, Math.min(100, (duration-remaining)/duration*100))}%`;
  }

  function detachVideo() {
    if (!currentVideo) return;
    suppressMedia = true;
    currentVideo.pause();
    currentVideo.removeAttribute("src");
    currentVideo.load();
    currentVideo = null;
    currentVideoTrial = null;
    suppressMedia = false;
  }

  function renderVideo() {
    const trial = state.session.current_trial;
    if (!trial?.video?.url) { renderWaiting("transition"); notice("当前 trial 缺少有效视频地址，已停止页面播放。请检查服务端状态。"); return; }
    $("#main-content").innerHTML = `<section class="card video-card"><div class="phase-header"><div><h2>${esc(conditionLabels[trial.condition] || "实验视频")}</h2><p class="subtitle">${esc(trial.video.path || trial.video.video_id)} · 按原始速度完整播放</p></div><span class="phase-badge">Trial ${esc(trial.trial_order)} / ${trialCount()}</span></div><div class="video-stage"><video id="experiment-video" preload="auto" playsinline disablepictureinpicture disableremoteplayback tabindex="-1" aria-label="当前实验视频"></video><div id="video-overlay" class="video-overlay"><p id="video-overlay-text">正在准备视频…</p><button id="play-button" class="button" hidden>点击开始播放</button></div></div><div class="video-footer"><span id="play-status" class="playing-label">等待实际播放</span><span>播放结束后进入本段视频的量表</span></div></section>`;
    const video = $("#experiment-video");
    currentVideo = video; currentVideoTrial = trial.trial_id;
    const identity = {session_id:state.session.session_id,trial_id:trial.trial_id};
    video.controls = false; video.loop = false; video.muted = false; video.playbackRate = 1;
    video.addEventListener("contextmenu", (event) => event.preventDefault());
    video.addEventListener("ratechange", () => { if (video.playbackRate !== 1) video.playbackRate = 1; });
    for (const eventType of ["playing", "waiting", "pause", "ended", "error"]) {
      video.addEventListener(eventType, () => {
        if (suppressMedia || currentVideo !== video || currentVideoTrial !== trial.trial_id || !controller() || state.session.pending_failure || state.session.session_id !== identity.session_id || state.session.stage === "questionnaire" || terminalStatuses.has(state.session.status)) return;
        if (eventType === "error") { if (reportedMediaErrors.has(video)) return; reportedMediaErrors.add(video); }
        const payload = {...identity,event_type:eventType,client_event_id:crypto.randomUUID(),client_performance_ms:performance.now(),media_position_ms:Number.isFinite(video.currentTime)?video.currentTime*1000:0,playback_rate:video.playbackRate};
        if (eventType === "error" && video.error) payload.message = `媒体错误 ${video.error.code}：${video.error.message || "浏览器无法解码或读取视频"}`;
        if (eventType === "playing") { $("#video-overlay").hidden = true; setText("#play-status","视频播放中 · 原有音轨已保留"); }
        if (eventType === "waiting") { $("#video-overlay").hidden = false; setText("#video-overlay-text","视频缓冲中，请稍候…"); $("#play-button").hidden = true; }
        if (eventType === "error") { $("#video-overlay").hidden = false; setText("#video-overlay-text","视频无法播放，正在记录中断原因。"); $("#play-button").hidden = true; }
        mediaQueue = mediaQueue.then(() => api("media",payload)).catch((error) => { notice(`媒体事件保存失败：${error.message}`); if (currentVideo === video && !video.paused) video.pause(); });
      });
    }
    $("#play-button").addEventListener("click", () => void tryPlay(video));
    video.src = trial.video.url;
    video.load();
    void (async () => {
      try { await synchronizeClock(); if (currentVideo === video && controller()) await tryPlay(video); }
      catch (error) { notice(`视频开始前的时钟同步失败：${error.message}`); reportPlaybackFailure(video,`视频开始前的时钟同步失败：${error.message}`); }
    })();
  }

  async function tryPlay(video) {
    if (video !== currentVideo || !controller() || state.session.pending_failure) return;
    $("#play-button").hidden = true;
    try { await video.play(); }
    catch (error) {
      if (video !== currentVideo) return;
      $("#video-overlay").hidden = false;
      if (error.name === "NotAllowedError") { setText("#video-overlay-text","浏览器需要一次点击来开始有声播放。"); $("#play-button").hidden = false; }
      else if (error.name !== "AbortError") { setText("#video-overlay-text",`播放失败：${error.message}`); notice("视频未能开始播放，正在记录中断原因。"); reportPlaybackFailure(video,`${error.name}：${error.message}`); }
    }
  }

  function reportPlaybackFailure(video, message) {
    if (currentVideo !== video || !controller() || state.session.pending_failure || reportedMediaErrors.has(video)) return;
    reportedMediaErrors.add(video);
    const payload = {session_id:state.session.session_id,trial_id:currentVideoTrial,event_type:"error",client_event_id:crypto.randomUUID(),client_performance_ms:performance.now(),media_position_ms:Number.isFinite(video.currentTime)?video.currentTime*1000:0,playback_rate:video.playbackRate,message};
    mediaQueue = mediaQueue.then(()=>api("media",payload)).catch((error)=>notice(`无法登记视频中断：${error.message}`));
  }

  function updateVideoStatus() {
    if (!$("#play-status")) return;
    if (state.session.pending_failure) {
      setText("#play-status", "采集已停止 · 正在处理已有记录");
      $("#video-overlay").hidden = false;
      setText("#video-overlay-text", `采集已停止：${state.session.pending_failure}`);
      $("#play-button").hidden = true;
      return;
    }
    const stage = state.session.stage;
    if (stage === "video_buffering") setText("#play-status", "视频缓冲中 · 当前区间单独记录");
    else if (["attention","relax"].includes(stage)) setText("#play-status", "视频播放中 · 原有音轨已保留");
    else setText("#play-status", "过渡阶段 · 等待实际播放");
  }

  function questionCopy(condition) {
    const cfg = state.config?.questionnaires?.[condition] || {};
    const word = condition === "attention" ? "专心" : "放松";
    const anchor = (value, number) => new RegExp(`^${number}`).test(value) ? value : `${number}＝${value}`;
    return {question:cfg.question || cfg.score_question || `你刚刚的${word}程度是多少？`,confidence:cfg.confidence_question || `你对刚刚${word}程度评分的确信度是多少？`,low:anchor(cfg.low_label || `非常不${word}`,1),high:anchor(cfg.high_label || `非常${word}`,5),confidenceLow:anchor(cfg.confidence_low_label || "非常不确信",1),confidenceHigh:anchor(cfg.confidence_high_label || "非常确信",5)};
  }
  function radioGroup(name) { return `<div class="rating-options">${[1,2,3,4,5].map((n)=>`<label class="rating-option"><input type="radio" name="${name}" value="${n}" required><span>${n}</span></label>`).join("")}</div>`; }
  function renderQuestionnaire() {
    const trial = state.session.current_trial;
    if (!trial) { renderWaiting("transition"); return; }
    const copy = questionCopy(trial.condition);
    ratingRequestId = crypto.randomUUID();
    $("#main-content").innerHTML = `<section class="card phase-card"><div class="phase-header"><div><h2>${trial.condition === "attention" ? "专心" : "放松"}体验量表</h2><p class="subtitle">仅评价刚刚结束的 ${esc(trial.video?.video_id || "这段视频")}</p></div><span class="phase-badge">量表 ${esc(trial.trial_order)} / ${trialCount()}</span></div><form id="rating-form" class="questionnaire-body"><fieldset class="question-block" style="border:0;margin:0;padding:0"><legend class="question-title"><span>01</span>${esc(copy.question)}</legend>${radioGroup("score")}<div class="rating-anchors"><span>${esc(copy.low)}</span><span>${esc(copy.high)}</span></div></fieldset><fieldset class="question-block" style="border:0;padding:0;margin-left:0;margin-right:0;margin-bottom:0"><legend class="question-title"><span>02</span>${esc(copy.confidence)}</legend>${radioGroup("confidence")}<div class="rating-anchors"><span>${esc(copy.confidenceLow)}</span><span>${esc(copy.confidenceHigh)}</span></div></fieldset><div class="questionnaire-submit"><p>本阶段仍在连续采集 EEG。<br>两题均选择后才可提交。</p><button id="rating-submit" type="submit" class="button" disabled>${Number(trial.trial_order) === trialCount() ? "提交并完成本轮" : "提交并进入下一段"} →</button></div></form></section>`;
    const form = $("#rating-form");
    form.addEventListener("change", () => { $("#rating-submit").disabled = !!state.session?.pending_failure || !(form.querySelector('[name="score"]:checked') && form.querySelector('[name="confidence"]:checked')); });
    let submitting = false;
    const sessionId = state.session.session_id;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submitting || !controller() || state.session.pending_failure || !form.reportValidity()) return;
      submitting = true; const button = $("#rating-submit"); button.disabled = true; clearNotice();
      try { await api("rating",{session_id:sessionId,trial_id:trial.trial_id,score:Number(new FormData(form).get("score")),confidence_score:Number(new FormData(form).get("confidence")),request_id:ratingRequestId}); }
      catch (error) { notice(error.message); submitting = false; if (button.isConnected) button.disabled = false; }
    });
  }

  function renderTerminal() {
    const session = state.session;
    const completed = session.status === "completed";
    const title = completed ? "本轮采集完成，数据已保存" : session.status === "save_failed" ? "保存失败，请检查已有文件" : session.status === "aborted" ? "本轮已主动中止" : "本轮采集已中断";
    const files = array(session.files).length ? session.files.map((file)=>typeof file === "string" ? file.split(/[\\/]/).pop() : file.name || file.filename || "") : ["eeg_raw.csv","labels.csv","config.json"];
    const descriptions = {"eeg_raw.csv":"两路未经滤波的原始 EEG", "labels.csv":"阶段、媒体事件与量表", "config.json":"本次采集配置与结束摘要"};
    $("#main-content").innerHTML = `<section class="card"><div class="terminal-body"><div class="terminal-symbol ${completed ? "" : "error"}" aria-hidden="true">${completed ? "✓" : "!"}</div><h2>${esc(title)}</h2><p class="terminal-description">${completed ? `本轮 ${trialCount(session)} 个 trial 与 ${trialCount(session)} 份量表已完成，三文件保存及收尾校验成功。` : esc(session.reason || "实验未完整结束。已写出的数据将保留；再次尝试会创建独立记录。")}</p><div class="result-meta"><span>被试 <strong>${esc(session.subject_id)}</strong></span><span>Round ${esc(session.round)}</span><span>${esc(statusLabels[session.status])}</span></div><p class="subtitle" style="margin-bottom:8px">本轮保存目录</p><div class="output-path">${esc(session.output_directory || "目录信息尚不可用")}</div><ul class="file-list">${files.map((file)=>`<li><strong>${esc(file)}</strong><span>${esc(descriptions[file] || "采集数据文件")}</span></li>`).join("")}</ul>${!completed ? `<div class="notice caution">${session.status === "save_failed" ? "写盘失败时无法保证三文件完整，请保留现有文件并检查磁盘或服务日志。" : "这次记录不能视为完整实验。重采需要重新进行 30 秒基线，之前的文件不会覆盖。"}</div>` : ""}<div class="terminal-actions"><button id="new-registration" class="button">登记下一轮 →</button><button id="retry-registration" class="button secondary">重新采集本轮</button></div></div></section>`;
    $("#new-registration").addEventListener("click", () => {
      dismissedSession = session.session_id; preflight = null; draft = {subject_id:session.subject_id,round:"",audio_confirmed:false,confirm_retry:false}; startRequestId = crypto.randomUUID(); clearNotice(); render();
    });
    $("#retry-registration").addEventListener("click", () => {
      dismissedSession = session.session_id; preflight = null; draft = {subject_id:session.subject_id,round:String(session.round),audio_confirmed:false,confirm_retry:false}; startRequestId = crypto.randomUUID(); clearNotice(); render(); void runPreflight();
    });
  }

  function renderWaiting(view) {
    $("#main-content").innerHTML = `<section class="card phase-card"><div class="empty-state"><h2>${view === "readonly" ? "当前页面为只读状态" : view === "saving" ? "正在保存与校验" : "正在准备实验步骤"}</h2><p>${view === "readonly" ? "请在启动本轮实验的原页面继续操作。" : "请保持页面打开，等待服务端确认。"}</p>${view === "readonly" ? '<p id="readonly-stage" style="margin-top:20px"></p>' : ""}</div></section>`;
  }

  async function probeOne(videoInfo) {
    return new Promise((resolve) => {
      const video = document.createElement("video");
      video.preload = "auto"; video.muted = true; video.playsInline = true;
      let finished = false;
      const finish = (playable) => {
        if (finished) return; finished = true; clearTimeout(timeout);
        const duration = video.duration;
        video.removeAttribute("src"); video.load(); video.remove();
        resolve({video_id:videoInfo.video_id,duration_seconds:Number.isFinite(duration)?duration:null,playable:playable && Number.isFinite(duration) && duration>0});
      };
      const timeout = setTimeout(()=>finish(false),20000);
      video.addEventListener("loadeddata",()=>finish(true),{once:true});
      video.addEventListener("error",()=>finish(false),{once:true});
      $("#preload-bin").append(video); video.src = videoInfo.url; video.load();
    });
  }

  async function probeCatalog() {
    const catalog = array(state?.media_catalog);
    if (!catalog.length || probing || probeComplete || activeSession()) return;
    probing = true; probeDone = 0; probeTotal = catalog.length; probeErrors = [];
    const results = [];
    for (const videoInfo of catalog) {
      const result = await probeOne(videoInfo); results.push(result); probeDone++;
      if (!result.playable) probeErrors.push(`浏览器无法解码视频或读取有效时长：${videoInfo.path || videoInfo.video_id}`);
      updateRegistration();
    }
    try { await api("browser_probe", {videos:results}); }
    catch (error) { probeErrors.push(`视频检查结果未能登记：${error.message}`); }
    probing = false; probeComplete = true; updateRegistration();
  }

  function preloadRound(plan) {
    document.querySelectorAll("[data-round-preload]").forEach((node)=>{node.removeAttribute("src");node.load();node.remove();});
    for (const trial of plan) {
      if (!trial.video?.url) continue;
      const video = document.createElement("video"); video.dataset.roundPreload = "true"; video.preload = "auto"; video.src = trial.video.url;
      $("#preload-bin").append(video); video.load();
    }
  }

  function setConnection(ok) {
    connected = ok;
    $("#connection-warning").hidden = ok;
    if (!ok && currentVideo && !currentVideo.paused) currentVideo.pause();
    render();
  }
  function connectSocket() {
    if (unloading) return;
    socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws?client_id=${encodeURIComponent(clientId)}`);
    socket.addEventListener("open", () => { setConnection(true); clearTimeout(reconnectTimer); });
    socket.addEventListener("message", (event) => {
      try { const data = JSON.parse(event.data); if (data.type === "state") acceptState(data.state); }
      catch { notice("收到无法解析的状态数据，请检查本地服务版本。"); }
    });
    socket.addEventListener("close", () => { setConnection(false); if (!unloading) reconnectTimer = setTimeout(connectSocket,1500); });
    socket.addEventListener("error", () => setConnection(false));
  }

  $("#abort-button").addEventListener("click",()=>$("#abort-dialog").showModal());
  $("#cancel-abort").addEventListener("click",()=>$("#abort-dialog").close());
  $("#confirm-abort").addEventListener("click",async()=>{
    const button = $("#confirm-abort"); button.disabled = true;
    try { if (controller()) await api("abort",{session_id:state.session.session_id}); $("#abort-dialog").close(); }
    catch (error) { notice(error.message); $("#abort-dialog").close(); }
    finally { button.disabled = false; }
  });
  window.addEventListener("pagehide", () => {
    unloading = true;
    if (controller()) {
      const payload = JSON.stringify({client_id:clientId,session_id:state.session.session_id});
      const blob = new Blob([payload],{type:"application/json"});
      if (!navigator.sendBeacon("/api/unload",blob)) void fetch("/api/unload",{method:"POST",headers:{"Content-Type":"application/json"},body:payload,keepalive:true});
    }
    socket?.close();
  });
  window.addEventListener("pageshow",(event)=>{if(event.persisted)location.reload();});
  setInterval(async()=>{
    if (!controller() || heartbeatBusy || unloading) return;
    heartbeatBusy = true;
    try { await api("heartbeat",{session_id:state.session.session_id}); }
    catch (error) { notice(`采集页面心跳失败：${error.message}`); }
    finally { heartbeatBusy = false; }
  },750);
  setInterval(()=>{if(controller())void synchronizeClock().catch((error)=>notice(error.message));},30000);
  void (async()=>{
    try { const response = await fetch("/api/state",{cache:"no-store"}); if(!response.ok)throw new Error("无法读取本地服务状态"); acceptState(await response.json()); }
    catch(error) { notice(error.message); }
    connectSocket();
  })();
})();

/* 사적 입력과 원문은 로컬 API에서만 처리한다. 화면은 현재 게시가 허용된 일정만 소비한다. */
(() => {
  const $ = id => document.getElementById(id);
  const state = {token: "", season: null, seasons: [], rows: [], conversation: null,
    jobs: [], jobSignature: null, contentRevision: null, researchSummary: null, rosterReady: false, pendingRegistry: false, recordPending: false, activeJobId: null, pendingTime: new Set(), pendingActions: new Set(), timeErrors: new Map(), stopped: false};
  function message(text) { $("message").textContent = text; $("message").hidden = !text; }
  async function api(path, {method = "GET", body} = {}) {
    const headers = {};
    if (method !== "GET") { headers["X-App-Token"] = state.token; headers["Idempotency-Key"] = crypto.randomUUID(); }
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || "요청을 처리하지 못했어요. 다시 시도해 주세요.");
    return result;
  }
  async function action(button, work) {
    state.pendingActions.add(button); button.disabled = true; message("");
    try { await work(); } catch (error) { message(error.message); }
    finally { state.pendingActions.delete(button); button.disabled = false; renderTimeStates(); }
  }
  function button(text, click) {
    const node = document.createElement("button"); node.type = "button"; node.textContent = text; node.onclick = click; return node;
  }
  const activeStatuses = new Set(["queued", "running", "pausing", "cancelling"]);
  function timeKey(row) { return `${state.season}:${row.event_ids?.length === 1 ? row.event_ids[0] : ""}`; }
  function timeJob(row) {
    const event = row.event_ids?.length === 1 ? row.event_ids[0] : null;
    if (!event) return null;
    // 서버 투영은 오래된 작업도 복원하며, 방금 수락된 작업은 다음 결과 조회 전에 연결한다.
    const listed = state.jobs.find(job => job.season_id === state.season &&
      job.scope_snapshot?.purpose === "time" && job.scope_snapshot.target_event_id === event);
    const saved = row.time_search;
    if (!saved) return listed;
    if (!listed || saved.job_id === listed.id) return {...listed, ...saved, id:saved.job_id};
    return listed.created_at >= (saved.created_at || "") ? listed : {...saved, id:saved.job_id};
  }
  function renderTimeStates() {
    const active = state.jobs.find(job => activeStatuses.has(job.status));
    const busy = Boolean(active || state.activeJobId || state.pendingTime.size || state.pendingRegistry);
    $("results").querySelectorAll("tbody tr").forEach(tr => {
      const row = tr.resultRow, cell = tr.children[2];
      if (!row) return;
      const key = timeKey(row), pending = state.pendingTime.has(key), job = timeJob(row);
      const signature = JSON.stringify([row.start_time, row.status, row.date, key, busy, pending,
        state.timeErrors.get(key), job?.id, job?.revision, job?.status, job?.outcome, job?.counts?.failed, job?.feedback]);
      // 변하지 않은 버튼을 다시 만들지 않아 폴링 중 키보드 포커스를 보존한다.
      if (cell.timeSignature === signature) return;
      cell.timeSignature = signature; cell.replaceChildren();
      const running = job && activeStatuses.has(job.status);
      // 중간 저장값이 생겨도 실행 중이거나 멈춘 미완료 작업은 해당 칸의 안내를 유지한다.
      // 종료된 과거 작업은 이후 직접 확인한 정상 시간을 가리지 않는다.
      if (row.start_time && !running && !pending && job?.status !== "paused") { cell.textContent = row.start_time; return; }
      if (row.status) return;
      const area = document.createElement("div"); area.className = "time-research";
      area.dataset.state = pending ? "requesting" : (job?.status || "idle");
      const labels = {queued:"찾기 준비 중…", running:"찾는 중…", pausing:"잠시 멈추는 중…",
        cancelling:"취소하는 중…", paused:"잠시 멈췄어요.", cancelled:"취소했어요.", failed:"찾기를 마치지 못했어요."};
      let text = pending ? "요청 중…" : (state.timeErrors.get(key) || labels[job?.status]);
      if (!state.timeErrors.has(key) && ["completed", "failed"].includes(job?.status)) text = window.scheduleTimeFeedback(job);
      if (text) {
        const notice = document.createElement("span"); notice.className = "time-notice";
        notice.setAttribute("role", "status"); notice.setAttribute("aria-live", "polite");
        if (pending || running) { const spinner = document.createElement("span"); spinner.className = "time-spinner"; spinner.setAttribute("aria-hidden", "true"); notice.append(spinner); }
        notice.append(document.createTextNode(text)); area.append(notice);
      }
      if (!pending && !running) {
        const resumable = job?.status === "paused" && !["job_budget", "configuration_required"].includes(job.paused_reason);
        const find = button(resumable ? "이어서 찾기" : (job ? "다시 찾기" : "시간 찾기"), async () => {
          if (state.pendingTime.has(key)) return;
          state.pendingTime.add(key); state.timeErrors.delete(key); renderTimeStates(); message("");
          try {
            if (resumable) { await api(`/api/jobs/${job.id}/resume`, {method:"POST",body:{expected_revision:job.revision,increase_limits:null}}); await jobs(); }
            else await startJob({purpose:"time", eventId:row.event_ids[0], institutionId:row.institution_id});
          } catch(error) { state.timeErrors.set(key, "요청을 보내지 못했어요."); message(error.message); }
          finally { state.pendingTime.delete(key); renderTimeStates(); }
        });
        find.className = "find-time";
        const eligible = Boolean(row.date && row.event_ids?.length === 1);
        find.disabled = busy || !eligible;
        if (!eligible) find.title = "일정찾기로 졸업식 날짜를 먼저 확인해 주세요.";
        else if (busy) find.title = "진행 중인 조사가 끝나면 찾을 수 있어요.";
        area.append(find);
      }
      cell.append(area);
    });
    const hasSeason = Boolean(state.season);
    $("season-bar").hidden = !hasSeason;
    $("season-select").disabled = !hasSeason;
    $("roster-guide").hidden = state.rosterReady;
    $("season-guide").hidden = hasSeason || !state.rosterReady;
    $("research-submit").hidden = !hasSeason;
    $("research-submit").disabled = busy || !state.rosterReady || !hasSeason;
    $("registry-sync").disabled = busy;
    $("load-roster").disabled = busy;
    $("load-roster").textContent = state.pendingRegistry || active?.kind === "registry_sync" ?
      "학교 목록 불러오는 중…" : "학교 목록 불러오기";
    $("add-season").disabled = busy || !state.rosterReady;
    $("add-season").textContent = hasSeason ? "새 시즌 조사" : "조사 기간 선택";
    $("add-season").classList.toggle("primary", !hasSeason);
    $("season-submit").disabled = busy || !state.rosterReady;
    $("record-guide").hidden = hasSeason;
    ["record-text", "record-submit", "new-record"].forEach(id => {
      $(id).disabled = !hasSeason || state.recordPending;
    });
    ["open-html", "save-html"].forEach(id => { $(id).disabled = !hasSeason; });
    $("results").hidden = !hasSeason;
    $("results-guide").hidden = hasSeason;
    // 폴링이 새로 계산한 준비 상태로 아직 응답 중인 버튼을 풀지 않게 한다.
    state.pendingActions.forEach(node => { node.disabled = true; });
  }
  function htmlSaveStatus(saved) {
    $("html-save-message").textContent = saved?.status === "failed" ? saved.message || "일정표 파일을 저장하지 못했어요." : "";
    $("html-save-message").hidden = saved?.status !== "failed";
  }
  async function results() {
    const season = state.season;
    // 시즌을 임의로 만들지 않고 명부 준비 상태만 조회한다.
    if (!season) {
      const status = await api("/api/status");
      if (state.season) return;
      state.rows = []; state.researchSummary = null;
      state.rosterReady = status.eligible_institution_count > 0;
      state.activeJobId = status.active_job_id || null;
      $("results").querySelector("tbody").replaceChildren();
      $("research-summary").hidden = true; htmlSaveStatus(null); renderTimeStates();
      return;
    }
    const result = await api(`/api/results?season_id=${season}`);
    if (season !== state.season) return;
    state.rows = result.rows;
    state.rosterReady = result.eligible_institution_count > 0;
    $("roster-guide").hidden = state.rosterReady;
    htmlSaveStatus(result.html_save);
    state.researchSummary = result.research_summary || null;
    $("research-summary").textContent = state.researchSummary?.message || "";
    $("research-summary").hidden = !state.researchSummary?.message || !state.rosterReady;
    state.activeJobId = result.active_job_id || null;
    const labels = ["날짜", "학교명", "시간", "졸업인원수", "장소"];
    const rows = result.rows.map(row => {
      const dates = row.dates || (row.date ? [row.date] : []);
      const tr = document.createElement("tr"); tr.resultRow = row;
      tr.dataset.eventId = row.event_ids?.length === 1 ? row.event_ids[0] : "";
      tr.dataset.name = row.name; tr.dataset.date = row.date || "";
      tr.dataset.dates = dates.join(" "); tr.dataset.district = row.district || "";
      [dates.join("\n"), row.name, row.start_time || "", row.graduation_count ?? "", row.address || ""].forEach((value, i) => {
        const td = document.createElement("td"); td.dataset.label = labels[i];
        if (i === 0) td.className = "date-list";
        if (i === 1 && row.url) { const link = document.createElement("a"); link.textContent = value; link.href = row.url; link.target = "_blank"; link.rel = "noopener noreferrer"; td.append(link); }
        else td.textContent = value;
        if (i === 1 && row.event_label) { const label = document.createElement("span"); label.className = "event-label"; label.textContent = row.event_label; td.append(label); }
        if (i === 1 && row.status) { const label = document.createElement("span"); label.className = "cancelled"; label.textContent = row.status === "cancelled" ? " · 취소" : " · 미실시"; td.append(label); }
        tr.append(td);
      }); return tr;
    });
    $("results").querySelector("tbody").replaceChildren(...rows);
    window.setupSchedule($("results")); renderTimeStates();
  }
  async function startJob({kind = "schedule_scan", purpose = "date_count", eventId = null, institutionId = null} = {}) {
    if (kind === "schedule_scan" && !state.rosterReady)
      throw new Error("조사할 학교 목록이 없어요. 먼저 학교 목록을 불러와 주세요.");
    if (kind === "schedule_scan" && !state.season)
      throw new Error("조사할 기간을 선택해 주세요.");
    // 목록 확보와 시즌 조사는 서로 다른 요청 계약을 사용한다.
    const body = kind === "registry_sync" ? {kind, providers:["neis", "higher_education"], limits:null} :
      {kind, season_id:state.season, mode:"recheck", limits:null, purpose, target_event_id:eventId,
        selection:institutionId ? {scope:"ids",institution_ids:[institutionId]} : {scope:"all"}};
    // 두 위치의 목록 버튼이 같은 요청을 중복 전송하지 않게 응답 전부터 잠근다.
    if (kind === "registry_sync") { state.pendingRegistry = true; renderTimeStates(); }
    let job;
    try { job = await api("/api/jobs", {method:"POST", body}); }
    finally { state.pendingRegistry = false; }
    message(kind === "registry_sync" ? "학교 목록 불러오기를 요청했어요." : "조사를 시작했어요.");
    state.jobs = [job, ...state.jobs.filter(item => item.id !== job.id)];
    state.activeJobId = activeStatuses.has(job.status) ? job.id : null;
    renderTimeStates(); await jobs(); await results(); return job;
  }
  async function jobs() {
    const season = state.season;
    const [data, status] = await Promise.all([api("/api/jobs?limit=100"),
      api(season ? `/api/status?season_id=${season}` : "/api/status")]);
    if (season !== state.season) return;
    htmlSaveStatus(status.html_save);
    // 오래전에 멈춘 작업도 재개될 수 있어 최근 100건 밖의 활성 작업을 따로 확인한다.
    if (status.active_job_id && !data.items.some(job => job.id === status.active_job_id))
      data.items.unshift(await api(`/api/jobs/${status.active_job_id}`));
    state.jobs = data.items;
    state.rosterReady = status.eligible_institution_count > 0;
    state.activeJobId = status.active_job_id;
    const signature = data.items.map(job => `${job.id}:${job.revision}:${job.status}`).join("|");
    const changed = state.jobSignature !== null && (state.jobSignature !== signature || state.contentRevision !== status.content_revision);
    state.jobSignature = signature; state.contentRevision = status.content_revision;
    if (changed) await results();
    renderTimeStates();
    const current = data.items.filter(j => j.season_id === state.season || j.kind === "registry_sync");
    const active = data.items.find(j => ["queued","running","pausing","cancelling"].includes(j.status));
    const job = current.find(j => ["queued","running","pausing","cancelling"].includes(j.status)) ||
      current.find(j=>j.status!=="cancelled") || current[0];
    $("job").hidden = !job;
    if (!job) return;
    const labels = {queued:"조사를 준비하고 있어요.",running:"학교별로 일정을 확인하고 있어요.",pausing:"지금까지 확인한 내용을 저장하고 있어요.",
      paused:"조사를 잠시 멈췄어요.",cancelling:"조사를 마무리하고 있어요.",cancelled:"조사를 취소했어요. 확인한 기록은 남아 있어요.",
      completed:"조사를 마쳤어요. 아래 일정표에서 확인해 주세요.",failed:"조사를 마치지 못했어요. 이 시즌 다시 조사를 눌러 주세요."};
    const dateCount = job.kind === "schedule_scan" && job.scope_snapshot?.purpose !== "time";
    const timeSearch = job.scope_snapshot?.purpose === "time";
    const registryLabels = {queued:"학교 목록을 불러올 준비를 하고 있어요.", running:"학교 목록을 불러오고 있어요.",
      completed: job.counts?.failed ? "일부 학교 목록을 가져오지 못했어요. 학교 목록 갱신을 다시 눌러 주세요." :
        state.rosterReady ? "학교 목록을 불러왔어요. 기간을 선택해 조사를 시작해 주세요." :
        "조사할 학교 목록을 가져오지 못했어요. 다시 불러와 주세요.",
      failed:"학교 목록을 가져오지 못했어요. 다시 불러와 주세요."};
    $("job-message").textContent = (job.kind === "registry_sync" && registryLabels[job.status]) ||
      (timeSearch && window.scheduleTimeJobMessage(job, state.rows)) || labels[job.status] || "";
    $("job-message").hidden = dateCount && Boolean(state.researchSummary?.message);
    const controls = [];
    if (["queued","running"].includes(job.status)) controls.push(button("잠시 멈추기", e => action(e.currentTarget, async () => { await api(`/api/jobs/${job.id}/pause`, {method:"POST"}); await jobs(); })));
    if (job.status === "paused" && !active) {
      if (job.paused_reason === "job_budget") { $("job-message").hidden = false; $("job-message").textContent = job.kind === "registry_sync" ?
        "학교 목록 불러오기가 멈췄어요. 학교 목록 갱신을 다시 눌러 주세요." :
        "이번 조사에서 확인한 내용을 저장했어요. 필요하면 이 시즌 다시 조사를 눌러 계속 확인해 주세요."; }
      else if (job.paused_reason === "configuration_required") { $("job-message").hidden = false; $("job-message").textContent = job.kind === "registry_sync" ?
        "연결 설정이 바뀌었어요. 학교 목록을 다시 불러와 주세요." : "조사 조건이 바뀌었어요. 다시 조사해 주세요."; }
      else controls.push(button(job.kind === "registry_sync" ? "이어서 불러오기" : "이어서 조사", e => action(e.currentTarget, async () => { await api(`/api/jobs/${job.id}/resume`, {method:"POST",body:{expected_revision:job.revision,increase_limits:null}}); await jobs(); })));
    }
    if (["queued","running","paused"].includes(job.status)) controls.push(button("조사 취소", e => action(e.currentTarget, async () => { await api(`/api/jobs/${job.id}/cancel`, {method:"POST"}); await jobs(); })));
    $("job-actions").replaceChildren(...controls);
  }
  function newRecord() { state.conversation = null; $("conversation").replaceChildren(); $("record-text").value = ""; }
  async function exportHtml(download) {
    if (!state.season) throw new Error("조사할 기간을 먼저 선택해 주세요.");
    const popup = download ? null : window.open("about:blank", "_blank");
    try {
      const result = await api("/api/exports/html", {method:"POST",body:{season_id:state.season,include_notes:false,publication_policy_version:"4.0"}});
      if (result.status !== "current") throw new Error(result.status === "pending" ?
        "일정표를 갱신하고 있어요. 잠시 후 다시 시도해 주세요." :
        "기록은 보관됐지만 일정표 파일을 만들지 못했어요. 다시 시도해 주세요.");
      if (!result.url) throw new Error("일정표 파일을 만들지 못했어요. 다시 시도해 주세요.");
      if (download) {
        if (typeof result.filename !== "string" || !result.filename.trim())
          throw new Error("일정표 파일 이름을 확인하지 못했어요. 다시 시도해 주세요.");
        const response = await fetch(result.url);
        if (!response.ok) throw new Error("파일을 내려받지 못했어요.");
        const url = URL.createObjectURL(await response.blob());
        const a = document.createElement("a"); a.href = url;
        a.download = result.filename;
        a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
        message("파일을 내려받았어요. 프로그램 없이 열어볼 수 있어요.");
      } else if (popup) { popup.opener = null; popup.location.href = result.url; }
      else message("브라우저의 새 창 허용 후 다시 눌러 주세요.");
    } catch(error) { if (popup) popup.close(); throw error; }
  }
  async function seasonChanged() {
    state.season = $("season-select").value || null; newRecord(); state.jobSignature = null;
    state.researchSummary = null; $("research-summary").hidden = true; htmlSaveStatus(null);
    renderTimeStates();
    $("results").dataset.page = "0";
    if (state.season) await api("/api/selected-season", {method:"POST",body:{season_id:state.season}});
    await Promise.all([results(), jobs()]);
  }
  $("research-form").onsubmit = e => { e.preventDefault(); action($("research-submit"), () => startJob()); };
  $("record-form").onsubmit = e => { e.preventDefault(); const submit=e.submitter;
    action(submit, async () => {
      if (!state.season) throw new Error("조사할 기간을 먼저 선택해 주세요.");
      const text = $("record-text").value.trim(); if (!text) return;
      state.recordPending = true;
      let result;
      try { result = await api("/api/records/natural", {method:"POST",body:{season_id:state.season,text,
        conversation_id:state.conversation?.id || null,expected_revision:state.conversation?.revision || null}}); }
      finally { state.recordPending = false; }
      const user = document.createElement("div"); user.className="user-line"; user.textContent=text;
      const reply = document.createElement("div"); reply.className="reply"; reply.textContent = result.status === "research_request" ? "새 조사는 일정찾기에서 시작해 주세요. 이곳에는 직접 확인한 내용을 기록할 수 있어요." : [result.message,...result.questions].join("\n");
      $("conversation").append(user,reply); $("record-text").value="";
      state.conversation = result.status === "question" ? result : null;
      await results();
    });
  };
  $("new-record").onclick = newRecord;
  $("save-html").onclick = e => action(e.currentTarget, () => exportHtml(true));
  $("open-html").onclick = e => action(e.currentTarget, () => exportHtml(false));
  $("season-select").onchange = () => seasonChanged().catch(e=>message(e.message));
  $("backup").onclick = e => action(e.currentTarget, async () => {
    const result=await api("/api/backups",{method:"POST"}); const a=document.createElement("a");a.href=result.download_url;a.download="졸업식-기록-백업.sqlite3";a.click();
  });
  $("registry-sync").onclick = e => action(e.currentTarget, () => startJob({kind:"registry_sync"}));
  $("load-roster").onclick = e => action(e.currentTarget, () => startJob({kind:"registry_sync"}));
  function seasonOption(season) { return new Option(`${season.search_start} ~ ${season.search_end}`, season.id); }
  function validateSeasonRange() {
    const start = $("season-start"), end = $("season-end");
    const range = window.seasonRangeBounds(start.value, end.value);
    end.min = start.value; end.max = range.maxEnd || "";
    end.setCustomValidity(range.message);
    $("season-limit").textContent = range.maxEnd ? `종료일은 ${range.maxEnd}까지 선택할 수 있어요.` : "6개월 미만의 기간을 선택해 주세요.";
    return range.valid;
  }
  $("season-start").oninput = validateSeasonRange;
  $("season-end").oninput = validateSeasonRange;
  $("add-season").onclick = () => {
    const season = state.seasons.find(item => item.id === state.season);
    $("season-start").value = season?.search_start || "";
    $("season-end").value = season?.search_end || "";
    validateSeasonRange(); $("season-dialog").showModal();
  };
  $("close-season").onclick = () => $("season-dialog").close();
  $("season-form").onsubmit = e => {e.preventDefault();
    if (!validateSeasonRange()) { e.target.reportValidity(); return; }
    action(e.submitter, async () => {
      const season = await api("/api/seasons", {method:"POST",body:{search_start:$("season-start").value,search_end:$("season-end").value}});
      if (!state.seasons.some(item => item.id === season.id)) {
        state.seasons.push(season); $("season-select").add(seasonOption(season));
      }
      $("season-select").value = season.id; $("season-dialog").close(); await seasonChanged();
      try { await startJob(); }
      catch(error) { throw new Error(`기간은 저장했어요. ${error.message} 저장된 시즌에서 다시 조사할 수 있어요.`); }
    });
  };
  $("shutdown").onclick = e => action(e.currentTarget, async () => {
    if (state.recordPending) throw new Error("기록하는 중이에요. 잠시 후 종료해 주세요.");
    if ($("record-text").value.trim()) {
      $("record-text").focus();
      throw new Error("아직 기록하지 않은 내용이 있어요. 기록하기를 누른 뒤 종료해 주세요.");
    }
    const result = await api("/api/app/shutdown",{method:"POST",body:{pause_active_job:true}});
    state.stopped = true;
    message(window.scheduleShutdownMessage(result.paused_active_job === true));
  });
  async function init() {
    const data=await api("/api/bootstrap");state.token=data.token;state.seasons=data.seasons;
    state.rosterReady = data.status.eligible_institution_count > 0;
    state.activeJobId = data.status.active_job_id;
    $("season-select").replaceChildren(...data.seasons.map(seasonOption));
    $("season-select").value=data.selected_season_id || ""; await seasonChanged();
    async function poll() { if(state.stopped)return;try{await jobs();}catch{}setTimeout(poll,Math.max(2000,data.poll_interval_ms)); }
    setTimeout(poll,Math.max(2000,data.poll_interval_ms));
  }
  init().catch(error=>message(error.message));
})();

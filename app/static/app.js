/* 사적 입력과 원문은 로컬 API에서만 처리한다. 화면은 네 열 결과만 소비한다. */
(() => {
  const $ = id => document.getElementById(id);
  const state = {token: "", season: null, seasons: [], rows: [], selected: new Map(), conversation: null,
    jobs: [], jobSignature: "", querySerial: 0, stopped: false};
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
    button.disabled = true; message("");
    try { await work(); } catch (error) { message(error.message); }
    finally { button.disabled = false; }
  }
  function button(text, click) {
    const node = document.createElement("button"); node.type = "button"; node.textContent = text; node.onclick = click; return node;
  }
  function selected() {
    $("selected-schools").replaceChildren(...[...state.selected.values()].map(s =>
      button(`${s.name} ×`, () => { state.selected.delete(s.id); selected(); })));
  }
  async function results() {
    const season = state.season;
    const result = await api(`/api/results?season_id=${season}`);
    if (season !== state.season) return;
    state.rows = result.rows;
    const labels = ["학교명", "졸업식 날짜", "실제 시작 시각", "학교 위치(주소)"];
    const rows = result.rows.map(row => {
      const tr = document.createElement("tr"); tr.dataset.name = row.name; tr.dataset.date = row.date || "";
      [row.name, row.date || "미확인", row.start_time || "미확인", row.address || "미확인"].forEach((value, i) => {
        const td = document.createElement("td"); td.dataset.label = labels[i];
        if (i === 0 && row.url) { const link = document.createElement("a"); link.textContent = value; link.href = row.url; link.target = "_blank"; link.rel = "noopener noreferrer"; td.append(link); }
        else td.textContent = value;
        if (i === 0 && row.event_label) { const label = document.createElement("span"); label.className = "event-label"; label.textContent = row.event_label; td.append(label); }
        if (i === 0 && row.status) { const label = document.createElement("span"); label.className = "cancelled"; label.textContent = row.status === "cancelled" ? " · 취소" : " · 미실시"; td.append(label); }
        tr.append(td);
      }); return tr;
    });
    $("results").querySelector("tbody").replaceChildren(...rows);
    window.setupSchedule($("results"));
  }
  async function startJob(ids, all = false, kind = "schedule_scan") {
    const job = await api("/api/jobs", {method:"POST", body:{kind, season_id:kind === "schedule_scan" ? state.season : null,
      mode:"recheck", selection:all ? {scope:"all"} : {scope:"ids", institution_ids:ids}, limits:null}});
    message("조사를 시작했어요.");
    state.jobSignature = ""; await jobs(); await results(); return job;
  }
  async function jobs() {
    const data = await api("/api/jobs?limit=100");
    state.jobs = data.items;
    const current = data.items.filter(j => j.season_id === state.season || j.kind === "registry_sync");
    const active = data.items.find(j => ["queued","running","pausing","cancelling"].includes(j.status));
    const job = current.find(j => ["queued","running","pausing","cancelling"].includes(j.status)) ||
      current.find(j=>j.status!=="cancelled") || current[0];
    $("job").hidden = !job;
    if (!job) return;
    const signature = `${job.id}:${job.revision}`;
    const changed = state.jobSignature && state.jobSignature !== signature;
    state.jobSignature = signature;
    const labels = {queued:"조사를 준비하고 있어요.",running:"학교별로 일정을 확인하고 있어요.",pausing:"지금까지 확인한 내용을 저장하고 있어요.",
      paused:"조사를 잠시 멈췄어요.",cancelling:"조사를 마무리하고 있어요.",cancelled:"조사를 취소했어요. 확인한 기록은 남아 있어요.",
      completed:"조사를 마쳤어요. 아래 일정표에서 확인해 주세요.",failed:"조사를 마치지 못했어요. 필요한 학교를 선택해 다시 조사해 주세요."};
    $("job-message").textContent = labels[job.status] || "";
    const controls = [];
    if (["queued","running"].includes(job.status)) controls.push(button("잠시 멈추기", e => action(e.currentTarget, async () => { await api(`/api/jobs/${job.id}/pause`, {method:"POST"}); await jobs(); })));
    if (job.status === "paused" && !active) {
      if (job.paused_reason === "job_budget") $("job-message").textContent = "이번 조사에서 확인한 내용을 저장했어요. 더 필요한 학교를 골라 새로 조사해 주세요.";
      else controls.push(button("이어서 조사", e => action(e.currentTarget, async () => { await api(`/api/jobs/${job.id}/resume`, {method:"POST",body:{expected_revision:job.revision,increase_limits:null}}); await jobs(); })));
    }
    if (["queued","running","paused"].includes(job.status)) controls.push(button("조사 취소", e => action(e.currentTarget, async () => { await api(`/api/jobs/${job.id}/cancel`, {method:"POST"}); await jobs(); })));
    $("job-actions").replaceChildren(...controls);
    if (changed) await results();
  }
  function newRecord() { state.conversation = null; $("conversation").replaceChildren(); $("record-text").value = ""; }
  async function exportHtml(download) {
    const popup = download ? null : window.open("about:blank", "_blank");
    try {
      const result = await api("/api/exports/html", {method:"POST",body:{season_id:state.season,include_notes:false,publication_policy_version:"4.0"}});
      if (!result.url) throw new Error("HTML을 만들지 못했어요. 다시 시도해 주세요.");
      if (download) {
        const response = await fetch(result.url);
        if (!response.ok) throw new Error("HTML을 저장하지 못했어요.");
        const url = URL.createObjectURL(await response.blob());
        const a = document.createElement("a"); a.href = url;
        a.download = `graduation-schedule-${state.seasons.find(s=>s.id===state.season).academic_year}.html`;
        a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
        message("HTML 파일을 저장했어요. 프로그램 없이 열어볼 수 있어요.");
      } else if (popup) { popup.opener = null; popup.location.href = result.url; }
      else message("브라우저의 새 창 허용 후 다시 눌러 주세요.");
    } catch(error) { if (popup) popup.close(); throw error; }
  }
  async function seasonChanged() {
    state.season = $("season-select").value; state.selected.clear(); selected(); newRecord(); state.jobSignature = "";
    const season = state.seasons.find(s=>s.id === state.season);
    $("season-range").textContent = `${season.search_start} ~ ${season.search_end}`;
    await api("/api/selected-season", {method:"POST",body:{season_id:state.season}});
    await Promise.all([results(), jobs()]);
  }
  $("school-query").oninput = async () => {
    const serial = ++state.querySerial, query = $("school-query").value.trim();
    if (!query) { $("school-options").replaceChildren(); return; }
    try {
      const found = await api(`/api/institutions?query=${encodeURIComponent(query)}&limit=8`);
      if (serial !== state.querySerial) return;
      $("school-options").replaceChildren(...found.items.map(s => button(`${s.name}${s.campus_label ? " · "+s.campus_label : ""} (${s.district})`, () => {
        state.selected.set(s.id,s); selected(); $("school-options").replaceChildren(); $("school-query").value = "";
      })));
      if (!found.items.length) $("school-options").textContent = "등록된 학교 이름을 다시 확인해 주세요.";
    } catch(error) { message(error.message); }
  };
  $("reuse-targets").onclick = () => { state.rows.forEach(r=>state.selected.set(r.institution_id,{id:r.institution_id,name:r.name})); selected(); };
  $("research-form").onsubmit = e => { e.preventDefault(); action($("research-submit"), async () => {
    if (!state.selected.size && !$("all-schools").checked) throw new Error("조사할 학교를 검색해서 선택해 주세요.");
    await startJob([...state.selected.keys()], $("all-schools").checked);
  }); };
  $("record-form").onsubmit = e => { e.preventDefault(); const submit=e.submitter;
    action(submit, async () => {
      const text = $("record-text").value.trim(); if (!text) return;
      const result = await api("/api/records/natural", {method:"POST",body:{season_id:state.season,text,
        conversation_id:state.conversation?.id || null,expected_revision:state.conversation?.revision || null}});
      const user = document.createElement("div"); user.className="user-line"; user.textContent=text;
      const reply = document.createElement("div"); reply.className="reply"; reply.textContent=[result.message,...result.questions].join("\n");
      $("conversation").append(user,reply); $("record-text").value="";
      state.conversation = result.status === "question" ? result : null;
      if (result.status === "research_request" && !result.questions.length && result.institution_ids.length) await startJob(result.institution_ids);
      await results();
    });
  };
  $("new-record").onclick = newRecord;
  $("refresh-results").onclick = e => action(e.currentTarget, results);
  $("save-html").onclick = e => action(e.currentTarget, () => exportHtml(true));
  $("open-html").onclick = e => action(e.currentTarget, () => exportHtml(false));
  $("season-select").onchange = () => seasonChanged().catch(e=>message(e.message));
  $("backup").onclick = e => action(e.currentTarget, async () => {
    const result=await api("/api/backups",{method:"POST"}); const a=document.createElement("a");a.href=result.download_url;a.download="졸업식-기록-백업.sqlite3";a.click();
  });
  $("registry-sync").onclick = e => action(e.currentTarget, () => startJob([],true,"registry_sync"));
  $("add-season").onclick = () => $("season-dialog").showModal();
  $("close-season").onclick = () => $("season-dialog").close();
  $("season-form").onsubmit = e => {e.preventDefault(); action(e.submitter,async () => {
    const data=Object.fromEntries(new FormData(e.target));data.academic_year=Number(data.academic_year);
    const season=await api("/api/seasons",{method:"POST",body:{...data,export_key:`season-${data.academic_year}-${Date.now()}`,timezone:"Asia/Seoul",region_profile:"gwangju_5_gu"}});
    state.seasons.push(season);const option=new Option(season.label,season.id);$("season-select").add(option);$("season-select").value=season.id;
    $("season-dialog").close();await seasonChanged();
  });};
  $("shutdown").onclick = e => action(e.currentTarget, async () => {
    await api("/api/app/shutdown",{method:"POST",body:{pause_active_job:true}});state.stopped=true;
    message("기록을 저장하고 프로그램을 종료했어요. 이 창을 닫아도 됩니다.");
  });
  async function init() {
    const data=await api("/api/bootstrap");state.token=data.token;state.seasons=data.seasons;
    $("season-select").replaceChildren(...data.seasons.map(s=>new Option(s.label,s.id)));
    $("season-select").value=data.selected_season_id; await seasonChanged();
    async function poll() { if(state.stopped)return;try{await jobs();}catch{}setTimeout(poll,Math.max(2000,data.poll_interval_ms)); }
    setTimeout(poll,Math.max(2000,data.poll_interval_ms));
  }
  init().catch(error=>message(error.message));
})();

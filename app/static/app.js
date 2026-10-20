"use strict";

const TYPES = {childcare:"어린이집",kindergarten:"유치원",elementary:"초등학교",middle:"중학교",high:"고등학교",special_school:"특수학교",other_school:"기타학교",university:"대학교",junior_college:"전문대학"};
const DISTRICTS = ["동구","서구","남구","북구","광산구"];
const FIELDS = {ceremony_status:"행사 상태",event_date:"졸업식 날짜",start_time:"실제 시작 시각",venue:"장소"};
const BASES = {official:"공식 자료",phone_confirmed:"전화 확인",document_confirmed:"문서 직접 확인",user_recorded:"확인되지 않은 직접 기록",none:"미확인"};
const CEREMONY = {scheduled:"개최 예정",cancelled:"취소",not_held:"미개최",unclear:"미확인"};
const EVENT_TYPES = {graduation_ceremony:"졸업식",graduation_and_completion:"졸업·수료식",completion_ceremony:"수료식",degree_award_ceremony:"학위수여식",unknown:"행사 구분 미확인"};
const JOB_STATUS = {queued:"대기",running:"조사 중",pausing:"안전하게 멈추는 중",paused:"멈춤",cancelling:"취소 중",cancelled:"취소됨",completed:"완료",failed:"실패"};
const AUTO_STATUS = {unchecked:"미조사",missing:"근거 미발견",reference_only:"참고 후보",official_confirmed:"공식 근거 확인",conflict:"근거 충돌"};
const REASONS = {
  not_found:"공개 근거를 찾지 못함",access_failed:"원문 접근 실패",unsupported_format:"미지원 문서 형식",parse_failed:"문서 읽기 실패",document_partial:"일부 구간만 읽음",
  source_unverified:"공식 출처 미확인",season_unclear:"학년도·시즌 불명확",event_unclear:"행사 식별 불명확",event_match_required:"같은 행사인지 확인 필요",
  date_missing:"날짜 미확인",time_missing:"실제 시작 시각 미확인",conflict:"근거 사이 충돌",manual_difference:"직접 입력과 웹 자료 차이",date_context_mismatch:"시각에 연결된 날짜 불일치",
  cancelled_notice:"취소·미개최 주의",configuration_required:"연결 설정 필요",provider_error:"외부 서비스 오류",item_budget:"기관별 한도 도달",item_timeout:"기관별 시간 한도",
  segment_budget:"실행 구간 한도 도달",job_budget:"작업 누적 한도 도달",interrupted:"프로그램 중단",user_pause:"사용자가 멈춤",user_cancel:"사용자가 취소",
  outside_window:"탐색 기간 밖",wrong_source:"다른 기관·잘못된 출처",content_mismatch:"본문 불일치",empty_article:"본문 없음",tls_error:"보안 연결 실패",
  connection_reset:"연결 끊김",search_request_error:"검색 요청 실패",domain_filter_unverified:"검색 도메인 필터 미검증",sample_limited:"표본 한정",coverage_incomplete:"전체 목록 미확보",
  month_only:"월 계획만 확인",unsupported_time:"실제 시작 시각 근거 부족",quote_absent:"인용 원문 없음",quote_support_ambiguous:"인용 의미 불명확",value_not_supported:"값을 뒷받침하는 근거 부족",
  preview_only:"미리보기만 확인",visual_grounding_unverified:"이미지 근거 미검증",semantic_review_required:"의미 재검토 필요",evidence_invalidated:"기존 근거 무효",
  external_transfer_blocked:"외부 문서 전송 차단",rule_not_enabled:"검증 규칙 미활성",publication_held:"사용이 보류된 항목 있음",confirmation_time_missing:"실제 확인 시점 미상",
  recheck_failed:"필요 근거 재확인 실패",rule_version_mismatch:"검증 규칙 버전 불일치",evidence_link_invalid:"근거 연결 무효",field_review_required:"항목 확인 필요",manual_confirmation_invalid:"직접 확인 요건 부족"
};
const $ = (selector, root=document) => root.querySelector(selector);
const state = {token:null, seasons:[], season:null, view:"calendar", selected:new Set(), items:[], nextCursor:null, overview:null, settings:{}, loadVersion:0,pendingIds:new Map()};

function element(tag, attributes={}, ...children) {
  const node = document.createElement(tag);
  for (const [key,value] of Object.entries(attributes)) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2),value);
    else if (key in node && !key.startsWith("aria-") && key !== "form") node[key] = value;
    else node.setAttribute(key,String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function button(label, action, className="quiet") {
  return element("button",{type:"button",class:className,text:label,onclick:action});
}

function badge(label, style="") {
  return element("span",{class:`badge ${style}`,text:label});
}

function reasons(codes=[]) {
  return element("div",{class:"reason-list"}, [...new Set(codes)].map(code => badge(REASONS[code] || code,"warning")));
}

function moment(value) {
  if (!value) return "확인 시점 미상";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("ko-KR",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(date);
}

function valueText(value) {
  if (value === null || value === undefined || value === "") return "미확인";
  if (typeof value === "object") return value.precision === "month" ? `${value.month} (월 계획)` : value.date || "미확인";
  return CEREMONY[value] || String(value);
}

function safeURL(value) {
  try {
    const url = new URL(value);
    if (!["http:","https:"].includes(url.protocol) || url.username || url.password) return null;
    const sensitive = new Set(["key","api_key","apikey","servicekey","token","access_token","authorization"]);
    for (const key of [...url.searchParams.keys()]) if (sensitive.has(key.toLowerCase())) url.searchParams.delete(key);
    return url.href;
  } catch { return null; }
}

function sourceLink(url, label="근거 원문 보기") {
  const safe = safeURL(url);
  return safe ? element("a",{href:safe,target:"_blank",rel:"noopener noreferrer",text:label}) : element("span",{class:"muted",text:"공개 링크 없음"});
}

function notify(message, error=false) {
  const node = $("#message");
  node.textContent = message;
  node.className = error ? "error" : "";
  node.hidden = false;
}

function errorMessage(error) {
  if (error.code === "settings_changed") return "이 작업을 시작했을 때와 조사·모델 설정이 달라 재개할 수 없습니다. 이전 설정으로 복원하거나 기존 기록을 유지한 새 작업을 시작하세요.";
  if (error.status === 409) return `저장된 내용이 변경되었거나 같은 대상 작업이 이미 실행 중입니다. 새로고침 후 다시 확인하세요. ${error.message}`;
  return error.message || "요청을 처리하지 못했습니다.";
}

async function api(path, {method="GET",body,idem=false}={}) {
  const headers = {};
  if (state.token) headers["X-App-Token"] = state.token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const operation = idem ? `${method}:${path}:${JSON.stringify(body)}` : null;
  if (idem) {
    if (!state.pendingIds.has(operation)) state.pendingIds.set(operation,crypto.randomUUID());
    headers["Idempotency-Key"] = state.pendingIds.get(operation);
  }
  let response;
  try { response = await fetch(path,{method,headers,body:body === undefined ? undefined : JSON.stringify(body),credentials:"same-origin"}); }
  catch { throw new Error("프로그램에 연결할 수 없습니다. 프로그램이 실행 중인지 확인하세요. 저장해 둔 HTML 일정표는 계속 열 수 있습니다."); }
  const type = response.headers.get("Content-Type") || "";
  const result = type.includes("json") ? await response.json() : null;
  if (!response.ok) {
    if (response.status < 500) state.pendingIds.delete(operation);
    const detail = result?.error || result?.detail || result || {};
    const error = new Error(typeof detail.message === "string" ? detail.message : Array.isArray(detail) ? detail.map(d => d.msg).join(" / ") : `요청 실패 (${response.status})`);
    error.code = detail.code; error.status = response.status; error.details = detail.details;
    throw error;
  }
  state.pendingIds.delete(operation);
  return result;
}

async function task(action) {
  try { return await action(); } catch (error) { notify(errorMessage(error),true); }
}

function options(values, placeholder) {
  const valuesArray = Array.isArray(values) ? values.map(value => [value,value]) : Object.entries(values);
  return [...(placeholder ? [element("option",{value:"",text:placeholder})] : []),...valuesArray.map(([value,label]) => element("option",{value,text:label}))];
}

function control(label, name, {type="text",value="",choices,required=false,hint,full=false,placeholder,...extra}={}) {
  const input = choices ? element("select",{name,required,...extra},options(choices)) : type === "textarea" ? element("textarea",{name,value,required,maxLength:4000,...extra}) : element("input",{name,type,value,required,placeholder,...extra});
  if (choices) input.value = value;
  const wrapper = element("label",{class:full ? "full-width" : ""},label,input,hint ? element("span",{class:"hint",text:hint}) : null);
  return {wrapper,input};
}

function openDialog(title, content) {
  $("#dialog-title").textContent = title;
  $("#dialog-content").replaceChildren(content);
  if (!$("#dialog").open) $("#dialog").showModal();
  $("#dialog").scrollTop = 0;
}

function formShell(title, fields, onsubmit, submitLabel="저장하기") {
  const form = element("form",{});
  const error = element("div",{class:"form-error",role:"alert",hidden:true});
  const submit = element("button",{type:"submit",class:"primary",text:submitLabel});
  form.append(...fields,error,element("div",{class:"form-actions"},button("닫기",()=>$("#dialog").close()),submit));
  form.addEventListener("submit",async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    submit.disabled = true; error.hidden = true;
    try { await onsubmit(form); }
    catch (reason) { error.textContent = errorMessage(reason); error.hidden = false; error.scrollIntoView({block:"nearest"}); }
    finally { submit.disabled = false; }
  });
  openDialog(title,form);
  return form;
}

function currentSeason() {
  if (!state.season) throw new Error("먼저 조사할 시즌을 만들어 주세요.");
  return state.season;
}

async function loadSeasons(preferred) {
  const page = await api("/api/seasons?limit=500");
  state.seasons = page.items || page || [];
  const saved = preferred || state.season?.id || localStorage.getItem("graduation-season");
  state.season = state.seasons.find(season=>season.id===saved) || state.seasons[0] || null;
  const select = $("#season-select");
  select.replaceChildren(...state.seasons.map(season=>element("option",{value:season.id,text:season.label})));
  if (!state.season) select.append(element("option",{value:"",text:"새 시즌을 만들어 주세요"}));
  else { select.value = state.season.id; localStorage.setItem("graduation-season",state.season.id); }
  $("#season-range").textContent = state.season ? `${state.season.academic_year}학년도 · ${state.season.search_start} ~ ${state.season.search_end}` : "시즌을 만들면 조사와 직접 입력을 시작할 수 있습니다.";
}

function renderOverview(data) {
  state.overview = data;
  const ratio = value=>value === null || value === undefined ? "대상 없음" : `${Math.round(value*100)}%`;
  const cards = [
    ["등록 대상 기관",data.eligible_institutions ?? 0,`조사 시도 ${data.attempted_institutions ?? 0}개 기관`],
    ["현재 날짜 확인",data.date_confirmed_institutions ?? 0,`기관 기준 확인율 ${ratio(data.date_confirmation_ratio)}`],
    ["현재 시작 시각 확인",data.time_confirmed_institutions ?? 0,`기관 기준 확인율 ${ratio(data.time_confirmation_ratio)}`],
    ["확인이 필요한 기관",data.review_institutions ?? 0,"미확인·충돌·보류 포함","warning"],
  ];
  $("#overview").replaceChildren(...cards.map(([label,value,sub,style])=>element("article",{class:`metric ${style || ""}`},element("div",{class:"label",text:label}),element("div",{class:"value",text:`${value}곳`}),element("div",{class:"sub",text:sub}))));
  $("#coverage-note").textContent = `${data.registry_complete ? "공식 전체 목록 확보 완료" : "등록 기관 기준입니다. 공식 전체 목록 확보가 완료되지 않았습니다."} · 저장 내용 버전 ${data.content_revision ?? 0} · 게시 정책 ${data.publication_policy_version || "4.0"}`;
  $("#review-count").textContent = data.review_institutions || "";
}

async function reloadOverview() {
  if (state.season) renderOverview(await api(`/api/overview?season_id=${state.season.id}`));
}

const VIEW_INFO = {
  calendar:["우리 동네 졸업식 일정","날짜와 실제 시작 시각, 언제 어떻게 확인했는지 함께 살펴보세요.","일반 일정표는 정당하게 확인했던 값을 표시합니다. 필요한 근거를 재확인하지 못한 경우 과거 확인값에 현재 사용 보류 경고가 붙습니다."],
  sales:["꽃다발 판매 준비가 가능한 일정","현재 사용 가능한 날짜와 실제 시작 시각이 함께 확인된 행사입니다.","판매 준비 가능 표시는 저장된 근거와 직접 확인을 기준으로 합니다. 행사 변경 가능성이 있으므로 확인 시점도 함께 살펴보세요."],
  review:["추가 확인이 필요한 기관","아직 행사를 찾지 못한 기관도 빠짐없이 확인 목록에 남습니다.","공개 자료가 부족하면 전화나 문서를 직접 확인해 입력할 수 있습니다. 실제 확인한 시각을 모르면 미상으로 저장하세요."],
  all:["조사·직접 입력한 모든 기록","월 계획, 보류된 후보, 취소·미개최와 직접 기록까지 살펴보세요.","이 목록의 후보값과 월 계획은 확정 일정이 아닙니다. 일반 일정표와 판매 준비 목록은 게시 판정을 통과한 값만 사용합니다."],
  institutions:["조사할 교육기관 관리","공식 기관 목록을 가져오거나 기관 연락처와 홈페이지를 보완하세요.","공식 목록의 확보 범위와 기준일을 확인하세요. 직접 추가한 기관만으로 전체 기관을 확보했다고 판단하지 않습니다."],
  jobs:["조사 진행 상황과 이력","전체 조사 대상과 누적 사용량을 저장하므로 멈춘 뒤 이어갈 수 있습니다.","멈춤은 다시 이어갈 수 있습니다. 취소한 작업은 결과를 남기고 종료하며 재개하지 않습니다. 새로 등록한 기관은 기존 작업 대상에 자동 추가되지 않습니다."]
};

function updateViewUI() {
  const [title,description,notice] = VIEW_INFO[state.view];
  $("#page-title").textContent=title; $("#page-description").textContent=description;
  $("#view-notice").textContent=notice; $("#view-notice").hidden=false;
  document.querySelectorAll(".tabs button").forEach(tab=>{tab.classList.toggle("active",tab.dataset.view===state.view); if(tab.dataset.view===state.view)tab.setAttribute("aria-current","page");else tab.removeAttribute("aria-current");});
  $("#institution-actions").hidden = state.view !== "institutions";
  $("#research-actions").hidden = ["jobs","institutions"].includes(state.view);
  $("#list-filters").hidden = state.view === "jobs";
  $("#registry-status").hidden = state.view !== "institutions";
  document.querySelectorAll(".date-filter").forEach(label=>label.hidden=!["calendar","sales","all"].includes(state.view));
}

function queryParams(cursor) {
  const params = new URLSearchParams({limit:"100"});
  if (state.season && !["institutions","jobs"].includes(state.view)) params.set("season_id",state.season.id);
  if (cursor) params.set("cursor",cursor);
  if (state.view !== "jobs") {
    for (const [id,key] of [["search","query"],["district-filter","district"],["type-filter","type"]]) if ($(`#${id}`).value) params.set(key,$(`#${id}`).value);
    if (["calendar","sales","all"].includes(state.view)) {
      params.set("view",state.view);
      if ($("#from-date").value) params.set("from_date",$("#from-date").value);
      if ($("#to-date").value) params.set("to_date",$("#to-date").value);
    }
  }
  return params;
}

function endpoint() {
  return state.view === "review" ? "/api/review-queue" : state.view === "institutions" ? "/api/institutions" : state.view === "jobs" ? "/api/jobs" : "/api/events";
}

async function loadView(append=false) {
  const version = ++state.loadVersion;
  updateViewUI();
  if (!state.season && !["institutions","jobs"].includes(state.view)) {
    $("#results").replaceChildren(empty("시즌을 만들고 시작해 주세요","학년도와 조사 기간을 정하면 기관별 졸업식 날짜와 시각을 기록할 수 있습니다.",button("+ 새 시즌",()=>seasonForm(),"primary")));
    $("#result-count").textContent="시즌 없음"; return;
  }
  if (!append) $("#results").replaceChildren(element("div",{class:"loading",text:"저장된 기록을 불러오고 있습니다…"}));
  const page = await api(`${endpoint()}?${queryParams(append ? state.nextCursor : null)}`);
  if (version !== state.loadVersion) return;
  state.items = append ? [...state.items,...page.items] : page.items || [];
  state.nextCursor = page.next_cursor;
  $("#result-count").textContent = `전체 ${page.total ?? state.items.length}건 · ${state.items.length}건 표시`;
  $("#load-more").hidden = !state.nextCursor;
  renderItems();
  if (state.view === "institutions") await renderRegistry();
}

function empty(title, description, action) {
  return element("div",{class:"empty"},element("div",{class:"symbol","aria-hidden":"true",text:"✿"}),element("h3",{text:title}),element("p",{text:description}),action);
}

function selectInstitution(id, name) {
  return element("input",{type:"checkbox",checked:state.selected.has(id),"aria-label":`${name} 조사 대상으로 선택`,onchange:event=>{
    event.target.checked ? state.selected.add(id) : state.selected.delete(id);
    $("#selected-count").textContent=state.selected.size;
    document.querySelectorAll(`input[data-institution="${CSS.escape(id)}"]`).forEach(input=>input.checked=state.selected.has(id));
  },"data-institution":id});
}

function institutionCell(institution, title) {
  return element("td",{class:"institution-cell"},element("div",{class:"institution-name",text:institution.name}),title ? element("div",{text:title}) : null,element("small",{text:`${institution.district} · ${TYPES[institution.type] || institution.type}${institution.campus_label ? " · "+institution.campus_label : ""}`}),institution.phone ? element("small",{text:institution.phone}) : null);
}

function table(headers, rows) {
  return element("div",{},element("p",{class:"mobile-scroll-hint",text:"↔ 표를 좌우로 밀면 날짜와 근거·입력 버튼까지 볼 수 있습니다."}),element("div",{class:"table-wrap"},element("table",{},element("thead",{},element("tr",{},headers.map(header=>element("th",{scope:"col",text:header})))),element("tbody",{},rows))));
}

function renderItems() {
  if (!state.items.length) {
    const title = state.view==="sales" ? "판매 준비 가능한 일정이 아직 없습니다" : state.view==="calendar" ? "확인된 졸업식 날짜가 아직 없습니다" : "조건에 맞는 기록이 없습니다";
    $("#results").replaceChildren(empty(title,state.view==="sales" ? "날짜와 같은 행사의 실제 시작 시각을 함께 확인하면 이 목록에 표시됩니다. 확인할 기관에서 부족한 내용을 살펴보세요." : "기관 목록을 등록한 뒤 조사하거나, 전화·문서로 직접 확인한 일정을 입력해 주세요."));
    return;
  }
  if (state.view==="jobs") {$("#results").replaceChildren(...state.items.map(jobCard));return;}
  if (state.view==="institutions") {
    $("#results").replaceChildren(table(["선택","기관","주소 · 홈페이지","운영 상태","관리"],state.items.map(i=>element("tr",{},
      element("td",{},selectInstitution(i.id,i.name)),institutionCell(i),
      element("td",{},element("div",{text:i.address}),i.homepage_url ? sourceLink(i.homepage_url,"홈페이지") : element("small",{class:"muted",text:"홈페이지 미등록"})),
      element("td",{},badge(({active:"운영 중",temporarily_closed:"휴원·휴교",operation_review:"운영 확인 필요",closed_confirmed:"운영 종료 확인",unknown:"운영 미확인"})[i.operation_status] || i.operation_status,"neutral")),
      element("td",{},button("정보 정정",()=>institutionForm(i),"detail-link"),element("br"),button("일정 직접 입력",()=>eventForm(i),"detail-link"),element("br"),button("이 기관 조사",()=>task(()=>startScan([i.id])),"detail-link"))
    ))));return;
  }
  if (state.view==="review") {
    $("#results").replaceChildren(table(["선택","기관","확인할 내용","최근 조사","다음 행동"],state.items.map(row=>element("tr",{},
      element("td",{},selectInstitution(row.institution.id,row.institution.name)),institutionCell(row.institution),
      element("td",{},reasons(row.reason_codes),element("ul",{class:"list-bullets"},(row.unresolved_questions || []).map(q=>element("li",{text:q})))),
      element("td",{},element("small",{text:row.last_attempt_at ? moment(row.last_attempt_at) : "아직 조사하지 않음"})),
      element("td",{},...(row.event_ids || []).map(id=>button("기록·근거 보기",()=>task(()=>eventDetail(id)),"detail-link")),element("br"),button("직접 입력",()=>eventForm(row.institution),"detail-link"),element("br"),button("이 기관 조사",()=>task(()=>startScan([row.institution.id])),"detail-link"))
    ))));return;
  }
  $("#results").replaceChildren(table(["선택","기관 · 행사","날짜 · 실제 시작","장소","확인 상태 · 시점","근거 · 입력"],state.items.map(event=>{
    const p=event.publication, datePermission=p.field_permissions?.event_date || {}, timePermission=p.field_permissions?.start_time || {};
    const status = p.sales_ready ? badge("판매 준비 가능") : p.state==="excluded" ? badge("취소·제외","neutral") : p.calendar_eligible ? badge("추가 확인 필요","warning") : badge("게시 보류","warning");
    const summary = element("td",{class:"confirm-cell"},status,
      p.calendar_eligible && !p.current_date_allowed ? badge("과거 확인값 · 현재 사용 보류","warning") : null,
      p.calendar_date ? element("p",{text:`날짜 · ${BASES[event.fields?.event_date?.effective_basis] || "확인"} · ${moment(datePermission.confirmed_at)}`}) : null,
      p.calendar_time ? element("p",{text:`시각 · ${BASES[event.fields?.start_time?.effective_basis] || "확인"} · ${moment(timePermission.confirmed_at)}`}) : null,
      datePermission.recheck_status==="failed" || timePermission.recheck_status==="failed" ? element("p",{text:"필요 근거 재확인 실패 · 상세 확인 필요"}) : null,
      state.view==="all" ? reasons(p.reason_codes) : null
    );
    const schedule=element("td",{class:"schedule-cell"},element("div",{class:p.calendar_date?"date":"unknown",text:p.calendar_date || "날짜 미확인"}),element("div",{class:p.calendar_time?"time":"unknown",text:p.calendar_time || "시각 미확인"}));
    if (state.view==="all" && !p.calendar_eligible) {
      const candidate = event.fields?.event_date?.manual?.value || event.fields?.event_date?.automatic?.value;
      if (candidate) schedule.append(element("small",{class:"muted",text:`관리 기록: ${valueText(candidate)}`}));
    }
    return element("tr",{},element("td",{},selectInstitution(event.institution.id,event.institution.name)),institutionCell(event.institution,`${event.title}${event.audience ? " · "+event.audience : ""}${event.campus_label ? " · "+event.campus_label : ""}`),schedule,
      element("td",{class:"venue-cell",text:p.calendar_venue || "장소 미확인"}),summary,
      element("td",{},button("근거·상세 보기",()=>task(()=>eventDetail(event.id)),"detail-link"),element("br"),button("직접 확인·정정",()=>task(()=>eventDetail(event.id,true)),"detail-link")));
  })));
}

function seasonForm(existing) {
  const defaults = existing || state.settings.season || {};
  const year = defaults.academic_year || Number(new Intl.DateTimeFormat("en",{year:"numeric",timeZone:"Asia/Seoul"}).format(new Date()));
  const inputs = {
    label:control("시즌 이름","label",{value:defaults.label || `${year}학년도 겨울 졸업 시즌`,required:true,full:true}),
    academic_year:control("학년도","academic_year",{type:"number",value:year,required:true,min:2000,max:2100,disabled:!!existing,hint:"예: 2027년 1~2월 졸업식은 대개 2026학년도입니다."}),
    export_key:control("저장 파일 구분 이름","export_key",{value:defaults.export_key || `${year}-winter`,required:true,pattern:"[a-z0-9][a-z0-9_-]{0,63}",disabled:!!existing,hint:"영문 소문자·숫자·하이픈만 사용합니다."}),
    search_start:control("조사 시작 날짜","search_start",{type:"date",value:defaults.search_start || `${year}-12-01`,required:true}),
    search_end:control("조사 종료 날짜","search_end",{type:"date",value:defaults.search_end || `${year+1}-03-31`,required:true}),
  };
  formShell(existing?"시즌 이름·기간 수정":"새 조사 시즌 만들기",[element("div",{class:"form-grid"},Object.values(inputs).map(x=>x.wrapper))],async()=>{
    const values=Object.fromEntries(Object.entries(inputs).map(([name,x])=>[name,x.input.value]));
    if(values.search_start>values.search_end) throw new Error("조사 종료 날짜는 시작 날짜 이후여야 합니다.");
    const body = existing ? {expected_revision:existing.revision,label:values.label,search_start:values.search_start,search_end:values.search_end} : {...values,academic_year:Number(values.academic_year),timezone:"Asia/Seoul",region_profile:"gwangju_5_gu"};
    const season=await api(existing?`/api/seasons/${existing.id}`:"/api/seasons",{method:existing?"PATCH":"POST",body,idem:!existing});
    await loadSeasons(season.id); $("#dialog").close(); await Promise.all([reloadOverview(),loadView()]);notify("시즌을 저장했습니다.");
  });
}

function institutionForm(existing) {
  const inputs={
    name:control("기관 이름","name",{value:existing?.name || "",required:true}),
    type:control("기관 종류","type",{choices:TYPES,value:existing?.type || "elementary",disabled:!!existing}),
    district:control("지역","district",{choices:DISTRICTS,value:existing?.district || "북구"}),
    address:control("주소","address",{value:existing?.address || "",required:true}),
    phone:control("전화번호","phone",{value:existing?.phone || "",type:"tel"}),
    homepage_url:control("공식 홈페이지","homepage_url",{value:existing?.homepage_url || "",type:"url",placeholder:"https://"}),
    campus_label:control("캠퍼스·분원 (선택)","campus_label",{value:existing?.campus_label || "",disabled:!!existing}),
  };
  const note=control("정정 이유 (선택)","note",{type:"textarea",full:true});
  const release=element("div",{class:"full-width"},element("p",{class:"hint",text:"직접 정정은 공식 목록 갱신으로 덮어쓰지 않습니다. 아래 항목을 선택하면 저장된 공식 원본값으로 돌아갑니다."}));
  const releases=[];
  if(existing) {
    for(const field of ["name","address","phone","homepage_url","district"]) {
      const labels={name:"기관 이름",address:"주소",phone:"전화번호",homepage_url:"홈페이지",district:"지역"};
      const box=element("input",{type:"checkbox",value:field,onchange:()=>inputs[field].input.disabled=box.checked});
      releases.push(box); release.append(element("label",{class:"check-label"},box,`${labels[field]} 공식 원본값 사용`));
    }
  }
  formShell(existing?"기관 정보 정정":"조사할 기관 추가",[element("div",{class:"form-grid"},Object.values(inputs).map(x=>x.wrapper),existing?[note.wrapper,release]:[])],async()=>{
    const values=Object.fromEntries(Object.entries(inputs).map(([key,x])=>[key,x.input.value.trim() || null]));
    let body=values;
    if(existing) {
      const releaseFields=releases.filter(input=>input.checked).map(input=>input.value);
      const set={}; for(const key of ["name","address","phone","homepage_url","district"]) if(!releaseFields.includes(key)&&values[key]!==existing[key])set[key]=values[key];
      body={expected_revision:existing.revision,set,release_fields:releaseFields,note:note.input.value};
    }
    const result=await api(existing?`/api/institutions/${existing.id}`:"/api/institutions",{method:existing?"PATCH":"POST",body,idem:!existing});
    $("#dialog").close(); await Promise.all([reloadOverview(),loadView()]);notify(`${result.name} 기관 정보를 저장했습니다.`);
  });
}

async function allInstitutions() {
  let cursor=null, result=[];
  do {
    const page=await api(`/api/institutions?limit=500${cursor?`&cursor=${encodeURIComponent(cursor)}`:""}`);
    result.push(...page.items);cursor=page.next_cursor;
  } while(cursor);
  return result;
}

function localDateTime(value) {
  if(!value)return "";
  const parsed = new Date(value);
  if(Number.isNaN(parsed.getTime()))return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(parsed).map(p=>[p.type,p.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function manualEditor(field, projection, documents=[], fresh=false) {
  const manual=projection?.manual || {}, automatic=projection?.automatic || {};
  const box=element("section",{class:"field-card"});
  const action=control("이번에 할 일",`${field}-action`,{choices:fresh?{skip:"입력하지 않음",set:"직접 확인값 저장"}:{keep:"현재 기록 유지",set:"직접 확인값 저장",clear:"빈값으로 보호",release:"수기 보호 해제 · 자동값 사용",acknowledge:"차이 알림 읽음"},value:fresh?(field==="ceremony_status"?"set":"skip"):"keep"});
  box.append(element("h3",{text:FIELDS[field]}),action.wrapper);
  if(projection) {
    box.append(element("div",{class:"comparison"},
      element("div",{},element("small",{class:"muted",text:"저장된 직접 입력"}),element("div",{class:"field-value",text:manual.mode==="clear"?"빈값 보호":valueText(manual.value)})),
      element("div",{},element("small",{class:"muted",text:"자동 조사 기록 · 후보일 수 있음"}),element("div",{class:"field-value",text:valueText(automatic.value)}))
    ));
    if(automatic.conflict_detected_at)box.append(element("p",{class:"hint",text:`현재 충돌 최초 감지: ${moment(automatic.conflict_detected_at)}`}));
  }
  const grid=element("div",{class:"form-grid field-editor",hidden:action.input.value!=="set"});
  let value,precision,date,month;
  if(field==="event_date") {
    precision=control("확인한 날짜 범위","precision",{choices:{day:"정확한 날짜",month:"월 계획만 확인"},value:manual.value?.precision || "day"});
    date=control("졸업식 날짜","date",{type:"date",value:manual.value?.date || ""});
    month=control("졸업식 예정 월","month",{type:"month",value:manual.value?.month || "",hint:"월 계획은 일반 일정표와 판매 준비 목록에 표시하지 않습니다."});
    const toggle=()=>{date.wrapper.hidden=precision.input.value!=="day";month.wrapper.hidden=precision.input.value!=="month";};precision.input.addEventListener("change",toggle);toggle();
    grid.append(precision.wrapper,date.wrapper,month.wrapper);
  } else {
    value=control(field==="ceremony_status"?"행사 상태":FIELDS[field],`${field}-value`,{type:field==="start_time"?"time":"text",choices:field==="ceremony_status"?CEREMONY:undefined,value:manual.value || (field==="ceremony_status"?"scheduled":""),hint:field==="start_time"?"학생 집합·리허설·입장 시간이 아닌 졸업식의 실제 시작 시각입니다.":undefined});
    grid.append(value.wrapper);
  }
  const bound = ["start_time","venue"].includes(field)?control("이 값이 적용되는 졸업식 날짜","bound_date",{type:"date",value:manual.bound_date || "",hint:"확인한 일 단위 날짜와 연결해야 합니다. 날짜 변경 시 과거 시각을 자동으로 옮기지 않습니다."}):null;
  if(bound)grid.append(bound.wrapper);
  const basis=control("어떻게 확인했나요?","basis",{choices:{phone_confirmed:"전화로 직접 확인",document_confirmed:"문서를 직접 확인",user_recorded:"확인되지 않은 직접 기록"},value:manual.basis || "phone_confirmed"});
  const confirmed=control("실제로 확인한 시각 (한국 시간)","confirmed_at",{type:"datetime-local",value:localDateTime(manual.confirmed_at),hint:"모르면 비워 두세요. 입력 시각으로 대신 채우지 않으며 관리 기록으로만 저장합니다."});
  const url=control("직접 확인한 공개 문서 URL (선택)","source_url",{type:"url",value:manual.source_url || "",placeholder:"https://"});
  const document=control("확인한 저장 문서 버전 (선택)","source_document_id",{choices:{"":"종이·오프라인 또는 저장 버전 지정 없음",...Object.fromEntries(documents.map(doc=>[doc.id,`${doc.title || "문서"} · ${moment(doc.retrieved_at)}`]))},value:manual.source_document_id || "",hint:"실제로 확인한 버전만 선택하세요."});
  const note=control("확인 메모","note",{type:"textarea",value:manual.note || "",full:true,hint:"일반 확인에서는 선택 사항입니다. 충돌을 재확인한 경우 근거 메모가 필요합니다. 메모는 HTML 기본 저장에서 제외됩니다."});
  grid.append(basis.wrapper,confirmed.wrapper,url.wrapper,document.wrapper,note.wrapper);
  const resolve=element("input",{type:"checkbox"});
  if(automatic.fingerprint && (projection?.has_web_difference || projection?.review_required || automatic.status==="conflict")) {
    grid.append(element("label",{class:"check-label full-width"},resolve,"현재 충돌을 직접 재확인하여 이 값으로 해결합니다."));
    grid.append(element("p",{class:"hint full-width",text:"전화 또는 문서를 실제로 다시 확인한 시각과 근거 메모가 필요합니다. 알림 읽음만으로 충돌이 해결되지는 않습니다."}));
  }
  const other=control("처리 메모 (선택)","action_note",{type:"textarea",full:true});
  other.wrapper.hidden=true;
  const explanation=element("p",{class:"hint",hidden:true});
  action.input.addEventListener("change",()=>{
    const selected=action.input.value;
    grid.hidden=selected!=="set";other.wrapper.hidden=["keep","skip","set"].includes(selected);
    explanation.hidden=["keep","skip","set"].includes(selected);
    explanation.textContent=({clear:"빈값 보호는 자동 조사가 값을 채우지 못하도록 비워 둡니다. 기존 기록은 이력에 보존합니다.",release:"직접 입력 보호를 해제한 뒤 저장된 자동 근거로 다시 판정합니다. 이전 수기 기록은 이력에 남습니다.",acknowledge:"차이 알림을 읽었다고 기록합니다. 충돌 해결이나 일정표 사용 재개가 아닙니다."})[selected] || "";
  });
  box.append(grid,explanation,other.wrapper);
  return {node:box,operation:()=>{
    const selected=action.input.value;
    if(["keep","skip"].includes(selected))return null;
    if(selected!=="set")return {field,action:selected,note:other.input.value};
    let selectedValue=value?.input.value;
    if(field==="event_date")selectedValue=precision.input.value==="day"?{precision:"day",date:date.input.value,month:null}:{precision:"month",date:null,month:month.input.value};
    if(field==="event_date" ? !(selectedValue.date || selectedValue.month) : !selectedValue)throw new Error(`${FIELDS[field]} 값을 입력하거나 '현재 기록 유지/입력하지 않음'을 선택하세요.`);
    if(field==="start_time" && !bound.input.value)throw new Error("실제 시작 시각이 적용되는 졸업식 날짜를 입력하세요.");
    const op={field,action:"set",value:selectedValue,basis:basis.input.value,confirmed_at:confirmed.input.value?`${confirmed.input.value}:00+09:00`:null,note:note.input.value,resolves_fingerprint:resolve.checked?automatic.fingerprint:null,source_url:url.input.value.trim() || null,source_document_id:document.input.value || null};
    if(bound)op.bound_date=bound.input.value || null;
    if(resolve.checked && (!op.confirmed_at || !op.note.trim() || op.basis==="user_recorded"))throw new Error("충돌 재확인은 전화·문서 확인 방법, 실제 확인 시각과 근거 메모가 모두 필요합니다.");
    return op;
  }};
}

async function eventForm(institution) {
  await task(async()=>{
    const season=currentSeason();
    const institutions=institution?[institution]:await allInstitutions();
    if(!institutions.length){institutionForm();notify("먼저 조사할 기관을 등록해 주세요.");return;}
    const institutionInput=control("교육기관","institution_id",{choices:Object.fromEntries(institutions.map(i=>[i.id,`${i.name} · ${i.district}`])),value:institution?.id || institutions[0].id,full:true});
    const title=control("행사 이름","title",{value:"졸업식",required:true});
    const eventType=control("행사 구분","event_type",{choices:EVENT_TYPES,value:"graduation_ceremony"});
    const audience=control("대상·학과 (선택)","audience",{});
    const campus=control("캠퍼스·분원 (선택)","campus_label",{value:institution?.campus_label || ""});
    const separate=element("input",{type:"checkbox"});
    const ceremony=control("행사 상태","ceremony_status",{choices:CEREMONY,value:"scheduled"});
    const precision=control("확인한 날짜 범위","date_precision",{choices:{day:"정확한 날짜",month:"월 계획만 확인",unknown:"날짜 미확인"},value:"day"});
    const date=control("졸업식 날짜","event_date",{type:"date"});
    const month=control("졸업식 예정 월","event_month",{type:"month"});
    const time=control("실제 시작 시각 (모르면 비움)","start_time",{type:"time",hint:"학생 집합·리허설·입장 시각이 아닌 행사 시작 시각입니다."});
    const venue=control("장소 (모르면 비움)","venue",{});
    const basis=control("어떻게 확인했나요?","basis",{choices:{phone_confirmed:"전화로 직접 확인",document_confirmed:"문서를 직접 확인",user_recorded:"확인되지 않은 직접 기록"},value:"phone_confirmed"});
    const confirmed=control("실제로 확인한 시각 (한국 시간)","confirmed_at",{type:"datetime-local",hint:"모르면 비워 두세요. 입력한 시각으로 대신 채우지 않습니다."});
    const source=control("직접 확인한 공개 문서 URL (선택)","source_url",{type:"url",placeholder:"https://",full:true});
    const memo=control("확인 메모 (선택)","note",{type:"textarea",full:true,hint:"담당 부서와 확인한 내용을 적어 두세요. 메모는 HTML 기본 저장과 모델 전송에서 제외됩니다."});
    const toggleDate=()=>{
      date.wrapper.hidden=precision.input.value!=="day";month.wrapper.hidden=precision.input.value!=="month";
      time.input.disabled=precision.input.value!=="day";
    };precision.input.addEventListener("change",toggleDate);toggleDate();
    const note=element("div",{class:"notice",text:"전화·문서를 직접 확인한 경우 실제 확인 시각을 입력해 주세요. 시점을 모르는 기록이나 월 계획은 저장하되 확정 일정으로 사용하지 않습니다. 날짜와 시각은 각각 같은 행사인지 확인해 주세요."});
    formShell("직접 확인한 졸업식 입력",[note,element("div",{class:"form-grid"},institutionInput.wrapper,title.wrapper,eventType.wrapper,audience.wrapper,campus.wrapper,
      ceremony.wrapper,precision.wrapper,date.wrapper,month.wrapper,time.wrapper,venue.wrapper,basis.wrapper,confirmed.wrapper,source.wrapper,memo.wrapper,
      element("label",{class:"check-label full-width"},separate,"같은 기관·시즌에 있는 기존 행사와 별개의 행사로 등록합니다."))],async()=>{
      const common={action:"set",basis:basis.input.value,confirmed_at:confirmed.input.value?`${confirmed.input.value}:00+09:00`:null,note:memo.input.value,resolves_fingerprint:null,source_url:source.input.value.trim() || null,source_document_id:null};
      const operations=[{...common,field:"ceremony_status",value:ceremony.input.value}];
      const exactDate=precision.input.value==="day"?date.input.value:null;
      if(precision.input.value==="day" && exactDate)operations.push({...common,field:"event_date",value:{precision:"day",date:exactDate,month:null}});
      if(precision.input.value==="month" && month.input.value)operations.push({...common,field:"event_date",value:{precision:"month",date:null,month:month.input.value}});
      if(precision.input.value==="day" && time.input.value) {
        if(!exactDate)throw new Error("실제 시작 시각이 적용되는 졸업식 날짜를 먼저 입력하세요.");
        operations.push({...common,field:"start_time",value:time.input.value,bound_date:exactDate});
      }
      if(venue.input.value.trim())operations.push({...common,field:"venue",value:venue.input.value.trim(),bound_date:exactDate || null});
      const event=await api("/api/events",{method:"POST",idem:true,body:{institution_id:institutionInput.input.value,season_id:season.id,title:title.input.value,event_type:eventType.input.value,audience:audience.input.value || null,campus_label:campus.input.value || null,allow_separate_event:separate.checked,operations}});
      $("#dialog").close();await Promise.all([reloadOverview(),loadView()]);notify("직접 확인한 내용을 저장하고 게시 가능 여부를 다시 판정했습니다.");await eventDetail(event.id);
    });
  });
}

async function eventDetail(id, edit=false) {
  const detail=await api(`/api/events/${id}`), event=detail.event;
  const content=element("div",{});
  const body=element("div",{});
  const tabs=element("div",{class:"modal-subtabs"});
  const select=key=>{
    tabs.querySelectorAll("button").forEach(tab=>tab.classList.toggle("active",tab.dataset.key===key));
    body.replaceChildren(key==="manual"?manualDetailForm(detail):key==="metadata"?metadataForm(detail):key==="history"?historyDetail(detail):evidenceDetail(detail));
  };
  for(const [key,label] of [["evidence","확인값·근거"],["manual","직접 확인·수기 관리"],["metadata","행사 제목·대상 수정"],["history","변경 이력"]])tabs.append(element("button",{type:"button",class:"quiet",text:label,"data-key":key,onclick:()=>select(key)}));
  content.append(element("p",{class:"muted",text:`${event.institution.name} · ${event.title}${event.audience ? " · 대상 "+event.audience : ""}${event.campus_label ? " · "+event.campus_label : ""} · 기록 버전 ${event.revision}`}),tabs,body);
  openDialog("졸업식 기록과 근거",content);select(edit?"manual":"evidence");
}

function evidenceDetail(detail) {
  const event=detail.event,p=event.publication;
  const content=element("div",{},element("div",{class:"notice",text:p.sales_ready?"날짜와 실제 시작 시각을 현재 판매 준비에 사용할 수 있습니다.":p.calendar_eligible?"일반 일정표에 확인값이 남아 있습니다. 현재 사용할 수 있는 항목과 보류 항목을 구분해 주세요.":"이 기록은 현재 일반 일정표와 판매 준비 목록에 게시되지 않습니다."}),reasons(p.reason_codes));
  for(const [field,label] of Object.entries(FIELDS)) {
    const projection=event.fields[field],permission=p.field_permissions[field];
    const published=({event_date:p.calendar_date,start_time:p.calendar_time,venue:p.calendar_venue})[field];
    const card=element("section",{class:"field-card"},element("h3",{},label,badge(permission.current_allowed?"현재 사용 가능":permission.general_visible?"과거 확인값":"사용 보류",permission.current_allowed?"":"warning")));
    card.append(element("div",{class:"field-value",text:field==="ceremony_status"?valueText(projection.effective_value):published || "일정표 확인값 없음"}));
    const dt=(label,value)=>[element("dt",{text:label}),element("dd",{text:value})];
    card.append(element("dl",{class:"field-data"},
      dt("확인 방법",BASES[projection.effective_basis] || projection.effective_basis),
      dt("실제 확인",moment(permission.confirmed_at)),
      dt("재확인 상태",({not_attempted:"재확인 미수행",succeeded:"재확인 성공",failed:"실제 재확인 실패",not_applicable:"재확인 대상 아님"})[permission.recheck_status] || permission.recheck_status),
      dt("자동 조사 기록",`${valueText(projection.automatic?.value)} · ${AUTO_STATUS[projection.automatic?.status] || "미조사"}`),
      dt("직접 입력 기록",projection.manual?`${valueText(projection.manual.value)} · ${projection.manual.mode==="clear"?"빈값 보호":BASES[projection.manual.basis]}`:"없음"),
      projection.manual?.bound_date?dt("연결된 날짜",projection.manual.bound_date):[],
      projection.manual?.note?dt("사적 확인 메모",projection.manual.note):[]
    ),reasons([...permission.reason_codes,...projection.reason_codes]));
    const check=projection.automatic?.value_check;
    if(check)card.append(element("details",{},element("summary",{text:"서버 검증 기록"}),element("p",{class:"hint",text:`규칙 ${check.rule_id || "없음"} · 버전 ${check.rule_version || "없음"} · 결과 ${check.status || "기록 참조"}`}),element("pre",{class:"inline-code pre-wrap",text:JSON.stringify(check,null,2)})));
    content.append(card);
  }
  content.append(element("h3",{text:"확인에 사용하거나 보류한 공개 근거"}));
  const documents=new Map(detail.documents.map(doc=>[doc.id,doc]));
  for(const observation of detail.observations) {
    const candidate=observation.candidate,doc=documents.get(observation.document_id);
    const status=({pending:"검토 대기",accepted:"채택",reference:"참고 후보",rejected:"기각"})[observation.disposition] || observation.disposition;
    const card=element("article",{class:"evidence-card"},element("h4",{},candidate.event_title," ",badge(status,observation.disposition==="accepted"?"":"warning")),element("p",{text:`후보 날짜 ${valueText(candidate.event_date)} · 후보 시각 ${candidate.start_time || "미확인"} · ${EVENT_TYPES[candidate.event_type] || candidate.event_type}`}),reasons(observation.reason_codes));
    if(doc)card.append(sourceLink(doc.display_url,doc.title || "근거 원문"),element("p",{text:`자료 취득 ${moment(doc.retrieved_at)} · ${doc.authority==="official"?"공식성 확인":"공식성 미확인"} · ${doc.source_kind}`}),element("p",{text:`읽은 범위: ${doc.read_scope || "미기록"}${doc.partial?" · 일부 문서":""}`}));
    const quotes=new Set([...candidate.context_evidence,...candidate.date_evidence,...candidate.time_evidence,...candidate.venue_evidence].map(e=>e.quote).filter(Boolean));
    for(const quote of quotes)card.append(element("blockquote",{text:quote}));
    content.append(card);
  }
  if(!detail.observations.length)content.append(element("p",{class:"muted",text:"자동 조사 관측이 없습니다. 직접 확인한 내용은 위 항목과 변경 이력에 저장됩니다."}));
  for(const doc of detail.documents)content.append(element("details",{class:"evidence-card"},element("summary",{text:`저장 문서 · ${doc.title || doc.source_kind}`}),sourceLink(doc.display_url),element("p",{text:`취득 ${moment(doc.retrieved_at)} · 읽기 상태 ${doc.status} · 재확인 ${doc.recheck?.status || "미상"}`}),element("p",{text:`읽은 범위: ${doc.read_scope || "미기록"}`}),element("p",{class:"inline-code",text:`문서 버전 ${doc.id} · hash ${doc.content_hash || "없음"}`}),reasons(doc.reason_codes)));
  return content;
}

function inlineForm(children, submitLabel, submitAction) {
  const form=element("form",{}),error=element("div",{class:"form-error",hidden:true,role:"alert"}),submit=element("button",{type:"submit",class:"primary",text:submitLabel});
  form.append(...children,error,element("div",{class:"form-actions"},submit));
  form.addEventListener("submit",async ev=>{
    ev.preventDefault();if(!form.reportValidity())return;
    error.hidden=true;submit.disabled=true;
    try{await submitAction();}catch(reason){error.textContent=errorMessage(reason);error.hidden=false;}finally{submit.disabled=false;}
  });
  return form;
}

function manualDetailForm(detail) {
  const event=detail.event,editors=Object.keys(FIELDS).map(field=>manualEditor(field,event.fields[field],detail.documents));
  return inlineForm([element("div",{class:"notice",text:"변경할 항목의 '이번에 할 일'을 선택하세요. 저장 원본은 자동 조사로 덮어쓰지 않지만, 충돌이나 근거 무효가 있으면 일정표 사용이 보류될 수 있습니다."}),...editors.map(editor=>editor.node)],"변경 내용 저장",async()=>{
    const operations=editors.map(editor=>editor.operation()).filter(Boolean);
    if(!operations.length)throw new Error("변경할 항목을 하나 이상 선택하세요.");
    await api(`/api/events/${event.id}/overrides`,{method:"PATCH",body:{expected_revision:event.revision,operations}});
    await Promise.all([reloadOverview(),loadView()]);notify("직접 입력과 보호 설정을 저장했습니다.");await eventDetail(event.id);
  });
}

function metadataForm(detail) {
  const event=detail.event,inputs={
    title:control("행사 이름","title",{value:event.title,required:true}),
    event_type:control("행사 구분","event_type",{choices:EVENT_TYPES,value:event.event_type}),
    audience:control("대상·학과","audience",{value:event.audience || ""}),
    campus_label:control("캠퍼스·분원","campus_label",{value:event.campus_label || ""}),
  };
  return inlineForm([element("div",{class:"form-grid"},Object.values(inputs).map(x=>x.wrapper))],"행사 정보 저장",async()=>{
    const body={expected_revision:event.revision,...Object.fromEntries(Object.entries(inputs).map(([key,x])=>[key,x.input.value || null]))};
    await api(`/api/events/${event.id}`,{method:"PATCH",body});
    await Promise.all([reloadOverview(),loadView()]);notify("행사 제목과 대상을 저장했습니다.");await eventDetail(event.id);
  });
}

function historyDetail(detail) {
  return element("div",{},element("p",{class:"hint",text:"입력 시각과 실제 확인 시각은 서로 다릅니다. 과거 입력과 조사 기록은 삭제하지 않고 보존합니다."}),
    detail.history.length?element("ol",{class:"history-list"},detail.history.map(row=>element("li",{},element("strong",{text:`${moment(row.recorded_at)} · ${row.actor==="user"?"직접 입력":"자동 조사"}`}),element("div",{text:`${FIELDS[row.field] || row.field || "행사"} · ${row.action}`}),element("div",{class:"pre-wrap",text:row.summary})))):element("p",{text:"아직 변경 이력이 없습니다."}));
}

async function startScan(ids) {
  const season=currentSeason();
  const body={kind:"schedule_scan",season_id:season.id,mode:$("#scan-mode").value,selection:ids?{scope:"ids",institution_ids:ids}:{scope:"all"},limits:null};
  if(ids && !ids.length)throw new Error("조사할 기관을 하나 이상 선택하세요.");
  await api("/api/jobs",{method:"POST",body,idem:true});
  state.view="jobs";await loadView();notify(ids?`선택한 ${ids.length}개 기관의 조사를 시작했습니다.`:"시즌 전체 대상 기관의 조사를 시작했습니다. 화면에 보이는 페이지로 대상을 제한하지 않습니다.");
}

function jobCard(job) {
  const complete=(job.counts.done || 0)+(job.counts.failed || 0)+(job.counts.cancelled || 0);
  const card=element("article",{class:"job-card"},
    element("div",{class:"job-title"},element("h3",{text:job.kind==="registry_sync"?"공식 기관 목록 가져오기":"졸업식 일정 조사"}),badge(JOB_STATUS[job.status] || job.status,["paused","failed"].includes(job.status)?"warning":"")),
    element("p",{text:`${state.seasons.find(s=>s.id===job.season_id)?.label || "시즌 공통 목록"} · 시작 ${moment(job.created_at)} · 고정 대상 ${job.target_count}개 · 처리 ${complete}개`}),
    element("progress",{max:Math.max(job.target_count,1),value:complete,"aria-label":"기관 처리 진행률"}),
    element("div",{class:"progress-label",text:`완료 ${job.counts.done} · 실패 ${job.counts.failed} · 대기 ${job.counts.pending} · 조사 중 ${job.counts.running} · 취소 ${job.counts.cancelled}`}),
    job.paused_reason?reasons([job.paused_reason]):null
  );
  const usage=job.usage || {};
  card.append(element("p",{text:"지금까지 누적 사용량 (재개해도 초기화하지 않습니다)"}),element("div",{class:"usage"},
    [["active_seconds","실행 초"],["search_calls","검색"],["document_fetches","문서"],["http_requests","HTTP"],["llm_calls","모델 호출"],["tokens","토큰"]].map(([key,label])=>element("span",{text:`${label} ${Math.round(usage[key] || 0).toLocaleString()}`}))
  ));
  const actions=element("div",{class:"button-row"},button("기관별 결과",()=>task(()=>jobItems(job)),"quiet"));
  if(["queued","running"].includes(job.status))actions.append(button("잠시 멈춤",()=>task(async()=>{await api(`/api/jobs/${job.id}/pause`,{method:"POST"});await loadView();notify("현재 처리를 안전하게 마친 뒤 멈춥니다.");})));
  if(job.status==="paused") {
    actions.append(button("이어서 조사",()=>task(async()=>{await api(`/api/jobs/${job.id}/resume`,{method:"POST",idem:true,body:{expected_revision:job.revision,increase_limits:null}});await loadView();notify("기존 대상과 누적 사용량을 유지하며 재개했습니다.");}),"primary"));
    actions.append(button("누적 한도 늘려 재개",()=>resumeLimits(job)));
  }
  if(["queued","running","pausing","paused"].includes(job.status))actions.append(button("작업 취소",()=>confirmCancel(job),"danger"));
  card.append(actions);
  return card;
}

function confirmCancel(job) {
  formShell("조사 작업 취소",[element("p",{text:"이미 조사한 결과와 기록은 보존합니다. 남은 대상을 취소한 뒤 이 작업은 다시 재개할 수 없습니다. 잠시 중단하려면 '닫기' 후 '잠시 멈춤'을 이용하세요."})],async()=>{
    await api(`/api/jobs/${job.id}/cancel`,{method:"POST"});$("#dialog").close();await loadView();notify("취소 요청을 저장했습니다. 처리 중인 기관을 안전하게 정리합니다.");
  },"취소하고 결과 보존");
}

function resumeLimits(job) {
  const fields={job_max_active_seconds:"누적 실행 시간 (초)",job_max_search_calls:"검색 호출",job_max_document_fetches:"문서 읽기",job_max_http_requests:"HTTP 요청",job_max_llm_calls:"모델 호출",job_max_tokens:"모델 토큰",job_max_browser_reads:"브라우저 읽기"};
  const controls=Object.entries(fields).map(([key,label])=>({key,...control(label,key,{type:"number",min:1,value:job.limits[key],hint:"기존 한도보다 큰 값을 입력하면 해당 한도를 늘립니다."})}));
  formShell("조사 누적 한도 조정",[element("p",{class:"notice",text:"새 실행 구간에서도 지금까지 사용한 누적량은 유지합니다. 외부 검색·모델을 사용하면 설정된 서비스의 사용량이 발생할 수 있습니다."}),element("div",{class:"form-grid"},controls.map(x=>x.wrapper))],async()=>{
    const increase={};for(const input of controls){const n=Number(input.input.value);if(n>job.limits[input.key])increase[input.key]=n;}
    if(!Object.keys(increase).length)throw new Error("늘릴 누적 한도를 하나 이상 입력하세요.");
    await api(`/api/jobs/${job.id}/resume`,{method:"POST",idem:true,body:{expected_revision:job.revision,increase_limits:increase}});
    $("#dialog").close();await loadView();notify("누적 한도를 늘리고 조사를 재개했습니다.");
  },"한도 늘려 이어서 조사");
}

async function jobItems(job) {
  let cursor=null;
  const institutions=new Map((await allInstitutions()).map(i=>[i.id,i.name]));
  const content=element("div",{},element("p",{class:"hint",text:`작업 대상은 시작할 때 ${job.target_count}개로 고정했습니다. 이후 추가한 기관은 새 작업에서 조사합니다.`}));
  const list=element("div",{}),more=button("다음 기관 더 보기",()=>task(()=>load()));content.append(list,more);
  const load=async()=>{
    const page=await api(`/api/jobs/${job.id}/items?limit=100${cursor?`&cursor=${encodeURIComponent(cursor)}`:""}`);
    for(const item of page.items) {
      const data=item.result_summary || item.summary;
      list.append(element("article",{class:"evidence-card"},element("h4",{text:institutions.get(item.institution_id) || item.target_ref || "기관 처리"}),
        badge(({pending:"대기",running:"처리 중",done:"완료",failed:"실패",cancelled:"취소"})[item.status] || item.status,item.status==="failed"?"warning":""),
        element("p",{text:`결과: ${{verified:"확인값 확보",needs_review:"추가 확인 필요",no_data:"공개 정보 없음",blocked:"진행 차단",limit_reached:"한도 도달",error:"오류"}[item.outcome] || "처리 전"} · 시도 ${item.attempt}회 · 최근 ${moment(item.last_attempt_at)}`}),
        reasons(item.reason_codes || []),element("p",{class:"pre-wrap",text:typeof data==="string"?data:data?JSON.stringify(data):""}),
        item.error?element("p",{text:item.error.message || String(item.error)}):null));
    }
    cursor=page.next_cursor;more.hidden=!cursor;
  };
  openDialog("기관별 조사 결과",content);await load();
}

function registryNote(text) {
  return text.replaceAll("as_of는 null이다","기준일은 미상으로 남긴다").replaceAll("coverage_complete=false로 적재한다","전체 확보로 표시하지 않는다").replaceAll("서버 검증:","원문 대조:");
}

async function renderRegistry() {
  const page=await api("/api/registry/snapshots?limit=500"),container=$("#registry-status");
  container.replaceChildren(element("h3",{text:"공식 목록 확보 이력"}));
  if(!page.items.length){container.append(element("p",{class:"muted",text:"등록된 공식 목록이 없습니다. 기관을 직접 추가해도 전체 목록 확보로 간주하지 않습니다."}));return;}
  for(const snapshot of page.items) {
    const source=snapshot.source;
    container.append(element("article",{class:"registry-card"},element("h3",{},source.source_name," ",badge(source.coverage_complete?"해당 범위 전체 확보":"일부 목록","neutral")),
      element("p",{text:`기준일 ${source.as_of || "미상"} · 취득 ${moment(snapshot.retrieved_at)}`}),
      element("p",{text:`${source.scope_types.map(type=>TYPES[type] || type).join(", ")} · ${source.scope_districts.join(", ")}`}),
      element("p",{text:`등록 ${snapshot.loaded_count} · 명부 연결 ${snapshot.linked_count} · 제외 ${snapshot.rejected_count} · 명부 전체 행수 ${source.expected_total ?? "미상"}`}),
      element("p",{text:registryNote(source.completeness_note)}),sourceLink(source.official_url,"공식 목록 출처")));
  }
}

function syncRegistry() {
  const checkboxes=Object.entries({neis:"NEIS · 초·중·고·특수·기타학교",kindergarten:"유치원 공식 목록",childcare:"어린이집 공식 목록"}).map(([value,label])=>{
    const input=element("input",{type:"checkbox",value,checked:value==="neis"});return {input,node:element("label",{class:"check-label"},input,label)};
  });
  formShell("공식 API로 기관 목록 가져오기",[element("p",{class:"notice",text:"설정된 공식 목록 API를 호출합니다. 제공자별 키나 연결이 없으면 해당 항목의 오류를 남깁니다. 대학·전문대학 등은 공식 목록 파일 가져오기를 이용하세요."}),...checkboxes.map(x=>x.node)],async()=>{
    const providers=checkboxes.filter(x=>x.input.checked).map(x=>x.input.value);
    if(!providers.length)throw new Error("가져올 공식 목록을 선택하세요.");
    await api("/api/jobs",{method:"POST",idem:true,body:{kind:"registry_sync",providers,limits:null}});
    $("#dialog").close();state.view="jobs";await loadView();notify("공식 기관 목록 가져오기를 시작했습니다.");
  },"목록 가져오기");
}

function parseCSV(text) {
  const rows=[];let row=[],field="",quoted=false;
  for(let i=0;i<text.length;i++) {
    const c=text[i];
    if(c==='"') {if(quoted && text[i+1]==='"'){field+='"';i++;}else quoted=!quoted;}
    else if(c==="," && !quoted){row.push(field);field="";}
    else if((c==="\n" || c==="\r")&&!quoted){if(c==="\r"&&text[i+1]==="\n")i++;row.push(field);if(row.some(v=>v.trim()))rows.push(row);row=[];field="";}
    else field+=c;
  }
  if(quoted)throw new Error("CSV 따옴표가 닫히지 않았습니다. 원본 파일을 확인해 주세요.");
  row.push(field);if(row.some(v=>v.trim()))rows.push(row);
  if(rows.length<2)throw new Error("CSV에 열 이름과 기관 행이 필요합니다.");
  return rows;
}

function registryImport() {
  const file=control("공식 목록 파일","file",{type:"file",required:true,accept:".json,.csv",full:true});
  const encoding=control("CSV 문자 인코딩","encoding",{choices:{"utf-8":"UTF-8 (기본)","euc-kr":"한글 Windows / EUC-KR"},value:"utf-8",hint:"한글이 깨지는 CSV는 EUC-KR을 선택하세요."});
  formShell("공식 기관 목록 파일 가져오기",[element("p",{text:"정규 형식 JSON 또는 CSV 파일을 읽고 저장 전에 기관 수·누락·중복을 확인합니다. 원본 파일은 변경하지 않습니다."}),file.wrapper,encoding.wrapper],async()=>{
    const selected=file.input.files[0];
    if(!selected)throw new Error("파일을 선택하세요.");
    if(selected.size>20*1024*1024)throw new Error("파일은 20MB 이하로 준비해 주세요.");
    const bytes=await selected.arrayBuffer();
    const text=new TextDecoder(encoding.input.value).decode(bytes).replace(/^\uFEFF/,"");
    if(selected.name.toLowerCase().endsWith(".json")) {
      let payload;try{payload=JSON.parse(text);}catch{throw new Error("JSON 형식을 읽을 수 없습니다. 정규 기관 목록 파일을 확인하세요.");}
      await previewRegistry(payload);
    } else {
      const rows=parseCSV(text),digest=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes))).map(b=>b.toString(16).padStart(2,"0")).join("");
      csvMapping(rows,digest,selected.name);
    }
  },"파일 읽고 미리보기");
}

async function preparedRegistry() {
  const result=await api("/api/registry/official-files");
  const choices=Object.fromEntries(result.items.map(item=>[item.filename,`${item.source.source_name} · ${item.row_count}곳`]));
  const choice=control("가져올 공식 명부","official_bundle",{choices,value:result.items[0]?.filename,full:true});
  const note=element("p",{class:"notice"});
  const describe=()=>{
    const item=result.items.find(row=>row.filename===choice.input.value);
    note.textContent=`원문 기준일 ${item.source.as_of || "미상"} · ${registryNote(item.source.completeness_note)} 홈페이지 관계는 취득 당시 공식 명부로 확인했으며 졸업식 일정 확인과는 별개입니다.`;
  };
  choice.input.addEventListener("change",describe);describe();
  formShell("준비된 공식 명부 가져오기",[element("p",{text:"공식 원문과 기관 정보가 대조된 목록입니다. 저장 전 중복 기관과 범위를 확인합니다. 현재의 전체 기관 목록임을 보장하지 않습니다."}),choice.wrapper,note],async()=>{
    await previewRegistry(await api(`/api/registry/official-files/${encodeURIComponent(choice.input.value)}`));
  },"저장 전 미리보기");
}

function csvMapping(csv,hash,filename) {
  const headers=csv[0],records=csv.slice(1),choose={"":"선택하지 않음",...Object.fromEntries(headers.map((name,index)=>[String(index),name]))};
  const names={
    external_key:["기관코드","학교코드","어린이집코드","외부키","external_key"],name:["학교명","기관명","유치원명","어린이집명","대학명","name"],
    type:["기관유형","학교종류명","학교종류","유형","type"],district:["자치구","구","구명","행정구","district"],address:["주소","도로명주소","도로명주소(상세)","소재지도로명주소","address"],
    phone:["전화번호","대표전화번호","phone"],homepage_url:["홈페이지","홈페이지주소","홈페이지URL","homepage_url"],campus_label:["캠퍼스","분원","campus_label"],
  };
  const mapping={};for(const [key,aliases] of Object.entries(names)) {
    const index=headers.findIndex(header=>aliases.some(alias=>header.trim().toLowerCase()===alias.toLowerCase()));
    mapping[key]=control(({external_key:"기관 고유 코드",name:"기관 이름",type:"기관 종류",district:"지역",address:"주소",phone:"전화번호",homepage_url:"홈페이지",campus_label:"캠퍼스"})[key],key,{choices:choose,value:index<0?"":String(index)});
  }
  const sourceName=control("공식 목록 이름","source_name",{value:filename,required:true});
  const sourceURL=control("공식 목록 게시 URL","official_url",{type:"url",required:true,placeholder:"https://"});
  const asOf=control("목록 기준일 (모르면 비움)","as_of",{type:"date"});
  const defaultType=control("기관 종류 열이 없을 때","default_type",{choices:TYPES,value:"elementary"});
  const expected=control("공식 목록의 전체 기관 수 (모르면 비움)","expected_total",{type:"number",min:0});
  const completeness=control("전체 확보 여부의 근거·범위 설명","completeness_note",{type:"textarea",full:true});
  const complete=element("input",{type:"checkbox"});
  const scopes=Object.entries(TYPES).map(([type,label])=>({type,input:element("input",{type:"checkbox"}) ,label}));
  const districts=DISTRICTS.map(district=>({district,input:element("input",{type:"checkbox",checked:true})}));
  const grid=element("div",{class:"form-grid"},...Object.values(mapping).map(x=>x.wrapper),defaultType.wrapper);
  const scopeBox=element("div",{class:"full-width"},element("p",{text:"이 공식 목록이 다루는 기관 종류 (행이 없는 종류도 범위에 포함되면 선택)"}),element("div",{class:"button-row"},scopes.map(x=>element("label",{class:"check-label"},x.input,x.label))),element("p",{text:"공식 목록 대상 지역"}),element("div",{class:"button-row"},districts.map(x=>element("label",{class:"check-label"},x.input,x.district))));
  formShell("CSV 열 연결과 공식 출처 확인",[element("p",{class:"notice",text:`기관 행 ${records.length}개를 읽었습니다. 기관 종류는 한글 이름 또는 계약 영문 코드, 지역은 광주 5개 구로 읽습니다. 고유 코드가 없으면 파일 행 번호로 구분합니다. 기존 기관 중복은 다음 화면에서 검토합니다.`}),grid,element("hr",{class:"section-divider"}),element("div",{class:"form-grid"},sourceName.wrapper,sourceURL.wrapper,asOf.wrapper,expected.wrapper,scopeBox,completeness.wrapper,element("label",{class:"check-label full-width"},complete,"위 범위의 공식 전체 목록임을 확인했습니다."))],async()=>{
    if(mapping.name.input.value==="" || mapping.address.input.value==="")throw new Error("기관 이름과 주소 열을 연결하세요.");
    const get=(row,key)=>mapping[key].input.value===""?null:(row[Number(mapping[key].input.value)] || "").trim() || null;
    const typeAliases={...Object.fromEntries(Object.entries(TYPES).map(([k,v])=>[v,k])),"초등":"elementary","중등":"middle","고등":"high","대학":"university","전문대":"junior_college","특수":"special_school"};
    const rows=records.map((row,index)=>{
      const type=get(row,"type"),address=get(row,"address") || "",district=get(row,"district") || DISTRICTS.find(d=>address.includes(d)) || "";
      return {external_key:get(row,"external_key") || `row-${index+1}`,name:get(row,"name") || "",type:type?(typeAliases[type] || type):defaultType.input.value,district,address,phone:get(row,"phone"),homepage_url:get(row,"homepage_url"),campus_label:get(row,"campus_label"),operation_status:"unknown",operation_effective_date:null,operation_evidence_url:null};
    });
    const scopeTypes=scopes.filter(x=>x.input.checked).map(x=>x.type),scopeDistricts=districts.filter(x=>x.input.checked).map(x=>x.district);
    if(!scopeTypes.length || !scopeDistricts.length)throw new Error("공식 목록의 기관 종류와 지역 범위를 선택하세요.");
    if(complete.checked && (!completeness.input.value.trim() || !expected.input.value))throw new Error("전체 확보로 기록하려면 공식 전체 기관 수와 확보 근거를 입력하세요.");
    const source={provider:"official_file",source_name:sourceName.input.value,official_url:sourceURL.input.value,as_of:asOf.input.value || null,scope_types:scopeTypes,scope_districts:scopeDistricts,coverage_complete:complete.checked,completeness_note:completeness.input.value,expected_total:expected.input.value?Number(expected.input.value):null,source_hash:hash};
    await previewRegistry({schema_version:"4.0",source,rows});
  },"저장 전 미리보기");
}

async function previewRegistry(payload) {
  const preview=await api("/api/registry/imports/preview",{method:"POST",idem:true,body:payload});
  const mappings=[],content=element("div",{});
  content.append(element("div",{class:"notice",text:`받은 행 ${preview.received_count}개 · 저장 가능 ${preview.accepted_count}개 · 오류 ${preview.rejected_rows.length}개 · 중복 검토 ${preview.duplicate_candidates.length}개. 미리보기 유효기한 ${moment(preview.expires_at)}`}));
  if(preview.rejected_rows.length)content.append(element("h3",{text:"저장하지 못하는 행"}),element("ul",{class:"list-bullets"},preview.rejected_rows.map(row=>element("li",{text:`${row.row_index+1}번째 행: ${row.reason}`}))));
  const institutions=new Map((await allInstitutions()).map(i=>[i.id,i]));
  for(const duplicate of preview.duplicate_candidates) {
    const row=payload.rows[duplicate.row_index],choices={skip:"이 행 건너뛰기",create:"별개의 기관으로 추가",...Object.fromEntries(duplicate.institution_ids.map(id=>[`update:${id}`,`기존 기관 연결·갱신: ${institutions.get(id)?.name || id}`]))};
    const choice=control(`${duplicate.row_index+1}번째 행 · ${row?.name || "기관"}`,"decision",{choices,value:"skip",full:true});
    content.append(choice.wrapper);mappings.push({row_index:duplicate.row_index,input:choice.input});
  }
  const partial=element("input",{type:"checkbox"});
  if(preview.rejected_rows.length)content.append(element("label",{class:"check-label"},partial,"오류 행을 제외한 일부 목록을 저장합니다. 전체 확보 표시가 제한됩니다."));
  formShell("기관 목록 검토 후 저장",[content],async()=>{
    const result=await api(`/api/registry/imports/${preview.id}/commit`,{method:"POST",idem:true,body:{expected_preview_hash:preview.preview_hash,expected_content_revision:preview.content_revision,allow_partial:partial.checked,mappings:mappings.map(m=>({row_index:m.row_index,decision:m.input.value.startsWith("update:")?"update":m.input.value,institution_id:m.input.value.startsWith("update:")?m.input.value.slice(7):null}))}});
    $("#dialog").close();state.view="institutions";await Promise.all([reloadOverview(),loadView()]);notify(`기관 목록을 저장했습니다. 적재 ${result.loaded_count}개, 명부 연결 ${result.linked_count}개.`);
  },"검토한 목록 저장");
}

function exportDialog() {
  const season=currentSeason(),include=element("input",{type:"checkbox"});
  formShell("프로그램 없이 볼 HTML 일정표 저장",[element("p",{text:"현재 시즌의 같은 저장 버전에서 일반 일정표와 판매 준비 필터를 만듭니다. 파일을 내려받아 두면 이 프로그램을 종료한 후에도 열 수 있습니다."}),
    element("div",{class:"notice",text:"사적 확인 메모는 기본으로 포함하지 않습니다. 저장된 파일은 자동 갱신되지 않으므로 최신 결과가 필요할 때 다시 저장하세요."}),
    element("label",{class:"check-label"},include,"사적 확인 메모도 이 파일에 포함하기")],async()=>{
    const result=await api("/api/exports/html",{method:"POST",body:{season_id:season.id,include_notes:include.checked,publication_policy_version:"4.0"}});
    const current=result?.url?result:await api(`/api/exports/${season.id}`);
    if(!current.url)throw new Error(current.error?.message || "HTML 저장이 아직 끝나지 않았습니다. 잠시 후 다시 시도하세요.");
    const content=element("div",{},
      element("div",{class:current.status==="current"?"notice good":"notice",text:current.status==="current"?"일정표 파일을 만들었습니다. 아래 'HTML 파일 내려받기'를 눌러 컴퓨터에 저장하세요.":"마지막으로 저장에 성공한 파일입니다. 최신 기록과 버전이 다를 수 있습니다. 다시 저장해 갱신하세요."}),
      element("p",{text:`생성 ${moment(current.exported_at)} · 저장 버전 ${current.exported_revision} / 현재 버전 ${current.content_revision} · 보류·제외 ${current.withheld_event_count}건`}),
      element("a",{class:"export-link",href:current.url,download:`${season.export_key}-졸업식-일정표.html`,text:"HTML 파일 내려받기"}),
      element("a",{href:current.url,target:"_blank",rel:"noopener",text:"새 창에서 일정표 열기"}),
      element("p",{class:"hint",text:"내려받은 파일을 더블 클릭하면 열립니다. 파일 안의 검색·지역·기관 종류·판매 준비 필터와 인쇄가 인터넷 없이 동작합니다. 근거 원문 링크를 열 때는 인터넷이 필요합니다."})
    );openDialog("HTML 일정표 저장 완료",content);notify("오프라인 일정표를 생성했습니다.");
  },"현재 기록으로 HTML 만들기");
}

async function statusDialog() {
  const status=await api("/api/status");
  const settings=state.settings,llm=settings.llm || {};
  const content=element("div",{},element("p",{text:`실행 모드: ${status.app_mode==="live"?"실제 사용":"데모"} · 앱 ${status.app_version} · 게시 정책 ${status.schema_version}`}));
  for(const capability of status.capabilities)content.append(element("section",{class:"evidence-card"},element("h3",{},capability.name," ",badge(capability.available?"사용 가능":capability.configured?"설정됨 · 연결 별도 확인":"설정 필요",capability.available?"":"warning")),element("p",{text:capability.message}),capability.last_checked_at?element("p",{text:`최근 확인 ${moment(capability.last_checked_at)}`}):null));
  content.append(element("p",{class:"notice",text:"선택한 모델 백엔드만 사용합니다. 연결이 실패해도 다른 모델이나 가상 데이터로 자동 전환하지 않습니다. 저장 조회·직접 입력은 외부 연결 없이 계속 사용할 수 있습니다."}));
  if(llm.backend)content.append(element("p",{text:`선택한 백엔드: ${llm.backend} · 텍스트 모델: ${llm.text_model || "설정 참조"}`}));
  if(settings.external_transfer)content.append(element("p",{class:"hint",text:`공개 문서 텍스트 외부 전송: ${settings.external_transfer.document_text_allowed ? "허용" : "금지"} · 사적 수기 메모는 모델에 보내지 않습니다.`}));
  content.append(element("p",{class:"hint",text:".env의 준비 항목과 config 설정 변경은 프로그램 재시작 후 새 조사에 적용됩니다. 실행 중이던 작업은 시작 당시와 조사·모델 설정이 같아야 이어갈 수 있습니다. 비밀키는 이 화면에 표시하지 않습니다."}));
  openDialog("기능별 연결·설정 상태",content);
}

async function backup() {
  const result=await api("/api/backups",{method:"POST",idem:true});
  const response=await fetch(result.download_url,{headers:{"X-App-Token":state.token},credentials:"same-origin"});
  if(!response.ok)throw new Error("백업은 생성했지만 다운로드하지 못했습니다. 저장 폴더의 backups를 확인하세요.");
  const blob=await response.blob(),url=URL.createObjectURL(blob),link=element("a",{href:url,download:`졸업식-기록-백업-${new Date().toISOString().slice(0,10)}.sqlite3`});
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  notify("SQLite 백업을 만들고 다운로드했습니다. 백업에는 근거와 사적 입력 기록이 포함됩니다.");
}

function shutdownDialog() {
  formShell("저장하고 프로그램 종료",[element("p",{text:"조사 중인 작업을 안전하게 멈추고 저장한 뒤 프로그램을 종료합니다. 다음에 프로그램을 실행하면 기록이 남아 있습니다. 내려받은 HTML 일정표는 계속 열 수 있습니다."})],async()=>{
    await api("/api/app/shutdown",{method:"POST",body:{pause_active_job:true}});
    clearInterval(state.pollTimer);$("#dialog").close();
    notify("기록을 저장하고 프로그램을 종료했습니다. 이 창을 닫아도 됩니다.");
    document.querySelectorAll("main button,main select,main input").forEach(node=>node.disabled=true);
  },"저장 후 종료");
}

async function refresh() {
  await loadSeasons();await Promise.all([reloadOverview(),loadView()]);
}

function debounce(fn,delay=300) {
  let timer;return (...args)=>{clearTimeout(timer);timer=setTimeout(()=>fn(...args),delay);};
}

async function initialize() {
  $("#type-filter").append(...options(TYPES));
  $("#close-dialog").addEventListener("click",()=>$("#dialog").close());
  $("#dialog").addEventListener("click",event=>{if(event.target===$("#dialog")){const rect=$("#dialog").getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)$("#dialog").close();}});
  $(".tabs").addEventListener("click",event=>{
    const tab=event.target.closest("button[data-view]");if(!tab)return;
    state.view=tab.dataset.view;task(()=>loadView());
  });
  $("#season-select").addEventListener("change",()=>task(async()=>{
    state.season=state.seasons.find(season=>season.id===$("#season-select").value);state.selected.clear();$("#selected-count").textContent="0";
    await api("/api/selected-season",{method:"POST",body:{season_id:state.season.id}});
    localStorage.setItem("graduation-season",state.season.id);await refresh();
  }));
  for(const selector of ["#district-filter","#type-filter","#from-date","#to-date"])$(selector).addEventListener("change",()=>task(()=>loadView()));
  $("#search").addEventListener("input",debounce(()=>task(()=>loadView())));
  $("#reset-filters").addEventListener("click",()=>{for(const id of ["search","district-filter","type-filter","from-date","to-date"])$(`#${id}`).value="";task(()=>loadView());});
  $("#refresh-button").addEventListener("click",()=>window.location.reload());
  $("#load-more").addEventListener("click",()=>task(()=>loadView(true)));
  $("#add-season").addEventListener("click",()=>seasonForm());
  $("#edit-season").addEventListener("click",()=>task(()=>seasonForm(currentSeason())));
  $("#add-institution").addEventListener("click",()=>institutionForm());
  $("#new-event-button").addEventListener("click",()=>eventForm());
  $("#scan-all").addEventListener("click",()=>task(()=>startScan()));
  $("#scan-selected").addEventListener("click",()=>task(()=>startScan([...state.selected])));
  $("#sync-registry").addEventListener("click",syncRegistry);
  $("#import-registry").addEventListener("click",registryImport);
  $("#prepared-registry").addEventListener("click",()=>task(preparedRegistry));
  $("#export-button").addEventListener("click",()=>task(exportDialog));
  $("#settings-button").addEventListener("click",()=>task(statusDialog));
  $("#backup-button").addEventListener("click",()=>task(backup));
  $("#shutdown-button").addEventListener("click",shutdownDialog);
  const bootstrap=await api("/api/bootstrap");
  state.token=bootstrap.token;state.settings=bootstrap.settings || {};
  const mode=bootstrap.status?.app_mode || state.settings.mode || "live";
  $("#mode-badge").textContent=mode==="live"?"내 컴퓨터에 저장":"데모 데이터";
  $("#mode-badge").className=mode==="live"?"badge neutral":"badge warning";
  await loadSeasons(bootstrap.selected_season_id);
  await Promise.all([reloadOverview(),loadView()]);
  state.lastRevision=bootstrap.status?.content_revision;
  let polling=false;
  state.pollTimer=setInterval(async()=>{
    if(polling || document.hidden)return;
    polling=true;
    try{
      const status=await api("/api/status");
      if(status.content_revision!==state.lastRevision){state.lastRevision=status.content_revision;await reloadOverview();if(!$("#dialog").open)await loadView();}
      else if(state.view==="jobs"&&!$("#dialog").open)await loadView();
    }catch(error){clearInterval(state.pollTimer);notify(errorMessage(error),true);}
    finally{polling=false;}
  },Math.max(3000,bootstrap.poll_interval_ms || 5000));
}

task(initialize);

/* 서버와 같은 달력 덧셈: 6개월 뒤 말일로 보정한 날짜 자체는 선택할 수 없다. */
window.seasonRangeBounds = function (start, end) {
  const parse = value => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const date = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? date : null;
  };
  const first = parse(start), last = parse(end);
  if (!first) return {valid:false, maxEnd:null, message:"시작 날짜를 선택해 주세요."};
  const threshold = new Date(first);
  threshold.setUTCDate(1); threshold.setUTCMonth(threshold.getUTCMonth() + 6);
  const monthEnd = new Date(threshold); monthEnd.setUTCMonth(monthEnd.getUTCMonth() + 1); monthEnd.setUTCDate(0);
  threshold.setUTCDate(Math.min(first.getUTCDate(), monthEnd.getUTCDate()));
  const maximum = new Date(threshold); maximum.setUTCDate(maximum.getUTCDate() - 1);
  const maxEnd = maximum.toISOString().slice(0, 10);
  return {maxEnd, valid:Boolean(last && last >= first && last < threshold),
    message:!last ? "종료 날짜를 선택해 주세요." : last < first ? "종료일은 시작일보다 빠를 수 없어요." :
      last >= threshold ? "6개월 미만의 기간을 선택해 주세요." : ""};
};
/* 앱과 저장 HTML은 필터에 맞는 전체 행을 보존해 같은 인쇄 결과를 만든다. */
window.setupSchedule = function (root) {
  const find = id => root.querySelector(`[data-control="${id}"]`);
  const rows = [...root.querySelectorAll("tbody tr[data-name]")];
  const query = find("query"), district = find("district"), day = find("date");
  const selectedDistrict = district.value;
  const districts = [...new Set(["동구", "서구", "남구", "북구", "광산구", ...rows.map(row => row.dataset.district).filter(Boolean)])];
  district.replaceChildren(new Option("전체 구", ""), ...districts.map(value => new Option(value, value)));
  district.value = districts.includes(selectedDistrict) ? selectedDistrict : "";
  let page = Number(root.dataset.page || 0);
  function render() {
    const term = query.value.trim().toLocaleLowerCase().replace(/\s+/g, "");
    const matching = rows.filter(row =>
      row.dataset.name.toLocaleLowerCase().replace(/\s+/g, "").includes(term) &&
      (!district.value || row.dataset.district === district.value) &&
      (!day.value || (row.dataset.dates || row.dataset.date).split(" ").includes(day.value)));
    const pages = Math.max(1, Math.ceil(matching.length / 20));
    page = Math.max(0, Math.min(page, pages - 1));
    rows.forEach(row => { row.hidden = true; row.dataset.match = "false"; row.classList.remove("date-group-start", "page-start"); });
    // 먼저 필터 전체의 날짜 그룹을 칠해야 페이지 경계에서도 같은 날짜가 같은 색이다.
    let group = -1, previousDate = null;
    matching.forEach((row, index) => {
      const date = row.dataset.date, boundary = index === 0 || date !== previousDate;
      if (boundary && date) group++;
      row.className = date ? `date-color-${group % 6}` : "date-unknown";
      row.dataset.match = "true";
      row.classList.toggle("date-group-start", boundary);
      previousDate = date;
    });
    root.dataset.page = String(page);
    matching.slice(page * 20, (page + 1) * 20).forEach((row, index) => { row.hidden = false; row.classList.toggle("page-start", index === 0); });
    find("empty").hidden = matching.length > 0;
    find("count").textContent = `${matching.length}건 / 전체 ${rows.length}건`;
    find("page").textContent = `${page + 1} / ${pages} 페이지`;
    find("previous").disabled = page === 0;
    find("next").disabled = page >= pages - 1;
  }
  [query, district, day].forEach(input => {
    input.oninput = () => { page = 0; render(); };
    input.onchange = () => { page = 0; render(); };
  });
  find("reset").onclick = () => { query.value = district.value = day.value = ""; page = 0; render(); };
  find("previous").onclick = () => { page--; render(); };
  find("next").onclick = () => { page++; render(); };
  find("print").onclick = () => window.print();
  render();
};
document.querySelectorAll("[data-schedule]").forEach(window.setupSchedule);

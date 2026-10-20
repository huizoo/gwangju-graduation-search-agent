/* 저장 HTML과 실행 앱은 같은 검색·기간·페이지 규칙을 사용한다. */
window.setupSchedule = function (root) {
  const find = id => root.querySelector(`[data-control="${id}"]`);
  const rows = [...root.querySelectorAll("tbody tr[data-name]")];
  const query = find("query"), mode = find("date-mode");
  const from = find("from-date"), to = find("to-date");
  let page = 0;
  function render() {
    const term = query.value.trim().toLocaleLowerCase().replace(/\s+/g, "");
    const end = mode.value === "day" ? from.value : to.value;
    const invalid = from.value && end && from.value > end;
    const matching = rows.filter(row => !invalid &&
      row.dataset.name.toLocaleLowerCase().replace(/\s+/g, "").includes(term) &&
      (!from.value || (row.dataset.date && row.dataset.date >= from.value)) &&
      (!end || (row.dataset.date && row.dataset.date <= end)));
    const pages = Math.max(1, Math.ceil(matching.length / 20));
    page = Math.max(0, Math.min(page, pages - 1));
    rows.forEach(row => { row.hidden = true; });
    matching.slice(page * 20, (page + 1) * 20).forEach(row => { row.hidden = false; });
    find("to-wrap").hidden = mode.value === "day";
    find("empty").hidden = matching.length > 0;
    find("empty").textContent = invalid ? "날짜 범위를 확인해 주세요." : "조건에 맞는 학교가 없습니다.";
    find("count").textContent = `${matching.length}건 / 전체 ${rows.length}건`;
    find("page").textContent = `${page + 1} / ${pages} 페이지`;
    find("previous").disabled = page === 0;
    find("next").disabled = page >= pages - 1;
  }
  [query, mode, from, to].forEach(input => {
    input.oninput = () => { page = 0; render(); };
    input.onchange = () => { page = 0; render(); };
  });
  find("reset").onclick = () => { query.value = from.value = to.value = ""; mode.value = "day"; page = 0; render(); };
  find("previous").onclick = () => { page--; render(); };
  find("next").onclick = () => { page++; render(); };
  render();
};
document.querySelectorAll("[data-schedule]").forEach(window.setupSchedule);

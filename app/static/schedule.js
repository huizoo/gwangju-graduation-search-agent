/* 앱과 저장 HTML은 필터에 맞는 전체 행을 보존해 같은 인쇄 결과를 만든다. */
window.setupSchedule = function (root) {
  const find = id => root.querySelector(`[data-control="${id}"]`);
  const rows = [...root.querySelectorAll("tbody tr[data-name]")];
  const query = find("query"), district = find("district"), day = find("date");
  const selectedDistrict = district.value;
  const districts = [...new Set(["동구", "서구", "남구", "북구", "광산구", ...rows.map(row => row.dataset.district).filter(Boolean)])];
  district.replaceChildren(new Option("전체 구", ""), ...districts.map(value => new Option(value, value)));
  district.value = districts.includes(selectedDistrict) ? selectedDistrict : "";
  let page = 0;
  rows.forEach(row => {
    const date = row.dataset.date;
    const color = [...date].reduce((value, char) => (value * 31 + char.charCodeAt(0)) >>> 0, 0) % 6;
    row.className = date ? `date-color-${color}` : "date-unknown";
  });
  function render() {
    const term = query.value.trim().toLocaleLowerCase().replace(/\s+/g, "");
    const matching = rows.filter(row =>
      row.dataset.name.toLocaleLowerCase().replace(/\s+/g, "").includes(term) &&
      (!district.value || row.dataset.district === district.value) &&
      (!day.value || (row.dataset.dates || row.dataset.date).split(" ").includes(day.value)));
    const pages = Math.max(1, Math.ceil(matching.length / 20));
    page = Math.max(0, Math.min(page, pages - 1));
    rows.forEach(row => { row.hidden = true; row.dataset.match = "false"; row.classList.remove("date-group-start", "page-start"); });
    matching.forEach((row, index) => {
      row.dataset.match = "true";
      row.classList.toggle("date-group-start", index === 0 || row.dataset.date !== matching[index - 1].dataset.date);
    });
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

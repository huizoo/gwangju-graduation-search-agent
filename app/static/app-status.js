/* 작업 결과의 이유를 숨기거나 미공개 여부로 단정하지 않는 짧은 사용자 문구다. */
window.scheduleTimeFeedback = function (job) {
  const messages = {
    not_found: "이번 조사에서 시간을 찾지 못했어요.",
    source_unavailable: "일부 자료를 읽지 못했어요.",
    service_error: "조사 중 오류가 났어요.",
    incomplete: "찾기를 마치지 못했어요."
  };
  if (messages[job.feedback]) return messages[job.feedback];
  // 방금 끝난 목록 응답에는 원문 읽기 결과가 없으므로 결과 투영을 기다린다.
  if (job.status === "completed" && !Object.hasOwn(job, "feedback")) return "결과를 확인하는 중…";
  return "찾기를 마치지 못했어요.";
};
window.scheduleShutdownMessage = function (pausedActiveJob) {
  return pausedActiveJob ? "조사를 멈추고 종료했어요. 다음에 이어서 할 수 있어요." :
    "프로그램을 종료했어요. 이 창을 닫아도 됩니다.";
};

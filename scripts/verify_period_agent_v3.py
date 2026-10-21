"""기존 기관별 한도를 유지하며 과거 기간을 실제 선택 모델로 조사한다."""

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.domain import utcnow
from app.jobs import BudgetStop
from app.periods import validate_period
from app.research import ResearchRunner
from verify_sources_v2 import AuditGateway, database_signature


class ItemMeter:
    """독립 Runner 진단에도 기존 기관 한도와 실제 토큰 정산을 적용한다.

    Args:
        settings: 변경하지 않은 실행 설정.
        output: 공개 체크포인트 기록 폴더.
    """

    def __init__(self, settings, output):
        self.limits, self.output = settings.data["execution"], output
        self.usage, self.reservations = Counter(), {}
        self.started, self.latest = time.monotonic(), None

    async def reserve(self, kind, amount=1):
        """실제 외부 호출 전에 동일 기관 한도를 초과하면 중지한다.

        Args:
            kind: 호출·토큰 사용량 항목.
            amount: 호출 전 예약량.
        Returns:
            정산할 예약 식별자.
        Raises:
            BudgetStop: 기존 기관별 시간 또는 사용 한도를 초과한 경우.
        """
        if time.monotonic() - self.started >= self.limits["item_timeout_seconds"]:
            raise BudgetStop("item_timeout")
        cap = self.limits.get("item_max_" + kind)
        if cap is not None and self.usage[kind] + amount > cap:
            raise BudgetStop("item_budget")
        identity = str(uuid4())
        self.usage[kind] += amount
        self.reservations[identity] = (kind, amount)
        return identity

    async def settle(self, identity, actual_amount=None):
        """알려진 실제 사용량만 정산하고 미상 토큰은 예약분을 유지한다.

        Args:
            identity: 호출 전에 기록한 예약 식별자.
            actual_amount: 응답으로 확인한 실제 사용량 또는 None.
        Returns:
            None. 메모리 계량 값을 갱신한다.
        """
        kind, reserved = self.reservations.pop(identity)
        if actual_amount is not None:
            self.usage[kind] += actual_amount - reserved

    async def checkpoint(self, result, *, stage):
        """예산 중단 전까지 확보한 공개 원문·후보·모델 시도를 보존한다.

        Args:
            result: 실제 Runner의 현재 공개 조사 결과.
            stage: 체크포인트 단계.
        Returns:
            None. 격리 폴더에 현재 근거를 저장한다.
        """
        self.latest = deepcopy(result)
        (self.output / "research-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


class Gateway(AuditGateway):
    """실제 HTTP 연결과 모델 응답을 인증값 없이 기록한다."""

    async def request(self, method, url, **kwargs):
        """선택 모델 응답만 공개 입력의 실제 추론 증거로 보존한다.

        Args:
            method: HTTP 메서드.
            url: 기존 Gateway에서 허용하는 실제 요청 주소.
            **kwargs: 인증값을 출력하지 않고 전달할 요청 인자.
        Returns:
            실제 HTTP 응답.
        """
        response = await super().request(method, url, **kwargs)
        if url.endswith("/api/chat") or url == "https://api.openai.com/v1/responses":
            number = len(list(self.meter.output.glob("model-response-*.json"))) + 1
            (self.meter.output / f"model-response-{number}.json").write_text(
                json.dumps(response.json(), ensure_ascii=False, indent=2) + "\n")
        return response


async def collect(settings, output, school, season, identifiers):
    """실제 Runner·MCP·선택 모델을 호출하되 앱 저장 검증과 구분한다.

    Args:
        settings: 원본을 변경하지 않은 설정.
        output: 격리 증빙 폴더.
        school: 공식 학교 식별 정보.
        season: 사용자가 고정한 실제 행사 날짜 범위.
        identifiers: 확인된 공식 자료원 학교 코드.
    Returns:
        실제 사용량과 모델 호출·작업 종료 결과.
    """
    meter = ItemMeter(settings, output)
    gateway = Gateway(settings, meter)
    runner = ResearchRunner(settings, meter, gateway)
    stopped = None
    try:
        try:
            result = await runner.run(school, season, identifiers=identifiers)
        except BudgetStop as error:
            stopped = error.reason
            result = meter.latest or {"documents": [], "candidates": [], "counts": []}
        (output / "research-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return {"result_status": result.get("status"), "stop_reason": stopped or result.get("stop_reason"),
                "missing_fields": result.get("missing_fields"), "conflicts": result.get("conflicts"),
                "model_initialized": runner.model is not None, "usage": dict(meter.usage), "requests": gateway.requests,
                "candidate_count": len(result.get("candidates", [])), "count_claims": len(result.get("counts", [])),
                "passed": result.get("status") == "completed", "app_publication_validation": False,
                "scope": "실제 단일 학교 Runner/MCP 진단, 기관 한도 적용. JobManager·앱 저장 검증은 별도."}
    finally:
        await gateway.close()


def main():
    """원본 기록·설정을 보존하고 날짜 정답을 주입하지 않은 결과를 남긴다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--school", default="건국초등학교")
    parser.add_argument("--start", default="2025-12-01")
    parser.add_argument("--end", default="2026-03-15")
    args = parser.parse_args()
    validate_period(args.start, args.end)
    stamp = utcnow().replace(":", "").replace("-", "").replace(".", "")
    output = ROOT / "runtime/period-v3/agent" / stamp
    output.mkdir(parents=True)
    source = ROOT / "data/live/graduation.sqlite3"
    original = database_signature(source)
    env_hash = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        school = dict(conn.execute("SELECT * FROM institutions WHERE name=?", (args.school,)).fetchone())
        school["official_channels"] = json.loads(school.pop("official_channels_json"))
        identifiers = [dict(row) for row in conn.execute(
            "SELECT provider,external_key FROM institution_identifiers WHERE institution_id=?", (school["id"],))]
    season = {"id": str(uuid4()), "academic_year": None, "search_start": args.start, "search_end": args.end}
    settings = load_settings()
    report = {"started_at": utcnow(), "school": school["name"], "season": season,
              "backend": settings.data["app"]["llm_backend"], "passed": False, "date_answer_seed": False}
    try:
        report.update(asyncio.run(collect(settings, output, school, season, identifiers)))
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "code": getattr(error, "code", None)}
    finally:
        report["original_db_unchanged"] = database_signature(source) == original
        report["env_unchanged"] = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest() == env_hash
        report["finished_at"] = utcnow()
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": str(output / "result.json"), "passed": report["passed"],
                          "stop_reason": report.get("stop_reason"), "failure": report.get("failure")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

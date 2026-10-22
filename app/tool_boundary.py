"""같은 프로세스의 MCP 클라이언트·서버로 읽기 도구 권한을 분리한다."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from jsonschema import Draft202012Validator, FormatChecker
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.shared.memory import create_connected_server_and_client_session

from .domain import SCHEMA
from .providers import ProviderError, canonical, utcnow
from .rules import REGISTERED_RULES
from .periods import fact_season
from .tracing import CURRENT


class ResearchTools:
    """기관 한 곳으로 제한된 읽기 전용 MCP 세션.

    Args:
        institution: 고정된 기관. 다른 기관 ID를 도구 인자로 받지 않는다.
        season: 고정된 시즌.
        identifiers: 정규 기관 식별자.
        calendar: NEIS Adapter.
        search: 공개 검색 Adapter.
        reader: 공개 원문 reader.
        students: 해당 학년도 졸업 학년 인원 Adapter.
    """

    def __init__(self, institution, season, identifiers, calendar, search, reader, students=None):
        self.institution, self.season, self.identifiers = institution, season, identifiers
        self.calendar, self.search, self.reader = calendar, search, reader
        self.students = students
        self.student_years = set()
        self.trace = []
        self.server = Server("gwangju-graduation-read-tools", version="0.1.0")
        self.definitions = SCHEMA["$defs"]
        self._register()
        self.session = None
        self.pending_error = None

    def allow_student_years(self, years):
        """서버가 독립 검증한 행사 원문 학년도만 인원 조회 대상으로 허용한다.

        Args:
            years: 같은 기관·기간의 근거 검사에 통과한 행사 학년도.
        Returns:
            없음. 외부 MCP 도구에는 이 권한 갱신 기능을 노출하지 않는다.
        """
        self.student_years.update(year for year in years if type(year) is int)

    def _schema(self, name):
        """도구 경계에서 같은 4.0 요청·응답 계약을 사용한다."""
        return {"$ref": f"#/$defs/{name}", "$defs": self.definitions}

    def _register(self):
        """공개 읽기 도구와 고정 검증 정책 리소스를 등록한다."""
        specs = {
            "get_school_calendar": ("CalendarInput", "CalendarResult", "인증된 공식 학교 학사일정 전체 기간 조회"),
            "search_public_web": ("SearchInput", "SearchResult", "공개 웹에서 URL 후보 발견"),
            "read_public_document": ("ReadInput", "DocumentRead", "공개 HTML 또는 텍스트 PDF 읽기"),
        }
        if self.students is not None:
            specs["get_graduating_students"] = ("StudentInput", "StudentResult", "해당 학년도 졸업 학년 재학생 인원 조회")

        @self.server.list_tools()
        async def list_tools():
            """실제로 호출할 수 있는 고정 도구 계약을 반환한다."""
            return [types.Tool(name=name, description=description, inputSchema=self._schema(input_name))
                    for name, (input_name, _, description) in specs.items()]

        @self.server.call_tool()
        async def call_tool(name, arguments):
            """고정된 기관·시즌으로 범위를 검사한 뒤 Adapter를 호출한다."""
            if name not in specs:
                raise ValueError("허용되지 않은 도구")
            Draft202012Validator(self._schema(specs[name][0]), format_checker=FormatChecker()).validate(arguments)
            if name != "search_public_web" and arguments["institution_id"] != self.institution["id"]:
                raise ValueError("기관 범위를 벗어난 호출")
            try:
                if name == "get_school_calendar":
                    if arguments["season_id"] != self.season["id"] or arguments["from_date"] != self.season["search_start"] or arguments["to_date"] != self.season["search_end"]:
                        raise ValueError("고정 조사 기간을 벗어난 호출")
                    result = await self.calendar.calendar(self.institution, self.season, self.identifiers)
                elif name == "get_graduating_students":
                    if arguments["season_id"] != self.season["id"]:
                        raise ValueError("고정 조사 학년도를 벗어난 호출")
                    year = arguments.get("academic_year")
                    if year not in self.student_years:
                        raise ValueError("검증한 행사 원문 학년도만 인원을 조회할 수 있습니다.")
                    result = await self.students.lookup(self.institution, fact_season(self.season, year), self.identifiers)
                elif name == "search_public_web":
                    result = await self.search.search(arguments["query"], arguments.get("include_domains"), arguments["max_results"])
                else:
                    result = await self.reader.read(self.institution, arguments["url"])
            except Exception as error:
                # 예산·취소 의도를 일반 provider 오류로 바꾸면 작업 재개 상태가 손상된다.
                self.pending_error = error
                raise ValueError("읽기 도구 실행이 중단되었습니다.") from None
            Draft202012Validator(self._schema(specs[name][1]), format_checker=FormatChecker()).validate(result)
            self.trace.append({"tool": name, "at": utcnow(), "status": result.get("status"),
                               "institution_id": self.institution["id"],
                               **({"academic_year": year} if name == "get_graduating_students" else {})})
            return [types.TextContent(type="text", text=canonical(result))]

        @self.server.list_resources()
        async def list_resources():
            """고정 검증 정책의 읽기 경로를 공개한다."""
            return [types.Resource(uri="policy://graduation/verification", name="서버 검증 정책", mimeType="application/json")]

        @self.server.read_resource()
        async def read_resource(uri):
            """모델이나 문서가 바꿀 수 없는 등록 규칙 버전을 반환한다."""
            if str(uri) != "policy://graduation/verification":
                raise ValueError("알 수 없는 정책 리소스")
            return [ReadResourceContents(canonical({"policy_version": "4.0", "registered_rules": REGISTERED_RULES,
                "approval": "서버 ValueCheck와 활성 규칙 및 Publication만 최종 값을 승인"}), "application/json")]

    @asynccontextmanager
    async def connect(self):
        """실제 MCP 세션을 열고 시작 시 검증 정책을 읽는다."""
        saved_error = None
        async with create_connected_server_and_client_session(self.server) as session:
            self.session = session
            resource = await session.read_resource("policy://graduation/verification")
            if not resource.contents:
                raise ProviderError("provider_error", "검증 정책 리소스를 읽지 못했습니다.", "mcp")
            self.trace.append({"tool": "read_resource", "resource": "policy://graduation/verification",
                               "at": utcnow(), "status": "ok"})
            try:
                yield self
            except Exception as error:
                # AnyIO TaskGroup이 예산·멈춤을 ExceptionGroup으로 감싸지 않게 정리 후 전달한다.
                saved_error = error
            finally:
                self.session = None
        if saved_error is not None:
            raise saved_error

    async def call(self, name: str, arguments: dict) -> dict:
        """도구 입력 원문을 보내지 않고 호출 결과의 상태만 추적한다."""
        parent = CURRENT.get()
        if parent is None:
            return await self._call(name, arguments)
        with parent.recorder.span(name) as trace:
            result = await self._call(name, arguments)
            trace.update(status=result.get("status"), reason_codes=result.get("reason_codes", []))
            return result

    async def _call(self, name: str, arguments: dict) -> dict:
        """직접 Adapter 우회 없이 MCP 요청·응답 검사를 통과해 결과를 얻는다."""
        if self.session is None:
            raise RuntimeError("MCP 세션이 열려 있지 않습니다.")
        result = await self.session.call_tool(name, arguments)
        if self.pending_error is not None:
            error, self.pending_error = self.pending_error, None
            raise error
        if result.isError:
            # SDK 오류 본문은 원문·비밀을 포함할 수 있으므로 그대로 사용자에게 노출하지 않는다.
            raise ProviderError("provider_error", "읽기 도구 요청이 계약 또는 자료원 검사에서 실패했습니다.", "mcp")
        return json.loads(next(item.text for item in result.content if item.type == "text"))

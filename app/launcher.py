"""한 명령으로 지속 저장소·서버·브라우저를 시작하는 로컬 런처."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import webbrowser

import httpx
import uvicorn

from .api import create_app
from .config import ConfigurationError, load_settings
from .domain import ApiError


class InstanceLock:
    """같은 데이터 파일을 사용하는 앱의 중복 서버 실행을 막는다."""

    def __init__(self, path: Path):
        """프로세스 종료 시 운영체제가 해제하는 파일 잠금을 준비한다.

        Args:
            path: 해당 데이터 저장소의 잠금 파일 경로.
        Raises:
            OSError: 잠금 디렉터리나 파일을 열 수 없는 경우.
        """
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+", encoding="utf-8")
        self.acquired = False

    def acquire(self) -> bool:
        """사용자 프로세스를 종료하지 않고 현재 잠금 보유 여부만 확인한다.

        Returns:
            잠금 획득 성공 여부. 다른 실행의 점유나 잠금 오류는 False다.
        """
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except (BlockingIOError, OSError):
            self.acquired = False
        return self.acquired

    def read(self) -> dict:
        """이전 실행의 주소·식별자를 읽는다.

        Returns:
            저장된 실행 메타정보. 읽기나 JSON 해석 실패 시 빈 사전.
        """
        self.file.seek(0)
        try:
            return json.load(self.file)
        except (ValueError, OSError):
            return {}

    def write(self, metadata: dict) -> None:
        """중복 실행 확인에 필요한 비밀 아닌 메타정보만 저장한다.

        Args:
            metadata: 로컬 주소·실행 식별자·PID 등 비밀을 제외한 값.
        Raises:
            OSError: 잠금 파일에 기록하거나 flush할 수 없는 경우.
        """
        self.file.seek(0)
        self.file.truncate()
        json.dump(metadata, self.file)
        self.file.flush()

    def close(self) -> None:
        """자신이 연 잠금 파일 핸들만 닫는다."""
        self.file.close()


def bind_server(host: str, port: int) -> socket.socket:
    """요청 포트를 우선 사용하고 점유 중이면 OS가 고른 빈 포트를 예약한다.

    Args:
        host: 검증된 loopback 주소.
        port: 사용자 설정 포트.
    Returns:
        다른 프로세스와 경합하지 않도록 이미 bind된 socket.
    Raises:
        OSError: 포트 대체 후에도 socket을 준비할 수 없는 경우.
    """
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        sock.bind((host, 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


async def _serve(settings, lock: InstanceLock) -> None:
    """readiness 이후에만 화면을 열고 종료 시 작업을 checkpoint한다."""
    app = create_app(settings)
    sock = bind_server(settings.data["app"]["host"], settings.data["app"]["port"])
    host, port = sock.getsockname()[:2]
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on"))
    app.state.runtime.shutdown_callback = lambda: setattr(server, "should_exit", True)
    lock.write({"url": url, "instance_id": app.state.runtime.instance_id, "pid": os.getpid()})
    print(f"광주 졸업식 일정 도우미: {url}", flush=True)
    print(f"기록 저장 위치: {settings.data_dir}", flush=True)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started or task.done():
                break
            await asyncio.sleep(0.1)
        if server.started and settings.data["app"]["auto_open_browser"]:
            webbrowser.open(url)
        await task
    finally:
        if not task.done():
            server.should_exit = True
            await task
        sock.close()


def main() -> int:
    """설정을 보존하며 앱을 실행한다.

    Returns:
        정상·중복 화면 열기·사용자 중단은 0, 설정 오류나 실행 확인 실패는 1.
    Raises:
        OSError: 데이터 파일이나 서버 socket을 준비할 수 없는 경우.
    """
    lock = None
    try:
        settings = load_settings()
        lock = InstanceLock(settings.data_dir / "instance.lock")
        if not lock.acquire():
            metadata = lock.read()
            url = metadata.get("url", "")
            # 파일의 주소만 믿고 임의 사이트를 열지 않고 같은 앱 식별자까지 확인한다.
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
                try:
                    response = httpx.get(url + "/api/instance", timeout=3, trust_env=False).json()
                    if response.get("instance_id") == metadata.get("instance_id"):
                        print(f"이미 실행 중입니다: {url}")
                        if settings.data["app"]["auto_open_browser"]:
                            webbrowser.open(url)
                        return 0
                except (httpx.HTTPError, ValueError):
                    pass
            print("같은 저장소의 앱이 시작 또는 종료 중입니다. 잠시 뒤 다시 실행해 주세요.", file=sys.stderr)
            return 1
        asyncio.run(_serve(settings, lock))
        return 0
    except (ConfigurationError, ApiError) as error:
        print(f"시작할 수 없습니다: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if lock:
            lock.close()

"""
웹 버전 출석체크 서버. 키오스크 PC(또는 라즈베리파이)에서 띄우고 브라우저로 http://127.0.0.1:8000 을 연다.

    python web/server.py                 # 이 PC에서만 접속
    python web/server.py --host 0.0.0.0  # 같은 와이파이의 다른 기기에서도 관리자 화면(/admin)에 접속

카메라와 얼굴 인식은 서버(이 PC)에서 돌고, 브라우저는 /ws로 상태를 받아 화면만 그린다.
처음 열면 조교 로그인(/login, 지금은 더미 — staff_auth.py)을 거쳐 출석 화면이 나온다.
관리자 화면은 http://127.0.0.1:8000/admin (기본 계정 1234 / 1234 — 운영 전에 꼭 바꾼다).
"""
import argparse
import asyncio
import multiprocessing
import os
import re
import sys
import time
from contextlib import asynccontextmanager

# 프로젝트 폴더의 core.py를 불러오기 위해 (python web/server.py 로 실행해도 되게)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import admin_api  # noqa: E402
import diag  # noqa: E402
import staff_auth  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VIDEO_FPS = 10 if core.LOW_POWER else 20  # 브라우저로 보내는 영상의 초당 장수

engine = None  # 서버가 뜰 때 만든다 (얼굴 인식 프로세스가 이 파일을 다시 불러와도 엔진은 하나만 생기게)


def _log_thread_crash(args):
    """백그라운드 스레드(카메라·인식 등)가 예외로 죽으면 콘솔에만 찍히고 로그에는 안 남는다 — 로그에도 남긴다."""
    import traceback
    tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    diag.event("[서버] 스레드 %s 가 오류로 멈췄습니다\n%s", getattr(args.thread, "name", "?"), tb.rstrip(),
               level=diag.logging.ERROR)


@asynccontextmanager
async def lifespan(_app):
    global engine
    import threading
    threading.excepthook = _log_thread_crash
    from engine import AttendanceEngine
    engine = AttendanceEngine()
    _app.state.engine = engine  # 관리자 API(admin_api.py)가 쓴다
    diag.event("[서버] 시작 (python %s, 영상 %dfps, 저전력 %s)", sys.version.split()[0], VIDEO_FPS, core.LOW_POWER)
    diag.start_summary_thread(engine)
    lag_task = asyncio.create_task(watch_loop_lag())
    core.start_weekly_mail_scheduler()  # 목요일 06:00 이후 주간 미달 학생에게 안내 메일 (core.py 3-1)
    yield
    lag_task.cancel()
    diag.event("[서버] 종료")
    engine.close()


LOOP_LAG_STEP = 0.05     # 이 간격으로 깨어나서, 늦게 깨어난 만큼을 '서버 멈춤'으로 잰다
LOOP_LAG_WARN_MS = 300   # 이보다 오래 멈추면 따로 경고를 남긴다


async def watch_loop_lag():
    """
    서버의 이벤트 루프가 다른 일에 붙잡혀 멈춘 시간을 잰다. 루프가 멈추면 웹소켓(화면 전환)·영상·버튼 요청이
    한꺼번에 늦어져서 영상이 끊기고 화면이 깜빡이는 것처럼 보인다.
    """
    while True:
        started = time.perf_counter()
        await asyncio.sleep(LOOP_LAG_STEP)
        lag_ms = (time.perf_counter() - started - LOOP_LAG_STEP) * 1000
        diag.meter.time_ms("loop_lag_ms", max(0.0, lag_ms))
        if lag_ms > LOOP_LAG_WARN_MS:
            diag.event("[서버] 이벤트 루프가 %.0fms 동안 멈췄습니다", lag_ms, level=diag.logging.WARNING)


class RevalidatedStaticFiles(StaticFiles):
    """
    화면 파일(css/js/이미지)을 쓸 때마다 바뀌었는지 서버에 확인하게 한다 (Cache-Control: no-cache).
    이게 없으면 브라우저가 옛 파일을 알아서 한동안 재사용해서, 업데이트해도 화면이 그대로인 일이 생긴다.
    안 바뀌었으면 서버는 내용 없이 304만 돌려주므로 부담은 거의 없다.
    """
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="창의공간 출석체크", lifespan=lifespan)
app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")
app.mount("/assets", RevalidatedStaticFiles(directory=os.path.join(core.RESOURCE_DIR, "assets")), name="assets")
app.include_router(admin_api.router)
app.include_router(staff_auth.router)

SLOW_REQUEST_MS = 1000


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - started) * 1000
    # 영상 스트림은 계속 이어지는 응답이라 빼고, 오류 응답과 느린 요청만 남긴다
    if request.url.path != "/video.mjpg" and (ms > SLOW_REQUEST_MS or response.status_code >= 500):
        diag.event("[요청] %s %s → %d (%.0fms)", request.method, request.url.path, response.status_code, ms,
                   level=diag.logging.WARNING)
    return response


# 로그인 여부에 따라 달라지는 페이지 — 브라우저가 저장해 둔 화면을 그대로 띄우면 로그아웃해도 출석 화면이 보인다
NO_STORE = {"Cache-Control": "no-store"}
STATIC_REF = re.compile(r'(/static/[\w.-]+\.(?:css|js))"')


def page(name, headers):
    """
    화면 HTML. css/js 주소 뒤에 파일 수정 시각(?v=...)을 붙여서,
    파일을 고치면 주소가 바뀌어 브라우저가 옛 파일을 절대 다시 쓰지 않게 한다.
    """
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
        html = f.read()

    def versioned(m):
        path = os.path.join(STATIC_DIR, m.group(1)[len("/static/"):])
        version = int(os.path.getmtime(path)) if os.path.exists(path) else 0
        return f'{m.group(1)}?v={version}"'

    return HTMLResponse(STATIC_REF.sub(versioned, html), headers=headers)


@app.get("/")
def index(request: Request):
    # 조교가 로그인해야 출석 화면이 열린다
    if staff_auth.staff_session(request) is None:
        return RedirectResponse("/login", headers=NO_STORE)
    return page("index.html", NO_STORE)


@app.get("/login")
def login_page(request: Request):
    if staff_auth.staff_session(request) is not None:
        return RedirectResponse("/", headers=NO_STORE)
    return page("login.html", NO_STORE)


@app.get("/admin")
def admin_page():
    return page("admin.html", NO_STORE)


@app.get("/api/state")
def get_state():
    return engine.state()


class SessionRequest(BaseModel):
    mode: str                  # "attendance" / "lookup" / "test"
    action: str | None = None  # attendance일 때 "CHECK_IN" / "CHECK_OUT"


@app.post("/api/session")
def start_session(req: SessionRequest):
    try:
        engine.start_session(req.mode, req.action)
    except ValueError:
        raise HTTPException(400, "mode는 attendance/lookup/test, action은 CHECK_IN/CHECK_OUT 이어야 합니다.")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return engine.state()


@app.post("/api/idle")
def go_idle():
    if engine.state()["mode"] != "register":  # 관리자가 등록 중인 카메라는 키오스크에서 끄지 않는다
        engine.go_idle("출석 화면 [처음으로]")
    return engine.state()


class ClientLog(BaseModel):
    page: str = ""
    message: str


_client_log_times = []  # 브라우저가 오류를 쏟아내도 로그가 넘치지 않게 1분에 60건까지만 받는다


@app.post("/api/log")
def client_log(req: ClientLog, request: Request):
    """브라우저에서 생긴 문제(웹소켓 끊김, 영상 끊김, 스크립트 오류)를 서버 로그에 같이 남긴다."""
    now = time.time()
    _client_log_times[:] = [t for t in _client_log_times if now - t < 60]
    if len(_client_log_times) < 60:
        _client_log_times.append(now)
        diag.event("[브라우저 %s %s] %s", request.client.host if request.client else "?", req.page[:20],
                   req.message[:500])
    return {}


@app.websocket("/ws")
async def ws_state(ws: WebSocket):
    """상태가 바뀔 때마다 보낸다 (0.1초마다 확인)."""
    await ws.accept()
    who = f"{ws.client.host if ws.client else '?'} {ws.headers.get('referer', '').rsplit('/', 1)[-1] or '/'}"
    diag.event("[웹소켓] 접속 %s (지금 %d개)", who, diag.gauge_add("ws_clients", 1))
    sent = -1
    reason = "끊김"
    try:
        while True:
            st = engine.state()
            if st["version"] != sent:
                sent = st["version"]
                await ws.send_json(st)
                diag.meter.count("ws_sent")
            await asyncio.sleep(0.1)
    except (WebSocketDisconnect, RuntimeError) as e:
        reason = type(e).__name__
    finally:
        diag.event("[웹소켓] 종료 %s (%s, 남은 %d개)", who, reason, diag.gauge_add("ws_clients", -1))


@app.get("/video.mjpg")
async def video(request: Request):
    """
    카메라 화면(얼굴 상자·이름 포함)을 MJPEG로 보낸다. <img src="/video.mjpg">로 바로 볼 수 있다.
    학생 얼굴이 보이므로 이 PC(키오스크)에서 열었거나 관리자로 로그인한 경우에만 보낸다.
    """
    if request.client.host not in ("127.0.0.1", "::1") and admin_api.admin_session(request) is None:
        raise HTTPException(403, "카메라 화면은 키오스크 PC나 관리자 로그인 후에만 볼 수 있습니다.")

    who = request.client.host if request.client else "?"

    async def frames():
        diag.event("[영상] 시청 시작 %s (지금 %d명)", who, diag.gauge_add("stream_clients", 1))
        try:
            last_seq = -1
            next_at = time.perf_counter()
            while True:
                # 인코딩에 걸린 시간을 빼고 기다려서 정해진 초당 장수(VIDEO_FPS)를 지킨다
                next_at += 1 / VIDEO_FPS
                seq, jpg = await asyncio.to_thread(engine.stream_jpeg, last_seq)
                if jpg:
                    last_seq = seq
                    diag.meter.count("stream_sent")
                    diag.meter.count("stream_bytes", len(jpg))
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                now = time.perf_counter()
                if next_at < now:
                    next_at = now  # 많이 밀렸으면 따라잡으려 몰아 보내지 않고 지금부터 다시 센다
                await asyncio.sleep(next_at - now)
        finally:
            diag.event("[영상] 시청 끝 %s (남은 %d명)", who, diag.gauge_add("stream_clients", -1))

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


def main():
    parser = argparse.ArgumentParser(description="창의공간 출석체크 웹 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # 카메라는 한 프로그램만 쓸 수 있으므로 tkinter 앱이나 다른 서버가 켜져 있으면 띄우지 않는다
    if not core.acquire_single_instance():
        print("[출석] 출석체크 프로그램이 이미 실행 중입니다.")
        sys.exit(1)

    import uvicorn
    print(f"[출석] 브라우저에서 http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port} 을 여세요.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

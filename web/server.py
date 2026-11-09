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
from contextlib import asynccontextmanager

# 프로젝트 폴더의 core.py를 불러오기 위해 (python web/server.py 로 실행해도 되게)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import admin_api  # noqa: E402
import staff_auth  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VIDEO_FPS = 10 if core.LOW_POWER else 20  # 브라우저로 보내는 영상의 초당 장수

engine = None  # 서버가 뜰 때 만든다 (얼굴 인식 프로세스가 이 파일을 다시 불러와도 엔진은 하나만 생기게)


@asynccontextmanager
async def lifespan(_app):
    global engine
    from engine import AttendanceEngine
    engine = AttendanceEngine()
    _app.state.engine = engine  # 관리자 API(admin_api.py)가 쓴다
    core.start_weekly_mail_scheduler()  # 목요일 06:00 이후 주간 미달 학생에게 안내 메일 (core.py 3-1)
    yield
    engine.close()


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
        engine.go_idle()
    return engine.state()


@app.websocket("/ws")
async def ws_state(ws: WebSocket):
    """상태가 바뀔 때마다 보낸다 (0.1초마다 확인)."""
    await ws.accept()
    sent = -1
    try:
        while True:
            st = engine.state()
            if st["version"] != sent:
                sent = st["version"]
                await ws.send_json(st)
            await asyncio.sleep(0.1)
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get("/video.mjpg")
async def video(request: Request):
    """
    카메라 화면(얼굴 상자·이름 포함)을 MJPEG로 보낸다. <img src="/video.mjpg">로 바로 볼 수 있다.
    학생 얼굴이 보이므로 이 PC(키오스크)에서 열었거나 관리자로 로그인한 경우에만 보낸다.
    """
    if request.client.host not in ("127.0.0.1", "::1") and admin_api.admin_session(request) is None:
        raise HTTPException(403, "카메라 화면은 키오스크 PC나 관리자 로그인 후에만 볼 수 있습니다.")

    async def frames():
        last_seq = -1
        while True:
            seq = engine.frame_seq
            jpg = engine.jpeg_frame() if seq != last_seq else None
            if jpg:
                last_seq = seq
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            await asyncio.sleep(1 / VIDEO_FPS)

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

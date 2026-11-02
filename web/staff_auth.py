"""
첫 화면의 조교 로그인 (사번 + 비밀번호). 로그인해야 출석 화면(/)이 열린다.

※ 지금은 더미다 — 사번·비밀번호를 비우지만 않으면 누구든 통과한다.
  실제 조교 계정이 정해지면 check_staff_login()만 바꾸면 된다 (예: DB의 조교 테이블, 학교 인증 서버).
"""
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

SESSION_COOKIE = "eclipse_staff"
SESSION_SECONDS = 24 * 3600  # 키오스크는 하루 종일 켜 두므로 하루 동안 유지한다

_sessions = {}               # 세션 토큰 → (사번, 만료 시각)
_sessions_lock = threading.Lock()

router = APIRouter(prefix="/api/staff")


def check_staff_login(staff_id, password):
    """조교 사번·비밀번호 확인. TODO: 더미 — 실제 조교 계정 확인으로 바꾼다."""
    return bool(staff_id and password)


def staff_session(request: Request):
    """로그인한 조교 사번. 로그인하지 않았거나 만료됐으면 None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with _sessions_lock:
        entry = _sessions.get(token)
        if entry is None or entry[1] < time.time():
            _sessions.pop(token, None)
            return None
        return entry[0]


class StaffLogin(BaseModel):
    staff_id: str
    password: str


@router.post("/login")
def login(req: StaffLogin, response: Response):
    staff_id, password = req.staff_id.strip(), req.password.strip()
    if not (staff_id and password):
        raise HTTPException(400, "사번과 비밀번호를 입력해주세요.")
    if not check_staff_login(staff_id, password):
        raise HTTPException(401, "사번 또는 비밀번호가 틀렸습니다.")
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = (staff_id, time.time() + SESSION_SECONDS)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=SESSION_SECONDS)
    return {"staff_id": staff_id}


@router.post("/logout")
def logout(request: Request, response: Response):
    with _sessions_lock:
        _sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response.delete_cookie(SESSION_COOKIE)
    return {}


@router.get("/me")
def me(request: Request):
    staff_id = staff_session(request)
    if staff_id is None:
        raise HTTPException(401, "조교 로그인이 필요합니다.")
    return {"staff_id": staff_id}

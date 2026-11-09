"""
웹 관리자 화면(/admin)이 쓰는 API. tkinter 관리자 화면(AdminDashboardFrame)과 같은 일을 core.py 함수로 한다.
로그인하면 쿠키에 세션 표시를 넣고, /api/admin/* 는 그 쿠키가 있어야 쓸 수 있다.
"""
import io
import os
import secrets
import shutil
import threading
import time
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

import core

SESSION_COOKIE = "eclipse_admin"
SESSION_SECONDS = 8 * 3600       # 마지막으로 쓴 뒤 이 시간이 지나면 다시 로그인해야 한다
LOGIN_FAIL_DELAY = 1.0           # 비밀번호를 틀리면 잠깐 기다리게 해서 마구 대입해 보는 것을 늦춘다

_sessions = {}                   # 세션 토큰 → (관리자 아이디, 만료 시각)
_sessions_lock = threading.Lock()

router = APIRouter(prefix="/api/admin")


def admin_session(request: Request):
    """요청의 로그인 세션 (관리자 아이디). 없거나 만료됐으면 None. 쓸 때마다 만료 시각을 늦춘다."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with _sessions_lock:
        entry = _sessions.get(token)
        if entry is None or entry[1] < time.time():
            _sessions.pop(token, None)
            return None
        _sessions[token] = (entry[0], time.time() + SESSION_SECONDS)
        return entry[0]


def require_admin(request: Request):
    admin_id = admin_session(request)
    if admin_id is None:
        raise HTTPException(401, "관리자 로그인이 필요합니다.")
    return admin_id


def engine_of(request: Request):
    return request.app.state.engine


# ---------- 로그인 ----------
class LoginRequest(BaseModel):
    user_id: str
    password: str


@router.post("/login")
def login(req: LoginRequest, response: Response):
    user_id, password = req.user_id.strip(), req.password.strip()
    if not (user_id and password):
        raise HTTPException(400, "아이디와 비밀번호를 입력해주세요.")
    result = core.check_admin_login(user_id, password)
    if result != "ok":
        time.sleep(LOGIN_FAIL_DELAY)
        raise HTTPException(401, "로그인 정보가 틀렸습니다." if result == "wrong" else "관리자 권한이 없습니다.")
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = (user_id, time.time() + SESSION_SECONDS)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=SESSION_SECONDS)
    return {"user_id": user_id}


@router.post("/logout")
def logout(request: Request, response: Response):
    with _sessions_lock:
        _sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response.delete_cookie(SESSION_COOKIE)
    engine_of(request).stop_register_camera()
    return {}


@router.get("/me")
def me(admin_id: str = Depends(require_admin)):
    return {"user_id": admin_id}


# ---------- 학생 현황/수정 ----------
STUDENT_KEYS = ("user_id", "name", "major", "penalty", "phone", "grade")


@router.get("/students")
def students(_admin=Depends(require_admin)):
    emails = core.student_emails()
    return [dict(zip(STUDENT_KEYS, row), email=emails.get(str(row[0]), "")) for row in core.list_students()]


@router.get("/presence")
def presence(_admin=Depends(require_admin)):
    """
    학생별 오늘 출퇴근 상태 (출근 중 LED · 타임라인 · 퇴근 누락). 화면이 열려 있는 동안 몇십 초마다 부른다.
    지금 시각도 같이 보내서, 다른 기기에서 열어도 그 기기 시계가 아니라 서버 시계로 경과 시간을 센다.
    """
    now = datetime.now()
    return {"now": now.isoformat(timespec="seconds"), "date": now.strftime("%Y-%m-%d"),
            "students": core.student_presence(now)}


class CheckoutRequest(BaseModel):
    date: str        # 출근한 날짜 YYYY-MM-DD
    check_in: str    # 퇴근을 넣을 출근 시각 HH:MM:SS
    check_out: str   # 넣을 퇴근 시각 HH:MM 또는 HH:MM:SS


@router.post("/students/{student_id}/checkout")
def manual_checkout(student_id: str, req: CheckoutRequest, admin_id: str = Depends(require_admin)):
    try:
        time_str = core.manual_checkout(student_id, req.date, req.check_in, req.check_out, admin_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"check_out": time_str}


@router.get("/weekly-shortfall")
def weekly_shortfall(base: date | None = None, _admin=Depends(require_admin)):
    monday, sunday, rows = core.weekly_shortfall(base)
    emails = core.student_emails()
    mailed = core.weekly_mail_status(monday)
    for row in rows:
        row["email"] = emails.get(row["user_id"], "")
        row["mail"] = mailed.get(row["user_id"])  # 이번 주 안내 메일 보낸 기록 (없으면 None)
    return {"start": monday, "end": sunday, "rows": rows,
            "required_days": core.WEEKLY_REQUIRED_DAYS, "required_seconds": core.WEEKLY_REQUIRED_SECONDS,
            "this_week": monday <= date.today() <= sunday, "mail_ready": bool(core.mail_settings()[0])}


# ---------- 주간 미달 안내 메일 ----------
@router.get("/mail")
def mail_info(_admin=Depends(require_admin)):
    url, secret = core.mail_settings()
    return {"url": url, "secret": secret, "last": core.load_settings().get("weekly_mail_last"),
            "weekday": core.WEEKDAY_KO[core.WEEKLY_MAIL_WEEKDAY], "time": core.WEEKLY_MAIL_TIME[:5]}


class MailUrlRequest(BaseModel):
    url: str


@router.put("/mail")
def set_mail_url(req: MailUrlRequest, _admin=Depends(require_admin)):
    url = req.url.strip()
    if url and not url.startswith("https://script.google.com/"):
        raise HTTPException(400, "Apps Script 웹 앱 주소(https://script.google.com/macros/s/.../exec)를 넣어주세요.")
    core.save_setting("mail_webapp_url", url)
    return {}


class MailTestRequest(BaseModel):
    to: str


@router.post("/mail/test")
def send_test_mail(req: MailTestRequest, _admin=Depends(require_admin)):
    """설정이 맞는지 아무 주소(보통 조교 본인)로 시험 메일을 보낸다. 학생에게는 보내지 않는다."""
    try:
        to = core.normalize_email(req.to)
        if not to:
            raise ValueError("시험 메일을 받을 주소를 넣어주세요.")
        result = core.send_mail(to, "[창의공간] 출석체크 메일 설정 확인",
                                "출석체크 프로그램의 주간 미달 안내 메일 설정이 잘 되었습니다.\n"
                                f"매주 {core.WEEKDAY_KO[core.WEEKLY_MAIL_WEEKDAY]}요일 "
                                f"{core.WEEKLY_MAIL_TIME[:5]} 이후 미달 학생에게 자동으로 보냅니다.")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return {"remaining": result.get("remaining")}


@router.post("/mail/weekly-shortfall")
def send_weekly_mails_now(_admin=Depends(require_admin)):
    """이번 주 미달 학생에게 지금 바로 보낸다 (이미 받은 학생은 건너뛴다)."""
    if not core.mail_settings()[0]:
        raise HTTPException(400, "먼저 [설정] → 주간 미달 안내 메일에서 Apps Script 웹 앱 주소를 저장하세요.")
    return core.send_weekly_shortfall_mails()


class StudentUpdate(BaseModel):
    name: str
    major: str
    penalty: str
    phone: str = ""
    grade: str = ""
    email: str = ""


@router.put("/students/{student_id}")
def update_student(student_id: str, req: StudentUpdate, request: Request, _admin=Depends(require_admin)):
    try:
        core.update_student(student_id, req.name, req.major, req.penalty, req.phone, req.grade, req.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    engine_of(request).reload_users()
    return {}


class DeleteRequest(BaseModel):
    ids: list[str]


@router.post("/students/delete")
def delete_students(req: DeleteRequest, request: Request, _admin=Depends(require_admin)):
    if not req.ids:
        raise HTTPException(400, "삭제할 학생을 고르세요.")
    core.delete_students(req.ids)
    engine_of(request).reload_users()
    return {"deleted": len(req.ids)}


@router.get("/export.xlsx")
def export_excel(_admin=Depends(require_admin)):
    try:
        wb, _count = core.attendance_workbook()
    except ImportError:
        raise HTTPException(500, "엑셀 내보내기에 필요한 openpyxl 패키지가 설치되어 있지 않습니다. (pip install openpyxl)")
    if wb is None:
        raise HTTPException(404, "저장된 출석 기록이 없습니다.")
    buf = io.BytesIO()
    wb.save(buf)
    name = f"출석기록_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


# ---------- 학생 등록 ----------
@router.post("/register/camera")
def keep_register_camera(request: Request, _admin=Depends(require_admin)):
    """[학생 등록] 탭이 열려 있는 동안 주기적으로 부른다 (카메라를 켜 두기)."""
    engine_of(request).keep_register_camera()
    return {}


@router.post("/register/camera/stop")
def stop_register_camera(request: Request, _admin=Depends(require_admin)):
    engine_of(request).stop_register_camera()
    return {}


@router.post("/register/recenter")
def recenter_register(request: Request, _admin=Depends(require_admin)):
    """[초점 맞추기] — 지금 학생의 고개 방향을 정면 기준점으로 다시 잡는다."""
    try:
        engine_of(request).recenter_register()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {}


class RegisterRequest(BaseModel):
    user_id: str
    name: str
    major: str = ""
    phone: str = ""
    grade: str = ""
    email: str = ""


@router.post("/register")
def register(req: RegisterRequest, request: Request, _admin=Depends(require_admin)):
    try:
        engine_of(request).register_student(req.user_id, req.name, req.major, req.phone, req.grade, req.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"name": req.name.strip()}


@router.get("/roster")
def roster(request: Request, _admin=Depends(require_admin)):
    """명단(구글 시트 / 구글폼 CSV / 창공시스템)을 다시 읽어 아직 등록 안 된 학생을 돌려준다."""
    try:
        students, source = core.load_student_roster()
    except Exception as e:
        return {"error": core.roster_error_message(e), "checked_at": f"{datetime.now():%H:%M:%S}"}
    engine_of(request).apply_roster(students)
    filled = core.fill_blank_contacts_from_roster(students)
    unregistered = [
        {"user_id": str(s.get("StudentId", "")).strip(), "name": str(s.get("Name") or "").strip(),
         "major": str(s.get("Department") or "").strip(), "phone": str(s.get("Phone") or "").strip(),
         "grade": core.normalize_grade(s.get("Grade")), "email": str(s.get("Email") or "").strip()}
        for s in core.unregistered_roster(students)
    ]
    return {
        "source": source,
        "total": len(students),
        "unregistered": unregistered,
        "filled": filled,
        "live_seconds": core.ROSTER_REFRESH_SECONDS if source == "구글 시트" else None,
        "checked_at": f"{datetime.now():%H:%M:%S}",
    }


# ---------- 전체 출결로그 ----------
@router.get("/logs")
def logs(mode: str = "custom", start: date | None = None, end: date | None = None, keyword: str = "",
         _admin=Depends(require_admin)):
    today = date.today()
    if mode != "custom" and mode not in core.LOG_SUMMARY_MODES and mode != "month":
        raise HTTPException(400, "알 수 없는 조회 조건입니다.")
    start, end = core.log_period(mode, start or today, end or today)
    keyword = keyword.strip()
    if mode in core.LOG_SUMMARY_MODES:
        summary = core.query_log_summary(mode, start, end, keyword)
        if mode == "recent7_sum":
            total = sum(s["seconds"] for s in summary)
            attended = sum(1 for s in summary if s["days"] > 0)
            result = f"학생 {len(summary)}명 중 출석 {attended}명 · 전체 출석 시간 {core.format_duration(total)}"
        else:
            result = f"조건을 충족한 학생 {len(summary)}명"
        return {"kind": "summary", "start": start, "end": end, "rows": summary, "result": result}

    rows = core.query_log_rows(start, end, keyword)
    check_in = sum(1 for r in rows if r["log_type"] == "CHECK_IN")
    result = (f"총 {len(rows)}건 (출근 {check_in} · 퇴근 {len(rows) - check_in}) "
              f"· 학생 {len({r['user_id'] for r in rows})}명")
    return {"kind": "rows", "start": start, "end": end, "rows": rows, "result": result}


@router.get("/photo")
def photo(path: str, _admin=Depends(require_admin)):
    """출퇴근 사진. 사진 폴더 밖의 파일은 열지 않는다."""
    root = os.path.realpath(core.PHOTO_DIR)
    target = os.path.realpath(core.photo_abs_path(path))
    if os.path.commonpath([root, target]) != root or not os.path.isfile(target):
        raise HTTPException(404, "사진 파일을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.")
    return FileResponse(target, media_type="image/jpeg")


# ---------- 설정 ----------
CAMERA_CHOICES = 5  # 0~4번


@router.get("/settings")
def settings(_admin=Depends(require_admin)):
    preferred = core.preferred_camera_index()
    return {
        "timeouts": dict(core.timeouts),
        "timeout_range": {"min": core.TIMEOUT_MIN, "max": core.TIMEOUT_MAX, "step": core.TIMEOUT_STEP},
        "camera": {"preferred": preferred if preferred is not None else 0, "active": core.ACTIVE_CAMERA_INDEX,
                   "choices": CAMERA_CHOICES},
        "google_form_url": str(core.load_settings().get("google_form_csv_url", "")),
        "key_email": core.google_service_account_email(),
        "photo_retention_days": core.PHOTO_RETENTION_DAYS,
        "log_modes": core.LOG_FILTER_MODES,
    }


class TimeoutRequest(BaseModel):
    name: str
    seconds: int


@router.put("/settings/timeout")
def set_timeout(req: TimeoutRequest, _admin=Depends(require_admin)):
    if req.name not in core.TIMEOUT_DEFAULTS:
        raise HTTPException(400, "알 수 없는 화면입니다.")
    if not core.TIMEOUT_MIN <= req.seconds <= core.TIMEOUT_MAX or req.seconds % core.TIMEOUT_STEP:
        raise HTTPException(400, f"{core.TIMEOUT_MIN}~{core.TIMEOUT_MAX}초, {core.TIMEOUT_STEP}초 단위로 정할 수 있습니다.")
    core.save_timeout(req.name, req.seconds)
    return dict(core.timeouts)


class CameraRequest(BaseModel):
    index: int


@router.put("/settings/camera")
def set_camera(req: CameraRequest, request: Request, _admin=Depends(require_admin)):
    if not 0 <= req.index < CAMERA_CHOICES:
        raise HTTPException(400, "카메라 번호가 올바르지 않습니다.")
    core.save_preferred_camera_index(req.index)
    engine_of(request).reopen_camera()
    return {}


class FormUrlRequest(BaseModel):
    url: str


@router.put("/settings/form-url")
def set_form_url(req: FormUrlRequest, _admin=Depends(require_admin)):
    url = req.url.strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "구글 응답 시트 주소(https://docs.google.com/spreadsheets/d/...) 또는 "
                                 "CSV로 게시한 https:// 주소를 넣어주세요.")
    core.save_setting("google_form_csv_url", url)
    return {}


class KeyRequest(BaseModel):
    content: str  # 브라우저에서 고른 JSON 키 파일의 내용


@router.post("/settings/google-key")
def set_google_key(req: KeyRequest, _admin=Depends(require_admin)):
    """서비스 계정 JSON 키를 프로그램 폴더에 저장하고, 시트를 공유할 이메일을 알려준다."""
    temp_path = core.GOOGLE_KEY_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(req.content)
    email = core.google_service_account_email(temp_path)
    if not email:
        os.remove(temp_path)
        raise HTTPException(400, "서비스 계정 JSON 키가 아닙니다.\n"
                                 "Google Cloud → IAM 및 관리자 → 서비스 계정 → 키 → [JSON]으로 받은 파일을 골라주세요.")
    shutil.move(temp_path, core.GOOGLE_KEY_PATH)
    return {"key_email": email}

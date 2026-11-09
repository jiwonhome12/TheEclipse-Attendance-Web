"""
웹 버전 출석체크의 엔진 — 카메라, 얼굴 인식, 사람 확인(고개 돌리기·위조판별), 출근/퇴근 흐름.
tkinter 앱(AttendanceApp)이 화면과 섞어서 하던 일을 화면 없이 옮겼다. 사진/영상으로 대리 출석하는 것은
tkinter 앱의 눈 깜빡임 대신 '상하좌우 중 무작위 2방향 고개 돌리기'(TurnChallenge)로 막는다.
브라우저는 state()로 지금 상태를 받아 그리기만 하고, 버튼을 누르면 start_session()/go_idle()을 부른다.
"""
import multiprocessing
import random
import threading
import time
from datetime import date

import cv2
import numpy as np
from PIL import Image, ImageDraw

import core

# 화면 상태 (tkinter 앱과 같은 이름)
#   idle       : 메인 화면. 카메라/얼굴 인식 꺼짐
#   attendance : 출근/퇴근을 누른 뒤. 얼굴이 확인되면 자동으로 기록한다
#   lookup     : [출석 조회]. 얼굴로 본인 확인만 하고 기록은 하지 않는다
#   result     : 출근/퇴근 처리 결과 (카메라 꺼짐, 잠시 뒤 메인 화면으로)
#   stats      : 본인 출석 현황 (카메라 꺼짐, 잠시 뒤 메인 화면으로)
#   test       : [카메라 테스트]. 카메라와 인식 결과만 보여주고 기록은 하지 않는다
#   register   : 관리자 [학생 등록] 탭이 열려 있다. 카메라를 켜고 등록할 얼굴을 기다린다
SESSION_MODES = ("attendance", "lookup", "test")
REGISTER_HEARTBEAT_SECONDS = 60  # 관리자 화면이 이 시간 동안 소식이 없으면(창을 닫았으면) 카메라를 끈다
FACE_LOST_SECONDS = 2.0          # 얼굴이 이 시간 넘게 안 보이면 고개 돌리기를 처음부터 다시 한다
TURN_IDENTITY_SIM = 0.30         # 고개를 돌린 얼굴이 처음 얼굴과 이만큼은 닮아야 같은 사람이 돌린 것으로 센다
# 고개 돌리기 방향 — (화면에 보이는 이름, 안내 문구)
TURN_DIRECTIONS = {
    "left": ("왼쪽", "고개를 왼쪽으로 돌려 주세요"),
    "right": ("오른쪽", "고개를 오른쪽으로 돌려 주세요"),
    "up": ("위", "고개를 위로 들어 주세요"),
    "down": ("아래", "고개를 아래로 숙여 주세요"),
}


class TurnChallenge:
    """
    위·아래·왼쪽·오른쪽 중 무작위로 고른 2방향으로 차례로 고개를 돌린 뒤 정면으로 돌아오면 통과한다.
    방향과 순서를 미리 알 수 없으니(4×3=12가지) 녹화해 둔 영상으로 따라 하기 어렵고, 평평한 사진은
    기울여도 코의 상대 위치가 바뀌지 않아(core.head_pose 참고) 돌린 것으로 잡히지 않는다.
    돌리는 동안 다른 사람이 대신 하지 못하게, 처음 얼굴(ref)과 비슷한 얼굴이 돌린 것만 센다.
    상하 값은 사람·카메라 높이마다 다르므로 처음 정면 값(ref_pitch)과의 차이로 본다.
    """

    def __init__(self, ref_embedding, pose, center=None):
        """center: 관리자가 [초점 맞추기]로 정한 정면 (좌우, 상하). 없으면 좌우 0, 상하는 처음 정면 값."""
        self.steps = random.sample(list(TURN_DIRECTIONS), 2)
        self.index = 0
        self.done_at = None   # 다 돌리고 정면으로 돌아온 시각
        norm = np.linalg.norm(ref_embedding)
        self.ref = ref_embedding / norm if norm else ref_embedding
        self.ref_yaw = center[0] if center else 0.0
        self.ref_pitch = center[1] if center else None
        self._set_ref_pitch(pose)

    def _set_ref_pitch(self, pose):
        # 정면(좌우로 안 돌린 상태)일 때의 상하 값을 기준으로 삼는다
        if self.ref_pitch is None and pose is not None and abs(pose[0] - self.ref_yaw) <= core.HEAD_CENTER_RATIO:
            self.ref_pitch = pose[1]

    def recenter(self, pose):
        """지금 고개 방향을 정면(기준점)으로 다시 잡고, 고개 돌리기를 처음 단계부터 다시 한다."""
        self.ref_yaw, self.ref_pitch = pose
        self.index = 0
        self.done_at = None

    @property
    def turned(self):
        return self.index >= len(self.steps)

    def same_person(self, embedding):
        norm = np.linalg.norm(embedding)
        return norm > 0 and float(self.ref @ (embedding / norm)) >= TURN_IDENTITY_SIM

    def offsets(self, pose):
        """(좌우, 상하) — 정면 기준값과의 차이. 상하 기준값이 아직 없으면 상하는 None."""
        if pose is None:
            return None, None
        yaw, pitch = pose
        return yaw - self.ref_yaw, None if self.ref_pitch is None else pitch - self.ref_pitch

    def is_center(self, pose):
        yaw, dpitch = self.offsets(pose)
        return (yaw is not None and dpitch is not None
                and abs(yaw) <= core.HEAD_CENTER_RATIO and abs(dpitch) <= core.HEAD_PITCH_CENTER)

    def update(self, pose, embedding):
        """이번 프레임의 고개 방향을 반영한다. 다 돌리고 정면으로 돌아오면 done_at이 정해진다."""
        self._set_ref_pitch(pose)
        if pose is None or self.ref_pitch is None or self.done_at is not None or not self.same_person(embedding):
            return
        yaw, dpitch = self.offsets(pose)
        if not self.turned:
            reached = {
                "left": yaw <= -core.HEAD_TURN_RATIO,
                "right": yaw >= core.HEAD_TURN_RATIO,
                "up": dpitch <= -core.HEAD_PITCH_DELTA,
                "down": dpitch >= core.HEAD_PITCH_DELTA,
            }
            if reached[self.steps[self.index]]:
                self.index += 1
        elif self.is_center(pose):
            self.done_at = time.time()

    def to_dict(self, pose):
        """화면의 고개 돌리기 안내에 쓰는 값."""
        yaw, dpitch = self.offsets(pose)
        return {"steps": self.steps, "index": self.index, "done": self.done_at is not None,
                "yaw": None if yaw is None else round(yaw, 2),
                "pitch": None if dpitch is None else round(dpitch, 2),
                "turn": core.HEAD_TURN_RATIO, "pitch_turn": core.HEAD_PITCH_DELTA}

    def instruction(self):
        if not self.turned:
            return TURN_DIRECTIONS[self.steps[self.index]][1]
        return "정면을 바라봐 주세요"


class AttendanceEngine:
    def __init__(self):
        core.init_db()
        core.load_timeouts()
        core.export_attendance_json()  # 창공시스템이 항상 최신 출결 데이터를 읽어갈 수 있도록 시작할 때도 한 번

        self._lock = threading.RLock()
        self.version = 0                # 상태가 바뀔 때마다 1씩 증가 — 웹소켓이 바뀐 것만 보낸다
        self.is_running = True

        self.enrolled_users = []
        self.embed_matrix = np.empty((0, 512), dtype=np.float32)
        self.reload_users()
        self.roster_lookup = {}
        self.apply_roster(core.load_roster())

        self.mode = "idle"
        self.pending_action = None      # attendance 모드에서 누른 버튼 ("CHECK_IN" / "CHECK_OUT")
        self.session_started = 0.0      # 지금 화면에 들어간 시각 (시간 초과 계산용)
        self.card = {}                  # 화면에 보여줄 안내 (state, title, detail, sim)
        self.payload = None             # result/stats 화면에 보여줄 내용
        self._candidate_id = None       # 지금 인식되고 있는 학생 — RECOGNIZE_HOLD_SECONDS 동안 유지돼야 기록
        self._candidate_user = None
        self._candidate_since = 0.0
        self.challenge = None           # 지금 진행 중인 고개 돌리기 (TurnChallenge)
        self._last_face_time = 0.0
        self.last_pose = None           # 마지막 고개 방향 (core.head_pose)
        self.register_center = None     # [학생 등록]에서 [초점 맞추기]로 정한 정면 (좌우, 상하) — 등록 탭을 닫으면 지운다

        self.ai_ready = False
        self.ai_failed = False
        self.camera_on = False
        self.camera_error = False
        self.camera_reopen = False      # True면 캡처 스레드가 카메라를 닫고 camera.txt의 번호로 다시 연다
        self.cached_faces = []          # 마지막 인식 결과 [(bbox, embedding)] — 학생 등록에 쓴다
        self.camera_generation = 0      # 카메라를 켜고 끌 때마다 1씩 증가 — 이전 세션의 늦은 인식 결과를 버린다
        self.latest_frame = None
        self.frame_seq = 0
        self.face_labels = []           # 영상에 그릴 (bbox, 글자, 등록 여부) — 인식 결과가 올 때마다 바뀐다
        self.face_labels_seq = -1       # face_labels를 계산한 프레임 번호

        # InsightFace 위조판별 애드온 상태 (애드온이 결과를 보내주면 켜진다)
        self.antispoof_enabled = False
        self.last_live_time = 0.0
        self.last_fake_time = 0.0
        self.last_liveness_status = None

        # InsightFace는 별도 프로세스에서 로드/실행한다 (core.face_inference_process 참고)
        self.face_conn, child_conn = multiprocessing.Pipe()
        self.face_proc = multiprocessing.Process(
            target=core.face_inference_process, args=(child_conn, core.INFERENCE_THREADS), daemon=True
        )
        self.face_proc.start()

        core.ensure_camera_permission()
        threading.Thread(target=core.cleanup_old_photos, daemon=True).start()
        self._photo_cleanup_date = date.today()

        for target in (self._capture_worker, self._ai_worker, self._tick_worker):
            threading.Thread(target=target, daemon=True).start()

    # ---------- 학생 / 명단 ----------
    def reload_users(self):
        users = core.load_users()
        if users:
            mat = np.stack([u["embedding"] for u in users]).astype(np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = mat / norms
        else:
            matrix = np.empty((0, 512), dtype=np.float32)
        with self._lock:
            self.enrolled_users, self.embed_matrix = users, matrix

    def apply_roster(self, roster):
        self.roster_lookup = {
            str(s.get("StudentId", "")).strip(): s
            for s in roster
            if str(s.get("StudentId", "")).strip()
        }

    def _display_name(self, user):
        """창공시스템 명단 이름을 먼저, 없으면 등록 당시 이름, 그마저 없으면 '?'."""
        entry = self.roster_lookup.get(str(user.get("user_id", "")).strip())
        if entry and entry.get("Name"):
            return entry["Name"]
        return user.get("name") or "?"

    # ---------- 화면 상태 ----------
    def _changed(self):
        self.version += 1

    def _set_card(self, state, title, detail="", sim=None, challenge=None):
        card = {"state": state, "title": title, "detail": detail, "sim": sim, "challenge": challenge}
        if card != self.card:
            self.card = card
            self._changed()

    def state(self):
        """브라우저에 보내는 지금 상태. 남은 시간은 브라우저가 deadline으로 직접 센다."""
        with self._lock:
            if self.mode in ("attendance", "lookup"):
                limit = core.timeouts["verify"]
            elif self.mode == "test":
                limit = core.TEST_TIMEOUT
            elif self.mode in ("result", "stats"):
                limit = core.timeouts[self.mode]
            elif self.mode == "register":
                limit = REGISTER_HEARTBEAT_SECONDS
            else:
                limit = None
            return {
                "version": self.version,
                "mode": self.mode,
                "action": self.pending_action,
                "card": self.card,
                "payload": self.payload,
                "deadline": self.session_started + limit if limit else None,
                "now": time.time(),
                "ai": "failed" if self.ai_failed else ("ready" if self.ai_ready else "loading"),
                "camera": "off" if not self.camera_on else ("error" if self.camera_error else "on"),
            }

    def start_session(self, mode, action=None):
        """출근/퇴근(attendance) · 출석 조회(lookup) · 카메라 테스트(test)를 시작한다."""
        if mode not in SESSION_MODES:
            raise ValueError(mode)
        if mode == "attendance" and action not in ("CHECK_IN", "CHECK_OUT"):
            raise ValueError(action)
        with self._lock:
            if self.mode == "register":
                raise RuntimeError("관리자가 학생을 등록하는 중입니다.")
            self.mode = mode
            self.pending_action = action if mode == "attendance" else None
            self.session_started = time.time()
            self.payload = None
            self._reset_candidate()
            self._reset_liveness()
            self.card = {}
            self._set_card("loading" if not self.ai_ready else "search",
                           "얼굴 인식 준비 중.." if not self.ai_ready else "얼굴을 찾는 중..")
            self._set_camera(True)
            self._changed()

    def go_idle(self):
        with self._lock:
            self._enter("idle", None)

    def keep_register_camera(self):
        """
        관리자 [학생 등록] 탭이 열려 있는 동안 주기적으로 부른다. 처음이면 카메라를 켜고,
        이미 켜져 있으면 꺼지기까지 남은 시간만 늘린다 (탭을 닫으면 REGISTER_HEARTBEAT_SECONDS 뒤 꺼진다).
        """
        with self._lock:
            if self.mode == "register":
                self.session_started = time.time()
                return
            self.mode = "register"
            self.pending_action = None
            self.payload = None
            self.register_center = None
            self.session_started = time.time()
            self._reset_candidate()
            self._reset_liveness()
            self.card = {}
            self._set_card("loading" if not self.ai_ready else "search",
                           "얼굴 인식 준비 중.." if not self.ai_ready else "얼굴을 찾는 중..")
            self._set_camera(True)
            self._changed()

    def stop_register_camera(self):
        with self._lock:
            if self.mode == "register":
                self._enter("idle", None)

    def recenter_register(self):
        """
        [초점 맞추기] — 지금 카메라 앞 학생의 고개 방향을 정면(방향 판의 가운데)으로 다시 잡는다.
        카메라가 비스듬히 달려 있거나 학생이 카메라를 바로 못 보는 자리라 점이 가운데로 안 올 때 쓴다.
        등록 탭이 열려 있는 동안 다음 학생에게도 같은 기준을 쓰고, 고개 돌리기는 처음 단계부터 다시 한다.
        """
        with self._lock:
            if self.mode != "register":
                raise ValueError("[학생 등록] 탭에서 카메라가 켜져 있을 때 쓸 수 있습니다.")
            if not self.cached_faces or self.last_pose is None:
                raise ValueError("카메라에 얼굴이 보이지 않습니다.\n학생이 편하게 카메라를 바라본 상태에서 눌러주세요.")
            self.register_center = tuple(self.last_pose)
            if self.challenge is not None:
                self.challenge.recenter(self.register_center)
                self._set_card("turn", self.challenge.instruction(), "기준점을 다시 잡았습니다 — 처음 방향부터 돌려 주세요",
                               challenge=self.challenge.to_dict(self.last_pose))

    def reopen_camera(self):
        """관리자 화면에서 카메라 번호를 바꿨다 — 캡처 스레드가 닫고 camera.txt의 번호로 다시 연다."""
        core.ACTIVE_CAMERA_INDEX = None  # 새로 연결될 때까지 이전 번호를 보여주지 않는다
        self.camera_reopen = True

    def register_student(self, student_id, name, major, phone, grade, email=""):
        """
        지금 카메라 앞의 얼굴로 학생을 등록한다. 등록하지 못하면 안내 문구를 담은 ValueError.
        사진으로 학생이 등록되면 이후 출석도 사진으로 뚫리므로 등록할 때도 사람인지 확인한다.
        """
        email = core.normalize_email(email)  # 형식이 틀리면 얼굴 확인 전에 알려준다
        student_id, name = str(student_id or "").strip(), str(name or "").strip()
        if not (student_id and name):
            raise ValueError("학번과 이름을 입력하세요.")
        with self._lock:
            if self.mode != "register" or not self.cached_faces:
                raise ValueError("카메라 영역에 등록할 학생의 얼굴이 감지되지 않았습니다.")
            ch = self.challenge
            if ch is None or ch.done_at is None or time.time() - ch.done_at > core.HEAD_TURN_VALID_SECONDS:
                raise ValueError("사람 확인이 끝나지 않았습니다.\n"
                                 "등록할 학생이 화면 안내대로 고개를 돌린 뒤 다시 눌러주세요.")
            if not ch.is_center(self.last_pose):
                raise ValueError("카메라를 정면으로 바라본 상태에서 눌러주세요.")
            if not self.passes_antispoof():
                raise ValueError(self.liveness_reject_message())
            embedding = np.asarray(self.cached_faces[0][1], dtype=np.float32)
            if not ch.same_person(embedding):
                raise ValueError("고개를 돌린 사람과 지금 얼굴이 다릅니다. 다시 시도해주세요.")
            dup = core.find_duplicate_face(embedding, self.enrolled_users, self.embed_matrix)
        if dup:
            raise ValueError("이미 등록된 얼굴과 유사도가 높습니다.\n"
                             f"기존 등록자: {dup['name']} ({dup['user_id']})\n"
                             "동일 인물로 판단되어 등록을 진행하지 않습니다.")
        core.insert_student(student_id, name, str(major or "").strip() or core.DEPARTMENT_NAME,
                            str(phone or "").strip(), grade, embedding, email)
        self.reload_users()
        with self._lock:
            self._reset_candidate()  # 다음 학생은 고개 돌리기부터 다시

    def _enter(self, mode, payload):
        """세션이 끝나 결과 / 출석 현황 / 메인 화면으로 넘어간다 — 카메라는 끈다."""
        self.mode = mode
        self.pending_action = None
        self.payload = payload
        self.session_started = time.time()
        self._reset_candidate()
        self.card = {}
        self._set_camera(False)
        self._changed()

    def _set_camera(self, on):
        if on == self.camera_on:
            return
        self.camera_generation += 1
        self.camera_on = on
        self.latest_frame = None
        self.face_labels = []
        self.cached_faces = []  # 꺼지거나 새로 켜면 예전 얼굴로 출결/등록이 되지 않게 비운다
        if on:
            self.camera_error = False

    def _reset_candidate(self):
        """인식 중인 학생과 고개 돌리기를 처음부터 다시 한다."""
        self._candidate_id = None
        self._candidate_user = None
        self.challenge = None

    def _reset_liveness(self):
        """다음 사람이 앞 사람의 실제 사람 판정을 이어받지 않도록 비운다."""
        self.last_live_time = 0.0
        self.last_fake_time = 0.0
        self.last_liveness_status = None

    # ---------- 백그라운드 작업 ----------
    def _capture_worker(self):
        """camera_on에 맞춰 카메라를 열고 닫으며, 켜져 있는 동안 최신 프레임 1장만 보관한다."""
        cap = None
        last_frame_time = 0.0
        while self.is_running:
            try:
                if self.camera_reopen:
                    self.camera_reopen = False
                    if cap is not None:
                        cap.release()
                        cap = None

                if not self.camera_on:
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(0.05)
                    continue

                if cap is None:
                    cap = core.open_working_camera(640, 480, core.CAMERA_FPS)
                    if cap is None:
                        if not self.camera_error:
                            self.camera_error = True
                            self._changed()
                        time.sleep(1.0)
                        continue
                    if self.camera_error:
                        self.camera_error = False
                        self._changed()
                    last_frame_time = time.time()

                ret, frame = cap.read()
                if not ret:
                    if time.time() - last_frame_time > core.CAMERA_STALL_SECONDS:
                        print(f"[출석] 카메라에서 {core.CAMERA_STALL_SECONDS:.0f}초간 영상이 오지 않아 다시 연결합니다.")
                        cap.release()
                        cap = None
                        self.camera_error = True
                        self._changed()
                    else:
                        time.sleep(0.02)
                    continue
                last_frame_time = time.time()
                if self.camera_on:
                    self.latest_frame = cv2.flip(frame, 1)
                    self.frame_seq += 1
            except Exception as e:
                print(f"[출석] 카메라 처리 중 오류가 있었지만 계속 진행합니다 ({type(e).__name__}: {e})")
                if cap is not None:
                    cap.release()
                    cap = None
                self.camera_error = True
                time.sleep(1.0)
        if cap is not None:
            cap.release()

    def _ai_worker(self):
        """최신 프레임을 인식 프로세스로 보내고, 결과가 오면 출근/퇴근 흐름을 한 걸음 진행한다."""
        try:
            self.face_conn.recv()  # 모델 로딩 완료 신호
            with self._lock:
                self.ai_ready = True
                if self.card.get("state") == "loading":
                    self._set_card("search", "얼굴을 찾는 중..")
                self._changed()
            last_seq = -1
            while self.is_running:
                frame = self.latest_frame
                seq = self.frame_seq
                generation = self.camera_generation
                if not (self.camera_on and frame is not None and seq != last_seq):
                    time.sleep(0.005)
                    continue
                last_seq = seq
                started = time.time()
                self.face_conn.send(frame)
                # 눈 깜빡임 값은 tkinter 앱만 쓴다 — 웹은 고개 방향(pose)으로 확인한다
                results, _blink, liveness, pose = self.face_conn.recv()
                rest = core.AI_MIN_INTERVAL - (time.time() - started)
                if rest > 0:
                    time.sleep(rest)
                with self._lock:
                    # 분석하는 사이에 카메라를 껐다 켰다면(다음 사람) 예전 얼굴 결과는 버린다
                    if not (self.camera_on and generation == self.camera_generation):
                        continue
                    self.cached_faces = results
                    self.last_pose = pose
                    self._update_liveness_state(liveness)
                    matched_user, best_sim = self._match_faces(results, seq)
                    if self.mode in ("attendance", "lookup"):
                        self._process_attendance(results, matched_user, pose)
                    elif self.mode == "register":
                        self._process_register(results, matched_user, pose)
                    elif self.mode == "test":
                        self._process_camera_test(results, matched_user, best_sim, pose)
        except (EOFError, OSError):
            if self.is_running:
                self.ai_failed = True
                self._changed()

    def _tick_worker(self):
        """시간이 지나면 메인 화면으로 돌아가고, 날짜가 바뀌면 오래된 사진을 정리한다."""
        while self.is_running:
            time.sleep(0.2)
            st = self.state()
            if st["deadline"] is not None and time.time() >= st["deadline"]:
                with self._lock:
                    if self.mode == st["mode"]:  # 그사이 다른 화면으로 넘어가지 않았을 때만
                        self._enter("idle", None)
            today = date.today()
            if today != self._photo_cleanup_date:
                self._photo_cleanup_date = today
                threading.Thread(target=core.cleanup_old_photos, daemon=True).start()

    # ---------- 인식 / 사람 확인 ----------
    def _match_faces(self, results, seq):
        """등록된 전원과의 유사도를 행렬곱으로 한 번에 계산한다. (가장 닮은 등록 학생, 그 유사도)."""
        best_user, best_sim = None, -1.0
        labels = []
        if results:
            embs = np.asarray([emb for _bbox, emb in results], dtype=np.float32)
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            has_users = len(self.embed_matrix) > 0
            if has_users:
                sims = (embs / norms) @ self.embed_matrix.T
                match_idx = np.argmax(sims, axis=1)
                match_sims = sims[np.arange(len(results)), match_idx]
            for i, (bbox, _emb) in enumerate(results):
                sim = float(match_sims[i]) if has_users else -1.0
                user = self.enrolled_users[match_idx[i]] if has_users else None
                if sim >= core.THRESHOLD and user:
                    labels.append((bbox, f"{self._display_name(user)}  유사도 {sim:.2f}", True))
                else:
                    labels.append((bbox, f"미등록  유사도 {sim:.2f}" if sim >= 0 else "미등록", False))
                if sim > best_sim:
                    best_sim, best_user = sim, user
        self.face_labels, self.face_labels_seq = labels, seq
        matched = best_user if (best_user and best_sim >= core.THRESHOLD) else None
        return matched, best_sim

    def _update_liveness_state(self, liveness):
        if liveness is None:
            return
        is_live, status = liveness
        self.antispoof_enabled = True
        self.last_liveness_status = status
        if status == "ok":
            if is_live:
                self.last_live_time = time.time()
            else:
                self.last_fake_time = time.time()

    def passes_antispoof(self):
        if not self.antispoof_enabled:
            return True
        return (time.time() - self.last_live_time) <= core.LIVENESS_VALID_SECONDS

    def liveness_reject_message(self):
        """위조판별에서 막혔을 때 보여줄 안내 문구."""
        if (time.time() - self.last_fake_time) <= core.LIVENESS_VALID_SECONDS:
            return "사진이나 화면으로 판단됩니다.\n본인이 직접 카메라 앞에 서서 다시 시도해주세요."
        if self.last_liveness_status == "input_rejected":
            return "얼굴이 너무 크게 잡혀 확인할 수 없습니다.\n카메라에서 조금 물러난 뒤 다시 시도해주세요."
        return "본인 확인을 하지 못했습니다.\n카메라를 정면으로 바라본 뒤 다시 시도해주세요."

    def _main_face(self, faces):
        """인식 대상(가장 큰 얼굴, 인식 프로세스가 맨 앞에 둔다)의 임베딩."""
        return np.asarray(faces[0][1], dtype=np.float32)

    def _face_present(self, faces):
        """얼굴이 보이는지. 잠깐(FACE_LOST_SECONDS 안) 놓친 것은 계속 있는 것으로 본다."""
        now = time.time()
        if faces:
            self._last_face_time = now
            return True
        return self.challenge is not None and now - self._last_face_time < FACE_LOST_SECONDS

    def _process_register(self, faces, matched_user, pose):
        """[학생 등록] 탭: 고개 돌리기를 마치면 [얼굴 등록]을 눌러도 된다고 알려준다."""
        if not self._face_present(faces):
            self._reset_candidate()
            self._set_card("search", "얼굴을 찾는 중..")
            return
        if not faces:
            return  # 잠깐 놓쳤다 — 안내는 그대로 둔다
        if matched_user is not None:
            self._reset_candidate()
            self._set_card("warn", "이미 등록된 얼굴입니다", f"{self._display_name(matched_user)} 학생")
            return
        embedding = self._main_face(faces)
        if self.challenge is None:
            self.challenge = TurnChallenge(embedding, pose, self.register_center)
        ch = self.challenge
        ch.update(pose, embedding)
        if ch.done_at is not None and time.time() - ch.done_at > core.HEAD_TURN_VALID_SECONDS:
            self.challenge = ch = TurnChallenge(embedding, pose, self.register_center)  # 너무 오래 지났다 — 다시 돌린다
        if ch.done_at is None:
            self._set_card("turn", ch.instruction(), "화면의 안내를 따라 천천히 돌려 주세요",
                           challenge=ch.to_dict(pose))
        elif not self.passes_antispoof():
            self._set_card("error", "사람 확인이 안 됩니다", self.liveness_reject_message(),
                           challenge=ch.to_dict(pose))
        else:
            self._set_card("ok", "등록할 수 있습니다", "정면을 바라본 채 [얼굴 등록]을 눌러 주세요",
                           challenge=ch.to_dict(pose))

    def _process_attendance(self, faces, matched_user, pose):
        """
        같은 학생이 RECOGNIZE_HOLD_SECONDS 동안 인식되면 고개 돌리기(상하좌우 중 무작위 2방향)를 시키고,
        다 돌린 뒤 정면에서 다시 그 학생으로 확인되고 위조판별까지 통과하면
        출근/퇴근을 기록하거나(attendance) 출석 현황을 보여준다(lookup).
        """
        now = time.time()
        if not self._face_present(faces):
            self._reset_candidate()
            self._set_card("search", "얼굴을 찾는 중..")
            return
        if not faces:
            return  # 잠깐 놓쳤다 — 안내는 그대로 둔다

        embedding = self._main_face(faces)
        if matched_user is not None and matched_user["user_id"] != self._candidate_id:
            # 처음 인식했거나 다른 학생이 섰다 — 처음부터
            self._candidate_id = matched_user["user_id"]
            self._candidate_user = matched_user
            self._candidate_since = now
            self.challenge = TurnChallenge(embedding, pose)
        if self._candidate_id is None:
            self._set_card("warn", "등록되지 않은 얼굴입니다")
            return

        name = self._display_name(self._candidate_user)
        ch = self.challenge
        if now - self._candidate_since < core.RECOGNIZE_HOLD_SECONDS:
            self._set_card("search", f"{name} 학생 얼굴 확인 중", "카메라를 바라봐 주세요")
            return
        ch.update(pose, embedding)
        if ch.done_at is None:
            self._set_card("turn", f"{name} 학생, {ch.instruction()}", "화면의 안내를 따라 천천히 돌려 주세요",
                           challenge=ch.to_dict(pose))
            return
        # 다 돌렸다 — 정면에서 같은 학생인지 다시 확인한다 (돌리는 사이 사람이 바뀌지 않았는지)
        if matched_user is None or matched_user["user_id"] != self._candidate_id:
            self._set_card("turn", f"{name} 학생, 정면을 바라봐 주세요", "", challenge=ch.to_dict(pose))
            return
        if not self.passes_antispoof():
            if (now - self.last_fake_time) <= core.LIVENESS_VALID_SECONDS:
                self._set_card("error", "사진이나 화면으로 판단됩니다", "본인이 직접 카메라 앞에 서 주세요")
            elif self.last_liveness_status == "input_rejected":
                self._set_card("warn", "얼굴이 너무 가깝습니다", "카메라에서 조금 물러나 주세요")
            else:
                self._set_card("warn", f"{name} 학생 얼굴 확인 중", "카메라를 정면으로 바라봐 주세요")
            return

        student = {"user_id": self._candidate_id, "name": name}
        if self.mode == "lookup":
            self._enter("stats", {"student": student, **self._stats_for(student["user_id"])})
            return
        log_type = self.pending_action
        success, detail = core.log_attendance(student["user_id"], name, log_type)
        self._enter("result", {"student": student, "log_type": log_type, "success": success,
                               "detail": detail, "today": core.get_today_attendance_status(student["user_id"])})

    def _process_camera_test(self, faces, matched_user, best_sim, pose):
        # 고개 방향 값도 같이 보여준다 — HEAD_TURN_RATIO / HEAD_PITCH_DELTA를 이 카메라에 맞게 조정할 때 참고한다
        direction = "" if pose is None else f" · 좌우 {pose[0]:+.2f} · 상하 {pose[1]:.2f}"
        if matched_user is not None:
            self._set_card("ok", f"{self._display_name(matched_user)} 학생 인식",
                           f"유사도 {best_sim:.2f}{direction}", round(best_sim, 2))
        elif faces:
            self._set_card("unknown", "등록되지 않은 얼굴입니다",
                           f"유사도 {max(best_sim, 0):.2f}{direction}", round(max(best_sim, 0), 2))
        else:
            self._set_card("search", "얼굴을 찾는 중..")

    @staticmethod
    def _stats_for(user_id):
        weekly = core.get_weekly_attendance_stats(user_id)
        return {
            "weekly": weekly,
            "today": core.get_today_attendance_status(user_id),
            "required_seconds": core.WEEKLY_REQUIRED_SECONDS,
            "required_days": core.WEEKLY_REQUIRED_DAYS,
        }

    # ---------- 영상 ----------
    def jpeg_frame(self, max_width=960):
        """카메라 화면에 얼굴 상자와 이름을 그려 JPEG로 만든다. 카메라가 꺼져 있으면 None."""
        frame = self.latest_frame
        if not self.camera_on or frame is None:
            return None
        labels = self.face_labels
        src_h, src_w = frame.shape[:2]
        scale = min(1.0, max_width / src_w)
        display = cv2.resize(frame, (int(src_w * scale), int(src_h * scale))) if scale < 1.0 else frame.copy()

        tags = []
        for bbox, text, known in labels:
            x1, y1, x2, y2 = (np.asarray(bbox[:4]) * scale).astype(int)
            color_bgr = (74, 163, 22) if known else (43, 54, 194)  # 초록 #16A34A / 빨강 #C2362B
            cv2.rectangle(display, (x1, y1), (x2, y2), color_bgr, 3)
            tags.append((x1, y1, text, color_bgr[::-1]))

        if tags:
            # cv2.putText는 한글을 못 그려서 글자는 PIL로 그린다
            img = Image.fromarray(cv2.cvtColor(display, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(img)
            font = core.get_korean_font(18)
            for box_x, box_y, text, color_rgb in tags:
                left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
                tag_w, tag_h = right - left + 16, bottom - top + 10
                tag_x = max(0, min(box_x - 1, img.width - tag_w))
                tag_y = box_y - tag_h if box_y - tag_h >= 0 else box_y
                draw.rectangle((tag_x, tag_y, tag_x + tag_w, tag_y + tag_h), fill=color_rgb)
                draw.text((tag_x + 8 - left, tag_y + 5 - top), text, font=font, fill=(255, 255, 255))
            display = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)

        ok, buf = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    def close(self):
        self.is_running = False
        self.camera_on = False
        try:
            self.face_conn.close()
        except OSError:
            pass
        if self.face_proc.is_alive():
            self.face_proc.terminate()

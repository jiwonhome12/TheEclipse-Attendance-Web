"""
웹 버전 출석체크의 엔진 — 카메라, 얼굴 인식, 사람 확인(눈 깜빡임·위조판별), 출근/퇴근 흐름.
tkinter 앱(AttendanceApp)이 화면과 섞어서 하던 일을 화면 없이 그대로 옮겼다.
브라우저는 state()로 지금 상태를 받아 그리기만 하고, 버튼을 누르면 start_session()/go_idle()을 부른다.
"""
import multiprocessing
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
SESSION_MODES = ("attendance", "lookup", "test")


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
        self._candidate_since = 0.0

        self.ai_ready = False
        self.ai_failed = False
        self.camera_on = False
        self.camera_error = False
        self.camera_generation = 0      # 카메라를 켜고 끌 때마다 1씩 증가 — 이전 세션의 늦은 인식 결과를 버린다
        self.latest_frame = None
        self.frame_seq = 0
        self.face_labels = []           # 영상에 그릴 (bbox, 글자, 등록 여부) — 인식 결과가 올 때마다 바뀐다
        self.face_labels_seq = -1       # face_labels를 계산한 프레임 번호

        # 사진/영상 판별 상태 (인식 프로세스가 값을 보내주면 켜진다)
        self.liveness_enabled = False
        self.last_blink_time = 0.0
        self._eye_closed = False
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

    def _set_card(self, state, title, detail="", sim=None):
        card = {"state": state, "title": title, "detail": detail, "sim": sim}
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
            self.mode = mode
            self.pending_action = action if mode == "attendance" else None
            self.session_started = time.time()
            self.payload = None
            self._candidate_id = None
            self._reset_liveness()
            self.card = {}
            self._set_card("loading" if not self.ai_ready else "search",
                           "얼굴 인식 준비 중.." if not self.ai_ready else "얼굴을 찾는 중..")
            self._set_camera(True)
            self._changed()

    def go_idle(self):
        with self._lock:
            self._enter("idle", None)

    def _enter(self, mode, payload):
        """세션이 끝나 결과 / 출석 현황 / 메인 화면으로 넘어간다 — 카메라는 끈다."""
        self.mode = mode
        self.pending_action = None
        self.payload = payload
        self.session_started = time.time()
        self._candidate_id = None
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
        if on:
            self.camera_error = False

    def _reset_liveness(self):
        """다음 사람이 앞 사람의 깜빡임/실제 사람 판정을 이어받지 않도록 비운다."""
        self.last_blink_time = 0.0
        self._eye_closed = False
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
                if self.mode in SESSION_MODES and self.card.get("state") == "loading":
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
                results, blink_score, liveness = self.face_conn.recv()
                rest = core.AI_MIN_INTERVAL - (time.time() - started)
                if rest > 0:
                    time.sleep(rest)
                with self._lock:
                    # 분석하는 사이에 카메라를 껐다 켰다면(다음 사람) 예전 얼굴 결과는 버린다
                    if not (self.camera_on and generation == self.camera_generation):
                        continue
                    self._update_blink_state(blink_score)
                    self._update_liveness_state(liveness)
                    matched_user, best_sim = self._match_faces(results, seq)
                    if self.mode in ("attendance", "lookup"):
                        self._process_attendance(frame, results, matched_user)
                    elif self.mode == "test":
                        self._process_camera_test(results, matched_user, best_sim)
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

    def _update_blink_state(self, blink_score):
        """눈을 감았다가(BLINK_CLOSED_SCORE 이상) 다시 떴을 때(BLINK_OPEN_SCORE 이하) 한 번 깜빡인 것으로 본다."""
        if blink_score is None:
            return
        self.liveness_enabled = True
        if blink_score >= core.BLINK_CLOSED_SCORE:
            self._eye_closed = True
        elif self._eye_closed and blink_score <= core.BLINK_OPEN_SCORE:
            self._eye_closed = False
            self.last_blink_time = time.time()

    def has_recent_blink(self):
        if not self.liveness_enabled:
            return True
        return (time.time() - self.last_blink_time) <= core.BLINK_VALID_SECONDS

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

    def _process_attendance(self, frame, faces, matched_user):
        """
        같은 학생이 RECOGNIZE_HOLD_SECONDS 동안 계속 인식되고 사람 확인(위조판별 + 눈 깜빡임)까지
        통과하면 출근/퇴근을 기록하거나(attendance) 출석 현황을 보여준다(lookup).
        """
        if matched_user is None:
            self._candidate_id = None
            if faces:
                self._set_card("warn", "등록되지 않은 얼굴입니다")
            else:
                self._set_card("search", "얼굴을 찾는 중..")
            return

        name = self._display_name(matched_user)
        checking = f"{name} 학생 얼굴 확인 중"
        now = time.time()
        if self._candidate_id != matched_user["user_id"]:
            self._candidate_id = matched_user["user_id"]
            self._candidate_since = now
        if now - self._candidate_since < core.RECOGNIZE_HOLD_SECONDS:
            self._set_card("blink", checking, "눈을 천천히 감았다 떠 주세요")
            return
        if not self.passes_antispoof():
            if (now - self.last_fake_time) <= core.LIVENESS_VALID_SECONDS:
                self._set_card("error", "사진이나 화면으로 판단됩니다", "본인이 직접 카메라 앞에 서 주세요")
            elif self.last_liveness_status == "input_rejected":
                self._set_card("warn", "얼굴이 너무 가깝습니다", "카메라에서 조금 물러나 주세요")
            else:
                self._set_card("warn", checking, "카메라를 정면으로 바라봐 주세요")
            return
        if not self.has_recent_blink():
            self._set_card("blink", checking, "눈을 천천히 감았다 떠 주세요")
            return

        student = {"user_id": matched_user["user_id"], "name": name}
        if self.mode == "lookup":
            self._enter("stats", {"student": student, **self._stats_for(student["user_id"])})
            return
        log_type = self.pending_action
        success, detail = core.log_attendance(student["user_id"], name, log_type)
        self._enter("result", {"student": student, "log_type": log_type, "success": success,
                               "detail": detail, "today": core.get_today_attendance_status(student["user_id"])})

    def _process_camera_test(self, faces, matched_user, best_sim):
        if matched_user is not None:
            self._set_card("ok", f"{self._display_name(matched_user)} 학생 인식",
                           f"유사도 {best_sim:.2f}", round(best_sim, 2))
        elif faces:
            self._set_card("unknown", "등록되지 않은 얼굴입니다",
                           f"유사도 {max(best_sim, 0):.2f}", round(max(best_sim, 0), 2))
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

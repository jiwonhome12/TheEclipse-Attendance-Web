"""
출석체크 프로그램의 화면과 상관없는 부분 — 설정, 카메라, 얼굴 인식, DB, 학생 명단, 출결 계산.
tkinter 화면(attendance_app.py)과 앞으로 만들 웹 서버가 같이 쓴다. 여기서는 tkinter를 불러오지 않는다.
"""

import os

# MediaPipe/TFLite는 C++ 쪽에서 stderr로 안내 로그(XNNPACK delegate, feedback manager 등)를 직접 찍는다.
# 파이썬 logging으로는 막히지 않고, 이 설정은 mediapipe를 불러오기 전에 해야 효과가 있어서 맨 위에 둔다.
# (그래도 남는 absl 로그는 silence_native_stderr()가 모델을 불러오는 동안만 막는다)
os.environ.setdefault("GLOG_minloglevel", "3")        # 0=INFO 1=WARNING 2=ERROR 3=FATAL
os.environ.setdefault("GLOG_stderrthreshold", "3")
os.environ.setdefault("GLOG_logtostderr", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
os.environ.setdefault("ABSL_MIN_LOG_LEVEL", "3")

import collections
import contextlib
import cv2
import io
import numpy as np
import time
import sqlite3
import json
import shutil
import sys
import calendar
from datetime import date, datetime, timedelta
from PIL import ImageFont


# 프로그램 파일이 있는 폴더 — 어느 폴더에서 실행해도 같은 DB/모델을 쓰도록 기준으로 삼는다
# exe(PyInstaller)로 실행하면 데이터(DB·사진·로그)는 exe 옆에, 같이 묶은 파일은 _internal 폴더에 있다.
IS_FROZEN = getattr(sys, "frozen", False)
if IS_FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = APP_DIR

# 콘솔 없이(pythonw, 작업 스케줄러) 실행하면 sys.stdout/stderr가 None이라 모델 로딩 중
# 진행 표시 등이 출력하다 죽을 수 있다. 이때는 logs/app.log 파일로 남긴다.
# (얼굴 인식 프로세스도 이 파일(core.py)을 다시 불러오므로 거기에도 똑같이 적용된다)
if sys.stdout is None or sys.stderr is None:
    os.makedirs(os.path.join(APP_DIR, "logs"), exist_ok=True)
    _log_file = open(os.path.join(APP_DIR, "logs", "app.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log_file
    sys.stderr = sys.stderr or _log_file

DB_PATH = os.path.join(APP_DIR, "faces.db")
# 이 PC(키오스크)에만 해당하는 화면 설정 — 관리자 화면에서 바꾼다
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")


def load_settings():
    """settings.json을 읽는다. 없거나 깨졌으면 빈 설정(전부 기본값)."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_setting(key, value):
    """설정 하나를 저장한다. 임시 파일에 다 쓴 뒤 바꿔치기해서, 쓰는 도중 꺼져도 파일이 깨지지 않게 한다."""
    data = load_settings()
    data[key] = value
    temp_path = SETTINGS_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, SETTINGS_PATH)
THRESHOLD = 0.50
DEPARTMENT_NAME = "소프트웨어전공"  # 학과는 한 곳뿐이라 입력받지 않고 이 값으로 고정한다

# 사진/화면 영상으로 대리 출석하는 것을 막기 위한 눈 깜빡임 검사 (MediaPipe FaceLandmarker)
# 모델 파일이 없으면 검사는 자동으로 꺼지고, 출퇴근은 예전처럼 그대로 동작한다.
LIVENESS_MODEL_PATH = os.path.join(RESOURCE_DIR, "face_landmarker.task")
LIVENESS_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)
BLINK_CLOSED_SCORE = 0.5    # 이 값을 넘으면 눈을 감은 것으로 본다
BLINK_OPEN_SCORE = 0.3      # 다시 이 아래로 내려오면 한 번 깜빡인 것으로 센다
BLINK_VALID_SECONDS = 10    # 출퇴근을 누르기 전 이 시간 안에 깜빡임이 있어야 한다

# 출근/퇴근 확인 중 "눈을 깜빡여 주세요" 안내가 뜰 때 같이 보여주는 힌트 이미지.
# 파일이 없어도 이미지만 안 뜰 뿐 출퇴근은 그대로 동작한다.
BLINK_HINT_IMAGE_PATH = os.path.join(RESOURCE_DIR, "assets", "blink_hint.gif")

# InsightFace 공식 위조판별(liveness) 애드온 기준 점수. 이 값 이상이면 실제 사람으로 본다.
# 모델은 처음 실행할 때 ~/.insightface/addons/liveness.onnx 로 자동 다운로드된다.
LIVENESS_THRESHOLD = 0.8
LIVENESS_VALID_SECONDS = 3  # 출퇴근을 누르기 전 이 시간 안에 '실제 사람' 판정이 있어야 한다

# 출근/퇴근 버튼을 누른 뒤의 흐름
TEST_TIMEOUT = 60              # 카메라 테스트 화면도 켜둔 채 잊지 않도록 이 시간이 지나면 메인 화면으로 돌아간다
RECOGNIZE_HOLD_SECONDS = 0.6   # 같은 사람으로 이 시간 동안 계속 인식돼야 기록한다 (지나가는 사람 오인식 방지)

# 화면별로 저절로 메인 화면으로 돌아가는 시간(초). 관리자 [설정] 탭에서 5초 단위(5~60초)로 바꾸고 settings.json에 저장한다.
#   verify : 출근/퇴근/출석 조회 — 이 시간 안에 얼굴이 확인되지 않으면 돌아간다
#   result : "OOO 학생 출근 되었습니다" 결과 화면
#   stats  : 이번 주 출석 현황 (뒤 사람이 보지 않도록)
TIMEOUT_DEFAULTS = {"verify": 30, "result": 10, "stats": 20}
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_STEP = 5, 60, 5
timeouts = dict(TIMEOUT_DEFAULTS)  # load_timeouts()가 DB 값으로 채운다

# 출퇴근할 때 찍힌 얼굴 사진 — 관리자 화면에서만 볼 수 있고, 보관 기간이 지나면 자동으로 지운다
PHOTO_DIR = os.path.join(APP_DIR, "attendance_photos")
PHOTO_WIDTH = 320              # 640x480 원본을 320x240으로 줄여서 저장 (장당 약 10~15KB)
PHOTO_JPEG_QUALITY = 70
PHOTO_RETENTION_DAYS = 90      # 3개월

# 주간 출석 기준 — 월요일~일요일 동안 채워야 하는 시간/일수
WEEKLY_REQUIRED_SECONDS = 20 * 3600
WEEKLY_REQUIRED_DAYS = 3
# 00:00~06:00 사이에 찍은 출근/퇴근은 무효. 06:00~24:00 사이 기록만 인정한다.
VALID_ATTENDANCE_START = "06:00:00"

# ==========================================
# 윈도우 / macOS 공통 실행을 위한 플랫폼별 설정
# ==========================================
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")


def _is_raspberry_pi():
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return b"Raspberry Pi" in f.read()
    except OSError:
        return False


# ===== 라즈베리파이처럼 느린 기기용 설정 =====
# 라즈베리파이에서는 얼굴 분석이 CPU를 쉬지 않고 쓰고, 카메라 30fps를 크게 확대해 그리는 것만으로도
# 과부하가 걸려 화면이 끊기고 카메라가 멈췄다. 그래서 받는 양·그리는 양·분석 빈도를 줄인다.
# PC에서 같은 설정을 시험하려면 THE_ECLIPSE_LOW_POWER=1 로 실행한다.
IS_RASPBERRY_PI = _is_raspberry_pi()
LOW_POWER = IS_RASPBERRY_PI or os.environ.get("THE_ECLIPSE_LOW_POWER") == "1"
CAMERA_FPS = 15 if LOW_POWER else 30          # 카메라에서 받는 초당 프레임
DISPLAY_FPS = 10 if LOW_POWER else None       # 화면에 영상을 그리는 최대 횟수/초 (None = 받는 대로)
AI_MIN_INTERVAL = 0.25 if LOW_POWER else 0.0  # 얼굴 분석 사이에 쉬는 최소 간격(초) — 화면·카메라에 CPU를 양보
CAMERA_STALL_SECONDS = 3.0                    # 이 시간 동안 새 영상이 안 오면 카메라를 닫았다 다시 연다



def inference_providers():
    """
    onnxruntime 실행 장치. macOS에서는 CoreML을 먼저 쓴다.
    (i9-9880H 측정: 얼굴 1명 기준 CPU 85ms → CoreML 36ms, 임베딩 코사인 유사도 0.99999 로 결과 동일)
    """
    import onnxruntime
    available = onnxruntime.get_available_providers()
    providers = []
    if IS_MAC and "CoreMLExecutionProvider" in available:
        providers.append("CoreMLExecutionProvider")
    providers.append("CPUExecutionProvider")
    return providers


# 얼굴 인식에 쓸 CPU 스레드 수. 12스레드(6코어)에서 측정: 6개면 화면이 27fps로 끊기고 4개면 29fps.
INFERENCE_THREADS = max(2, (os.cpu_count() or 4) // 3)


# ==========================================
# 0-0. 얼굴 인식 전용 프로세스
# ==========================================
# 같은 프로세스의 스레드에서 추론을 돌리면 파이썬 GIL과 CPU를 화면 그리기와 나눠 써서
# 카메라 화면이 뚝뚝 끊겼다(측정: 인식 끄면 29.6fps, 켜면 25~27fps + 50ms 이상 멈춤 다수).
# 별도 프로세스로 분리하면 UI 프로세스는 그리기만 한다.
class DetectedFace:
    __slots__ = ("bbox", "embedding")

    def __init__(self, bbox, embedding):
        self.bbox = bbox
        self.embedding = embedding


def download_liveness_model():
    """
    깜빡임 검사용 모델(face_landmarker.task, 약 3.7MB)을 내려받는다.
    인터넷이 없거나 실패하면 False를 돌려주고, 이때는 깜빡임 검사 없이 프로그램이 그대로 동작한다.
    """
    import urllib.request

    temp_path = LIVENESS_MODEL_PATH + ".part"
    try:
        print("[출석] 깜빡임 검사 모델을 내려받는 중입니다... (최초 1회)")
        urllib.request.urlretrieve(LIVENESS_MODEL_URL, temp_path)
        os.replace(temp_path, LIVENESS_MODEL_PATH)
        print("[출석] 깜빡임 검사 모델 준비 완료")
        return True
    except Exception as e:
        print(f"[출석] 깜빡임 검사 모델을 받지 못했습니다 ({e}). 깜빡임 검사 없이 실행합니다.")
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return False


@contextlib.contextmanager
def silence_native_stderr():
    """
    C++ 라이브러리(MediaPipe/TFLite)가 stderr로 직접 쓰는 안내 메시지를 잠깐 막는다.
    환경변수(GLOG_*)로 안 막히는 absl 로그까지 확실히 숨기려면 파일 디스크립터 수준에서 돌려야 한다.
    이 블록이 끝나면 원래대로 돌려놓으므로 우리 앱의 print·오류 메시지는 그대로 보인다.
    """
    try:
        fd = sys.stderr.fileno()
    except (AttributeError, ValueError, io.UnsupportedOperation):
        yield  # pythonw처럼 진짜 stderr가 없는 경우 — 막을 것도 없다
        return

    saved_fd = os.dup(fd)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stderr.flush()
        os.dup2(devnull_fd, fd)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved_fd, fd)
        os.close(saved_fd)
        os.close(devnull_fd)


def create_blink_detector():
    """
    MediaPipe FaceLandmarker를 준비한다. 눈 깜빡임 정도(blendshape)를 읽어
    사진/화면으로 얼굴을 들이대는 것을 걸러내는 데 쓴다.
    모델 파일이 없거나 MediaPipe를 못 불러오면 None을 돌려주고, 이 경우 깜빡임 검사는 꺼진다.
    """
    if not os.path.exists(LIVENESS_MODEL_PATH):
        # 다른 PC에서 처음 실행할 때 모델 파일이 없으면 한 번 내려받는다 (실패하면 검사만 꺼진다)
        if not download_liveness_model():
            return None
    try:
        # 모델을 불러오는 동안 MediaPipe가 찍는 안내 로그는 숨긴다 (silence_native_stderr 참고)
        with silence_native_stderr():
            from mediapipe.tasks.python import BaseOptions
            from mediapipe.tasks.python import vision
            # 경로 대신 파일 내용을 넘긴다 — MediaPipe는 한글이 들어간 경로(예: '새 폴더')를 열지 못해서
            # 경로로 넘기면 깜빡임 검사가 소리 없이 꺼진다
            with open(LIVENESS_MODEL_PATH, "rb") as f:
                model_bytes = f.read()
            return vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_buffer=model_bytes),
                running_mode=vision.RunningMode.VIDEO,
                output_face_blendshapes=True,   # eyeBlinkLeft / eyeBlinkRight 값을 쓰기 위해
                num_faces=1,
            ))
    except Exception as e:
        print(f"[출석] 깜빡임 검사를 켜지 못했습니다 ({type(e).__name__}: {e})")
        return None


def crop_face_region(frame, bbox, margin=0.35):
    """얼굴 bbox 주변을 여유 있게 잘라낸다. 화면 밖으로 나가지 않게 잘라서 돌려준다."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    mx, my = (x2 - x1) * margin, (y2 - y1) * margin
    x1 = max(0, int(x1 - mx))
    y1 = max(0, int(y1 - my))
    x2 = min(w, int(x2 + mx))
    y2 = min(h, int(y2 + my))
    if x2 - x1 < 20 or y2 - y1 < 20:
        return None
    return frame[y1:y2, x1:x2]


def detect_blink_score(landmarker, frame, timestamp_ms):
    """
    프레임에서 눈이 감긴 정도(0~1)를 구한다. 양쪽 눈 중 더 크게 감긴 쪽 값을 쓴다.
    얼굴을 못 찾았거나 검사가 꺼져 있으면 None.
    주의: 인식 대상 얼굴만 잘라서 넘겨야 한다. 화면 전체를 넘기면 사진을 들고 있는
    사람의 깜빡임이 대신 잡혀서 사진이 통과해 버린다.
    """
    if landmarker is None or frame is None or frame.size == 0:
        return None
    try:
        import mediapipe as mp
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        result = landmarker.detect_for_video(image, timestamp_ms)
    except Exception:
        return None

    if not result.face_blendshapes:
        return None
    scores = {b.category_name: b.score for b in result.face_blendshapes[0]}
    return max(scores.get("eyeBlinkLeft", 0.0), scores.get("eyeBlinkRight", 0.0))


# ---------- mediapipe를 쓸 수 없는 기기(라즈베리파이 등)용 대체 깜빡임 검사 ----------
# 얼굴 인식 모델 묶음(buffalo_l)에 들어 있는 106점 얼굴 윤곽 모델(2d106det)의 점 번호
LMK106_LEFT_EYE = slice(33, 43)
LMK106_RIGHT_EYE = slice(87, 97)
LMK106_MOUTH = slice(52, 72)
LANDMARK_MODEL_PATH = os.path.join(os.path.expanduser("~"), ".insightface", "models", "buffalo_l", "2d106det.onnx")


def eye_openness(landmarks):
    """
    106점 얼굴 윤곽에서 두 눈이 뜬 정도를 (왼눈, 오른눈)으로 구한다.
    눈의 세로 폭을 '눈~입 거리'로 나눈 값이라, 사진을 앞뒤로 기울여 얼굴 전체가 납작해져도 값이 거의 그대로다
    (진짜 깜빡임은 눈만 납작해진다). 세로 방향은 두 눈을 잇는 선에 수직으로 잡아서 고개를 기울여도 괜찮다.
    """
    lmk = np.asarray(landmarks, dtype=np.float32)
    left, right, mouth = lmk[LMK106_LEFT_EYE], lmk[LMK106_RIGHT_EYE], lmk[LMK106_MOUTH]
    left_c, right_c = left.mean(axis=0), right.mean(axis=0)
    axis = right_c - left_c
    length = float(np.linalg.norm(axis))
    if length < 1.0:
        return None
    axis /= length
    normal = np.array([-axis[1], axis[0]], dtype=np.float32)
    face_height = abs(float((mouth.mean(axis=0) - (left_c + right_c) / 2) @ normal))
    if face_height < 1.0:
        return None

    def height(points):
        proj = points @ normal
        return float(proj.max() - proj.min())

    return height(left) / face_height, height(right) / face_height


class LandmarkBlinkMeter:
    """
    mediapipe 없이 눈이 감긴 정도(0~1)를 낸다 — mediapipe의 eyeBlink 값과 같은 뜻이라 나머지 판정은 그대로 쓴다.
    - 사람마다 원래 눈 크기가 달라서 고정 기준 대신 '그 사람의 최근 뜬 눈 크기' 대비 얼마나 줄었는지로 본다.
    - 두 눈이 함께 감겨야 감긴 것으로 본다 (고개를 돌려 한쪽 눈이 가려지는 것은 깜빡임이 아니다).
    """
    HISTORY = 30             # 기준(뜬 눈 크기)을 잡을 최근 측정 수
    MIN_SAMPLES = 5          # 이만큼 모이기 전에는 판단하지 않는다 (None)
    RESET_AFTER_MISSES = 5   # 얼굴이 이만큼 연속으로 안 보이면 다른 사람으로 보고 기준을 새로 잡는다

    def __init__(self, landmark_model):
        self.model = landmark_model
        self.history = collections.deque(maxlen=self.HISTORY)
        self.misses = 0

    def no_face(self):
        self.misses += 1
        if self.misses >= self.RESET_AFTER_MISSES:
            self.history.clear()

    def score(self, frame, face):
        self.misses = 0
        try:
            openness = eye_openness(self.model.get(frame, face))
        except Exception:
            return None
        if openness is None:
            return None
        current = max(openness)  # 한쪽이라도 떠 있으면 뜬 것 — 두 눈이 다 감겨야 감긴 정도가 올라간다
        self.history.append(current)
        if len(self.history) < self.MIN_SAMPLES:
            return None
        baseline = float(np.percentile(self.history, 90))  # 깜빡이는 순간이 섞여도 '뜬 눈' 크기를 가리킨다
        if baseline <= 0:
            return None
        return float(np.clip(1.0 - current / baseline, 0.0, 1.0))


def create_landmark_blink_meter(sess_options=None):
    """106점 얼굴 윤곽 모델로 대체 깜빡임 검사기를 만든다. 모델 파일이 없거나 실패하면 None."""
    if not os.path.exists(LANDMARK_MODEL_PATH):
        print(f"[출석] 눈 깜빡임 대체 검사용 모델이 없습니다 ({LANDMARK_MODEL_PATH})")
        return None
    try:
        import onnxruntime
        from insightface.model_zoo import model_zoo
        model = model_zoo.get_model(LANDMARK_MODEL_PATH, providers=inference_providers())
        model.prepare(ctx_id=0)
        if sess_options is not None:
            # 다른 얼굴 모델과 똑같이 CPU 스레드 수를 제한한다 (카메라·화면이 밀리지 않게)
            model.session = onnxruntime.InferenceSession(
                LANDMARK_MODEL_PATH, sess_options=sess_options, providers=inference_providers())
        return LandmarkBlinkMeter(model)
    except Exception as e:
        print(f"[출석] 눈 깜빡임 대체 검사를 켜지 못했습니다 ({type(e).__name__}: {e})")
        return None


def blink_mode_forced_to_landmark():
    """THE_ECLIPSE_BLINK=landmark 이면 mediapipe가 있어도 대체 검사를 쓴다 (PC에서 라즈베리파이 방식을 시험할 때)."""
    return os.environ.get("THE_ECLIPSE_BLINK", "").lower() == "landmark"


_instance_mutex = None


def acquire_single_instance():
    """
    프로그램이 하나만 켜지게 한다. 이미 켜져 있으면 False.
    윈도우는 명명 뮤텍스, 그 외(라즈베리파이·맥)는 잠금 파일(flock)을 쓴다 —
    둘 다 프로그램이 꺼지면 OS가 알아서 풀어준다.
    """
    global _instance_mutex
    if not IS_WINDOWS:
        import fcntl
        try:
            lock_file = open(os.path.join(APP_DIR, ".app.lock"), "w")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        _instance_mutex = lock_file  # 닫히면 잠금이 풀리므로 프로그램이 끝날 때까지 들고 있는다
        return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\TheEclipseAttendance")
    ERROR_ALREADY_EXISTS = 183
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def create_face_app():
    """InsightFace 얼굴 인식기를 만든다 (처음이면 모델을 ~/.insightface 로 내려받는다)."""
    from insightface.app import FaceAnalysis

    # addons=["liveness"] — InsightFace 공식 위조판별(사진/화면 판별) 애드온.
    # observe 모드로 두면 가짜로 보여도 얼굴 인식은 그대로 하고, 판정 결과만 받아서
    # "사진으로 보입니다" 같은 안내를 우리 쪽에서 띄울 수 있다.
    try:
        app = FaceAnalysis(
            name="buffalo_l",
            allowed_modules=['detection', 'recognition'],
            addons=["liveness"],
            liveness_mode="observe",
            liveness_threshold=LIVENESS_THRESHOLD,
            providers=["CPUExecutionProvider"]
        )
    except Exception as e:
        # 구버전 insightface이거나 애드온을 못 받은 경우 — 위조판별 없이 그대로 동작한다
        print(f"[출석] 위조판별 애드온을 쓸 수 없습니다 ({e}). 깜빡임 검사만 사용합니다.")
        app = FaceAnalysis(
            name="buffalo_l",
            allowed_modules=['detection', 'recognition'],
            providers=["CPUExecutionProvider"]
        )
    app.prepare(ctx_id=0, det_size=(256, 256))
    return app


def face_inference_process(conn, threads):
    import onnxruntime

    # 느린 기기(라즈베리파이)에서는 얼굴 분석 프로세스의 우선순위를 낮춘다 — 분석이 CPU를 다 써도
    # 카메라 읽기와 화면 그리기가 먼저 돌아서 화면이 멈추거나 카메라가 끊기지 않는다 (분석만 조금 늦어진다)
    if LOW_POWER and hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass
    elif LOW_POWER and IS_WINDOWS:  # PC에서 THE_ECLIPSE_LOW_POWER=1로 시험할 때도 똑같이 동작하게
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x4000
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),
                                                BELOW_NORMAL_PRIORITY_CLASS)

    app = create_face_app()

    # onnxruntime은 기본으로 CPU 코어를 전부 써서 카메라/화면 쪽이 밀린다.
    # insightface는 세션 옵션을 넘겨받지 않으므로 같은 모델 파일로 세션만 다시 만든다.
    opts = onnxruntime.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    for model in app.models.values():
        model.session = onnxruntime.InferenceSession(
            model.session.model_path, sess_options=opts, providers=inference_providers()
        )

    # 사진/영상 판별용 깜빡임 검출기 — mediapipe를 먼저 쓰고, 이 기기에서 안 되면(라즈베리파이 등)
    # 얼굴 인식 모델 묶음의 106점 윤곽 모델로 대신 잰다. 둘 다 없으면 깜빡임 검사는 꺼진다.
    blink_landmarker = None if blink_mode_forced_to_landmark() else create_blink_detector()
    blink_meter = None
    if blink_landmarker is None:
        blink_meter = create_landmark_blink_meter(opts)
        if blink_meter is not None:
            print("[출석] 눈 깜빡임 검사: mediapipe 대신 얼굴 윤곽 모델(2d106det)을 사용합니다.")

    conn.send("ready")
    while True:
        frame = conn.recv()
        faces = app.get(frame)

        # 깜빡임은 "인식된 얼굴"에서만 본다. 화면 전체로 보면 사진을 들고 있는 사람의
        # 깜빡임이 대신 잡혀서 사진이 통과한다. 가장 큰 얼굴(= 인식 대상)만 잘라서 검사한다.
        blink_score = None
        liveness = None   # (is_live, status) — 위조판별 애드온 결과
        if not faces and blink_meter is not None:
            blink_meter.no_face()
        if faces:
            main_face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
            if blink_landmarker is not None:
                face_crop = crop_face_region(frame, main_face.bbox)
                # MediaPipe VIDEO 모드는 타임스탬프가 계속 커져야 한다
                blink_score = detect_blink_score(blink_landmarker, face_crop, int(time.perf_counter() * 1000))
            elif blink_meter is not None:
                blink_score = blink_meter.score(frame, main_face)

            result = getattr(main_face, "liveness", None)
            if result is not None:
                liveness = (result.is_live, result.status)

        conn.send(([(f.bbox, f.embedding) for f in faces], blink_score, liveness))


def ensure_camera_permission(timeout=60):
    """
    macOS에서 카메라 권한을 미리 요청하고 사용자가 허용/거부할 때까지 기다린다.
    OpenCV도 권한을 요청하지만 응답을 기다리지 않고 바로 실패 처리해서, 처음 실행하면
    [허용]을 눌러도 그 실행에서는 카메라가 안 켜진다. 권한 상태: 0=미결정 1=제한 2=거부 3=허용.
    pyobjc(pyobjc-framework-AVFoundation)가 없으면 아무것도 하지 않는다.
    """
    if not IS_MAC:
        return True
    try:
        import AVFoundation
        from Foundation import NSRunLoop, NSDate
    except ImportError:
        return True
    media = AVFoundation.AVMediaTypeVideo
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(media)
    if status != 0:
        return status == 3

    result = {}

    def on_done(granted):
        result["granted"] = bool(granted)

    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(media, on_done)
    deadline = time.time() + timeout
    while "granted" not in result and time.time() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
    return result.get("granted", False)


def open_camera(index=0):
    """
    OS에 맞는 백엔드로 웹캠을 연다.
    - Windows: DSHOW가 MSMF와 FPS는 같고 여는 속도가 훨씬 빠르다
    - macOS: DSHOW가 없으므로 AVFoundation을 쓴다
    - 그 외(리눅스 등)는 V4L2
    원하는 백엔드로 못 열면 OpenCV 기본값(CAP_ANY)으로 한 번 더 시도한다.
    """
    if IS_WINDOWS:
        backend = cv2.CAP_DSHOW
    elif IS_MAC:
        backend = cv2.CAP_AVFOUNDATION
    else:
        backend = cv2.CAP_V4L2
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(index)
    return cap


def preferred_camera_index():
    """
    쓸 카메라 번호를 직접 정했으면 그 번호, 아니면 None.
    환경변수 THE_ECLIPSE_CAMERA 가 먼저이고, 없으면 프로그램 폴더의 camera.txt (숫자 한 줄)를 읽는다.
    노트북 내장 카메라가 0번이면 외장 USB 카메라는 보통 1번이다. 번호는 --list-cameras 로 확인한다.
    """
    value = os.environ.get("THE_ECLIPSE_CAMERA", "").strip()
    if not value:
        try:
            with open(os.path.join(APP_DIR, "camera.txt"), encoding="utf-8-sig") as f:
                value = f.read().strip()
        except OSError:
            return None
    return int(value) if value.isdigit() else None


ACTIVE_CAMERA_INDEX = None  # 지금 실제로 연결된 카메라 번호 (관리자 화면의 카메라 선택에서 보여준다)


def save_preferred_camera_index(index):
    """관리자 화면에서 고른 카메라 번호를 camera.txt에 저장한다 (다음 실행에도 유지)."""
    with open(os.path.join(APP_DIR, "camera.txt"), "w", encoding="utf-8") as f:
        f.write(str(int(index)))


def camera_candidates():
    """
    열어 볼 카메라 번호. 직접 정한 번호(preferred_camera_index)가 있으면 그것이 먼저이고, 없으면 0번이 먼저.
    리눅스(라즈베리파이)는 과부하·전원 부족으로 USB 카메라가 잠깐 끊겼다 다시 붙으면 번호가 바뀌는
    경우가 있어서(/dev/video0 → video1), 있는 장치를 차례로 시도한다.
    """
    preferred = preferred_camera_index()
    first = [preferred] if preferred is not None else []
    if IS_WINDOWS or IS_MAC:
        return first + [i for i in (0,) if i not in first]
    try:
        found = sorted(int(name[5:]) for name in os.listdir("/dev")
                       if name.startswith("video") and name[5:].isdigit())
    except OSError:
        found = []
    rest = [0] + [i for i in found if i != 0][:10]
    return first + [i for i in rest if i not in first]


def list_cameras(max_index=6):
    """--list-cameras: 번호별로 실제 영상이 나오는 카메라를 찾아 안내 문구 목록으로 돌려준다."""
    lines = []
    for index in range(max_index):
        cap = open_camera(index)
        ok = False
        if cap.isOpened():
            for _ in range(20):
                ok, frame = cap.read()
                if ok:
                    h, w = frame.shape[:2]
                    lines.append(f"{index}번: 사용 가능 ({w}x{h})")
                    break
                time.sleep(0.05)
        cap.release()
        if not ok:
            lines.append(f"{index}번: 없음 / 영상 안 나옴")
    return lines


def open_working_camera(width=640, height=480, fps=30):
    """
    실제로 영상이 나오는 카메라를 찾아 연다. 못 찾으면 None.
    라즈베리파이에는 영상이 안 나오는 내부 장치(/dev/video10 이후 코덱 장치 등)도 있어서
    '열림'만 보지 않고 첫 영상을 실제로 받아 본다.
    """
    for index in camera_candidates():
        cap = open_camera(index)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 밀린 옛 프레임 대신 항상 최신 프레임
        for _ in range(20):  # 켜자마자는 몇 장 비어서 올 수 있다
            ok, _frame = cap.read()
            if ok:
                global ACTIVE_CAMERA_INDEX
                ACTIVE_CAMERA_INDEX = index
                if index != 0:
                    print(f"[출석] 카메라 {index}번 장치로 연결했습니다.")
                return cap
            time.sleep(0.05)
        cap.release()
    return None

# ==========================================
# 0-1. 카메라 화면에 한글 이름을 그리기 위한 폰트
# ==========================================
# cv2.putText는 Hershey 계열 폰트만 지원해서 한글을 못 그린다(글자 수만큼 '?'로 깨져 보인다).
# PIL(Pillow)로 트루타입 폰트를 써서 그려야 한글이 제대로 나온다.
_KOREAN_FONT_CACHE = {}
_KOREAN_FONT_CANDIDATES = [
    "malgun.ttf",                              # 맑은 고딕 (윈도우 기본, UI 폰트와 통일)
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\malgunbd.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",  # macOS 기본 한글 폰트
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/Library/Fonts/AppleGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",          # 리눅스(라즈베리파이) — fonts-nanum
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # fonts-noto-cjk
]


def get_korean_font(size=18):
    """지정한 크기의 한글 지원 폰트를 반환한다(캐시해서 매 프레임 다시 읽지 않는다)."""
    if size in _KOREAN_FONT_CACHE:
        return _KOREAN_FONT_CACHE[size]

    font = None
    for candidate in _KOREAN_FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()  # 최후의 수단 — 이 경우 한글은 못 그리지만 프로그램은 안 죽는다

    _KOREAN_FONT_CACHE[size] = font
    return font

# ==========================================
# 0. 창공시스템(SeatManagerApp, C#) 연동
# ==========================================
# 두 프로그램이 서로 다른 컴퓨터/설치 경로에서 실행돼도 항상 같은 곳을 가리키도록,
# 소스에 특정 컴퓨터 경로를 적어두지 않고 사용자 홈 폴더를 공유 폴더로 쓴다.
# 창공시스템(C#)도 %USERPROFILE%\SeatManagerApp\face-integration 로 정확히 같은 경로를 계산한다.
# %APPDATA%가 아니라 %USERPROFILE%을 쓰는 이유: Microsoft Store/Python Install Manager로 설치한
# 패키지형 파이썬은 %APPDATA%·%LOCALAPPDATA%를 앱별로 가상화해서, 창공시스템이 그 경로에 쓴 파일이
# 이 프로그램에서는 안 보일 수 있다(실제로 테스트 중 발견됨). 홈 폴더 바로 아래는 가상화 대상이 아니다.
def _face_integration_dir():
    home = os.path.expanduser("~")
    path = os.path.join(home, "SeatManagerApp", "face-integration")
    os.makedirs(path, exist_ok=True)
    return path


ROSTER_PATH = os.path.join(_face_integration_dir(), "roster.json")
ATTENDANCE_EXPORT_PATH = os.path.join(_face_integration_dir(), "attendance.json")


def load_roster():
    """창공시스템이 내보낸 학생 명단(roster.json)을 읽는다. 없거나 깨졌으면 빈 목록."""
    try:
        return load_roster_strict()
    except (FileNotFoundError, ValueError):
        return []


def load_roster_strict():
    """
    관리자 화면의 [새로고침]처럼, 실패했을 때 사용자에게 직접 알려줘야 하는 경우 쓴다.
    파일이 없거나(FileNotFoundError) 형식이 잘못되면(ValueError, json.JSONDecodeError는 ValueError의 하위 클래스)
    예외를 그대로 올려서 호출한 쪽에서 메시지를 보여주게 한다.
    """
    with open(ROSTER_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("roster.json의 최상위 형식이 배열([...])이 아닙니다.")
    return data


# 구글폼 응답 시트의 열 제목에 이 글자가 들어 있으면 그 열로 본다 (예: "학번 (8자리)", "휴대폰 번호")
GOOGLE_FORM_COLUMNS = {
    "StudentId": ("학번",),
    "Name": ("이름", "성명"),
    "Department": ("전공", "학과", "소속"),
    "Phone": ("전화", "연락처", "휴대폰"),
    "Grade": ("학년",),
}


def normalize_grade(value):
    """학년 응답에서 숫자만 남긴다 ("2학년" → "2", "3 학년" → "3"). 숫자가 없으면 빈 문자열."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:1]


def roster_from_rows(rows):
    """
    응답 시트의 행 목록(첫 행은 열 제목)을 roster.json과 같은 모양({StudentId, Name, Department, Phone})으로 바꾼다.
    같은 학번이 여러 번 응답했으면 마지막 응답을 쓴다.
    """
    if not rows:
        return []
    header = [str(title) for title in rows[0]]

    def find(keys):
        return next((i for i, title in enumerate(header) if any(k in title for k in keys)), None)

    index = {field: find(keys) for field, keys in GOOGLE_FORM_COLUMNS.items()}
    if index["StudentId"] is None or index["Name"] is None:
        raise ValueError("구글폼 응답에 '학번'과 '이름' 열이 있어야 합니다.\n"
                         f"찾은 열: {', '.join(header)}")
    students = {}
    for row in rows[1:]:
        entry = {field: (str(row[i]).strip() if i is not None and i < len(row) else "")
                 for field, i in index.items()}
        if entry["StudentId"]:
            students[entry["StudentId"]] = entry
    return list(students.values())


def load_google_form_roster(url, timeout=10):
    """구글폼 응답 시트를 [파일 → 공유 → 웹에 게시 → CSV]로 게시한 주소에서 학생 명단을 읽는다."""
    import csv
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        text = resp.read().decode("utf-8-sig")
    return roster_from_rows(list(csv.reader(io.StringIO(text))))


# 구글 Sheets API — 응답 시트를 웹에 게시하지 않고, 서비스 계정에 '보기' 권한으로 공유해서 읽는다.
# 키 파일은 관리자 [설정]에서 고르면 프로그램 폴더로 복사된다 (비밀 키라서 git에는 올리지 않는다).
GOOGLE_KEY_PATH = os.path.join(APP_DIR, "google_service_account.json")
SHEETS_API_URL = "https://sheets.googleapis.com/v4/spreadsheets"
ROSTER_REFRESH_SECONDS = 15  # [학생 등록] 탭이 열려 있는 동안 이 간격으로 명단을 다시 읽는다


class SheetsApiError(Exception):
    """Sheets API가 오류로 답했을 때 — status(HTTP 코드)와 구글이 준 설명을 담는다."""

    def __init__(self, status, detail):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def google_sheet_id(url):
    """구글 시트 편집 주소(…/spreadsheets/d/<ID>/edit…)에서 시트 ID를 꺼낸다. 웹에 게시한 주소(/d/e/…)는 None."""
    import re
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", url)
    return match.group(1) if match else None


def google_service_account_email(path=GOOGLE_KEY_PATH):
    """키 파일의 서비스 계정 이메일 (응답 시트를 이 주소에 공유해야 한다). 키가 없거나 이상하면 None."""
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    if info.get("type") != "service_account" or not info.get("private_key"):
        return None
    return info.get("client_email")


_sheets_session = {}


def _sheets_api_session():
    """서비스 계정으로 인증된 세션. 키 파일이 바뀌지 않았으면 다시 만들지 않는다 (토큰도 재사용)."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    if not os.path.exists(GOOGLE_KEY_PATH):
        raise FileNotFoundError(2, "서비스 계정 키 파일이 없습니다", GOOGLE_KEY_PATH)
    mtime = os.path.getmtime(GOOGLE_KEY_PATH)
    if _sheets_session.get("mtime") != mtime:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_KEY_PATH, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        _sheets_session.update(mtime=mtime, session=AuthorizedSession(creds))
    return _sheets_session["session"]


def load_sheets_api_roster(sheet_id, timeout=10):
    """Sheets API로 응답 시트(첫 번째 시트)의 A~Z열을 읽어 학생 명단으로 바꾼다."""
    resp = _sheets_api_session().get(f"{SHEETS_API_URL}/{sheet_id}/values/A:Z", timeout=timeout)
    if resp.status_code != 200:
        try:
            detail = resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = resp.text[:200]
        raise SheetsApiError(resp.status_code, detail)
    return roster_from_rows(resp.json().get("values", []))


def load_student_roster():
    """
    학생 등록에 쓰는 명단과 그 출처 이름. 설정의 주소가
      - 구글 시트 주소(…/spreadsheets/d/<ID>/…)면 Sheets API로 (서비스 계정 키 필요, 시트 공개 안 해도 됨)
      - 웹에 게시한 CSV 주소면 그 CSV로
      - 비어 있으면 창공시스템 roster.json에서 읽는다.
    읽지 못하면 예외를 그대로 올린다.
    """
    url = str(load_settings().get("google_form_csv_url", "")).strip()
    if url:
        sheet_id = google_sheet_id(url)
        if sheet_id:
            return load_sheets_api_roster(sheet_id), "구글 시트"
        return load_google_form_roster(url), "구글폼"
    return load_roster_strict(), "창공시스템"


def pair_attendance_sessions(rows):
    """
    (user_id, name, log_date, log_type, log_time) 기록을 받아 학번+날짜별로 출근→퇴근 한 쌍씩 묶는다.
    하루에 여러 번 출퇴근할 수 있어서 한 사람이 하루에 여러 건이 나올 수 있다.
    - 퇴근 없이 다시 출근하면 앞의 출근은 퇴근 기록 없음(check_out=None)으로 남는다.
    - 출근 없이 퇴근만 있으면 출근 기록 없음(check_in=None)으로 남긴다.
    반환: {"user_id", "name", "date", "check_in", "check_out"} 목록 (날짜/시간 순)
    """
    sessions = []
    open_session = {}  # (user_id, log_date) -> 아직 퇴근 안 한 세션
    ordered = sorted(
        (r for r in rows if r[0] and r[2]),  # 학번/날짜 없는 이상 데이터는 제외
        key=lambda r: (r[2], r[4] or "", r[0]),
    )
    for user_id, name, log_date, log_type, log_time in ordered:
        key = (user_id, log_date)
        if log_type == "CHECK_IN":
            session = {"user_id": user_id, "name": name, "date": log_date, "check_in": log_time, "check_out": None}
            sessions.append(session)
            open_session[key] = session
        elif log_type == "CHECK_OUT":
            session = open_session.pop(key, None)
            if session is not None:
                session["check_out"] = log_time
            else:
                sessions.append({"user_id": user_id, "name": name, "date": log_date, "check_in": None, "check_out": log_time})
    return sessions


def is_valid_attendance_time(log_time):
    """06:00~24:00 사이에 찍은 기록인지. 00:00~06:00 사이 출근/퇴근은 무효로 본다."""
    return bool(log_time) and log_time >= VALID_ATTENDANCE_START


def format_duration(seconds):
    """초를 'N시간 N분 N초' 형태로 바꾼다."""
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}시간 {minutes:02d}분 {secs:02d}초"


def session_seconds(session):
    """
    출근~퇴근 사이 인정 시간(초).
    출근/퇴근 중 하나라도 없거나, 00:00~06:00 사이에 찍은 기록이거나, 계산이 안 되면 0.
    """
    if not (session["check_in"] and session["check_out"]):
        return 0
    if not (is_valid_attendance_time(session["check_in"]) and is_valid_attendance_time(session["check_out"])):
        return 0
    try:
        t_in = datetime.strptime(f"{session['date']} {session['check_in']}", "%Y-%m-%d %H:%M:%S")
        t_out = datetime.strptime(f"{session['date']} {session['check_out']}", "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0
    return max(0, (t_out - t_in).total_seconds())


def export_attendance_json():
    """출결 기록 전체를 출근→퇴근 한 쌍씩 묶어서 창공시스템이 읽을 수 있는 attendance.json으로 내보낸다."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, log_date, log_type, log_time
            FROM attendance_logs
            ORDER BY log_date ASC, log_time ASC
        """)
        rows = cursor.fetchall()
        conn.close()

        # 하루에 여러 번 출퇴근하면 같은 학번/날짜로 여러 건이 들어간다 (시간순)
        records = [
            {
                "StudentId": s["user_id"],
                "Name": s["name"],
                "Date": s["date"],
                "CheckIn": s["check_in"],
                "CheckOut": s["check_out"],
            }
            for s in pair_attendance_sessions(rows)
        ]

        with open(ATTENDANCE_EXPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception:
        # 내보내기 실패는 치명적이지 않다 — 다음 출결 처리 때 다시 시도한다
        pass


# ==========================================
# 0-2. 출퇴근 사진 저장 / 자동 삭제
# ==========================================
# 사진은 PHOTO_DIR/날짜/시각_학번_IN|OUT.jpg 로 저장하고, DB에는 PHOTO_DIR 기준 상대 경로만 남긴다.
# (폴더를 통째로 옮기거나 구글 드라이브 동기화 폴더로 바꿔도 경로가 깨지지 않게)
def photo_abs_path(rel_path):
    return os.path.join(PHOTO_DIR, *rel_path.split("/"))


def save_attendance_photo(frame, user_id, log_type, when):
    """출퇴근 순간의 카메라 화면을 작게 줄여 JPEG로 저장한다. 저장한 상대 경로, 실패하면 None."""
    if frame is None:
        return None
    try:
        h, w = frame.shape[:2]
        if w > PHOTO_WIDTH:
            frame = cv2.resize(frame, (PHOTO_WIDTH, int(h * PHOTO_WIDTH / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, PHOTO_JPEG_QUALITY])
        if not ok:
            return None
        safe_id = "".join(c if c.isalnum() else "_" for c in str(user_id))
        kind = "IN" if log_type == "CHECK_IN" else "OUT"
        rel_path = f"{when:%Y-%m-%d}/{when:%H%M%S}_{safe_id}_{kind}.jpg"
        abs_path = photo_abs_path(rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        # cv2.imwrite는 윈도우에서 한글이 들어간 경로(예: '새 폴더')에 저장하지 못해서 바이트로 직접 쓴다
        with open(abs_path, "wb") as f:
            f.write(buf.tobytes())
        return rel_path
    except Exception as e:
        # 사진 저장 실패로 출퇴근 기록까지 막지는 않는다
        print(f"[출석] 출퇴근 사진을 저장하지 못했습니다 ({e})")
        return None


def delete_photo_files(rel_paths):
    for rel_path in rel_paths:
        if not rel_path:
            continue
        try:
            os.remove(photo_abs_path(rel_path))
        except OSError:
            pass


def cleanup_old_photos(today=None):
    """
    PHOTO_RETENTION_DAYS(3개월)가 지난 날짜 폴더를 통째로 지우고, DB의 사진 경로도 비운다.
    출퇴근 기록(시간) 자체는 그대로 남는다. 지운 폴더 수를 돌려준다.
    """
    cutoff = (today or date.today()) - timedelta(days=PHOTO_RETENTION_DAYS)
    removed = 0
    if os.path.isdir(PHOTO_DIR):
        for name in os.listdir(PHOTO_DIR):
            try:
                folder_date = datetime.strptime(name, "%Y-%m-%d").date()
            except ValueError:
                continue  # 날짜 폴더가 아닌 것은 건드리지 않는다
            if folder_date < cutoff:
                shutil.rmtree(os.path.join(PHOTO_DIR, name), ignore_errors=True)
                removed += 1
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE attendance_logs SET photo_path = NULL WHERE photo_path IS NOT NULL AND log_date < ?",
            (cutoff.isoformat(),),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass
    return removed

# ==========================================
# 1. DB 초기화 및 관련 함수
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 사용자 테이블 (비밀번호, 권한, 전공, 패널티 필드 추가)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE,
            password TEXT NOT NULL DEFAULT '1234',
            name TEXT NOT NULL,
            major TEXT NOT NULL DEFAULT '소프트웨어전공',
            role TEXT NOT NULL DEFAULT 'student', -- 'admin' 또는 'student'
            embedding BLOB NOT NULL,
            penalty INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 컬럼 존재 확인 및 마이그레이션
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN password TEXT NOT NULL DEFAULT '1234'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN major TEXT NOT NULL DEFAULT '소프트웨어전공'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN penalty INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 관리자 [학생 현황/수정]에서 입력하는 연락처·학년 (등록할 때는 비어 있다)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN grade TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    # 출석 기록 테이블 (구분, 날짜, 시간 필드 보강)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            name TEXT,
            log_type TEXT DEFAULT 'CHECK_IN', -- 'CHECK_IN' (출근) 또는 'CHECK_OUT' (퇴근)
            log_date TEXT,
            log_time TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    # 출퇴근 순간 사진 (PHOTO_DIR 기준 상대 경로, 보관 기간이 지나면 NULL)
    try:
        cursor.execute("ALTER TABLE attendance_logs ADD COLUMN photo_path TEXT")
    except sqlite3.OperationalError:
        pass

    # 예전 기본 관리자 계정(dongseo@mail.com / admin1234)이 남아 있으면 새 계정으로 바꾼다
    cursor.execute("SELECT id FROM users WHERE user_id = '1234'")
    if not cursor.fetchone():
        cursor.execute("""
            UPDATE users SET user_id = '1234', password = '1234'
            WHERE user_id = 'dongseo@mail.com' AND role = 'admin'
        """)

    # 기본 관리자 계정 생성 (1234 / 1234)
    cursor.execute("SELECT id FROM users WHERE user_id = '1234'")
    if not cursor.fetchone():
        dummy_embedding = np.zeros(512, dtype=np.float32).tobytes()
        cursor.execute("""
            INSERT INTO users (user_id, password, name, major, role, embedding, penalty)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('1234', '1234', '관리자', '시스템관리', 'admin', dummy_embedding, 0))

    conn.commit()
    conn.close()


def load_timeouts():
    """settings.json에 저장된 메인 화면 복귀 시간을 timeouts에 읽어 온다. 없거나 범위를 벗어나면 기본값."""
    saved = load_settings()
    for name, default in TIMEOUT_DEFAULTS.items():
        try:
            value = int(saved.get(f"timeout_{name}", default))
        except (TypeError, ValueError):
            value = default
        timeouts[name] = value if TIMEOUT_MIN <= value <= TIMEOUT_MAX else default


def save_timeout(name, seconds):
    timeouts[name] = seconds
    save_setting(f"timeout_{name}", seconds)


def load_users():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, name, major, penalty, embedding FROM users WHERE role = 'student'")
    rows = cursor.fetchall()
    conn.close()

    users = []
    for u_id, name, major, penalty, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        users.append({
            "user_id": u_id, 
            "name": name, 
            "major": major, 
            "penalty": penalty, 
            "embedding": emb
        })
    return users

def summarize_attendance(logs):
    """
    (user_id, name, log_date, log_type, log_time) 기록으로 (출근 일수, 인정 시간 초)를 구한다.
    학생 화면의 주간 현황과 관리자 출결로그 필터가 같은 규칙을 쓰도록 여기 한 곳에서 계산한다.
    """
    # 00:00~06:00 사이 출근/퇴근은 무효라서 아예 빼고 계산한다
    valid_logs = [log for log in logs if is_valid_attendance_time(log[4])]

    # 출근 일수: 인정되는 출근 기록이 있는 날 (퇴근을 안 찍었어도 일수는 인정)
    attended_days = len({log[2] for log in valid_logs if log[3] == "CHECK_IN"})

    # 출근 시간: 출근→퇴근 한 쌍이 모두 있어야 인정 (하루 여러 번이면 모두 더한다)
    total_seconds = int(sum(session_seconds(s) for s in pair_attendance_sessions(valid_logs)))
    return attended_days, total_seconds


def get_attendance_summary_by_student(start_date, end_date):
    """기간(YYYY-MM-DD, 양 끝 포함) 안의 학생별 출근 일수와 인정 시간. 기록이 없는 등록 학생도 0으로 넣는다."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, name FROM users WHERE role = 'student'")
    names = {str(user_id): name for user_id, name in cursor.fetchall()}
    cursor.execute("""
        SELECT user_id, name, log_date, log_type, log_time
        FROM attendance_logs
        WHERE log_date BETWEEN ? AND ?
    """, (start_date, end_date))
    logs = cursor.fetchall()
    conn.close()

    logs_by_user = {}
    for log in logs:
        if not log[0]:
            continue
        user_id = str(log[0])
        logs_by_user.setdefault(user_id, []).append(log)
        names.setdefault(user_id, log[1])  # 삭제된 학생의 기록도 이름과 함께 보여준다

    summary = []
    for user_id, name in names.items():
        days, seconds = summarize_attendance(logs_by_user.get(user_id, []))
        summary.append({"user_id": user_id, "name": name or "", "days": days, "seconds": seconds})
    return summary


def get_weekly_attendance_stats(user_id):
    # 이번주 월요일 00:00:00부터 일요일 23:59:59까지의 출석 계산
    today = datetime.now()
    start_of_week = today - timedelta(days=today.weekday())
    start_date_str = start_of_week.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, name, log_date, log_type, log_time
        FROM attendance_logs
        WHERE user_id = ? AND log_date >= ?
        ORDER BY log_date ASC, log_time ASC
    """, (user_id, start_date_str))
    logs = cursor.fetchall()
    conn.close()

    attended_days, total_seconds = summarize_attendance(logs)

    return {
        "sessions": pair_attendance_sessions(logs),  # 이번주 출근→퇴근 목록 ([출석 조회] 창에 표시)
        "days": attended_days,
        "seconds": total_seconds,
        "missing_days": max(0, WEEKLY_REQUIRED_DAYS - attended_days),
        "missing_seconds": max(0, WEEKLY_REQUIRED_SECONDS - total_seconds),
    }

def get_today_attendance_status(user_id):
    """
    오늘 출석 상태를 구한다.
    - finished_seconds : 오늘 퇴근까지 찍어서 인정된 시간(초)
    - working_since    : 지금 출근 중이면 그 출근 시각("HH:MM:SS"), 아니면 None
    - working_seconds  : 지금 출근 중인 시간(초). 출근 중이 아니면 0
    00:00~06:00 사이 기록은 인정되지 않으므로 계산에서 뺀다.
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, name, log_date, log_type, log_time
        FROM attendance_logs
        WHERE user_id = ? AND log_date = ?
        ORDER BY log_time ASC
    """, (user_id, today_str))
    logs = [log for log in cursor.fetchall() if is_valid_attendance_time(log[4])]
    conn.close()

    sessions = pair_attendance_sessions(logs)
    finished_seconds = int(sum(session_seconds(s) for s in sessions))

    # 지금 출근 중인지는 "오늘의 마지막 기록이 출근인지"로 본다.
    # 짝이 안 맞는 예전 기록(중복 출근 등)이 남아 있어도 퇴근을 찍으면 바로 멈추게 하기 위함이다.
    working_since = None
    if logs and logs[-1][3] == "CHECK_IN":
        working_since = logs[-1][4]

    working_seconds = 0
    if working_since:
        try:
            started = datetime.strptime(f"{today_str} {working_since}", "%Y-%m-%d %H:%M:%S")
            working_seconds = int(max(0, (now - started).total_seconds()))
        except ValueError:
            working_since = None

    return {
        "finished_seconds": finished_seconds,
        "working_since": working_since,
        "working_seconds": working_seconds,
    }

def log_attendance(user_id, name, log_type, frame=None):
    """
    출근/퇴근을 기록한다. frame을 넘기면 그 순간 카메라 화면을 작게 줄여 사진으로 남긴다.
    반환: (성공 여부, 안내 문구) — 성공이면 기록 시각(+ 미인정 안내), 실패면 그 이유.
    """
    now = datetime.now()
    today_date = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M:%S")

    # 하루에 여러 번 출퇴근할 수 있지만, 퇴근을 안 한 상태에서 또 출근을 누르는 것은 막는다
    if log_type == 'CHECK_IN' and is_valid_attendance_time(now_time):
        working_since = get_today_attendance_status(user_id)["working_since"]
        if working_since:
            return False, (f"이미 출근 완료 되었습니다. ({working_since} 출근)\n"
                           "퇴근을 먼저 찍은 뒤에 다시 출근할 수 있습니다.")

    photo_path = save_attendance_photo(frame, user_id, log_type, now)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 하루에 출근/퇴근을 여러 번 할 수 있어서 누를 때마다 새 기록으로 남긴다
    cursor.execute("""
        INSERT INTO attendance_logs (user_id, name, log_type, log_date, log_time, photo_path)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, name, log_type, today_date, now_time, photo_path))
    conn.commit()
    conn.close()
    export_attendance_json()  # 창공시스템이 읽는 출결 파일도 즉시 갱신

    if not is_valid_attendance_time(now_time):
        return True, (f"기록 시각 {now_time}\n"
                      "단, 00:00~06:00 사이 출퇴근은 출석 일수와 시간에 인정되지 않습니다.")
    return True, f"기록 시각 {now_time}"


# ==========================================
# 3. 관리자 기능 — tkinter 관리자 화면과 웹 관리자 화면이 같이 쓴다
# ==========================================
# 출결로그 조회 조건 — (키, 이름). 아무것도 안 고르면 "custom"(기간 내역)
LOG_FILTER_MODES = [
    ("week_days", f"주 {WEEKLY_REQUIRED_DAYS}회 이상 출석"),
    ("week_hours", f"주 {WEEKLY_REQUIRED_SECONDS // 3600}시간 이상 출석"),
    ("month", "월 단위"),
    ("recent7_sum", "최근 7일 합계"),
]
LOG_SUMMARY_MODES = {"recent7_sum", "week_days", "week_hours"}


def check_admin_login(user_id, password):
    """관리자 로그인 확인. "ok" / "wrong"(아이디·비밀번호 틀림) / "not_admin"(학생 계정)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT role FROM users WHERE user_id = ? AND password = ?", (user_id, password)).fetchone()
    conn.close()
    if not row:
        return "wrong"
    return "ok" if row[0] == "admin" else "not_admin"


def log_period(mode, start, end, today=None):
    """
    조회 조건의 실제 조회 기간 (시작일, 종료일).
    최근 7일은 오늘까지 7일, 월 단위는 start가 속한 달, 주 단위는 start가 속한 월~일, 나머지는 start~end.
    """
    today = today or date.today()
    if mode == "recent7_sum":
        return today - timedelta(days=6), today
    if mode == "month":
        last_day = calendar.monthrange(start.year, start.month)[1]
        return start.replace(day=1), start.replace(day=last_day)
    if mode in ("week_days", "week_hours"):
        monday = start - timedelta(days=start.weekday())
        return monday, monday + timedelta(days=6)
    return (start, end) if start <= end else (end, start)


def query_log_rows(start, end, keyword=""):
    """기간 안의 출근/퇴근 기록 (최근 것이 먼저). keyword는 이름 또는 학번 일부."""
    query = """
        SELECT a.user_id, a.name, a.log_type, a.log_date, a.log_time, a.photo_path, u.penalty
        FROM attendance_logs a
        LEFT JOIN users u ON u.user_id = a.user_id
        WHERE a.log_date BETWEEN ? AND ?
    """
    params = [start.isoformat(), end.isoformat()]
    if keyword:
        query += " AND (a.name LIKE ? OR a.user_id LIKE ?)"
        params += [f"%{keyword}%", f"%{keyword}%"]
    query += " ORDER BY a.log_date DESC, a.log_time DESC"
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    keys = ("user_id", "name", "log_type", "log_date", "log_time", "photo_path", "penalty")
    # 00:00~06:00 기록(valid=False)은 일수/시간 계산에서 빠진다
    return [dict(zip(keys, row), valid=is_valid_attendance_time(row[4])) for row in rows]


def query_log_summary(mode, start, end, keyword=""):
    """
    학생별 출근 일수·인정 시간 합계. 주 단위 조건이면 기준(주 3일 / 20시간)을 채운 학생만 남긴다.
    기준은 학생 화면의 주간 현황과 같다.
    """
    summary = get_attendance_summary_by_student(start.isoformat(), end.isoformat())
    if keyword:
        summary = [s for s in summary if keyword in s["name"] or keyword in s["user_id"]]
    if mode == "week_days":
        summary = [s for s in summary if s["days"] >= WEEKLY_REQUIRED_DAYS]
        summary.sort(key=lambda s: (-s["days"], -s["seconds"], s["name"]))
    elif mode == "week_hours":
        summary = [s for s in summary if s["seconds"] >= WEEKLY_REQUIRED_SECONDS]
        summary.sort(key=lambda s: (-s["seconds"], -s["days"], s["name"]))
    else:
        summary.sort(key=lambda s: (-s["seconds"], -s["days"], s["name"]))
    return summary


def list_students():
    """등록된 학생 목록 (학번, 이름, 전공, 패널티, 전화번호, 학년)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id, name, major, penalty, phone, grade FROM users WHERE role = 'student'").fetchall()
    conn.close()
    return rows


def update_student(student_id, name, major, penalty, phone, grade):
    """학생 정보를 고친다. 입력이 잘못됐으면 안내 문구를 담은 ValueError."""
    name, major, phone, grade = (str(v or "").strip() for v in (name, major, phone, grade))
    penalty = str(penalty if penalty is not None else "").strip()
    # 전화번호·학년은 아직 모르는 학생도 있어서 비워둘 수 있다
    if not (name and major and penalty):
        raise ValueError("이름, 전공, 패널티는 꼭 입력하세요.")
    try:
        penalty = int(penalty)
    except ValueError:
        raise ValueError("패널티는 정수형만 입력 가능합니다.") from None
    if grade and not grade.isdigit():
        raise ValueError("학년은 숫자만 입력 가능합니다.")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET name = ?, major = ?, penalty = ?, phone = ?, grade = ? WHERE user_id = ?",
                 (name, major, penalty, phone, grade, student_id))
    conn.commit()
    conn.close()


def delete_students(student_ids):
    """학생들과 그 학생들의 출결 기록·출퇴근 사진을 지운다."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    photo_paths = []
    for student_id in student_ids:
        # SQLite 타입 유연성을 고려하여 문자열 및 정수형 모두 삭제 쿼리 반영
        alt_id = int(student_id) if str(student_id).isdigit() else student_id
        cursor.execute("SELECT photo_path FROM attendance_logs WHERE user_id = ? OR user_id = ?",
                       (str(student_id), alt_id))
        photo_paths += [row[0] for row in cursor.fetchall()]
        # 관리자 계정은 학생 목록에 없으므로 학생만 지운다
        cursor.execute("DELETE FROM users WHERE (user_id = ? OR user_id = ?) AND role = 'student'",
                       (str(student_id), alt_id))
        cursor.execute("DELETE FROM attendance_logs WHERE user_id = ? OR user_id = ?", (str(student_id), alt_id))
    conn.commit()
    conn.close()
    delete_photo_files(photo_paths)


def find_duplicate_face(embedding, enrolled_users, embed_matrix):
    """
    새로 등록하려는 얼굴이 기존 등록자와 같은 사람으로 보이면 그 학생, 아니면 None.
    인식 화면과 같은 코사인 유사도 THRESHOLD를 기준으로 삼는다. embed_matrix는 행마다 정규화된 임베딩.
    """
    if not enrolled_users or embed_matrix is None or len(embed_matrix) == 0:
        return None
    norm = np.linalg.norm(embedding)
    if norm == 0:
        return None
    sims = embed_matrix @ (embedding / norm)
    best_idx = int(np.argmax(sims))
    return enrolled_users[best_idx] if float(sims[best_idx]) >= THRESHOLD else None


def insert_student(student_id, name, major, phone, grade, embedding):
    """얼굴 임베딩과 함께 학생을 등록한다. 이미 있는 학번이면 ValueError."""
    conn = sqlite3.connect(DB_PATH)
    try:
        if conn.execute("SELECT id FROM users WHERE user_id = ?", (student_id,)).fetchone():
            raise ValueError("이미 가입된 학번 ID입니다.")
        conn.execute("""
            INSERT INTO users (user_id, name, major, role, embedding, penalty, phone, grade)
            VALUES (?, ?, ?, 'student', ?, 0, ?, ?)
        """, (student_id, name, major or DEPARTMENT_NAME, np.asarray(embedding, dtype=np.float32).tobytes(),
              phone, normalize_grade(grade)))
        conn.commit()
    finally:
        conn.close()


def fill_blank_contacts_from_roster(roster):
    """
    이미 등록된 학생 중 전화번호·학년이 비어 있으면 명단(구글폼 응답) 값으로 채운다.
    관리자가 직접 입력한 값은 덮어쓰지 않는다. 바뀐 학생 수를 돌려준다.
    """
    by_id = {str(s.get("StudentId", "")).strip(): s for s in roster}
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, phone, grade FROM users WHERE role = 'student' AND (phone = '' OR grade = '')")
    changed = 0
    for user_id, phone, grade in cursor.fetchall():
        student = by_id.get(str(user_id))
        if not student:
            continue
        new_phone = phone or str(student.get("Phone") or "").strip()
        new_grade = grade or normalize_grade(student.get("Grade"))
        if (new_phone, new_grade) != (phone, grade):
            cursor.execute("UPDATE users SET phone = ?, grade = ? WHERE user_id = ?", (new_phone, new_grade, user_id))
            changed += 1
    conn.commit()
    conn.close()
    return changed


def unregistered_roster(roster):
    """명단에서 아직 얼굴 등록이 안 된 학생만 (학생 등록 목록에 보여줄 것)."""
    conn = sqlite3.connect(DB_PATH)
    registered = {str(row[0]) for row in conn.execute("SELECT user_id FROM users WHERE role = 'student'")}
    conn.close()
    result = []
    for student in roster:
        sid = str(student.get("StudentId", "")).strip()
        if sid and sid not in registered:
            result.append(student)
    return result


def roster_error_message(error):
    """명단을 못 읽었을 때 관리자에게 보여줄 안내 문구 (원인별로 고치는 방법까지)."""
    import urllib.error
    if isinstance(error, SheetsApiError):
        if error.status == 403 and "has not been used" in error.detail:
            return ("Google Cloud 프로젝트에서 Google Sheets API가 꺼져 있습니다.\n"
                    "Cloud 콘솔 → API 및 서비스 → 라이브러리에서 'Google Sheets API'를 사용 설정해주세요.\n\n"
                    f"{error.detail}")
        if error.status in (403, 404):
            email = google_service_account_email() or "(키 파일의 서비스 계정 이메일)"
            return ("응답 시트를 읽을 권한이 없거나 시트를 찾지 못했습니다.\n"
                    f"응답 시트의 [공유]에서 아래 이메일을 '뷰어'로 추가해주세요.\n\n{email}\n\n"
                    f"({error.status}: {error.detail})")
        return f"구글 시트를 읽지 못했습니다.\n\n{error.status}: {error.detail}"
    if isinstance(error, FileNotFoundError) and error.filename == GOOGLE_KEY_PATH:
        return ("서비스 계정 키 파일이 없습니다.\n"
                "[설정] 탭의 '서비스 계정 키'에서 Google Cloud에서 받은 JSON 키 파일을 골라주세요.")
    if isinstance(error, FileNotFoundError):
        return ("roster.json 파일을 찾을 수 없습니다.\n"
                "[설정] 탭에 구글폼 응답 주소를 넣거나, 창공시스템을 먼저 실행해서 학생을 등록해주세요.\n\n"
                f"찾아본 위치:\n{ROSTER_PATH}")
    if isinstance(error, json.JSONDecodeError):
        return (f"roster.json 파일의 형식이 올바르지 않습니다 (JSON 오류: {error}).\n"
                "창공시스템 쪽에서 파일이 다 써지는 중에 읽었을 수도 있으니 잠시 후 다시 시도해주세요.")
    if isinstance(error, (urllib.error.URLError, OSError)) or type(error).__module__.startswith(
            ("requests", "google.auth")):
        return ("구글폼 응답을 불러오지 못했습니다.\n"
                f"인터넷 연결과 [설정] 탭의 구글폼 응답 주소를 확인해주세요.\n\n{error}")
    return f"명단을 불러오는 중 오류가 발생했습니다.\n\n{error}"


def attendance_workbook():
    """
    출석 기록 전체를 출근→퇴근 한 쌍씩 한 행으로 묶은 엑셀 통합문서와 행 수. 기록이 없으면 (None, 0).
    DB는 읽기만 한다. openpyxl이 없으면 ImportError.
    """
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT user_id, name, log_date, log_type, log_time
        FROM attendance_logs
        ORDER BY log_date ASC, name ASC, log_time ASC
    """).fetchall()
    conn.close()

    # 출근→퇴근 한 쌍을 한 행으로 묶는다 (attendance.json 내보내기와 같은 방식, 하루 여러 번이면 여러 행)
    records = sorted(
        pair_attendance_sessions(rows),
        key=lambda r: (r["date"], r["name"] or "", r["check_in"] or r["check_out"] or ""),
    )
    if not records:
        return None, 0

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "출석기록"
    headers = ["학번", "이름", "날짜", "출근시간", "퇴근시간", "근무시간(시간)", "비고"]
    ws.append(headers)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for rec in records:
        check_in, check_out = rec["check_in"], rec["check_out"]
        seconds = session_seconds(rec)
        work_hours = round(seconds / 3600.0, 2) if seconds > 0 else ""
        if check_in and check_out:
            note = "정상 출퇴근"
        elif check_in:
            note = "퇴근 기록 없음"
        elif check_out:
            note = "출근 기록 없음"
        else:
            note = "결석"
        ws.append([rec["user_id"], rec["name"], rec["date"], check_in or "-", check_out or "-", work_hours, note])

    for col_idx, width in enumerate([12, 10, 12, 10, 10, 14, 14], start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = width
    return wb, len(records)

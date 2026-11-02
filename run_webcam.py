import cv2
import numpy as np
import threading
import time
from insightface.app import FaceAnalysis

# 1. 모델 경량화 로드 (필수 모델인 det, recognition만 활성화)
print("⏳ 초고속 InsightFace 엔진 초기화 중...")
app = FaceAnalysis(
    name="buffalo_l",
    allowed_modules=['detection', 'recognition'],  # 랜드마크, 성별/나이 연산 스킵
    providers=["CPUExecutionProvider"]
)
app.prepare(ctx_id=0, det_size=(256, 256))  # 256x256으로 경량화
print("✅ 준비 완료!")

def normalize(vec):
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec

enrolled_embedding = None  # 등록 시 정규화해서 저장 -> 매 프레임 norm 재계산 불필요
enrolled_name = "User"
THRESHOLD = 0.50

# 비동기 처리를 위한 전역 변수
latest_frame = None
cached_faces = []
is_running = True
is_processing = False

# 백그라운드 AI 연산 스레드
def ai_worker():
    global latest_frame, cached_faces, is_processing, is_running
    while is_running:
        if latest_frame is not None and not is_processing:
            is_processing = True
            # 최신 프레임 1장 복사 후 분석
            frame_to_process = latest_frame.copy()
            faces = app.get(frame_to_process)
            cached_faces = faces
            is_processing = False
        time.sleep(0.03)  # 초당 약 15~20회 추론

# 백그라운드 스레드 시작
worker_thread = threading.Thread(target=ai_worker, daemon=True)
worker_thread.start()

# 웹캠 설정
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_FPS, 30)

print("\n's': 등록 | 'r': 초기화 | 'q': 종료\n")

while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break

    frame = cv2.flip(frame, 1)
    latest_frame = frame  # 백그라운드 AI 스레드에 최신 프레임 전달

    display_frame = frame.copy()

    # 상단 상태 표시
    if enrolled_embedding is None:
        status_text = "Status: READY (Press 's' to Register)"
        status_color = (0, 165, 255)
    else:
        status_text = f"Status: REGISTERED [{enrolled_name}] (Press 'r' to Reset)"
        status_color = (0, 255, 0)
    
    cv2.putText(display_frame, status_text, (20, 35),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)

    # 캐시된 얼굴 정보 시각화 (화면 끊김 전혀 없음)
    for face in cached_faces:
        bbox = face.bbox.astype(int)
        query_embedding = face.embedding

        if enrolled_embedding is not None:
            sim = float(np.dot(normalize(query_embedding), enrolled_embedding))
            if sim >= THRESHOLD:
                color = (0, 255, 0)
                label = f"AUTH SUCCESS ({sim:.2f})"
            else:
                color = (0, 0, 255)
                label = f"UNKNOWN ({sim:.2f})"
        else:
            color = (255, 255, 0)
            label = "Face Detected"

        cv2.rectangle(display_frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color, 2)
        cv2.putText(display_frame, label, (bbox[0], max(20, bbox[1] - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    cv2.imshow("Ultra Smooth Face Auth", display_frame)

    key = cv2.waitKey(1) & 0xFF
    if key == ord('s'):
        if len(cached_faces) == 1:
            enrolled_embedding = normalize(cached_faces[0].embedding.copy())
            print("🎉 얼굴 등록 완료!")
        elif len(cached_faces) == 0:
            print("⚠️ 얼굴이 감지되지 않았습니다.")
        else:
            print("⚠️ 화면에 1명의 얼굴만 들어오게 해주세요.")
    elif key == ord('r'):
        enrolled_embedding = None
        print("🔄 등록 초기화 완료")
    elif key == ord('q'):
        is_running = False
        break

cap.release()
cv2.destroyAllWindows()
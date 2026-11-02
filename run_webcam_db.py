import cv2
import numpy as np
import threading
import time
import sqlite3
import os
from insightface.app import FaceAnalysis

# ==========================================
# 1. SQLite 데이터베이스 관리 모듈
# ==========================================
DB_PATH = "faces.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE,
            name TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_user_to_db(user_id, name, embedding):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 512차원 numpy float32 배열을 binary blob으로 직렬화
    blob_data = embedding.astype(np.float32).tobytes()
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO users (user_id, name, embedding)
            VALUES (?, ?, ?)
        """, (user_id, name, blob_data))
        conn.commit()
        print(f"💾 [DB 저장 성공] 학번: {user_id}, 이름: {name}")
        return True
    except Exception as e:
        print(f"❌ DB 저장 오류: {e}")
        return False
    finally:
        conn.close()

def load_all_users_from_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, name, embedding FROM users")
    rows = cursor.fetchall()
    conn.close()

    users = []
    for row in rows:
        u_id, name, blob_data = row
        # binary blob을 다시 512차원 numpy 배열로 복원
        emb = np.frombuffer(blob_data, dtype=np.float32)
        users.append({
            "user_id": u_id,
            "name": name,
            "embedding": emb
        })
    return users

# ==========================================
# 2. InsightFace 모델 초기화
# ==========================================
print("⏳ 초고속 InsightFace 엔진 초기화 중...")
app = FaceAnalysis(
    name="buffalo_l",
    allowed_modules=['detection', 'recognition'],
    providers=["CPUExecutionProvider"]
)
app.prepare(ctx_id=0, det_size=(256, 256))
print("✅ InsightFace 로드 완료!")

# DB 초기화 및 기존 등록 사용자 로드
init_db()
enrolled_users = load_all_users_from_db()
print(f"📂 DB에서 총 {len(enrolled_users)}명의 등록 사용자를 로드했습니다.")

def build_embedding_matrix(users):
    # 등록자 임베딩을 미리 정규화해서 쌓아두면 프레임마다 norm을 다시
    # 계산할 필요 없이 행렬곱 한 번으로 전원과의 유사도를 구할 수 있다.
    if not users:
        return np.empty((0, 512), dtype=np.float32)
    mat = np.stack([u["embedding"] for u in users]).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms

embed_matrix = build_embedding_matrix(enrolled_users)

# ==========================================
# 3. 비동기 AI 연산 스레드
# ==========================================
latest_frame = None
cached_faces = []
is_running = True
is_processing = False
THRESHOLD = 0.50

def ai_worker():
    global latest_frame, cached_faces, is_processing, is_running
    while is_running:
        if latest_frame is not None and not is_processing:
            is_processing = True
            frame_to_process = latest_frame.copy()
            faces = app.get(frame_to_process)
            cached_faces = faces
            is_processing = False
        time.sleep(0.03)

worker_thread = threading.Thread(target=ai_worker, daemon=True)
worker_thread.start()

# ==========================================
# 4. 웹캠 실행 루프
# ==========================================
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

print("\n" + "="*50)
print("📌 [조작 방법]")
print(" - 's' 키 : 현재 화면 얼굴을 신규 사용자로 DB에 등록")
print(" - 'q' 키 : 프로그램 종료")
print("="*50 + "\n")

while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break

    frame = cv2.flip(frame, 1)
    latest_frame = frame
    display_frame = frame.copy()

    # 상단 상태바
    cv2.putText(display_frame, f"Registered: {len(enrolled_users)} users (Press 's' to enroll)",
                (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)

    # 실시간 얼굴 인식 & 1:N DB 비교 (전원과의 유사도를 행렬곱 한 번으로 계산)
    if cached_faces:
        face_embs = np.asarray([f.embedding for f in cached_faces], dtype=np.float32)
        face_norms = np.linalg.norm(face_embs, axis=1, keepdims=True)
        face_norms[face_norms == 0] = 1.0
        face_embs_normed = face_embs / face_norms

        has_users = len(embed_matrix) > 0
        if has_users:
            sims = face_embs_normed @ embed_matrix.T  # (num_faces, num_users)
            match_idx = np.argmax(sims, axis=1)
            match_sims = sims[np.arange(len(cached_faces)), match_idx]

        for i, face in enumerate(cached_faces):
            bbox = face.bbox.astype(int)
            best_sim = float(match_sims[i]) if has_users else -1.0
            best_match = enrolled_users[match_idx[i]] if has_users else None

            if best_sim >= THRESHOLD and best_match is not None:
                color = (0, 255, 0)
                label = f"{best_match['name']} ({best_sim:.2f})"
            else:
                color = (0, 0, 255)
                label = f"UNKNOWN ({best_sim:.2f})" if best_sim > 0 else "UNKNOWN"

            cv2.rectangle(display_frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color, 2)
            cv2.putText(display_frame, label, (bbox[0], max(20, bbox[1] - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    cv2.imshow("Creative Space Face Auth with DB", display_frame)

    key = cv2.waitKey(1) & 0xFF

    # 1) 's' 키: 얼굴 등록
    if key == ord('s'):
        if len(cached_faces) == 1:
            target_embedding = cached_faces[0].embedding.copy()
            
            # 터미널에서 사용자 정보 입력
            print("\n--- 신규 사용자 등록 ---")
            input_id = input("학번(ID)을 입력하세요: ").strip()
            input_name = input("이름을 입력하세요: ").strip()

            if input_id and input_name:
                if save_user_to_db(input_id, input_name, target_embedding):
                    # 메모리 캐시 및 유사도 비교용 행렬 갱신
                    enrolled_users = load_all_users_from_db()
                    embed_matrix = build_embedding_matrix(enrolled_users)
                    print(f"🎉 {input_name}님 등록이 완료되었습니다!\n")
            else:
                print("⚠️ 학번과 이름을 올바르게 입력해주세요.\n")
        elif len(cached_faces) == 0:
            print("⚠️ 화면에 얼굴이 감지되지 않았습니다.")
        else:
            print("⚠️ 화면에 1명의 얼굴만 들어오게 해주세요.")

    # 2) 'q' 키: 종료
    elif key == ord('q'):
        is_running = False
        break

cap.release()
cv2.destroyAllWindows()
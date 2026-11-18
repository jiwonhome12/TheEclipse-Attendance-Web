"""
진단 로그 — 영상이 끊기거나 화면이 깜빡이거나 인식이 안 될 때 원인을 찾으려고 남긴다.

    logs/web.log  (5MB × 5개까지 돌려 쓴다)

남기는 것
  · 이벤트: 카메라 열기/끊김/다시 연결, 화면 모드 바뀜(누가 바꿨는지), 웹소켓 접속/끊김,
            영상 스트림 시작/끝, 느린 요청, 브라우저에서 보낸 오류
  · 10초마다 요약(카메라가 켜져 있거나 영상을 보는 사람이 있을 때만):
      카메라 초당 프레임 · 읽기 실패 · 인식 속도(ms) · 영상 인코딩(ms) · 보낸 영상 초당 장수 · 서버 멈춤(루프 지연)
관리자 화면 [설정] → 진단 로그에서 지금 수치를 보고 로그 파일을 내려받을 수 있다.
"""
import collections
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler

import core

LOG_DIR = os.path.join(core.APP_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "web.log")
SUMMARY_SECONDS = 10

os.makedirs(LOG_DIR, exist_ok=True)
log = logging.getLogger("eclipse")
if not log.handlers:
    log.setLevel(logging.INFO)
    _file = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    _file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_file)
    log.propagate = False


class Meter:
    """구간마다 횟수·시간(ms)을 모아 두었다가 요약할 때 꺼내고 비운다. 여러 스레드에서 써도 된다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = collections.Counter()
        self._times = collections.defaultdict(list)

    def count(self, name, n=1):
        with self._lock:
            self._counts[name] += n

    def time_ms(self, name, ms):
        with self._lock:
            self._times[name].append(ms)

    def take(self):
        with self._lock:
            counts, times = self._counts, self._times
            self._counts, self._times = collections.Counter(), collections.defaultdict(list)
        return counts, times


meter = Meter()
latest = {}            # 마지막 요약 (관리자 화면에 보여준다)
gauges = {"stream_clients": 0, "ws_clients": 0}
_gauge_lock = threading.Lock()


def gauge_add(name, n):
    with _gauge_lock:
        gauges[name] = gauges.get(name, 0) + n
        return gauges[name]


def event(message, *args, level=logging.INFO):
    log.log(level, message, *args)


def _stats(values):
    if not values:
        return None
    values = sorted(values)
    return {"avg": round(sum(values) / len(values), 1), "max": round(values[-1], 1),
            "p90": round(values[int(len(values) * 0.9) - 1 if len(values) > 1 else 0], 1), "n": len(values)}


def summarize(engine, seconds):
    """지난 구간의 수치를 정리한다. 아무 일도 없던 구간이면 None."""
    counts, times = meter.take()
    st = {"camera_on": engine.camera_on, "camera_error": engine.camera_error, "mode": engine.mode,
          "ai": "failed" if engine.ai_failed else ("ready" if engine.ai_ready else "loading"),
          "stream_clients": gauges["stream_clients"], "ws_clients": gauges["ws_clients"]}
    busy = engine.camera_on or gauges["stream_clients"] or counts
    if not busy:
        return None
    per_s = lambda name: round(counts[name] / seconds, 1)  # noqa: E731
    st.update({
        "camera_fps": per_s("frame"), "camera_read_fail": counts["read_fail"],
        "ai_fps": per_s("ai"), "ai_ms": _stats(times["ai_ms"]), "faces_seen": counts["faces"],
        "jpeg_ms": _stats(times["jpeg_ms"]), "stream_fps": per_s("stream_sent"),
        "stream_kb": round(counts["stream_bytes"] / 1024 / seconds, 1),
        "loop_lag_ms": _stats(times["loop_lag_ms"]),
        "ws_sent": counts["ws_sent"],
    })
    return st


def format_summary(st):
    def ms(stats):
        return "-" if not stats else f"평균 {stats['avg']}ms/최대 {stats['max']}ms"
    return (f"[요약] 모드 {st['mode']} · 카메라 {'켜짐' if st['camera_on'] else '꺼짐'}"
            f"{'(오류)' if st['camera_error'] else ''} {st['camera_fps']}fps 실패 {st['camera_read_fail']}회"
            f" | 인식 {st['ai']} {st['ai_fps']}fps {ms(st['ai_ms'])} 얼굴 {st['faces_seen']}회"
            f" | 영상 인코딩 {ms(st['jpeg_ms'])} 보냄 {st['stream_fps']}fps {st['stream_kb']}KB/s"
            f" 시청 {st['stream_clients']}명 | 서버 멈춤 {ms(st['loop_lag_ms'])} | 웹소켓 {st['ws_clients']}개")


def start_summary_thread(engine):
    def loop():
        last = time.time()
        while engine.is_running:
            time.sleep(SUMMARY_SECONDS)
            now = time.time()
            try:
                st = summarize(engine, now - last)
                last = now
                if st:
                    latest.clear()
                    latest.update(st, at=time.strftime("%H:%M:%S"))
                    log.info(format_summary(st))
            except Exception as e:  # 진단 때문에 서버가 멈추면 안 된다
                log.warning("요약 실패: %s", e)

    threading.Thread(target=loop, name="diag-summary", daemon=True).start()


def tail(lines=300):
    """로그 파일 끝부분 (관리자 화면에 보여준다)."""
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])

// 서버(/ws)가 보내주는 상태대로 화면을 그린다. 화면 전환·기록은 전부 서버가 정하고,
// 여기서는 버튼을 누르면 서버에 알리기만 한다.
const $ = (id) => document.getElementById(id);
const VIEWS = { idle: "view-idle", attendance: "view-verify", lookup: "view-verify",
                result: "view-result", stats: "view-stats", test: "view-test", register: "view-busy" };
const ACTION_NAME = { CHECK_IN: "출근", CHECK_OUT: "퇴근" };

let state = null;
let clockSkew = 0;  // 서버 시각 - 브라우저 시각 (남은 시간 계산용, 다른 기기에서 열어도 맞게)

// ---------- 서버와 주고받기 ----------
async function post(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.ok) render(await resp.json());
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => render(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 1000);  // 서버를 다시 켜면 저절로 다시 붙는다
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "home") post("/api/idle");
  else if (action === "lookup") post("/api/session", { mode: "lookup" });
  else if (action === "test") post("/api/session", { mode: "test" });
  else post("/api/session", { mode: "attendance", action });
});

// ---------- 그리기 ----------
function render(st) {
  if (state && st.version < state.version) return;  // 늦게 도착한 옛 상태
  const modeChanged = !state || state.mode !== st.mode;
  state = st;
  clockSkew = st.now - Date.now() / 1000;

  for (const [mode, id] of Object.entries(VIEWS)) $(id).hidden = VIEWS[st.mode] !== id;
  renderAiStatus(st.ai);

  if (st.mode === "attendance" || st.mode === "lookup") renderVerify(st);
  else if (st.mode === "result") renderResult(st.payload);
  else if (st.mode === "stats") renderStats(st.payload);
  else if (st.mode === "test") renderTest(st, modeChanged);

  // 카메라 테스트가 아니면 영상 연결을 끊는다 (카메라가 꺼져도 연결이 남아 있지 않게)
  if (st.mode !== "test") $("video").removeAttribute("src");
  tick();
}

function renderAiStatus(ai) {
  const el = $("ai-status");
  el.hidden = ai === "ready";
  el.classList.toggle("failed", ai === "failed");
  el.textContent = ai === "failed"
    ? "얼굴 인식을 시작하지 못했습니다.\n인터넷 연결을 확인하고 서버를 다시 실행하세요. (logs/app.log 참고)"
    : "얼굴 인식을 준비하고 있습니다...\n처음 실행하면 모델(약 300MB)을 받느라 몇 분 걸립니다.";
}

// 카드 상태(state) → 동그라미 안에 보여줄 것
function setVisual(el, cardState) {
  el.className = "visual";
  el.innerHTML = "";
  if (cardState === "loading" || cardState === "search" || cardState === "turn") el.classList.add("spin");
  else if (cardState === "blink") el.innerHTML = '<img src="/assets/blink_hint.gif" alt="">';
  else if (cardState === "ok") { el.classList.add("ok"); el.textContent = "✓"; }
  else if (cardState === "warn" || cardState === "unknown") { el.classList.add("warn"); el.textContent = "!"; }
  else if (cardState === "error") { el.classList.add("error"); el.textContent = "✕"; }
}

function renderVerify(st) {
  $("verify-kind").textContent = st.mode === "lookup" ? "출석 조회" : `${ACTION_NAME[st.action]} 확인`;
  const card = cameraCard(st);
  // 고개 돌리기 중에는 동그라미 대신 따라 할 수 있는 안내(얼굴 그림·단계·방향 막대)를 보여준다
  const turning = Boolean(card.challenge);
  $("verify-visual").hidden = turning;
  $("verify-turn").hidden = !turning;
  if (turning) renderTurnGuide($("verify-turn"), card.challenge);
  else setVisual($("verify-visual"), card.state);
  $("verify-title").textContent = card.title || "";
  $("verify-detail").textContent = card.detail || "카메라를 바라봐 주세요";
}

// 카메라를 못 열면 인식 안내 대신 카메라 오류를 보여준다
function cameraCard(st) {
  if (st.camera === "error") return { state: "error", title: "카메라를 열 수 없습니다", detail: "관리자에게 알려주세요" };
  return st.card || {};
}

function renderResult(p) {
  const name = ACTION_NAME[p.log_type];
  setVisual($("result-visual"), p.success ? "ok" : "error");
  $("result-title").textContent = p.success
    ? `${p.student.name} 학생 ${name} 되었습니다`
    : `${name}하지 못했습니다`;
  $("result-detail").textContent = p.detail;
  $("result-today").textContent = todayText(p.today);
}

function renderStats(p) {
  const w = p.weekly;
  $("stats-title").textContent = `${p.student.name} 학생 이번 주 출석 현황`;
  $("stats-time").textContent = formatDuration(w.seconds);
  $("stats-days").textContent = `${w.days}일`;
  setBar($("stats-time-bar"), w.seconds / p.required_seconds);
  setBar($("stats-days-bar"), w.days / p.required_days);
  $("stats-time-need").textContent = w.missing_seconds > 0
    ? `기준까지 ${formatDuration(w.missing_seconds)} 남음` : "이번 주 시간 기준을 채웠습니다";
  $("stats-days-need").textContent = w.missing_days > 0
    ? `기준까지 ${w.missing_days}일 남음` : "이번 주 일수 기준을 채웠습니다";
  $("stats-today").textContent = todayText(p.today);
  $("stats-sessions").innerHTML = w.sessions.map((s) =>
    `<tr><td>${s.date}</td><td>${s.check_in || "-"}</td><td>${s.check_out || "-"}</td></tr>`).join("")
    || '<tr><td colspan="3" class="muted">이번 주 기록이 없습니다</td></tr>';
}

function renderTest(st, modeChanged) {
  const card = cameraCard(st);
  $("test-title").textContent = card.title || "";
  $("test-detail").textContent = card.detail || "";
  $("video-msg").textContent = st.camera === "error" ? "카메라를 열 수 없습니다" : "";
  // 새로 들어올 때마다 영상 연결을 새로 연다 (주소가 같으면 브라우저가 다시 받지 않아서 시각을 붙인다)
  if (modeChanged || !$("video").getAttribute("src")) $("video").src = `/video.mjpg?t=${Date.now()}`;
}

function setBar(el, fraction) {
  const f = Math.max(0, Math.min(1, fraction || 0));
  el.style.width = `${f * 100}%`;
  el.classList.toggle("done", f >= 1);
}

function todayText(today) {
  if (!today) return "";
  const total = today.finished_seconds + today.working_seconds;
  return today.working_since
    ? `오늘 ${formatDuration(total)} · ${today.working_since}부터 출근 중`
    : `오늘 출석 시간 ${formatDuration(total)}`;
}

function formatDuration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return `${h}시간 ${String(m).padStart(2, "0")}분`;
}

// ---------- 시계 / 남은 시간 ----------
function tick() {
  // tkinter 앱과 같은 "2026-10-26  15:58:51" 형식
  const now = new Date();
  const p = (n) => String(n).padStart(2, "0");
  $("clock").textContent = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())} `
    + `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
  if (!state) return;
  const id = { attendance: "verify-remaining", lookup: "verify-remaining", result: "result-remaining",
               stats: "stats-remaining", test: "test-remaining" }[state.mode];
  if (id && state.deadline) {
    const left = Math.max(0, Math.ceil(state.deadline - (Date.now() / 1000 + clockSkew)));
    $(id).textContent = `${left}초 뒤 처음 화면으로`;
  }
}

setInterval(tick, 250);
fetch("/api/state").then((r) => r.json()).then((st) => {
  render(st);
  connect();
});

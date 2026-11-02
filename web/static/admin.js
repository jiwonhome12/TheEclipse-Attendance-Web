// 관리자 화면 — tkinter 관리자 화면(학생 현황/수정 · 학생 등록 · 전체 출결로그 · 설정)과 같은 기능.
// 데이터는 전부 /api/admin/* 로 서버에 묻고, 등록용 카메라 상태는 출석 화면과 같은 /ws로 받는다.
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let settings = null;   // /api/admin/settings
let currentTab = null;

// ---------- 서버와 주고받기 ----------
async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(`/api/admin${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401 && path !== "/login") {
    showLogin();
    throw new Error("관리자 로그인이 필요합니다.");
  }
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
  if (!resp.ok) throw new Error(data?.detail || `요청 실패 (${resp.status})`);
  return data;
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
}

// 버튼 처리 중 오류는 알림으로 보여준다
const guarded = (fn) => async (...args) => {
  try { await fn(...args); } catch (e) { toast(e.message, true); }
};

// ---------- 로그인 ----------
function showLogin() {
  stopRegisterCamera();
  $("login").hidden = false;
  $("tabs").hidden = true;
  $("btn-logout").hidden = true;
  $("admin-who").textContent = "관리자 로그인이 필요합니다";
  for (const page of document.querySelectorAll(".tab-page")) page.hidden = true;
  $("login-id").focus();
}

async function showAdmin(userId) {
  $("login").hidden = true;
  $("tabs").hidden = false;
  $("btn-logout").hidden = false;
  $("admin-who").textContent = `${userId} 로그인 · 나갈 때는 [로그아웃]`;
  settings = await api("/settings");
  buildLogModes();
  selectTab("students");
}

$("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    const res = await api("/login", { method: "POST", body: { user_id: $("login-id").value, password: $("login-pw").value } });
    $("login-pw").value = "";
    await showAdmin(res.user_id);
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});

$("btn-logout").addEventListener("click", guarded(async () => {
  await api("/logout", { method: "POST" });
  showLogin();
}));

// ---------- 탭 ----------
const TAB_LOADERS = {
  students: loadStudents,
  register: openRegister,
  logs: loadLogs,
  settings: loadSettings,
};

function selectTab(name) {
  if (currentTab === "register" && name !== "register") stopRegisterCamera();
  currentTab = name;
  for (const btn of document.querySelectorAll(".tab")) btn.classList.toggle("active", btn.dataset.tab === name);
  for (const page of document.querySelectorAll(".tab-page")) page.hidden = page.id !== `tab-${name}`;
  guarded(TAB_LOADERS[name])();
}

for (const btn of document.querySelectorAll(".tab")) btn.addEventListener("click", () => selectTab(btn.dataset.tab));

// ---------- 학생 현황/수정 ----------
let students = [];
let selectedStudent = null;

async function loadStudents() {
  students = await api("/students");
  selectedStudent = null;
  $("edit-form").reset();
  $("check-all").checked = false;
  $("student-count").textContent = `등록된 학생 ${students.length}명 · 체크한 학생은 [선택 삭제]로 지웁니다`;
  $("student-rows").innerHTML = students.map((s, i) => `
    <tr data-index="${i}">
      <td class="check"><input type="checkbox" data-id="${esc(s.user_id)}"></td>
      <td>${esc(s.user_id)}</td><td>${esc(s.name)}</td><td>${esc(s.major)}</td>
      <td>${esc(s.penalty)}</td><td>${esc(s.phone)}</td><td>${esc(s.grade)}</td>
    </tr>`).join("") || '<tr><td colspan="7" class="muted">등록된 학생이 없습니다</td></tr>';
}

$("student-rows").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-index]");
  if (!row || e.target.matches("input[type=checkbox]")) return;
  for (const r of $("student-rows").children) r.classList.toggle("selected", r === row);
  selectedStudent = students[row.dataset.index];
  const form = $("edit-form");
  for (const key of ["user_id", "name", "major", "phone", "grade", "penalty"]) form.elements[key].value = selectedStudent[key] ?? "";
});

$("check-all").addEventListener("change", (e) => {
  for (const box of $("student-rows").querySelectorAll("input[type=checkbox]")) box.checked = e.target.checked;
});

$("edit-form").addEventListener("submit", guarded(async (e) => {
  e.preventDefault();
  if (!selectedStudent) throw new Error("수정할 대상을 리스트에서 선택하세요.");
  const f = e.target.elements;
  await api(`/students/${encodeURIComponent(selectedStudent.user_id)}`, {
    method: "PUT",
    body: { name: f.name.value, major: f.major.value, penalty: f.penalty.value, phone: f.phone.value, grade: f.grade.value },
  });
  toast("학생 정보 변경이 완료되었습니다.");
  await loadStudents();
}));

$("btn-delete").addEventListener("click", guarded(async () => {
  let ids = [...$("student-rows").querySelectorAll("input[type=checkbox]:checked")].map((b) => b.dataset.id);
  if (!ids.length && selectedStudent) ids = [selectedStudent.user_id];
  if (!ids.length) throw new Error("삭제할 학생을 체크하거나 리스트에서 선택하세요.");
  const who = ids.length === 1 ? `[${students.find((s) => s.user_id === ids[0])?.name}] 학생` : `체크한 학생 ${ids.length}명`;
  if (!confirm(`${who} 정보를 완전히 삭제하시겠습니까?\n출결 기록과 출퇴근 사진도 함께 삭제됩니다.`)) return;
  await api("/students/delete", { method: "POST", body: { ids } });
  toast(`학생 ${ids.length}명의 정보가 삭제되었습니다.`);
  await loadStudents();
}));

$("btn-export").addEventListener("click", guarded(async (e) => {
  // 기록이 없으면 빈 파일 대신 안내를 보여준다
  e.preventDefault();
  const resp = await fetch("/api/admin/export.xlsx");
  if (!resp.ok) throw new Error((await resp.json()).detail);
  const name = decodeURIComponent((resp.headers.get("content-disposition") || "").split("''")[1] || "출석기록.xlsx");
  const url = URL.createObjectURL(await resp.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  URL.revokeObjectURL(url);
}));

// ---------- 학생 등록 ----------
let registerTimer = null;  // [학생 등록] 탭이 열려 있는 동안 카메라를 켜 두는 타이머
let roster = [];
let rosterTimer = null;
let ws = null;

async function openRegister() {
  const keep = () => api("/register/camera", { method: "POST" }).catch((e) => toast(e.message, true));
  await keep();
  clearInterval(registerTimer);
  registerTimer = setInterval(keep, 20000);  // 탭이 열려 있는 동안 카메라를 켜 둔다 (서버는 60초 소식이 없으면 끈다)
  $("register-video").src = `/video.mjpg?t=${Date.now()}`;
  connectState();
  await loadRoster(false);
}

function stopRegisterCamera() {
  if (registerTimer === null) return;  // [학생 등록] 탭을 연 적이 없다
  clearInterval(registerTimer);
  clearTimeout(rosterTimer);
  registerTimer = null;
  $("register-video").removeAttribute("src");
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  navigator.sendBeacon?.("/api/admin/register/camera/stop");
}
window.addEventListener("pagehide", () => { if (currentTab === "register") stopRegisterCamera(); });

function connectState() {
  if (ws) return;
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => renderRegisterState(JSON.parse(e.data));
  ws.onclose = () => { ws = null; if (currentTab === "register") setTimeout(connectState, 1000); };
}

function renderRegisterState(st) {
  let card = st.card || {};
  if (st.mode !== "register") card = { state: "warn", title: "카메라가 꺼져 있습니다", detail: "잠시 뒤 다시 켜집니다" };
  else if (st.camera === "error") card = { state: "error", title: "카메라를 열 수 없습니다", detail: "다른 프로그램이 카메라를 쓰고 있는지 확인하세요" };
  else if (st.ai !== "ready") card = { state: "loading", title: "얼굴 인식 준비 중..", detail: "" };
  setVisual($("register-visual"), card.state);
  $("register-title").textContent = card.title || "";
  $("register-detail").textContent = card.detail || "";
}

function setVisual(el, state) {
  el.className = "visual small";
  el.innerHTML = "";
  if (state === "loading" || state === "search") el.classList.add("spin");
  else if (state === "blink") el.innerHTML = '<img src="/assets/blink_hint.gif" alt="">';
  else if (state === "ok") { el.classList.add("ok"); el.textContent = "✓"; }
  else if (state === "warn" || state === "unknown") { el.classList.add("warn"); el.textContent = "!"; }
  else if (state === "error") { el.classList.add("error"); el.textContent = "✕"; }
}

async function loadRoster(showMessage) {
  clearTimeout(rosterTimer);
  const res = await api("/roster");
  if (res.error) {
    $("roster-status").textContent = `● 명단을 불러오지 못했습니다 (${res.checked_at}) — 새로고침을 눌러 원인 확인`;
    $("roster-status").style.color = "var(--err)";
    if (!roster.length) $("roster-select").innerHTML = '<option value="">명단 없음 — 아래에 직접 입력하세요</option>';
    if (showMessage) toast(res.error, true);
  } else {
    roster = res.unregistered;
    const select = $("roster-select");
    select.innerHTML = '<option value="">학생을 고르세요</option>' + roster.map((s, i) =>
      `<option value="${i}" data-id="${esc(s.user_id)}">${esc(s.user_id)} - ${esc(s.name)}</option>`).join("");
    // 자동 새로고침 중에도 고르던 학생은 그대로 둔다 (방금 등록해서 목록에서 빠졌으면 비운다)
    const keep = [...select.options].find((o) => o.dataset.id && o.dataset.id === select.dataset.picked);
    select.value = keep ? keep.value : "";
    const live = res.live_seconds ? ` · ${res.live_seconds}초마다 자동 갱신` : "";
    $("roster-status").textContent = `● ${res.source} 명단 ${res.total}명 · 미등록 ${roster.length}명 · ${res.checked_at} 업데이트${live}`;
    $("roster-status").style.color = "var(--ok)";
    if (showMessage) toast(`${res.source} 명단 ${res.total}건을 다시 불러왔습니다.\n얼굴인식 결과에 표시되는 이름에도 바로 반영됩니다.`);
    if (res.live_seconds && currentTab === "register") rosterTimer = setTimeout(() => loadRoster(false).catch(() => {}), res.live_seconds * 1000);
  }
}

$("btn-roster").addEventListener("click", guarded(() => loadRoster(true)));

$("roster-select").addEventListener("change", (e) => {
  const s = roster[e.target.value];
  e.target.dataset.picked = s ? s.user_id : "";
  if (!s) return;
  const f = $("register-form").elements;
  for (const key of ["user_id", "name", "major", "phone", "grade"]) f[key].value = s[key] || "";
  if (!f.major.value) f.major.value = "소프트웨어전공";
});

$("register-form").addEventListener("submit", guarded(async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const res = await api("/register", {
    method: "POST",
    body: { user_id: f.user_id.value, name: f.name.value, major: f.major.value, phone: f.phone.value, grade: f.grade.value },
  });
  toast(`[${res.name}] 학생이 성공적으로 등록되었습니다.`);
  e.target.reset();
  $("roster-select").dataset.picked = "";
  await loadRoster(false);  // 방금 등록한 학생은 명단 목록에서 빠지도록
}));

// ---------- 전체 출결로그 ----------
let logMode = "custom";
let logRows = [];
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

function buildLogModes() {
  $("log-modes").innerHTML = settings.log_modes.map(([key, label]) =>
    `<button type="button" class="chip" data-mode="${key}">${esc(label)}</button>`).join("");
  if (!$("log-end").value) {
    $("log-end").value = today();
    $("log-start").value = new Date(Date.now() - 6 * 86400000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }
  $("log-photo-note").textContent = `사진이 있는 기록(📷)을 누르면 출퇴근 사진을 봅니다 · 사진은 ${settings.photo_retention_days}일 뒤 자동 삭제`;
}

$("log-modes").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  logMode = logMode === chip.dataset.mode ? "custom" : chip.dataset.mode;  // 다시 누르면 해제
  for (const c of $("log-modes").children) c.classList.toggle("on", c.dataset.mode === logMode);
  guarded(loadLogs)();
});

function syncLogDateInputs() {
  // 최근 7일 합계는 날짜가 필요 없고, 월/주 단위는 기준일 1개, 기간 내역은 시작~종료 2개
  const fixed = logMode === "recent7_sum", single = !fixed && logMode !== "custom";
  $("log-date-label").textContent = single ? "기준일" : "기간";
  $("log-start").hidden = fixed;
  $("log-tilde").hidden = fixed || single;
  $("log-end").hidden = fixed || single;
  $("log-fixed").hidden = !fixed;
}

const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];
const dateKo = (iso) => `${iso} (${WEEKDAYS[new Date(`${iso}T00:00:00`).getDay()]})`;
const duration = (sec) => {
  sec = Math.max(0, Math.floor(sec));
  return `${Math.floor(sec / 3600)}시간 ${String(Math.floor((sec % 3600) / 60)).padStart(2, "0")}분 ${String(sec % 60).padStart(2, "0")}초`;
};

async function loadLogs() {
  syncLogDateInputs();
  const params = new URLSearchParams({ mode: logMode, keyword: $("log-keyword").value });
  if ($("log-start").value) params.set("start", $("log-start").value);
  if ($("log-end").value) params.set("end", $("log-end").value);
  const res = await api(`/logs?${params}`);
  logRows = res.rows;

  if (res.kind === "rows") {
    // 거꾸로 골랐으면 서버가 순서를 바로잡은 기간으로 맞춰 보여준다
    if (logMode === "custom") { $("log-start").value = res.start; $("log-end").value = res.end; }
    $("log-head").innerHTML = "<tr><th>학번</th><th>이름</th><th>구분</th><th>날짜</th><th>시간</th><th>패널티</th></tr>";
    $("log-rows").innerHTML = logRows.map((r, i) => {
      const type = (r.log_type === "CHECK_IN" ? "출근" : "퇴근") + (r.valid ? "" : " (미인정)");
      return `<tr data-index="${i}" class="${r.valid ? "" : "invalid"}">
        <td>${esc(r.user_id)}</td><td>${esc(r.name)}</td><td class="${r.photo_path ? "has-photo" : ""}">${type}</td>
        <td>${dateKo(r.log_date)}</td><td>${esc(r.log_time)}</td><td>${esc(r.penalty)}</td></tr>`;
    }).join("") || '<tr><td colspan="6" class="muted">기록이 없습니다</td></tr>';
  } else {
    $("log-head").innerHTML = "<tr><th>학번</th><th>이름</th><th>출근 일수</th><th>출석 시간 합계</th></tr>";
    $("log-rows").innerHTML = logRows.map((s) =>
      `<tr><td>${esc(s.user_id)}</td><td>${esc(s.name)}</td><td>${s.days}일</td><td>${duration(s.seconds)}</td></tr>`).join("")
      || '<tr><td colspan="4" class="muted">조건을 충족한 학생이 없습니다</td></tr>';
  }

  // 기준일로 정해지는 기간(주/월/최근 7일)은 실제 조회 기간을 결과 위에 같이 보여준다
  let period = "";
  if (logMode === "month") period = `${res.start.slice(0, 4)}년 ${Number(res.start.slice(5, 7))}월 전체\n`;
  else if (logMode !== "custom") period = `${dateKo(res.start)} ~ ${dateKo(res.end)}\n`;
  $("log-result").textContent = period + res.result;
}

$("log-form").addEventListener("submit", (e) => { e.preventDefault(); guarded(loadLogs)(); });
for (const id of ["log-start", "log-end"]) $(id).addEventListener("change", () => guarded(loadLogs)());

$("log-rows").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-index]");
  const r = row && logRows[row.dataset.index];
  if (!r || !("photo_path" in r)) return;
  if (!r.photo_path) {
    toast(`이 기록에는 사진이 없습니다.\n(사진 기능 도입 전 기록이거나, ${settings.photo_retention_days}일 보관 기간이 지나 삭제되었습니다)`);
    return;
  }
  $("photo-img").src = `/api/admin/photo?path=${encodeURIComponent(r.photo_path)}`;
  $("photo-img").onerror = () => { $("photo-dialog").close(); toast("사진 파일을 찾을 수 없습니다. 이미 삭제되었을 수 있습니다.", true); };
  $("photo-caption").textContent = `${r.name} (${r.user_id}) · ${dateKo(r.log_date)} ${r.log_time} ${r.log_type === "CHECK_IN" ? "출근" : "퇴근"}`;
  $("photo-dialog").showModal();
});

// ---------- 설정 ----------
const TIMEOUT_ROWS = [
  ["verify", "얼굴 확인 화면", "출근 · 퇴근 · 출석 조회를 누른 뒤 얼굴을 기다리는 시간"],
  ["result", "출퇴근 결과 화면", "\"OOO 학생 출근 되었습니다\" 화면이 떠 있는 시간"],
  ["stats", "출석 현황 화면", "이번 주 출석 현황이 떠 있는 시간"],
];

async function loadSettings() {
  settings = await api("/settings");
  const { min, max, step } = settings.timeout_range;
  $("timeout-desc").textContent = `아무 조작이 없으면 이 시간 뒤에 메인 화면으로 돌아갑니다 (${step}초 단위, ${min}~${max}초).`;
  $("timeouts").innerHTML = TIMEOUT_ROWS.map(([name, title, desc]) => {
    const v = settings.timeouts[name];
    return `<div class="setting-row">
      <div><strong>${title}</strong><small>${desc}</small></div>
      <div class="stepper">
        <button data-timeout="${name}" data-delta="${-step}" ${v <= min ? "disabled" : ""}>−</button>
        <span>${v}초</span>
        <button data-timeout="${name}" data-delta="${step}" ${v >= max ? "disabled" : ""}>+</button>
      </div></div>`;
  }).join("");

  const cam = settings.camera;
  $("camera-select").innerHTML = Array.from({ length: cam.choices }, (_, i) =>
    `<option value="${i}" ${i === cam.preferred ? "selected" : ""}>${i}번</option>`).join("");
  $("camera-info").textContent = cam.active == null ? "카메라가 켜질 때([학생 등록] 탭 등) 연결된 번호가 보입니다"
    : cam.active === cam.preferred ? `● ${cam.active}번 연결됨` : `● ${cam.preferred}번이 열리지 않아 ${cam.active}번 사용 중`;

  $("key-email").textContent = settings.key_email || "Google Cloud에서 받은 JSON 키 파일을 고르세요";
  $("key-email").style.color = settings.key_email ? "" : "var(--err)";
  $("form-url").value = settings.google_form_url;
}

$("timeouts").addEventListener("click", guarded(async (e) => {
  const btn = e.target.closest("button[data-timeout]");
  if (!btn) return;
  const name = btn.dataset.timeout;
  await api("/settings/timeout", { method: "PUT", body: { name, seconds: settings.timeouts[name] + Number(btn.dataset.delta) } });
  await loadSettings();
}));

$("camera-select").addEventListener("change", guarded(async (e) => {
  await api("/settings/camera", { method: "PUT", body: { index: Number(e.target.value) } });
  toast("카메라 번호를 저장했습니다. 다음에 카메라를 켤 때 이 번호로 연결합니다.");
  await loadSettings();
}));

$("key-file").addEventListener("change", guarded(async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const res = await api("/settings/google-key", { method: "POST", body: { content: await file.text() } });
  toast(`서비스 계정 키를 등록했습니다.\n응답 시트의 [공유]에서 아래 이메일을 '뷰어'로 추가해야 명단을 읽을 수 있습니다.\n\n${res.key_email}`);
  await loadSettings();
}));

$("btn-form-url").addEventListener("click", guarded(async () => {
  await api("/settings/form-url", { method: "PUT", body: { url: $("form-url").value } });
  const res = await api("/roster");  // 바로 읽어 봐서 주소가 맞는지 알려준다
  if (res.error) toast(res.error, true);
  else toast(`저장했습니다. ${res.source} 명단 ${res.total}명을 읽었습니다.`);
}));

// ---------- 시작 ----------
api("/me").then((me) => showAdmin(me.user_id)).catch(() => showLogin());

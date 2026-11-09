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
// 출근 중 LED · 오늘 타임라인은 서버에 /presence를 PRESENCE_POLL_MS마다 한 번 묻고,
// 1초마다 흐르는 경과 시간은 브라우저가 혼자 센다 (서버에 매초 묻지 않는다).
const PRESENCE_POLL_MS = 20000;
const DAY_START_MIN = 6 * 60, DAY_END_MIN = 24 * 60;  // 타임라인 06:00~24:00 (00~06시 기록은 인정 안 됨)

let students = [];
let selectedStudent = null;
let presence = { students: {}, date: "" };
let serverOffsetMs = 0;          // 서버 시계 − 이 기기 시계
let presenceTimer = null;
const checkedIds = new Set();    // 30초마다 표를 다시 그려도 체크한 학생은 그대로 둔다

const serverNow = () => new Date(Date.now() + serverOffsetMs);
const pad2 = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const toMin = (hms) => { const [h, m, s] = hms.split(":").map(Number); return h * 60 + m + (s || 0) / 60; };
const nowMin = () => { const n = serverNow(); return n.getHours() * 60 + n.getMinutes() + n.getSeconds() / 60; };
const hhmm = (hms) => hms.slice(0, 5);
const statusOf = (id) => presence.students[id] || { today: [], working_since: null, missing: [] };

async function loadStudents() {
  [students] = await Promise.all([api("/students"), loadPresence()]);
  selectedStudent = null;
  checkedIds.clear();
  $("edit-form").reset();
  renderCheckout();
  $("check-all").checked = false;
  renderStudents();
  markShortfallButton();
  startPresencePolling();
}

async function loadPresence() {
  const before = Date.now();
  const res = await api("/presence");
  serverOffsetMs = new Date(res.now).getTime() - (before + Date.now()) / 2;
  presence = res;
}

function startPresencePolling() {
  clearInterval(presenceTimer);
  presenceTimer = setInterval(() => {
    if (currentTab !== "students" || document.hidden) return;  // 다른 탭·창을 보는 동안은 묻지 않는다
    refreshPresence().catch(() => {});
  }, PRESENCE_POLL_MS);
}

async function refreshPresence() {
  await loadPresence();
  renderStudents();
  if (selectedStudent) renderCheckout();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && currentTab === "students") refreshPresence().catch(() => {});
});

// 검색 — 이름·학번·전공은 글자 일부, 전화번호는 '-' 없이 숫자만 비교한다
function matchesSearch(s, q) {
  if (!q) return true;
  const digits = q.replace(/\D/g, "");
  return [s.user_id, s.name, s.major, s.grade, s.email].some((v) => String(v ?? "").toLowerCase().includes(q))
    || (digits.length >= 2 && String(s.phone ?? "").replace(/\D/g, "").includes(digits))
    || String(s.phone ?? "").includes(q);
}

function renderStudents() {
  const q = $("student-search").value.trim().toLowerCase();
  // 출근 중인 학생이 맨 위(먼저 온 순), 그다음 퇴근 누락, 나머지는 원래 순서
  const rank = (s) => { const st = statusOf(s.user_id); return st.working_since ? 0 : st.missing.length ? 1 : 2; };
  const shown = students
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => matchesSearch(s, q))
    .sort((a, b) => rank(a.s) - rank(b.s)
      || (statusOf(a.s.user_id).working_since || "").localeCompare(statusOf(b.s.user_id).working_since || "")
      || a.i - b.i)
    .map(({ s }) => s);

  const working = students.filter((s) => statusOf(s.user_id).working_since).length;
  const missing = students.filter((s) => statusOf(s.user_id).missing.length).length;
  $("working-count").textContent = working;
  $("missing-count").textContent = missing;
  $("student-count").textContent = q
    ? `검색 결과 ${shown.length}명 / 등록된 학생 ${students.length}명`
    : `등록된 학생 ${students.length}명 · 체크한 학생은 [선택 삭제]로 지웁니다`;

  $("student-rows").innerHTML = shown.map((s) => {
    const st = statusOf(s.user_id);
    const led = st.working_since ? "on" : st.missing.length ? "missing" : "";
    const ledTitle = st.working_since ? `출근 중 (${st.working_since} 출근)`
      : st.missing.length ? `퇴근 누락 ${st.missing.length}건 — 학생을 고르면 퇴근 처리할 수 있습니다` : "출근 전 / 퇴근함";
    return `
    <tr data-id="${esc(s.user_id)}" class="${selectedStudent?.user_id === s.user_id ? "selected" : ""}">
      <td class="check"><input type="checkbox" data-id="${esc(s.user_id)}" ${checkedIds.has(s.user_id) ? "checked" : ""}></td>
      <td>${esc(s.user_id)}</td><td>${esc(s.name)}</td><td>${esc(s.major)}</td>
      <td>${esc(s.penalty)}</td><td>${esc(s.phone)}</td><td>${esc(s.grade)}</td>
      <td class="timeline-col">${timelineHtml(st)}</td>
      <td class="led-col"><i class="led ${led}" title="${esc(ledTitle)}" aria-label="${esc(ledTitle)}"></i></td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="muted">${q ? "검색 결과가 없습니다" : "등록된 학생이 없습니다"}</td></tr>`;
  $("check-all").checked = shown.length > 0 && shown.every((s) => checkedIds.has(s.user_id));
  tickLive();
}

// 오늘 출퇴근을 06~24시 막대 위에 칠한다. 출근 중인 구간은 지금 시각까지 늘어난다 (tickLive)
function timelineHtml(st) {
  if (!st.today.length) return '<span class="muted tl-empty">—</span>';
  const span = DAY_END_MIN - DAY_START_MIN;
  const pct = (m) => (Math.min(Math.max(m, DAY_START_MIN), DAY_END_MIN) - DAY_START_MIN) / span * 100;
  const segs = st.today.map((x) => {
    const start = toMin(x.check_in);
    const live = !x.check_out && x.check_in === st.working_since;
    if (!x.check_out && !live) {  // 퇴근 없이 끝난 앞 출근 — 점으로만 표시
      return `<span class="tl-seg dot" style="left:${pct(start)}%" title="${hhmm(x.check_in)} 출근 · 퇴근 기록 없음"></span>`;
    }
    const end = live ? nowMin() : toMin(x.check_out);
    const title = live ? `${hhmm(x.check_in)} 출근 · 출근 중` : `${hhmm(x.check_in)} ~ ${hhmm(x.check_out)}`;
    return `<span class="tl-seg ${live ? "live" : ""}" ${live ? `data-start="${start}"` : ""}
      style="left:${pct(start)}%;width:${Math.max(pct(end) - pct(start), 0.6)}%" title="${title}"></span>`;
  }).join("");
  const label = st.working_since
    ? `<b>${hhmm(st.working_since)}</b> 출근 · <span data-since="${st.working_since}"></span>`
    : `${st.today.length}회 · ${st.today.filter((x) => x.check_out).map((x) => `${hhmm(x.check_in)}~${hhmm(x.check_out)}`).join(", ") || "퇴근 기록 없음"}`;
  return `<div class="tl-bar">${segs}</div><div class="tl-label">${label}</div>`;
}

// 1초마다 — 출근 중인 학생의 경과 시간과 막대 길이만 고친다 (서버에 묻지 않는다)
function tickLive() {
  const now = nowMin(), span = DAY_END_MIN - DAY_START_MIN;
  for (const el of document.querySelectorAll("#student-rows [data-since]")) {
    el.textContent = duration((now - toMin(el.dataset.since)) * 60);
  }
  for (const el of document.querySelectorAll("#student-rows .tl-seg.live")) {
    const start = Number(el.dataset.start);
    el.style.width = `${Math.max((Math.min(now, DAY_END_MIN) - start) / span * 100, 0.6)}%`;
  }
}
setInterval(() => { if (currentTab === "students" && !document.hidden) tickLive(); }, 1000);

// 날짜가 바뀌면(자정) 오늘 기준이 달라지므로 바로 다시 읽는다
setInterval(() => {
  if (currentTab === "students" && presence.date && ymd(serverNow()) !== presence.date) {
    refreshPresence().then(markShortfallButton).catch(() => {});
  }
}, 60000);

$("student-search").addEventListener("input", renderStudents);

$("student-rows").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-id]");
  if (!row) return;
  if (e.target.matches("input[type=checkbox]")) {
    if (e.target.checked) checkedIds.add(row.dataset.id); else checkedIds.delete(row.dataset.id);
    return;
  }
  for (const r of $("student-rows").children) r.classList.toggle("selected", r === row);
  selectedStudent = students.find((s) => s.user_id === row.dataset.id);
  const form = $("edit-form");
  for (const key of ["user_id", "name", "major", "phone", "grade", "penalty", "email"]) form.elements[key].value = selectedStudent[key] ?? "";
  renderCheckout(true);
});

$("check-all").addEventListener("change", (e) => {
  for (const box of $("student-rows").querySelectorAll("input[type=checkbox]")) {
    box.checked = e.target.checked;
    if (e.target.checked) checkedIds.add(box.dataset.id); else checkedIds.delete(box.dataset.id);
  }
});

// ---------- 퇴근 처리 ----------
let checkoutKey = "";  // 고른 학생의 미퇴근 목록 — 바뀌었을 때만 다시 그려서 고르던 값을 지키지 않게
function renderCheckout(force = false) {
  const box = $("checkout-box");
  const st = selectedStudent ? statusOf(selectedStudent.user_id) : null;
  const open = st ? [
    ...(st.working_since ? [{ date: presence.date, check_in: st.working_since, now: true }] : []),
    ...[...st.missing].reverse(),  // 최근 것부터
  ] : [];
  const key = `${selectedStudent?.user_id}|${open.map((o) => o.date + o.check_in).join(",")}`;
  if (!force && key === checkoutKey) return;
  checkoutKey = key;
  box.hidden = !open.length;
  if (!open.length) return;
  $("checkout-session").innerHTML = open.map((o, i) =>
    `<option value="${i}" data-date="${o.date}" data-in="${o.check_in}">${dateKo(o.date)} ${hhmm(o.check_in)} 출근 ${o.now ? "· 지금 출근 중" : "· 퇴근 기록 없음"}</option>`).join("");
  syncCheckoutTime();
}

function syncCheckoutTime() {
  const opt = $("checkout-session").selectedOptions[0];
  if (!opt) return;
  const isToday = opt.dataset.date === presence.date;
  const nowHm = `${pad2(serverNow().getHours())}:${pad2(serverNow().getMinutes())}`;
  const input = $("checkout-time");
  input.min = hhmm(opt.dataset.in);
  input.max = isToday ? nowHm : "23:59";
  input.value = isToday ? nowHm : "";
  $("checkout-hint").textContent = `${hhmm(opt.dataset.in)} 이후 ~ ${isToday ? `지금(${nowHm})` : "23:59"} 사이로 정하세요. `
    + "그날 이 출근 뒤에 다른 기록이 있으면 그 앞까지만 됩니다.";
}
$("checkout-session").addEventListener("change", syncCheckoutTime);

$("btn-checkout").addEventListener("click", guarded(async () => {
  const opt = $("checkout-session").selectedOptions[0];
  const time = $("checkout-time").value;
  if (!selectedStudent || !opt) throw new Error("퇴근 처리할 학생을 리스트에서 선택하세요.");
  if (!time) throw new Error("퇴근 시각을 정하세요.");
  const who = `${selectedStudent.name} (${selectedStudent.user_id})`;
  if (!confirm(`${who}\n${dateKo(opt.dataset.date)} ${hhmm(opt.dataset.in)} 출근 → ${time} 퇴근으로 처리할까요?\n관리자 처리 기록으로 남습니다.`)) return;
  const res = await api(`/students/${encodeURIComponent(selectedStudent.user_id)}/checkout`, {
    method: "POST", body: { date: opt.dataset.date, check_in: opt.dataset.in, check_out: time },
  });
  toast(`${selectedStudent.name} 학생을 ${res.check_out}에 퇴근 처리했습니다.`);
  await refreshPresence();
  renderCheckout(true);
}));

// ---------- 주간 미달자 ----------
const isThursday = () => serverNow().getDay() === 4;

function markShortfallButton() {
  const btn = $("btn-shortfall");
  btn.classList.toggle("primary", isThursday());
  btn.classList.toggle("ghost", !isThursday());
  btn.textContent = isThursday() ? "주간 미달자 · 오늘 목요일" : "주간 미달자";
}

async function loadShortfall() {
  const base = $("shortfall-date").value;
  const res = await api(`/weekly-shortfall${base ? `?base=${base}` : ""}`);
  const hours = res.required_seconds / 3600;
  $("shortfall-desc").textContent = `${dateKo(res.start)} ~ ${dateKo(res.end)} · 주 ${res.required_days}회 · ${hours}시간 중 하나라도 못 채운 학생`;
  $("shortfall-rows").innerHTML = res.rows.map((s) => {
    const working = statusOf(s.user_id).working_since && res.start <= presence.date && presence.date <= res.end;
    return `<tr>
      <td>${esc(s.user_id)}</td><td>${esc(s.name)}${working ? ' <i class="led on" title="지금 출근 중 (진행 중인 시간은 퇴근해야 더해집니다)"></i>' : ""}</td>
      <td>${s.days}일</td><td>${duration(s.seconds)}</td>
      <td class="${s.missing_days ? "short" : ""}">${s.missing_days ? `${s.missing_days}일` : "충족"}</td>
      <td class="${s.missing_seconds ? "short" : ""}">${s.missing_seconds ? duration(s.missing_seconds) : "충족"}</td>
      <td class="mail-cell">${mailStatusHtml(s)}</td></tr>`;
  }).join("") || '<tr><td colspan="7" class="muted">모든 학생이 조건을 채웠습니다</td></tr>';
  const noEmail = res.rows.filter((s) => !s.email).length;
  $("shortfall-result").textContent = `미달 ${res.rows.length}명 / 등록된 학생 ${students.length}명`
    + (noEmail ? ` · 이메일 없음 ${noEmail}명` : "") + " · 출석 시간은 퇴근까지 찍힌 기록만 더합니다";
  // 메일은 이번 주 미달자에게만 보낸다 (지난주를 보고 있으면 버튼을 숨긴다)
  $("btn-shortfall-mail").hidden = !res.this_week || !res.rows.length;
  $("btn-shortfall-mail").disabled = !res.mail_ready;
  $("shortfall-mail-note").textContent = !res.mail_ready ? "메일을 보내려면 [설정] → 주간 미달 안내 메일을 먼저 설정하세요"
    : res.this_week ? `매주 목요일 06:00 이후 미달자에게 자동으로 보냅니다 (받은 학생에게는 다시 보내지 않음)` : "";
}

function mailStatusHtml(s) {
  if (!s.email) return '<span class="muted">이메일 없음</span>';
  const title = esc(s.email);
  if (!s.mail) return `<span class="muted" title="${title}">보내기 전</span>`;
  if (s.mail.ok) return `<span class="mail-ok" title="${title}">✓ ${esc(s.mail.sent_at.slice(5, 16))}</span>`;
  return `<span class="mail-fail" title="${esc(s.mail.error || "")}">✕ 실패</span>`;
}

$("btn-shortfall-mail").addEventListener("click", guarded(async () => {
  if (!confirm("이번 주 미달 학생에게 지금 안내 메일을 보낼까요?\n이미 받은 학생과 이메일이 없는 학생은 건너뜁니다.")) return;
  $("btn-shortfall-mail").disabled = true;
  try {
    const res = await api("/mail/weekly-shortfall", { method: "POST" });
    let msg = `보냄 ${res.sent.length}명 · 이미 받음 ${res.already.length}명 · 이메일 없음 ${res.no_email.length}명`;
    if (res.failed.length) msg += `\n실패 ${res.failed.length}명: ${res.failed[0][1]}`;
    toast(msg, res.failed.length > 0);
  } finally {
    await loadShortfall();
  }
}));

$("btn-shortfall").addEventListener("click", guarded(async () => {
  $("shortfall-date").value = ymd(serverNow());
  await loadShortfall();
  $("shortfall-dialog").showModal();
}));
$("shortfall-date").addEventListener("change", () => guarded(loadShortfall)());

$("edit-form").addEventListener("submit", guarded(async (e) => {
  e.preventDefault();
  if (!selectedStudent) throw new Error("수정할 대상을 리스트에서 선택하세요.");
  const f = e.target.elements;
  await api(`/students/${encodeURIComponent(selectedStudent.user_id)}`, {
    method: "PUT",
    body: { name: f.name.value, major: f.major.value, penalty: f.penalty.value, phone: f.phone.value, grade: f.grade.value, email: f.email.value },
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

// 화면 배치(영상 2 : 카드 1, 높이 맞춤)는 admin.css에서 한다. 여기서는 고개 돌리기 안내가 입력 카드 자리에
// 나와도 높이가 같게(영상 크기가 들썩이지 않게) 입력 카드가 보일 때의 높이만 기억해 둔다 (--reg-side-h)
function sizeRegisterCamera() {
  if (!$("register-form").hidden && $("register-form").offsetHeight > 0) {
    $("tab-register").style.setProperty("--reg-side-h", `${$("register-form").offsetHeight}px`);
  }
}
const registerSizer = new ResizeObserver(sizeRegisterCamera);
registerSizer.observe($("register-form"));

async function openRegister() {
  sizeRegisterCamera();
  const keep = () => api("/register/camera", { method: "POST" }).catch((e) => toast(e.message, true));
  await keep();
  clearInterval(registerTimer);
  registerTimer = setInterval(keep, 20000);  // 탭이 열려 있는 동안 카메라를 켜 둔다 (서버는 60초 소식이 없으면 끈다)
  $("register-video").src = `/video.mjpg?t=${Date.now()}`;
  connectState();
  await loadRoster(false);
  sizeRegisterCamera();  // 명단 안내 문구가 들어오면 입력 카드 높이가 바뀐다
}

function stopRegisterCamera() {
  if (registerTimer === null) return;  // [학생 등록] 탭을 연 적이 없다
  clearInterval(registerTimer);
  clearTimeout(rosterTimer);
  registerTimer = null;
  $("register-guide").hidden = true;
  $("register-form").hidden = false;
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
  // 고개 돌리기 중에는 입력 카드 자리에 안내를 띄우고, 다 돌리면(done) 입력 카드로 돌아간다
  const turning = st.mode === "register" && !!card.challenge && !card.challenge.done;
  if ($("register-guide").hidden === turning) {
    $("register-guide").hidden = !turning;
    $("register-form").hidden = turning;
    sizeRegisterCamera();
  }
  if (turning) renderTurnGuide($("register-turn"), card.challenge);
}

$("btn-recenter").addEventListener("click", guarded(async () => {
  await api("/register/recenter", { method: "POST" });
  toast("지금 고개 방향을 정면으로 다시 잡았습니다.\n처음 방향부터 다시 돌려 주세요.");
}));

function setVisual(el, state) {
  el.className = "visual small";
  el.innerHTML = "";
  if (state === "loading" || state === "search" || state === "turn") el.classList.add("spin");
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
  for (const key of ["user_id", "name", "major", "phone", "grade", "email"]) f[key].value = s[key] || "";
});

$("register-form").addEventListener("submit", guarded(async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const res = await api("/register", {
    method: "POST",
    body: { user_id: f.user_id.value, name: f.name.value, major: f.major.value, phone: f.phone.value, grade: f.grade.value, email: f.email.value },
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
      const type = (r.log_type === "CHECK_IN" ? "출근" : "퇴근") + (r.valid ? "" : " (미인정)")
        + (r.edited_by ? ` · 관리자 처리(${r.edited_by})` : "");
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
  await loadMailSettings();
}

async function loadMailSettings() {
  const mail = await api("/mail");
  $("mail-desc").textContent = `매주 ${mail.weekday}요일 ${mail.time} 이후 처음 켜져 있을 때, 그 주 미달 학생에게 출근 일수·남은 시간을 메일로 보냅니다. `
    + "메일은 조교 구글 계정의 Apps Script가 보냅니다.";
  $("mail-secret").value = mail.secret;
  $("mail-url").value = mail.url;
  const last = mail.last;
  $("mail-last").textContent = !mail.url ? "● 아직 설정하지 않았습니다 — 웹 앱 주소를 저장하면 자동 발송이 켜집니다"
    : last ? `● 자동 발송 켜짐 · 마지막 발송 ${last.at} — 보냄 ${last.sent}명, 실패 ${last.failed}명, 이메일 없음 ${last.no_email}명`
    : "● 자동 발송 켜짐 · 아직 보낸 적 없음";
  $("mail-last").style.color = mail.url ? "var(--ok)" : "var(--warn)";
}

$("btn-copy-secret").addEventListener("click", guarded(async () => {
  $("mail-secret").select();
  try { await navigator.clipboard.writeText($("mail-secret").value); } catch { document.execCommand("copy"); }
  toast("비밀값을 복사했습니다. Apps Script의 SECRET = '...' 안에 붙여넣으세요.");
}));

$("btn-mail-url").addEventListener("click", guarded(async () => {
  await api("/mail", { method: "PUT", body: { url: $("mail-url").value } });
  toast($("mail-url").value.trim() ? "저장했습니다. [시험 메일]로 잘 가는지 확인하세요." : "웹 앱 주소를 지웠습니다. 자동 발송이 꺼집니다.");
  await loadMailSettings();
}));

$("btn-mail-test").addEventListener("click", guarded(async () => {
  const res = await api("/mail/test", { method: "POST", body: { to: $("mail-test-to").value } });
  toast(`시험 메일을 보냈습니다. 받은편지함을 확인하세요.${res.remaining != null ? `\n(오늘 더 보낼 수 있는 메일: ${res.remaining}통)` : ""}`);
}));

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

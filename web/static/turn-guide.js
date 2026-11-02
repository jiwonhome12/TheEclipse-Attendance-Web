// 고개 돌리기 안내 — 출석 화면과 관리자 [학생 등록] 탭이 같이 쓴다.
// 서버(engine.TurnChallenge)가 보내준 challenge = {steps: ["left","right"], index, done, yaw, turn}로 그린다.
//  · 얼굴 그림이 지금 돌려야 할 방향으로 고개를 돌리는 모습을 반복해서 보여준다
//  · 단계 표시: ① 첫 방향 → ② 둘째 방향 → ③ 정면
//  · 방향 막대: 지금 고개가 어느 쪽을 향하는지 점으로 보여주고, 넘어야 할 구간을 칠해 둔다
// 화면은 거울처럼 뒤집혀 있으므로 '왼쪽' = 본인 기준 왼쪽 = 화면 왼쪽이다.
const TURN_LABEL = { left: "왼쪽", right: "오른쪽", center: "정면" };
const TURN_ARROW = { left: "←", right: "→", center: "●" };
const YAW_RANGE = 0.5;  // 방향 막대 끝이 나타내는 값 (이보다 더 돌려도 끝에 머문다)

const TURN_GUIDE_HTML = `
  <div class="tg-stage">
    <span class="tg-arrow left" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>
    </span>
    <svg class="tg-head" viewBox="0 0 120 130" aria-hidden="true">
      <ellipse class="tg-ear l" cx="22" cy="68" rx="7" ry="12"/>
      <ellipse class="tg-ear r" cx="98" cy="68" rx="7" ry="12"/>
      <ellipse class="tg-skull" cx="60" cy="64" rx="40" ry="50"/>
      <g class="tg-face">
        <path class="tg-brow" d="M36 46q8-5 16 0M68 46q8-5 16 0"/>
        <circle class="tg-eye" cx="44" cy="58" r="4.5"/>
        <circle class="tg-eye" cx="76" cy="58" r="4.5"/>
        <path class="tg-nose" d="M60 60v16h-6"/>
        <path class="tg-mouth" d="M48 92q12 9 24 0"/>
      </g>
    </svg>
    <span class="tg-arrow right" aria-hidden="true">
      <svg viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg>
    </span>
  </div>
  <ol class="tg-steps"></ol>
  <div class="tg-meter" aria-hidden="true">
    <span class="tg-zone left"></span>
    <span class="tg-zone right"></span>
    <span class="tg-center"></span>
    <span class="tg-dot"></span>
  </div>
  <div class="tg-meter-labels" aria-hidden="true"><span>← 왼쪽</span><span>정면</span><span>오른쪽 →</span></div>`;

function renderTurnGuide(el, ch) {
  if (!el.dataset.ready) {
    el.innerHTML = TURN_GUIDE_HTML;
    el.dataset.ready = "1";
  }
  const target = ch.done || ch.index >= ch.steps.length ? "center" : ch.steps[ch.index];
  el.dataset.dir = target;  // CSS가 얼굴 그림·화살표를 이 방향으로 움직인다

  // 단계: 돌릴 두 방향 + 마지막 정면
  const steps = [...ch.steps, "center"];
  const doneCount = ch.done ? steps.length : ch.index;
  el.querySelector(".tg-steps").innerHTML = steps.map((dir, i) => {
    const cls = i < doneCount ? "done" : i === doneCount ? "now" : "";
    const mark = i < doneCount ? "✓" : i + 1;
    return `<li class="${cls}"><b>${mark}</b>${TURN_ARROW[dir]} ${TURN_LABEL[dir]}</li>`;
  }).join("");

  // 방향 막대: 가운데가 정면, 양 끝이 ±YAW_RANGE. 넘어야 할 구간(±turn 바깥)을 칠한다
  const zone = Math.max(0, (YAW_RANGE - ch.turn) / YAW_RANGE) * 50;
  for (const side of ["left", "right"]) {
    const z = el.querySelector(`.tg-zone.${side}`);
    z.style.width = `${zone}%`;
    z.classList.toggle("target", side === target);
  }
  const dot = el.querySelector(".tg-dot");
  dot.hidden = ch.yaw === null || ch.yaw === undefined;
  if (!dot.hidden) {
    const x = 50 + Math.max(-1, Math.min(1, ch.yaw / YAW_RANGE)) * 50;
    dot.style.left = `${x}%`;
    dot.classList.toggle("hit", (target === "left" && ch.yaw <= -ch.turn) || (target === "right" && ch.yaw >= ch.turn));
  }
}

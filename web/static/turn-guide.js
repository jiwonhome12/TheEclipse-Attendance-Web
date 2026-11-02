// 고개 돌리기 안내 — 출석 화면과 관리자 [학생 등록] 탭이 같이 쓴다.
// 서버(engine.TurnChallenge)가 보내준 challenge로 그린다:
//   {steps: ["up","left"] (상하좌우 중 무작위 2방향), index, done, yaw, pitch, turn, pitch_turn}
//  · 얼굴 그림이 지금 돌려야 할 방향으로 고개를 돌리는 모습을 반복해서 보여준다 (그쪽 화살표가 깜빡인다)
//  · 단계 표시: ① 첫 방향 → ② 둘째 방향 → ③ 정면
//  · 방향 판: 지금 고개가 향한 쪽을 점으로 보여주고, 넘어야 할 구간을 칠해 둔다
// 화면은 거울처럼 뒤집혀 있으므로 '왼쪽' = 본인 기준 왼쪽 = 화면 왼쪽이다.
const TURN_LABEL = { left: "왼쪽", right: "오른쪽", up: "위", down: "아래", center: "정면" };
const TURN_ARROW = { left: "←", right: "→", up: "↑", down: "↓", center: "●" };
const YAW_RANGE = 0.5;    // 방향 판 좌우 끝이 나타내는 값
const PITCH_RANGE = 0.3;  // 방향 판 위아래 끝이 나타내는 값

const chevron = (d) => `<svg viewBox="0 0 24 24"><path d="${d}"/></svg>`;
const TURN_GUIDE_HTML = `
  <div class="tg-body">
    <div class="tg-stage">
      <span class="tg-arrow up" aria-hidden="true">${chevron("M5 15l7-7 7 7")}</span>
      <span class="tg-arrow left" aria-hidden="true">${chevron("M15 5l-7 7 7 7")}</span>
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
      <span class="tg-arrow right" aria-hidden="true">${chevron("M9 5l7 7-7 7")}</span>
      <span class="tg-arrow down" aria-hidden="true">${chevron("M5 9l7 7 7-7")}</span>
    </div>
    <div class="tg-pad" aria-hidden="true">
      <span class="tg-zone left"></span><span class="tg-zone right"></span>
      <span class="tg-zone up"></span><span class="tg-zone down"></span>
      <span class="tg-cross"></span>
      <span class="tg-dot"></span>
      <small class="tg-pad-label">지금 고개 방향</small>
    </div>
  </div>
  <ol class="tg-steps"></ol>`;

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

  // 방향 판: 가운데가 정면. 넘어야 할 구간(기준값 바깥)을 칠하고, 지금 돌려야 할 쪽을 강조한다
  const zx = Math.max(0, (YAW_RANGE - ch.turn) / YAW_RANGE) * 50;
  const zy = Math.max(0, (PITCH_RANGE - ch.pitch_turn) / PITCH_RANGE) * 50;
  for (const side of ["left", "right", "up", "down"]) {
    const z = el.querySelector(`.tg-zone.${side}`);
    z.style[side === "left" || side === "right" ? "width" : "height"] = `${side === "left" || side === "right" ? zx : zy}%`;
    z.classList.toggle("target", side === target);
  }
  const dot = el.querySelector(".tg-dot");
  const known = ch.yaw !== null && ch.yaw !== undefined;
  dot.hidden = !known;
  if (known) {
    const clamp = (v) => Math.max(-1, Math.min(1, v));
    const pitch = ch.pitch ?? 0;  // 정면 기준값을 잡기 전에는 가운데 높이에 둔다
    dot.style.left = `${50 + clamp(ch.yaw / YAW_RANGE) * 50}%`;
    dot.style.top = `${50 + clamp(pitch / PITCH_RANGE) * 50}%`;
    const hit = {
      left: ch.yaw <= -ch.turn, right: ch.yaw >= ch.turn,
      up: pitch <= -ch.pitch_turn, down: pitch >= ch.pitch_turn, center: false,
    }[target];
    dot.classList.toggle("hit", hit);
  }
}

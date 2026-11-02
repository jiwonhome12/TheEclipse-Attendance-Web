// 고개 돌리기 안내 — 출석 화면과 관리자 [학생 등록] 탭이 같이 쓴다.
// 서버(engine.TurnChallenge)가 보내준 challenge로 그린다:
//   {steps: ["up","left"] (상하좌우 중 무작위 2방향), index, done, yaw, pitch, turn, pitch_turn}
//  · 얼굴 그림: 눈·코·입을 입체로 돌려서 지금 돌려야 할 방향으로 고개를 돌리는 모습을 반복한다
//  · 단계 표시: ① 첫 방향 → ② 둘째 방향 → ③ 정면 (끝난 단계는 ✓가 튀어나온다)
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
      <span class="tg-arrow up" aria-hidden="true">${chevron("M6 15l6-6 6 6")}</span>
      <span class="tg-arrow left" aria-hidden="true">${chevron("M15 6l-6 6 6 6")}</span>
      <div class="tg-head" aria-hidden="true">
        <div class="tg-skull"></div>
        <div class="tg-rotor">
          <span class="tg-brow l"></span><span class="tg-brow r"></span>
          <span class="tg-eye l"></span><span class="tg-eye r"></span>
          <span class="tg-cheek l"></span><span class="tg-cheek r"></span>
          <span class="tg-nose"></span>
          <span class="tg-mouth"></span>
        </div>
        <div class="tg-shadow"></div>
      </div>
      <span class="tg-arrow right" aria-hidden="true">${chevron("M9 6l6 6-6 6")}</span>
      <span class="tg-arrow down" aria-hidden="true">${chevron("M6 9l6 6 6-6")}</span>
    </div>
    <div class="tg-pad" aria-hidden="true">
      <span class="tg-zone left"></span><span class="tg-zone right"></span>
      <span class="tg-zone up"></span><span class="tg-zone down"></span>
      <span class="tg-cross"></span>
      <span class="tg-dot-track"><span class="tg-dot"></span></span>
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
  // 같은 방향이면 그대로 둔다 — 다시 넣으면 애니메이션이 처음부터 다시 시작해서 끊겨 보인다
  if (el.dataset.dir !== target) el.dataset.dir = target;

  // 단계: 돌릴 두 방향 + 마지막 정면. 바뀌었을 때만 다시 그려서 ✓ 튀어나오는 효과가 한 번만 나게 한다
  const steps = [...ch.steps, "center"];
  const doneCount = ch.done ? steps.length : ch.index;
  const key = `${steps.join(",")}|${doneCount}`;
  const list = el.querySelector(".tg-steps");
  if (list.dataset.key !== key) {
    const before = list.dataset.key && list.dataset.key.split("|")[0] === steps.join(",")
      ? Number(list.dataset.key.split("|")[1]) : doneCount;
    list.dataset.key = key;
    list.innerHTML = steps.map((dir, i) => {
      const cls = [i < doneCount ? "done" : i === doneCount ? "now" : "", i >= before && i < doneCount ? "pop" : ""]
        .filter(Boolean).join(" ");
      const mark = i < doneCount ? "✓" : i + 1;
      return `<li class="${cls}"><b>${mark}</b>${TURN_ARROW[dir]} ${TURN_LABEL[dir]}</li>`;
    }).join('<li class="tg-link" aria-hidden="true"></li>');
  }

  // 방향 판: 가운데가 정면. 넘어야 할 구간(기준값 바깥)을 칠하고, 지금 돌려야 할 쪽을 강조한다
  const zx = Math.max(0, (YAW_RANGE - ch.turn) / YAW_RANGE) * 50;
  const zy = Math.max(0, (PITCH_RANGE - ch.pitch_turn) / PITCH_RANGE) * 50;
  for (const side of ["left", "right", "up", "down"]) {
    const z = el.querySelector(`.tg-zone.${side}`);
    const horizontal = side === "left" || side === "right";
    z.style[horizontal ? "width" : "height"] = `${horizontal ? zx : zy}%`;
    z.classList.toggle("target", side === target);
  }
  const track = el.querySelector(".tg-dot-track");
  const dot = track.firstElementChild;
  const known = ch.yaw !== null && ch.yaw !== undefined;
  track.hidden = !known;
  if (known) {
    const clamp = (v) => Math.max(-1, Math.min(1, v));
    const pitch = ch.pitch ?? 0;  // 정면 기준값을 잡기 전에는 가운데 높이에 둔다
    // 판 크기 기준 %로 옮긴다 (transform이라 부드럽게 미끄러진다)
    track.style.transform = `translate(${clamp(ch.yaw / YAW_RANGE) * 50}%, ${clamp(pitch / PITCH_RANGE) * 50}%)`;
    const hit = {
      left: ch.yaw <= -ch.turn, right: ch.yaw >= ch.turn,
      up: pitch <= -ch.pitch_turn, down: pitch >= ch.pitch_turn, center: false,
    }[target];
    dot.classList.toggle("hit", hit);
  }
}

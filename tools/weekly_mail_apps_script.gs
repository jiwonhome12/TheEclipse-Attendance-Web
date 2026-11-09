/**
 * 창의공간 출석체크 — 주간 미달 안내 메일 보내기 (Google Apps Script 웹 앱)
 *
 * 출석체크 프로그램(core.py send_mail)이 이 웹 앱에 {secret, to, subject, body}를 보내면
 * 이 스크립트를 배포한 구글 계정(조교 계정)으로 메일을 보낸다.
 * 프로그램에 메일 비밀번호를 저장하지 않으려고 이렇게 나눴다.
 *
 * 설치 방법
 *  1. https://script.google.com → [새 프로젝트] → 이 파일 내용을 통째로 붙여넣기
 *  2. 아래 SECRET에 관리자 화면 [설정] → 주간 미달 안내 메일의 '비밀값'을 그대로 넣기
 *  3. [배포] → [새 배포] → 유형: 웹 앱
 *       - 다음 사용자 인증 정보로 실행: 나
 *       - 액세스 권한이 있는 사용자: 모든 사용자
 *     → [배포] → 권한 허용 (메일 보내기 권한)
 *  4. 나온 '웹 앱 URL'(https://script.google.com/macros/s/.../exec)을 관리자 화면에 붙여넣고 저장
 *  5. [시험 메일 보내기]로 확인
 *  ※ 코드를 고친 뒤에는 [배포] → [배포 관리] → 수정 → 버전: 새 버전 으로 다시 배포해야 반영된다.
 *  ※ 하루에 보낼 수 있는 메일 수: 개인 Gmail 100통, Google Workspace(학교) 계정 1,500통
 */
const SECRET = '여기에-관리자-화면의-비밀값을-넣으세요';
const SENDER_NAME = '창의공간 출석체크';

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    if (data.secret !== SECRET) return reply({ ok: false, error: 'unauthorized' });
    if (!data.to || !data.subject || !data.body) return reply({ ok: false, error: '받는 주소·제목·본문이 비어 있습니다.' });
    MailApp.sendEmail({ to: data.to, subject: data.subject, body: data.body, name: SENDER_NAME });
    return reply({ ok: true, remaining: MailApp.getRemainingDailyQuota() });
  } catch (err) {
    return reply({ ok: false, error: String(err && err.message || err) });
  }
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

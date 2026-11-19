// 관점별 장면(타임라인). 시각은 "이야기 시각"(초)이고, holds 의 멈춤은 재생 중에 자동으로 끼워진다.
// 패킷은 멈춤 중에도 움직인다. 재생 기본 속도는 M.base(0.5)라 실제 재생 시간은 (이야기+멈춤)/0.5 초.
(function () {
  'use strict';

  function sceneStructure() {
    M.setup({
      duration: 20,
      chapters: [{ t: 0, label: '이전' }, { t: 6, label: '장애' }, { t: 10, label: '변경' }, { t: 14, label: '이후' }],
      holds: [[1.2, 5], [5.6, 4], [7.4, 4], [9.6, 4], [12.4, 5], [16.4, 5], [19.6, 8]],
    });
    M.title('서비스 클러스터 재배치 — 이전과 이후', '관점 · 구성 변화');
    M.legend(24, 82, { chips: [['prd', 'api', '운영 API'], ['dev', 'dev', '개발'], ['val', 'batch', '배치'], ['obs', 'log', '관측']], packets: [['req', '요청'], ['obs', '로그·메트릭'], ['fail', '실패']] });
    const B = diagram('before');
    const API = ['api1', 'api2', 'api3'];

    B.req(API, 0.2, 6.2, { every: 1 });
    B.db('api1', 0.2, 6.0);
    B.mon({ groups: [0, 1, 2], log: ['log', 'log', 'log'], mon: 'mon' }, 0.4, 6.0);
    M.show(M.callout(350, 500, 420, ['API 3대가 server1에 몰림', '배치 제약이 없음'], 'warn', { arrow: [495, 240] }), 0.6, 10);
    M.cap(0, 6, '이전', '요청은 LB를 거쳐 API로 갑니다', 'API 3대가 모두 server1에 있고, 로그와 모니터링은 server2 한 곳에 모여 있습니다.');

    B.down(0, 6.4, 10);
    B.chipState(['api1', 'api2', 'api3', 'batch'], 'dead', 6.5, 10);
    B.req(API, 6.4, 7.6, { kind: 'fail', every: 1 });
    const c = M.show(M.counter(790, 500, 260, '실패한 요청', 'warn'), 6.6, 10);
    M.num(c.num, [[6.7, 0], [7.3, 1200]], v => Math.round(v).toLocaleString('ko-KR') + '건');
    M.cap(6, 10, '장애', 'server1 하나가 멈추면 서비스 전체가 멈춥니다', '예시 수치입니다. 실제 프로젝트에서는 근거 문서의 수치와 출처를 씁니다.', 'warn');

    for (let i = 0; i < 2; i++) B.role(i, 'prod', 10.2);
    B.role(2, 'non', 10.2);
    const end = B.morph('before', 'after', 10.5, 1.2, 0.04, { log2: 'log' });
    M.show(M.callout(350, 500, 460, ['API를 서버마다 1대씩', '로그 저장소 2대로 이중화'], 'ok'), end + 0.1, 14);
    M.cap(10, 14, '변경', '서버는 그대로, 배치 규칙만 바꿨습니다', 'API를 서버마다 1대씩 나누고 로그 저장소를 2대로 늘렸습니다.', 'ok');

    B.req(API, 14.0, 20);
    B.mon({ groups: [0, 1, 2], log: ['log', 'log', 'log2'], mon: 'mon' }, 14.2, 20);
    B.down(2, 16.0);
    B.chipState(['api3', 'batch', 'log2', 'dev'], 'dead', 16.1);
    const left = M.show(M.counter(350, 500, 300, '남은 API', 'ok'), 16.0, null);
    M.text(left.num, [[0, '2 / 3대']]);
    M.cap(14, 20.01, '이후', '서버 1대가 멈춰도 API 2/3이 남습니다', 'server3을 멈춘다고 가정하면 API 2대가 계속 처리하고 관측 화면도 유지됩니다.', 'ok');
  }

  window.SCENE_TITLE = '서비스 클러스터 재배치';
  window.SCENES = {
    structure: { label: '구성 변화', desc: '이전 배치 → 장애 → 변경 → 이후 배치에서 같은 장애', build: sceneStructure },
  };
  // 관점이 둘 이상이면 장 대응을 준다: SCENE_MAP[from][to][from의 장 번호] = to의 장 번호
  window.SCENE_MAP = {};
})();

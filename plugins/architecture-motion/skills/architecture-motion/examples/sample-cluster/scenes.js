// 합성 예제의 장면. 모든 이름과 수치는 설명용으로 지어낸 것이다.
(function () {
  'use strict';

  const WEB = ['webA', 'webB'];

  function sceneStructure() {
    M.setup({
      duration: 16,
      chapters: [{ t: 0, label: '이전' }, { t: 5, label: '장애' }, { t: 8, label: '변경' }, { t: 11, label: '이후' }],
      holds: [[1.2, 4], [5.8, 4], [9.6, 4], [13.4, 5], [15.6, 6]],
    });
    M.title('사진 공유 서비스 — 노드 재배치', '관점 · 구성 변화');
    M.legend(24, 82, { chips: [['prd', 'web', '서비스'], ['val', 'worker', '작업'], ['obs', 'metrics', '관측'], ['dev', 'stage', '시험']], packets: [['req', '요청'], ['obs', '지표'], ['fail', '실패']] });
    const B = diagram('before');

    B.req(WEB, 0.2, 5.0);
    B.hop('webA', 'queue', 0.4, 5.0);
    B.watch(0.4, 5.0);
    M.show(M.callout(350, 500, 400, ['web 2대가 node-a에 같이 있음', '작업자도 같은 노드'], 'warn', { arrow: [495, 240] }), 0.6, 8);
    M.cap(0, 5, '이전', '요청은 LB를 거쳐 web으로 갑니다', 'web-a·web-b·worker가 모두 node-a 한 대에 있습니다.');

    B.down(0, 5.2, 8);
    B.chipState(['webA', 'webB', 'worker'], 'dead', 5.3, 8);
    B.req(WEB, 5.2, 6.4, { kind: 'fail' });
    const c = M.show(M.counter(790, 500, 260, '실패한 요청(예시)', 'warn'), 5.4, 8);
    M.num(c.num, [[5.5, 0], [6.0, 340]], v => Math.round(v).toLocaleString('ko-KR') + '건');
    M.cap(5, 8, '장애', 'node-a가 멈추면 web이 모두 멈춥니다', '수치는 설명용 예시입니다.', 'warn');

    B.role(0, 'prod', 8.1);
    B.role(1, 'prod', 8.1);
    B.role(2, 'non', 8.1);
    const end = B.morph('before', 'after', 8.3, 1.0, 0.04);
    M.show(M.callout(350, 500, 420, ['web을 노드마다 1대씩', 'cache는 web 옆으로'], 'ok'), end + 0.1, 11);
    M.cap(8, 11, '변경', '노드 수는 그대로, 배치만 바꿨습니다', 'web-a는 node-a, web-b는 node-b, worker는 node-c로 나눴습니다.', 'ok');

    B.req(WEB, 11.0, 16);
    B.watch(11.2, 16);
    B.down(0, 13.0);
    B.chipState(['webA', 'cache'], 'dead', 13.1);
    const left = M.show(M.counter(350, 500, 300, '남은 web', 'ok'), 13.0, null);
    M.text(left.num, [[0, '1 / 2대']]);
    M.cap(11, 16.01, '이후', '노드 하나가 멈춰도 web 1대가 남습니다', 'node-a를 멈춘다고 가정하면 web-b가 계속 요청을 받습니다.', 'ok');
  }

  function sceneFlow() {
    M.setup({
      duration: 12,
      chapters: [{ t: 0, label: '읽기' }, { t: 4, label: '쓰기' }, { t: 8, label: '지연' }],
      holds: [[1.4, 4], [5.4, 4], [9.4, 5], [11.6, 5]],
    });
    M.title('사진 공유 서비스 — 요청 흐름', '관점 · 데이터 경로');
    M.legend(24, 82, { chips: [['prd', 'web', '서비스'], ['val', 'queue', '작업']], packets: [['req', '요청'], ['db', 'DB'], ['fail', '지연']] });
    const B = diagram('after');

    B.req(WEB, 0.2, 4.0);
    B.hop('webB', 'cache', 0.4, 4.0);
    B.read('webB', B.boxes.dbReplica, 0.8, 4.0);
    M.cap(0, 4, '읽기', '사진 목록은 cache를 먼저 봅니다', '없으면 db-replica에서 읽습니다.');

    B.hop('webA', 'queue', 4.2, 8.0);
    B.hop('queue', 'worker', 4.6, 8.0);
    B.read('worker', B.boxes.dbPrimary, 5.0, 8.0);
    M.cap(4, 8, '쓰기', '올린 사진은 queue를 거쳐 worker가 저장합니다', 'web은 바로 응답하고, 저장은 worker가 이어서 합니다.');

    M.state(B.boxes.dbPrimary, 'down', 8.2, 12);
    B.read('worker', B.boxes.dbPrimary, 8.2, 12, 'fail');
    B.hop('webA', 'queue', 8.2, 12);
    const q = M.show(M.counter(790, 500, 260, '쌓인 작업(예시)', 'warn'), 8.4, null);
    M.num(q.num, [[8.5, 0], [9.2, 75]], v => Math.round(v) + '건');
    M.cap(8, 12.01, '지연', 'db-primary가 느려도 업로드는 받습니다', '작업이 queue에 쌓였다가 db-primary가 돌아오면 처리됩니다. 수치는 예시입니다.', 'warn');
  }

  window.SCENE_TITLE = '사진 공유 서비스 (합성 예제)';
  window.SCENES = {
    structure: { label: '구성 변화', desc: '이전 배치 → 장애 → 변경 → 이후', build: sceneStructure },
    flow: { label: '요청 흐름', desc: '읽기·쓰기 경로와 DB 지연', build: sceneFlow },
  };
  window.SCENE_MAP = { structure: { flow: [0, 0, 1, 2] }, flow: { structure: [0, 3, 3] } };
})();

// 프로젝트별 구성도 배치와 흐름 묶음. 템플릿 예: 웹 서비스 서버 3대의 이전(한 서버에 몰림) → 이후(분산)
// 실제 프로젝트에서는 CHIPS·배치값·상자·흐름 묶음을 그 시스템에 맞게 바꾼다.
(function () {
  'use strict';

  // [구역 번호, 칸 번호]. before/after 는 레이아웃 이름이다. 한쪽에만 있으면 그 레이아웃에서만 보인다
  const CHIPS = {
    api1: { cat: 'prd', label: 'api', at: { before: [0, 0], after: [0, 0] } },
    api2: { cat: 'prd', label: 'api', at: { before: [0, 1], after: [1, 0] } },
    api3: { cat: 'prd', label: 'api', at: { before: [0, 2], after: [2, 0] } },
    batch: { cat: 'val', label: 'batch', at: { before: [0, 3], after: [2, 1] } },
    log: { cat: 'obs', label: 'log', at: { before: [1, 0], after: [1, 1] } },
    mon: { cat: 'obs', label: 'monitor', at: { before: [1, 1], after: [0, 1] } },
    log2: { cat: 'obs', label: 'log', at: { after: [2, 2] } },
    dev: { cat: 'dev', label: 'dev', at: { before: [2, 0], after: [2, 3] } },
  };

  window.diagram = function (layout) {
    const G = {
      frame: { x: 330, y: 118, w: 925, h: 362 },
      groups: ['server1', 'server2', 'server3'],
      roles: { prod: '운영', non: '개발' },
      badge: null,
      barText: '에이전트 · 프록시',
      downLines: ['응답 없음', '헬스체크 실패'],
      nodeX0: 350, nodeW: 290, nodeGap: 12, nodeY: 154, nodeH: 314,
      bandH: 34, podTop: 50, cols: 3, podW: 82, podH: 32, podGapX: 8, podGapY: 10, barH: 26, pk: 6,
      fs: { node: 17, small: 12.5, pod: 14, ext: 17, extSub: 13, row: 15 },
      entry: [330, 300],
    };
    const B = M.board(G, { layout, chips: CHIPS, title: '서비스 클러스터 · 서버 3대' });
    B.addBox('client', [24, 262, 140, 76], '사용자', '웹·앱', null, 'legacy');
    B.addBox('lb', [184, 270, 124, 60], 'LB', ':443');
    B.addBox('db', [1278, 118, 307, 62], 'DB', '주문 원장', '✕ 접속 불가');
    B.addPanel([1278, 196, 307, 200], '운영자 화면', [['대시보드', '메트릭'], ['로그 검색', '로그']]);

    const P = M.P;
    // 요청 흐름: 패킷은 3개 중 1개 칩에만 보내 화면을 덜 붐비게 한다(선은 전부 그려 배치가 보이게)
    B.req = function (ids, t0, t1, opt) {
      opt = opt || {};
      const every = opt.every || 3;
      ids.forEach((id, i) => {
        const go = [P(B.boxes.client, 'r'), P(B.boxes.lb, 'l'), P(B.boxes.lb, 'r'), G.entry, P(B.chips[id])];
        M.flow({ pts: go.concat(go.slice(0, -1).reverse()), t0: t0 + 0.17 * i, t1, n: i % every === 0 ? 1 : 0, travel: 3.2, kind: opt.kind || 'req', size: G.pk });
      });
    };
    B.db = (id, t0, t1) => M.flow({ pts: [P(B.chips[id]), P(B.boxes.db, 'l')], t0, t1, n: 1, travel: 1.8, kind: 'db', size: G.pk - 1 });
    B.mon = (o, t0, t1) => {
      for (const i of o.groups) M.flow({ pts: [B.groups[i].bar, P(B.chips[o.log[i]])], t0: t0 + 0.1 * i, t1, n: 1, travel: 1.6, kind: 'obs', size: G.pk - 1 });
      M.flow({ pts: [P(B.chips[o.mon]), B.panel.rowAnchor(0)], t0: t0 + 0.5, t1, n: 1, travel: 1.4, kind: 'obs', size: G.pk });
    };
    return B;
  };
})();

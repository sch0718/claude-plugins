// 합성 예제: 가상의 사진 공유 서비스. 실제 시스템·수치와 관계없다.
// 이전: 웹 2대와 큐 작업자가 node-a 한 대에 몰려 있다 → 이후: node-a·b·c 에 나눠 놓는다.
(function () {
  'use strict';

  const CHIPS = {
    webA: { cat: 'prd', label: 'web-a', at: { before: [0, 0], after: [0, 0] } },
    webB: { cat: 'prd', label: 'web-b', at: { before: [0, 1], after: [1, 0] } },
    worker: { cat: 'val', label: 'worker', at: { before: [0, 2], after: [2, 0] } },
    queue: { cat: 'val', label: 'queue', at: { before: [1, 0], after: [1, 1] } },
    cache: { cat: 'prd', label: 'cache', at: { before: [1, 1], after: [0, 1] } },
    metrics: { cat: 'obs', label: 'metrics', at: { before: [2, 0], after: [2, 1] } },
    stage: { cat: 'dev', label: 'stage', at: { before: [2, 1], after: [2, 2] } },
  };

  window.diagram = function (layout) {
    const G = {
      frame: { x: 330, y: 118, w: 925, h: 362 },
      groups: ['node-a', 'node-b', 'node-c'],
      roles: { prod: '서비스', non: '보조' },
      badge: null,
      barText: '노드 에이전트',
      downLines: ['노드 응답 없음'],
      nodeX0: 350, nodeW: 290, nodeGap: 12, nodeY: 154, nodeH: 314,
      bandH: 34, podTop: 50, cols: 3, podW: 82, podH: 32, podGapX: 8, podGapY: 10, barH: 26, pk: 6,
      fs: { node: 17, small: 12.5, pod: 14, ext: 17, extSub: 13, row: 15 },
      entry: [330, 300],
    };
    const B = M.board(G, { layout, chips: CHIPS, title: '사진 공유 서비스 · 노드 3대' });
    B.addBox('client', [24, 262, 140, 76], '사용자', '앱·브라우저', null, 'legacy');
    B.addBox('lb', [184, 270, 124, 60], 'LB', ':443');
    B.addBox('dbPrimary', [1278, 118, 307, 62], 'db-primary', '쓰기', '✕ 응답 지연');
    B.addBox('dbReplica', [1278, 192, 307, 62], 'db-replica', '읽기');
    B.addPanel([1278, 268, 307, 150], '운영 화면', [['지표', 'metrics'], ['알림', '규칙 3개']]);

    const P = M.P;
    B.req = function (ids, t0, t1, opt) {
      opt = opt || {};
      ids.forEach((id, i) => {
        const go = [P(B.boxes.client, 'r'), P(B.boxes.lb, 'l'), P(B.boxes.lb, 'r'), G.entry, P(B.chips[id])];
        M.flow({ pts: go.concat(go.slice(0, -1).reverse()), t0: t0 + 0.2 * i, t1, n: 1, travel: 3.0, kind: opt.kind || 'req', size: G.pk });
      });
    };
    B.read = (from, to, t0, t1, kind) => M.flow({ pts: [P(B.chips[from]), P(to, 'l')], t0, t1, n: 1, travel: 1.8, kind: kind || 'db', size: G.pk - 1 });
    B.hop = (from, to, t0, t1, kind) => M.flow({ pts: [P(B.chips[from]), P(B.chips[to])], t0, t1, n: 1, travel: 1.6, kind: kind || 'req', size: G.pk - 1 });
    B.watch = (t0, t1) => M.flow({ pts: [P(B.chips.metrics), B.panel.rowAnchor(0)], t0, t1, n: 1, travel: 1.4, kind: 'obs', size: G.pk });
    return B;
  };
})();

// 플러그인이 어떻게 이어지는지 보여 주는 배치. 관점마다 구역 이름과 칩이 다르다.
// 항목 수(6개)와 하위 세션 수(2개)는 설명용 예시다.
(function () {
  'use strict';

  const FANOUT = {
    fanout: { cat: 'val', label: 'fanout', at: { pile: [0, 0], split: [0, 0], fix: [0, 0], merge: [0, 0] } },
    i1: { cat: 'dev', label: '항목1', at: { pile: [0, 1], split: [1, 0], fix: [1, 0] } },
    i2: { cat: 'dev', label: '항목2', at: { pile: [0, 2], split: [1, 1], fix: [1, 1] } },
    i3: { cat: 'dev', label: '항목3', at: { pile: [0, 3], split: [1, 2], fix: [1, 2] } },
    i4: { cat: 'dev', label: '항목4', at: { pile: [0, 4], split: [2, 0], fix: [2, 0] } },
    i5: { cat: 'dev', label: '항목5', at: { pile: [0, 5], split: [2, 1], fix: [2, 1] } },
    i6: { cat: 'dev', label: '항목6', at: { pile: [0, 6], split: [2, 2], fix: [2, 2] } },
    sonA: { cat: 'prd', label: 'Sonnet', at: { fix: [1, 3] } },
    haikuA: { cat: 'prd', label: 'Haiku', at: { fix: [1, 4] } },
    opusA: { cat: 'obs', label: 'Opus', at: { fix: [1, 5] } },
    sonB: { cat: 'prd', label: 'Sonnet', at: { fix: [2, 3] } },
    haikuB: { cat: 'prd', label: 'Haiku', at: { fix: [2, 4] } },
    opusB: { cat: 'obs', label: 'Opus', at: { fix: [2, 5] } },
    prA: { cat: 'val', label: 'PR A', at: { merge: [0, 1] } },
    prB: { cat: 'val', label: 'PR B', at: { merge: [0, 2] } },
    verify: { cat: 'obs', label: '통합 검증', at: { merge: [0, 3] } },
  };

  const DOCS = {
    search: { cat: 'prd', label: 'search', at: { main: [0, 0] } },
    impact: { cat: 'prd', label: 'impact', at: { main: [0, 1] } },
    review: { cat: 'val', label: '검토', at: { main: [1, 0] } },
    fix: { cat: 'val', label: '수정', at: { main: [1, 1] } },
    html: { cat: 'obs', label: 'HTML', at: { main: [2, 0] } },
    toc: { cat: 'obs', label: '목차', at: { main: [2, 1] } },
    pdf: { cat: 'obs', label: 'PDF', at: { main: [2, 2] } },
  };

  const INCIDENT = {
    post: { cat: 'dev', label: '회고 문서', at: { main: [0, 0] } },
    compose: { cat: 'prd', label: '구성 변화', at: { main: [1, 0] } },
    clock: { cat: 'prd', label: '장애 시계', at: { main: [1, 1] } },
    check: { cat: 'obs', label: '점검', at: { main: [1, 2] } },
    html: { cat: 'obs', label: 'HTML', at: { main: [2, 0] } },
    toc: { cat: 'obs', label: '목차', at: { main: [2, 1] } },
    zip: { cat: 'val', label: '압축', at: { main: [2, 2] } },
  };

  function frame(groups, roles) {
    return {
      frame: { x: 330, y: 118, w: 925, h: 362 },
      groups, roles, badge: null, barText: null, downLines: ['중단'],
      nodeX0: 350, nodeW: 290, nodeGap: 12, nodeY: 154, nodeH: 314,
      bandH: 34, podTop: 50, cols: 3, podW: 82, podH: 32, podGapX: 8, podGapY: 10, barH: 26, pk: 6,
      fs: { node: 17, small: 12.5, pod: 14, ext: 17, extSub: 13, row: 15 },
      entry: [330, 300],
    };
  }

  window.diagram = function (view, layout) {
    const P = M.P;
    let G, B;
    if (view === 'fanout') {
      G = frame(['메인 세션', '하위 세션 A', '하위 세션 B'], { prod: '', non: '' });
      B = M.board(G, { layout, chips: FANOUT, title: '고칠 것 → fanout → cascade-fix' });
      B.addBox('list', [24, 262, 140, 76], '고칠 것 목록', '진단 뒤 쌓임', null, 'legacy');
      B.addBox('repo', [1278, 118, 307, 62], 'main 브랜치', 'PR이 병합되는 곳');
      B.addPanel([1278, 196, 307, 120], 'PR 병합', [['PR A', '하위 세션 A'], ['PR B', '하위 세션 B']], { ok: '✓', down: '✕' });
      B.addBox('move', [1278, 340, 307, 100], 'session-move', '흐름 밖에서 세션·계정을 옮김');
      B.hop = (from, to, t0, t1, kind) => M.flow({ pts: [P(B.chips[from]), P(B.chips[to])], t0, t1, n: 1, travel: 1.8, kind: kind || 'val', size: G.pk });
      B.send = (t0, t1) => M.flow({ pts: [P(B.boxes.list, 'r'), G.entry, P(B.chips.fanout)], t0, t1, n: 1, travel: 2.6, kind: 'req', size: G.pk });
    } else if (view === 'docs') {
      G = frame(['doclookup', 'cascade-fix', 'offline-doc-bundle'], { prod: '', non: '' });
      B = M.board(G, { layout, chips: DOCS, title: '찾기 → 검토·수정 → 배포본' });
      B.addBox('docs', [24, 262, 140, 76], '마크다운 문서', '문서 묶음', null, 'legacy');
      B.addBox('out', [1278, 118, 307, 80], '배포본', '인터넷 없이 열리는 묶음');
      B.hop = (from, to, t0, t1, kind) => M.flow({ pts: [P(B.chips[from]), P(B.chips[to])], t0, t1, n: 1, travel: 1.8, kind: kind || 'val', size: G.pk });
      B.send = (t0, t1) => M.flow({ pts: [P(B.boxes.docs, 'r'), G.entry, P(B.chips.search)], t0, t1, n: 1, travel: 2.6, kind: 'req', size: G.pk });
    } else {
      G = frame(['회고 자료', 'architecture-motion', 'offline-doc-bundle'], { prod: '', non: '' });
      B = M.board(G, { layout, chips: INCIDENT, title: '장애 회고 발표' });
      B.addBox('incident', [24, 262, 140, 76], '장애 회고', '발표 준비', null, 'legacy');
      B.addBox('out', [1278, 118, 307, 80], '회고 배포본', '문서와 그림을 함께 묶음');
      B.hop = (from, to, t0, t1, kind) => M.flow({ pts: [P(B.chips[from]), P(B.chips[to])], t0, t1, n: 1, travel: 1.8, kind: kind || 'val', size: G.pk });
      B.send = (t0, t1) => M.flow({ pts: [P(B.boxes.incident, 'r'), G.entry, P(B.chips.post)], t0, t1, n: 1, travel: 2.6, kind: 'req', size: G.pk });
    }
    B.G = G;
    return B;
  };
})();

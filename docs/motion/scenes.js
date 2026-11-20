// 관점별 장면. 사실은 루트 README의 「함께 쓰는 조합」과 각 플러그인 소개에서 가져왔다.
// 항목 수·하위 세션 수는 설명용 예시다.
(function () {
  'use strict';

  const P = M.P;

  function sceneFanout() {
    M.setup({
      duration: 42,
      chapters: [{ t: 0, label: '쌓임' }, { t: 6, label: '나눔' }, { t: 13, label: '고침' }, { t: 27, label: '병합' }, { t: 34, label: '검증' }],
      holds: [[1.2, 4], [9.2, 5], [14.2, 5], [21.2, 7], [30.2, 5], [35.4, 6], [41.6, 6]],
    });
    M.title('고칠 것이 쌓이면 — fanout과 cascade-fix', '관점 · 리뷰 → 수정 → 병합');
    M.legend(24, 82, { chips: [['dev', '항목', '고칠 것'], ['val', 'fanout', '나누는 쪽'], ['prd', 'Sonnet', '작업자'], ['obs', 'Opus', '판가름·검증']], packets: [['req', '목록'], ['val', '맡김'], ['obs', '검증'], ['db', '병합']] });
    const B = diagram('fanout', 'pile');

    // 1. 쌓임
    B.send(0.2, 6.2);
    M.show(M.callout(350, 500, 420, ['진단 뒤 고칠 것이 쌓였어요', '한 세션에 항목이 모여 있음'], 'warn', { arrow: [495, 250] }), 0.6, 6);
    M.cap(0, 6, '쌓임', '진단하고 나니 고칠 것이 여럿 남았어요', '동시에 처리하고 싶은 항목이 쌓였습니다. 항목 수는 설명용 예시입니다.', 'warn');

    // 2. 나눔
    B.send(6.0, 6.5);
    const e1 = B.morph('pile', 'split', 6.5, 1.2, 0.04);
    M.show(M.callout(350, 500, 420, ['서로 같은 파일을 건드리지 않게 묶음', '묶음마다 하위 세션을 띄움'], 'ok'), e1 + 0.1, 13);
    B.hop('fanout', 'i1', e1 + 0.2, 13);
    B.hop('fanout', 'i4', e1 + 0.3, 13);
    M.cap(6, 13, '나눔', 'fanout이 겹치지 않게 묶어 하위 세션에 맡겨요', '같은 파일을 건드리는 항목은 한 묶음으로 둡니다. 묶음마다 하위 세션이 하나씩 떠서 따로 일합니다.', 'ok');

    // 3. 고침
    M.show(M.callout(350, 500, 420, ['하위 세션마다 같은 순서로 진행', '설계·계획 검토 → Codex 구현 → Opus 검토'], 'val'), 13.3, 18.6);
    const e2 = B.morph('split', 'fix', 18.6, 1.0, 0.03);
    M.flow({ pts: [P(B.chips.i1), P(B.chips.sonA)], t0: e2 + 0.1, t1: 27, n: 0, kind: 'obs' });
    M.flow({ pts: [P(B.chips.i1), P(B.chips.haikuA)], t0: e2 + 0.1, t1: 27, n: 0, kind: 'obs' });
    M.flow({ pts: [P(B.chips.i4), P(B.chips.sonB)], t0: e2 + 0.2, t1: 27, n: 0, kind: 'obs' });
    M.flow({ pts: [P(B.chips.i4), P(B.chips.haikuB)], t0: e2 + 0.2, t1: 27, n: 0, kind: 'obs' });
    M.flow({ pts: [P(B.chips.sonA), P(B.chips.opusA)], t0: e2 + 0.3, t1: 27, n: 0, kind: 'val' });
    M.flow({ pts: [P(B.chips.haikuA), P(B.chips.opusA)], t0: e2 + 0.3, t1: 27, n: 0, kind: 'val' });
    M.show(M.callout(350, 500, 420, ['cascade-fix: 지적 하나씩', '작업자 둘이 서로 다른 워크트리에서 고침'], 'obs'), e2 + 0.2, 27);
    M.show(M.callout(800, 500, 420, ['테스트를 다시 돌려 둘이 맞으면 채택', '갈린 것만 Opus가 판가름'], 'ok'), e2 + 0.5, 27);
    M.cap(13, 27, '고침', '각 하위 세션이 cascade-fix를 불러 써요', '구현과 검토를 거친 뒤 지적을 하나씩 고칩니다. Sonnet과 Haiku가 따로 고치고, 스크립트가 테스트를 다시 돌려 둘이 맞으면 채택합니다. 갈린 지적에만 Opus가 붙습니다.');

    // 4. 병합
    const e3 = B.morph('fix', 'merge', 27.3, 1.0, 0.03);
    M.show(B.panel, e3 - 0.5, null);
    M.flow({ pts: [P(B.chips.prA), B.panel.rowAnchor(0)], t0: e3 + 0.2, t1: 34, n: 1, travel: 2.2, kind: 'db' });
    M.flow({ pts: [P(B.chips.prB), B.panel.rowAnchor(1)], t0: e3 + 0.4, t1: 34, n: 1, travel: 2.2, kind: 'db' });
    M.show(M.callout(350, 500, 420, ['하위 세션이 PR을 만들어 병합', 'cascade-fix는 고친 것을 준비됨으로 넘김'], 'ok'), e3 + 0.1, 34);
    M.cap(27, 34, '병합', '각 세션이 PR 병합까지 끝내요', '직접 병합을 금지한 저장소에서는 PR까지만 만듭니다.', 'ok');

    // 5. 검증
    B.chipState('verify', 'hl', 34.2);
    M.show(M.callout(350, 500, 420, ['합쳐진 상태를 메인 세션이 다시 검증', '끝나면 세션·워크트리·브랜치를 정리'], 'ok'), 34.5, 42);
    M.show(M.callout(800, 500, 420, ['흐름 밖: session-move', '계정·기기를 옮길 때만 따로 써요'], 'info'), 35.0, 42);
    M.cap(34, 42.01, '검증', '모두 끝나면 메인 세션이 통합 검증해요', '병합이 끝난 뒤 합쳐진 결과를 다시 확인하고 정리합니다. 사용량 한도로 계정을 바꿀 때는 이 흐름과 별개로 session-move가 세션을 옮깁니다.', 'ok');
  }


  function sceneDocs() {
    M.setup({
      duration: 24,
      chapters: [{ t: 0, label: '찾기' }, { t: 8, label: '검토·수정' }, { t: 16, label: '배포본' }],
      holds: [[1.2, 5], [9.2, 5], [17.2, 6], [23.6, 6]],
    });
    M.title('문서 작업 — 찾고, 고치고, 배포본으로', '관점 · 문서 작업');
    M.legend(24, 82, { chips: [['prd', 'search', 'doclookup'], ['val', '검토', 'cascade-fix'], ['obs', 'HTML', 'offline-doc-bundle']], packets: [['req', '문서'], ['val', '넘김']] });
    const B = diagram('docs', 'main');
    B.fadeIn(['review', 'fix'], 8.2, 0.5);
    B.fadeIn(['html', 'toc', 'pdf'], 16.2, 0.5);

    B.send(0.2, 8);
    M.show(M.callout(350, 500, 420, ['용어·ID가 쓰인 곳을 파일:줄로', '바꾸면 같이 고칠 곳도 알려 줌'], 'info'), 0.6, 8);
    M.cap(0, 8, '찾기', 'doclookup으로 바꿀 곳을 먼저 찾아요', '문서를 제목 단위 절로 색인해 두고 용어나 ID가 쓰인 곳과 바꾸면 같이 고칠 곳을 돌려줍니다.');

    B.hop('search', 'review', 8.3, 16);
    B.hop('impact', 'fix', 8.5, 16);
    M.show(M.callout(350, 500, 420, ['문서 검토 지적을 하나씩 수정', '커밋·push 없이 준비됨에서 멈춤'], 'obs'), 8.8, 16);
    M.cap(8, 16, '검토·수정', 'cascade-fix가 문서를 검토하고 고쳐요', '설계·계획 문서 같은 마크다운도 지적 단위로 고칩니다. 고친 결과는 준비됨 상태로 사람에게 넘깁니다.', 'ok');

    B.hop('fix', 'html', 16.3, 24);
    B.hop('fix', 'toc', 16.5, 24);
    B.hop('fix', 'pdf', 16.7, 24);
    M.flow({ pts: [P(B.chips.toc), P(B.boxes.out, 'l')], t0: 17, t1: 24, n: 1, travel: 2.4, kind: 'db' });
    M.show(M.callout(350, 500, 420, ['Mermaid를 SVG로 굳히고 링크를 .html로', '← 목차 바 · 목차 페이지 · 검증 · 압축'], 'ok'), 16.8, 24);
    M.cap(16, 24.01, '배포본', 'offline-doc-bundle이 배포본을 만들어요', '인터넷이 없어도 열리는 HTML 묶음과 목차, 발표용 PDF로 정리하고 검증합니다. 변환기는 프로젝트의 것을 씁니다.', 'ok');
  }

  function sceneIncident() {
    M.setup({
      duration: 24,
      chapters: [{ t: 0, label: '회고' }, { t: 8, label: '그림' }, { t: 16, label: '묶음' }],
      holds: [[1.2, 4], [9.2, 5], [17.2, 6], [23.6, 6]],
    });
    M.title('장애 회고 발표 — 그림과 문서를 함께', '관점 · 장애 회고');
    M.legend(24, 82, { chips: [['dev', '회고 문서', '문서'], ['prd', '장애 시계', 'architecture-motion'], ['obs', 'HTML', 'offline-doc-bundle']], packets: [['req', '준비'], ['val', '넘김']] });
    const B = diagram('incident', 'main');
    B.fadeIn(['compose', 'clock', 'check'], 8.2, 0.5);
    B.fadeIn(['html', 'toc', 'zip'], 16.2, 0.5);

    B.send(0.2, 8);
    M.show(M.callout(350, 500, 420, ['장애 회고 문서가 준비돼 있음', '발표에는 움직이는 그림도 필요'], 'info'), 0.6, 8);
    M.cap(0, 8, '회고', '장애 회고를 발표해야 해요', '회고 문서는 있는데 장애가 어떻게 번졌는지는 움직이는 그림으로 보여 주고 싶은 상황입니다.');

    B.hop('post', 'compose', 8.3, 16);
    B.hop('post', 'clock', 8.5, 16);
    B.hop('clock', 'check', 9.0, 16, 'obs');
    M.show(M.callout(350, 500, 420, ['구성 변화와 장애 시계, 관점 토글', '글자 넘침·말풍선 겹침을 점검'], 'obs'), 8.8, 16);
    M.cap(8, 16, '그림', 'architecture-motion이 움직이는 그림을 만들어요', '외부 CDN 없는 정적 HTML 묶음이라 폐쇄망에서도 열립니다. MP4·GIF 변환은 하지 않습니다.', 'ok');

    B.hop('check', 'html', 16.3, 24);
    B.hop('html', 'toc', 16.5, 24);
    B.hop('toc', 'zip', 16.7, 24);
    M.flow({ pts: [P(B.chips.zip), P(B.boxes.out, 'l')], t0: 17, t1: 24, n: 1, travel: 2.4, kind: 'db' });
    M.show(M.callout(350, 500, 420, ['회고 문서와 함께 하나로 묶음', '목차·검증·압축까지'], 'ok'), 16.8, 24);
    M.cap(16, 24.01, '묶음', 'offline-doc-bundle로 회고 문서와 함께 묶어요', '그림과 문서를 한 배포본으로 정리해 메일로 보내거나 폐쇄망에 반입할 수 있습니다.', 'ok');
  }

  window.SCENE_TITLE = 'claude-plugins가 이어지는 방식';
  window.SCENES = {
    fanout: { label: '리뷰 → 수정 → 병합', desc: '고칠 것 → fanout → 하위 세션의 cascade-fix → PR 병합 → 통합 검증', build: sceneFanout },
    docs: { label: '문서 작업', desc: 'doclookup → cascade-fix → offline-doc-bundle', build: sceneDocs },
    incident: { label: '장애 회고', desc: 'architecture-motion → offline-doc-bundle', build: sceneIncident },
  };
  window.SCENE_MAP = {};
})();

# 엔진 API (`motion.js`)

전역 객체 `M` 하나다. 무대는 1600×900 좌표계이고 뷰어가 창 크기에 맞춰 늘리거나 줄인다.

## 목차
- 두 가지 시각
- 무대 구역 배치 기준
- 장면 골격
- 보드(구역·칩)
- 흐름(패킷)
- 말풍선·카운터·시계·표
- 상태·글자·숫자 바꾸기
- 함정

## 두 가지 시각

- **이야기 시각**(story, 초): 장면을 정의하는 시각. 모든 `t0/t1`, 장 시작, 멈춤 위치가 이 시각이다.
- **재생 시각**(playback): 이야기 시각 + 그 앞에 있는 멈춤 길이의 합. 실제 재생 초 = 재생 시각 ÷ `M.base`(0.5).
- `M.toPlay(st)`, `M.toStory(rt)` 로 바꾼다. 디버그 이동은 `M.seekStory(이야기 초)`.
- 뷰어 주소 `?t=<이야기 초>` 로 그 장면에서 멈춘 채 열린다. `?view=<관점 키>`, `?speed=1.5`, `?play=0` 도 된다.

## 무대 구역 배치 기준(1600×900)

| 구역 | y 범위 | 용도 |
|---|---|---|
| 제목 | 0~70 | `M.title(제목, 부제)` (x 28) · 오른쪽 위 x 1335 부근에 시계 |
| 범례 | 82~104 | `M.legend(24, 82, {...})` |
| 구성도 | 118~480 | 보드 프레임(가운데 x 330~1255), 왼쪽 x 24~310 진입점, 오른쪽 x 1278~1585 바깥 상자·패널 |
| 말풍선·카운터 | 492~735 | 한 줄에 3~5개(폭 280~420). 기록 상자(경과 로그)도 여기 |
| 자막 | 745~890 | 뷰어가 HTML로 그린다(`M.cap`) |

## 장면 골격

```js
function sceneX() {
  M.setup({
    duration: 30,                                  // 이야기 길이(초)
    chapters: [{ t: 0, label: '이전' }, { t: 8, label: '장애' }],
    holds: [[1.0, 5], [7.6, 6]],                  // [이야기 시각, 멈춤 초]
  });
  M.title('제목', '관점 · 부제');
  M.legend(24, 82, { chips: [['prd', 'api', '운영 API']], packets: [['req', '요청'], ['fail', '실패']] });
  const B = diagram('before');                     // diagram.js 에서 만든 보드
  // ... 흐름·말풍선·상태 ...
  M.cap(0, 8, '이전', '자막 제목', '자막 본문', '');  // kind: '' | 'warn' | 'ok'
}
window.SCENES = { structure: { label: '구성 변화', desc: '한 줄 설명', build: sceneX } };
window.SCENE_MAP = { structure: { clock: [0, 0, 1] } };  // 관점 전환 시 장 대응(선택)
window.SCENE_TITLE = '문서 제목';
```

`M.setup` 이 무대를 비우므로 관점 전환 때 뷰어가 `build` 를 다시 부른다. 장면 함수 밖에 상태를 두지 않는다.
마지막 자막은 `t1` 을 `duration + 0.01` 로 줘야 끝 프레임에서도 보인다.

## 보드(구역·칩) — `M.board(G, opt)`

`G`(배치값): `frame{x,y,w,h}`, `groups`(구역 이름 배열), `roles{prod, non}`(역할 띠 글자), `badge`(prod 구역 밑줄 글자, 없으면 null),
`barText`(구역 아래 막대 글자, 없으면 null), `downLines`(장애 표시 문구 배열), `nodeX0/nodeW/nodeGap/nodeY/nodeH`,
`bandH`, `podTop`(구역 위에서 첫 칩 줄까지), `cols`, `podW/podH/podGapX/podGapY`, `barH`, `pk`(패킷 크기),
`fs{node, small, pod, ext, extSub, row}`, `entry`(진입점 좌표, 흐름에서 씀).

`opt.chips`: `{ id: { cat, label, at: { 레이아웃: [구역, 칸] } } }`. `cat` 은 색 분류:
`prd` 파랑·굵은 테두리(핵심 운영), `dev` 회색(비운영), `val` 보라 점선(배치·보조 작업), `obs` 청록(관측).
칸은 `cols` 개마다 줄을 바꾼다. 칩 글자가 길면 엔진이 글자 크기를 자동으로 줄인다.

반환값 `B`:
- `B.chips[id]`, `B.groups[i]`(`.g`, `.ov`, `.downTexts`, `.bar()`), `B.boxes[key]`, `B.panel`
- `B.slot(구역, 칸)` → `{x,y}`, `B.slotCenter(구역, 칸)` → `[x,y]`(말풍선 화살표 끝에 씀)
- `B.chipState(ids, 'fail'|'dead'|'hl', t0, t1)` — 주황 실패(!) / 빗금 중단(✕) / 테두리 강조
- `B.role(i, 'prod'|'non', t0, t1)` — 구역 역할 띠 색
- `B.down(i, t0, t1)` — 구역 장애 빗금 + 문구(`B.groups[i].downTexts[k].textContent` 로 문구 변경)
- `B.morph(from, to, t0, d, stagger, spawnFrom)` — 레이아웃 전환, 끝나는 이야기 시각을 반환. `spawnFrom{새칩: 원본칩}` 은 원본 자리에서 나타나 이동(ES 1대 → 3대 분열 같은 장면)
- `B.moveTo(id, t0, t1, 구역, 칸)`, `B.fadeOut(ids, t0)`, `B.fadeIn(ids, t0)`
- `B.addBox(key, [x,y,w,h], 제목, 부제, 장애부제, cls)` — `M.state(B.boxes.key, 'down', t0, t1)` 로 빗금+장애부제
- `B.addPanel([x,y,w,h], 제목, [[이름, 설명], ...], {ok, down})` — 행에 `M.state(B.panel.rows[i].g, 'down', ...)`, 설명 변경은 `M.text(B.panel.rows[i].note, ...)`, 흐름 끝점은 `B.panel.rowAnchor(i)`

## 흐름(패킷) — `M.flow(o)`

`o.pts`: 점 배열. 각 점은 `[x,y]`, 함수(매 프레임 좌표 반환), 또는 아이템(가운데). 가장자리는 `M.P(item, 'l'|'r'|'t'|'b')`.
움직이는 칩을 가리키면 선이 칩을 따라간다. 왕복은 `go.concat(go.slice(0, -1).reverse())`.

`o.t0/t1`(이야기 시각, 앞뒤 0.3초 서서히), `o.n`(패킷 수, 0이면 선만), `o.travel`(한 바퀴에 걸리는 재생 시각. 실제 초 = travel ÷ base — 1.2~3.2가 무난), `o.kind`: `req` 파랑 원 · `val` 보라 사각 · `obs` 청록 마름모 · `db` 회색 원 · `fail` 주황 ✕,
`o.line`(false면 선 없이 패킷만), `o.alive`(false 를 돌려주면 숨김, 예: `B.alive(id)`).

패킷 위치는 재생 시각으로 움직여 멈춤 중에도 흐른다.

## 말풍선·카운터·시계·표

- `M.callout(x, y, w, lines, kind, { arrow:[x,y], fs, pad, center, layer })` — 높이는 줄 수로 자동. 첫 줄 굵게.
  kind: `warn`(⚠ 주황) `ok`(✓ 파랑) `val` `obs` `info` `req` `rewind`. 반환 아이템을 `M.show(it, t0, t1)` 로 띄운다
- `M.counter(x, y, w, 라벨, kind, { fs, nfs, h })` → `.num` 에 `M.num`(올라가는 숫자) 또는 `M.text`(바뀌는 글자)
- `M.clock(x, y, w, h)` → `.day`, `.time`. 시계 값은 `M.hook(t => ...)` 에서 직접 계산해 넣는다(장애 시계 예시 참고)
- `M.table(x, y, 열너비[], 행높이, rows, { backdrop:[y, h], fs })` — 정리 표. 첫 행은 머리글, 열 0·1·2 색이 머리/이전/이후

## 상태·글자·숫자 바꾸기

- `M.show(it, t0, t1?, { fade=0.35, pop=true })` — 나타남/사라짐. `t1` 이 없으면 계속 보임
- `M.state(대상, 클래스, t0, t1?)` — 그 구간에 클래스를 붙인다. 같은 대상·클래스 여러 구간은 OR
- `M.text(텍스트요소, [[t, '글자'], ...])`, `M.num(텍스트요소, [[t, 값], ...], 형식함수)`
- `M.key(it, [[t, {x, y, o, s}, 'l'|'step'?], ...])` — 직접 키프레임. 첫 키프레임 전에는 첫 값이 유지된다
- `M.hook(fn)` — 매 프레임 `fn(이야기 시각)`

## 함정

- **멈춤 위치**: 나타나는 중(`t0`~`t0+fade`)·사라지는 중(`t1-fade`~`t1`)·morph 중·카운터가 오르는 중에 멈추면 그 상태로 얼어붙는다. `check.mjs` 가 반투명 멈춤을 잡는다
- **장 경계로 이동**: `M.toPlay(장.t)` 로 옮기면 부동소수점 때문에 직전 장이 보일 수 있다. 뷰어는 +0.02 를 더해 옮긴다
- **CSS 전환**: 색 바뀜·장애 표시·자막 등장은 CSS 전환이라 실시간으로 진행된다. 장면 이동 직후 캡처하면 덜 바뀐 상태가 찍히니 1초 기다리거나 `document.getAnimations().forEach(a => a.finish())`.
- **장애 가정 중 흐름**: 죽은 칩으로 가는 흐름은 `t1` 을 장애 시각으로 끊고, 재배치 뒤 새 흐름을 따로 연다
- **좁은 화면**: 뷰어는 `clientWidth` 로 맞춘다(모바일에서 `innerWidth` 를 쓰면 무대가 커지는 순환이 생긴다). 엔진을 고치지 말 것

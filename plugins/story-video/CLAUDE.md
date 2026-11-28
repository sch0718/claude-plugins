# story-video

이야기를 5~10분 영상으로 만드는 Claude Code 플러그인(마켓플레이스 `sch0718/claude-plugins`의 `plugins/story-video`, PolyForm Noncommercial). 스킬 아홉 개(순서표 `story-video`와 단계 스킬 여덟 개)는 `skills/`, 공용 스크립트는 `scripts/`, 예시는 `examples/mini/`(일러스트 방식), `examples/mini-cutscene/`(게임 컷씬식), `examples/mini-motion/`(로컬 그림과 키프레임 움직임)에 있다.

- 시험: `node --test 'scripts/test/*.test.mjs'`(시간표·자막·길이 계산만). 저장소 루트의 `python3 -m unittest discover -s tests`도 통과해야 한다.
- 끝내기 전에 예시를 두 크기로 끝까지 렌더한다: `node scripts/build.mjs examples/mini all --provider say --no-phone [--format shorts]`, 그다음 `node scripts/check.mjs examples/mini [--format shorts] --frames 2,10,20`으로 뽑은 PNG를 본다. 그림이 없으면 단색 자리 그림으로 렌더된다. 낭독 검증은 `node scripts/voicecheck.mjs examples/mini --no-fix`. 게임 컷씬식은 `examples/mini-cutscene`을 같은 방법으로 두 크기 렌더하고, 애니메이션 장면 s02는 `art/<format>/anim/s02.png`에 2×2 자리 시트(ffmpeg 네 색)를 두면 칸이 바뀌는 것까지 볼 수 있다. 0.4.0의 컷·펀치·흔들림은 그림이 있을 때만 거치므로(단색 자리 그림은 카메라를 안 쓴다) `art/<format>/s0N.png`에 ffmpeg `testsrc2` 한 장씩을 두고 렌더한 뒤, 컷 경계 앞뒤 프레임과 `check.mjs`의 「움직임」 줄(장면당 컷 수·정지 구간)을 본다.
- 예시의 `art/`·`voice/`·`out/`는 커밋하지 않는다(`.gitignore`).
- `scripts/tts.mjs`·`scripts/finish.sh`는 narrated-video 스킬의 것을 복사해 온 것이다. 0.2.0부터 `tts.mjs`는 연기 지시(`direction`)와 다시 만들기 번호(`take`)를 받도록 갈라졌다. 지시·take가 없을 때의 캐시 해시는 0.1.0과 같아야 한다(시험이 지킨다).
- 이 Mac의 ffmpeg에는 자막 필터(libass·drawtext)가 없다. 자막은 `scripts/subs.py`(Pillow)로 투명 PNG를 그려 `overlay`로 겹친다.
- 0.3.0부터 `examples/mini`의 `voices`는 작성자 환경의 VoiceStudio 복제 프로필이다. 예시 렌더는 `--provider say`로 돌린다(임시판, 구조 검사만).
- 게임 컷씬식 설정(`style.mode`·`style.preset`·`style.anim`·`fx`·`sfx`·`anim`)과 컷 분할(`shotPlan`)·움직임 판정(`motionReport`)은 `lib.mjs`가 아니라 `scripts/cutscene.mjs`가 읽고 검증한다. `story.json`을 고치면 시간표가 오래된 것으로 보이므로 `build.mjs <에피소드> timeline --voices <에피소드>/out/voices.json` 뒤에 `visual`을 돌린다.
- 0.4.0부터 `story.pace`(`fast`·`normal`, 없으면 쇼츠는 `fast`)가 쉼·말 빠르기를 정하고(`lib.mjs`의 `PACES`), `selectVoices`가 fast면 speed를 채운다. 빠르기가 바뀌면 음성 캐시 해시도 바뀐다.
- 0.5.0부터 컷씬 움직임을 줄였다: 컷 3~7초(`shotPlan`의 `minShot`·`targetShot`·`maxShot`), 느낌표 확대는 장면당 한 번, 정지 판정은 4초(`STILL_SECONDS`), 시트 권장 0.7~1.5fps. 「정신없다」는 사용자 피드백 때문이니 다시 빠르게 바꾸지 않는다. 고르기 단계는 순위표를 사용자에게 보여 주고 멈춘다.
- 로컬 그림 설정과 명령 인자는 `scripts/local-art.mjs`, motion 설정·배치·렌더·캐시 입력은 `scripts/keyframes.mjs`가 맡는다. 자동 시험은 mflux를 실행하지 않고 인자와 ffmpeg 합성 그림으로 확인한다.
- 릴리스할 때 `.claude-plugin/plugin.json`의 `version`과 저장소 루트 `.claude-plugin/marketplace.json`의 story-video 항목을 함께 고친다.

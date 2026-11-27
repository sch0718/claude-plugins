# story-video

이야기를 5~10분 영상으로 만드는 Claude Code 플러그인(마켓플레이스 `sch0718/claude-plugins`의 `plugins/story-video`, PolyForm Noncommercial). 스킬 다섯 개는 `skills/`, 공용 스크립트는 `scripts/`, 예시는 `examples/mini/`에 있다.

- 시험: `node --test 'scripts/test/*.test.mjs'`(시간표·자막·길이 계산만). 저장소 루트의 `python3 -m unittest discover -s tests`도 통과해야 한다.
- 끝내기 전에 예시를 끝까지 렌더한다: `node scripts/build.mjs examples/mini all --provider say --no-phone`, 그다음 `node scripts/check.mjs examples/mini --frames 2,10,20`으로 뽑은 PNG를 본다. 그림이 없으면 단색 자리 그림으로 렌더된다.
- 예시의 `art/`·`voice/`·`out/`는 커밋하지 않는다(`.gitignore`).
- `scripts/tts.mjs`·`scripts/finish.sh`는 narrated-video 스킬의 것을 복사해 온 것이다. 플러그인이 혼자 설치돼도 돌게 하려고 복사했다. 고칠 때는 공급자 동작을 바꾸지 않는다.
- 이 Mac의 ffmpeg에는 자막 필터(libass·drawtext)가 없다. 자막은 `scripts/subs.py`(Pillow)로 투명 PNG를 그려 `overlay`로 겹친다.
- 릴리스할 때 `.claude-plugin/plugin.json`의 `version`과 저장소 루트 `.claude-plugin/marketplace.json`의 story-video 항목을 함께 고친다.

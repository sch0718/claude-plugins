"""시험용 확장. 첫 인자로 하위 명령을 받아 고정 JSON을 찍는다.

환경 변수: FAKE_EXT_OUTSIDE(체크아웃 밖 원문 폴더), FAKE_EXT_LOG(부른 하위 명령을 한 줄씩 적을 파일),
FAKE_EXT_SLEEP(이 초만큼 잔 뒤 답한다), FAKE_EXT_FAIL(이 하위 명령이면 종료 코드 3),
FAKE_EXT_TITLE(제7조 단위의 제목), FAKE_EXT_QUOTES_EXIT(quotes 종료 코드, 기본 0), FAKE_EXT_QUOTES_CWD(켜 두면 quotes가 작업 폴더를 한 줄 더 찍는다).
"""
import json
import os
import sys
import time
from pathlib import Path

sub = sys.argv[1]
if log := os.environ.get("FAKE_EXT_LOG"):
    with open(log, "a", encoding="utf-8") as f:
        f.write(sub + "\n")
if delay := os.environ.get("FAKE_EXT_SLEEP"):
    time.sleep(float(delay))
if os.environ.get("FAKE_EXT_FAIL") == sub:
    sys.exit(3)

outside = Path(os.environ["FAKE_EXT_OUTSIDE"])
if sub == "signature-files":
    print(json.dumps({"files": [str(outside / "외부.md")]}))
elif sub == "units":
    print(json.dumps({"units": [
        {"uid": "가짜규정#제7조", "doc": "가짜규정", "kind": "조", "number": "제7조",
         "title": os.environ.get("FAKE_EXT_TITLE", "변경"),
         "path": "제7조", "parent": None, "file": "20.원문/가짜규정.md", "line": 3, "text": "변경은 파트장이 승인한다."},
        {"uid": "가짜규정#제7조/1항", "doc": "가짜규정", "kind": "항", "number": "1", "title": None,
         "path": "제7조/1항", "parent": "가짜규정#제7조", "file": "20.원문/가짜규정.md", "line": 4, "text": "기록한다."},
    ]}, ensure_ascii=False))
elif sub == "roots":
    print(json.dumps({"roots": {"20.원문/": str(outside)}}, ensure_ascii=False))
elif sub == "cites":
    print(json.dumps({"cites": [
        {"file": "docs/지침.md", "line": 3, "raw": "가짜규정 제7조", "target": "가짜규정#제7조"},
        {"file": "docs/지침.md", "line": 4, "raw": "없는규정 제1조", "target": None},
    ]}, ensure_ascii=False))
elif sub == "run":
    name, *args = sys.argv[2:]
    if name == "quotes":
        print(f"quotes: {' '.join(args)}")
        if os.environ.get("FAKE_EXT_QUOTES_CWD"):
            print(f"cwd: {Path.cwd().resolve()}")
        sys.exit(int(os.environ.get("FAKE_EXT_QUOTES_EXIT", "0")))
    if name == "find" and args == ["제7조"]:
        print("가짜규정 제7조: 변경은 파트장이 승인한다.")
        sys.exit(0)
    print(f"찾지 못했다: {' '.join(args)}")
    sys.exit(1)
elif sub == "bad-json":
    print("json 아님")
else:
    sys.exit(2)

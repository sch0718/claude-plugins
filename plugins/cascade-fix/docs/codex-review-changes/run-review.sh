#!/usr/bin/env bash
# MCP·플러그인 없는 임시 CODEX_HOME에서 Codex 리뷰를 실행한다.
# 코드 변경: run-review.sh (--uncommitted | --base <브랜치> | --commit <SHA>) [--korean] [--out <파일>]
# 문서(설계·계획): run-review.sh --doc <파일> [--doc <파일>...] [--kind spec|plan] [--focus <관점>] [--known <파일>] [--repo <디렉터리>] [--out <파일>]
# 프롬프트 파일 실행(cascade-fix 재검토·의견): run-review.sh (--recheck | --opinion) <프롬프트 파일> [--repo <디렉터리>] [--out <파일>]
set -euo pipefail

usage() { sed -n '2,5p' "$0" | sed 's/^# //' >&2; exit 64; }

mode=""; target=""; korean=0; out=""; kind=""; focus=""; known=""; repo=""; docs=(); prompt_file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --uncommitted) mode=uncommitted; shift ;;
    --base) mode=base; target="${2:?--base 에는 브랜치가 필요하다}"; shift 2 ;;
    --commit) mode=commit; target="${2:?--commit 에는 SHA가 필요하다}"; shift 2 ;;
    --doc) mode=doc; docs+=("${2:?--doc 에는 파일이 필요하다}"); shift 2 ;;
    --recheck) mode=recheck; prompt_file="${2:?--recheck 에는 프롬프트 파일이 필요하다}"; shift 2 ;;
    --opinion) mode=opinion; prompt_file="${2:?--opinion 에는 프롬프트 파일이 필요하다}"; shift 2 ;;
    --kind) kind="${2:?--kind 에는 spec 또는 plan이 필요하다}"; shift 2 ;;
    --focus) focus="${2:?--focus 에는 관점이 필요하다}"; shift 2 ;;
    --known) known="${2:?--known 에는 파일이 필요하다}"; shift 2 ;;
    --repo) repo="${2:?--repo 에는 디렉터리가 필요하다}"; shift 2 ;;
    --korean) korean=1; shift ;;
    --out) out="${2:?--out 에는 경로가 필요하다}"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$mode" ] || usage

find_codex() {
  if command -v codex >/dev/null 2>&1; then command -v codex; return; fi
  local c
  for c in \
    /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex \
    /Applications/Codex.app/Contents/Resources/codex \
    "$HOME"/.cursor/extensions/openai.chatgpt-*/bin/macos-aarch64/codex; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  return 1
}
CODEX=$(find_codex) || { echo "codex 실행 파일을 찾지 못했다" >&2; exit 69; }

abs() { (cd "$(dirname "$1")" && printf '%s/%s' "$(pwd -P)" "$(basename "$1")"); }

if [ "$mode" = doc ]; then
  resolved=()
  for d in "${docs[@]}"; do
    [ -f "$d" ] || { echo "문서를 찾지 못했다: $d" >&2; exit 3; }
    resolved+=("$(abs "$d")")
  done
  docs=("${resolved[@]}")
  if [ -n "$known" ]; then
    [ -f "$known" ] || { echo "기각 목록을 찾지 못했다: $known" >&2; exit 64; }
    known=$(abs "$known")
  fi
  [ -n "$repo" ] || repo=$(dirname "${docs[0]}")
  ROOT=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null) || ROOT=$(cd "$repo" && pwd -P)
  if [ -z "$kind" ]; then
    case "${docs[0]}" in
      */plans/*|*plan*) kind=plan ;;
      */specs/*|*design*|*spec*) kind=spec ;;
      *) kind=spec ;;
    esac
  fi
elif [ "$mode" = recheck ] || [ "$mode" = opinion ]; then
  [ -f "$prompt_file" ] || { echo "프롬프트 파일을 찾지 못했다: $prompt_file" >&2; exit 3; }
  { [ -r "$prompt_file" ] && [ -s "$prompt_file" ]; } || { echo "프롬프트 파일을 읽을 수 없거나 비어 있다: $prompt_file" >&2; exit 3; }
  prompt_file=$(abs "$prompt_file")
  prompt=$(cat "$prompt_file") || { echo "프롬프트 파일을 읽지 못했다: $prompt_file" >&2; exit 3; }
  [ -n "$prompt" ] || { echo "프롬프트 파일을 읽을 수 없거나 비어 있다: $prompt_file" >&2; exit 3; }
  if [ -n "$repo" ]; then
    [ -d "$repo" ] || { echo "저장소 디렉터리를 찾지 못했다: $repo" >&2; exit 3; }
  else
    repo=$(dirname "$prompt_file")
  fi
  ROOT=$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null) || ROOT=$(cd "$repo" && pwd -P)
else
  ROOT=$(git rev-parse --show-toplevel)
fi
cd "$ROOT"

case "$mode" in
  uncommitted) [ -n "$(git status --porcelain)" ] || { echo "리뷰할 변경이 없다 (--uncommitted)" >&2; exit 3; } ;;
  base) git diff --quiet "$target"...HEAD && { echo "$target 대비 변경이 없다" >&2; exit 3; } || true ;;
  commit) git cat-file -e "$target^{commit}" 2>/dev/null || { echo "커밋을 찾지 못했다: $target" >&2; exit 3; } ;;
esac

SRC="$HOME/.codex"
[ -f "$SRC/auth.json" ] || { echo "$SRC/auth.json 없음: codex login 필요" >&2; exit 77; }

HOME_DIR=$(mktemp -d "${TMPDIR:-/tmp}/codex-review-home.XXXXXX")
trap 'rm -rf "$HOME_DIR"' EXIT
ln -s "$SRC/auth.json" "$HOME_DIR/auth.json"
[ "$korean" -eq 1 ] && [ -f "$SRC/AGENTS.md" ] && ln -s "$SRC/AGENTS.md" "$HOME_DIR/AGENTS.md"

model=$(sed -n 's/^model = "\(.*\)"/\1/p' "$SRC/config.toml" 2>/dev/null | head -1 || true)
effort=$(sed -n 's/^model_reasoning_effort = "\(.*\)"/\1/p' "$SRC/config.toml" 2>/dev/null | head -1 || true)
{
  [ -n "$model" ] && echo "model = \"$model\""
  [ -n "$effort" ] && echo "model_reasoning_effort = \"$effort\""
  printf '\n[skills]\ninclude_instructions = false\n'
  printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$ROOT"
} > "$HOME_DIR/config.toml"

[ -n "$out" ] || out=$(mktemp "${TMPDIR:-/tmp}/codex-review.XXXXXX")

doc_prompt() {
  local list="" d
  for d in "${docs[@]}"; do list="$list- $d"$'\n'; done
  local kind_label criteria
  if [ "$kind" = plan ]; then
    kind_label="구현 계획서"
    criteria='- 설계 문서와의 일치: 설계의 요구사항 중 계획에 빠진 것, 설계에 없는 것을 추가한 것
- 단계 순서와 의존성: 앞 단계 산출물이 뒤 단계의 전제로 맞물리는지, 순서를 바꾸면 깨지는 곳
- 각 단계의 검증 가능성: 완료를 판단할 테스트·명령이 있는지
- 계획이 가리키는 파일·함수·경로·설정 키가 저장소에 실제로 있는지 (직접 열어 확인)
- 계획 속 코드 스니펫의 오류(문법, 타입, 존재하지 않는 API, 기존 코드와의 충돌)
- 범위 이탈과 과잉 구현'
  else
    kind_label="설계 문서"
    criteria='- 요구사항의 누락·모호·모순
- 설계 결정의 근거와 검토된 대안
- 기존 코드·아키텍처와의 충돌 (관련 코드를 직접 열어 확인)
- 실패·보안·운영 경계 조건과 롤백
- 범위 과다(YAGNI)와 테스트 가능성
- 문서 안에서 서로 어긋나는 수치·이름·절차'
  fi
  cat <<PROMPT
다음 ${kind_label}를 리뷰하라. 한국어로 답하라.

대상 파일:
${list}
작업 디렉터리는 문서가 속한 저장소다. 읽기 전용이므로 어떤 파일도 수정하지 말고, 문서의 주장은 저장소 코드를 읽어 대조하라.

검토 관점:
${criteria}
${focus:+
추가 관점: ${focus}
}
출력 형식:
- Critical / Warning / Suggestion 세 절을 이 순서로 모두 쓴다. 각 절의 항목은 번호를 붙이고 들여 쓴 "위치:" / "근거:" / "제안:" 줄을 둔다. 해당 항목이 없는 절은 "없음."이라고 쓴다.
- "위치:"는 파일과 행(파일:행)이다. "근거:"에는 직접 확인한 코드 경로나 문서 구절을 적는다.
- 직접 확인하지 못한 것은 "미확인"으로 표시하고 단정하지 않는다. 추측으로 지적하지 않는다.
- 마지막 줄에 지적 수를 "SUMMARY: critical=<수> warning=<수> suggestion=<수>" 형식으로 적는다.

예:
Critical
1. <한 줄 요약>
   위치: <파일:행>
   근거: <직접 확인한 내용>
   제안: <고칠 방법>

Warning
없음.

Suggestion
없음.

SUMMARY: critical=1 warning=0 suggestion=0
PROMPT
  if [ -n "$known" ]; then
    cat <<PROMPT

아래는 이전 회차에서 검토해 반영하지 않기로 한 지적이다. 새 근거가 없으면 다시 지적하지 말고 지적 수에도 넣지 마라. 새 근거가 있으면 그 근거를 밝혀 지적하라.
--- 기각 목록 시작 ---
$(cat "$known")
--- 기각 목록 끝 ---
PROMPT
  fi
}

status=0
if [ "$mode" = doc ]; then
  CODEX_HOME="$HOME_DIR" "$CODEX" exec -s read-only --ephemeral -C "$ROOT" \
    -o "$out.final" "$(doc_prompt)" < /dev/null > "$out" 2>&1 || status=$?
elif [ "$mode" = recheck ] || [ "$mode" = opinion ]; then
  # 프롬프트는 stdin(-)으로 넘긴다. 큰 diff가 인자 길이 상한(ARG_MAX)에 걸리지 않게 한다
  CODEX_HOME="$HOME_DIR" "$CODEX" exec -s read-only --ephemeral -C "$ROOT" \
    -o "$out.final" - < "$prompt_file" > "$out" 2>&1 || status=$?
else
  case "$mode" in
    uncommitted) args=(--uncommitted) ;;
    base) args=(--base "$target") ;;
    commit) args=(--commit "$target") ;;
  esac
  CODEX_HOME="$HOME_DIR" "$CODEX" review "${args[@]}" < /dev/null > "$out" 2>&1 || status=$?
  awk '/^codex$/ {buf=""; next} {buf = buf $0 "\n"} END {printf "%s", buf}' "$out" | awk '$0 != prev {print} {prev=$0}' > "$out.final"
fi

echo "exit=$status"
echo "mode=$mode${kind:+ kind=$kind}"
echo "full_log=$out"
echo "final=$out.final"
if [ "$mode" = doc ]; then
  echo "summary=$(grep -oE 'SUMMARY: critical=[0-9]+ warning=[0-9]+ suggestion=[0-9]+' "$out.final" | tail -1 | sed 's/^SUMMARY: //')"
elif [ "$mode" != recheck ] && [ "$mode" != opinion ]; then
  echo "summary=$(for p in P0 P1 P2 P3; do printf '%s=%s ' "$p" "$(grep "\\[$p\\]" "$out.final" | sort -u | wc -l | tr -d ' ')"; done | sed 's/ $//')"
fi
echo "mcp_calls=$(grep -c '^mcp:' "$out" || true)"
echo "git_dirty=$(git status --porcelain | wc -l | tr -d ' ')"
exit "$status"

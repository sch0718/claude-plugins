#!/bin/sh
# 조각 영상을 잇고 음성을 붙인 뒤, 폰용 축소본까지 만든다.
#   sh finish.sh <출력.mp4> <음성.wav> <조각1.mp4> [조각2.mp4 ...]
#   → <출력.mp4> (영상은 다시 인코딩하지 않음, AAC 192k) + <출력>-phone.mp4 (960x540 2패스, AAC 96k)
#   sh finish.sh --phone-only <입력.mp4> <출력.mp4>
# 환경 변수: PHONE=0 이면 폰용을 건너뜀. PHONE_VBITRATE(기본 420k: 6분 30초가 약 25MB), PHONE_SCALE(기본 960:540)
# 조각은 모두 같은 해상도·fps·코덱이어야 한다(같은 렌더러로 만든 조각이면 된다).
set -e
make_phone() {
  vf="scale=${PHONE_SCALE:-960:540}:flags=lanczos"
  br=${PHONE_VBITRATE:-420k}
  ffmpeg -nostdin -v error -y -i "$1" -vf "$vf" -c:v libx264 -preset slow -b:v "$br" -pass 1 -passlogfile "$tmp/p" -an -f mp4 /dev/null
  ffmpeg -nostdin -v error -y -i "$1" -vf "$vf" -c:v libx264 -preset slow -b:v "$br" -pass 2 -passlogfile "$tmp/p" \
    -c:a aac -b:a 96k -movflags +faststart "$2"
}

if [ "${1:-}" = --phone-only ] && [ "$#" -eq 3 ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  make_phone "$2" "$3"
  exit
fi

[ $# -ge 3 ] || { echo "쓰는 법: sh finish.sh <출력.mp4> <음성.wav> <조각.mp4...>" >&2; exit 2; }
out=$1; audio=$2; shift 2
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

for f in "$@"; do printf "file '%s'\n" "$(cd "$(dirname "$f")" && pwd)/$(basename "$f")" >> "$tmp/list.txt"; done
ffmpeg -v error -y -f concat -safe 0 -i "$tmp/list.txt" -i "$audio" -map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k \
  -movflags +faststart "$out"

probe() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }
echo "$out: $(probe "$out")초, $(($(stat -f %z "$out" 2>/dev/null || stat -c %s "$out") / 1000000))MB (음성 $(probe "$audio")초)"

[ "${PHONE:-1}" = 0 ] && exit 0
phone="${out%.mp4}-phone.mp4"
make_phone "$out" "$phone"
echo "$phone: $(probe "$phone")초, $(($(stat -f %z "$phone" 2>/dev/null || stat -c %s "$phone") / 1000000))MB"

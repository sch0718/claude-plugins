import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel


def load(file):
    # faster-whisper의 PyAV 디코더는 판마다 인자가 달라 깨진다. ffmpeg로 16kHz 모노 float를 직접 만든다
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", file, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def main():
    parser = argparse.ArgumentParser(description="문장 음성 파일을 한 번에 받아 적는다.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="large-v3-turbo")
    args = parser.parse_args()

    files = json.loads(args.input.read_text(encoding="utf-8"))
    name = "deepdml/faster-whisper-large-v3-turbo-ct2" if args.model == "large-v3-turbo" else args.model
    model = WhisperModel(name, device="cpu", compute_type="int8")
    transcripts = []
    for i, file in enumerate(files, 1):
        print(f"{i}/{len(files)}", file=sys.stderr, flush=True)
        segments, _ = model.transcribe(
            load(file), language="ko", beam_size=5, vad_filter=False, condition_on_previous_text=False,
        )
        transcripts.append(" ".join(segment.text.strip() for segment in segments).strip())
    args.output.write_text(json.dumps(transcripts, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()

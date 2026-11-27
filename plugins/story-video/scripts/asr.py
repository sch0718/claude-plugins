import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np


def load(file):
    # faster-whisper의 PyAV 디코더는 판마다 인자가 달라 깨진다. ffmpeg로 16kHz 모노 float를 직접 만든다
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", file, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def median_pitch(audio):
    """16kHz 모노 음성에서 유성 구간의 기본 주파수 중앙값을 구한다."""
    rate = 16000
    window = 640  # 40ms
    hop = 160  # 10ms
    first = rate // 500
    last = (rate + 59) // 60
    lags = np.arange(first - 1, last + 2)
    pitches = []
    for offset in range(0, len(audio) - window + 1, hop):
        frame = audio[offset:offset + window].astype(np.float64)
        frame -= frame.mean()
        energy = np.concatenate(([0], np.cumsum(frame ** 2)))
        if energy[-1] < window * 0.003 ** 2:
            continue
        ac = np.correlate(frame, frame, mode="full")[window - 1 + lags]
        overlap = np.sqrt(energy[window - lags] * (energy[-1] - energy[lags]))
        ac = np.divide(ac, overlap, out=np.zeros_like(ac), where=overlap > 0)
        peaks = np.flatnonzero((ac[1:-1] > ac[:-2]) & (ac[1:-1] >= ac[2:])) + 1
        if not peaks.size:
            continue
        strength = ac[peaks].max()
        if strength < 0.6:
            continue
        # 비슷하게 강한 봉우리가 여럿이면 배음 주기의 배수가 되지 않도록 첫 봉우리를 쓴다.
        peak = peaks[ac[peaks] >= max(0.6, 0.9 * strength)][0]
        left, center, right = ac[peak - 1:peak + 2]
        lag = lags[peak] + (left - right) / (2 * (left - 2 * center + right))
        pitches.append(np.clip(rate / lag, 60, 500))
    return float(np.median(pitches)) if pitches else None


def main():
    parser = argparse.ArgumentParser(description="문장 음성 파일을 한 번에 받아 적는다.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--pitch", type=Path)
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    files = json.loads(args.input.read_text(encoding="utf-8"))
    name = "deepdml/faster-whisper-large-v3-turbo-ct2" if args.model == "large-v3-turbo" else args.model
    model = WhisperModel(name, device="cpu", compute_type="int8")
    transcripts = []
    pitches = []
    for i, file in enumerate(files, 1):
        print(f"{i}/{len(files)}", file=sys.stderr, flush=True)
        audio = load(file)
        segments, _ = model.transcribe(
            audio, language="ko", beam_size=5, vad_filter=False, condition_on_previous_text=False,
        )
        transcripts.append(" ".join(segment.text.strip() for segment in segments).strip())
        if args.pitch:
            pitches.append(median_pitch(audio))
    args.output.write_text(json.dumps(transcripts, ensure_ascii=False), encoding="utf-8")
    if args.pitch:
        args.pitch.write_text(json.dumps(pitches), encoding="utf-8")


if __name__ == "__main__":
    main()

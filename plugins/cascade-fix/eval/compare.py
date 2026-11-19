import argparse
import json
import math
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from select_profiles import flip_rate  # noqa: E402

GROUPS = {
    "solo": "단일 Claude",
    "claude": "cascade-fix · Claude만",
    "external": "cascade-fix · Codex·Cursor 사용",
}
SHAPES = ("circle", "square", "triangle", "diamond", "triangle-down", "hexagon")
THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "grid": "#e1e0d9", "axis": "#c3c2b7",
              "border": "#e4e4e7", "series": {"solo": "#2a78d6", "claude": "#eb6834", "external": "#1baf7a"}},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "grid": "#2c2c2a", "axis": "#383835",
             "border": "#3f3f46", "series": {"solo": "#3987e5", "claude": "#d95926", "external": "#199e70"}},
}
FONT = "'Pretendard Variable',Pretendard,'Apple SD Gothic Neo','Noto Sans KR',system-ui,sans-serif"
WIDTH, HEIGHT = 960, 560
PLOT = {"left": 86, "right": 900, "top": 118, "bottom": 440}


def load_spec(path) -> dict:
    path = Path(path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["_dir"] = str(path.parent)
    return spec


def read(spec: dict, name: str) -> dict:
    return json.loads((Path(spec["_dir"]) / name).read_text(encoding="utf-8"))


def stats(values: list[float]) -> dict:
    return {"mean": sum(values) / len(values), "min": min(values), "max": max(values)}


def shifted(s: dict, delta: float) -> dict:
    return {k: v + delta for k, v in s.items()}


def run_cost(spec: dict, run: dict) -> float:
    """점수 파일의 Claude 비용에 extra.from 점수 파일의 칸별 비용(extra.cells)을 더한다. 비용이 없는 칸(외부 runner)은 0."""
    cost = read(spec, run["score"])["claude_cost"]
    extra = run.get("extra")
    if extra:
        cells = read(spec, extra["from"]).get("cells", {})
        cost += sum(cells[c]["cost"] or 0.0 for c in extra["cells"] if c in cells)
    return cost


def summarize(spec: dict) -> list[dict]:
    """구성마다 회차 평균·최소·최대를 낸다. 진행 비용은 cascade-fix 구성에만 항목당으로 더한다."""
    total_items = spec["total_items"]
    orchestration = spec.get("orchestration", {}).get("per_item", 0.0)
    rows = []
    for i, config in enumerate(spec["configs"]):
        scores = [read(spec, run["score"]) for run in config["runs"]]
        costs = [run_cost(spec, run) / total_items for run in config["runs"]]
        orch = 0.0 if config["group"] == "solo" else orchestration
        minutes = [run["minutes"] for run in config["runs"] if "minutes" in run]
        work = stats(costs)
        cli = {name: sum(s.get("usage", {}).get(name, {}).get("input", 0) for s in scores) / len(scores)
               for name in ("codex", "cursor")}
        rows.append({
            "name": config["name"], "group": config["group"], "shape": SHAPES[i % len(SHAPES)], "runs": len(scores),
            "accuracy": stats([s["correct"] / total_items for s in scores]),
            "false_fact": stats([s["false_fact"] for s in scores]),
            "to_human": stats([s["to_human"] for s in scores]),
            "completion": stats([s["completion"] for s in scores]),
            "work_item": work, "orchestration_item": orch, "total_item": shifted(work, orch),
            "codex_input": cli["codex"], "cursor_input": cli["cursor"],
            "minutes": stats(minutes) if minutes else None, "minutes_runs": len(minutes),
            "flip_rate": flip_rate([s.get("predicted") or {} for s in scores]) if len(scores) > 1 else None,
        })
    base = next(r for r in rows if r["name"] == spec["baseline"])
    for row in rows:
        row["ratio"] = row["total_item"]["mean"] / base["total_item"]["mean"]
    return rows


def pct(value: float) -> str:
    return f"{value * 100:.1f}"


def spread(s: dict, fmt, runs: int, unit: str = "") -> str:
    if runs == 1 or s["min"] == s["max"]:
        return fmt(s["mean"]) + unit
    return f"{fmt(s['mean'])}{unit} [{fmt(s['min'])}~{fmt(s['max'])}]"


def number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def tokens(value: float) -> str:
    if not value:
        return "-"
    return f"{value / 10_000:,.0f}만" if value >= 10_000 else f"{value:,.0f}"


def markdown(rows: list[dict], spec: dict) -> str:
    n = spec["total_items"]
    head = (f"| 구성 | 회차 | 정확도(맞힘/{n}) | 「아님」 오채택 | 사람 목록 | 항목당 Claude 비용(작업 + 진행) "
            f"| {spec['baseline']} 대비 | Codex · Cursor 입력 토큰(회당) | 항목당 처리 시간 |")
    lines = [head, "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in rows:
        cost = (f"${r['total_item']['mean']:.2f}" if r["group"] == "solo" else
                f"${r['work_item']['mean']:.2f} + ${r['orchestration_item']:.2f} = ${r['total_item']['mean']:.2f}")
        lines.append(" | ".join([
            f"| {r['name']}", str(r["runs"]), spread(r["accuracy"], pct, r["runs"], "%"),
            spread(r["false_fact"], number, r["runs"]), spread(r["to_human"], number, r["runs"]), cost,
            f"{r['ratio'] * 100:.0f}%", f"{tokens(r['codex_input'])} · {tokens(r['cursor_input'])}",
            "-" if r["minutes"] is None else spread(r["minutes"], lambda v: f"{v:.1f}", r["minutes_runs"], "분")
            + ("" if r["minutes_runs"] == r["runs"] else f"({r['minutes_runs']}회)"),
        ]) + " |")
    basis = spec.get("orchestration", {}).get("basis")
    if basis:
        lines.append("")
        lines.append(f"진행 비용(추정): {basis}")
    return "\n".join(lines) + "\n"


def sensitivity(rows: list[dict], spec: dict) -> str:
    """진행 비용 가정마다 항목당 Claude 비용과 기준선 대비 비율을 보인다."""
    orch = spec.get("orchestration", {})
    cases = [(name, value) for name, value in orch.get("alternatives", {}).items()]
    cases.insert(1 if cases else 0, ("평균 규모(기본)", orch.get("per_item", 0.0)))
    base = next(r for r in rows if r["name"] == spec["baseline"])

    def total(row, value):
        return row["work_item"]["mean"] + (0.0 if row["group"] == "solo" else value)

    lines = ["| 구성 | " + " | ".join(name for name, _ in cases) + " |", "| --- |" + " ---: |" * len(cases)]
    for r in rows:
        cells = [f"${total(r, v):.2f} ({total(r, v) / total(base, v) * 100:.0f}%)" for _, v in cases]
        lines.append(f"| {r['name']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def nice_step(span: float, target: int) -> float:
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def axis_range(lo: float, hi: float, target: int, floor_zero: bool) -> tuple[float, float, float]:
    lo = 0.0 if floor_zero else lo
    if hi <= lo:
        hi = lo + 1
    step = nice_step(hi - lo, target)
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    if end - hi < step * 0.25:
        end += step
    if not floor_zero and lo - start < step * 0.25:
        start -= step
    return max(0.0, start), end, step


def marker(shape: str, x: float, y: float, r: float, fill: str, ring: str) -> str:
    attrs = f'fill="{fill}" stroke="{ring}" stroke-width="2" data-shape="{shape}"'
    if shape == "circle":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" {attrs}/>'
    if shape == "square":
        return f'<rect x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r}" height="{2 * r}" rx="1.5" {attrs}/>'
    k = r * 1.25
    points = {
        "triangle": [(0, -k), (k, k * 0.8), (-k, k * 0.8)],
        "triangle-down": [(0, k), (k, -k * 0.8), (-k, -k * 0.8)],
        "diamond": [(0, -k), (k, 0), (0, k), (-k, 0)],
        "hexagon": [(r * math.cos(a), r * math.sin(a)) for a in (math.pi / 3 * i for i in range(6))],
    }[shape]
    path = " ".join(f"{x + dx:.1f},{y + dy:.1f}" for dx, dy in points)
    return f'<polygon points="{path}" {attrs}/>'


def text(x, y, body, size, fill, weight=400, anchor="start") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}">{escape(body)}</text>')


def text_width(body: str, size: float) -> float:
    return sum(size * (0.92 if ord(ch) > 0x1100 else 0.56) for ch in body)


def overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def place_labels(marks: list[dict], bounds: tuple, obstacles: list[tuple]) -> list[tuple[float, float]]:
    """레이블(두 줄)을 채운 표시 오른쪽→위→아래→왼쪽 순으로, 막대·점선·다른 레이블과 겹치지 않는 자리에 둔다."""
    taken = [m["box"] for m in marks] + list(obstacles)
    placed = []
    for m in marks:
        w, h = m["width"], 32
        x, y, (bx0, by0, bx1, by1) = m["x"], m["y"], m["box"]
        candidates = []
        for dy in (0, -18, 18, -36, 36):
            candidates.append((bx1 + 8, y - 16 + dy))
        candidates += [(x - w / 2, by0 - 6 - h), (x - w / 2, by1 + 6)]
        for dy in (0, -18, 18):
            candidates.append((bx0 - 8 - w, y - 16 + dy))
        choice = candidates[0]
        for cx, cy in candidates:
            box = (cx, cy, cx + w, cy + h)
            inside = bounds[0] <= box[0] and box[2] <= bounds[2] and bounds[1] <= box[1] and box[3] <= bounds[3]
            if inside and not any(overlaps(box, t) for t in taken):
                choice = (cx, cy)
                break
        taken.append((choice[0], choice[1], choice[0] + w, choice[1] + h))
        placed.append(choice)
    return placed


def svg(rows: list[dict], theme: str) -> str:
    t = THEMES[theme]
    acc_lo = min(r["accuracy"]["min"] for r in rows) * 100
    acc_hi = max(r["accuracy"]["max"] for r in rows) * 100
    cost_hi = max(r["total_item"]["max"] for r in rows)
    y0, y1, ys = axis_range(acc_lo, acc_hi, 5, False)
    x0, x1, xs = axis_range(0, cost_hi, 6, True)
    left, right, top, bottom = PLOT["left"], PLOT["right"], PLOT["top"], PLOT["bottom"]
    px = (lambda v: left + (v - x0) / (x1 - x0) * (right - left))
    py = (lambda v: bottom - (v * 100 - y0) / (y1 - y0) * (bottom - top))
    desc = "; ".join(f"{r['name']}: 정확도 {pct(r['accuracy']['mean'])}%, 항목당 Claude 비용 ${r['total_item']['mean']:.2f}"
                     for r in rows)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'role="img" aria-labelledby="t d">',
        '<title id="t">정확도 대비 항목당 Claude 비용 — 단일 Claude와 cascade-fix 구성</title>',
        f'<desc id="d">{escape(desc)}</desc>',
        f"<style>text{{font-family:{FONT};font-variant-numeric:tabular-nums}}</style>",
        f'<rect x="1" y="1" width="{WIDTH - 2}" height="{HEIGHT - 2}" rx="20" fill="{t["surface"]}" stroke="{t["border"]}"/>',
        text(32, 46, "정확도 대비 항목당 Claude 비용", 20, t["ink"], 700),
        text(32, 72, "코드 지적 37건 소급 재생 · 채운 표시는 작업 + 진행(추정) · 막대는 회차 최소~최대 · 왼쪽 위일수록 싸고 정확함",
             13, t["ink2"]),
    ]
    tick = y0
    while tick <= y1 + 1e-9:
        y = bottom - (tick - y0) / (y1 - y0) * (bottom - top)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{t["grid"]}" stroke-width="1"/>')
        out.append(text(left - 10, y + 4, f"{number(tick)}%", 12, t["ink2"], anchor="end"))
        tick += ys
    tick = x0
    while tick <= x1 + 1e-9:
        x = px(tick)
        out.append(f'<line x1="{x:.1f}" y1="{bottom}" x2="{x:.1f}" y2="{bottom + 5}" stroke="{t["axis"]}" stroke-width="1"/>')
        out.append(text(x, bottom + 20, f"${tick:.2f}", 12, t["ink2"], anchor="middle"))
        tick += xs
    out.append(f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="{t["axis"]}" stroke-width="1"/>')
    out.append(text((left + right) / 2, bottom + 44, "항목당 Claude 비용(추정, 달러)", 13, t["ink2"], 600, "middle"))
    out.append(f'<text x="{24}" y="{(top + bottom) / 2:.1f}" font-size="13" font-weight="600" fill="{t["ink2"]}" '
               f'text-anchor="middle" transform="rotate(-90 24 {(top + bottom) / 2:.1f})">정확도(%, 맞힘 / 37)</text>')
    centers = [(px(r["total_item"]["mean"]), py(r["accuracy"]["mean"])) for r in rows]
    for r, (x, y) in zip(rows, centers):
        color = t["series"][r["group"]]
        if r["runs"] > 1:
            xa, xb = px(r["total_item"]["min"]), px(r["total_item"]["max"])
            ya, yb = py(r["accuracy"]["min"]), py(r["accuracy"]["max"])
            out.append(f'<path d="M{xa:.1f} {y:.1f}H{xb:.1f}M{xa:.1f} {y - 5:.1f}V{y + 5:.1f}M{xb:.1f} {y - 5:.1f}V{y + 5:.1f}'
                       f'M{x:.1f} {ya:.1f}V{yb:.1f}M{x - 5:.1f} {ya:.1f}H{x + 5:.1f}M{x - 5:.1f} {yb:.1f}H{x + 5:.1f}" '
                       f'fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round"/>')
    obstacles, used_y = [], []
    for r, (x, y) in zip(rows, centers):
        if r["orchestration_item"] > 0:
            wx, color = px(r["work_item"]["mean"]), t["series"][r["group"]]
            wy = y + 7 * sum(abs(y - u) < 3 for u in used_y)
            used_y.append(y)
            out.append(f'<path d="M{wx + 7:.1f} {wy:.1f}H{x - 8:.1f}{"" if wy == y else f"L{x - 8:.1f} {y:.1f}"}" fill="none" '
                       f'stroke="{color}" stroke-width="1.5" stroke-dasharray="4 4"/>')
            out.append('<g data-kind="work-only">' + marker(r["shape"], wx, wy, 5, t["surface"], color) + "</g>")
            obstacles.append((wx - 8, min(y, wy) - 8, x - 8, max(y, wy) + 8))
    marks = []
    for r, (x, y) in zip(rows, centers):
        detail = f"{pct(r['accuracy']['mean'])}% · ${r['total_item']['mean']:.2f}"
        many = r["runs"] > 1
        box = (min(x - 8, px(r["total_item"]["min"]) - 2) if many else x - 8,
               min(y - 8, py(r["accuracy"]["max"]) - 6) if many else y - 8,
               max(x + 8, px(r["total_item"]["max"]) + 2) if many else x + 8,
               max(y + 8, py(r["accuracy"]["min"]) + 6) if many else y + 8)
        marks.append({"x": x, "y": y, "box": box, "detail": detail,
                      "width": max(text_width(r["name"], 13), text_width(detail, 12))})
    for r, m, (lx, ly) in zip(rows, marks, place_labels(marks, (left + 4, top - 30, WIDTH - 16, bottom - 4), obstacles)):
        out.append(marker(r["shape"], m["x"], m["y"], 6, t["series"][r["group"]], t["surface"]))
        out.append(text(lx, ly + 12, r["name"], 13, t["ink"], 700))
        out.append(text(lx, ly + 28, m["detail"], 12, t["ink2"]))
    lx = 32
    for group, label in GROUPS.items():
        if not any(r["group"] == group for r in rows):
            continue
        out.append(f'<rect x="{lx}" y="{HEIGHT - 42}" width="14" height="14" rx="3" fill="{t["series"][group]}"/>')
        out.append(text(lx + 22, HEIGHT - 30, label, 13, t["ink2"]))
        lx += 22 + text_width(label, 13) + 28
    if any(r["orchestration_item"] > 0 for r in rows):
        out.append(marker("circle", lx + 7, HEIGHT - 35, 5, t["surface"], t["ink2"]))
        out.append(text(lx + 22, HEIGHT - 30, "빈 표시: 진행 비용 제외(작업 비용만)", 13, t["ink2"]))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="구성별 점수 JSON을 모아 요약 표(Markdown)와 산점도(SVG)를 만든다")
    parser.add_argument("--spec", required=True, help="구성·회차별 점수 파일과 진행 비용을 적은 JSON")
    parser.add_argument("--md")
    parser.add_argument("--svg-light")
    parser.add_argument("--svg-dark")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    spec = load_spec(args.spec)
    rows = summarize(spec)
    table = markdown(rows, spec) + ("\n" + sensitivity(rows, spec) if spec.get("orchestration", {}).get("alternatives") else "")
    print(table)
    for path, content in ((args.md, table), (args.svg_light, args.svg_light and svg(rows, "light")),
                          (args.svg_dark, args.svg_dark and svg(rows, "dark")),
                          (args.json, args.json and json.dumps(rows, ensure_ascii=False, indent=2) + "\n")):
        if path:
            Path(path).write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

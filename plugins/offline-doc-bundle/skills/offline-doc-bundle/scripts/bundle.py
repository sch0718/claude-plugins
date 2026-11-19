#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""폐쇄망 배포용 HTML 묶음을 다듬고 검증한다.

마크다운을 HTML로 변환한 뒤 남는 뒷정리를 담당한다. 변환 자체는 하지 않는다.

  link-fix  변환된 HTML의 .md 링크를 .html 로 바꾸고 각 문서에 목차 바를 넣는다
  index     분류된 목차 페이지(index.html)를 만든다
  exclude   배포하지 않을 문서를 지우고, 그 문서를 가리키던 링크를 텍스트로 낮춘다
  verify    깨진 링크·외부 리소스·누락된 이미지를 찾는다

표준 라이브러리만 쓴다.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse

# ─────────────────────────────────────────────────────────────────────────────
# 공통

def iter_html(root, skip_index=True):
    for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            if not n.endswith(".html"):
                continue
            if skip_index and n == "index.html" and dirpath == root:
                continue
            yield os.path.join(dirpath, n)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def is_external(href):
    return href.startswith(("http://", "https://", "mailto:", "tel:", "#", "data:"))


def resolve(dirpath, href):
    """href 를 파일 경로로 바꾼다. 앵커·쿼리는 떼고 퍼센트 인코딩을 푼다."""
    target = urllib.parse.unquote(href.split("#")[0].split("?")[0])
    if not target:
        return None
    return os.path.normpath(os.path.join(dirpath, target))


# ─────────────────────────────────────────────────────────────────────────────
# link-fix

NAV = '''<div id="odb-nav"><a href="{up}index.html">&larr; 목차</a>{crumb}</div>
<style>/* odb-nav */
#odb-nav{{position:sticky;top:0;z-index:99;display:flex;align-items:center;gap:12px;
 margin:-16px -16px 18px;padding:10px 18px;background:#f2f5f8;border-bottom:1px solid #dfe3e8;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif;font-size:13.5px}}
#odb-nav a{{color:#0b5cad;font-weight:700;text-decoration:none}}
#odb-nav a:hover{{text-decoration:underline}}
#odb-nav .crumb{{color:#5b6470}}
@media print{{#odb-nav{{display:none}}}}
</style>
'''


def cmd_link_fix(args):
    root = args.root
    files = fixed = 0
    for p in iter_html(root):
        s = read(p)
        orig = s
        dirpath = os.path.dirname(p)

        def sub(m):
            nonlocal fixed
            href = m.group(1)
            if is_external(href):
                return m.group(0)
            new = re.sub(r"\.md(?=$|[#?])", ".html", href)
            if new != href:
                fixed += 1
            return 'href="%s"' % new

        s = re.sub(r'href="([^"]+)"', sub, s)

        if not args.no_nav and 'id="odb-nav"' not in s:
            rel = os.path.relpath(dirpath, root)
            up = "" if rel == "." else "../" * len(rel.split(os.sep))
            crumb = "" if rel == "." else '<span class="crumb">%s</span>' % html.escape(
                rel.replace(os.sep, " / "))
            m = re.search(r"<body[^>]*>", s)
            if m:
                s = s[:m.end()] + "\n" + NAV.format(up=up, crumb=crumb) + s[m.end():]

        if s != orig:
            write(p, s)
        files += 1
    print(f"link-fix: {files}개 파일, .md→.html 링크 {fixed}개")


# ─────────────────────────────────────────────────────────────────────────────
# exclude

def cmd_exclude(args):
    root = args.root
    names = [os.path.basename(t) for t in args.targets]
    removed = []
    for t in args.targets:
        p = t if os.path.isabs(t) else os.path.join(root, t)
        if os.path.exists(p):
            os.remove(p)
            removed.append(os.path.relpath(p, root))

    if not names:
        print("exclude: 대상 없음")
        return

    stems = "|".join(re.escape(os.path.splitext(n)[0]) for n in names)
    pat = re.compile(
        r'<a\s+href="([^"]*(?:%s)\.html[^"]*)"[^>]*>(.*?)</a>' % stems, re.S)
    demoted = 0
    for p in iter_html(root, skip_index=False):
        s = read(p)

        def rep(m):
            nonlocal demoted
            demoted += 1
            return ('<span class="odb-off">%s <span class="odb-tag">%s</span></span>'
                    % (m.group(2), html.escape(args.label)))

        s2 = pat.sub(rep, s)
        if s2 != s:
            if "/* odb-off */" not in s2:
                s2 = s2.replace("</head>",
                                '<style>/* odb-off */\n'
                                '.odb-off{color:#5b6470}\n'
                                '.odb-tag{display:inline-block;padding:0 6px;font-size:.82em;'
                                'font-weight:700;color:#6b4f12;background:#fff6e5;'
                                'border:1px solid #f0d9a8;border-radius:99px;vertical-align:1px;'
                                'white-space:nowrap}\n</style></head>', 1)
            write(p, s2)
    print(f"exclude: {len(removed)}개 삭제, 참조 링크 {demoted}개를 '{args.label}' 로 낮춤")
    for r in removed:
        print("  -", r)


# ─────────────────────────────────────────────────────────────────────────────
# index

INDEX_CSS = """
:root{--bg:#f7f8fa;--fg:#1a1d21;--muted:#5b6470;--line:#dfe3e8;--card:#fff;
 --accent:#0b5cad;--star:#b32a2a;--starbg:#fdeaea}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);line-height:1.6;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR","Malgun Gothic",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 64px}
header{border-bottom:2px solid var(--line);padding-bottom:20px;margin-bottom:28px}
h1{font-size:26px;margin:0 0 8px;letter-spacing:-.3px}
.sub{color:var(--muted);font-size:14px;margin:0}
.lead{margin:14px 0 0;font-size:14px}
.lead a{color:var(--accent);font-weight:600}
.notice{margin:18px 0 0;padding:12px 14px;background:#fff6e5;border:1px solid #f0d9a8;
 border-radius:8px;font-size:13.5px;color:#6b4f12}
section{margin:34px 0 0}
h2{font-size:18px;margin:0 0 4px;padding-left:10px;border-left:4px solid var(--accent)}
.sd{color:var(--muted);font-size:13.5px;margin:0 0 14px;padding-left:14px}
.grid{list-style:none;margin:0;padding:0;display:grid;gap:10px;
 grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:14px 16px;text-decoration:none;color:inherit;height:100%;
 transition:border-color .12s,box-shadow .12s}
.card:hover,.card:focus{border-color:var(--accent);box-shadow:0 2px 10px rgba(11,92,173,.10);outline:none}
.card:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.t{display:block;font-weight:700;font-size:15px;margin-bottom:5px;color:var(--accent)}
.n{display:block;font-size:13px;color:var(--muted);line-height:1.5}
.star{display:inline-block;margin-left:7px;padding:1px 7px;font-size:11px;font-weight:700;
 color:var(--star);background:var(--starbg);border:1px solid #f2c9c9;border-radius:99px;
 vertical-align:2px}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
 color:var(--muted);font-size:12.5px}
@media (max-width:520px){.wrap{padding:22px 16px 48px}h1{font-size:21px}}
"""


def first_heading(path):
    """HTML 문서의 첫 <h1> 을 제목으로 쓴다. 없으면 파일명."""
    try:
        s = read(path)
    except OSError:
        return None
    m = re.search(r"<h1[^>]*>(.*?)</h1>", s, re.S)
    if m:
        t = re.sub(r"<[^>]+>", "", m.group(1))
        return html.unescape(t).strip()
    return os.path.splitext(os.path.basename(path))[0]


def auto_manifest(root):
    """manifest 가 없을 때 디렉터리 구조로 목차를 만든다."""
    sections = {}
    for p in iter_html(root):
        rel = os.path.relpath(p, root)
        sec = os.path.dirname(rel) or "문서"
        sections.setdefault(sec, []).append(
            {"path": rel.replace(os.sep, "/"), "title": first_heading(p), "note": ""})
    # _html 같은 기술적 폴더명을 제목으로 쓰면 읽는 사람에게 아무 정보가 없다.
    base = os.path.basename(os.path.abspath(root))
    if base.startswith("_") or base.lower() in ("html", "out", "dist", "build", "site"):
        base = os.path.basename(os.path.dirname(os.path.abspath(root)))
    return {"title": base or "문서 모음",
            "sections": [{"name": k, "desc": "", "items": v} for k, v in sorted(sections.items())]}


def cmd_index(args):
    root = args.root
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as f:
            man = json.load(f)
    else:
        man = auto_manifest(root)

    esc = lambda x: html.escape(str(x), quote=True)
    missing, blocks = [], []
    for sec in man.get("sections", []):
        cards = []
        for it in sec.get("items", []):
            path = it["path"]
            if not os.path.exists(os.path.join(root, path)):
                missing.append(path)
                continue
            title = it.get("title") or first_heading(os.path.join(root, path))
            badge = ('<span class="star">%s</span>' % esc(it["badge"])) if it.get("badge") else ""
            note = ('<span class="n">%s</span>' % esc(it["note"])) if it.get("note") else ""
            cards.append(
                '      <li><a class="card" href="%s">\n'
                '        <span class="t">%s%s</span>\n%s\n      </a></li>'
                % (esc(path), esc(title), badge, note))
        if cards:
            blocks.append(
                '  <section>\n    <h2>%s</h2>\n%s    <ul class="grid">\n%s\n    </ul>\n  </section>'
                % (esc(sec.get("name", "")),
                   ('    <p class="sd">%s</p>\n' % sec["desc"]) if sec.get("desc") else "",
                   "\n".join(cards)))

    lead = ('<p class="lead">%s</p>' % man["lead"]) if man.get("lead") else ""
    notice = ('<p class="notice">%s</p>' % man["notice"]) if man.get("notice") else ""
    doc = (
        '<!doctype html>\n<html lang="%s"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>%s</title>\n<style>%s</style></head>\n<body><div class="wrap">\n'
        '<header>\n  <h1>%s</h1>\n  <p class="sub">%s</p>\n  %s\n  %s\n</header>\n%s\n'
        '<footer>각 문서의 본문 링크로 서로 이동할 수 있습니다. '
        '문서 상단의 <strong>← 목차</strong>를 누르면 이 페이지로 돌아옵니다.</footer>\n'
        '</div></body></html>\n'
        % (man.get("lang", "ko"), esc(man.get("title", "문서 모음")), INDEX_CSS,
           esc(man.get("title", "문서 모음")), esc(man.get("subtitle", "")),
           lead, notice, "\n".join(blocks)))
    write(os.path.join(root, "index.html"), doc)
    print("index: index.html 생성 (섹션 %d개)" % len(blocks))
    if missing:
        print("  manifest 에 있으나 파일이 없어 건너뜀:", ", ".join(missing))


# ─────────────────────────────────────────────────────────────────────────────
# verify

def cmd_verify(args):
    root = args.root
    broken, ext, scripts, imgs = [], {}, [], []
    total = 0

    for p in iter_html(root, skip_index=False):
        s = read(p)
        rel = os.path.relpath(p, root)
        dirpath = os.path.dirname(p)

        for m in re.finditer(r'href="([^"]+)"', s):
            href = m.group(1)
            if is_external(href):
                if href.startswith(("http://", "https://")):
                    host = re.sub(r"(https?://[^/]+).*", r"\1", href)
                    ext.setdefault(host, 0)
                    ext[host] += 1
                continue
            total += 1
            t = resolve(dirpath, href)
            if t and not os.path.exists(t):
                broken.append((rel, href))

        # 페이지를 그리는 데 네트워크가 필요한 참조를 찾는다.
        for m in re.finditer(
                r'<(script|link|img|iframe|source|video|audio)\b[^>]*\b(?:src|href)="([^"]+)"',
                s, re.I):
            tag, url = m.group(1).lower(), m.group(2)
            if tag == "link" and "stylesheet" not in m.group(0).lower():
                continue
            if url.startswith(("http://", "https://", "//")):
                scripts.append((rel, tag, url))
            elif tag == "img" and not url.startswith("data:"):
                t = resolve(dirpath, url)
                if t and not os.path.exists(t):
                    imgs.append((rel, url))

        for m in re.finditer(r"<script\b", s, re.I):
            scripts.append((rel, "script", "(inline)"))

    ok = True
    # 같은 대상을 여러 문서가 가리키는 경우가 흔하므로 대상 기준으로 묶는다.
    by_target = {}
    for src, href in broken:
        by_target.setdefault(href, []).append(src)
    print(f"내부 링크 {total}개 검사 — 깨짐 {len(broken)}개 (대상 {len(by_target)}종)")
    for href, srcs in sorted(by_target.items(), key=lambda kv: -len(kv[1]))[:20]:
        where = srcs[0] if len(srcs) == 1 else f"{srcs[0]} 외 {len(srcs)-1}곳"
        print(f"    {href}  ← {where}")
    if len(by_target) > 20:
        print(f"    … 외 {len(by_target)-20}종")
    if broken:
        ok = False

    net = [x for x in scripts if x[2] != "(inline)"]
    print(f"외부 리소스 참조 {len(net)}개" + (" (폐쇄망에서 깨짐)" if net else ""))
    for x in net[:20]:
        print("   ", x[0], x[1], x[2]); ok = False

    inline = [x for x in scripts if x[2] == "(inline)"]
    if inline:
        print(f"<script> 태그 {len(inline)}개 — 오프라인 동작 여부 확인 필요")

    print(f"깨진 이미지 참조 {len(imgs)}개")
    for x in imgs[:20]:
        print("   ", x[0], "->", x[1]); ok = False

    if ext:
        top = sorted(ext.items(), key=lambda kv: -kv[1])[:8]
        print("본문 링크의 외부 도메인 (클릭 시에만 열림):")
        for host, n in top:
            print(f"    {n:4d}  {host}")
        print("    ※ http://www.w3.org 은 SVG 네임스페이스 선언이라 네트워크 요청이 아니다.")

    print("\n결과:", "이상 없음" if ok else "위 항목을 확인할 것")
    return 0 if ok else 1


# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("link-fix", help=".md 링크를 .html 로 바꾸고 목차 바를 넣는다")
    p.add_argument("root")
    p.add_argument("--no-nav", action="store_true", help="목차 바를 넣지 않는다")
    p.set_defaults(func=cmd_link_fix)

    p = sub.add_parser("index", help="목차 페이지를 만든다")
    p.add_argument("root")
    p.add_argument("--manifest", help="분류·설명을 담은 JSON (없으면 디렉터리 구조로 자동 생성)")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("exclude", help="배포 제외 문서를 지우고 링크를 텍스트로 낮춘다")
    p.add_argument("root")
    p.add_argument("targets", nargs="+", help="root 기준 상대 경로")
    p.add_argument("--label", default="미배포")
    p.set_defaults(func=cmd_exclude)

    p = sub.add_parser("verify", help="링크·외부 리소스·이미지를 검사한다")
    p.add_argument("root")
    p.set_defaults(func=cmd_verify)

    a = ap.parse_args()
    sys.exit(a.func(a) or 0)


if __name__ == "__main__":
    main()

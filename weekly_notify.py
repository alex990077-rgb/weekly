#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
科技爱好者周刊（ruanyf/weekly）新刊监听 + Server酱微信推送

做什么：
  1. 以「上游」为准（默认 ruanyf/weekly），解析根 README.md 的期号索引，
     找出发行日期最新的一期（形如 `- 第 412 期：[标题](docs/issue-412.md)`）；
  2. 和本地状态 latest.json 比对期号 + 文件 sha：
        - 没变      -> 直接退出，不推送（所以一天跑多次也不会重复）
        - 是新一期  -> 推送
        - 同号但 sha 变了 -> 「第 N 期已更新」补推一次（阮老师经常发完再改错别字）
  3. 通过 Server酱（sctapi.ftqq.com）推到微信；
  4. 把每期落到 archive/issue-NNN.md + archive/index.jsonl（历史可回看，仓库也有活动）；
  5. 写回 latest.json（只在推送成功后写，避免推送失败丢消息）。

用法：
  python weekly_notify.py                # 正常跑一次（有新刊就推）
  python weekly_notify.py --dry-run      # 只探测和打印，不推送、不写状态（Actions 里预演用）
  python weekly_notify.py --check        # 只打印最新期号，然后退出（不推送、不写任何文件）
  python weekly_notify.py --force        # 忽略状态，强制重推当前最新一期
  python weekly_notify.py --full         # 覆盖环境变量 WEEKLY_PUSH_MODE=full，推全文

环境变量：
  SERVERCHAN_SENDKEY   必填（除 --dry-run/--check 外）。Server酱 SendKey（sct... 或 sctp...）
  GITHUB_TOKEN         选填。读 GitHub API 用，缺省用匿名（12 次/小时足够），配了更稳
  WEEKLY_UPSTREAM      选填。默认 ruanyf/weekly
  WEEKLY_PUSH_MODE     选填。digest（默认，摘要）| full（全文 markdown）
  WEEKLY_TITLE_STYLE   选填。short（默认，卡片标题 = 第 N 期）| full（标题含副标题）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- 常量

UPSTREAM_DEFAULT = "ruanyf/weekly"
STATE_FILE = "latest.json"
ARCHIVE_DIR = "archive"
INDEX_FILE = os.path.join(ARCHIVE_DIR, "index.jsonl")

DIGEST_LIMIT = 6000      # 摘要模式正文上限（Server酱单条上限 32KB，6000 字足够看重点）
FULL_CHUNK = 12000       # 全文模式每条上限（约 3.6 万字节，留出 JSON 转义余量）
TITLE_LIMIT = 32         # Server酱 title 上限 32 字，超出会被拒
API = "https://api.github.com"
UA = "weekly-notify/1.0 (+https://github.com/alex990077-rgb/weekly)"

# 摘要模式里收录的小节（按顺序；不在列表里的小节只记标题）
DIGEST_SECTIONS = ("封面", "科技动态", "工具", "资源", "文摘", "言论")


# ---------------------------------------------------------------- 基础工具

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and url.startswith(API):
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json;charset=utf-8", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def content_of(path: str, ref: str, repo: str) -> tuple[str, str]:
    """取仓库里某个文件的文本内容和 blob sha。"""
    url = f"{API}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
    data = json.loads(http_get(url).decode("utf-8"))
    if isinstance(data, list):
        raise RuntimeError(f"{repo}/{path} 是目录，不是文件")
    raw = base64.b64decode(data["content"])
    return raw.decode("utf-8", errors="replace"), data["sha"]


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- 探测新刊

MD_LINK = re.compile(r"\[(?P<text>[^\]]+)\]\((?P<path>docs/issue-(?P<num>\d+)\.md)\)")

# README 的月份标题是中文数字：**九月** / **十一月**
CN_MONTHS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "10": 10, "11": 11, "12": 12,
}
MONTH_HEAD = re.compile(r"^\*\*(十[一二]|[一二三四五六七八九十]|\d{1,2})月\*\*$")


def parse_entries(readme: str) -> list[dict]:
    """从 README 索引里解析所有期号条目。

    README 是按年 → 月分组的嵌套列表，且**月内是最新在前**（九月里 412 在 411 前面），
    所以月和年是「向上继承」的：先记住最近看到的月份标题，再把它补给上面的条目。
    """
    lines = readme.splitlines()
    year_at: list[int | None] = [None] * len(lines)
    month_at: list[int | None] = [None] * len(lines)
    year = month = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        m_year = re.match(r"^##\s*(\d{4})\s*$", stripped)
        if m_year:
            year, month = int(m_year.group(1)), None
        else:
            m_month = MONTH_HEAD.match(stripped)
            if m_month:
                month = CN_MONTHS.get(m_month.group(1))
        year_at[i], month_at[i] = year, month

    entries: list[dict] = []
    for i, line in enumerate(lines):
        m = MD_LINK.search(line.strip())
        if not m:
            continue
        m_year = None
        for j in range(i, -1, -1):          # 向上找最近的年份标题
            if year_at[j] is not None:
                m_year = year_at[j]
                break
        m_month = None
        for j in range(i, -1, -1):          # 向上找最近的月份标题
            if month_at[j] is not None:
                m_month = month_at[j]
                break
            if year_at[j] is not None and j != i:
                break                       # 撞到年份标题还没月份 → 该年年初，放弃
        entries.append({
            "number": int(m.group("num")),
            "title": m.group("text").strip(),
            "path": m.group("path"),
            "year": m_year,
            "month": m_month,
        })
    return entries


def find_latest(readme: str) -> dict:
    entries = parse_entries(readme)
    if not entries:
        raise RuntimeError("README 里没解析到期号条目，上游格式可能变了")
    # 期号单调递增，直接按期号取最大；同号取出现的第一条
    best = max(entries, key=lambda e: e["number"])
    dated = [e for e in entries if e["year"] and e["month"]]
    dated.sort(key=lambda e: (e["year"], e["month"], e["number"]))
    best["month_hint"] = f"{dated[-1]['year']}-{dated[-1]['month']:02d}" if dated else ""
    best["total"] = len(entries)
    return best


def plain_text(text: str) -> str:
    """把一小段 markdown 变成纯文本（推送导语用，避免一堆链接语法）。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # 链接 -> 文字
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)            # 粗体
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)   # 斜体
    text = re.sub(r"`([^`]+)`", r"\1", text)                  # 行内代码
    return " ".join(text.split()).strip()


def first_paragraph(md: str) -> str:
    """取「封面」之后第一个正文小节的段落——那才是本期主题，而不是杂志固定介绍语。"""
    lines = md.splitlines()
    start = 0
    seen_cover = False
    for i, line in enumerate(lines):
        if re.match(r"^##\s*封面\s*$", line.strip()):
            seen_cover = True
            continue
        if seen_cover and re.match(r"^##\s+", line.strip()):
            start = i + 1
            break
    else:
        start = 1 if lines and lines[0].startswith("# ") else 0

    body = "\n".join(lines[start:])
    for block in re.split(r"\n\s*\n", body):
        raw = " ".join(l.strip() for l in block.strip().splitlines() if l.strip())
        if not raw or raw.startswith(("#", "!", ">", "-", "*", "|", "`", "（")):
            continue
        text = plain_text(raw)
        if len(text) < 8:
            continue
        return text
    return ""


def digest_sections(md: str) -> tuple[list[str], str]:
    """把一期 markdown 拆成 (各小节标题列表, 摘要正文)。"""
    headings: list[str] = []
    blocks: list[tuple[str, list[str]]] = []   # (小节名, 行)
    cur = ""
    for line in md.splitlines():
        h2 = re.match(r"^##\s+(.+?)\s*$", line)
        if h2:
            cur = h2.group(1).strip()
            headings.append(cur)
            blocks.append((cur, []))
            continue
        if blocks:
            blocks[-1][1].append(line)

    chosen = [b for b in blocks if b[0] in DIGEST_SECTIONS]
    kept = set(b[0] for b in chosen)
    out: list[str] = []
    for name, lines in chosen:
        body = "\n".join(lines).strip()
        if not body:
            continue
        out.append(f"## {name}\n\n{body}")
    if not out:                      # 兜底：格式变了就整篇截断
        return headings, md.strip()
    skipped = [h for h in headings if h not in kept]
    if skipped:
        out.append("## 本期其它小节\n\n" + "、".join(skipped))
    return headings, "\n\n".join(out).strip()


def issue_url(number: int) -> str:
    return f"https://github.com/ruanyf/weekly/blob/master/docs/issue-{number}.md"


def issue_web_url(number: int) -> str:
    return f"https://www.ruanyifeng.com/blog/{datetime.now().year}/09/weekly-issue-{number}.html"


def short_title(entry: dict) -> str:
    return f"第 {entry['number']} 期 {entry['title']}"


def clipped_title(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= TITLE_LIMIT:
        return text
    return text[: TITLE_LIMIT - 1] + "…"


# ---------------------------------------------------------------- 推送

def split_markdown(md: str, limit: int) -> list[str]:
    """按段落切成不超过 limit 字的几块（用于全文模式绕开 32KB 上限）。"""
    if len(md) <= limit:
        return [md]
    parts, buf = [], ""
    for block in re.split(r"\n\s*\n", md):
        if len(buf) + len(block) + 2 > limit and buf:
            parts.append(buf.rstrip())
            buf = ""
        if len(block) > limit:                      # 单块超长（极少），硬切
            for i in range(0, len(block), limit):
                piece = block[i:i + limit]
                if len(buf) + len(piece) + 2 > limit and buf:
                    parts.append(buf.rstrip())
                    buf = ""
                buf += piece + "\n\n"
            continue
        buf += block + "\n\n"
    if buf.strip():
        parts.append(buf.rstrip())
    return parts or [md[:limit]]


def serverchan_send(sendkey: str, title: str, desp: str, short: str = "") -> dict:
    key = sendkey.strip()
    url = (f"https://{key}.push.ft07.com/send"
           if key.lower().startswith("sctp")
           else f"https://sctapi.ftqq.com/{key}.send")
    payload = {"title": title, "desp": desp}
    if short:
        payload["short"] = short[:100]
    resp = http_post_json(url, payload)
    code = resp.get("code")
    if code not in (0, "0"):
        raise RuntimeError(f"Server酱返回 code={code} message={resp.get('message')}")
    return resp


def push_issue(sendkey: str, entry: dict, md: str, mode: str, style: str) -> list[dict]:
    """推送一期，返回每次调用的 Server酱响应。"""
    number, title = entry["number"], entry["title"]
    headings, body = digest_sections(md)
    lead = first_paragraph(md)
    card_title = short_title(entry)
    head_bits = [f"# 科技爱好者周刊 · 第 {number} 期", "", f"**{title}**", ""]
    if lead:
        head_bits += [f"> {lead}", ""]
    head_bits += [f"- 原文：<{issue_url(number)}>"]
    if headings:
        head_bits += [f"- 本期小节：{'、'.join(headings)}"]
    head_bits += ["", "---", ""]
    head = "\n".join(head_bits)

    if mode == "full":
        chunks = split_markdown(md, FULL_CHUNK)
        parts = [head + c for c in chunks]
    else:
        d = body
        if len(d) > DIGEST_LIMIT:
            d = d[:DIGEST_LIMIT].rstrip() + "\n\n……（摘要到此为止，全文见上方原文链接）"
        parts = [head + d]

    # 卡片标题：短标题（微信通知栏好看），正文第一条仍带完整副标题
    results = []
    total = len(parts)
    for i, part in enumerate(parts, 1):
        t = title_of(card_title, i, total, heading_style=style, entry=entry)
        log(f"  推送 {i}/{total}：title={t!r} desp={len(part)} 字")
        resp = serverchan_send(sendkey, t, part, short=lead[:100])
        msg = resp.get("message") or resp.get("data") or "ok"
        log(f"    Server酱受理：code={resp.get('code')} {str(msg)[:120]}")
        results.append(resp)
        if i < total:
            time.sleep(2)                     # 别把 Server酱 打急眼
    return results


def title_of(card_title: str, idx: int, total: int, heading_style: str, entry: dict) -> str:
    base = card_title if heading_style == "full" else f"第 {entry['number']} 期"
    if total > 1:
        return clipped_title(f"{base}（{idx}/{total}）")
    return clipped_title(base)


# ---------------------------------------------------------------- 存档

def save_archive(entry: dict, md: str, mode: str) -> str:
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, f"issue-{entry['number']}.md")
    if not os.path.exists(path):
        header = (
            f"<!-- 自动存档：来自 {UPSTREAM_DEFAULT} 的 docs/issue-{entry['number']}.md -->\n"
            f"<!-- 期号索引：{entry.get('year')} 年 {entry.get('month')} 月 -->\n\n"
        )
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header + md)

    row = {
        "number": entry["number"],
        "title": entry["title"],
        "url": issue_url(entry["number"]),
        "year": entry.get("year"),
        "month": entry.get("month"),
        "sha": entry.get("sha"),
        "chars": len(md),
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    rows = []
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    rows = [r for r in rows if r.get("number") != row["number"]]
    rows.append(row)
    rows.sort(key=lambda r: r.get("number") or 0)
    with open(INDEX_FILE, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    write_index_md(rows)
    return path


def write_index_md(rows: list[dict]) -> None:
    lines = [
        "# 科技爱好者周刊 · 本地存档",
        "",
        "> 本目录由 `.github/workflows/weekly-notify.yml` 自动维护：每次推送到微信时，",
        "> 把当期原文落到 `issue-NNN.md`，并刷新本索引。",
        "",
        f"> 共 {len(rows)} 期；最后更新：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    for r in sorted(rows, key=lambda r: r.get("number") or 0, reverse=True):
        lines.append(f"- 第 {r['number']} 期：[{r['title']}](issue-{r['number']}.md) · [原文]({r['url']})")
    with open(os.path.join(ARCHIVE_DIR, "index.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- 主流程

def resolve_entry(upstream: str) -> tuple[dict, str]:
    readme, _ = content_of("README.md", "master", upstream)
    entry = find_latest(readme)
    md, sha = content_of(entry["path"], "master", upstream)
    entry["sha"] = sha
    return entry, md


def decide(state: dict, entry: dict) -> tuple[bool, str]:
    last = state.get("last_number")
    if not last:
        return True, "first-run"
    if entry["number"] > last:
        return True, f"new-issue(+{entry['number'] - last})"
    if entry["number"] == last and state.get("last_sha") != entry["sha"]:
        return True, "same-issue-updated"
    return False, "up-to-date"


def main() -> int:
    ap = argparse.ArgumentParser(description="科技爱好者周刊新刊 → Server酱微信推送")
    ap.add_argument("--dry-run", action="store_true", help="只探测并打印，不推送、不写状态")
    ap.add_argument("--check", action="store_true", help="只打印最新期号后退出")
    ap.add_argument("--force", action="store_true", help="忽略状态，强制重推最新一期")
    ap.add_argument("--full", action="store_true", help="推送全文（覆盖 WEEKLY_PUSH_MODE）")
    ap.add_argument("--offline-file", metavar="PATH",
                    help="离线测试：把本地 markdown 当作最新一期，不访问 GitHub（仅配合 --dry-run 时安全）")
    ap.add_argument("--offline-number", type=int, default=9999,
                    help="配合 --offline-file 的期号（默认 9999）")
    args = ap.parse_args()
    if args.offline_file and not args.dry_run:
        ap.error("--offline-file 只用于离线自测，必须同时加 --dry-run（避免把假刊推到微信）")

    upstream = os.environ.get("WEEKLY_UPSTREAM", UPSTREAM_DEFAULT).strip() or UPSTREAM_DEFAULT
    mode = "full" if args.full else os.environ.get("WEEKLY_PUSH_MODE", "digest").strip().lower()
    if mode not in ("digest", "full"):
        mode = "digest"
    style = os.environ.get("WEEKLY_TITLE_STYLE", "short").strip().lower()

    if args.offline_file:
        with open(args.offline_file, "r", encoding="utf-8") as fh:
            md = fh.read()
        entry = {
            "number": args.offline_number,
            "title": "离线测试用假刊",
            "path": f"docs/issue-{args.offline_number}.md",
            "year": datetime.now().year,
            "month": datetime.now().month,
            "sha": "0" * 40,
            "total": 0,
            "month_hint": datetime.now().strftime("%Y-%m"),
        }
        log(f"离线模式：{args.offline_file}（按第 {entry['number']} 期处理）")
    else:
        log(f"上游：{upstream}")
        entry, md = resolve_entry(upstream)
    log(f"最新一期：第 {entry['number']} 期《{entry['title']}》"
        f"（{entry.get('year')} 年 {entry.get('month')} 月，sha {str(entry['sha'])[:10]}，{len(md)} 字）")

    if args.check:
        print(json.dumps({k: v for k, v in entry.items()}, ensure_ascii=False))
        return 0

    state = read_json(STATE_FILE, {})
    should, reason = decide(state, entry)
    log(f"状态判定：{reason}（last={state.get('last_number')} / now={entry['number']}）")

    if not should and not args.force:
        log("已是最新，本次不推送。")
        return 0

    headings, _ = digest_sections(md)
    lead = first_paragraph(md)
    print("-" * 68)
    print(f"准备推送：第 {entry['number']} 期《{entry['title']}》")
    print(f"原文：{issue_url(entry['number'])}")
    print(f"小节：{'、'.join(headings)}")
    print(f"导语：{lead[:200]}")
    print(f"摘要正文：{len(digest_sections(md)[1])} 字 | 模式：{mode} | 标题样式：{style}")
    print("-" * 68)

    if args.dry_run:
        if args.offline_file and os.environ.get("WEEKLY_OFFLINE_ARCHIVE") == "1":
            path = save_archive(entry, md, mode)
            write_json(STATE_FILE, {
                "last_number": entry["number"],
                "last_title": entry["title"],
                "last_sha": entry["sha"],
                "last_path": entry["path"],
                "pushed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "upstream": upstream,
                "reason": "offline-test",
            })
            log(f"离线自测：已写 {path} / {INDEX_FILE} / {STATE_FILE}（未推送）")
        else:
            log("--dry-run：不推送、不写状态。")
        return 0

    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        log("错误：没读到 SERVERCHAN_SENDKEY，无法推送。")
        print("::error title=缺少推送密钥::没有读到仓库 Secret SERVERCHAN_SENDKEY。"
              "请到 Settings → Secrets and variables → Actions 添加，Name 必须是 SERVERCHAN_SENDKEY。")
        return 1

    log(f"Server酱 SendKey 已读到（{len(sendkey)} 位，{sendkey[:4]}…），开始推送……")
    try:
        push_issue(sendkey, entry, md, mode, style)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, TimeoutError) as exc:
        log(f"推送失败：{exc}")
        print(f"::error title=推送失败::{exc}")
        return 1

    log("推送成功，写入存档……")
    path = save_archive(entry, md, mode)
    log(f"存档：{path} 与 {INDEX_FILE}")

    write_json(STATE_FILE, {
        "last_number": entry["number"],
        "last_title": entry["title"],
        "last_sha": entry["sha"],
        "last_path": entry["path"],
        "pushed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "upstream": upstream,
        "reason": reason,
    })
    log("状态已写回 latest.json，本次完成。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(f"::error title=HTTP {exc.code}::{exc.reason}（{exc.url}）")
        sys.exit(1)
    except Exception as exc:                                    # noqa: BLE001
        print(f"::error title=运行异常::{type(exc).__name__}: {exc}")
        sys.exit(1)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
标准条款原文检索 —— 防幻觉闸门

用途：按条款号从技能内置的「标准原文」中取出该条款的原文，供判定、
      比对、报告、整改验证时逐字引用。

铁律：任何标准条款的引用都必须来自本脚本的输出，不得凭记忆复述标准内容。
      条款号不存在时必须报错，不得猜测、不得用相近条款顶替。

零配置：自动扫描 references/01-标准原文/ 下的全部 .md 文件，
        并按同目录 _sources.json（如有）读取来源与可靠性说明。

用法：
    python scripts/clause_lookup.py --clause 7.1.5        取条款原文
    python scripts/clause_lookup.py --clause 8.5.1.1      IATF 补充条款
    python scripts/clause_lookup.py --clause 7.1.9        条款不存在：报错并给相近条款
    python scripts/clause_lookup.py --list                列出全部条款号
    python scripts/clause_lookup.py --sources             列出内置标准清单
    python scripts/clause_lookup.py --clause 7.1.5 --json JSON 输出
    python scripts/clause_lookup.py --clause 7.1.5 --full 不截断

退出码：
    0 = 正常（找到条款 / --list / --sources）
    1 = 参数错误，或条款在内置原文中不存在（防幻觉闸门触发）
    2 = 环境错误（未找到任何内置标准原文）
"""

import argparse
import json
import os
import re
import sys

REF_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "references")


def detect_ref_dir():
    """定位标准原文目录。

    约定优先 `references/01-标准原文/`；若不存在（如 iso17025-audit 把 CNAS
    文件平铺在 references/ 下、gjb-audit-assistant 用 01-核心标准/… 分册），
    则退回 `references/` 本身递归扫描——不能因为目录布局不同就让闸门失效。
    """
    preferred = os.path.join(REF_ROOT, "01-标准原文")
    return preferred if os.path.isdir(preferred) else REF_ROOT


REF_DIR = detect_ref_dir()
SOURCES_JSON = os.path.join(REF_DIR, "_sources.json")

# 条款标题行：## 4 组织环境 / ### 4.1 理解组织及其环境 / ### A.2 产品和服务
HEAD_RE = re.compile(r"^(#{2,6})\s+((?:[A-Z]{1,2}\.)?\d{1,2}(?:\.\d{1,2}){0,3})(?:[\s　]+|$)(.*)$")
# 「看起来像标题」的行：Markdown 标题，或行首直接是条款号（目次原貌 `9.1 监视…`）。
# 用于判定一个条款的 body 是否只是目次续行，而非真实条款正文。
HEADISH_RE = re.compile(r"^(?:#{2,6}\s|\d{1,2}(?:\.\d{1,2}){0,3}[.、\s　])")
PAGE_RE = re.compile(r"<!--\s*\[P(\d+)\]\s*-->")

# —— FAQ / SI 文件的专用处理 ——
# IATF 官方 FAQ / SI 的条目编号是 `## FAQ 9 — 8.4.2.2 法律法规要求…`、
# `## SI 3 — 修订 6.1.2.3 应急计划`：编号在「FAQ/SI n」上，条款号在标题文字里。
# 用通用 HEAD_RE 抓不到，导致 FAQ/SI 正式条目全部检索不到（实测仅 2/11 条）。
# 更糟的是这类文件里「变更原因」用的编号列表（## 1 …、## 2 …）会被当成条款 1/2/3，
# 与 GB/T 19001 的「1 范围」「2 规范性引用文件」撞号——查 `1` 会返回 SI 的变更说明
# 冒充标准条款，这是最强的幻觉入口，必须挡掉。
QA_HEAD_RE = re.compile(
    r"^(#{2,6})\s+((?:FAQ|SI)\s*\d+)\s*(?:（[^）]*）)?\s*[—–\-]?\s*(.*)$", re.I)
# 从 FAQ/SI 标题里抽取被引用的条款号，作为别名登记（如 `FAQ 9 — 8.4.2.2 … 8.6.5 …`）
# 前后不允许再接数字或点，否则会把「2021 年 4 月」误抽成 21.4
QA_CLAUSE_ALIAS = re.compile(r"(?<![\d.])([0-9]{1,2}(?:\.[0-9]{1,2}){1,3})(?![\d.])")

DEFAULT_MAX_LINES = 40
DEFAULT_MAX_CHARS = 2000


def load_meta():
    """读取标准来源清单；缺失时按文件名推断"""
    meta = {}
    if os.path.exists(SOURCES_JSON):
        try:
            for s in json.load(open(SOURCES_JSON, encoding="utf-8")):
                meta[s.get("file", "")] = s
        except Exception:
            pass
    return meta


def guess_label(path, text):
    """从 H1 标题或文件名推断标准显示名"""
    for line in text.splitlines()[:12]:
        if line.startswith("# "):
            return line[2:].strip()
    return os.path.splitext(os.path.basename(path))[0]


def is_ocr(text):
    """从文件头说明中判断是否 OCR 件。

    坑：文件头常写「PDF 文本层直提，无 OCR」这类否定说明，
    裸匹配 /OCR/ 会把「无 OCR」也判成 OCR 件（实测误报 4 份）。
    故先判否定式，再判肯定式。
    """
    head = "\n".join(text.splitlines()[:12])
    if re.search(r"无\s*OCR|未经\s*OCR|非\s*OCR", head, re.I):
        return False
    return bool(re.search(r"RapidOCR|OCR\s*(识别|提取|还原)|扫描件|光学字符识别", head))


def load_file(path, meta):
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()
    fname = os.path.basename(path)
    m = meta.get(fname, {})
    label = m.get("name") or guess_label(path, text)
    ocr = m.get("ocr")
    if ocr is None:
        ocr = is_ocr(text)
    reliability = m.get("reliability") or (
        "扫描件 OCR 识别，个别字符可能有误差；作为判定依据时须按页码回原 PDF 核对"
        if ocr else "PDF 文本层直提，无 OCR，可逐字引用")

    heads = collect_heads(lines)

    entries = []
    for k, h in enumerate(heads):
        end = len(lines)
        for nxt in heads[k + 1:]:
            if nxt["level"] <= h["level"]:
                end = nxt["line"]
                break
        pages, body = [], []
        for raw in lines[h["line"] + 1:end]:
            pm = PAGE_RE.search(raw)
            if pm:
                pages.append(int(pm.group(1)))
                raw = PAGE_RE.sub("", raw)
            if raw.strip():
                body.append(raw.rstrip())
        # 「真实正文」判定：目次残留的 body 往往就是下一个目次行
        # （如查 8.2 拿到 "## 9绩效评价 11"），这类 body 不是条款正文。
        # 只有存在非标题行、非页码标记的行，才算真有正文。
        real = [x for x in body
                if not HEADISH_RE.match(x.strip())
                and not PAGE_RE.fullmatch(x.strip())]
        entries.append({
            "source": fname,
            "source_label": label,
            "reliability": reliability,
            "ocr": ocr,
            "num": h["num"],
            "title": h["title"],
            "body": body,
            "real_body": bool(real),
            "pages": sorted(set(pages)),
        })

    entries.extend(build_qa_aliases(entries, fname, label, reliability, ocr))
    return {"file": fname, "label": label, "ocr": ocr,
            "reliability": reliability, "entries": entries}


def collect_heads(lines):
    """提取条款标题行。

    FAQ / SI 文件走专用分支：只认 `## FAQ n` / `## SI n`，
    其余（尤其是「变更原因」用的 ## 1 / ## 2 编号列表）一律不登记，
    否则会与真实条款号撞号，查 `1` 会返回 SI 变更说明冒充标准条款。
    """
    qa = []
    for i, line in enumerate(lines):
        qm = QA_HEAD_RE.match(line)
        if qm:
            num = re.sub(r"\s+", " ", qm.group(2).strip()).upper()
            qa.append({"level": len(qm.group(1)), "num": num,
                       "title": (qm.group(3) or "").strip(), "line": i})
    if qa:
        return qa

    heads = []
    for i, line in enumerate(lines):
        hm = HEAD_RE.match(line)
        if not hm:
            continue
        num = hm.group(2).rstrip(".").strip()
        if not num:
            continue
        heads.append({"level": len(hm.group(1)), "num": num,
                      "title": (hm.group(3) or "").strip(), "line": i})
    return heads


def build_qa_aliases(entries, fname, label, reliability, ocr):
    """把 FAQ / SI 标题里提到的条款号登记为别名条目。

    例：`## FAQ 9 — 8.4.2.2 法律法规要求 以及 8.6.5 法律法规的符合性`
    → 另建 num=8.4.2.2 与 num=8.6.5 两条别名，查这两个条款号时能连带看到官方解释。
    别名标题明确标注来自 FAQ/SI，避免与标准正文混淆。
    """
    out = []
    for e in entries:
        if not re.match(r"^(FAQ|SI)\s", e["num"], re.I):
            continue
        tag = e["num"].upper()
        for cnum in dict.fromkeys(QA_CLAUSE_ALIAS.findall(e["title"])):
            if cnum == e["num"]:
                continue
            # 标题不要再重复条款号：render_text 会以「【标题】num + title」输出，
            # 拼上 cnum 就成了 "8.4.2.2 8.4.2.2 的官方常见问题：…"
            out.append(dict(e, num=cnum,
                            title="【%s】%s" % (tag, e["title"]),
                            alias_of=tag))
    return out


SKIP_NAMES = {"readme.md", "index.md"}


def iter_md():
    """遍历标准原文 md。约定目录只扫一层；退回 references/ 时须递归。

    README.md / index.md 是目录索引不是标准原文，混入会污染条款表
    （实测 iso17025-audit 的 index.md 贡献 16 条假条款）。
    """
    if os.path.basename(REF_DIR) == "01-标准原文":
        for f in sorted(os.listdir(REF_DIR)):
            if f.endswith(".md") and not f.startswith("_") and f.lower() not in SKIP_NAMES:
                yield os.path.join(REF_DIR, f)
        return
    for dp, dns, fns in os.walk(REF_DIR):
        dns[:] = [d for d in dns if not d.startswith("_")]
        for f in sorted(fns):
            if f.endswith(".md") and not f.startswith("_") and f.lower() not in SKIP_NAMES:
                yield os.path.join(dp, f)


def build_index():
    if not os.path.isdir(REF_DIR):
        return []
    meta = load_meta()
    index = []
    for p in iter_md():
        try:
            index.extend(load_file(p, meta)["entries"])
        except Exception as e:
            sys.stderr.write("警告：解析 %s 失败（%s）\n" % (p, e))
    return purge_toc(index)


def purge_toc(index):
    """丢弃「目次副本」条目。

    同一个条款号可能既有目次条目（body 是下一个目次行，无真实正文），
    又有正文条目。此时只保留有真实正文的条目——否则查 `8.2` 会返回
    "## 9 绩效评价 11" 这类错误内容，比查不到更容易致幻。
    若某条款号只有目次条目（原文确实残缺），则原样保留，交给上层如实提示。
    """
    real_nums = {e["num"] for e in index if e.get("real_body")}
    return [e for e in index
            if e.get("real_body") or e["num"] not in real_nums]


def find_exact(index, num):
    return [e for e in index if e["num"] == num]


def find_related(index, num):
    """条款不存在时给出子/同级/父条款，便于纠正条款号"""
    parts = num.split(".")
    prefix = ".".join(parts[:-1])
    children = sorted({e["num"] for e in index if e["num"].startswith(num + ".")})
    parent, siblings = None, []
    if len(parts) > 1:
        if any(e["num"] == prefix for e in index):
            parent = prefix
        siblings = sorted({e["num"] for e in index
                           if e["num"].startswith(prefix + ".")
                           and len(e["num"].split(".")) == len(parts)})
    return {"children": children, "siblings": siblings, "parent": parent}


def truncate(body, full):
    if full:
        return body, False
    out, total = [], 0
    for line in body:
        if len(out) >= DEFAULT_MAX_LINES or total + len(line) > DEFAULT_MAX_CHARS:
            return out, True
        out.append(line)
        total += len(line)
    return out, False


def page_note(e):
    if not e["pages"]:
        return ""
    lo, hi = min(e["pages"]), max(e["pages"])
    return "第 %d 页" % lo if lo == hi else "第 %d–%d 页" % (lo, hi)


def render_text(num, hits, related, full):
    bar = "=" * 74
    w = sys.stdout
    if hits:
        w.write("%s\n条款 %s 原文（命中 %d 处）\n%s\n" % (bar, num, len(hits), bar))
        for e in hits:
            w.write("\n【来源】%s\n" % e["source_label"])
            w.write("【可靠性】%s\n" % e["reliability"])
            if e["title"]:
                w.write("【标题】%s %s\n" % (e["num"], e["title"]))
            if page_note(e):
                w.write("【位置】%s\n" % page_note(e))
            body, cut = truncate(e["body"], full)
            w.write("-" * 74 + "\n")
            w.write("\n".join(body) + "\n" if body else "（该条款无正文，仅有标题）\n")
            if cut:
                w.write("…（已截断，加 --full 查看完整原文）\n")
        if any(e["ocr"] for e in hits):
            w.write("\n提示：命中含 OCR 来源，作为判定依据时须按页码回原 PDF 核对。\n")
        return

    w.write("%s\n条款 %s —— 在内置标准原文中不存在\n%s\n" % (bar, num, bar))
    w.write("\n请核对条款号，或确认该条款所属标准是否已放入 references/01-标准原文/。\n")
    w.write("相近条款：\n")
    r = related
    if r["parent"]:
        w.write("  父条款：%s\n" % r["parent"])
    if r["siblings"]:
        w.write("  同级条款：%s\n" % "、".join(r["siblings"][:20]))
    if r["children"]:
        w.write("  下级条款：%s\n" % "、".join(r["children"][:20]))
    if not (r["parent"] or r["siblings"] or r["children"]):
        w.write("  （无相近条款）\n")
    w.write("\n不得凭记忆推断该条款内容。请确认条款号后重新检索。\n")


def main():
    ap = argparse.ArgumentParser(description="标准条款原文检索（防幻觉闸门）")
    ap.add_argument("--clause", help="条款号，如 7.1.5")
    ap.add_argument("--list", action="store_true", help="列出全部条款号")
    ap.add_argument("--sources", action="store_true", help="列出内置标准清单")
    ap.add_argument("--json", action="store_true", help="JSON 格式输出")
    ap.add_argument("--full", action="store_true", help="不截断，输出完整原文")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    index = build_index()
    if not index:
        sys.stderr.write("错误：未在 %s 下找到任何标准原文文件\n" % REF_DIR)
        sys.exit(2)

    if args.sources:
        seen = {}
        for e in index:
            seen.setdefault(e["source"], {"label": e["source_label"], "ocr": e["ocr"],
                                          "reliability": e["reliability"], "n": 0})
            seen[e["source"]]["n"] += 1
        for f, v in seen.items():
            print("\n【%s】%d 条条款" % (v["label"], v["n"]))
            print("  文件：%s" % f)
            print("  可靠性：%s" % v["reliability"])
        return

    if args.list:
        if args.json:
            print(json.dumps([{"num": e["num"], "title": e["title"], "source": e["source_label"]}
                              for e in index], ensure_ascii=False, indent=2))
        else:
            by = {}
            for e in index:
                by.setdefault(e["source_label"], []).append(e)
            for label, items in by.items():
                print("\n【%s】共 %d 条" % (label, len(items)))
                print("-" * 74)
                for e in items:
                    print("  %-10s %s" % (e["num"], e["title"]))
        return

    if not args.clause:
        ap.error("需提供 --clause、--list 或 --sources")

    num = args.clause.strip().rstrip(".").strip()
    hits = find_exact(index, num)
    related = find_related(index, num)

    if args.json:
        print(json.dumps({
            "clause": num,
            "exists": bool(hits),
            "hits": [{"source": e["source"], "source_label": e["source_label"],
                      "ocr": e["ocr"], "reliability": e["reliability"],
                      "num": e["num"], "title": e["title"],
                      "pages": e["pages"], "body": e["body"]} for e in hits],
            "related": related if not hits else {},
        }, ensure_ascii=False, indent=2))
    else:
        render_text(num, hits, related, args.full)

    sys.exit(0 if hits else 1)


if __name__ == "__main__":
    main()

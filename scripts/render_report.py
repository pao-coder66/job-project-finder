#!/usr/bin/env python3
"""把报告 JSON 注入 HTML 模板，产出单文件报告。

用法:
  python3 render_report.py data.json report.html
  python3 render_report.py data.json report.html --template /path/to/report_template.html

模板默认取同级 assets/report_template.html。注入前先校验 JSON 能解析、
必需字段在位，避免产出一个打开就报错的页面。
"""

import argparse
import json
import os
import re
import sys

PLACEHOLDER = "__REPORT_DATA__"
DIMS = ("ai_core", "novelty", "timeliness", "onboarding", "stars", "business")


def default_template():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "assets", "report_template.html")


def weighted_total(c, weights):
    if isinstance(c.get("total"), (int, float)):
        return c["total"]
    sc = c.get("scores") or {}
    s = w = 0.0
    for d in DIMS:
        v = sc.get(d)
        if isinstance(v, (int, float)):
            s += v * weights.get(d, 0)
            w += weights.get(d, 0)
    return round(s / w, 2) if w else None


def validate(data):
    problems = []
    weights = data.get("weights") or {
        "ai_core": 35, "novelty": 10, "timeliness": 15,
        "onboarding": 15, "stars": 10, "business": 15,
    }
    if not isinstance(data.get("candidates"), list):
        problems.append("candidates 必须是数组")
    for i, c in enumerate(data.get("candidates") or []):
        tag = c.get("repo") or f"#{i}"
        if not c.get("repo"):
            problems.append(f"{tag} 缺 repo")
        if not c.get("url"):
            problems.append(f"{tag} 缺 url，报告里的链接会点不开")
        sc = c.get("scores") or {}
        missing = [d for d in DIMS if not isinstance(sc.get(d), (int, float))]
        if missing:
            problems.append(f"{tag} scores 缺维度 {','.join(missing)}")
        ev = c.get("evidence") or {}
        noev = [d for d in DIMS if not ev.get(d)]
        if noev:
            problems.append(f"{tag} evidence 缺依据 {','.join(noev)}，打分不能只给数字")
        if c.get("track") not in ("runnable", "source-reading"):
            problems.append(f"{tag} track 应为 runnable 或 source-reading")
        pf = c.get("platform", "github")
        if pf not in ("github", "gitee"):
            problems.append(f"{tag} platform 应为 github 或 gitee")
        form = c.get("product_form", "product")
        if form not in ("product", "library", "infra", "unknown"):
            problems.append(f"{tag} product_form 应为 product/library/infra/unknown")
        if form in ("library", "infra"):
            problems.append(
                f"{tag} 是 {form} 不是产品型项目，不该留在候选里。"
                "用 --exclude 移进淘汰区，求职者要的是代表某类产品的 Agent 项目")
        url = c.get("url") or ""
        if url.endswith(".git"):
            problems.append(f"{tag} url 带 .git 后缀，Gitee 的 html_url 原样搬过来会点不开，去掉它")
        if pf == "gitee" and "gitee.com" not in url:
            problems.append(f"{tag} 标了 gitee 但 url 不指向 gitee.com")
        if pf == "github" and "github.com" not in url:
            problems.append(f"{tag} 标了 github 但 url 不指向 github.com")
        if not c.get("summary"):
            problems.append(f"{tag} 缺 summary，卡片要先一句话说清这是什么项目")
        if not c.get("pitch"):
            problems.append(f"{tag} 缺 pitch，立项动机是这个报告的核心")
        ct = c.get("core_tech") or []
        if len(ct) < 3:
            problems.append(f"{tag} core_tech 只有 {len(ct)} 格，核心技术方格至少要 3 格")
        for j, t in enumerate(ct):
            if not t.get("name") or not t.get("desc"):
                problems.append(f"{tag} core_tech 第 {j+1} 格缺 name 或 desc")
        # AI 占比 1 分的仓库必须诚实标注 AI 核心缺失，不能拿业务技术点冒充。
        # 2 分的仓库仍有真实但次要的 AI 能力，不强制标注缺失。
        ai = sc.get("ai_core")
        if isinstance(ai, (int, float)) and ai <= 1:
            if not any(re.search(r"缺失|没有|无\s*AI", t.get("name", "")) for t in ct):
                problems.append(
                    f"{tag} AI 占比只有 {ai} 分，core_tech 里要有一格明写 AI 核心技术缺失"
                )
        ch = c.get("challenges") or []
        if len(ch) < 3:
            problems.append(f"{tag} challenges 只有 {len(ch)} 组，难点与项目解法至少要 3 组")
        for j, t in enumerate(ch):
            if not t.get("problem") or not t.get("solution"):
                problems.append(f"{tag} challenges 第 {j+1} 组缺 problem 或 solution")
        # 教程化降档开关读 tutorial_heavy；缺省时模板从 risks 匹配固定措辞推断。
        # 「严重/极重」推断为 true，「中等/低/轻/无明显」推断为 false。
        # 措辞不在词表里开关会静默失效，这里拦住。
        risks = c.get("risks") or []
        mentions = any(re.search(r"教程|付费课程|实战视频|培训班", r) for r in risks)
        marked = any(re.search(r"教程化(严重|极重|中等|低|轻)|无明显教程", r) for r in risks)
        if mentions and not marked and not isinstance(c.get("tutorial_heavy"), bool):
            problems.append(
                f"{tag} risks 提到教程化但措辞不在识别词表里，降档开关会失效。"
                "重档写「教程化严重」或「教程化极重」，轻档写「教程化中等/低/轻」，"
                "或显式给 tutorial_heavy 布尔值"
            )
        sk = c.get("skeleton")
        if sk is not None:
            lanes = sk.get("lanes") or []
            if not 2 <= len(lanes) <= 5:
                problems.append(f"{tag} skeleton 需要 2 到 5 条泳道，当前 {len(lanes)}")
            for j, ln in enumerate(lanes):
                nodes = ln.get("nodes") or []
                if not nodes:
                    problems.append(f"{tag} skeleton 第 {j+1} 条泳道没有节点")
                if not ln.get("label"):
                    problems.append(f"{tag} skeleton 第 {j+1} 条泳道缺 label")
                for n in nodes:
                    if not n.get("name"):
                        problems.append(f"{tag} skeleton 第 {j+1} 条泳道有节点缺 name")
            if not sk.get("caption"):
                problems.append(f"{tag} skeleton 缺 caption，要说清主干链路")
            if not sk.get("basis"):
                problems.append(f"{tag} skeleton 缺 basis，骨架必须能追溯到实际目录或文件")
        # A 档准入是硬规则，机械校验掉，避免打分和档位自相矛盾。
        if c.get("tier") == "A":
            tot = weighted_total(c, weights)
            biz = sc.get("business")
            if tot is not None and tot < 4.0:
                problems.append(f"{tag} 标 A 档但总分 {tot} 低于 4.0")
            if isinstance(biz, (int, float)) and biz < 3:
                problems.append(f"{tag} 标 A 档但业务真实性 {biz} 低于 3，不满足准入")
    if not (data.get("meta") or {}).get("searched_at"):
        problems.append("meta.searched_at 缺失，元数据必须标注检索时间")
    return problems


def merge_data(base, extra_paths):
    """把额外的候选文件并进主报告。

    多平台工作流是分开跑的：GitHub 一个池、Gitee 一个池，最后合成一份报告。
    按 platform + repo 去重，后来的覆盖先前的，方便用重跑结果刷新旧条目。
    """
    merged = json.loads(json.dumps(base))
    merged.setdefault("candidates", [])
    seen = {(c.get("platform", "github"), c.get("repo")): i
            for i, c in enumerate(merged["candidates"])}
    added = replaced = extra_pool = 0
    for p in extra_paths:
        with open(p, encoding="utf-8") as f:
            extra = json.load(f)
        for c in extra.get("candidates") or []:
            key = (c.get("platform", "github"), c.get("repo"))
            if key in seen:
                merged["candidates"][seen[key]] = c
                replaced += 1
            else:
                seen[key] = len(merged["candidates"])
                merged["candidates"].append(c)
                added += 1
        for k in ("rejected", "unverified"):
            merged[k] = (merged.get(k) or []) + (extra.get(k) or [])
        em, bm = extra.get("meta") or {}, merged.setdefault("meta", {})
        for k in ("queries", "sources"):
            bm[k] = list(dict.fromkeys((bm.get(k) or []) + (em.get(k) or [])))
        extra_pool += (extra.get("funnel") or {}).get("pool", 0)
        if extra.get("weights") and not base.get("weights"):
            merged["weights"] = extra["weights"]
    f = merged.setdefault("funnel", {})
    f["scored"] = len(merged["candidates"])
    f["pool"] = (f.get("pool") or 0) + extra_pool
    print(f"合并完成：新增 {added} 个，刷新 {replaced} 个，当前共 {len(merged['candidates'])} 个候选。")
    return merged


def exclude_candidates(data, specs):
    """把指定候选移进淘汰区。specs 形如 repo=原因，原因可省略。

    用户说「不要这种」时用它，比手改几十 KB 的 JSON 稳。
    """
    want = {}
    for s in specs:
        repo, _, reason = s.partition("=")
        want[repo.strip()] = reason.strip() or "按用户口径排除"
    kept, moved = [], []
    for c in data.get("candidates") or []:
        if c.get("repo") in want:
            moved.append({"repo": c["repo"], "url": c.get("url", ""),
                          "reason": want[c["repo"]]})
        else:
            kept.append(c)
    missing = set(want) - {m["repo"] for m in moved}
    if missing:
        print(f"[提示] 这些名字不在候选里，未移动：{', '.join(sorted(missing))}", file=sys.stderr)
    data["candidates"] = kept
    data["rejected"] = moved + (data.get("rejected") or [])
    f = data.setdefault("funnel", {})
    f["scored"] = len(kept)
    f["hard_rejected"] = len(data["rejected"])
    print(f"已排除 {len(moved)} 个候选进淘汰区，剩 {len(kept)} 个。")
    return data


def apply_skeletons(data, path):
    """把骨架图贴到候选上。骨架单独一份文件，改图不用动分数。

    文件格式：{"owner/repo": {"lanes":[...], "caption":"...", "basis":"..."}}
    """
    with open(path, encoding="utf-8") as f:
        sk = json.load(f)
    hit = miss = 0
    for c in data.get("candidates") or []:
        if c.get("repo") in sk:
            c["skeleton"] = sk[c["repo"]]
            hit += 1
    known = {c.get("repo") for c in data.get("candidates") or []}
    miss = len(set(sk) - known)
    if miss:
        print(f"[提示] 骨架文件里有 {miss} 个仓库不在候选中，已忽略：",
              ", ".join(sorted(set(sk) - known)), file=sys.stderr)
    print(f"已贴 {hit} 张骨架图。")
    return data


def attach_search_process(data, path):
    """把 SearchAgent 的多轮检索过程并进报告，让动态造词可被看见和复核。"""
    with open(path, encoding="utf-8") as f:
        sp = json.load(f)
    keep = ("search_rounds", "coverage", "term_origins", "frontier",
            "learned_lex", "topic_graph_top", "queries")
    data["search_process"] = {k: sp[k] for k in keep if k in sp}
    data["search_process"]["target"] = sp.get("target") or {}
    rounds = sp.get("search_rounds") or []
    fr = sp.get("frontier") or []
    print(f"已并入检索过程：{len(rounds)} 轮，{len(fr)} 个词，"
          f"{len(sp.get('topic_graph_top') or [])} 条 topic 共现边。")
    return data


def main():
    ap = argparse.ArgumentParser(description="渲染求职项目报告")
    ap.add_argument("data", help="报告 JSON 路径")
    ap.add_argument("out", help="输出 HTML 路径")
    ap.add_argument("--template", default=default_template())
    ap.add_argument("--merge", action="append", default=[],
                    help="并入额外候选 JSON，可重复。GitHub 池 + Gitee 池合成一份报告")
    ap.add_argument("--exclude", action="append", default=[], metavar="REPO=原因",
                    help="把候选移进淘汰区，可重复。用于剔除库/框架/引擎这类非产品项目")
    ap.add_argument("--skeletons", metavar="FILE",
                    help="骨架图 JSON，按 repo 贴到候选上。改图不用动分数")
    ap.add_argument("--search-state", metavar="FILE",
                    help="search_agent.py export 产出的池子 JSON，并入多轮检索过程视图")
    ap.add_argument("--allow-warnings", action="store_true",
                     help="校验有问题也继续渲染，问题会打印出来")
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as f:
        data = json.load(f)
    if args.merge:
        data = merge_data(data, args.merge)
    if args.exclude:
        data = exclude_candidates(data, args.exclude)
    if args.skeletons:
        data = apply_skeletons(data, args.skeletons)
    if args.search_state:
        data = attach_search_process(data, args.search_state)
    with open(args.template, encoding="utf-8") as f:
        tpl = f.read()

    occurrences = tpl.count(PLACEHOLDER)
    if occurrences != 1:
        print(
            f"模板里 {PLACEHOLDER} 出现 {occurrences} 次，应当只有数据标签那一处。"
            "多处出现会把 JSON 注入到 JS 字符串里，产出一个打开就白屏的页面。",
            file=sys.stderr,
        )
        sys.exit(1)

    problems = validate(data)
    if problems:
        print("报告数据校验未通过：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        if not args.allow_warnings:
            print("补齐后再渲染，或加 --allow-warnings 强行产出。", file=sys.stderr)
            sys.exit(1)

    # JSON 里的 </script> 会提前闭合数据标签，转义掉。只替换第一处。
    payload = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
    html = tpl.replace(PLACEHOLDER, payload, 1)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    n = len(data.get("candidates") or [])
    a = sum(1 for c in data["candidates"] if c.get("tier") == "A")
    print(f"已渲染 {n} 个候选（A 档 {a} 个）→ {os.path.abspath(args.out)}")
    if problems:
        print(f"注意：带着 {len(problems)} 条校验问题产出，报告内容不完整。")


if __name__ == "__main__":
    main()
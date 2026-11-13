#!/usr/bin/env python3
"""SearchAgent 式多轮检索：每轮从上一轮结果动态生成新词，再去信息源站搜。

词不是预设的。预设词表只在第 0 轮冷启动用一次，之后所有查询词都由数据长出来：

  topic 共现图   同一仓库上一起出现的 topic 构成图，走图找从没种过的邻居词
  跨语言学习     中文查询在 Gitee 命中的仓库带英文 topic，把这个配对学下来
  短语挖掘       描述里跨多个仓库重复出现的二元组，是这个领域真正的说法
  形态拆分       仓库名按 camelCase / kebab / snake 拆出词素
  依赖生态       probe 出的 AI 依赖名本身就是生态词
  相关项目       README 的 Related / Alternatives 段落里的仓库名
  组合生成       两个高分 topic 的笛卡尔组合，只试没试过的
  Agent 提案     Agent 每轮读结果后自己造词，脚本负责校验去重打分

词的优先级由产出反馈决定：产出过产品型候选的词加权，只产出库和清单的词降权。
所以查询构造是自适应的，不是固定模板。

分工：脚本做确定性部分（API 检索、词生成、打分、去重、覆盖度、预算）；
WebSearch 与造词由 Agent 执行，用 ingest-web 和 propose 回灌。

用法:
  python3 search_agent.py init --role '...' --domain 电商搜索 --domain agent --stack python --state s.json
  python3 search_agent.py plan --state s.json
  python3 search_agent.py run --state s.json --sources github
  python3 search_agent.py propose --state s.json --terms terms.json
  python3 search_agent.py ingest-web --state s.json --findings web.json
  python3 search_agent.py deep --state s.json --top 6
  python3 search_agent.py status --state s.json
  python3 search_agent.py export --state s.json --out pool.json
"""

import argparse
import itertools
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gh_scout as G  # noqa: E402

# 冷启动词表。只在第 0 轮用，之后靠 learn_* 动态长词。
COLD_LEX = {
    "电商": ["ecommerce", "commerce", "retail"],
    "搜索": ["search", "retrieval"],
    "检索": ["retrieval", "rag"],
    "推荐": ["recommendation", "recommender"],
    "智能体": ["agent", "agentic"],
    "agent": ["智能体", "agentic"],
    "知识库": ["knowledge base", "rag"],
    "客服": ["customer support", "chatbot"],
    "风控": ["risk", "fraud"],
    "供应链": ["supply chain", "inventory"],
    "教育": ["education", "tutor"],
    "金融": ["finance", "trading"],
}
FORM_EN = ["app", "platform", "assistant", "studio", "console", "webapp", "self-hosted", "dashboard"]
FORM_CN = ["平台", "系统", "助手", "工作台", "应用"]
NEG_EN = ["-topic:awesome", "-topic:tutorial", "-topic:framework", "-topic:sdk"]
STOP = set("""the a an and or of for to in on with by is are be as at from that this it its
open source project projects repo repository github gitee python typescript java go rust
tool tools lib libs sdk api cli util utils core common base simple easy fast new
一个 的 和 与 及 了 是 在 有 为 支持 基于 使用 可以 提供 实现 开源 项目 系统 平台 工具""".split())
# GitHub 元数据与徽章词。它们在几乎每个 README 和描述里都出现，永远不是领域词。
# 第一版没排除，结果 R1 的头部词是 topic / stars / forks / awesome / framework，全是噪声。
NOISE = set("""topic topics star stars fork forks watcher watchers issue issues pull request requests
license readme badge badges shields img http https www com org io net md svg png jpg
contributor contributors release releases commit commits branch master main tag tags
awesome framework frameworks library libraries plugin plugins module modules package packages
version latest stable beta alpha update updated build status docs documentation example examples
demo test tests tutorial tutorials guide handbook checklist collection curated list lists
powered built using used use supports supported support provides provided features feature
powerful lightweight flexible robust modern complete full fully free code coding developer developers
app apps application applications web website site page pages data file files image images video
text string number type types name names value values key keys get set add remove create delete
all more most other others some any each every both one two three first second last next also only
just very much many few several various multiple different same similar related following
this that these those they them their we our you your i me my""".split())
# 形态词只做修饰语，不能单独当检索词，否则搜出来全是泛泛的东西
FORM_ALL = {w.lower() for w in FORM_EN + FORM_CN}
# GitHub 查询限定词。credit_yield 早期把整条查询串拆词去记分，
# 结果 language / stars / archived 这些限定词被记成「产出了 69 个产品」，
# 把噪声词顶到 frontier 第一。只有真正的检索词才该拿产出信用。
QUALIFIERS = set("""language stars archived fork topic in name description readme
pushed created size license good-first-issues help-wanted-issues sort order per_page page""".split())
REPO_URL_RE = re.compile(r"(?:https?://)?(github|gitee)\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
RELATED_HEAD_RE = re.compile(
    r"^#{1,4}\s*.*(related|alternative|see also|similar|ecosystem|comparison|"
    r"相关|同类|替代|生态|对比|推荐项目).*$", re.I | re.M)
HEAD_RE = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.M)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#]{2,}|[\u4e00-\u9fff]{2,6}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(st, p):
    st["updated_at"] = now()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def key_of(rec):
    return f"{rec.get('platform', 'github')}:{rec.get('repo')}"


def has_cjk(t):
    return bool(CJK_RE.search(t))


def topic_slug(t):
    s = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return s if s and re.search(r"[a-z0-9]", s) else None


def split_morphemes(name):
    """仓库名拆词素：camelCase、kebab、snake、数字边界都切开。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    s = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", " ", s)
    return [w.lower() for w in s.split() if len(w) >= 3 and w.lower() not in STOP]


def words(text):
    out = []
    for m in WORD_RE.finditer(text or ""):
        w = m.group(0).lower()
        if w in STOP or w.isdigit() or len(w) < 2:
            continue
        out.append(w)
    return out


# ---------- 词前沿 ----------
def bump(st, term, origin, rnd, weight=1):
    term = (term or "").strip().strip(".,;:-_").lower()
    if not term or term in STOP or term in NOISE or len(term) < 2 or len(term) > 40:
        return None
    if term in FORM_ALL:
        return None
    if re.fullmatch(r"[\d\W]+", term):
        return None
    e = st["frontier"].setdefault(term, {
        "score": 0.0, "df": 0, "origins": [], "first_round": rnd, "used": False,
        "yield": {"product": 0, "other": 0},
    })
    e["score"] += weight
    if origin not in e["origins"]:
        e["origins"].append(origin)
    st["term_origins"][origin] = st["term_origins"].get(origin, 0) + 1
    return term


def idf(st, e):
    """文档频率惩罚。出现在几乎每个仓库里的词是样板话不是领域词。
    第一版没这一项，R1 头部词全是 topic / stars / forks 这种元数据噪声。"""
    n = max(1, len(st.get("candidates") or {}))
    df = e.get("df", 0)
    return max(0.12, math.log((n + 1) / (df + 1)))


def effective(st, e):
    y = e["yield"]
    return e["score"] * idf(st, e) + 2.0 * y["product"] - 1.0 * y["other"]


def learn_from_records(st, recs, rnd, query):
    """从结果动态长词。这是覆盖度增长的引擎，不查任何预设表。

    每个词记一次文档频率 df：出现在多少个不同仓库里。df 高的词是样板话，
    排序时用 IDF 压下去，否则头部全是 topic / stars 这种元数据噪声。
    """
    q_cjk = has_cjk(query or "")
    for r in recs:
        touched = set()
        tps = [t.lower() for t in (r.get("topics") or []) if t]
        for t in tps:
            k = bump(st, t, "mined-topic", rnd, 3)
            if k:
                touched.add(k)
        for a, b in itertools.combinations(sorted(set(tps)), 2):
            pair = "|".join((a, b))
            st["topic_graph"][pair] = st["topic_graph"].get(pair, 0) + 1
        if q_cjk:
            for t in tps:
                if not has_cjk(t) and len(t) >= 3:
                    m = st["learned_lex"].setdefault(query.strip(), {})
                    m[t] = m.get(t, 0) + 1
        for w in split_morphemes(r.get("name") or ""):
            k = bump(st, w, "mined-morpheme", rnd, 1)
            if k:
                touched.add(k)
        dw = words(r.get("description") or "")[:16]
        for w in dw:
            k = bump(st, w, "mined-desc", rnd, 1)
            if k:
                touched.add(k)
        for i in range(len(dw) - 1):
            bg = f"{dw[i]} {dw[i+1]}"
            if has_cjk(bg) or len(bg) > 34:
                continue
            st["phrases"][bg] = st["phrases"].get(bg, 0) + 1
            # 短语也要记文档频率。早期短语走单独一轮 bump，df 永远是 0，
            # IDF 拿满分 4.08，把没验证过的短语顶到真实领域 topic 前面。
            pr = st.setdefault("phrase_repos", {}).setdefault(bg, [])
            rk = key_of(r)
            if rk not in pr:
                pr.append(rk)
        for k in touched:
            st["frontier"][k]["df"] += 1
    for bg, c in st["phrases"].items():
        if c >= 2:
            k = bump(st, bg, "mined-phrase", rnd, 2)
            if k:
                want = len(st.get("phrase_repos", {}).get(bg, []))
                if st["frontier"][k]["df"] < want:
                    st["frontier"][k]["df"] = want


def learn_from_probe(st, rec, rnd):
    """深度轮：README 标题、依赖名、相关项目名都是新词来源。"""
    p = rec.get("probe") or {}
    text = p.get("readme_excerpt") or ""
    seeds = []
    for h in (p.get("readme_headings") or []):
        for w in words(h)[:6]:
            bump(st, w, "readme-heading", rnd, 2)
    for d in ((rec.get("ai_dependencies") or {}).get("ai_deps") or []):
        bump(st, d, "dep-ecosystem", rnd, 2)
    seg = ""
    m = RELATED_HEAD_RE.search(text)
    if m:
        rest = text[m.end():]
        nxt = HEAD_RE.search(rest)
        seg = rest[:nxt.start()] if nxt else rest
    for plat, owner, name in REPO_URL_RE.findall(seg or text):
        full = f"{owner}/{name}".rstrip(".")
        if full.lower() == (rec.get("repo") or "").lower():
            continue
        seeds.append({"platform": plat.lower(), "repo": full})
        for w in split_morphemes(name):
            bump(st, w, "related-project", rnd, 2)
    return seeds


def credit_yield(st, recs, query):
    """产出反馈：这条查询的词，产出产品型就加权，只产出库和清单就降权。"""
    terms = [w for w in words(query)
             if w in st["frontier"] and w not in QUALIFIERS and w not in NOISE and w not in FORM_ALL]
    if not terms:
        return
    for r in recs:
        form = r.get("product_form")
        gate = r.get("gate")
        good = (form == "product") or (gate == "pass" and form is None)
        bad = form in ("library", "infra") or gate == "reject"
        for t in terms:
            e = st["frontier"][t]
            if good:
                e["yield"]["product"] += 1
                e["score"] += 1.5
            elif bad:
                e["yield"]["other"] += 1
                e["score"] -= 0.8


def expand_dynamic(term, st):
    """动态扩展。顺序：学过的翻译 → topic 图邻居 → 冷启动表（仅兜底）。"""
    t = term.strip().lower()
    out, how = [term], []
    for q, m in st["learned_lex"].items():
        if t == q.strip().lower() or t in [k.lower() for k in m]:
            for k, c in sorted(m.items(), key=lambda kv: -kv[1])[:4]:
                out.append(k)
            how.append("learned-translate")
            break
    nb = Counter()
    for pair, c in st["topic_graph"].items():
        a, b = pair.split("|", 1)
        if a == t:
            nb[b] += c
        elif b == t:
            nb[a] += c
    for k, _ in nb.most_common(4):
        out.append(k)
    if nb:
        how.append("topic-graph")
    if len(out) == 1:
        for k, vs in COLD_LEX.items():
            if t == k.lower() or k.lower() in t or t in [v.lower() for v in vs]:
                out.append(k)
                out.extend(vs)
        if len(out) > 1:
            how.append("cold-lex")
    for w in split_morphemes(term):
        if w != t:
            out.append(w)
            if "morpheme" not in how:
                how.append("morpheme")
    return list(dict.fromkeys(out)), (how or ["identity"])


def rank_terms(st, n):
    """IDF 加权 + 产出反馈排序。

    原始分乘以 IDF：出现在几乎每个仓库里的词（topic、stars、framework）被压到接近 0，
    只在少数仓库出现的领域词（rerank、pgvector、chunking）浮上来。
    再叠加产出反馈：产出过产品型候选的词加分，只产出库和清单的词扣分。
    """
    items = []
    for k, v in st["frontier"].items():
        if v["used"]:
            continue
        n_pool = max(1, len(st["candidates"]))
        if v.get("df", 0) / n_pool > 0.55 and v["first_round"] > 0:
            continue
        items.append((k, v, effective(st, v)))
    items.sort(key=lambda x: -x[2])
    return items[:n]


def gen_combos(st, n):
    """两个高分 topic 的新组合。只生成没试过的，这是纯生成不是查表。"""
    hot = [k for k, v in sorted(st["frontier"].items(), key=lambda kv: -kv[1]["score"])
           if not has_cjk(k) and " " not in k and topic_slug(k)][:8]
    out = []
    for a, b in itertools.combinations(hot, 2):
        ck = f"{a}+{b}"
        if ck in st["combos_tried"]:
            continue
        w = st["topic_graph"].get("|".join(sorted((a, b))), 0)
        out.append((a, b, w))
    out.sort(key=lambda x: -x[2])
    return out[:n]


# ---------- 查询构造 ----------
def gh_query(term, form, stack):
    parts = [f'"{term}"' if " " in term else term, "in:name,description,readme"]
    if form:
        parts.append(form)
    if stack:
        parts.append(f"language:{stack}")
    parts += ["stars:>50", "archived:false", "fork:false"] + NEG_EN
    return " ".join(parts)


def build_plan(st, kind):
    tgt, rnd = st["target"], len(st["rounds"])
    stack = (tgt.get("stack") or [None])[0]
    q = {"github": [], "gitee": [], "web": []}

    if kind == "seed":
        for d in tgt["domains"]:
            syns, how = expand_dynamic(d, st)
            for s in syns[:4]:
                if has_cjk(s):
                    q["gitee"].append({"q": s, "why": f"种子中文词 {s}", "origin": "seed"})
                else:
                    q["github"].append({"q": gh_query(s, None, stack),
                                        "why": f"种子 {d}→{s}（{'/'.join(how)}）", "origin": "seed"})
            sl = topic_slug(d)
            if sl:
                q["github"].append({"q": f"topic:{sl} stars:>200 archived:false",
                                    "why": f"topic 直命中 {sl}", "origin": "seed"})
            en = [x for x in syns if not has_cjk(x)][:1]
            cn = [x for x in syns if has_cjk(x)][:1]
            for e in en:
                q["web"] += [
                    {"q": f"site:github.com {e} {' '.join(FORM_EN[:3])}", "why": "源站直搜 GitHub", "origin": "seed"},
                    {"q": f"awesome {e} github", "why": "人工筛过的清单入口", "origin": "seed"},
                    {"q": f"site:news.ycombinator.com Show HN {e}", "why": "HN 发布帖捞新项目", "origin": "seed"},
                    {"q": f"site:reddit.com {e} open source self-hosted", "why": "社区自托管推荐", "origin": "seed"},
                ]
            for c in cn:
                q["web"] += [
                    {"q": f"site:gitee.com {c} {' '.join(FORM_CN[:2])}", "why": "源站直搜 Gitee", "origin": "seed"},
                    {"q": f"site:juejin.cn {c} 开源 项目 实战", "why": "中文社区实战文", "origin": "seed"},
                    {"q": f"site:zhihu.com {c} 开源 项目 推荐", "why": "中文社区推荐帖", "origin": "seed"},
                ]
    else:
        for term, meta, sc in rank_terms(st, 8):
            meta["used"] = True
            syns, how = expand_dynamic(term, st)
            en = [x for x in syns if not has_cjk(x)][:2]
            cn = [x for x in syns if has_cjk(x)][:1]
            tag = f"R{rnd} {term}（{'/'.join(meta['origins'])}→{'/'.join(how)}，分 {sc:.1f}）"
            for e in en:
                q["github"].append({"q": gh_query(e, FORM_EN[rnd % len(FORM_EN)], stack),
                                    "why": tag + f"→{e}", "origin": "dynamic"})
            for c in cn:
                q["gitee"].append({"q": c, "why": tag + f"→{c}", "origin": "dynamic"})
                q["web"].append({"q": f"site:gitee.com {c} {FORM_CN[rnd % len(FORM_CN)]}",
                                 "why": tag + " 源站扩展", "origin": "dynamic"})
            if en:
                q["web"].append({"q": f"site:github.com {en[0]} {FORM_EN[(rnd+1) % len(FORM_EN)]}",
                                 "why": tag + " 源站扩展", "origin": "dynamic"})
        for a, b, w in gen_combos(st, 3):
            st["combos_tried"].append(f"{a}+{b}")
            qq = f"{a} {b} in:name,description,readme stars:>30 archived:false fork:false " + " ".join(NEG_EN)
            q["github"].append({"q": qq, "why": f"R{rnd} 组合生成 {a}×{b}（共现 {w}）", "origin": "combo"})
        if kind == "deep":
            for r in top_candidates(st, 4):
                nm = r["repo"].split("/")[-1]
                q["web"].append({"q": f'"{nm}" alternatives open source',
                                 "why": f"深度轮：{nm} 的同类", "origin": "deep"})
                q["web"].append({"q": f'"{nm}" vs comparison github',
                                 "why": f"深度轮：{nm} 的对比对象", "origin": "deep"})
    used = set(st["used_queries"])
    for src in q:
        seen, keep = set(), []
        for x in q[src]:
            if x["q"] in used or x["q"] in seen:
                continue
            seen.add(x["q"])
            keep.append(x)
        q[src] = keep
    return {"round": rnd, "kind": kind, "queries": q}


def top_candidates(st, n):
    cs = [c for c in st["candidates"].values() if c.get("gate") != "reject"]
    cs.sort(key=lambda c: -(c.get("stars") or 0))
    return cs[:n]


def ingest(st, recs, source, query, rnd):
    new = 0
    for r in recs:
        if not r.get("repo"):
            continue
        k = key_of(r)
        prov = {"round": rnd, "source": source, "query": query}
        if k in st["candidates"]:
            cur = st["candidates"][k]
            cur.setdefault("discovered_by", []).append(prov)
            for f in ("topics", "flags"):
                cur[f] = list(dict.fromkeys((cur.get(f) or []) + (r.get(f) or [])))
        else:
            r["discovered_by"] = [prov]
            st["candidates"][k] = r
            new += 1
    learn_from_records(st, recs, rnd, query)
    credit_yield(st, recs, query)
    st["used_queries"].append(query)
    return new


def finish_round(st, rec, before):
    rec["new_unique"] = len(st["candidates"]) - before
    rec["pool_after"] = len(st["candidates"])
    rec["ended_at"] = now()
    rec["frontier_after"] = len(st["frontier"])
    rec["terms_generated"] = sum(1 for v in st["frontier"].values() if v["first_round"] == rec["n"])
    st["rounds"].append(rec)
    rate = rec["new_unique"] / max(1, len(st["candidates"]))
    st["coverage"]["saturation"].append(
        {"round": rec["n"], "kind": rec["kind"], "new": rec["new_unique"],
         "new_rate": round(rate, 3), "pool": rec["pool_after"],
         "frontier": len(st["frontier"])})
    return rate


def advise_stop(st):
    sat = st["coverage"]["saturation"]
    if len(sat) >= 2 and all(s["new_rate"] < 0.15 for s in sat[-2:]):
        st["coverage"]["stopped_reason"] = "连续两轮新增率低于 15%，覆盖已饱和"
        print("建议停止：" + st["coverage"]["stopped_reason"])
    if len(st["rounds"]) >= 6:
        st["coverage"]["stopped_reason"] = "已达 6 轮上限"
        print("建议停止：轮次上限")
    if st["budget"]["gitee_calls"] >= 50:
        print("Gitee 额度接近 60/小时上限，后续只做 GitHub 与 web。")


# ---------- 命令 ----------
def cmd_init(a):
    st = {
        "target": {"role": a.role, "domains": a.domain, "stack": a.stack or []},
        "created_at": now(), "rounds": [], "candidates": {}, "frontier": {},
        "topic_graph": {}, "learned_lex": {}, "phrases": {}, "combos_tried": [],
        "term_origins": {}, "used_queries": [], "web_findings": [], "unverified": [],
        "coverage": {"by_source": {}, "saturation": [], "stopped_reason": None},
        "budget": {"github_search_calls": 0, "gitee_calls": 0, "probe_calls": 0},
    }
    for d in a.domain:
        bump(st, d, "seed", 0, 3)
        syns, _ = expand_dynamic(d, st)
        for s in syns:
            bump(st, s, "cold-lex", 0, 1)
    save(st, a.state)
    print(f"已初始化。域词 {a.domain}，冷启动 frontier {len(st['frontier'])} 词。")
    print("冷启动表只用这一次，后续词由 topic 共现图、跨语言学习、短语、形态、依赖、相关项目和 Agent 提案长出来。")


def cmd_plan(a):
    st = load(a.state)
    kind = a.kind
    if kind == "auto":
        kind = "seed" if not st["rounds"] else ("deep" if len(st["rounds"]) % 2 == 0 else "wide")
    plan = build_plan(st, kind)
    st["_pending_plan"] = plan
    save(st, a.state)
    if a.emit:
        with open(a.emit, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
    print(f"第 {plan['round']} 轮（{plan['kind']}）")
    for src, qs in plan["queries"].items():
        print(f"\n[{src}] {len(qs)} 条")
        for x in qs:
            print(f"  {x['q']}\n      {x['why']}")
    if plan["queries"]["web"]:
        print("\nweb 那组交 Agent 用 WebSearch 跑，结果写 findings.json 后 ingest-web 回灌。")
    print("Agent 也可以读本轮结果自己造词，写 terms.json 后 propose 回灌。")


def cmd_propose(a):
    """Agent 造词入口。脚本不假装会生成语义，只做校验、去重、打分、记账。"""
    st = load(a.state)
    data = load(a.terms)
    rnd = len(st["rounds"])
    items = data if isinstance(data, list) else data.get("terms", [])
    added = 0
    for it in items:
        term = it.get("term") if isinstance(it, dict) else str(it)
        why = it.get("why", "") if isinstance(it, dict) else ""
        before = term.strip().lower() in st["frontier"]
        bump(st, term, "agent-proposed", rnd, float(it.get("weight", 4)) if isinstance(it, dict) else 4)
        if not before:
            added += 1
        if why:
            st["frontier"][term.strip().lower()]["note"] = why
    save(st, a.state)
    print(f"Agent 提案 {len(items)} 词，新增 {added}，frontier {len(st['frontier'])}。")
    print("这些词会按分数进入下一轮 plan，和挖出来的词一起排。")


def cmd_run(a):
    st = load(a.state)
    plan = st.get("_pending_plan") or build_plan(st, "seed" if not st["rounds"] else "wide")
    rnd = plan["round"]
    rec = {"n": rnd, "kind": plan["kind"], "started_at": now(), "queries": []}
    before = len(st["candidates"])
    for src in [s.strip() for s in a.sources.split(",")]:
        if src not in ("github", "gitee"):
            print(f"[跳过] {src} 不是 API 源，web 用 ingest-web", file=sys.stderr)
            continue
        for x in plan["queries"].get(src, [])[: a.max_per_source]:
            try:
                if src == "github":
                    data = G.api_get("/search/repositories",
                                     {"q": x["q"], "sort": "stars", "order": "desc",
                                      "per_page": min(a.limit, 100)})
                    items = data.get("items") or []
                    recs = [G.normalize(i) for i in items]
                    st["budget"]["github_search_calls"] += 1
                else:
                    if not (os.environ.get("GITEE_TOKEN") or os.environ.get("GITEE_ACCESS_TOKEN")):
                        print("[跳过 gitee] search 需要 GITEE_TOKEN，无 token 返回空数组", file=sys.stderr)
                        break
                    items = G.gitee_get("/search/repositories",
                                        {"q": x["q"], "sort": "stars_count", "order": "desc",
                                         "per_page": min(a.limit, 100), "page": 1})
                    items = items if isinstance(items, list) else []
                    recs = [G.normalize_gitee(i) for i in items]
                    st["budget"]["gitee_calls"] += 1
            except G.ScoutError as e:
                rec["queries"].append({"source": src, "q": x["q"], "error": str(e)})
                print(f"[失败] {src} {x['q'][:60]}\n  {e}", file=sys.stderr)
                if "限流" in str(e):
                    break
                continue
            n = ingest(st, recs, src, x["q"], rnd)
            rec["queries"].append({"source": src, "q": x["q"], "why": x["why"],
                                   "origin": x.get("origin"), "returned": len(recs), "new": n})
            st["coverage"]["by_source"][src] = st["coverage"]["by_source"].get(src, 0) + n
            print(f"[{src}] 返回 {len(recs):>3} 新增 {n:>3}  {x['q'][:70]}")
            time.sleep(a.delay)
    rate = finish_round(st, rec, before)
    st["_pending_plan"] = None
    save(st, a.state)
    print(f"\n第 {rnd} 轮：新增 {rec['new_unique']}，池子 {rec['pool_after']}，新增率 {rate:.1%}")
    print(f"本轮生成新词 {rec['terms_generated']}，frontier {len(st['frontier'])}，"
          f"未用 {sum(1 for v in st['frontier'].values() if not v['used'])}")
    print("词来源：" + json.dumps(st["term_origins"], ensure_ascii=False))
    advise_stop(st)


def cmd_ingest_web(a):
    st = load(a.state)
    f = load(a.findings)
    rnd = len(st["rounds"])
    rec = {"n": rnd, "kind": "web", "started_at": now(), "queries": []}
    before = len(st["candidates"])
    for item in (f if isinstance(f, list) else f.get("findings", [])):
        q, src = item.get("query", ""), item.get("source", "web")
        repos = list(item.get("repos") or [])
        for u in item.get("urls") or []:
            for plat, owner, name in REPO_URL_RE.findall(u):
                repos.append({"platform": plat.lower(), "repo": f"{owner}/{name}"})
        got = []
        for r in repos:
            full = (r["repo"] if isinstance(r, dict) else r).rstrip("/").removesuffix(".git")
            plat = (r.get("platform") if isinstance(r, dict) else None) or "github"
            try:
                if plat == "gitee":
                    got.append(G.normalize_gitee(G.gitee_get(f"/repos/{full}"), requested=full))
                    st["budget"]["gitee_calls"] += 1
                else:
                    got.append(G.normalize(G.api_get(f"/repos/{full}")))
            except G.ScoutError as e:
                print(f"[待核实] {full}: {e}", file=sys.stderr)
                st["unverified"].append({"repo": full, "platform": plat, "error": str(e)})
                if "限流" in str(e):
                    break
        n = ingest(st, got, src, q, rnd)
        for t in item.get("terms") or []:
            bump(st, t, "web-term", rnd, 2)
        st["web_findings"].append({"query": q, "source": src, "repos": len(got), "new": n})
        rec["queries"].append({"source": src, "q": q, "origin": "web", "returned": len(got), "new": n})
        print(f"[{src}] 核实 {len(got)} 新增 {n}  {q[:64]}")
    finish_round(st, rec, before)
    save(st, a.state)
    print(f"\nweb 轮：新增 {rec['new_unique']}，池子 {rec['pool_after']}，本轮新词 {rec['terms_generated']}")


def cmd_deep(a):
    st = load(a.state)
    rnd = len(st["rounds"])
    rec = {"n": rnd, "kind": "deep", "started_at": now(), "queries": []}
    before = len(st["candidates"])
    targets, seeds_all = top_candidates(st, a.top), []
    for c in targets:
        try:
            pr = G.probe_one(dict(c), with_tree=not a.no_tree)
            st["budget"]["probe_calls"] += 4
        except G.ScoutError as e:
            print(f"[跳过] {c['repo']}: {e}", file=sys.stderr)
            continue
        st["candidates"][key_of(pr)] = pr
        seeds = learn_from_probe(st, pr, rnd)
        seeds_all += seeds
        credit_yield(st, [pr], " ".join(words(c["repo"])[:2]))
        rec["queries"].append({"source": "readme", "q": c["repo"], "origin": "deep",
                               "seeds": len(seeds), "form": pr.get("product_form")})
        print(f"[深度] {c['repo']:<36} 形态 {str(pr.get('product_form')):<8} 相关项目 {len(seeds)}")
        time.sleep(a.delay)
    uniq = {}
    for s in seeds_all:
        uniq[(s["platform"], s["repo"].lower())] = s
    got = []
    for (plat, _), s in list(uniq.items())[: a.max_seeds]:
        if f"{plat}:{s['repo']}" in st["candidates"]:
            continue
        try:
            if plat == "gitee":
                got.append(G.normalize_gitee(G.gitee_get(f"/repos/{s['repo']}"), requested=s["repo"]))
                st["budget"]["gitee_calls"] += 1
            else:
                got.append(G.normalize(G.api_get(f"/repos/{s['repo']}")))
        except G.ScoutError as e:
            print(f"[待核实] {s['repo']}: {e}", file=sys.stderr)
            st["unverified"].append({"repo": s["repo"], "platform": plat, "error": str(e)})
            if "限流" in str(e):
                break
    ingest(st, got, "readme-related", "deep-round", rnd)
    rate = finish_round(st, rec, before)
    save(st, a.state)
    print(f"\n深度轮：读 {len(targets)} 个 README，挖 {len(uniq)} 个相关项目，"
          f"新增 {rec['new_unique']}，池子 {rec['pool_after']}，新词 {rec['terms_generated']}")
    advise_stop(st)


def cmd_status(a):
    st = load(a.state)
    cs = list(st["candidates"].values())
    print(f"目标：{st['target']['role']}｜域词 {st['target']['domains']}")
    print(f"池子 {len(cs)}｜轮次 {len(st['rounds'])}｜frontier {len(st['frontier'])} 词"
          f"｜topic 边 {len(st['topic_graph'])}｜学到翻译 {len(st['learned_lex'])} 组")
    print("\n各轮产出：")
    for r in st["rounds"]:
        print(f"  R{r['n']} {r['kind']:<6} 查询 {len(r['queries']):>2} 新增 {r['new_unique']:>3} "
              f"池子 {r['pool_after']:>3} 新词 {r.get('terms_generated', 0):>3}")
    print("\n词来源分布（证明词是长出来的不是预设的）：")
    for k, v in sorted(st["term_origins"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:<20} {v}")
    print("\n来源分布：" + json.dumps(st["coverage"]["by_source"], ensure_ascii=False))
    print("平台分布：" + json.dumps(dict(Counter(c.get("platform", "github") for c in cs)), ensure_ascii=False))
    print("饱和曲线：" + ", ".join(f"R{s['round']} {s['new_rate']:.0%}" for s in st["coverage"]["saturation"]))
    if st["coverage"].get("stopped_reason"):
        print("停止理由：" + st["coverage"]["stopped_reason"])
    print("\n未用高分词（下一轮候选，带来源与产出）：")
    for k, v, sc in rank_terms(st, 12):
        print(f"  {k:<26} 有效分 {sc:>6.1f} 原始 {v['score']:>6.1f} df {v.get('df',0):>3} "
              f"idf {idf(st,v):>4.2f} 来源 {'/'.join(v['origins'])} "
              f"产出 产品{v['yield']['product']}/其他{v['yield']['other']}")
    if st["learned_lex"]:
        print("\n学到的跨语言配对：")
        for q, m in list(st["learned_lex"].items())[:5]:
            top = sorted(m.items(), key=lambda kv: -kv[1])[:5]
            print(f"  {q} → " + ", ".join(f"{k}({c})" for k, c in top))
    print("\n预算：" + json.dumps(st["budget"], ensure_ascii=False))


def cmd_export(a):
    st = load(a.state)
    cs = sorted(st["candidates"].values(), key=lambda c: -(c.get("stars") or 0))
    out = {
        "platform": "mixed", "searched_at": st["created_at"],
        "search_rounds": st["rounds"], "coverage": st["coverage"],
        "term_origins": st["term_origins"],
        "frontier": sorted(
            [{"term": k, "score": round(effective(st, v), 1), "raw": round(v["score"], 1),
              "df": v.get("df", 0), "idf": round(idf(st, v), 2), "origins": v["origins"],
              "used": v["used"], "yield": v["yield"], "first_round": v["first_round"]}
             for k, v in st["frontier"].items()], key=lambda x: -x["score"])[:60],
        "learned_lex": st["learned_lex"],
        "topic_graph_top": sorted([{"pair": k, "count": v} for k, v in st["topic_graph"].items()],
                                  key=lambda x: -x["count"])[:30],
        "queries": st["used_queries"],
        "gate_counts": {g: sum(1 for c in cs if c.get("gate") == g)
                        for g in ("pass", "source-reading", "reject")},
        "candidates": cs, "unverified": st.get("unverified", []),
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"已导出 {len(cs)} 个候选 + {len(st['rounds'])} 轮检索过程到 {os.path.abspath(a.out)}")


def main():
    ap = argparse.ArgumentParser(description="SearchAgent 式多轮检索，词由结果动态生成")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init"); i.add_argument("--role", required=True)
    i.add_argument("--domain", action="append", required=True)
    i.add_argument("--stack", action="append"); i.add_argument("--state", required=True)
    i.set_defaults(func=cmd_init)

    p = sub.add_parser("plan"); p.add_argument("--state", required=True)
    p.add_argument("--kind", choices=["auto", "seed", "wide", "deep"], default="auto")
    p.add_argument("--emit"); p.set_defaults(func=cmd_plan)

    t = sub.add_parser("propose", help="回灌 Agent 自己造的词")
    t.add_argument("--state", required=True); t.add_argument("--terms", required=True)
    t.set_defaults(func=cmd_propose)

    r = sub.add_parser("run"); r.add_argument("--state", required=True)
    r.add_argument("--sources", default="github"); r.add_argument("--limit", type=int, default=20)
    r.add_argument("--max-per-source", type=int, default=6); r.add_argument("--delay", type=float, default=2.1)
    r.set_defaults(func=cmd_run)

    w = sub.add_parser("ingest-web"); w.add_argument("--state", required=True)
    w.add_argument("--findings", required=True); w.set_defaults(func=cmd_ingest_web)

    d = sub.add_parser("deep"); d.add_argument("--state", required=True)
    d.add_argument("--top", type=int, default=6); d.add_argument("--max-seeds", type=int, default=12)
    d.add_argument("--no-tree", action="store_true"); d.add_argument("--delay", type=float, default=0.3)
    d.set_defaults(func=cmd_deep)

    s = sub.add_parser("status"); s.add_argument("--state", required=True); s.set_defaults(func=cmd_status)
    e = sub.add_parser("export"); e.add_argument("--state", required=True)
    e.add_argument("--out", required=True); e.set_defaults(func=cmd_export)

    a = ap.parse_args()
    try:
        a.func(a)
    except G.ScoutError as ex:
        print(f"错误: {ex}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
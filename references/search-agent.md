# SearchAgent 式多轮检索

单轮固定查询式的覆盖面被种子词锁死。真实覆盖度来自迭代：搜到的仓库带 topics、名字、描述、README 标题和相关项目链接，这些都是下一轮的查询词来源。挖出来再搜，池子才会往外长。

`scripts/search_agent.py` 是这条链路的编排器。

## 目录

- [词是长出来的不是预设的](#词是长出来的不是预设的)
- [八条造词路径](#八条造词路径)
- [词的质量控制](#词的质量控制)
- [轮次与停止条件](#轮次与停止条件)
- [信息源与站点定向](#信息源与站点定向)
- [分工：脚本做什么，Agent 做什么](#分工脚本做什么agent-做什么)
- [完整跑法](#完整跑法)
- [实测记录](#实测记录)
- [踩过的坑](#踩过的坑)

## 词是长出来的不是预设的

`COLD_LEX` 只有 12 组对照词，**只在第 0 轮冷启动用一次**。之后所有查询词都由数据生成。

实测一次三轮检索的词来源分布：

| 来源 | 词数 |
|---|---|
| seed 种子 | 2 |
| cold-lex 预设词表 | 11 |
| mined-topic | 1176 |
| mined-phrase | 1507 |
| mined-desc | 853 |
| mined-morpheme | 182 |
| readme-heading | 161 |
| dep-ecosystem | 10 |

预设 13 个，长出来 3889 个。报告里的「检索过程」视图把这个分布画成堆叠条，点图例可以只看某一来源的词，直接回答「是不是只有预设词」这个问题。

## 八条造词路径

| 路径 | 机制 | 权重 |
|---|---|---|
| `mined-topic` | 仓库自己打的 topic 标签，作者亲手标注，最可信 | 3 |
| `topic-graph` | 同一仓库上共现的 topic 两两连边，走图找从没种过的邻居词 | 继承 |
| `mined-phrase` | 描述里跨 ≥2 个仓库重复出现的二元组，是这个领域真正的说法 | 2 |
| `mined-desc` | 描述分词 | 1 |
| `mined-morpheme` | 仓库名按 camelCase / kebab / snake / 数字边界拆词素 | 1 |
| `learned-translate` | 中文查询在 Gitee 命中的仓库带英文 topic，把这个配对学下来，不查词典 | 继承 |
| `readme-heading` | 深度轮读 README 标题里的架构词 | 2 |
| `dep-ecosystem` | probe 出的 AI 依赖名本身就是生态词 | 2 |
| `related-project` | README 的 Related / Alternatives / 相关项目 段落里的仓库名，既当新候选也拆成新词 | 2 |
| `combo` | 两个高分 topic 的笛卡尔组合，只生成没试过的 | 共现数 |
| `agent-proposed` | Agent 每轮读结果后自己造词，`propose` 回灌，脚本只做校验去重打分 | 4 |

`expand_dynamic()` 的扩展顺序是：学过的翻译 → topic 图邻居 → 形态拆分 → 冷启动表兜底。只有前三条都没命中才查预设表，所以预设表实际影响极小。

## 词的质量控制

动态造词最大的风险是造出垃圾。第一版就翻过车：R1 的头部词是 `topic`、`stars`、`forks`、`awesome`、`framework`，全是 GitHub 元数据噪声，因为它们在几乎每个描述里都出现，原始分累加最高。

三层防护：

**噪声词表**。`NOISE` 列出 GitHub 元数据与徽章词（topic / stars / forks / license / badge / shields / contributor / release…），永不进词前沿。`FORM_ALL` 里的形态词只做修饰语，不单独当检索词。

**IDF 加权**。每个词记文档频率 `df`，即出现在多少个不同仓库里。有效分 = 原始分 × IDF + 产出反馈，IDF = `log((N+1)/(df+1))`。出现在 45 个仓库里的 `agent` 拿 0.43，只出现在 3 个仓库里的 `cart` 拿 2.69。样板词被自动压下去，领域词浮上来。df 占池子 55% 以上的词直接不参与排序。

**产出反馈**。`credit_yield` 按查询的实际产出给词记分：产出产品型候选 +1.5，只产出库或清单 −0.8。所以造词是自适应的，历史产出好的词优先。

修完之后 R1 的头部词变成 `cart`、`machine-learning`、`mcp`、`claude-code`、`codex`、`openai`，都是真领域词。

## 轮次与停止条件

轮次类型交替：`seed` → `wide` → `deep` → `wide` → `deep`…

- **seed**：用求职坐标的域词冷启动，四路并发（GitHub 结构化、Gitee 关键词、源站定向 WebSearch、清单入口）
- **wide**：取未用过的最高有效分词，动态扩展后配形态词再搜，另加 3 条没试过的 topic 组合
- **deep**：probe 头部候选，读 README 挖相关项目与架构词，再对头部项目搜 alternatives 与 vs 对比

停止条件三条，任一命中就建议停：

- 连续两轮新增率低于 15%（覆盖饱和）
- 达到 6 轮上限
- Gitee 调用数接近 50（60 次/小时上限）

`status` 打印饱和曲线、词来源分布、未用高分词及其 df/idf/产出、学到的跨语言配对、预算消耗。

## 信息源与站点定向

WebSearch 查询全部带 `site:` 限定，直接打到信息源站而不是泛搜：

| 站点 | 用途 |
|---|---|
| `site:github.com` | 源站直搜，配形态词偏产品 |
| `site:gitee.com` | 中文项目源站 |
| `site:juejin.cn` | 中文技术社区实战文，捞 GitHub 检索式覆盖不到的业务项目 |
| `site:zhihu.com` | 中文社区推荐帖 |
| `site:news.ycombinator.com Show HN` | 新发布项目，时效性最好 |
| `site:reddit.com … self-hosted` | 社区自托管推荐，产品型密度高 |
| `awesome {域词} github` | 人工筛过的清单入口 |
| `"{项目名}" alternatives` / `vs` | 深度轮找同类与对比对象 |

GitHub 查询统一带 `archived:false fork:false -topic:awesome -topic:tutorial -topic:framework -topic:sdk`，在检索阶段就压住清单和框架，和产品形态硬门槛对齐。

## 分工：脚本做什么，Agent 做什么

脚本不假装能上网搜网页，也不假装会生成语义。

**脚本负责**：GitHub / Gitee API 检索、词挖掘与生成、IDF 与产出打分、查询构造、去重、溯源记账、覆盖度统计、预算控制、状态持久化。

**Agent 负责**：执行 `plan` 输出的 web 那组 WebSearch；读结果后自己造词；把两者回灌。

回灌格式：

```json
// findings.json → ingest-web
[{"query":"site:juejin.cn 电商搜索 开源 项目 实战","source":"juejin",
  "repos":[{"platform":"github","repo":"owner/name"}],
  "urls":["https://github.com/owner/other"],
  "terms":["选品","竞品分析"]}]

// terms.json → propose
[{"term":"rerank","why":"R1 的 ragflow 与 serviette 都做精排，这个词能捞到同类","weight":5},
 {"term":"query understanding","why":"电商搜索的查询改写环节，描述里反复出现"}]
```

`propose` 的词按 `agent-proposed` 来源记账，权重默认 4，比挖出来的词高，下一轮 `plan` 优先取用。

每个候选记 `discovered_by: [{round, source, query}]`，能回答「这个候选是哪一轮哪条查询捞到的」。

## 完整跑法

```bash
S=/path/to/skill/scripts

python3 $S/search_agent.py init --role 'AI Agent 应用工程师' \
  --domain 电商搜索 --domain agent --stack python --state s.json

# R0 种子轮
python3 $S/search_agent.py plan --state s.json --kind seed
python3 $S/search_agent.py run --state s.json --sources github --limit 15
# Agent 跑 plan 里的 web 查询，写 findings.json
python3 $S/search_agent.py ingest-web --state s.json --findings findings.json

# R1 广度轮：词由 R0 结果长出来
python3 $S/search_agent.py plan --state s.json --kind wide
python3 $S/search_agent.py run --state s.json --sources github
# Agent 读结果造词
python3 $S/search_agent.py propose --state s.json --terms terms.json

# R2 深度轮
python3 $S/search_agent.py deep --state s.json --top 6

python3 $S/search_agent.py status --state s.json
python3 $S/search_agent.py export --state s.json --out pool.json

# 核实入围候选，再出报告
python3 $S/gh_scout.py probe --from pool.json --top 20 --out probed.json
python3 $S/render_report.py report.json out.html --search-state pool.json
```

`--search-state` 把多轮过程并进报告，生成「检索过程」视图：词来源堆叠条、轮次卡片、覆盖度双轴曲线、topic 共现图、IDF 加权词前沿表、查询血缘。10 个控件常驻可见，改筛选即时更新，不重建整页。

## 实测记录

2026-11-13 实际跑过三轮，方向「电商搜索 + agent」，Python 栈：

| 轮次 | 类型 | 查询数 | 新增候选 | 池子 | 本轮新词 | 词前沿 |
|---|---|---|---|---|---|---|
| R0 | seed | 4 | 32 | 32 | 571 | 571 |
| R1 | wide | 5 | 26 | 58 | 484 | 1055 |
| R2 | deep | 3 | 0 | 58 | 90 | 1145 |

R1 的新增率 44.8%，说明动态造词确实把池子往外推了。R2 新增 0 但仍长出 90 个词，因为深度轮读的是 README 架构词，候选没增但词前沿在长，下一轮还有料。

topic 共现图头部边：`ai|llm` 14、`claude-code|codex` 13、`antigravity|claude-code` 12、`ai-agents|claude-code` 11。这些边直接生成了 `agent×llm`、`llm×ai` 这类组合查询。

## 踩过的坑

**元数据词霸榜**。第一版没有 IDF，R1 头部词是 topic / stars / forks / awesome / framework。加 NOISE 词表 + IDF 加权 + df 占比上限三层才压住。

**查询限定词被记产出信用**。`credit_yield` 早期把整条查询串拆词记分，`language:python` 里的 `language` 被记成「产出了 69 个产品」，顶到词前沿第一。现在只给真正的检索词记信用，`QUALIFIERS` 排除限定词。

**短语 df 永远是 0**。短语走单独一轮 bump，没进per-repo 的 df 统计，IDF 拿满分 4.08，把没验证过的短语顶到真实 topic 前面。现在用 `phrase_repos` 记短语出现在哪些仓库，df 取实际值。

**中文词 slug 化成垃圾**。`topic_slug("电商搜索")` 早期返回 `----`。现在非 ASCII 词不生成 topic 查询，先经 `expand_dynamic` 扩成英文再走 topic 路径。

**复合中文词扩不出英文**。`expand` 早期只做整词相等匹配，「电商搜索」查不到词表里的「电商」和「搜索」。现在做子串包含匹配，复合词能拆出 ecommerce 和 search。

**tuple 做 dict key 存不下去**。状态文件的 candidates 用 `(platform, repo)` 做 key，`json.dump` 直接报错。改成 `"platform:repo"` 字符串。
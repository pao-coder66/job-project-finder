---
name: job-project-finder
description: 为求职者和转行者从 GitHub 与公网发现值得做的项目，核实真实元数据后按 AI 核心技术占比、新颖性、时效性、上手难度、Star 与社区认可、业务真实性六维打分，输出单文件 HTML 候选报告。当用户提到找项目、找开源项目、找练手项目、简历项目、求职项目、面试项目、项目选型、想做个项目、不知道做什么项目、GitHub 上有什么值得做的项目、值得读源码的项目、fork 什么项目来改造、秋招项目、转行项目时，必须使用本技能。即使用户只说「帮我找几个项目」或「有什么项目能写进简历」，也应主动触发。不要用于已经选定项目之后的源码讲解、代码改造或简历撰写。
---

# 求职项目发现

给求职者找可以写进简历、面试能讲清楚的项目。

## 这个任务真正的难点

求职者缺的不是项目数量，GitHub 上有几十万个仓库。真正的失败模式是找到一个**教程项目**：跟着 README 敲一遍，跑通了，写进简历，面试官第一个问题就问穿。Netflix clone、todo list、一行 API 包装的 chatbot 都属于这类，它们没有真实业务约束，没有取舍，没有可量化的指标，追问两层就见底。

所以这个技能的主要工作是**淘汰**和**核实**，不是多搜几个仓库。判断一个项目有没有价值，核心就问一句话：能不能说清「谁在什么场景下遇到什么难题，这个项目怎么解，为什么这么解而不是那么解」。说不清的，star 再高也不推。

**只要代表某类产品的 Agent 项目。** 库、SDK、框架、引擎、中间件一律不要，哪怕分数很高。mem0 是记忆层 SDK，Meilisearch 是搜索引擎，这两个都不该出现在候选里。判据是有没有面向最终用户的交互面，不是名字里有没有 agent。特别注意：有管理台不等于有产品，mem0 有 76 个前端文件但全在 `server/dashboard/` 运维后台下，主用法仍然是 `from mem0 import`。这条对读源码档同样生效，读源码要读产品的源码而不是库的源码。细则与实测校准见 `references/scoring.md` 的产品形态硬门槛。

另一个高频失败模式是凭记忆报仓库名和 star 数。训练数据里的仓库可能已经改名、归档、停更，或者 star 数差了一个量级。所有元数据必须来自实际 API 调用。

## 前置条件

- **Python 3**，只用标准库，不需要 pip 装任何东西。
- **GitHub 访问**：优先 `gh` CLI 并已 `gh auth login`；没有 gh 时脚本回退到环境变量 `GITHUB_TOKEN` 或 `GH_TOKEN`。两者都没有会明确报错并停下，这时不要跳过核实凭记忆填元数据，告诉用户去配。
- **Gitee 访问**：`GITEE_TOKEN` 只在跑 Gitee **检索**时必需。按名核实免认证，但未认证只有 **60 次/小时**，probe 一个仓库要 3 到 5 次，一小时只够十个左右。macOS 上 Python 缺 CA bundle 时脚本会自动回退 curl 走系统信任链，不会关掉 TLS 校验。
- 本文里的 `scripts/...`、`references/...`、`assets/...` 都相对**技能目录**，不是会话工作目录。跑脚本时用技能目录的绝对路径。
- 报告产物写到会话工作目录，不要写进技能目录。

先确认环境再开工：

```bash
gh auth status || echo "没有 gh，检查 GITHUB_TOKEN"
python3 --version
[ -n "$GITEE_TOKEN" ] && echo "Gitee 可检索" || echo "Gitee 只能按名核实，60 次/小时"
```

## 第一步：定求职坐标

四项信息决定后面的打分权重和硬门槛，缺一项就会推出一堆跑不起来或者讲不深的仓库：

- 目标岗位。有 JD 原文最好，直接从里面抽技术关键词和领域词，不要自己发挥
- 已掌握的技术栈。决定哪些项目能改造，哪些只能读
- 时间预算。两周和三个月能承接的项目规模差很远
- 环境约束。有没有 GPU，能不能付费用商业 API，能不能跑 Docker

用一次 AskUserQuestion 把缺的项一起问齐，不要逐条挤牙膏。用户已经给过的信息不要重复问。

如果用户明确说了方向（比如「电商搜索 Agent」「推荐系统」），把方向词直接带进检索式，不要泛化成「AI 项目」。

## 第二步：多轮检索建池

单轮固定查询式的覆盖面被种子词锁死。用 SearchAgent 式多轮检索：每轮从上一轮结果动态生成新词，再去信息源站搜。编排器是 `scripts/search_agent.py`，完整策略见 `references/search-agent.md`。

**词是长出来的不是预设的。** 预设词表只在第 0 轮冷启动用一次。之后所有查询词由八条路径生成：topic 共现图走邻居、描述里跨仓库重复的短语、仓库名形态拆分、中文查询命中的英文 topic 学成翻译配对、README 标题架构词、probe 出的依赖生态词、Related Projects 段落的项目名、两个高分 topic 的新组合。Agent 每轮还可以读结果自己造词，用 `propose` 回灌。实测三轮下来预设 13 个词、长出来 3889 个。

**词的质量靠三层防护。** NOISE 词表挡掉 GitHub 元数据词；IDF 加权把出现在几乎每个仓库里的样板词压下去；产出反馈给产出过产品型候选的词加分、只产出库和清单的词扣分。第一版没这些，R1 头部词全是 topic / stars / forks，加完变成 cart / mcp / claude-code / codex。

```bash
S=<技能目录>/scripts
python3 $S/search_agent.py init --role 'AI Agent 应用工程师' \
  --domain 电商搜索 --domain agent --stack python --state s.json

python3 $S/search_agent.py plan --state s.json --kind seed   # 看本轮查询与构造理由
python3 $S/search_agent.py run --state s.json --sources github --limit 15
# plan 输出的 web 那组由你用 WebSearch 执行，结果写 findings.json
python3 $S/search_agent.py ingest-web --state s.json --findings findings.json

python3 $S/search_agent.py plan --state s.json --kind wide   # 词由 R0 结果长出来
python3 $S/search_agent.py run --state s.json --sources github
python3 $S/search_agent.py propose --state s.json --terms terms.json  # 你自己造的词

python3 $S/search_agent.py deep --state s.json --top 6       # 读 README 挖相关项目
python3 $S/search_agent.py status --state s.json             # 饱和曲线、词来源、预算
python3 $S/search_agent.py export --state s.json --out pool.json
```

**分工**：脚本做 API 检索、词生成、IDF 打分、去重、溯源、覆盖度、预算；WebSearch 与造词由你执行后回灌。脚本不假装能搜网页。

**信息源站点定向**。web 查询全部带 `site:` 打到源站：`site:github.com`、`site:gitee.com`、`site:juejin.cn`、`site:zhihu.com`、`site:news.ycombinator.com Show HN`、`site:reddit.com … self-hosted`，加 `awesome {域词} github` 清单入口。深度轮对头部项目搜 `"{名}" alternatives` 与 `vs` 找同类。

**Gitee 单独一路**。中文业务项目和国内厂商开源 GitHub 覆盖不到。`run --sources gitee` 需要 `GITEE_TOKEN`，无 token 时接口返回 200 加空数组而不是报错，脚本会直接拒绝执行避免误判成「Gitee 上没有项目」。没 token 就走 WebSearch 拿名字后 `gh_scout.py probe --platform gitee --repos` 按名核实。Gitee 的查询语法、60 次/小时限流、GVP 与镜像标记见 `references/search-recipes.md`。

**停止条件**：连续两轮新增率低于 15%、达到 6 轮上限、或 Gitee 额度接近上限。`status` 会打印建议。

不要只依赖 GitHub Trending。它反映短期热度，大量是 prompt 集合和演示 demo，跟「有业务场景、能讲立项」负相关。

## 第三步：核实与硬门槛

对候选池跑 `scripts/gh_scout.py probe`，拿真实元数据和 README 正文。

```bash
# 建池：多个查询式一起跑，自动去重
python3 scripts/gh_scout.py search \
  -q 'topic:rag stars:>1000 pushed:>2026-05-01 archived:false template:false' \
  -q 'hybrid retrieval rerank in:readme,description stars:>300 archived:false' \
  --limit 30 --out pool.json

# 核实：只对入围候选跑，按 star 排序取前 20，名字带 awesome/tutorial 的自动排到最后
python3 scripts/gh_scout.py probe --from pool.json --top 20 --out probed.json

# 从 awesome-list 或公网文章抓来的仓库名直接核实，接受完整 URL
python3 scripts/gh_scout.py probe --repos owner/a,owner/b --out extra.json

# Gitee：检索需 GITEE_TOKEN，按名核实免认证但只有 60 次/小时
python3 scripts/gh_scout.py search --platform gitee -q '智能体' --limit 20 --out ge-pool.json
python3 scripts/gh_scout.py probe --platform gitee --repos owner/a,owner/b --out ge.json
python3 scripts/gh_scout.py probe --from ge-pool.json --top 8 --no-tree --out ge.json
```

两个平台的记录归一到同一形状，`platform` 字段区分来源，下游打分与报告不用分叉。Gitee 额度紧张时加 `--no-tree` 省一次调用，代价是算不出 AI 占比，那一维要人工定分并说明。

search 阶段就能定的硬门槛，`gate` 字段给出 `pass` / `source-reading` / `reject`：

| flag | 判据 | 处理 |
|---|---|---|
| `archived` | 仓库已归档 | reject |
| `fork` / `template` | 是 fork 或模板仓库 | reject，不是原创项目 |
| `stale` | 最近推送距今超过 365 天 | source-reading，报告里标注停更 |
| `no-license` | 完全没有 license | 标注风险，改造后不能公开托管 |
| `custom-license` | 有 license 但非标准 SPDX | 读原文确认限制，别当成无 license |
| `suspect-list` | 仓库名或描述像清单、教程、面试合集 | 待 probe 确认 |
| `migrated-away` | 描述或 README 前 1500 字写着已迁移、停止维护 | reject。Gitee 没有 archived 字段，只能这样发现 |
| `gvp` | Gitee 最有价值项目，官方人工评选 | 加分信号，star 维度 4 分保底 |
| `mirror-of-upstream` | Gitee 的 mirrors/ 组织，是 GitHub 项目镜像 | 可当候选，但简历链接与 PR 要指向上游 |
| `renamed` | Gitee 静默解析了改名或转移，返回名与请求名不同 | 用返回名为准，报告里标注原名 |
| `not-a-product` | `product_form` 判为 library 或 infra | reject。库、SDK、引擎、中间件不是求职者要的项目 |
| `product-form-unknown` | 形态信号不足 | 保留但标注，人工读 README 定，别武断判库 |

probe 阶段补的结构信号：

| signal | 含义 |
|---|---|
| `docs-only-structure` | 根目录只有文档和素材，没有源码目录 |
| `looks-like-list` | README 是收录清单 |
| `no-run-path` | 没有可运行入口，gate 降为 source-reading |
| `weak-run-docs` | README 没有安装启动说明且没有 examples 目录 |
| `no-root-examples` | 根目录没有 examples/demo，注意是根级判断，子目录里可能有 |
| `nested-deps` | monorepo，依赖声明在子目录，已一并列出 |
| `readme-unavailable` | README 没拉下来，这个候选的判断不完整，要重跑 probe |

脚本给的是启发式信号，不是最终判决。你要读 probe 输出的 `readme_excerpt` 和 `readme_headings` 自己确认，尤其是 `looks-like-list` 和 `no-run-path`。误判的代价不对称：漏掉一个好项目只是少一个候选，推错一个教程项目会让用户在面试里踩坑。

`readme-unavailable` 出现时不要拿空 README 的判断当结论，重跑一次 probe。

限流注意：search 认证后 30 次/分钟，core 5000 次/小时。search 返回里已经带了 `archived`、`license`、`topics`、`pushed_at`，所以第一轮硬门槛在 search 阶段就完成，不额外花调用。probe 每个候选 2 到 4 次 core 调用。脚本遇到限流会明确报错并停下，看到限流就等重置或缩小范围，别循环重试。

## 第四步：六维打分

细则、阈值锚点和实测校准案例见 `references/scoring.md`。权重：

| 维度 | 字段 | 权重 | 数据来源 |
|---|---|---|---|
| AI 核心技术占比 | `ai_core` | 35% | 路径体积占比 + 依赖清单 AI 库，双信号 |
| 新颖性 | `novelty` | 10% | 同类项目检索计数 + 有无论文 + 当前做法 |
| 时效性 | `timeliness` | 15% | `timeliness_score` + 模型代际交叉验证 |
| 上手难度 | `onboarding` | 15% | 依赖声明 / Docker / examples / 安装文档 |
| Star 与社区认可 | `stars` | 10% | `star_score`，绝对值档与月增速档取高 |
| 业务真实性 | `business` | 15% | README 场景与难题 |

每一维必须给一句基于实际数据的证据，不能只给数字。用户要拿这些证据去面试，编出来的分数会在追问时崩掉。

**AI 占比这一维最容易出错，脚本给两个独立信号。** 路径体积占比会被目录命名带偏：核心代码放在 `serviette/` 这种产品名目录里会被低估，放在 `tradingagents/` 这种带 agent 的目录里会被高估。依赖清单信号不依赖命名。两者差 2 档以上时脚本标 `ai_band_divergence`，这时必须读 `top_other_paths` 和依赖列表人工定分，不要取平均糊过去。

新颖性和教程化都要实际 WebSearch，不能靠感觉。教程化的结果写进风险区，不进分数。

## 第五步：分档

- **A 档**：总分 4.0 以上，且业务真实性不低于 3 分
- **B 档**：总分 3.0 到 4.0
- **watch**：总分低于 3.0，或业务真实性 2 分以下
- **读源码档**：`no-run-path` 或 `stale` 命中的进这里，给阅读路径而不是部署步骤
- **淘汰区**：列出淘汰原因

业务真实性那条硬准入是必要的。AI 占比 35% 加新颖性 10% 等于 45%，权重集中在技术含量上，会系统性地把研究型和 demo 型仓库推上来，而这类业务场景弱，只靠 15% 权重压不住。

AI 占比 35% 也意味着纯业务系统基本出局：AI 核心 1 分的仓库业务真实性满分也补不回来。这是有意的，报告服务的是 AI 方向求职。

主推 3 到 5 个。达标不足 3 个就报实际数量，不要降标准凑数。淘汰区要留着，让用户知道筛掉了什么。

**主语言完全不考核**。语言不做门槛也不扣分，Rust、TypeScript、Go 的项目照样能进可跑档。报告里列出语言作为事实，但不影响分数和档位，上手难度也不含语言迁移成本。

## 第六步：出 HTML 报告

先把打分结果写成报告 JSON，数据结构在 `assets/report_template.html` 顶部注释里，然后渲染：

```bash
python3 scripts/render_report.py report.json report.html

# 两个平台分开跑，最后合成一份
python3 scripts/render_report.py github.json report.html --merge gitee.json
```

`--merge` 按 `platform + repo` 去重，后来的覆盖先前的，可以用重跑结果刷新旧条目。报告有平台筛选器和来源列，总览统计行给出 GitHub / Gitee 各多少个。

`--exclude 'owner/repo=原因'` 把候选移进淘汰区，可重复。用户说「不要这种」时用它，比手改几十 KB 的 JSON 稳：

```bash
python3 scripts/render_report.py report.json out.html \
  --exclude 'mem0ai/mem0=记忆层 SDK，不是产品型项目' \
  --exclude 'meilisearch/meilisearch=搜索引擎基础设施'
```

渲染脚本会拦住 `product_form` 为 library 或 infra 的候选留在主表，提示改用 `--exclude`。

`--skeletons file.json` 把骨架图贴到候选上，格式是 `{"owner/repo": {"lanes":[...], "caption":"...", "basis":"..."}}`。骨架单独一份文件，改图不用动分数：

```bash
python3 scripts/render_report.py report.json out.html --skeletons skeletons.json
```

渲染脚本会先校验数据：每个候选必须有 url、六维分数、六维依据、`summary`、`pitch`、至少 3 格 `core_tech`、至少 3 组 `challenges`，A 档必须满足准入，AI 占比 1 分的必须有一格标注核心缺失，`meta.searched_at` 必须在位。缺了会报错并列出缺哪一项，这是故意的，因为一份没有依据的打分表对用户没用。补齐再渲染，别用 `--allow-warnings` 绕过。

**报告 JSON 要把 probe 的实测字段一起搬过来**：`stars_per_month`、`days_since_push`、`releases_90d`、`signals`、`license`、`language`。这些是卡片元信息行和信号标签的数据源，漏了不报错但报告会显示破折号。

**教程化降档开关依赖 `tutorial_heavy`**。显式给布尔值最稳妥；不给时模板从 `risks` 里匹配「教程化严重」或「教程化极重」推断。写成别的措辞会推断不出来，开关静默失效，渲染脚本会拦这种写法。

用模板和脚本而不是每次手写 HTML，是为了保证排序、筛选、折叠这些交互稳定可用。报告是单文件，CSS 和 JS 内嵌，不引 CDN。用户的网络环境不确定，外部依赖会让报告打不开。

模板是仪表盘形态，左侧常驻导航切换六个视图：总览、候选详情、难点与解法、权重控制台、淘汰区、待核实。总览给主推 KPI、总分构成瀑布图、候选分布图和需要拍板的事项。权重控制台有六个滑块加两个档位开关，拖动即时重算总分、档位与排序，可以导出调整后的 JSON。所以数据里的分数只是初值，用户能自己改口径重排。两个开关的默认状态是业务准入**开**、教程化降档**关**，不要改。

**视觉语言不要退化**。暖灰画布托白卡、20px 圆角、20px 卡片间距、24 到 26px 内边距，这套留白是用户明确要的呼吸感。六个维度各有专属色并贯穿图表与标签，图表用斜纹区分未达标、实心区分达标，滚动条按报告配色自绘。完整设计契约、交互清单和验收方法见 `references/report-ui.md`，改模板前先读它。

每个候选卡片必须有六块内容，顺序固定：

- **一句话简介**（`summary`）：先说清这是什么项目，一个形态加做什么加品类
- **立项动机**（`pitch`）：业务场景和难题，放在简介之后
- **项目骨架**（`skeleton`）：2 到 5 条泳道的方格示意图，每格一个模块名加一句职责，泳道之间用箭头连成主干链路。模块名必须是真实目录或文件路径，`basis` 写清依据来自哪次 probe 的哪些字段
- **难点与项目解法**（`challenges`）：3 组左右分栏对照，左边难点写成具体工程困境，右边解法给出机制加可追溯来源。项目没解决的要如实写「仓库没有提供解法」
- **核心技术方格**（`core_tech`）：3 到 5 格分栏网格，每格一个技术点加一句解释，必须能追溯到文件路径、依赖库或 README 章节。AI 占比低的仓库要有一格明写 AI 核心缺失
- **风险**：依赖复杂度、付费门槛、停更状态、license 限制、教程化程度

上手路径（`onboarding`）放在折叠区，和六维打分依据并列，不占主版面。

渲染完要实际打开跑一遍验收，不能只看文件写成功。最低限度：控制台无报错、无横向溢出、无文字裁切、六个滑块和两个开关实测生效、筛选排序搜索可用、导出 JSON 内容正确、390 到 1440px 四档布局无破版。完整 playbook 和已知坑（隐藏页面里 CSS 过渡不推进、占位符重复注入白屏、雷达图标签裁切）见 `references/report-ui.md`。截图拿不到时改用几何测量，并如实说明这一轮是结构验证不是看图。

写文件到会话工作目录，给用户绝对路径链接。报告 JSON 一起留着，用户想调分数或补候选时改 JSON 重渲染就行。

## 诚实边界

所有 star、更新时间、license 来自实际 API 调用，报告里标注检索时间。没核实成功的候选单独放待核实区，不混进主表。不承诺某个项目能拿到 offer。README 里读不出业务场景的，业务真实性就打低分并说明读不出，不要替它编一个场景。

## 后续链路是可选的

报告末尾给三个入口：读源码路径、改造方向、简历句式。用户点哪个再展开哪个。

这个技能的职责到「找到并说清为什么值得做」为止。默认展开落地细节会让报告臃肿，而且用户可能只是想先看看有哪些选择。
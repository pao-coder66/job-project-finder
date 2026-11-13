# 检索配方

## 目录

- [已核实的查询限定词](#已核实的查询限定词)
- [按方向的查询式](#按方向的查询式)
- [awesome-list 入口](#awesome-list-入口)
- [公网检索式](#公网检索式)
- [限流预算](#限流预算)
- [Gitee 检索](#gitee-检索)
- [一个真实的误判案例](#一个真实的误判案例)

## 已核实的查询限定词

下面这些都在 GitHub Search API 上实际跑通过，可以放心组合：

| 限定词 | 作用 |
|---|---|
| `topic:agent` | 按仓库 topic 过滤，比全文匹配准 |
| `stars:>1000` | star 下限 |
| `pushed:>2026-05-01` | 最近推送时间下限，用来排除停更 |
| `language:python` | 主语言 |
| `archived:false` | 排除归档仓库 |
| `template:false` | 排除模板仓库 |
| `fork:false` | 排除 fork |
| `license:apache-2.0` | 按 license 过滤，用 SPDX 标识 |
| `good-first-issues:>3` | 有新手友好 issue，适合想提 PR 的场景 |
| `size:>200` | 仓库体积下限 KB，用来排掉空壳和纯文档 |
| `in:readme,description` | 把关键词匹配范围扩到 README |

`archived:false` 和 `template:false` 放进查询式能在检索阶段就省掉一批无效候选，比拉回来再筛划算。

注意 `size` 只能排掉极小的仓库，排不掉文档仓库。一个几万 star 的面试指南体积也可能很大。

## 按方向的查询式

每个方向给 3 到 4 个查询式，一起跑再合并去重。单个查询式的覆盖面往往不够，topic 命名在不同社区差异很大。

**Agent / LLM 应用**

```
topic:agent stars:>800 pushed:>2026-05-01 archived:false template:false
topic:llm-agent stars:>500 pushed:>2026-05-01 archived:false
topic:multi-agent stars:>300 pushed:>2026-05-01 archived:false
agent workflow in:readme,description stars:>500 pushed:>2026-05-01 language:python archived:false
```

**检索 / RAG / 搜索**

```
topic:rag stars:>1000 pushed:>2026-05-01 archived:false
topic:search-engine stars:>500 pushed:>2026-05-01 archived:false
topic:vector-database stars:>800 pushed:>2026-05-01 archived:false
hybrid retrieval rerank in:readme,description stars:>300 pushed:>2026-01-01 archived:false
```

**电商 / 交易 / 供应链业务系统**

```
ecommerce in:readme,description stars:>1000 pushed:>2026-01-01 archived:false size:>500
topic:ecommerce stars:>500 pushed:>2026-01-01 archived:false
topic:order-management stars:>100 pushed:>2025-11-01 archived:false
inventory pricing in:readme,description stars:>200 pushed:>2026-01-01 archived:false
```

**后端 / 分布式 / 中间件**

```
topic:distributed-systems stars:>1000 pushed:>2026-05-01 archived:false
topic:message-queue stars:>800 pushed:>2026-05-01 archived:false
topic:rpc stars:>500 pushed:>2026-05-01 archived:false
consensus raft in:readme,description stars:>300 pushed:>2026-01-01 archived:false
```

**数据 / 平台工程**

```
topic:data-pipeline stars:>500 pushed:>2026-05-01 archived:false
topic:etl stars:>500 pushed:>2026-05-01 archived:false
topic:observability stars:>800 pushed:>2026-05-01 archived:false
```

**前端 / 全栈**

```
topic:dashboard stars:>1000 pushed:>2026-05-01 archived:false template:false
topic:admin-dashboard stars:>500 pushed:>2026-05-01 archived:false template:false
```

前端方向要格外小心差异化这一维，`clone` 类仓库大量集中在这里。查询式里加 `template:false` 能挡掉一部分，但挡不掉教程化的 clone 项目，还得靠差异化检索。

方向不在上面这些里的时候，按同样的模式自己配：一个 topic 式，一个近义 topic 式，一个 `in:readme,description` 的领域词全文式。全文式用来捞 topic 没打全的项目。

## awesome-list 入口

这些清单人工筛过，质量高于裸 star 排序。用 WebSearch 找到清单页，从里面抽仓库名进候选池，然后照样走核实流程。清单里的项目也会停更和归档。

搜索式：`awesome {方向} github`、`{方向} 开源项目 汇总 github`。

清单本身不是项目，别把它当候选放进主表。

## 公网检索式

GitHub 检索式捞不到的东西走这条路，尤其是中文技术圈的业务型项目和论文配套代码。

```
{方向} 实战 项目 开源 完整
{方向} 系统 设计 开源 实现 github
{论文标题或方法名} official implementation github
{方向} 从0到1 项目 github 业务
```

搜到的仓库名一律回到 probe 核实，公网文章里的 star 数经常是写作时的旧值。

## 限流预算

认证后 search 30 次/分钟，core 5000 次/小时。实际跑一次完整流程的开销大致是：

- 建池：4 到 6 个查询式，每个 1 次 search 调用，翻页的话乘以页数
- 硬门槛第一轮：0 次额外调用，search 返回里已带 `archived`、`license`、`topics`、`pushed_at`、`is_template`、`fork`
- probe：每个入围候选 2 次 core 调用（README + 根目录），20 个候选就是 40 次

所以 probe 只对入围候选跑，别对全池跑。脚本遇到 403 会明确报限流并停下，不会静默重试，看到限流就等重置或者缩小候选范围。

## Gitee 检索

Gitee 值得单独跑一轮。中文业务项目、国内厂商开源、以及一批只在 Gitee 托管的实战项目，GitHub 检索式覆盖不到。但它和 GitHub 有五个实质差异，全部实测过，照搬 GitHub 的做法会出错。

### 一、search 需要 token，没 token 是静默空结果

```bash
export GITEE_TOKEN=...   # gitee.com/profile/personal_access_tokens 建只读 token
python3 scripts/gh_scout.py search --platform gitee -q '智能体' --limit 20 --out ge-pool.json
```

无 token 时 `GET /api/v5/search/repositories` 返回 **HTTP 200 加空数组**，不是 401。带无效 token 才返回 401。这意味着不检查就会得出「Gitee 上没有项目」的错误结论。脚本在缺 token 时直接拒绝执行并给出两条出路，不会让你拿着空池子往下走。

没 token 也能干活，走第二条路：用 WebSearch 或 awesome 清单拿到仓库名，再按名核实，这条路免认证。

```bash
python3 scripts/gh_scout.py probe --platform gitee --repos owner/a,owner/b --out ge.json
```

### 二、查询语法不通用

Gitee 的 `q` 是关键词，不是 GitHub 的限定词语法。`topic:rag stars:>1000 pushed:>2026-01-01` 这种串在 Gitee 上不适用。筛选走独立参数：`language`、`owner`、`sort`（`stars_count` / `forks_count` / `watches_count`）、`order`、`fork`、`page`、`per_page`。脚本已经按这套映射好了，你只需要给中文或英文关键词。

本轮没有配 token，带 token 的检索行为未实测。第一次跑先用小 `--limit` 确认返回非空，再放量。

关键词配方，中文优先：

```
智能体    大模型    知识库    检索增强    向量检索
电商 搜索    推荐系统    风控    客服机器人
工作流 引擎    低代码    数据同步
```

### 三、限流只有 60 次/小时

响应头 `X-RateLimit-Limit: 60`，是 GitHub core 5000 的八十分之一。probe 一个仓库要 3 到 5 次调用，也就是一小时只够十个仓库左右。

脚本读这个头并在剩余 ≤3 时告警、≤4 时跳过后续仓库记入待核实区，不会把额度耗光后半途崩掉。开跑前还会按 `仓库数 × 4` 估算并预警。额度紧张时加 `--no-tree` 省掉 trees 那一次，代价是算不出 AI 占比。

### 四、没有 archived，迁移只能读正文

Gitee 没有 `archived` 字段，`status` 是 issue 开关不是归档状态。项目搬走或停维护只能从 description 和 README 读。

实测案例：`mindspore/mindspore` 的 README 首行写着「本项目已经正式迁移至 AtomGit 平台」，但 API 元数据一切正常，star 9075、license Apache-2.0、语言 Python。脚本用 `migrated-away` 正则扫 description 和 README 前 1500 字，命中直接 reject。这条规则对 GitHub 同样生效。

### 五、star 量级小一个数量级，还有两个特有字段

实测对照：hutool 24357、rt-thread 5568、Paddle 4479、CowAgent 665。套 GitHub 阈值会把 Paddle 这种主力框架压到 3 分。所以 star 档位按平台分开，见 `scoring.md`。

两个 Gitee 特有字段脚本已经带上：

- **`gvp`**：Gitee 最有价值项目，官方人工评选。Paddle、hutool、rt-thread 都是 true。给 star 维度 4 分保底，报告里显示绿色 GVP 标签。
- **`mirrors/` 组织**：是 GitHub 项目的镜像副本，不是原创托管地。脚本打 `mirror-of-upstream` 标记。当候选可以，但简历链接和提 PR 都要指向上游。

还有一个坑：**Gitee 会静默解析改名和转移**。请求 `zhayujie/chatgpt-on-wechat` 返回的是 `zhayujie/CowAgent`，请求 `dromara/hutool` 返回 `chinabugotech/hutool`。脚本用返回的 `full_name` 为准并打 `renamed` 标记，报告里显示原名。另外 `html_url` 带 `.git` 后缀，脚本已去掉，直接搬进报告会点不开。

### 合并两个平台的报告

两边分开跑，最后合成一份：

```bash
python3 scripts/render_report.py github-report.json final.html --merge gitee-report.json
```

按 `platform + repo` 去重，后来的覆盖先前的，方便用重跑结果刷新旧条目。报告里有平台筛选器和来源列，总览的统计行给出 GitHub / Gitee 各多少个。

## 一个真实的误判案例

下面三个仓库都实际核实过，它们的元数据全部合格，但性质完全不同。这组对照能帮你校准判断。

**`Snailclimb/JavaGuide`**，约 15.9 万 star。能被 `topic:agent stars:>500 archived:false template:false license:apache-2.0` 搜出来，未归档、非模板、有 Apache-2.0，根目录还有 `package.json`。但根目录只有 `docs/`、`media/`、`scripts/`、`.husky/`，那个 `package.json` 是文档站点的 markdownlint 和 prettier 工具链。README 链接行占比 0.71，但 `github.com` 外链只有 4 个，因为链接全指向站内相对路径。它是 Java 面试指南，不是项目。

这个案例说明两件事：有依赖声明文件不代表是可运行项目；只数 github 外链会漏掉链接站内的文档仓库。

**`avelino/awesome-go`**，约 19.3 万 star。有 `go.mod` 和 `pkg/` 目录，看起来是个 Go 项目。那些代码是生成清单网站的工具，仓库本体是 README 里 2974 个 github 外链。判断清单要看 README 收录了多少外部仓库，不是看有没有代码目录。

**`open-webui/open-webui`**，约 15.8 万 star。README 链接行占比 0.54，比 JavaGuide 低不了多少，光看密度会被误判成清单。但它有 `backend/`、`src/`、`pyproject.toml`、`package.json` 和 9 个 docker-compose 文件，是实打实能跑起来的应用。它的 license 是自定义协议，SPDX 返回 `NOASSERTION`,属于 `custom-license` 而不是 `no-license`，风险描述要读原文再写。

所以判清单的强信号是大量 github 外链或者纯文档结构，不是链接密度单独一项。判可运行要看源码目录加依赖声明，monorepo 的依赖在子目录，根目录查不到不等于没有。
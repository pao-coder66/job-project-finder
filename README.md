# job-project-finder

Qoder Skill。给求职者和转行者从 GitHub 与 Gitee 找能写进简历、面试讲得清的项目，核实真实元数据后六维打分，输出单文件 HTML 报告。

## 解决什么问题

GitHub 上有几十万个仓库，求职者缺的不是数量。真正的失败模式是挑到教程项目：跟着 README 敲一遍，跑通了，写进简历，面试官第一个问题就问穿。

所以这个技能的主要工作是淘汰和核实。判断标准就一句话：能不能说清「谁在什么场景下遇到什么难题，这个项目怎么解，为什么这么解而不是那么解」。说不清的，star 再高也不推。

库、SDK、框架、引擎、中间件一律不进候选，哪怕分数很高。判据是有没有面向最终用户的交互面，不是名字里有没有 agent。有管理台不等于有产品。

## 安装

```bash
git clone https://github.com/pao-coder66/job-project-finder.git \
  ~/.qoder/skills/job-project-finder
```

## 前置条件

- Python 3，只用标准库，不需要 pip
- GitHub：`gh auth login`，或环境变量 `GITHUB_TOKEN` / `GH_TOKEN`。两者都没有会报错停下，不会凭记忆填元数据
- Gitee：`GITEE_TOKEN` 仅在跑 Gitee 检索时必需。按名核实免认证，但只有 60 次/小时

## 用法

在 Qoder 里说「帮我找几个能写进简历的项目」「有什么值得读源码的 Agent 项目」即可触发。技能会先问齐四项求职坐标（目标岗位、技术栈、时间预算、环境约束），再开工。

也可以直接跑脚本：

```bash
S=~/.qoder/skills/job-project-finder/scripts

# 1 多轮检索建池。词从上一轮结果动态生成，不是预设词典
python3 $S/search_agent.py init --role 'AI Agent 应用工程师' \
  --domain 电商搜索 --stack python --state s.json
python3 $S/search_agent.py plan --state s.json --kind seed
python3 $S/search_agent.py run  --state s.json --sources github --limit 15
python3 $S/search_agent.py export --state s.json --out pool.json

# 2 核实真实元数据与 README，输出硬门槛信号
python3 $S/gh_scout.py probe --from pool.json --top 20 --out probed.json

# 3 打分结果写成报告 JSON 后渲染
python3 $S/render_report.py report.json report.html --skeletons skeletons.json
```

`search_agent.py` 还有 `wide` / `deep` 轮次、`propose` 回灌自造词、`ingest-web` 吸收 WebSearch 结果、`status` 看饱和曲线。完整策略见 `references/search-agent.md`。

## 打分口径

| 维度 | 权重 | 看什么 |
|---|---|---|
| AI 核心技术占比 | 35% | 路径体积占比 + 依赖清单 AI 库计数，双信号 |
| 时效性 | 15% | 最近推送、近 90 天 release、模型代际 |
| 上手难度 | 15% | 依赖声明、Docker、examples、安装文档 |
| 业务真实性 | 15% | README 里的场景与难题 |
| 新颖性 | 10% | 同类项目计数、有无论文、当前做法 |
| Star 与社区认可 | 10% | 绝对值档与月增速档取高，GitHub / Gitee 分平台定档 |

A 档要求总分 4.0 以上且业务真实性不低于 3 分。主语言不考核，Rust、Go、TypeScript 照样能进可跑档。

AI 占比双信号差 2 档以上会标 `ai_band_divergence`，这时要人工读目录和依赖定分，不取平均。

## 输出

单文件 HTML，CSS 和 JS 内嵌，不引 CDN，离线可开。左侧导航切七个视图：总览、候选详情、难点与解法、检索过程、权重控制台、淘汰区、待核实。

每个候选卡片固定六块：一句话简介、立项动机、项目骨架图、难点与项目解法、核心技术方格、风险。权重控制台六个滑块拖动即时重算档位与排序，可导出调整后的 JSON。

渲染前会校验数据完整性，缺 url、缺六维依据、缺骨架依据都会报错列出缺项。

## 目录

```
SKILL.md                      技能主流程
scripts/search_agent.py       多轮检索编排、动态造词、IDF 打分、覆盖度
scripts/gh_scout.py           GitHub / Gitee 检索与核实、硬门槛信号
scripts/render_report.py      数据校验与报告渲染
assets/report_template.html   报告模板
references/scoring.md         六维锚点、产品形态门槛、实测校准
references/search-agent.md    造词路径、质量控制、停止条件
references/search-recipes.md  已验证的查询式与平台差异
references/report-ui.md       设计契约、交互清单、已知坑
```

## 边界

所有 star、更新时间、license 来自实际 API 调用，报告标注检索时间。没核实成功的候选单独放待核实区。README 里读不出业务场景的就打低分并说明，不替它编一个。

职责到「找到并说清为什么值得做」为止。读源码路径、改造方向、简历句式是报告末尾的可选入口，不默认展开。
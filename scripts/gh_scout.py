#!/usr/bin/env python3
"""GitHub / Gitee 项目检索与核实。

search  按查询式建候选池，用返回的元数据先过一轮硬门槛。
probe   对入围候选拉 README 和根目录结构，输出上手难度与文档仓库的判断信号。

两个平台的记录归一到同一形状，下游打分与报告不用区分。平台差异见 references/search-recipes.md。

脚本只给信号，不下最终判决。looks-like-list 和 no-run-path 这两类判断
需要读 README 摘要人工确认，误判代价不对称。

用法:
  python3 gh_scout.py search -q 'topic:rag stars:>1000' --out pool.json
  python3 gh_scout.py search --platform gitee -q '智能体' --out ge-pool.json   # 需 GITEE_TOKEN
  python3 gh_scout.py probe --from pool.json --top 20 --out probed.json
  python3 gh_scout.py probe --platform gitee --repos owner/a,owner/b --out ge.json

GitHub 依赖 gh CLI（已登录）或 GITHUB_TOKEN。
Gitee 的 search 必须要 GITEE_TOKEN；按名 probe 免认证，但只有 60 次/小时。
"""

import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
STALE_DAYS = 365

LIST_NAME_RE = re.compile(
    r"(^|[-_/])(awesome|tutorial|tutorials|roadmap|handbook|cheatsheet|"
    r"interview|interviews|study|learning|books|papers)([-_/]|$)",
    re.I,
)
LIST_DESC_RE = re.compile(
    r"(面试|教程|学习路线|学习指南|资料合集|知识总结|awesome|curated list|"
    r"a collection of|list of|roadmap|interview questions)",
    re.I,
)

DEP_MANIFESTS = (
    "pyproject.toml", "setup.py", "setup.cfg", "package.json", "go.mod",
    "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile",
    "composer.json", "mix.exs", "Podfile", "CMakeLists.txt", "Makefile",
)
DEP_MANIFEST_RE = re.compile(r"^(requirements.*\.txt|.*\.csproj|.*\.sln|Pipfile)$", re.I)
DOCKER_RE = re.compile(r"^(Dockerfile.*|docker-compose.*\.ya?ml|compose\.ya?ml)$", re.I)

SOURCE_DIRS = {
    "src", "source", "app", "apps", "lib", "libs", "pkg", "internal", "cmd",
    "core", "server", "backend", "frontend", "api", "service", "services",
    "modules", "packages", "engine", "runtime", "client", "web", "ui",
}
DOC_DIRS = {
    "docs", "doc", "documentation", "media", "assets", "asset", "images",
    "img", "image", "static", "book", "website", "site", "scripts", "script",
    ".github", ".vscode", ".idea", "public", "figures", "figs", "examples_doc",
}
EXAMPLE_DIRS = {"examples", "example", "demo", "demos", "samples", "sample", "notebooks"}

# ---- 产品形态判定 -------------------------------------------------------
# 求职者要的是「代表某类产品的 Agent 项目」，不是库、SDK、引擎、中间件。
# mem0 是记忆层 SDK，meilisearch 是搜索引擎，这两个都不该进候选，
# 哪怕它们分数不低。判据是有没有面向最终用户的交互面，而不是名字里有没有 agent。
UI_DIRS = {
    "web", "frontend", "front-end", "ui", "desktop", "client", "app", "apps",
    "mobile", "ios", "android", "miniprogram", "channel", "channels", "bot",
    "dashboard", "admin", "extension", "webapp", "portal", "console",
}
# 管理台不算产品界面。mem0 有 76 个 tsx，但全在 server/dashboard 下，
# 那是给运维看记忆条目的后台，主用法仍然是 from mem0 import。
STRONG_UI_DIRS = {
    "web", "frontend", "front-end", "ui", "desktop", "client", "app", "apps",
    "mobile", "ios", "android", "miniprogram", "channel", "channels", "bot",
    "extension", "webapp", "portal",
}
ADMIN_UI_RE = re.compile(r"(^|/)(dashboard|admin|console|ops|setup|auth|login|backoffice)(/|$)", re.I)
UI_EXT = {".vue", ".svelte", ".tsx", ".jsx", ".html", ".htm"}
# README 里出现这些说明起完服务是人去用，不是 import 进代码
PRODUCT_README_RE = re.compile(
    r"(localhost:\d+|127\.0\.0\.1:\d+|open\s+(your\s+)?browser|web\s*ui|webui|"
    r"chat\s+at|访问\s*http|浏览器打开|打开浏览器|控制台|dashboard|演示视频|"
    r"watch the demo|live demo|try online|screenshot)",
    re.I,
)
# 这些说明它是被 import 的对象或给开发者用的底座
LIBRARY_README_RE = re.compile(
    r"(^\s*(from\s+\w[\w.]*\s+import|import\s+\w)|library\s*\(pip|"
    r"\bpip\s+install\s+\S+\s*\n+\s*(from|import)|\bSDK\b|drop-in|"
    r"middleware|memory layer|infrastructure for|building block)",
    re.I | re.M,
)
INFRA_NAME_RE = re.compile(
    r"(engine|gateway|proxy|broker|database|\bdb\b|middleware|toolkit|"
    r"\bsdk\b|\blib\b|framework|runtime|scheduler|orchestrator)", re.I,
)
DELIVERABLE_RE = re.compile(
    r"(report|报告|导出|export|生成文档|产出|deliverable|html page|pdf)", re.I,
)


def product_form(rec, dirs, files, readme_text, ui_file_count, ui_paths, has_cli):
    """判产品形态。返回 (form, evidence)。

    form 取 product / library / infra / unknown。unknown 不自动淘汰，
    交人工读 README 定，误杀一个真产品比漏掉一个库代价更大。
    """
    name = (rec.get("name") or "").lower()
    desc = rec.get("description") or ""
    ui_dirs = [d for d in dirs if d.lower() in UI_DIRS]
    strong_dirs = [d for d in ui_dirs if d.lower() in STRONG_UI_DIRS]
    admin_dirs = [d for d in ui_dirs if d.lower() not in STRONG_UI_DIRS]
    # ui_paths 传进来的是 "path (size)" 字符串，取路径部分判是否全在管理台下
    raw_paths = [p.rsplit(" (", 1)[0] for p in ui_paths]
    end_user_files = [p for p in raw_paths if not ADMIN_UI_RE.search(p)]
    prod_hits = sorted({m.group(0).lower() for m in PRODUCT_README_RE.finditer(readme_text)})[:6]
    lib_hits = sorted({m.group(0).strip().lower() for m in LIBRARY_README_RE.finditer(readme_text)})[:6]
    infra_name = bool(INFRA_NAME_RE.search(name) or INFRA_NAME_RE.search(desc))
    deliverable = bool(has_cli and DELIVERABLE_RE.search(readme_text))
    lib_strong = any(
        k in " ".join(lib_hits)
        for k in ("import", "library (pip", "sdk", "drop-in", "memory layer",
                  "infrastructure for", "building block", "middleware")
    )
    strong_surface = bool(strong_dirs) or len(end_user_files) >= 3

    ev = {
        "end_user_ui_dirs": strong_dirs,
        "admin_only_ui_dirs": admin_dirs,
        "ui_file_count": ui_file_count,
        "end_user_ui_files": len(end_user_files),
        "top_ui_paths": ui_paths[:5],
        "product_readme_hits": prod_hits,
        "library_readme_hits": lib_hits,
        "infra_name_match": infra_name,
        "cli_with_deliverable": deliverable,
    }
    # CLI 产出人类可读交付物优先于库信号。TradingAgents 同时暴露 Python API
    # 和 CLI，但 CLI 产出研究报告与回测，那是垂直应用不是库。
    if deliverable:
        ev["reason"] = "CLI 产出人类可读交付物，属垂直应用而非库"
        return "product", ev
    # 库信号优先于管理台。有后台不等于有产品，主用法是 import 就是库。
    if lib_strong and not strong_surface:
        # 但自称 platform/product/system 且带管理台的，交人工定，别武断判库。
        if (admin_dirs or ui_file_count > 0) and re.search(
                r"(\bplatform\b|\bproduct\b|\bapplication\b|\bapp\b|\bsystem\b|平台|系统|产品)",
                desc, re.I):
            ev["reason"] = "有 import 用法也有管理台，且自称平台或产品，形态需人工确认"
            return "unknown", ev
        ev["reason"] = "README 主用法是 import 进代码，前端只有管理台，没有终端用户界面"
        return "library", ev
    if strong_surface:
        ev["reason"] = "有面向最终用户的界面（产品级 UI 目录或非管理台前端代码）"
        return "product", ev
    if prod_hits and not lib_strong:
        ev["reason"] = "README 指向可访问的服务界面"
        return "product", ev
    if infra_name:
        ev["reason"] = "名字与描述指向引擎或底座，且没有终端交互面"
        return "infra", ev
    if lib_strong:
        ev["reason"] = "README 主用法是 import 进代码"
        return "library", ev
    ev["reason"] = "信号不足，需人工读 README 定形态"
    return "unknown", ev

INSTALL_RE = re.compile(
    r"(quick\s*start|quickstart|getting\s*started|installation|install\b|"
    r"how\s*to\s*run|usage|run\s*the|docker\s*compose\s*up|pip\s*install|"
    r"npm\s*(install|run)|快速开始|快速上手|安装|运行|部署|启动)",
    re.I,
)
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
BADGE_LINK_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"<[^>]{1,400}>")
BARE_URL_RE = re.compile(r"^https?://\S+$")


def clean_readme(text):
    """剥掉徽章、logo、HTML 包装，留下正文。

    很多高星仓库 README 前三千字全是 badge 和居中 logo 的 HTML，
    直接截断会取不到任何实质内容，打分就没依据了。
    """
    t = HTML_COMMENT_RE.sub(" ", text)
    t = BADGE_LINK_RE.sub(" ", t)
    t = MD_IMG_RE.sub(" ", t)
    t = HTML_TAG_RE.sub(" ", t)
    out = []
    for ln in t.splitlines():
        ln = ln.strip()
        if not ln or BARE_URL_RE.match(ln):
            continue
        if not re.search(r"[\w\u4e00-\u9fff]", ln):
            continue
        if re.fullmatch(r"[\s|·\-–—=*_#>]+", ln):
            continue
        out.append(ln)
    return "\n".join(out)


class ScoutError(Exception):
    pass


# ---- AI 核心技术占比 ----------------------------------------------------
# 长 token 用子串匹配，短 token 必须加词边界：
# 不加边界的话 storage / average / fragment 都会命中 "rag"。
AI_LONG = (
    "agent", "prompt", "retriev", "rerank", "embed", "vector", "chunk",
    "memory", "planner", "orchestr", "inference", "finetune", "fine-tune",
    "lora", "tokenizer", "openai", "anthropic", "langchain", "langgraph",
    "transformers", "semantic", "knowledge", "chatbot", "copilot",
    "evaluation", "benchmark",
)
AI_SHORT_RE = re.compile(r"(?<![a-z])(rag|llm|llms|sft|nlp|eval|evals|mcp)(?![a-z])")
AI_LONG_RE = re.compile("|".join(re.escape(t) for t in AI_LONG))

CODE_EXT = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
    ".java", ".kt", ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".swift", ".scala", ".vue", ".svelte", ".sh", ".sql", ".ipynb", ".css",
    ".scss", ".html",
}
# 文档、测试、示例、benchmark、i18n、vendored 与生成产物都不计入分母。
# 不排除的话，一个 pdf.worker.min.js 就能有 1MB，20 个语言包又能堆几 MB，
# 真正的自研 AI 代码会被彻底淹没。
SKIP_PATH_RE = re.compile(
    r"(^|/)(node_modules|vendor|third_party|external-crates|deps|dist|build|"
    r"\.git|docs?|documentation|website|site|assets?|media|images?|img|"
    r"examples?|demos?|samples?|tests?|testing|__pycache__|migrations?|"
    r"benchmarks?|bench|locales?|i18n|translations?|public|static|"
    r"pdfjs-dist|iconfont|generated|gen)(/|$)",
    re.I,
)
# 文件级排除：压缩产物、各语言的测试文件命名、protobuf 等生成代码。
SKIP_FILE_RE = re.compile(
    r"(\.min\.(js|css|mjs)$|_test\.go$|(^|/)test_[^/]*\.py$|_test\.py$|"
    r"\.(spec|test)\.(ts|tsx|js|jsx|mjs)$|\.pb\.go$|_pb2(_grpc)?\.py$|"
    r"\.generated\.[a-z]+$|\.d\.ts$|(^|/)conftest\.py$)",
    re.I,
)

# 依赖清单里的 AI 库。这个信号不依赖目录命名，用来交叉校验路径占比。
AI_DEP_RE = re.compile(
    r"(?i)\b(openai|anthropic|langchain|langgraph|langchaingo|llama[-_]index|"
    r"transformers|torch|tensorflow|sentence[-_]transformers|faiss|chromadb|"
    r"qdrant|weaviate|milvus|pymilvus|pgvector|tiktoken|litellm|dspy|autogen|"
    r"crewai|agentscope|vllm|sglang|peft|trl|accelerate|ragas|trulens|deepeval|"
    r"ollama|huggingface|cohere|mistralai|google[-_]generativeai|google[-_]genai|"
    r"dashscope|zhipuai|modelscope|unstructured|docling|pypdf|marker|rapidocr|"
    r"paddleocr|whisper|diffusers|candle|tokenizers|rig[-_]core|eino)\b"
)
MANIFEST_PRIORITY = (
    "requirements.txt", "pyproject.toml", "package.json", "go.mod",
    "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json",
)


def ai_dependencies(repo, root_files, platform="github"):
    """读依赖清单找 AI 库。路径占比会被命名习惯带偏，这个信号不会。"""
    present = [m for m in MANIFEST_PRIORITY if m in root_files]
    present += [f for f in root_files if DEP_MANIFEST_RE.match(f) and f not in present]
    found, read = set(), []
    for name in present[:3]:
        try:
            data = pget(platform, f"/repos/{repo}/contents/{urllib.parse.quote(name)}")
        except ScoutError:
            continue
        if not isinstance(data, dict) or data.get("encoding") != "base64" or not data.get("content"):
            continue
        text = base64.b64decode(data["content"]).decode("utf-8", "ignore")
        read.append(name)
        found.update(m.group(0).lower() for m in AI_DEP_RE.finditer(text))
    deps = sorted(found)
    n = len(deps)
    band = 5 if n >= 6 else 4 if n >= 3 else 3 if n >= 1 else 1
    return {"ai_deps": deps, "ai_dep_count": n, "ai_dep_band": band, "manifests_read": read}


def is_ai_path(path):
    low = path.lower()
    return bool(AI_LONG_RE.search(low) or AI_SHORT_RE.search(low))


def ai_share(repo, branch, platform="github"):
    """用 git trees 递归拿全量文件体积，估算 AI 核心代码占比。

    返回的是估算，不是精确测量。top_ai_paths 和 top_other_paths 一并给出，
    方便人工复核归类是否合理。trees 被截断时明确标注，不要拿截断结果当定论。
    GitHub 与 Gitee v5 的 trees 响应形状一致，同一套归类逻辑通用。
    """
    data = pget(platform, f"/repos/{repo}/git/trees/{urllib.parse.quote(branch or 'HEAD')}",
                {"recursive": 1})
    tree = data.get("tree") or []
    ai_bytes = other_bytes = 0
    ai_paths, other_paths = [], []
    ui_paths = []
    skipped = 0
    for e in tree:
        if e.get("type") != "blob":
            continue
        p = e.get("path") or ""
        ext = os.path.splitext(p)[1].lower()
        if ext not in CODE_EXT:
            continue
        if SKIP_PATH_RE.search(p) or SKIP_FILE_RE.search(p):
            skipped += 1
            continue
        size = e.get("size") or 0
        if ext in UI_EXT:
            ui_paths.append((p, size))
        if is_ai_path(p):
            ai_bytes += size
            ai_paths.append((p, size))
        else:
            other_bytes += size
            other_paths.append((p, size))
    total = ai_bytes + other_bytes
    share = round(ai_bytes / total, 3) if total else None
    ai_paths.sort(key=lambda x: -x[1])
    other_paths.sort(key=lambda x: -x[1])
    ui_paths.sort(key=lambda x: -x[1])
    # 命名偏差自查：核心代码常放在以产品命名的目录里（serviette/、ragflow/），
    # 路径里没有 AI 词就会被误归到 other。这种情况占比会被系统性低估。
    short = repo.split("/")[-1].lower().replace("-", "").replace("_", "")
    norm = lambda s: s.lower().replace("-", "").replace("_", "")
    prod_other = sum(s for p, s in other_paths if norm(p.split("/")[0]) == short)
    return {
        "ai_code_bytes": ai_bytes,
        "other_code_bytes": other_bytes,
        "total_code_bytes": total,
        "ai_share": share,
        "ai_share_score": share_band(share),
        "tree_truncated": bool(data.get("truncated")),
        "code_files_counted": len(ai_paths) + len(other_paths),
        "code_files_skipped": skipped,
        "product_dir_other_bytes": prod_other,
        "naming_bias_warning": bool(other_bytes and prod_other / other_bytes > 0.4),
        "top_ai_paths": [f"{p} ({s})" for p, s in ai_paths[:8]],
        "top_other_paths": [f"{p} ({s})" for p, s in other_paths[:8]],
        "ui_file_count": len(ui_paths),
        "top_ui_paths": [f"{p} ({s})" for p, s in ui_paths[:8]],
    }


def share_band(share):
    if share is None:
        return None
    if share > 0.60:
        return 5
    if share >= 0.40:
        return 4
    if share >= 0.20:
        return 3
    if share >= 0.05:
        return 2
    return 1


def star_bands(stars, created_at, platform="github", gvp=False):
    """绝对值档与月增速档取较高者。

    只看绝对值会让新项目最高只能拿 1 到 2 分，和新颖性那一维直接对冲。

    Gitee 的 star 量级比 GitHub 小一个数量级：hutool 2.4 万已是头部，
    Paddle 4.5 千、rt-thread 5.6 千都是各自领域的主力项目。套用 GitHub 阈值
    会把它们全压到 3 分以下，所以按平台分别定档。GVP（Gitee 最有价值项目）
    是官方人工评选，给 4 分保底。
    """
    if platform == "gitee":
        a = 5 if stars > 8000 else 4 if stars >= 2000 else 3 if stars >= 500 else 2 if stars >= 100 else 1
        gcut = (300, 80, 20, 5)
    else:
        a = 5 if stars > 50000 else 4 if stars >= 10000 else 3 if stars >= 3000 else 2 if stars >= 500 else 1
        gcut = (2000, 500, 100, 20)
    age_days = days_since(created_at)
    months = max(1.0, (age_days or 0) / 30.44)
    spm = round(stars / months) if age_days else None
    if spm is None:
        g = None
    else:
        g = 5 if spm > gcut[0] else 4 if spm >= gcut[1] else 3 if spm >= gcut[2] else 2 if spm >= gcut[3] else 1
    best = max(a, g) if g is not None else a
    if platform == "gitee" and gvp:
        best = max(best, 4)
    return {
        "stars_per_month": spm,
        "repo_age_months": round(months, 1) if age_days else None,
        "star_band_absolute": a,
        "star_band_growth": g,
        "star_score": best,
        "star_scale": platform,
    }


def timeliness_band(days_since_push, releases_90d):
    if days_since_push is None:
        return None
    if days_since_push <= 30 and (releases_90d or 0) > 0:
        return 5
    if days_since_push <= 90:
        return 4
    if days_since_push <= 180:
        return 3
    if days_since_push <= 365:
        return 2
    return 1


def releases_recent(repo, days=90, platform="github"):
    try:
        rel = pget(platform, f"/repos/{repo}/releases", {"per_page": 30})
    except ScoutError:
        return None, []
    if not isinstance(rel, list):
        return None, []
    recent = []
    for r in rel:
        d = days_since(r.get("published_at") or r.get("created_at"))
        if d is not None and d <= days:
            recent.append({"tag": r.get("tag_name"),
                           "published_at": r.get("published_at") or r.get("created_at")})
    return len(recent), recent[:5]


def _has_gh():
    return shutil.which("gh") is not None


def api_get(path, params=None, _retry=True):
    """取一个 GitHub API 端点。优先 gh CLI，回退 GITHUB_TOKEN。

    限流和 404 直接抛出，不重试，重试只会掩盖问题并浪费额度。
    网络层抖动（TLS 超时、连接重置）重试一次并打印，避免静默降级判断。
    """
    if params:
        path = path + "?" + urllib.parse.urlencode(params)

    if _has_gh():
        cmd = ["gh", "api", path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        err = (proc.stderr or "") + (proc.stdout or "")
        _raise_for_api_error(err, path)
        if _retry and _is_transient(err):
            print(f"[网络抖动，重试一次] {path}", file=sys.stderr)
            time.sleep(1.5)
            return api_get(path, _retry=False)
        raise ScoutError(f"gh api {path} 失败: {err.strip()[:400]}")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise ScoutError(
            "没有可用的 GitHub 访问方式。装 gh CLI 并 gh auth login，"
            "或设置环境变量 GITHUB_TOKEN。不要跳过核实直接凭记忆填元数据。"
        )
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "job-project-finder",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _raise_for_api_error(f"HTTP {e.code} {e.read().decode('utf-8', 'ignore')}", path)
        raise
    except (urllib.error.URLError, TimeoutError) as e:
        if _retry:
            print(f"[网络抖动，重试一次] {path}", file=sys.stderr)
            time.sleep(1.5)
            return api_get(path, _retry=False)
        raise ScoutError(f"网络失败（{path}）: {e}")


def _is_transient(err):
    low = err.lower()
    return any(
        k in low
        for k in ("tls handshake timeout", "connection reset", "timeout", "eof",
                  "temporary failure", "connection refused", "no such host")
    )


def _raise_for_api_error(err, path):
    low = err.lower()
    if "403" in err and ("rate limit" in low or "secondary rate" in low):
        raise ScoutError(
            f"GitHub 限流（{path}）。search 认证后 30 次/分钟，core 5000 次/小时。"
            "等重置或缩小候选范围，不要循环重试。"
        )
    if "404" in err:
        raise ScoutError(f"仓库或端点不存在（{path}）。可能已改名或删除，记入待核实区。")
    if "401" in err or "bad credentials" in low:
        raise ScoutError(f"GitHub 认证失败（{path}）。检查 gh auth status 或 GITHUB_TOKEN。")


def days_since(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


# ---- Gitee -------------------------------------------------------------
# 三个和 GitHub 不一样的硬约束，都实测过：
# 1. search 接口无 token 时返回 200 + 空数组，不是报错。不检查就会误以为
#    Gitee 上没有项目。带无效 token 才返回 401，说明 token 是硬门禁。
# 2. 未认证限流只有 60 次/小时（GitHub 是 5000），probe 一个仓库要 3 到 5 次，
#    也就是一小时只够十个仓库。必须读 X-RateLimit-Remaining 并提前停。
# 3. 没有 archived 字段。status 是 issue 开关不是归档状态。项目是否已迁移
#    或停维护只能从 description 和 README 正文读出来。
GITEE_API = "https://gitee.com/api/v5"
GITEE_STATE = {"remaining": None, "limit": None}
MIGRATED_RE = re.compile(
    r"(已迁移|已经迁移|迁移至|迁移到|已停止维护|停止维护|不再维护|已归档|"
    r"migrated to|has moved to|no longer maintained|deprecated in favor)",
    re.I,
)


def _ssl_context():
    """macOS 上 python.org 版 Python 常常没配 CA bundle，urllib 会直接
    CERTIFICATE_VERIFY_FAILED，而同一台机器 curl 正常。按 certifi、
    系统根证书的顺序找一份可用的 CA，始终开启校验，绝不关 verify。"""
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    try:
        import certifi
        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        return _SSL_CTX
    except Exception:
        pass
    ctx = ssl.create_default_context()
    for p in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
              "/usr/local/etc/openssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem"):
        if os.path.exists(p):
            try:
                ctx.load_verify_locations(cafile=p)
                break
            except Exception:
                continue
    _SSL_CTX = ctx
    return ctx


_SSL_CTX = None


def _http_json(url):
    """取 JSON。urllib 优先，TLS 信任链缺失时回退 curl 走系统根证书。"""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "job-project-finder"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
            return dict(resp.headers), resp.read().decode("utf-8", "ignore"), resp.status
    except urllib.error.HTTPError as e:
        return dict(e.headers), e.read().decode("utf-8", "ignore"), e.code
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError) and "CERTIFICATE" not in str(e):
            raise
    if not shutil.which("curl"):
        raise ScoutError("TLS 信任链缺失且没有 curl 可回退。装 certifi（pip install certifi）"
                         "或运行 Python 自带的 Install Certificates.command。")
    proc = subprocess.run(
        ["curl", "-sS", "-m", "30", "-D", "-", "-o", "-", "-H", "Accept: application/json", url],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise ScoutError(f"curl 失败: {proc.stderr.strip()[:200]}")
    raw = proc.stdout
    head, _, body = raw.partition("\r\n\r\n")
    code = 200
    m = re.match(r"HTTP/[\d.]+ (\d+)", head)
    if m:
        code = int(m.group(1))
    hdrs = {}
    for ln in head.splitlines()[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip()] = v.strip()
    return hdrs, body, code


def gitee_get(path, params=None):
    q = dict(params or {})
    token = os.environ.get("GITEE_TOKEN") or os.environ.get("GITEE_ACCESS_TOKEN")
    if token:
        q["access_token"] = token
    url = GITEE_API + path + ("?" + urllib.parse.urlencode(q) if q else "")
    try:
        hdrs, body, code = _http_json(url)
    except ScoutError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ScoutError(f"Gitee 网络失败（{path}）: {e}")
    rem = hdrs.get("X-RateLimit-Remaining") or hdrs.get("x-ratelimit-remaining")
    lim = hdrs.get("X-RateLimit-Limit") or hdrs.get("x-ratelimit-limit")
    if rem is not None:
        GITEE_STATE["remaining"] = int(rem)
    if lim is not None:
        GITEE_STATE["limit"] = int(lim)
    if code == 401:
        raise ScoutError("Gitee token 无效或过期。检查 GITEE_TOKEN，或去掉它走免认证的按名核实。")
    if code == 404:
        raise ScoutError(f"Gitee 仓库或端点不存在（{path}）。可能已改名或删除，记入待核实区。")
    if code in (403, 429):
        raise ScoutError(f"Gitee 限流（{path}）。未认证只有 60 次/小时，等重置或配 GITEE_TOKEN。{body[:160]}")
    if code >= 400:
        raise ScoutError(f"Gitee API {code}（{path}）: {body[:160]}")
    if GITEE_STATE["remaining"] is not None and GITEE_STATE["remaining"] <= 3:
        print(f"[Gitee 额度告急] 剩余 {GITEE_STATE['remaining']}/{GITEE_STATE['limit']} 次，"
              "本轮到此为止，别把额度耗光。", file=sys.stderr)
    try:
        return json.loads(body)
    except ValueError:
        raise ScoutError(f"Gitee 返回非 JSON（{path}）: {body[:160]}")


def pget(platform, path, params=None):
    return gitee_get(path, params) if platform == "gitee" else api_get(path, params)


def gitee_budget_left():
    r = GITEE_STATE["remaining"]
    return r if r is not None else 60


def normalize_gitee(item, matched_query=None, requested=None):
    """Gitee 字段映射到与 GitHub 相同的记录形状，下游打分与报告不用改。"""
    stars = item.get("stargazers_count") or 0
    forks = item.get("forks_count") or 0
    lic = item.get("license")
    if isinstance(lic, dict):
        lic = lic.get("spdx_id") or lic.get("name")
    if lic in ("", "NOASSERTION", None):
        lic = None
    dsp = days_since(item.get("pushed_at"))
    full = item.get("full_name") or item.get("path") or ""
    desc = item.get("description") or ""
    name = item.get("path") or full.split("/")[-1]
    url = (item.get("html_url") or f"https://gitee.com/{full}").removesuffix(".git")

    flags, gate = [], "pass"
    if item.get("fork"):
        flags.append("fork")
        gate = "reject"
    if item.get("private") or item.get("internal"):
        flags.append("not-public")
        gate = "reject"
    if dsp is not None and dsp > STALE_DAYS:
        flags.append("stale")
        if gate == "pass":
            gate = "source-reading"
    if not lic:
        flags.append("no-license")
    if MIGRATED_RE.search(desc):
        flags.append("migrated-away")
        gate = "reject"
    if LIST_NAME_RE.search(name) or LIST_DESC_RE.search(desc):
        flags.append("suspect-list")
    if item.get("gvp"):
        flags.append("gvp")
    # Gitee 的 mirrors/ 组织是 GitHub 项目的镜像副本，不是原创托管地。
    # 当候选可以，但简历和 PR 要指向上游，报告里必须标出来。
    if full.split("/")[0].lower() in ("mirrors", "mirrors_huawei", "mirror"):
        flags.append("mirror-of-upstream")

    rec = {
        "platform": "gitee",
        "repo": full,
        "name": name,
        "url": url,
        "description": desc,
        "stars": stars,
        "forks": forks,
        "fork_star_ratio": round(forks / stars, 3) if stars else None,
        "pushed_at": item.get("pushed_at"),
        "created_at": item.get("created_at"),
        "days_since_push": dsp,
        "archived": False,
        "is_template": False,
        "is_fork": bool(item.get("fork")),
        "license": lic,
        "language": item.get("language"),
        "topics": item.get("project_labels") or [],
        "size_kb": None,
        "open_issues": item.get("open_issues_count"),
        "homepage": item.get("homepage"),
        "default_branch": item.get("default_branch") or "master",
        "gvp": bool(item.get("gvp")),
        "flags": flags,
        "gate": gate,
    }
    if requested and requested.lower() != full.lower():
        rec["renamed_from"] = requested
        rec["flags"].append("renamed")
    rec.update(star_bands(stars, item.get("created_at"), platform="gitee", gvp=rec["gvp"]))
    if matched_query:
        rec["matched_queries"] = [matched_query]
    return rec


def normalize(item, matched_query=None):
    stars = item.get("stargazers_count") or 0
    forks = item.get("forks_count") or 0
    raw_lic = (item.get("license") or {}).get("spdx_id")
    # NOASSERTION 表示有 license 但不是标准 SPDX（自定义协议），
    # 和完全没有 license 是两回事，风险描述不能混。
    lic = None if raw_lic in (None, "NOASSERTION") else raw_lic
    custom_license = raw_lic == "NOASSERTION"
    dsp = days_since(item.get("pushed_at"))
    name = item.get("name") or ""
    desc = item.get("description") or ""

    flags, gate = [], "pass"
    if item.get("archived"):
        flags.append("archived")
        gate = "reject"
    if item.get("fork"):
        flags.append("fork")
        gate = "reject"
    if item.get("is_template"):
        flags.append("template")
        gate = "reject"
    if dsp is not None and dsp > STALE_DAYS:
        flags.append("stale")
        if gate == "pass":
            gate = "source-reading"
    if not lic:
        flags.append("custom-license" if custom_license else "no-license")
    if MIGRATED_RE.search(desc):
        flags.append("migrated-away")
        gate = "reject"
    if LIST_NAME_RE.search(name) or LIST_DESC_RE.search(desc):
        flags.append("suspect-list")

    rec = {
        "platform": "github",
        "repo": item.get("full_name"),
        "name": name,
        "url": item.get("html_url"),
        "description": desc,
        "stars": stars,
        "forks": forks,
        "fork_star_ratio": round(forks / stars, 3) if stars else None,
        "pushed_at": item.get("pushed_at"),
        "created_at": item.get("created_at"),
        "days_since_push": dsp,
        "archived": bool(item.get("archived")),
        "is_template": bool(item.get("is_template")),
        "is_fork": bool(item.get("fork")),
        "license": lic,
        "language": item.get("language"),
        "topics": item.get("topics") or [],
        "size_kb": item.get("size"),
        "open_issues": item.get("open_issues_count"),
        "homepage": item.get("homepage"),
        "default_branch": item.get("default_branch") or "HEAD",
        "flags": flags,
        "gate": gate,
    }
    rec.update(star_bands(stars, item.get("created_at")))
    if matched_query:
        rec["matched_queries"] = [matched_query]
    return rec


def cmd_search(args):
    queries = list(args.query or [])
    if args.queries_file:
        with open(args.queries_file, encoding="utf-8") as f:
            queries += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not queries:
        raise ScoutError("至少要给一个查询式，用 -q 或 --queries-file。")

    platform = args.platform
    if platform == "gitee" and not (os.environ.get("GITEE_TOKEN") or os.environ.get("GITEE_ACCESS_TOKEN")):
        raise ScoutError(
            "Gitee 的 search 接口需要 access_token。没有 token 时它返回 200 加空数组，"
            "不是报错，会被误当成「Gitee 上没有项目」。\n"
            "两条路：一是去 gitee.com/profile/personal_access_tokens 建一个只读 token "
            "并 export GITEE_TOKEN=...；二是跳过 search，用 WebSearch 或 awesome 清单"
            "拿到仓库名后直接 probe --platform gitee --repos owner/repo，按名核实免认证。"
        )

    pool, seen, stats = [], {}, []
    for i, q in enumerate(queries):
        if i:
            time.sleep(args.delay)
        try:
            if platform == "gitee":
                items = gitee_get("/search/repositories", {
                    "q": q, "sort": "stars_count", "order": "desc",
                    "per_page": min(args.limit, 100), "page": 1})
                total = None
                if not isinstance(items, list):
                    items = []
            else:
                data = api_get(
                    "/search/repositories",
                    {"q": q, "sort": "stars", "order": "desc", "per_page": min(args.limit, 100)},
                )
                items = data.get("items") or []
                total = data.get("total_count")
        except ScoutError as e:
            stats.append({"query": q, "error": str(e)})
            print(f"[跳过] {q}\n  {e}", file=sys.stderr)
            continue
        if platform == "gitee" and not items:
            print(f"[空结果] {q}\n  Gitee 返回空数组。确认 token 有效，或换更宽的词。", file=sys.stderr)
        added = 0
        for it in items:
            fn = it.get("full_name") or it.get("path")
            if not fn:
                continue
            if fn in seen:
                mq = seen[fn].setdefault("matched_queries", [])
                if q not in mq:
                    mq.append(q)
                continue
            rec = normalize_gitee(it, q) if platform == "gitee" else normalize(it, q)
            seen[fn] = rec
            pool.append(rec)
            added += 1
        stats.append({"query": q, "total_count": total, "returned": len(items), "new": added})
        print(f"[{len(items):>3} 返回 / {added:>3} 新增] {q}")

    out = {
        "platform": platform,
        "searched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "queries": queries,
        "query_stats": stats,
        "gate_counts": {
            g: sum(1 for r in pool if r["gate"] == g) for g in ("pass", "source-reading", "reject")
        },
        "candidates": pool,
    }
    if platform == "gitee":
        out["gitee_rate_limit"] = dict(GITEE_STATE)
    write_out(out, args.out)
    print(f"\n候选池 {len(pool)} 个：" + "，".join(f"{k} {v}" for k, v in out["gate_counts"].items()))
    if platform == "gitee" and GITEE_STATE["remaining"] is not None:
        print(f"Gitee 额度剩余 {GITEE_STATE['remaining']}/{GITEE_STATE['limit']} 次。")
    print("下一步：probe 只对入围候选跑，别对全池拉 README。")


def probe_one(rec, with_tree=True):
    repo = rec["repo"]
    platform = rec.get("platform", "github")
    root = pget(platform, f"/repos/{repo}/contents" + ("/" if platform == "gitee" else ""))
    if not isinstance(root, list):
        root = []
    dirs = [e["name"] for e in root if e.get("type") == "dir"]
    files = [e["name"] for e in root if e.get("type") == "file"]

    dep = [f for f in files if f in DEP_MANIFESTS or DEP_MANIFEST_RE.match(f)]
    docker = [f for f in files if DOCKER_RE.match(f)]
    src_dirs = [d for d in dirs if d.lower() in SOURCE_DIRS]
    ex_dirs = [d for d in dirs if d.lower() in EXAMPLE_DIRS]

    # 隐藏目录是工具链配置（.husky/.github/.devcontainer），不参与内容结构判断。
    # 否则一个 .husky 就能让文档仓库伪装成有代码结构。
    content_dirs = [d for d in dirs if not d.startswith(".")]
    docs_only = not src_dirs and all(d.lower() in DOC_DIRS for d in content_dirs)

    # monorepo 的依赖声明在子目录，根目录查不到。只看根目录会把上手难度误判成很难。
    nested_dep, nested_docker = [], []
    if not dep and not docker and src_dirs:
        for d in src_dirs[:2]:
            try:
                sub = pget(platform, f"/repos/{repo}/contents/{urllib.parse.quote(d)}")
            except ScoutError:
                continue
            if not isinstance(sub, list):
                continue
            for e in sub:
                n = e.get("name", "")
                if e.get("type") == "file":
                    if n in DEP_MANIFESTS or DEP_MANIFEST_RE.match(n):
                        nested_dep.append(f"{d}/{n}")
                    elif DOCKER_RE.match(n):
                        nested_docker.append(f"{d}/{n}")
                elif e.get("type") == "dir":
                    try:
                        sub2 = pget(platform, f"/repos/{repo}/contents/{urllib.parse.quote(d)}/{urllib.parse.quote(n)}")
                    except ScoutError:
                        continue
                    if isinstance(sub2, list):
                        for e2 in sub2:
                            n2 = e2.get("name", "")
                            if e2.get("type") == "file" and (
                                n2 in DEP_MANIFESTS or DEP_MANIFEST_RE.match(n2) or DOCKER_RE.match(n2)
                            ):
                                nested_dep.append(f"{d}/{n}/{n2}")

    readme_text, readme_err = "", None
    try:
        rd = pget(platform, f"/repos/{repo}/readme")
        if isinstance(rd, dict) and rd.get("encoding") == "base64" and rd.get("content"):
            readme_text = base64.b64decode(rd["content"]).decode("utf-8", "ignore")
    except ScoutError as e:
        readme_err = str(e)

    lines = [ln for ln in readme_text.splitlines() if ln.strip()]
    link_lines = sum(1 for ln in lines if MD_LINK_RE.search(ln))
    links = MD_LINK_RE.findall(readme_text)
    # Gitee 的清单会大量链接 gitee.com，只数 github 外链会漏判。
    gh_links = sum(1 for u in links if "github.com" in u or "gitee.com" in u)
    ratio = round(link_lines / len(lines), 3) if lines else 0.0
    install_hits = sorted({m.group(0).lower() for m in INSTALL_RE.finditer(readme_text)})[:8]
    headings = [h[1] for h in HEADING_RE.findall(readme_text)][:25]

    has_deps = bool(dep or docker or nested_dep or nested_docker)
    code_structure = bool(src_dirs) and has_deps
    strong_list_name = bool(LIST_NAME_RE.search(rec.get("name") or ""))
    signals = []
    if readme_err:
        signals.append("readme-unavailable")
    if docs_only:
        signals.append("docs-only-structure")
    # 链接密度单独用会误判：真项目的 README 也可能徽章和特性链接一堆。
    # 大量 github 外链（清单收录几百上千条）和纯文档结构才是强信号。
    if readme_text and ratio > 0.35 and (
        docs_only
        or gh_links >= 50
        or (strong_list_name and ratio > 0.6)
        or (ratio > 0.6 and not code_structure)
    ):
        signals.append("looks-like-list")
    if (not has_deps and not install_hits) or docs_only:
        signals.append("no-run-path")
    if not install_hits and not ex_dirs:
        signals.append("weak-run-docs")
    if not ex_dirs:
        signals.append("no-root-examples")
    if not dep and not docker and (nested_dep or nested_docker):
        signals.append("nested-deps")
    # Gitee 没有 archived 字段，项目迁走或停维护只能从正文读。
    # mindspore 的 README 首行就写着已迁移到 AtomGit，元数据完全看不出来。
    if readme_text and MIGRATED_RE.search(readme_text[:1500]):
        signals.append("migrated-away")

    gate = rec.get("gate", "pass")
    if "migrated-away" in signals:
        gate = "reject"
        if "migrated-away" not in rec.get("flags", []):
            rec.setdefault("flags", []).append("migrated-away")
    if "no-run-path" in signals and gate == "pass":
        gate = "source-reading"

    rec["gate"] = gate

    share, tree_err = None, None
    if with_tree:
        try:
            share = ai_share(repo, rec.get("default_branch"), platform)
        except ScoutError as e:
            tree_err = str(e)
    rel_n, rel_list = releases_recent(repo, platform=platform)
    rec["releases_90d"] = rel_n
    rec["recent_releases"] = rel_list
    rec["timeliness_score"] = timeliness_band(rec.get("days_since_push"), rel_n)

    deps = ai_dependencies(repo, files, platform)
    rec["ai_dependencies"] = deps

    # 产品形态：库/SDK/引擎/中间件不是求职者要的项目，直接淘汰。
    # 放在 share 之后是因为要用 trees 里的前端文件数判断有没有终端交互面。
    has_cli = any(d.lower() in ("cli", "cmd", "bin") for d in dirs) or any(
        f.lower() in ("cli.py", "main.py", "__main__.py") for f in files)
    form, form_ev = product_form(
        rec, dirs, files, readme_text,
        (share or {}).get("ui_file_count", 0),
        (share or {}).get("top_ui_paths", []),
        has_cli,
    )
    rec["product_form"] = form
    rec["product_form_evidence"] = form_ev
    if form in ("library", "infra"):
        rec["gate"] = "reject"
        signals.append("not-a-product:" + form)
        if "not-a-product" not in rec.get("flags", []):
            rec.setdefault("flags", []).append("not-a-product")
    elif form == "unknown":
        signals.append("product-form-unknown")
    sb = (share or {}).get("ai_share_score")
    db = deps.get("ai_dep_band")
    # 两个信号差两档以上说明路径归类不可信，必须人工读代码定分，不要取平均糊过去。
    rec["ai_band_divergence"] = bool(sb is not None and db is not None and abs(sb - db) >= 2)

    rec["probe"] = {
        "root_dirs": dirs,
        "root_files": files,
        "dep_manifests": dep,
        "docker_files": docker,
        "nested_dep_manifests": nested_dep[:12],
        "nested_docker_files": nested_docker[:6],
        "source_dirs": src_dirs,
        "example_dirs": ex_dirs,
        "docs_only_structure": docs_only,
        "readme_chars": len(readme_text),
        "readme_headings": headings,
        "readme_link_line_ratio": ratio,
        "readme_github_links": gh_links,
        "readme_install_hits": install_hits,
        "readme_excerpt": clean_readme(readme_text)[:3500],
        "readme_error": readme_err,
        "ai_share": share,
        "ai_share_error": tree_err,
        "signals": signals,
    }
    return rec


def cmd_probe(args):
    records, failed = [], []
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            pool = json.load(f)
        cands = [r for r in pool.get("candidates", []) if r.get("gate") != "reject"]
        # 名字里就写着 awesome/tutorial 的排到最后。probe 额度有限，
        # 别拿去核实几乎肯定不是项目的仓库。
        cands.sort(key=lambda r: ("suspect-list" in r.get("flags", []), -(r.get("stars") or 0)))
        records = cands[: args.top] if args.top else cands
    if args.repos:
        have = {r["repo"] for r in records}
        for name in args.repos.split(","):
            name = name.strip().strip("/")
            if not name or name in have:
                continue
            if name.startswith("http"):
                name = "/".join(name.rstrip("/").removesuffix(".git").split("/")[-2:])
            # 从清单或公网文章抓来的仓库名可能已改名或删除。
            # 单个失效不该中断整批核实，记入待核实区继续。
            try:
                if args.platform == "gitee":
                    records.append(normalize_gitee(gitee_get(f"/repos/{name}"), requested=name))
                else:
                    records.append(normalize(api_get(f"/repos/{name}")))
            except ScoutError as e:
                failed.append({"repo": name, "platform": args.platform, "error": str(e)})
                print(f"[待核实        ] {name:<45} {e}", file=sys.stderr)
                if "限流" in str(e) or "认证失败" in str(e) or "token 无效" in str(e):
                    print("遇到限流或认证问题，停止后续核实。", file=sys.stderr)
                    break

    if not records and not failed:
        raise ScoutError("没有要核实的仓库。用 --from 指定候选池，或用 --repos 直接给仓库名。")

    if args.platform == "gitee":
        need = len(records) * (4 if not args.no_tree else 3)
        left = gitee_budget_left()
        if need > left:
            print(f"[额度预警] {len(records)} 个仓库约需 {need} 次调用，Gitee 本轮只剩 {left} 次。"
                  "建议减少 --top 或加 --no-tree，否则会中途断在待核实区。", file=sys.stderr)

    probed = []
    for i, rec in enumerate(records):
        if i:
            time.sleep(args.delay)
        if rec.get("platform") == "gitee" and gitee_budget_left() <= 4:
            failed.append({"repo": rec["repo"], "platform": "gitee",
                           "error": "Gitee 额度耗尽，本仓库未核实。等重置或配 GITEE_TOKEN。"})
            print(f"[额度耗尽      ] {rec['repo']} 跳过，记入待核实区。", file=sys.stderr)
            continue
        try:
            probed.append(probe_one(rec, with_tree=not args.no_tree))
            p = probed[-1]["probe"]
            sh = (p.get("ai_share") or {}).get("ai_share")
            sh_txt = f"AI{sh:.0%}" if isinstance(sh, float) else "AI?"
            warn = "!" if rec.get("ai_band_divergence") or (p.get("ai_share") or {}).get("naming_bias_warning") else " "
            pf = "GE" if rec.get("platform") == "gitee" else "GH"
            form = (rec.get("product_form") or "?")[:7]
            print(
                f"[{pf} {rec['gate']:<13}] {rec['repo']:<36} {form:<7} "
                f"★{rec['stars']:<7} {sh_txt}{warn:<2} "
                f"deps{rec['ai_dependencies']['ai_dep_band']} "
                f"增速{rec.get('stars_per_month')}/月 rel90={rec.get('releases_90d')} "
                f"{','.join(p['signals']) or '无信号'}"
            )
        except ScoutError as e:
            failed.append({"repo": rec["repo"], "platform": rec.get("platform", "github"), "error": str(e)})
            print(f"[待核实        ] {rec['repo']:<45} {e}", file=sys.stderr)
            if "限流" in str(e) or "认证失败" in str(e) or "token 无效" in str(e):
                print("遇到限流或认证问题，停止后续核实。", file=sys.stderr)
                break

    out = {
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platforms": sorted({r.get("platform", "github") for r in probed} |
                            {f.get("platform", "github") for f in failed}),
        "probed": probed,
        "unverified": failed,
    }
    if GITEE_STATE["remaining"] is not None:
        out["gitee_rate_limit"] = dict(GITEE_STATE)
    write_out(out, args.out)
    print(f"\n核实 {len(probed)} 个，待核实 {len(failed)} 个。")
    if GITEE_STATE["remaining"] is not None:
        print(f"Gitee 额度剩余 {GITEE_STATE['remaining']}/{GITEE_STATE['limit']} 次。")
    print("signals 是启发式信号，looks-like-list 和 no-run-path 要读 readme_excerpt 自己确认。")


def write_out(obj, path):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print(f"已写入 {os.path.abspath(path)}")


def main():
    ap = argparse.ArgumentParser(description="GitHub / Gitee 项目检索与核实")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="按查询式建候选池")
    s.add_argument("-q", "--query", action="append", help="查询式，可重复")
    s.add_argument("--queries-file", help="每行一个查询式，# 开头为注释")
    s.add_argument("--platform", choices=["github", "gitee"], default="github",
                   help="检索平台。gitee 的 search 需要 GITEE_TOKEN")
    s.add_argument("--limit", type=int, default=30, help="每个查询式取多少条，上限 100")
    s.add_argument("--delay", type=float, default=2.0, help="查询间隔秒，默认 2 对应 30 次/分钟")
    s.add_argument("--out", help="输出 JSON 路径")
    s.set_defaults(func=cmd_search)

    p = sub.add_parser("probe", help="拉 README 和目录结构核实")
    p.add_argument("--from", dest="from_file", help="search 产出的候选池 JSON，平台从记录里读")
    p.add_argument("--platform", choices=["github", "gitee"], default="github",
                   help="--repos 按名核实时用哪个平台。--from 时以池内记录为准")
    p.add_argument("--top", type=int, default=20, help="从候选池取前 N 个（按 star 排序，已排除 reject）")
    p.add_argument("--repos", help="直接指定 owner/repo，逗号分隔，也接受完整 URL")
    p.add_argument("--delay", type=float, default=0.2, help="仓库间隔秒")
    p.add_argument("--no-tree", action="store_true",
                   help="跳过 git trees 体积分析（不算 AI 核心占比），大仓库或 Gitee 省额度")
    p.add_argument("--out", help="输出 JSON 路径")
    p.set_defaults(func=cmd_probe)

    args = ap.parse_args()
    try:
        args.func(args)
    except ScoutError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
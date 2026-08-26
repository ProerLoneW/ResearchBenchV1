#!/usr/bin/env python3
"""
TeX → 飞书云文档 工具（独立脚本，不调用任何大模型）。

输入：一篇论文「已翻译为中文」的完整 TeX Source 仓库目录。
功能：
  1. 自动定位主 TeX 文件（含 \\documentclass 者；同名 main/paper/thesis 优先）
  2. 解析 \\input / \\include 引用，按原始章节顺序拼接全文
  3. 将 LaTeX 转为结构化正文，保留 标题/章节/公式/列表/表格
  4. 查找图片文件并上传、按原文位置插入飞书文档
  5. 写入与论文同名的飞书云文档，返回文档链接

用法：
  python tex2feishu.py <tex_repo_dir> [--title "论文标题"] [--app-id X --app-secret Y]

说明：飞书凭证默认使用 test_feishu_final_version.py 中的个人应用，
      也可在「设置-飞书」页覆盖（写入 data/feishu_config.json）。
"""
import os
import re
import sys
import json
import argparse
from pathlib import Path

# 让脚本可从项目根目录直接运行，复用 app 包内的飞书客户端
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.feishu import FeishuClient  # noqa: E402

DATA_DIR = ROOT / "data"
FEISHU_CONFIG = DATA_DIR / "feishu_config.json"
# 安全提示：不要在代码中硬编码 Secret。请通过环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 提供，
# 或在「设置-飞书」页覆盖（写入 data/feishu_config.json，已被 .gitignore 忽略）。
DEFAULT_APP_ID = os.getenv("FEISHU_APP_ID", "cli_aa0ec67a13789bd8")
DEFAULT_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

IMG_EXTS = [".png", ".jpg", ".jpeg", ".pdf", ".eps"]


def log(*a):
    print(*a, file=sys.stderr)


def load_feishu_creds():
    if FEISHU_CONFIG.exists():
        try:
            d = json.loads(FEISHU_CONFIG.read_text(encoding="utf-8"))
            return d.get("app_id", DEFAULT_APP_ID), d.get("app_secret", DEFAULT_APP_SECRET)
        except Exception:
            pass
    return DEFAULT_APP_ID, DEFAULT_APP_SECRET


# ---------- 1. 定位主文件 ----------
def find_main_tex(repo: Path):
    texes = list(repo.rglob("*.tex"))
    if not texes:
        return None
    docclass = [t for t in texes if "\\documentclass" in t.read_text(errors="ignore")]
    if not docclass:
        return max(texes, key=lambda t: t.stat().st_size)
    if len(docclass) == 1:
        return docclass[0]
    for name in ("main", "paper", "thesis", "root"):
        for t in docclass:
            if t.stem.lower() == name:
                return t
    return docclass[0]


# ---------- 2. 展开 input/include ----------
def resolve_input(target: str, repo: Path):
    target = target.strip()
    base = target if target.endswith(".tex") else target + ".tex"
    cand = repo / base
    if cand.exists():
        return cand
    matches = list(repo.rglob(base))
    if matches:
        return matches[0]
    return None


def expand(content: str, current_file: Path, repo: Path, visited: set):
    def repl(m):
        f = resolve_input(m.group(1), repo)
        if not f or f in visited:
            return ""
        visited.add(f)
        txt = f.read_text(encoding="utf-8", errors="ignore")
        return expand(txt, f, repo, visited)

    return re.sub(r"\\(?:input|include)\{([^}]*)\}", repl, content)


# ---------- 3. 行内格式化 ----------
def fmt(text: str) -> str:
    text = (text.replace("\\%", "%").replace("\\&", "&").replace("\\#", "#")
                .replace("\\{", "{").replace("\\}", "}").replace("\\_", "_")
                .replace("\\~", " ").replace("\\$", "$").replace("\\#", "#"))
    text = re.sub(r"\\textbf\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\textit\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\emph\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\texttt\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\cite[p]?\{[^}]*\}", "[引用]", text)
    text = re.sub(r"\\eqref\{[^}]*\}", "(式)", text)
    text = re.sub(r"\\ref\{[^}]*\}", "(引用)", text)
    text = re.sub(r"\\label\{[^}]*\}", "", text)
    text = text.replace("\\\\", " ").replace("~", " ")
    text = re.sub(r"\\[a-zA-Z]+\*?", "", text)   # 删除其余未知命令
    text = text.replace("{", "").replace("}", "").replace("\\", "")  # 清理残留花括号/反斜杠
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# ---------- 4. 结构解析 ----------
def parse_list(buf, env):
    out, cur = [], None
    for line in buf:
        s = line.strip()
        mi = re.match(r"\\item\b\s*(.*)", s)
        if mi:
            if cur:
                (out.append(FeishuClient.bullet_block(fmt(cur)))
                 if env == "itemize" else out.append(FeishuClient.ordered_block(fmt(cur))))
            cur = mi.group(1)
        elif cur is not None:
            cur += " " + s
    if cur:
        out.append(FeishuClient.bullet_block(fmt(cur))
                   if env == "itemize" else FeishuClient.ordered_block(fmt(cur)))
    return out


def parse_image(path: str, repo: Path, upload):
    base = Path(path)
    candidates = []
    for d in [repo, base.parent if base.is_absolute() else repo / base.parent]:
        for ext in ["", ".png", ".jpg", ".jpeg", ".pdf", ".eps"]:
            candidates.append(d / (base.name if ext == "" else base.stem + ext))
    found = next((c for c in candidates if c.exists()), None)
    if not found:
        found = next((p for p in repo.rglob(base.name + ".*")
                      if p.suffix.lower() in (".png", ".jpg", ".jpeg")), None)
    if not found:
        return [FeishuClient.text_block(f"[图片缺失: {path}]")]
    if found.suffix.lower() == ".pdf":
        return [FeishuClient.text_block(f"[图片(PDF，未内嵌): {found.name}]")]
    try:
        token = upload(str(found))
        return [FeishuClient.image_block(token)]
    except Exception as e:
        log("图片上传失败:", e)
        return [FeishuClient.text_block(f"[图片: {found.name}]")]


def parse_figure(buf, repo, upload):
    out = []
    for line in buf:
        mg = re.search(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", line)
        if mg:
            out.extend(parse_image(mg.group(1), repo, upload))
        mc = re.search(r"\\caption\{(.*)\}", line)
        if mc:
            out.append(FeishuClient.text_block("图注：" + fmt(mc.group(1))))
    return out


def parse_table_text(buf):
    text = "\n".join(buf)
    m = re.search(r"\\begin\{tabular\}.*?\\end\{tabular\}", text, re.S)
    if m:
        text = m.group(0)
    out = []
    for r in re.split(r"\\\\", text):
        r = r.strip()
        if (not r or r.startswith("\\begin") or r.startswith("\\end")
                or "rule" in r or "hline" in r):
            continue
        cells = [fmt(c) for c in r.split("&")]
        out.append(" | ".join(cells))
    return "\n".join(out)


def to_blocks(tex: str, repo: Path, upload):
    # 丢弃导言区与 document 环境之外的残余内容，确保任何调用方都得到干净正文
    if "\\begin{document}" in tex:
        tex = tex.split("\\begin{document}", 1)[1]
    if "\\end{document}" in tex:
        tex = tex.split("\\end{document}", 1)[0]

    blocks = []
    para = []

    def flush():
        if para:
            txt = fmt(" ".join(para)).strip()
            if txt:
                blocks.append(FeishuClient.text_block(txt))
            para.clear()

    for seg in re.split(r"(\$\$.*?\$\$|\\\[.*?\\\])", tex, flags=re.S):
        if not seg:
            continue
        if re.match(r"^\$\$.*\$\$$", seg.strip(), re.S) or re.match(r"^\\\[.*\\]$", seg.strip(), re.S):
            math = seg.strip().strip("$").strip().strip("[]").strip()
            blocks.append(FeishuClient.code_block(math))
            continue

        lines = seg.splitlines()
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if s.startswith("%"):
                i += 1
                continue
            # 跳过 document 环境及其分隔符（理论上已在函数入口剥离，双保险）
            if s in ("\\begin{document}", "\\end{document}") or s == "\\end{document}":
                i += 1
                continue
            m = re.match(r"\\(chapter|section|subsection|subsubsection|paragraph)\*?\{(.*)\}", s)
            if m:
                flush()
                lvl = {"chapter": 1, "section": 1, "subsection": 2,
                       "subsubsection": 3, "paragraph": 3}[m.group(1)]
                blocks.append(FeishuClient.heading_block(lvl, fmt(m.group(2))))
                i += 1
                continue
            mb = re.match(r"\\begin\{(itemize|enumerate|figure|table|tabular)\}", s)
            if mb:
                env = mb.group(1)
                buf, depth = [], 1
                i += 1
                while i < len(lines) and depth > 0:
                    ls = lines[i].strip()
                    if re.match(r"\\end\{([^}]*)\}", ls):
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                        buf.append(lines[i])
                    elif re.match(r"\\begin\{([^}]*)\}", ls):
                        depth += 1
                        buf.append(lines[i])
                    else:
                        buf.append(lines[i])
                    i += 1
                flush()
                if env in ("itemize", "enumerate"):
                    blocks.extend(parse_list(buf, env))
                elif env == "figure":
                    blocks.extend(parse_figure(buf, repo, upload))
                elif env in ("table", "tabular"):
                    tbl = parse_table_text(buf)
                    if tbl:
                        blocks.append(FeishuClient.code_block("表格：\n" + tbl))
                continue
            mg = re.search(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", s)
            if mg:
                flush()
                blocks.extend(parse_image(mg.group(1), repo, upload))
                i += 1
                continue
            if not s:
                flush()
                i += 1
                continue
            para.append(s)
            i += 1
    flush()
    return blocks


# ---------- 主流程 ----------
def run(repo_dir, title=None, app_id=None, app_secret=None):
    repo = Path(repo_dir)
    if not repo.exists():
        raise FileNotFoundError(f"仓库不存在: {repo_dir}")
    main = find_main_tex(repo)
    if not main:
        raise RuntimeError("未找到任何 .tex 主文件")
    log(f"主文件: {main}")
    content = main.read_text(encoding="utf-8", errors="ignore")
    visited = {main}
    full = expand(content, main, repo, visited)
    full = "\n".join(re.sub(r"(?<!\\)%.*$", "", ln) for ln in full.splitlines())
    # 丢弃导言区（\documentclass / \title 等）与 \end{document} 之后内容
    if "\\begin{document}" in full:
        full = full.split("\\begin{document}", 1)[1]
    if "\\end{document}" in full:
        full = full.split("\\end{document}", 1)[0]
    log(f"展开后全文约 {len(full)} 字符")

    aid, asec = (app_id, app_secret) if app_id else load_feishu_creds()
    client = FeishuClient(app_id=aid, app_secret=asec)

    def upload(p):
        return client.upload_image_token(p)

    blocks = to_blocks(full, repo, upload)
    doc_title = title or main.stem
    log(f"生成 {len(blocks)} 个 block，标题：{doc_title}")
    doc_id, url = client.write_document(doc_title, blocks)
    log(f"文档创建成功，document_id={doc_id}")
    return url


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TeX → 飞书云文档 工具")
    ap.add_argument("repo", help="TeX 仓库目录")
    ap.add_argument("--title", default=None, help="文档标题（默认用主文件名）")
    ap.add_argument("--app-id", default=None)
    ap.add_argument("--app-secret", default=None)
    args = ap.parse_args()
    try:
        url = run(args.repo, args.title, args.app_id, args.app_secret)
        print(url)  # 仅 URL 输出到 stdout，供后端解析
    except Exception as e:
        log("❌ 失败:", e)
        sys.exit(1)

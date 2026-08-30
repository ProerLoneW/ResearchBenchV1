---
name: paper-zh-translator
description: |
  Translate an English LaTeX paper (tex source + figures) to Chinese and recompile to PDF. Use when the user says "翻译论文"/"translate this paper"/"中文化 paper"/"把 tex 翻成中文"/"编译一份中文版", or hands over a folder containing `main.tex` and figures with English content and wants a 中文 PDF. Optimized for low token cost on big papers (10-30 pages): skip appendices, segment reads, parallelize multi-paper jobs, give workers a tight prompt template. Do NOT use for: translating PDF directly without source (use a PDF+OCR flow), single-page short docs (overhead > gain), or non-LaTeX formats like Word/Markdown.
---

# Paper ZH Translator

## Inputs to collect

- **论文目录**：绝对路径，含 `main.tex` 或同类入口文件
- **入口文件名**：相对路径（如 `main.tex`、`saber_arxiv.tex`、`iclr2026_conference.tex`、`arxiv_main_supp.tex`）
- **章节文件列表**：从 `\input{}` / `\include{}` 找出，正文 vs 附录分清楚
- **附录起止**：单文件则找 `\appendix` 行号；多文件则看 `X_suppl.tex` / `*_appendix.tex` / `6_supplementary.tex` 等
- **输出 PDF 路径**：用户指定位置（默认复制到上级目录 `<父目录>/<论文名>_zh.pdf`）
- **翻译范围确认**：是否翻译 caption/章节标题（默认是）；是否保留 author/email（默认保留）

缺这些信息会卡流程。先一次性向用户问清楚再启动。

## Procedure

### 1. 摸清结构（1 次 grep，token 极省）

```bash
grep -nE '\\(section|appendix|subsection|input|include|begin\{document})' <目录>/*.tex
```

目的：定位附录起点 + 章节文件列表。这一步**只读 grep 结果**，不 read 整文件。

### 2. 注入中文化方案（单次 Edit）

在入口文件 `\documentclass` 之后立即插入：

```latex
\usepackage{xeCJK}
\setCJKmainfont[BoldFont=Heiti SC,ItalicFont=Kaiti SC]{Songti SC}
\setCJKsansfont{Heiti SC}
\setCJKmonofont{Kaiti SC}
```

用 `Edit` 工具，不要 sed/awk（避免转义错误）。

**为什么用 Songti SC 不用 STSong**：macOS 上 STSong 不在 fc-list 中，Songti SC 是同类的宋体。Linux 上无这两个时换 `Noto Serif CJK SC`。

### 3. 翻译（按章节文件分批，不一次 read 全文）

- **不要**用一次性 read+write 替换整个文件：分段 read → edit
- **保留**：所有 `\cite{}`、`\label{}`、`\ref{}`、`\input` 命令、数学公式、变量名、模型名
- **翻译**：`\section{...}` 标题、`\subsection{...}` 标题、`\caption{...}`、正文段落、致谢
- **跳过**：附录文件 / 附录起行号之后的内容
- **不译**：`\author` / `\date` / `\maketitle`、teaser figure caption（首页大图注）

详细规则见 `references/translate-rules.md`。

### 4. 编译（不要用 latexmk）

```bash
cd "<论文目录>"
xelatex -interaction=nonstopmode <入口文件名>
bibtex <入口名（不带 .tex）>
xelatex -interaction=nonstopmode <入口文件名>
xelatex -interaction=nonstopmode <入口文件名>
```

**为什么不用 latexmk**：latexmk 在 xelatex 下跑 bibtex 时机有 bug，会反复报空 citation 错误。手动 xelatex/bibtex 循环更稳。

各模板特殊处理（axessibility、no .bib、字体回退等）见 `references/template-pitfalls.md`。

### 5. 复制 + 验证

```bash
cp "<工作目录>/<入口名>.pdf" "<目标目录>/<论文名>_zh.pdf"
```

- **大小校验**：文件 > 1MB（< 1MB 几乎肯定是编译失败）
- **中文校验**：用 `read` 工具读 PDF 第 1-3 页（`pages: "1-3"`），确认 abstract + 章节标题 + 一段正文为中文
- **保留校验**：模型名（DiT、CLIP、SkyReels-A2 等）、引用键 `[18, 38]`、URL/邮箱保持原样

### 6. 大论文 / 多论文的 token 节流

- **> 30KB 的单文件**：分块 read（每次 ≤ 300 行），不要一次性 read 进 context
- **> 6 个章节文件**：分批翻译，先 1-2 个文件验证流程，再批量推广
- **多论文并行**：用 `task` 工具 background 启动多个 worker，每个 worker 接收上面 1-5 步的紧凑版 prompt
- **父 session 不做翻译**：让 worker 内部 read+edit，父 session 只负责摸结构 + 调度

## Output contract

- 编译产物在源目录：`<工作目录>/<入口名>.pdf`
- 副本在目标目录：`<目标目录>/<论文名>_zh.pdf`
- 给用户报告：翻译了哪些文件、PDF 大小、页数、中文显示是否正常、遇到的问题

## Failure handling

| 失败模式 | 处理 |
|---------|------|
| **axessibility 与 xelatex 不兼容**（CVPR/ICCV 模板常见） | main.tex 中把 `\usepackage[accsupp]{axessibility}` 注释掉或加 `\ifxetex\else` 条件包装 |
| **没有 main.bib**（个别论文源不全） | worker 自己用占位条目构造 .bbl，参考文献会有占位词但能编译；如需完整可用从原 PDF 复制 |
| **STSong 不可用** | 用 Songti SC（macOS）或 Noto Serif CJK SC（Linux） |
| **axessibility / glyphtounicode 报错** | 加 `\providecommand{\pdfglyphtounicode}[2]{}` 把它降级为 no-op |
| **bibtex 报空 citation** | 正常现象，三轮 xelatex 后会解决 |
| **rm 在网络共享上被阻止** | 不要清理中间文件，保留 .aux/.bbl/.log |
| **编译产物 < 1MB** | 几乎肯定是 LaTeX 报错中断，检查 main.log 的 `! Error` 行 |
| **`axessibility` 报 `glyphtounicode.tex` 错误** | 用 `\IfFileExists{axessibility.sty}{\ifxetex\else\usepackage[accsupp]{axessibility}\fi}{}` 条件包装 |

更多细节见 `references/template-pitfalls.md`。

## Examples

### 多文件论文（最常见）

入口 `main.tex`（CVPR/ICCV/ECCV 模板）+ `sections/0_abstract.tex` ~ `sections/5_conclusion.tex` + 附录 `X_suppl.tex`。

- 翻译 8 个章节文件
- 跳过 X_suppl
- 中文化注入在 main.tex `\documentclass` 后

### 单文件论文（变体）

整个论文在 `main.tex` 中（如 SkyReels-A2），无独立章节文件。

- grep `\appendix` 找正文终点，没有则全文翻译
- 分块 read 翻译（每块 ≤ 300 行）
- 中文化注入在 line 1 后

### 多论文并行

7 篇论文任务：父 session 用 `task` 工具 background 启动 7 个 worker，每个 worker prompt 包含：

```
工作目录、入口文件、章节文件列表、附录标识
+ 中文化方案（直接复用上面第 2 步代码块）
+ 编译命令（直接复用上面第 4 步代码块）
+ 翻译规则摘要（reference to references/translate-rules.md）
+ 验证要求
+ 输出路径
```

worker 完成后父 session 汇总报告。

## Windows (win32) platform notes

PowerShell 与 macOS/Linux 的差异：

- **`xelatex` / `bibtex` 命令名相同**，但需要确保 TeX Live 或 MiKTeX 已安装并加入 PATH
- **`cd` 行为一致**：`cd "C:\path\to\paper"`
- **`cp` 改 `Copy-Item`**：把 `cp "a.pdf" "b.pdf"` 换成 `Copy-Item "a.pdf" "b.pdf"`
- **`fc-list` 不可用**：用 `Get-ChildItem "$env:WINDIR\Fonts" | Select-String -Pattern "Noto"` 或直接信任字体存在
- **中文字体优先 Noto 系列**（`Noto Serif CJK SC`、`Noto Sans CJK SC`、`AR PL UKai CN`）而非 Songti SC
- **路径分隔符**：LaTeX 内统一用正斜杠 `/`；Shell 内 PowerShell 接受正反斜杠但要转义

PowerShell 编译等价命令：

```powershell
cd "C:\path\to\paper"
xelatex -interaction=nonstopmode main.tex
bibtex main
xelatex -interaction=nonstopmode main.tex
xelatex -interaction=nonstopmode main.tex
Copy-Item "main.pdf" "..\main_zh.pdf"
```

# Template Pitfalls — 各 LaTeX 模板的特殊处理

> 翻译论文时碰到的 LaTeX 模板（CVPR/ICCV/ECCV/NeurIPS/ICLR）有各自的坑。这篇总结每个模板的入口结构、附录位置、特殊包冲突。

## 快速判断模板类型

```bash
grep -E "documentclass|usepackage\[[a-z,]*\]\{(cvpr|iccv|neurips|eccv|llncs|iclr|fairmeta)" <入口文件>
```

- `cvpr.sty` → CVPR 模板（双栏 letterpaper）
- `iccv.sty` → ICCV 模板（双栏 letterpaper）
- `neurips_2024.sty` → NeurIPS 模板（单栏）
- `llncs.cls` → ECCV/Springer LNCS 模板（单栏）
- `iclr2026_conference.sty` → ICLR 2026 模板
- `fairmeta.cls` → Meta 内部模板
- `main.sty` → 项目自带（很可能是 NeurIPS 变体）

## CVPR / ICCV（双栏 letterpaper）

### 入口文件结构

```latex
\documentclass[10pt,twocolumn,letterpaper]{article}
\usepackage[pagenumbers]{cvpr}  % 或 iccv
\input{preamble}
% ... 其他宏包 ...
\begin{document}
\maketitle
\input{sec_cvpr/0_abstract}
\input{sec_cvpr/1_intro}
% ... 更多章节 ...
\bibliographystyle{ieeenat_fullname}
\bibliography{main}
\appendix  % 或 \input{sec_cvpr/supp} 在 \appendix 外
\end{document}
```

### 常见问题

- **axessibility 与 xelatex 不兼容**：见 `xelatex-recipe.md` 第 1 条
- **章节文件命名**：`sec_cvpr/0_abstract.tex` ~ `sec_cvpr/3_exp.tex`，附录可能在 `sec_cvpr/supp.tex` 或独立目录
- **bibtex 样式**：`ieeenat_fullname`（完整作者名）
- **附录标识**：可能有显式 `\appendix` 也可能没有（仅在 main.tex 外 include）

### 中文化注入位置

```latex
\usepackage[pagenumbers]{cvpr}  % 这一行之后立即插入
\usepackage{xeCJK}
\setCJKmainfont[BoldFont=Heiti SC,ItalicFont=Kaiti SC]{Songti SC}
\setCJKsansfont{Heiti SC}
\setCJKmonofont{Kaiti SC}
\input{preamble}  % 接下来
```

## ECCV（llncs，单栏）

### 入口文件结构

```latex
\documentclass[runningheads]{llncs}
\usepackage[mobile]{eccv}
% ... 其他宏包 ...
\begin{document}
\title{...}
\author{...}
\institute{...}
\maketitle
\input{secs/abs}
\input{secs/intro}
% ... 更多章节 ...
\bibliographystyle{splncs04}
\bibliography{main}
\input{secs/supp}  % 补充材料在 \appendix 之外
\end{document}
```

### 常见问题

- **没有显式 `\appendix`**：补充材料在 `\input{secs/supp}` 处，**通常不翻译**（在 \appendix 之外）
- **作者机构用 `\institute{}`**：保持英文不译
- **bibtex 样式**：`splncs04`（Springer LNCS）

## NeurIPS（main.sty，单栏）

### 入口文件结构

```latex
\documentclass{article}
\usepackage{main}  % 自带样式（类似 neurips_2024）
% ... 其他宏包 ...
\begin{document}
\maketitle
% 单文件：所有章节在 main.tex 中
% 或：\input{sections/0_abstract}...
\end{document}
```

### 常见问题

- **main.sty 是项目自带**：不要修改它
- **main.bbl 通常存在**：不需要再 bibtex，但建议跑一次以防不匹配
- **作者信息**：`\author{...}` 含 `\And` / `\AND` 等控制命令，保留原样

## ICLR（iclr2026_conference.sty，单栏）

### 入口文件结构

```latex
\documentclass{article}
\usepackage{iclr2026_conference,times}
% ... 其他宏包 ...
\begin{document}
\iclrfinalcopy  % 必须在 \begin{document} 后立即
\maketitle
% 单文件或 \input{}
\end{document}
```

### 常见问题

- **必须 `\iclrfinalcopy`**：否则作者信息被隐藏
- **附录从 `\appendix` 开始**：\input{...} 切换到附录章节
- **bibtex 样式**：`iclr2026_conference.bst`（natbib 风格，支持 `\citep`/`\citet`）

## 自定义 / 罕见模板（fairmeta 等）

### fairmeta.cls（Meta）

```latex
\documentclass[]{fairmeta}
\usepackage{mathpazo}
\usepackage{tgpagella}
\input{my_setting}
\title{...}
\author[1,2,*]{Name}  % 多机构
\affiliation[1]{...}
\begin{document}
\maketitle
\input{sections/0_abstract}  % 通过 \input 引入
\end{document}
```

### 常见问题

- **自定义命令多**：`\method`、`\methodbench` 等需要保留不译
- **作者机构用 `\affiliation[1]`**：保留英文

## 章节文件命名约定

| 命名风格 | 例子 | 常见于 |
|---------|------|--------|
| `sec_X_name` | `sec_cvpr/3_exp.tex` | CVPR/ICCV |
| `sections/X_name` | `sections/3_method.tex` | ECCV/IEEE |
| `X_name` | `3_method.tex` | NeurIPS 自带 |
| `sec/X_name` | `sec/0_abstract.tex` | ICCV 自定义 |
| `sources/X_name` | `sources/3_methodology.tex` | Multi-subject |

## 附录文件命名约定

| 命名 | 处理 |
|------|------|
| `X_suppl.tex` | 不翻译（X = 罗马数字 10，超出正文章节编号） |
| `6_supplementary.tex` | 数字 6 通常表示第一个附录章节，**不翻译** |
| `*_appendix.tex` | 包含 "appendix"，**不翻译** |
| `A_detail_*.tex`、`B_*` | 字母编号（CVPR 常见），**不翻译** |
| `sec_cvpr/supp.tex` | "supp" 是 supplementary 缩写，**不翻译** |

## 单文件论文的章节定位

如果论文所有内容都在一个 `main.tex` 中，grep `\appendix` 找附录起点：

```bash
grep -nE '\\(appendix|section\{Conclusion)' <main.tex>
```

- 有 `\appendix` 标记：在 `\appendix` 之后的所有 `\section{...}` 都不翻译
- 没有 `\appendix` 标记（罕见）：翻译整个文件，但仍跳过 `*Acknowledges*` 之后的致谢（致谢可翻译）

## 翻译时的分块策略

单文件超过 30KB 时，分块 read：

```bash
# 找到翻译终点
grep -n "\\\\appendix" main.tex  # 比如第 460 行
# 翻译范围：line 1 ~ line 460
```

然后用 read 分段：
- `read(path=..., limit=300)` 读 1-300 行
- `read(path=..., offset=300, limit=300)` 读 300-600 行
- 以此类推

每段 edit 翻译，然后 read 下一段。**不要**一次性 read 整文件再 write（容易超 token limit）。

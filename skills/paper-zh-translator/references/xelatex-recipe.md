# xelatex 编译 Recipe — 论文中文版编译命令与故障排查

> 这篇总结的是把英文 LaTeX 论文翻译成中文后重新编译 PDF 的标准流程。覆盖最常见的模板（CVPR/ICCV/ECCV/NeurIPS/ICLR）和常见错误。

## 标准编译命令

```bash
cd "<论文目录>"
xelatex -interaction=nonstopmode <入口文件名>
bibtex <入口名（不带 .tex）>
xelatex -interaction=nonstopmode <入口文件名>
xelatex -interaction=nonstopmode <入口文件名>
```

需要 4 轮：1 次 xelatex 让 LaTeX 知道引用，1 次 bibtex 生成 .bbl，2 次 xelatex 解析交叉引用和参考文献。

## 为什么不用 latexmk

`latexmk -xelatex` 在 xelatex 下会出问题：

1. latexmk 的 `-xelatex` 模式会先调用 xelatex，然后立刻调 bibtex
2. 此时 .aux 还没稳定（只有空 citation 列表）
3. bibtex 报"I found no \citation commands"后停止
4. latexmk 认为编译失败

手动 xelatex → bibtex → xelatex → xelatex 循环更稳。

## 各模板的参考文献样式

| 模板 | bibtex 样式 | .bst 文件 |
|------|------------|-----------|
| CVPR | `ieeenat_fullname` | `ieeenat_fullname.bst` |
| ICCV | `ieeenat_fullname` | `ieeenat_fullname.bst` |
| ECCV (llncs) | `splncs04` | `splncs04.bst` |
| NeurIPS | `plainnat` 或 `ieeetr` | 视项目而定 |
| ICLR (iclr2026) | natbib | `iclr2026_conference.bst` |
| 自定义 (fairmeta) | `plainnat` | `assets/plainnat` |
| NeurIPS 自带 (main) | `ieeetr` 或 `plain` | `ieeetr.bst` / `plain.bst` |

如果项目自带的 main.bbl 存在（多数情况），第一轮 xelatex 会自动检测并跳过 bibtex，但仍建议显式跑一次 bibtex 以防 .bbl 与新引用不匹配。

## 字体配置

### macOS

```latex
\usepackage{xeCJK}
\setCJKmainfont[BoldFont=Heiti SC,ItalicFont=Kaiti SC]{Songti SC}
\setCJKsansfont{Heiti SC}
\setCJKmonofont{Kaiti SC}
```

字体可用性检查：
```bash
fc-list :lang=zh-cn family | head -10
```

### Linux

```latex
\usepackage{xeCJK}
\setCJKmainfont[BoldFont=Noto Sans CJK SC,ItalicFont=AR PL UKai CN]{Noto Serif CJK SC}
\setCJKsansfont{Noto Sans CJK SC}
\setCJKmonofont{AR PL UKai CN}
```

## 常见错误与处理

### 1. `axessibility.sty` 与 xelatex 不兼容

错误信息：
```
! LaTeX Error: File `glyphtounicode.tex' not found.
```
或
```
! Undefined control sequence.
\pdfglyphtounicode
```

原因：`axessibility.sty`（来自 CVPR/ICCV 模板）使用了 pdfTeX 专用的 `\pdfglyphtounicode`、`\pdfcompresslevel` 等命令，xelatex 不支持。

**解决 A**（推荐，最小侵入）：注释掉 axessibility 加载
```latex
% \usepackage[accsupp]{axessibility}
```

**解决 B**（条件加载）：
```latex
\IfFileExists{axessibility.sty}{%
  \ifxetex\else
    \usepackage[accsupp]{axessibility}%
  \fi
}{}
```

**解决 C**（如果 axessibility 必须用）：
```latex
\providecommand{\pdfglyphtounicode}[2]{}
\providecommand{\pdfgentounicode}{}
```
在 `\usepackage{xeCJK}` 之后追加。

### 2. 没有 main.bib

错误：bibtex 报 "I found no \bibdata command" 或 "Database file #1: main.bib" 找不到。

**解决**：
- 如果源项目完全没带 .bib：worker 自己用占位条目构造 .bbl
- 从原 PDF 提取参考文献，构造最小 .bib：
  ```bibtex
  @article{ref1,
    author = {Author, A.},
    title = {Title},
    journal = {Journal},
    year = {2024}
  }
  ```

### 3. STSong 不可用

错误：
```
! Package xeCJK Error: The font "STSong" does not have the CJK shape.
```

**解决**：用 Songti SC（macOS）或 Noto Serif CJK SC（Linux）。

### 4. 编译产物 < 1MB

几乎肯定是 LaTeX 报错中断。

```bash
grep -E "^!" main.log | head -20
```

### 5. `LaTeX Warning: Reference 'X' on page Y undefined`

正常现象。三轮 xelatex 后会解决。如果三轮后仍有，说明 `\label`/`\ref` 不匹配或忘了 `\bibliography{main}`。

### 6. `Font shape 'TU/...' undefined`

字体回退警告。不影响输出，可以忽略。

### 7. `multiply-defined labels`

附录里重复的 `\label`，原版就有，翻译不引入。可以忽略或重命名其中一个。

## 编译后清理

- 不要在网络共享上 `rm` 大量文件（可能被策略阻止）
- 保留中间文件 `.aux`、`.bbl`、`.log`、`.out`、`.fls`、`.fdb_latexmk`、`.synctex.gz`
- 如果要清理：等编译成功后用 trash 工具或 `mavis-trash`

## 验证产物

```bash
ls -la <工作目录>/<入口名>.pdf  # 确认存在且 > 1MB
```

用 `read` 工具读 PDF 第 1-3 页：
```python
read(path="<PDF 路径>", pages="1-3")
```

中文显示正常的标志：
- abstract 段为中文
- 第一个 `\section{...}` 标题为中文（如"引言"）
- 一段正文为中文
- 模型名（DiT、CLIP、SkyReels-A2 等）保持英文
- 引用编号 `[18, 38]` 格式正常

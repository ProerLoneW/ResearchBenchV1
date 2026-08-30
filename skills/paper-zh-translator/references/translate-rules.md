# Translation Rules — 论文翻译规则

> 翻译的目标是**信达雅**，让中文母语读者能流畅阅读，同时保留原论文的所有技术信息。

## 必须保留（不译）

- **LaTeX 命令**：`\cite{...}`、`\citep{...}`、`\citet{...}`、`\label{...}`、`\ref{...}`、`\eqref{...}`、`\input{...}`、`\include{...}`、`\includegraphics{...}`、`\begin{...}`、`\end{...}` 等
- **数学公式**：`$...$`、`$$...$$`、`\(...\)`、`\[...\]`、各种 `\mathbf`、`\mathcal`、`\mathcal{N}`、`\sim`、`\to`、`\approx`、`\uparrow`、`\downarrow` 等数学符号
- **变量与数学符号**：希腊字母（`\alpha`、`\beta`、`\theta`）、下标（`x_t`、`F_T`）、公式编号 `(1)`、`(2)` 等
- **模型/方法名**：`DiT`、`MMDiT`、`VAE`、`CLIP`、`ArcFace`、`LoRA`、`RoPE`、`Wan`、`CogVideoX`、`HunyuanVideo`、`SkyReels-A2`、`VACE`、`Phantom` 等，保持英文
- **技术术语**（首次出现时）保持英文：`S2V`、`E2V`、`T2V`、`I2V`、`R2V`、`V2V`、`MV2V`、`SDR`、`CFG`、`RoI`、`RoPE` 等
- **数据集/基准名**：`Panda70M`、`VBench`、`A2-Bench`、`MSRVTT-Personalization`、`Subject200k` 等保持英文
- **URL / DOI / 邮箱**：`https://...`、`doi:...`、`xxx@xxx.com` 等
- **作者信息**：`\author{...}`、`\date{...}`、`\institute{...}`、`\affiliation{...}` 整段不译
- **机构名**（如 `ByteDance Intelligent Creation`、`Meta AI`、`Tongyi Lab`）保持英文
- **参考文献条目**（由 bibtex 渲染的部分）：不译
- **Teaser/首页大图 caption**（如 `\begin{strip}` / `\begin{figure}[H]` 紧接 `\maketitle` 后的）：按规则不译

## 必须翻译

- **正文段落**：所有叙述性英文段落 → 通顺的中文
- **`\section{...}` 标题**：`Introduction` → `引言`、`Related Work` → `相关工作`、`Method` / `Methodology` → `方法`、`Experiments` → `实验`、`Conclusion` → `结论`、`Ablation Study` → `消融研究`、`Acknowledgment(s)` → `致谢`
- **`\subsection{...}` 标题**：按内容翻译
- **`\paragraph{...}` / `\textbf{...}` 小标题**：按内容翻译
- **图/表 caption** (`\caption{...}`)：描述性文字全部翻译。**注意**：caption 里如果包含在图/表内引用的术语（如 `Table 1`），保留英文术语
- **列表项**（`\item ...`）：翻译为中文，保留列表结构
- **数学公式外的标点**：中文段落用全角标点（，。；！？），公式内用半角
- **致谢正文**：翻译（如 "We would like to thank..." → "我们感谢..."）

## 翻译风格

- **信达雅**：技术论文风格，流畅自然
- **段落结构**：保留原段落的逻辑顺序，必要时调整句式让它符合中文表达
- **专有名词首次出现**：可加括号注释（如 `MMDiT（多模态扩散 Transformer）`），后续单独使用
- **避免直译**：不要逐词翻译，按中文表达习惯重组
- **避免过度本地化**：模型名、方法名、术语严格保留英文
- **避免冗余**：原文如果有"如图 X 所示"等可以直接对应"如图 \ref{fig:foo} 所示"

## 典型翻译对照

| 英文 | 中文 |
|------|------|
| `In this section, we present...` | `本节将介绍...` |
| `We propose \textbf{X}, a method for...` | `我们提出 \textbf{X}，一种用于...的方法。` |
| `As shown in Figure \ref{fig:foo}, ...` | `如图 \ref{fig:foo} 所示，...` |
| `The results demonstrate that...` | `结果表明，...` |
| `We conduct experiments on...` | `我们在...上进行实验。` |
| `For more details, please refer to the supplementary material.` | `更多细节请参见补充材料。` |
| `Equal Contributions.` | `同等贡献。`（脚注通常保持简短） |

## 编辑工具使用建议

- **read 全文后再 edit 大段**：适合 < 200 行的文件
- **分段 read + edit**：适合 > 300 行的单文件（每次 read 100-300 行）
- **不要用 `replace_all: true`**：会误改（除非确实全文替换某个固定字符串）
- **edit 失败时**：先重新 read 一次，看是否有微小字符差异（特别是换行、引号）
- **保留缩进**：edit 时 `old_string`/`new_string` 的缩进要严格匹配

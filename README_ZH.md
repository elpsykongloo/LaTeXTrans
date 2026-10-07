## LaTeXTrans — 持续维护的增强版 fork

这里是 [elpsykongloo/LaTeXTrans](https://github.com/elpsykongloo/LaTeXTrans) 的 **0.2.0 维护版**，基于 [NiuTrans/LaTeXTrans](https://github.com/NiuTrans/LaTeXTrans)。本仓库独立维护增强版的发布与问题反馈，不代表 NiuTrans 官方版本。

增强版加入断点续传、双语 PDF、受控请求并发、私有配置、用量报告和局部编译修复。续传与双语输出参考 [Saverm666 的 fork](https://github.com/Saverm666/LaTeXTrans)，解析与重建改进参考 [Hydrofoooil 的 TeXClaudeTrans](https://github.com/Hydrofoooil/TeXClaudeTrans)。保留原项目 MIT 许可和版权声明。

详见[版本说明](CHANGELOG.md)与[贡献及验证流程](CONTRIBUTING.md)。适合上游的独立小修复会单独贡献回原仓库。

<div align="center">

[English](README.md) | 中文



<img src="./logo.png" width="1000px"></img>

  **Turn arXiv Papers into Multilingual Masterpieces**
#
<!-- <p align="center">
  <a href="https://arxiv.org/abs/2503.06594" alt="paper"><img src="https://img.shields.io/badge/Paper-LaTeXTrans-blue?logo=arxiv&logoColor=white"/></a>
</p> -->

</div>

<div align="center">
<p dir="auto">

• 📖 [介绍](#-介绍) 
• 🛠️ [安装指南](#️-安装指南) 
• ⚙️ [配置说明](#️-配置说明)
• 📚 [使用方式](#-使用方式)
• 🖼️ [翻译样例](#️-翻译样例) 

</p>
</div>

 从 arXiv 论文 ID 到译文 PDF 的端到端翻译。LaTeXTrans 有如下的特点和优势 :
 - **🌟 保持公式、排版和交叉引用的完整性**
 - **🌟 保证术语翻译的一致性**
 - **🌟 支持从原文 LaTeX 源码（通过提供的 arXiv 论文 id 自动下载）到译文 PDF 的端到端翻译**

借助 LaTeXTrans，研究人员和学生可以得到更高质量的论文翻译而无需担心格式混乱或内容缺失，从而更高效地阅读和理解 arXiv 论文。

# 📖 介绍

LaTeXTrans 是一个基于多智能体协作的结构化 LaTeX 文档翻译系统. 该系统能够直接翻译 LaTeX 代码，并生成与原文排版高度一致的译文 PDF。 不同于传统文档翻译方法（例如 PDF 翻译）容易破坏公式和格式，该系统使用大模型直接翻译预处理过的论文 LaTeX 源码，并通过由 Parser, Translator, Validator, Summarizer, Terminology Extractor, Generator 这六个智能体组成的工作流实现了排版一致和格式保持. 

 
# 🛠️ 安装指南

#### 1. 克隆仓库

使用 Python **3.10–3.13**。CI 覆盖 Linux / Windows 的 Python 3.10、3.12，以及 Linux 的 Python 3.13。建议先创建虚拟环境：

```bash
git clone https://github.com/elpsykongloo/LaTeXTrans.git
cd LaTeXTrans
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
```

#### （可选）使用 Conda 虚拟环境

```bash
conda create -n latextrans python=3.10 -y
conda activate latextrans
git clone https://github.com/elpsykongloo/LaTeXTrans.git
cd LaTeXTrans
pip install -e .
```

#### 2. 安装MikTex（推荐, 更轻量）或TeXLive

如需编译LaTeX文件（例如生成PDF输出），需要安装 [MikTex](https://miktex.org/download) 或 [TeXLive](https://www.tug.org/texlive/) !

 > [!IMPORTANT]
*对于 MikTex，安装时请务必选择 “install on the fly”，此外，您需要额外安装 [Strawberry Perl](http://strawberryperl.com/) 支持编译。


# ⚙️ 配置说明


公共默认值保存在 `config/default.toml`。将 API 密钥和个人设置写入同目录下的
`config/local.toml`（已加入 `.gitignore`），例如：

```toml
[llm_config]
model = "your_model"
api_key = "your_api_key"
base_url = "https://api.example.com/v1" # 替换为服务商的真实地址
concurrency_limit = 10
```

配置按“主配置 → 同目录 `local.toml` → 环境变量 → CLI / GUI 参数”的顺序覆盖。
环境变量支持 `LATEXTRANS_API_KEY`、`LATEXTRANS_BASE_URL` 和 `LATEXTRANS_MODEL`。
Base URL 可使用服务根地址、`/v1` 地址或完整 `/chat/completions` 地址。
公共默认配置的模型、API 地址和密钥均留空，请按所用服务商填写。通过 wheel 安装后可直接使用包内默认配置，无需仓库目录；在当前工作目录创建 `config/local.toml` 保存个人覆盖项，或通过 `--config /path/to/config.toml` 指定配置。

默认 LLM 并发上限为 10；HTTP 429、502、503、504 会退避重试，并遵守 `Retry-After`。
`[llm_config]` 下可设置 `timeout`（秒，兼容上游）、`max_tokens`、`temperature`、
`max_chunk_chars`、`glossary_max_terms`。术语表在所有翻译模式中生效。

如需把标题、摘要、章节名作为翻译上下文，可设置 `use_context = true`；
`context_summary = true` 会额外调用模型生成摘要。每篇输出的 `usage.json` 记录请求和 token
用量，设置 `price_input_per_mtok` / `price_output_per_mtok` 后可按用户提供的价格估算费用。

请使用支持 OpenAI 兼容 Chat Completions 请求格式的服务。模型名称和 API 地址依服务商而异，请查阅其文档；思考模式等服务商特有选项可单独配置。


# 📚 使用方式

### 通过 ArXiv ID 翻译
只需提供 arXiv 论文 ID 即可完成翻译：

```bash
latextrans --arxiv ${xxxx}
# For example, 
# latextrans --arxiv 2508.18791
```

现在也支持带版本号的 arXiv ID，可以指定论文的具体修订版本：

```bash
latextrans --arxiv 2508.18791v2
```

该命令将：

1. 从 arXiv 下载 LaTeX 源码并解压
2. 执行由解析、翻译、重构和编译组成的工作流
3. 在 outputs 文件夹保存翻译后的论文 LaTeX 项目文件和编译生成的译文PDF

### 批量翻译 ArXiv 论文

支持逗号分隔的多个 arXiv ID：

```bash
latextrans --arxiv ${xxxx}, ${xxxx}
# For example,
# latextrans --arxiv 2508.18791v2, 2407.01648
```

### 翻译本地项目

本地压缩包（`.zip/.tar/.tar.gz/.tgz`）：

```bash
latextrans --project D:\\path\\to\\paper_source.tar.gz
```

本地已解压项目目录：

```bash
latextrans --project D:\\path\\to\\paper_project_dir
```

当你提供 `--arxiv` 或 `--project` 时，LaTeXTrans 只会处理这些显式指定的输入，
不会扫描并翻译 `tex source` 下其他已有项目。

如果你确实要处理 `tex source` 下全部已有项目，请使用：

```bash
latextrans --all-existing
```

### 断点续传、双语 PDF 与编译修复

默认开启断点续传。中断后运行相同命令，会保留已解析的数据和通过校验的翻译片段，
仅处理未完成的部分；已经完成且 PDF 仍存在的项目会直接复用。源文件的路径、大小、
修改时间，以及翻译配置和术语文件元数据用于判断是否可复用。改变模型、语言、术语或
分块设置后会重新翻译；仅改变编译修复选项时保留翻译并重新生成 PDF。

```bash
# 重新翻译，忽略现有续传记录（--no-resume 也可关闭续传）
latextrans --project "D:\\path\\to\\paper" --force

# 左右对照：原文在左，译文在右
latextrans --arxiv 2508.18791v2 --bilingual

# 逐页交错；本地项目可明确指定原文 PDF
latextrans --project "D:\\path\\to\\paper" --bilingual --bilingual-layout interleaved --original-pdf "D:\\path\\to\\original.pdf"

# 关闭编译自动修复和下载目录副本
latextrans --project "D:\\path\\to\\paper" --no-compile-repair --no-downloads
```

双语功能依赖 `pypdf`（安装项目时自动安装），两份 PDF 页数不同时补空白页。
arXiv 项目使用下载的原文 PDF；本地项目可使用 `--original-pdf` 或顶层 `original_pdf`。
双语合成失败会明确报告失败，同时保留已生成的译文 PDF。

可修复的编译错误会额外调用 LLM，并可能产生服务商费用。默认允许编译自动修复，顶层配置为 `compile_repair = true`、`compile_repair_attempts = 2`
（每次编译最多允许 5 轮修复）。
编译失败时模型只接收错误附近的有限 LaTeX 上下文，修复只写入生成的译文项目；
原文不被修改。修复前文件备份、`repair_report.json` 和 `repair_usage.json` 保留在输出目录。
缺少编译工具、包或字体等环境问题会报告诊断。任何项目翻译、校验、编译或双语合成失败，
CLI 均返回非零退出码。

校验允许正文独立约号和省略号的等价 Unicode 写法，如 `\(\sim\)` → “～”、
`\ldots` → “……”；公式内部的命令、数学定界符及结构占位符仍保持严格检查。
顶层 `validation_prose_equivalences = false` 可关闭这项等价处理。

解析器保护 `verbatim` / `Verbatim` / `lstlisting` / `minted` 等代码环境和行内代码，
支持带星号的列表、`captionof` 类型参数、作者 `thanks` 与宏定义内显式的可见文字命令。
主文件选择保留 `00README.json` 与已记录主文件的优先级，常规候选优先浅层论文主稿。

断点续传和双语输出参考 [Saverm666 的 fork](https://github.com/Saverm666/LaTeXTrans)，
解析与重建稳健性修复参考 [Hydrofoooil 的 TeXClaudeTrans](https://github.com/Hydrofoooil/TeXClaudeTrans)，
并按本地架构适配。双语 PDF 按页配对，译文分页变化可能使同页内容位置不同。

### Streamlit GUI

如果你想使用带有进度显示、日志和可配置参数的图形界面，可以运行：

```bash
latextrans-gui
```

也可以直接使用 Streamlit：

```bash
streamlit run src/gui/streamlit_app.py
```

 > [!NOTE]
LaTeXTrans 可配置源语言和目标语言，当前提供中文与日文的 Unicode 引擎编译适配。
其他目标语言的排版仍需根据具体模板验证。

# 🖼️ 翻译样例

以下是 **LaTeXTrans** 生成的四个真实翻译样例，左侧为原文，右侧为译文。

### 📄 样例 1 ( 英文->中文 ) :

<table>
  <tr>
    <td align="center"><b>原文</b></td>
    <td align="center"><b>译文</b></td>
  </tr>
  <tr>
    <td><img src="examples/case1src.png" width="100%"></td>
    <td><img src="examples/case1ch.png" width="100%"></td>
  </tr>
</table>

### 📄 样例 2 ( 英文->中文 ):

<table>
  <tr>
    <td align="center"><b>原文</b></td>
    <td align="center"><b>译文</b></td>
  </tr>
  <tr>
    <td><img src="examples/case3src.png" width="100%"></td>
    <td><img src="examples/case3ch.png" width="100%"></td>
  </tr>
</table>

### 📄 样例 3 ( 英文->日文 ):

<table>
  <tr>
    <td align="center"><b>原文</b></td>
    <td align="center"><b>译文</b></td>
  </tr>
  <tr>
    <td><img src="examples\case-en.png" width="100%"></td>
    <td><img src="examples\case-jp.png" width="100%"></td>
  </tr>
</table>

### 📄 样例 4 ( 英文->日文 ):

<table>
  <tr>
    <td align="center"><b>原文</b></td>
    <td align="center"><b>译文</b></td>
  </tr>
  <tr>
    <td><img src="examples\case5a-1-en.png" width="100%"></td>
    <td><img src="examples\case5b-1-jp.png" width="100%"></td>
  </tr>
</table>

📂 **更多样例请查看[`examples/`](examples/) 文件夹**, 包含每个样例的完整翻译 PDF。

---
## 致谢

感谢为本系统做出贡献的同学：
徐浩淞，刘奕扬，张恒，李晨阳，黄小茹，张一航，刘策，张硕昌，于济航，刘博扬。


---

## Citation
```bash
@article{zhu2025latextrans,
  title={LaTeXTrans: Structured LaTeX Translation with Multi-Agent Coordination},
  author={Zhu, Ziming and Wang, Chenglong and Xing, Shunjie and Huo, Yifu and Tian, Fengning and Du, Quan and Yang, Di and Zhang, Chunliang and Xiao, Tong and Zhu, Jingbo},
  journal={arXiv preprint arXiv:2508.18791},
  year={2025}
}

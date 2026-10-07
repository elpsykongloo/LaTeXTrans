## LaTeXTrans — maintained fork

This is the **0.2.0 maintained fork** at [elpsykongloo/LaTeXTrans](https://github.com/elpsykongloo/LaTeXTrans), based on [NiuTrans/LaTeXTrans](https://github.com/NiuTrans/LaTeXTrans). Fork releases and support are maintained here; this is not an official NiuTrans release.

The enhanced workflow adds resumable translation, bilingual PDFs, bounded request concurrency, private configuration, usage reports, and localized compilation repair. Resume and bilingual output draw on [Saverm666's fork](https://github.com/Saverm666/LaTeXTrans); parser and reconstruction improvements draw on [Hydrofoooil's TeXClaudeTrans](https://github.com/Hydrofoooil/TeXClaudeTrans). The original MIT license and copyright are retained.

See [release notes](CHANGELOG.md) and [contribution and verification guidance](CONTRIBUTING.md). Small fixes are contributed back to upstream independently.

<div align="center">

English | [中文](README_ZH.md)


<img src="./logo.png" width="1000px"></img>

  **Turn arXiv Papers into Multilingual Masterpieces**
#
<!-- <p align="center">
  <a href="https://arxiv.org/abs/2503.06594" alt="paper"><img src="https://img.shields.io/badge/Paper-LaTeXTrans-blue?logo=arxiv&logoColor=white"/></a>
</p> -->

</div>

<div align="center">
<p dir="auto">

• 📖 [Introduction](#-introduction) 
• 🛠️ [Installation Guide](#️-installation-guide) 
• ⚙️ [Configuration Guide](#️-configuration-guide)
• 📚 [Usage](#-Usage)
• 🖼️ [Translation Examples](#️-translation-examples) 

</p>
</div>

 End-to-end translation from arXiv paper ID to translated PDF. LaTeXTrans have the following **Features** :
 - **🌟 Preserve the integrity of formulas, layout, and cross-references**
 - **🌟 Ensure consistency in terminology translation**
 - **🌟 Support end-to-end conversion from original TeX source (automatically downloaded based on the arXiv paper id provided) to translated PDF**

With LaTeXTrans, researchers and students can obtain higher-quality arXiv paper translations without worrying about formatting confusion or missing content, thus reading and understanding arXiv papers more efficiently.

# 📖 Introduction

LaTeXTrans is a structured LaTeX document translation system based on multi-agent collaboration. It directly translates LaTeX code and generates translated PDFs with high fidelity to the original layout. Unlike traditional document translation methods (e.g., PDF translation), which often break formulas and formatting, LaTeXTrans leverages LLM to translate preprocessed LaTeX sources and employs a workflow composed of six agents—Parser, Translator, Validator, Summarizer, Terminology Extractor, and Generator to achieve the features. The figure below illustrates the system architecture of LaTeXTrans. 
<!-- For a more detailed introduction, please refer to our published paper 🔗 [LaTeXTrans: Structured LaTeX Translation with Multi-Agent Coordination](https://arxiv.org/abs/2508.18791). -->

<!-- <img src="./main-figure.jpg" width="1000px"></img> -->


# 🛠️ Installation Guide

#### 1. Clone Repository

Use Python **3.10–3.13**. CI checks Linux and Windows with Python 3.10/3.12 and Linux with Python 3.13. Create a virtual environment before installing:

```bash
git clone https://github.com/elpsykongloo/LaTeXTrans.git
cd LaTeXTrans
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
```

#### (Optional) Use Conda Environment

```bash
conda create -n latextrans python=3.10 -y
conda activate latextrans
git clone https://github.com/elpsykongloo/LaTeXTrans.git
cd LaTeXTrans
pip install -e .
```

#### 2. Install MikTex(Recommended) or TeXLive

If you need to compile LaTeX files (e.g., generate PDF output), install [MikTex](https://miktex.org/download) or [TeXLive](https://www.tug.org/texlive/) !

 > [!IMPORTANT]
For MikTex, installation please be sure to select "install on the fly", in addition, you need to install additional [Strawberry Perl](http://strawberryperl.com/) support compilation.

# ⚙️ Configuration Guide

### Local Configuration

Keep shared defaults in `config/default.toml`. Store API credentials and personal
settings in the adjacent `config/local.toml`, which is excluded from Git:

```toml
[llm_config]
model = "your_model"
api_key = "your_api_key"
base_url = "https://api.example.com/v1" # Replace with your provider's actual endpoint
concurrency_limit = 10
```

Settings are applied in this order: main config, adjacent `local.toml`, environment
variables, then CLI / GUI overrides. Supported environment variables are
`LATEXTRANS_API_KEY`, `LATEXTRANS_BASE_URL`, and `LATEXTRANS_MODEL`. The endpoint may
be a service root, a `/v1` base, or a complete `/chat/completions` URL.

The public default leaves the model, endpoint, and API key empty; configure all three for your provider. When installed from a wheel, bundled defaults are available without a checkout; create `config/local.toml` in your working directory for personal overrides, or pass `--config /path/to/config.toml`.

The default request concurrency is 10. HTTP 429 and 502/503/504 responses retry with
backoff and honor `Retry-After`. The `[llm_config]` section also accepts `timeout`
(seconds, compatible with upstream), `max_tokens`, `temperature`, `max_chunk_chars`,
and `glossary_max_terms`. Glossary terms apply in every translation mode.
Set `use_context = true` to provide title, abstract, and section context;
`context_summary = true` makes an additional model call to summarize the document.
Each project's `usage.json` records requests and token usage. Input and output
prices per million tokens can be supplied with `price_input_per_mtok` and
`price_output_per_mtok` to estimate cost.

Use a provider that supports the OpenAI-compatible Chat Completions request format. Model availability and endpoint URLs are provider-specific; consult your provider's documentation. Provider-specific thinking options can be configured separately.

# 📚 Usage

###  Translation via ArXiv ID 

Simply provide an arXiv paper ID to complete translation:

```bash
latextrans --arxiv ${xxxx}
# For example, 
# latextrans --arxiv 2508.18791
```

Versioned arXiv IDs are also supported, so you can target a specific revision:

```bash
latextrans --arxiv 2508.18791v2
```

This command will:

1. Download the LaTeX source code from arXiv and extract it
2. Execute a workflow consisting of parsing, translation, refactoring and compilation
3. Save the translated LaTeX project file of the paper and the PDF of the compiled translation in the outputs folder

### Batch Translation via ArXiv IDs

You can translate multiple arXiv papers in one run (comma-separated):

```bash
latextrans --arxiv ${xxxx}, ${xxxx}
# For example,
# latextrans --arxiv 2508.18791v2, 2407.01648
```

### Translation via Local Project

You can also pass a local compressed source package directly:

```bash
latextrans --project D:\\path\\to\\paper_source.tar.gz
```

Or pass a local extracted project directory:

```bash
latextrans --project D:\\path\\to\\paper_project_dir
```

When you provide `--arxiv` or `--project`, LaTeXTrans only processes those explicit inputs.
Existing folders under `tex source` are ignored in this mode.

To process every existing project under `tex source`, run:

```bash
latextrans --all-existing
```

### Resume, bilingual PDFs, and compile repair

Resume is enabled by default. Repeating the same command after an interruption
reuses parsed data and validated translation units, then processes the remaining
work. A completed project reuses its PDF if it still exists. Checkpoints use source
paths, file sizes, modification times, translation settings, and glossary metadata.
Changing the model, language, glossary, or chunk settings invalidates translations;
changing only compile repair options preserves translation and regenerates the PDF.

```bash
# Start a fresh translation; --no-resume also disables checkpoint reuse
latextrans --project "D:\\path\\to\\paper" --force

# Original on the left and translation on the right
latextrans --arxiv 2508.18791v2 --bilingual

# Interleave original and translated pages using an explicit local original PDF
latextrans --project "D:\\path\\to\\paper" --bilingual --bilingual-layout interleaved --original-pdf "D:\\path\\to\\original.pdf"

# Disable compile repair and the titled copy in Downloads
latextrans --project "D:\\path\\to\\paper" --no-compile-repair --no-downloads
```

Bilingual output uses `pypdf`, installed with the project, and pads unequal page
counts with blank pages. arXiv projects use the downloaded original PDF; local
projects can specify `--original-pdf` or the top-level `original_pdf` setting.
A bilingual export failure is reported as a failure while preserving the translated PDF.

Compile repair makes additional LLM requests and may incur provider charges when a compilation error is repairable. It is enabled with the top-level settings `compile_repair = true` and
`compile_repair_attempts = 2`, with a hard limit of five attempts. The model receives limited context around TeX errors
and edits only the generated translation. Backups, `repair_report.json`, and
`repair_usage.json` are retained in the output directory. Missing tools, packages,
or fonts produce diagnostics. Any translation, validation, compilation, or
bilingual export failure makes the CLI exit with a nonzero status.

Validation accepts equivalent standalone prose symbols, such as `\(\sim\)` and
“～”, or `\ldots` and “……”. Mathematical expressions, delimiters, and structural
placeholders remain strict. Set `validation_prose_equivalences = false` to disable
the prose equivalences.

The parser protects code environments such as `verbatim`, `Verbatim`, `lstlisting`,
and `minted`, as well as inline code. It handles starred lists, `captionof` type
arguments, author `thanks`, and explicit visible text commands inside macro
definitions. Main-file selection honors `00README.json` and recorded main files,
then favors shallow paper sources over supplementary candidates.

Resume and bilingual output were adapted from [Saverm666's fork](https://github.com/Saverm666/LaTeXTrans).
Parser and reconstruction fixes were adapted from [Hydrofoooil's TeXClaudeTrans](https://github.com/Hydrofoooil/TeXClaudeTrans).
Bilingual PDFs pair pages by index; changes in translated pagination can shift the
content on corresponding pages.

### GUI via Streamlit

If you want a browser-based GUI with live workflow progress, logs, and runtime configuration, launch:

```bash
latextrans-gui
```

Or run Streamlit directly:

```bash
streamlit run src/gui/streamlit_app.py
```

 > [!NOTE]
Source and target languages are configurable. Chinese and Japanese currently have
Unicode engine compilation support; other target languages need template-specific
layout validation.

<!-- # 🧰 Experimental Results

| System | COMETkiwi | LLM-score | FC-score | Cost |
|:-|:-:|:-:|:-:|:-:|
|NiuTrans |64.69|7.93|60.72|-|
|Google Translate |46.23|5.93|51.00|-|
|LLaMA-3.1-8b|42.89|2.92|49.40|-|
|Qwen-3-8b|45.55|7.87|48.68|-|
|Qwen-3-14b|68.18|8.76|65.63|-|
|DeepSeek-V3|67.26|**9.02**|63.68|$0.02|
|GPT-4o|67.22|8.58|58.32|$0.13|
|**LaTeXTrans(Qwen-3-14b)**|71.37|8.97|71.20|-|
|**LaTeXTrans(DeepSeek-V3)**|73.48|9.01|70.52|$0.10|
|**LaTeXTrans(GPT-4o)**|**73.59**|8.92|**71.52**|$0.35|

Note:
- **COMETkiwi** : a quality estimation model ([wmt22-cometkiwi-da](https://huggingface.co/Unbabel/wmt22-cometkiwi-da)) that reflects the quality of the translation, the higher the score, the better the translation quality.
- **LLM-score** : a method for evaluating the quality of translation using LLM (GPT-4o), the higher the score, the better the translation quality.
- **FC-score** : a method proposed in our paper to evaluate the formatting ability of LaTeX translation by detecting the number of errors in the compiled logs, the higher the score, the better the ability to maintain format.
- **Cost** : the average cost of translating each paper using the official API. -->
  


# 🖼️ Translation Examples

The following are four real translation examples generated by **LaTeXTrans**, with the original text on the left and translation results on the right.

### 📄 Case 1 ( en->ch ) :

<table>
  <tr>
    <td align="center"><b>Original</b></td>
    <td align="center"><b>Translation</b></td>
  </tr>
  <tr>
    <td><img src="examples/case1src.png" width="100%"></td>
    <td><img src="examples/case1ch.png" width="100%"></td>
  </tr>
</table>

### 📄 Case 2 ( en->ch ):

<table>
  <tr>
    <td align="center"><b>Original</b></td>
    <td align="center"><b>Translation</b></td>
  </tr>
  <tr>
    <td><img src="examples/case3src.png" width="100%"></td>
    <td><img src="examples/case3ch.png" width="100%"></td>
  </tr>
</table>

### 📄 Case 3 ( en->jp ):

<table>
  <tr>
    <td align="center"><b>Original</b></td>
    <td align="center"><b>Translation</b></td>
  </tr>
  <tr>
    <td><img src="examples\case-en.png" width="100%"></td>
    <td><img src="examples\case-jp.png" width="100%"></td>
  </tr>
</table>

### 📄 Case 4 ( en->jp ):

<table>
  <tr>
    <td align="center"><b>Original</b></td>
    <td align="center"><b>Translation</b></td>
  </tr>
  <tr>
    <td><img src="examples\case5a-1-en.png" width="100%"></td>
    <td><img src="examples\case5b-1-jp.png" width="100%"></td>
  </tr>
</table>

📂 **See [`examples/`](examples/) folder for more cases**, including complete translation PDFs for each case.

---
## Acknowledgments

We would like to thank all the students who contributed to this project：
Haosong Xv, Yiyang Liu, Heng Zhang, Xiaoru Huang, Yihang Zhang, Ce Liu, Shuochang Zhang, Jihang Yv, Boyang Liu.

---
## Citation
```bash
@article{zhu2025latextrans,
  title={LaTeXTrans: Structured LaTeX Translation with Multi-Agent Coordination},
  author={Zhu, Ziming and Wang, Chenglong and Xing, Shunjie and Huo, Yifu and Tian, Fengning and Du, Quan and Yang, Di and Zhang, Chunliang and Xiao, Tong and Zhu, Jingbo},
  journal={arXiv preprint arXiv:2508.18791},
  year={2025}
}

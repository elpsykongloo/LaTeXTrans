# arXiv 真实论文迭代记录

目标：通过 5–10 轮真实下载、翻译、校验、编译和人工页面复核，发现并修复实际使用中的问题。

开始状态：`b779d32`，工作区只有预先存在的未跟踪 `uv.lock`。使用本机现有模型配置；不记录凭据，不做哈希或逐字节校验。每轮先运行公开 CLI，保留失败和警告，再修复并按相同入口续跑，复用有效译文。

## 样本与进度

| 轮次 | 论文与来源 | 主要覆盖 | 状态 |
|---|---|---|---|
| 1 | [Attention Is All You Need, 1706.03762v7](https://arxiv.org/abs/1706.03762v7) | NeurIPS 模板、多文件、表格、注意力附录图 | 修复并复核完成，14 页 |
| 2 | [Deep Residual Learning for Image Recognition, 1512.03385v1](https://arxiv.org/abs/1512.03385v1) | CVPR 双栏、跨栏图表、网络结构表 | 完成双栏泛化验证，11 页 |
| 3 | [An Image is Worth 16x16 Words, 2010.11929v2](https://arxiv.org/abs/2010.11929v2) | ICLR、多图、长附录 | 修复驱动与脚注漏译，20 页 |
| 4 | [Optimality of the Johnson-Lindenstrauss Lemma, 1609.02094v2](https://arxiv.org/abs/1609.02094v2) | 数学证明、定理、密集公式 | 修复重新编译日期，8 页 |
| 5 | [BERT, 1810.04805v2](https://arxiv.org/abs/1810.04805v2) | ACL 双栏、模型名称、自定义宏、表格 | 修复 px 图片尺寸，14 页 |
| 6 | [Observation of Gravitational Waves from a Binary Black Hole Merger, 1602.03837v1](https://arxiv.org/abs/1602.03837v1) | 物理论文、源码不可用时的用户体验 | 正确识别只有 PDF，不计入生成成功 |
| 7 | [Polynomial-Time Algorithms for Prime Factorization and Discrete Logarithms on a Quantum Computer, quant-ph/9508027v2](https://arxiv.org/abs/quant-ph/9508027v2) | 旧编号、LaTeX 2.09、量子算法、长公式、伪代码 | 修复路径与旧模板，22 页 |

第 6 轮确认为仅 PDF 的提交，因此增加第 7 轮；总计 7 轮真实输入，其中 6 篇生成中文 PDF。

## 检查标准

- 从 arXiv URL 通过标准 CLI 开始，记录原始退出状态、警告及用量。
- 对照原文检查标题、摘要、正文、脚注、表格、公式、引用、图注及附录；代码、参考文献和图片内嵌文字的保留须明确区分。
- 渲染全篇页面预览，单独查看高风险页面；检查图文重叠、错列、页边溢出、异常空栏、文字缺失及字体问题。
- 将实际问题缩减为可复现用例，修复公共逻辑并添加必要的回归测试。
- 用相同流程重跑并重新查看 PDF；每轮只有证据完整后才记为完成。
- 最后重查早期样本，防止后续修复引入回归。

本地运行日志和渲染预览：`tmp/real-world-iterations/`。正式输出：`outputs/real-world-iterations/`。这些下载与生成物不提交到 Git。

## 逐轮记录

### 第 1 轮

入口：`python -m main --arxiv https://arxiv.org/abs/1706.03762v7 --no-downloads --output outputs/real-world-iterations`

证据：`tmp/real-world-iterations/round-01-baseline.log`。

实际发现：表 3 中文表头使表格超出正文栏宽 33.56 pt，编译成功但日志有严重排版警告。其余正文、公式、作者区、图及注意力可视化完整。

修复：仅对 table/table* 内最外层的普通 tabular/tabular* 测量宽度，超出当前 linewidth 时按比例缩小；合适宽度不放大，作者区、嵌套单元格、longtable/tabularx 不改变。报告中记录缩放值。编译缓存版本升级，复用本轮全部译文。

复核：表 3 从 431.04 pt 缩至 397.48 pt，严重排版警告清零；原文 15 页、译文 14 页均渲染检查，修复后全篇 14 页预览及表格页 8 复核通过。没有物理页边文字溢出。12 项相关回归测试通过，其中真实 XeLaTeX 双栏测试同时验证只缩小超宽表格、不缩小正常表格。初次翻译 50 次请求、68,137 tokens，修复重跑无需翻译请求。

修复日志：`round-01-fixed.log`；预览：`round-01-original/`、`round-01-translated/`、`round-01-fixed/`；输出：`outputs/real-world-iterations/ch_1706.03762v7/ch_1706.03762v7.pdf`。

### 第 2 轮

标准 CLI：`--arxiv https://arxiv.org/abs/1512.03385v1 --no-downloads --output outputs/real-world-iterations`。

第 1 轮修复在实际双栏模板中处理了 5 张超宽表格：跨栏宽度 496.86 pt，单栏宽度 237.14 pt，按各自当前位置测量，没有统一缩成一栏。图注和正文、残差结构公式、附录跨栏表格保持完整。原文 12 页、译文 11 页全篇预览检查，译文页 5 和页 10 的图表详细复核通过，没有严重排版警告或物理页边文字溢出。本轮未发现需要额外改写排版的缺陷，保留作双栏回归样本，不人为制造修改。

翻译期间结构标记丢失由现有重试与分段恢复成功处理，最终独立校验 0 错误；85 次请求、128,668 tokens。日志：`round-02-baseline.log`；预览：`round-02-original/`、`round-02-translated/`；输出：`outputs/real-world-iterations/ch_1512.03385v1/ch_1512.03385v1.pdf`。

### 第 3 轮

标准 CLI：`--arxiv https://arxiv.org/abs/2010.11929v2 --no-downloads --output outputs/real-world-iterations`。

实际发现：原稿显式 `\\usepackage[pdftex]{graphicx}` 导致中文 XeLaTeX 运行仍加载 pdftex 图形驱动，TikZ 在 pgfsys-pdftex.def 调用不存在的 `\\pdfobj` 并终止。直接编译和 latexmk 均失败。

修复：移除图形相关宏包中固定的旧驱动选项，保留颜色、草稿、页面等其他选项，让宏包自动识别 XeTeX；同样适配输出副本内本地 .sty/.cls。保留注释、代码示例和无关宏包选项。真实 XeLaTeX 测试核对 graphicx 和 PGF 的实际驱动。16 项相关测试及 2 项子测试通过。

人工页面检查又发现摘要脚注仍为英文。新增独立脚注覆盖检查，原检查因外层段落已有中文而漏过它。仅增强校验后，旧提示仍令模型连续保留括号里的英文；明确要求翻译脚注和花括号内的自然语言后，单次摘要重翻成功，新增回归与相关测试共 11 项通过。最终 93 次累计请求、133,319 tokens。

复核：原文 22 页、译文 20 页全篇渲染；环绕注意力图页 7 细查无重叠，图像、附录和模型比较表完整。保留一处 17 pt 作者区盒子警告（maketitle 内模板的 centerline/tabular 组合），人工检查作者文字在页面内且未遮挡正文，不把所有警告伪称为清零。日志：`round-03-baseline.log`、`round-03-fixed.log`、`round-03-final.log`；输出：`outputs/real-world-iterations/ch_2010.11929v2/ch_2010.11929v2.pdf`。

### 第 4 轮

标准 CLI：`--arxiv https://arxiv.org/abs/1609.02094v2 --no-downloads --output outputs/real-world-iterations`。

原文 9 页、译文 8 页，密集公式、定理、证明、图形和参考文献均完整；全篇预览和公式页 3 复核没有错行或越界，严重排版警告为 0。发现原稿没有明确 date 声明，LaTeX 自动把旧论文日期替换为当前编译日。

修复：仅抑制默认的动态 today 日期，保留作者明确写出的日期和模板自定义日期，不猜测论文发布日期。3 项真实 pdfLaTeX 回归验证默认日期、显式 today 和固定历史日期。原文源码未修改；重跑复用全部译文。初次 67 次请求、89,490 tokens。日志：`round-04-baseline.log`、`round-04-fixed.log`；输出：`outputs/real-world-iterations/ch_1609.02094v2/ch_1609.02094v2.pdf`。

### 第 5 轮

标准 CLI：`--arxiv https://arxiv.org/abs/1810.04805v2 --no-downloads --output outputs/real-world-iterations`。

实际发现：图 2 的 `width=360px` 在 XeLaTeX 中报 Illegal unit of measure，局部模型修复也未成功。修复只处理 includegraphics 的 width/height/totalheight 数值尺寸，把 px 转为可配置的 pdfpxdimen 长度；遵循 [pdfTeX 手册的像素长度定义](https://tug.ctan.org/systems/doc/pdftex/manual/pdftex-a.pdf)，支持原稿设置像素密度。5 项图形回归验证默认尺寸、96 dpi、自定义驱动及代码/路径保护。

复核：原文 16 页、译文 14 页全篇渲染，页 4 的输入嵌入图及双栏脚注细查通过；表格与附录完整，严重排版警告 0。初次翻译 84 次请求、125,978 tokens；初次失败后的局部编译修复另有费用，完整累计值见 usage.json。日志：`round-05-baseline.log`、`round-05-fixed.log`；输出：`outputs/real-world-iterations/ch_1810.04805v2/ch_1810.04805v2.pdf`。

### 第 6 轮

标准 CLI：`--arxiv https://arxiv.org/abs/1602.03837v1 --no-downloads --output outputs/real-world-iterations`。

实际发现：源码入口返回 PDF，程序误报“压缩包不完整或不可读”，随后仍额外下载原文 PDF。修复通过格式头识别 PDF 响应，以 source_unavailable 状态说明当前 TeX 流程不支持只有 PDF 的提交，并提示改用提供源码的版本或本地 TeX 项目；取消无源码项目的重复原文下载。真实重跑提示明确，无翻译请求，没有把失败伪装为生成成功。模拟测试验证没有重试下载或调用翻译代理。日志：`round-06-baseline.log`、`round-06-fixed.log`。

### 第 7 轮

标准 CLI：`--arxiv https://arxiv.org/abs/quant-ph/9508027v2 --no-downloads --output outputs/real-world-iterations`。

实际发现与修复：旧编号中的 `/` 被当成本地路径，导致下载文件无法创建；统一采用 `quant-ph_9508027v2` 作为本地项目名，网络地址继续使用标准编号，源码、原文 PDF、续跑缓存和分类映射保持一致。下载后发现原稿使用 `documentstyle[twoside]{article}`，处于 LaTeX 2.09 兼容模式而不能加载中文包；对标准旧文档类迁移为 documentclass，保留双面、字号等类选项，额外样式改为宏包加载。还补齐 quant-ph 和 physics.comp-ph 等分类名识别。

复核：原文 28 页、译文 22 页全篇预览及页 17 长公式细查通过；公式编号、算法伪代码、图、参考文献完整，严重排版警告 0。88 次翻译请求、132,066 tokens，路径和模板修复续跑复用有效译文。日志：`round-07-baseline.log`、`round-07-fixed.log`、`round-07-final.log`；输出：`outputs/real-world-iterations/ch_quant-ph_9508027v2/ch_quant-ph_9508027v2.pdf`。

## 最终交叉回归

批量通过公开 CLI 重跑 6 篇有源码的样本时，发现多个 --arxiv 参数被重新拼成一个字符串，裸 ID 全部失效、URL 则可能只处理第一篇。修复参数拆分，尊重 shell 已经确定的参数边界，同时保留逗号列表和带空格的引用路径；增加两项回归测试。

额外重跑用户原始故障论文 2611.07071，发现自动表格适配与原稿 resizebox 的盒子状态冲突。修复为避开已有 resizebox/scalebox/adjustbox 的表格，并避免对内部 lrbox 设置环绕终止钩子。增加组合回归；14 项表格/环绕/漏译测试通过。此交叉验证不另计新论文轮数。

最终批量日志：`final-regression.log`；原始故障论文：`original-case-regression.log`、`original-case-fixed.log`；完整测试：`full-tests-final.log`。

最终结果：6 篇有源码样本全部通过标准批量流程，共生成 89 页中文译文；原始故障论文重新生成 77 页，排版报告无警告。全部最终页面均重新渲染并检查全篇预览，另详细复核原始论文的引言、环绕图、总结框和结果表格。首页六张导览卡片恢复三列，引言已翻译，环绕图后正文恢复正常栏宽。所有最终 PDF 的物理页边文字检查均无越界。

完整测试结果：344 passed、2 skipped、4 deselected、46 subtests passed。按用户要求，排除了包含文件哈希或逐字节比较的 4 个既有测试；本轮验证没有执行此类校验。最终批量续跑复用了有效译文，没有再次请求全篇翻译。

最终预览位于 `tmp/real-world-iterations/final-<项目名>/` 和 `final-original-case/`。用户原始论文的新交付副本：`C:\Users\86166\Downloads\塑造与监测模型：通过其内部表征_2611.07071_真实论文回归修正版.pdf`。

已知边界：ViT 的作者区仍保留一处 17 pt 盒子警告，人工检查未见文字越界或遮挡。参考文献、图像内部文字、代码和算法伪代码按现有策略保留原文，部分模板固定标签也仍为英文。本轮验证覆盖编译、结构、漏译检测和页面排版，并非逐句翻译质量审校；6 篇成功样本不能证明所有未见模板都已兼容。

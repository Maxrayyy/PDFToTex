# 表格列数与边界错误

## 现象

表格行列数不一致、空单元格被丢弃、`\\hline` 不在行边界，出现列错位或 `Misplaced \\noalign`。

## 已尝试方案

- 根据列数自动补字段：复杂表格中无法可靠猜测缺失内容。
- 删除空单元格：会改变后续字段的列位置。
- 让模型重排整个表格：版式变化大且容易引入括号错误。

## 当前方案

保留每个可见单元格（包括空单元格），仅修复可确定的行结束符和边界；列数不一致作为阻断错误处理，回退原始 TeX 或升级视觉识别。

## 验证

通过表格结构单元测试和 XeLaTeX 双遍编译后才发布。

## 2026-09-15：合法横线前误插空行，导致竖线断开

### 现场与根因

样例批次：`data/optimized/U3/20260808/A37Z201202604025`。
样例文件：`MX-C4081R_20260805_140745.tex`，约 2306 行出现以下结构：

```tex
...\tabularnewline
  \\
  \hline
```

`optimization/syntax_repair.py` 的 `normalize_hline_row_boundaries`
原来只检查物理换行前的两个字符是否为 `\\`，否则在 `\hline` 前补上
`\\`。它不认识合法的 `\tabularnewline`，也不区分表格开头的横线、注释、
带间距的行结束符和连续横线。因此在合法表格中插入只有一个空单元格的行，
该行缺少后续列的竖线，视觉上表现为断线。重复规范化会触发此问题。

旧运行镜像中已复现：以下原本合法的输入被错误修改两处，分别位于表格开头
与 `\tabularnewline` 之后。这证明本地修复器本身可以制造该问题；并不意味着
历史文件里的每一条空行都来自该修复器，不能据此批量删除所有空行。

```tex
\begin{tabular}{|l|l|}
\hline
A & B\tabularnewline
\hline
\end{tabular}
```

### 修复

- 按表格结构检查行边界，仅在确实有未结束行内容时补 `\tabularnewline`。
- 识别普通、带星号和带间距的行结束符；跳过注释、verbatim、分组和嵌套环境的干扰。
- 保留合法起始横线、连续横线以及有意留空的完整表格行。
- 本地规范化版本更新为 `local-tex-v7-structural-rule-boundaries`，使相关识别检查缓存失效。

### 验证与部署范围

新增 `pipeline/tests/unit/test_hline_boundaries.py`，共 11 项回归测试，
覆盖上述触发条件、嵌套表格、缺失结束符修复和重复规范化。
其中图像回归使用 XeLaTeX 编译并以 Poppler 渲染，比较处理前后像素，
验证合法表格经过两次处理后外观保持不变。

正式测试镜像验证命令（在 `PDFToTex` 目录运行）：

```bash
docker compose --profile test build tests
docker compose --profile test run --rm --no-deps tests python -m pytest -q --import-mode=importlib --rootdir=/tests/run /tests/pipeline/tests/unit
docker compose build worker
```

结果：355 项单元测试及 3 项子测试通过。`pdftotex:local` 已重新构建，
在新运行镜像中直接执行上述最小复现，两次处理均保持原文，修改计数为零。

早期通过挂载源码运行扩大测试时出现模型配置缺失和相对导入失败；
切换到项目正式构建的测试镜像后，对应 103 项针对性测试全部通过。
这类环境错误应与代码回归分开记录，不能以源码挂载测试代替正式测试环境。

此次未重启或恢复已有容器，未修改样例批次的历史 TeX/PDF。
新镜像不会自动修复已发布文件或已经存在的错误空行；历史产物需要另行核对并修复，
不能简单扩大线宽或无差别删掉空行。

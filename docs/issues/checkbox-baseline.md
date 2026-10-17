# 复选框与文字的垂直对齐

2026-10-17：记录中的选中框高于相邻文字，小字号下更明显。

## 根因与修复

识别模板和优化模板原先以当前字体的 `ex` 设置方框，但勾号固定使用
`\scriptsize`，框的高度又会随内部字形改变，且未将框中心锚定到当前字体的数学轴。
最小样例中，scriptsize 的选中框高度为 5.67474pt，空框为 4.62376pt，
问号框为 5.78052pt；三者深度均为 0.85252pt。

两处模板现统一使用固定方框，内部符号按当前字号缩放、独立居中，
以 `\vcenter` 对齐数学轴。保留单参数与旧三参数接口。
已有 TeX 可通过 `texopt.optimization.preamble.inject()` 更新宏；
仅更新镜像不会自动改写历史 TeX/PDF。

旧版 `patch_lexoid_checkbox_prompt.py` 是兼容旧依赖的独立工具，当前镜像
构建及运行链路没有调用它；本次修复覆盖当前实际使用的两处模板。

## 验证与部署

- 新增 `pipeline/tests/unit/test_checkbox_baseline.py`，实际运行 XeLaTeX，
  检查七种字号、表格单元格、三种状态及重复注入后的尺寸与数学轴。
  原镜像三种模板模式均失败，新镜像全部通过。
- 原有复选框测试 8 项通过，Lexoid 字段模板测试 7 项通过。
- 实际表面微生物记录更新宏后编译成功，仍为 8 页，无缺字报告。
- 样例及部署 Dockerfile 保存在工作区 `data/fix/checkbox-baseline-20261017/`，
  服务器对应 `/srv/pdftotex/data/fix/checkbox-baseline-20261017/`。
- 基于 `pdftotex:297ccd2ae56c-amd64` 构建仅替换两个模块的增量镜像
  `pdftotex:checkbox-baseline-20261017`，直接在新镜像中通过回归测试。
  服务器默认 `pdftotex:local` 已指向该镜像；显式指定旧标签的历史任务
  如需重跑，应改用新标签。未重启已完成的队列。

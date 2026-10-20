# 2026-10-20 架构改进与验证

## 范围与提交

1. `2254403`：修复接受、定向重试与回退归入 `RepairTransaction`，候选缓存仍需经过验收。
2. `f24eb66`：编译及版面验收归入 `compilation.py`，返回独立结果，最多三轮调整。
3. `0682e83`：后台增量读取证据、持久化进度快照，HTTP 详情只读投影。
4. 文档任务独立提交：新增 [CONTEXT](../../CONTEXT.md)、[术语表](../../GLOSSARY.md)、[12 项 ADR](../adr/README.md) 和全部可达提交索引，并修正旧设计中的冲突描述。

成功模型修复允许改值并记录审计，沿用原实现；失败批次降级继续仍要求结构与完整性检查。审计不能证明新值正确，详见 [ADR-0004](../adr/0004-model-values-and-audit.md)。

## 自动验证

- `python3 -m compileall -q Lexoid/lexoid pipeline/texopt web/backend`：通过。
- `PDFTOTEX_TEST_IMAGE=pdftotex-test:architecture-20261020 docker compose run --rm --no-deps tests`：流水线 522 项及 6 个子测试通过，Lexoid 166 项通过。
- Web 完整测试启用真实 Docker：39 项通过；随后增加的损坏索引参数变体随最终进度投影测试一并运行，11 项通过。
- 前端 `npm run build`：通过，存在已有的超过 500 kB 分块提示。
- 隔离环境 Playwright：5 项通过，含实际上传、编译、中文预览、文件夹列表、详情、配置和移动端。

覆盖的边界包括缓存候选回退、成功模型改值、失败批次保护、裁切重试耗尽、编译异常、不修改输入报告、多查看者共享快照、追加日志、截断与轮换、半条 JSON、去重、未知用量、损坏索引重建及无浏览器时后台更新。

完整 Docker 日志位于本次执行机 `/tmp/pdftotex-architecture-full-tests.log`；测试镜像为独立验证镜像。

## 真实容器产物回归

只读获取服务器 `pdftotex-u2-static-20261017` 的产物（原镜像 `pdftotex:297ccd2ae56c-amd64`）：

- TeX：`/srv/pdftotex/data/optimized/U2/批次/静态表面微生物测试记录.tex`
- PDF：`/srv/pdftotex/data/workers/静态表面微生物测试记录/.pipeline/静态表面微生物测试记录/静态表面微生物测试记录.optimized.layout.pdf`
- 识别证据：同一 worker 的 `tex/静态表面微生物测试记录.recognition.json`。

本地审计目录：`../data/fix/architecture-20261020/`，包含原始 TeX/PDF、识别证据、回归脚本、日志、JSON、重编译产物及第 1/9 页渲染图。

结构检查无错误，保留已有 underfull 行警告。新编译一次通过，前后均为 9 个输出页，`outside_pages` 为空。8 张源页产生 9 张输出页，严格一对一检查仍提示分页差异，允许额外页的交付验收通过。第 1、9 页渲染 PNG 字节相同，并已人工式视觉抽查。这只证明所抽页面的渲染一致，不能证明全文业务内容正确。

未覆盖用户后来手工修订的本地定稿；未重启生产容器、未发布到 Overleaf、未推送 Git。隔离 Web 联调进程已停止。

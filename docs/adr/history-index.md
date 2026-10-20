# Git 历史检索范围

整理日期：2026-10-20。范围为两仓库本地全部可达引用（`git log --all --reverse`），两者均非浅克隆。逐项检查提交主题和变更文件范围，对 ADR 引用的关键政策另行核对补丁及当前实现。已删除、不可达对象及旧流水线子模块未导入的内部历史不在此范围；不据此推断未记录的讨论。

任务开始时主仓库 `29647fd` 共 145 项；本轮三个实现提交后共 148 项。Lexoid `5bf1a4e` 共 36 项。本索引不包含其自身提交。

历史回退：`ff1689d` 的四容器小时统计由 `a7e02b4` 撤回；Overleaf 替换策略最终由 `08cf971` 的仅新增策略替代。语法修复允许改值的策略在 `51ae061` 导入时已存在，不能将 `d2bacbc` 的协调策略调整当作该策略的起点。

## 主仓库

共 148 项；HEAD：`0682e8373e093a962262a251bead29a1629e35e0`。

本地检索引用：
```text
bf5d988dd2a177a9f4e2820debeb77d5be2423d2 refs/heads/feat/linux-server-migration
0682e8373e093a962262a251bead29a1629e35e0 refs/heads/main
29647fddd2a8657bddd18e20be8bd8ab4c5083fc refs/remotes/origin/HEAD
10a6727e9942e5d529e14123d33bca4ddfd77873 refs/remotes/origin/feat/linux-server-migration
29647fddd2a8657bddd18e20be8bd8ab4c5083fc refs/remotes/origin/main
```

| 提交 | 日期 | 主题 | 变更范围 |
| --- | --- | --- | --- |
| `faaa52b` | 2026-09-09 | 初始化 PDFToTex 总项目并关联识别与流水线子模块 | .dockerignore, .gitignore, .gitmodules, Lexoid, README.md, REFACTOR_WORKSPACE.md, docker-compose.yml, docs/engine-data-tables.md, lexiod-pipeline |
| `c784450` | 2026-09-09 | 将队列和续跑脚本纳入主仓库并更新 Docker 挂载 | README.md, docker-compose.yml, scripts/resume-none, scripts/run-sequential-queue.py |
| `d47fc8a` | 2026-09-09 | 使用 data/fix 修复工作区并规范运行脚本挂载路径 | README.md, docker-compose.yml, scripts/run-sequential-queue.py |
| `dda1a0f` | 2026-09-09 | feat: support cross-batch PDF queues and document progress monitoring | README.md, lexiod-pipeline, scripts/run-sequential-queue.py, scripts/test_sequential_queue.py |
| `5414616` | 2026-09-10 | fix: keep monitoring pending batch queues | lexiod-pipeline |
| `28c2229` | 2026-09-10 | chore: update pipeline submodule | lexiod-pipeline |
| `67c1977` | 2026-09-11 | chore: update pipeline submodule | lexiod-pipeline |
| `9895fbb` | 2026-09-11 | chore: update pipeline compose defaults | lexiod-pipeline |
| `6503ef0` | 2026-09-11 | 清理仓库历史无关文件 | Lexoid, lexiod-pipeline |
| `639e711` | 2026-09-11 | 继续清理历史文档与试验文件 | Lexoid, REFACTOR_WORKSPACE.md, docs/engine-data-tables.md, lexiod-pipeline |
| `6a622f4` | 2026-09-11 | 同步清理后的链路文档 | README.md, lexiod-pipeline |
| `61a8e34` | 2026-09-11 | 更新流水线文档引用 | lexiod-pipeline |
| `4ab3a66` | 2026-09-11 | docs: 清理旧方案并明确当前链路 | Lexoid, README.md, agent.md, lexiod-pipeline |
| `41184a7` | 2026-09-11 | chore: 更新 Lexoid 清理提交 | Lexoid |
| `22691cf` | 2026-09-11 | chore: 更新示例清理提交 | Lexoid |
| `088f046` | 2026-09-11 | chore: 统一 Docker 项目与镜像命名 | README.md, docker-compose.yml |
| `786ad26` | 2026-09-11 | chore: 更新 Lexoid 依赖裁剪提交 | Lexoid |
| `fc9ce82` | 2026-09-11 | chore: 清理分类前缀命名 | README.md, docker-compose.yml |
| `d9c2ac8` | 2026-09-11 | chore: 更新 Lexoid 文档清理提交 | Lexoid |
| `5c8a625` | 2026-09-11 | chore: 更新清理后的 Lexoid 子仓库 | Lexoid |
| `99f229f` | 2026-09-11 | chore: 更新测试环境与忽略配置 | .gitignore, lexiod-pipeline |
| `d34c71d` | 2026-09-11 | chore: 增加 Poetry 锁文件自动生成环境 | README.md, scripts/update-lock.sh |
| `771bea4` | 2026-09-11 | docs: 补充常用开发与运行命令 | agent.md |
| `80a2322` | 2026-09-11 | chore: 更新 PDF 转 JSON 功能清理 | Lexoid, lexiod-pipeline |
| `d9d5a05` | 2026-09-11 | chore: 更新 Lexoid 目录清理 | Lexoid, lexiod-pipeline |
| `7da5a27` | 2026-09-11 | chore: 更新 Lexoid 核心目录清理 | Lexoid |
| `d905ac0` | 2026-09-11 | chore: 更新流水线修复提交 | lexiod-pipeline |
| `856f1ad` | 2026-09-11 | docs: 记录转译链路顽固问题和运行镜像约束 | Lexoid, agent.md, docker-compose.yml, docs/KNOWN_ISSUES.md, lexiod-pipeline |
| `d487eda` | 2026-09-11 | docs: 更新镜像构建说明 | Lexoid, README.md, lexiod-pipeline |
| `a64d691` | 2026-09-11 | docs: 记录流水线目录重构方案 | README.md, docs/ARCHITECTURE.md, docs/superpowers, lexiod-pipeline |
| `a2000b6` | 2026-09-11 | refactor: 更新总项目目录入口 | docker-compose.yml, docs/ARCHITECTURE.md, docs/superpowers, lexiod-pipeline |
| `50459ea` | 2026-09-11 | docs: 修正重构后的路径说明 | README.md, docs/superpowers |
| `ad10398` | 2026-09-11 | chore: 更新流水线子仓库引用 | lexiod-pipeline |
| `51ae061` | 2026-09-11 | refactor: 纳入流水线源码到总仓库 | .gitmodules, lexiod-pipeline, lexiod-pipeline/.gitignore, lexiod-pipeline/app.py, lexiod-pipeline/docker, lexiod-pipeline/tests, lexiod-pipeline/texopt |
| `46a9492` | 2026-09-11 | refactor: 删除重复流水线 Docker 入口 | agent.md, lexiod-pipeline/texopt |
| `95cd63c` | 2026-09-11 | fix: 修正流水线环境文件路径 | docker-compose.yml |
| `c235adb` | 2026-09-11 | refactor: 分离流水线单元测试目录 | lexiod-pipeline/docker, lexiod-pipeline/tests, lexiod-pipeline/texopt |
| `42cd3a8` | 2026-09-11 | refactor: 统一流水线顶层目录命名 | README.md, agent.md, docker-compose.yml, docs/ARCHITECTURE.md, docs/superpowers, lexiod-pipeline/.gitignore, lexiod-pipeline/app.py, lexiod-pipeline/docker, lexiod-pipeline/tests, lexiod-pipeline/texopt, pipeline/.gitignore, pipeline/app.py, pipeline/docker, pipeline/tests, pipeline/texopt |
| `7b246a1` | 2026-09-11 | fix: 修复目录迁移后的变量引用 | README.md, docs/ARCHITECTURE.md, pipeline/texopt |
| `0d540a6` | 2026-09-11 | refactor: 清理流水线目录残留文件 | docs/ARCHITECTURE.md, docs/superpowers |
| `a136ba7` | 2026-09-11 | fix: 修复 PDF 扫描列表变量引用 | pipeline/texopt |
| `795774a` | 2026-09-11 | refactor: 整理监控模块目录 | README.md, agent.md, pipeline/tests, pipeline/texopt |
| `9c6bd9a` | 2026-09-11 | refactor: 整理识别模块目录 | pipeline/app.py, pipeline/tests, pipeline/texopt |
| `1367a3e` | 2026-09-11 | refactor: 按职责拆分识别和优化模块 | pipeline/app.py, pipeline/tests, pipeline/texopt |
| `c8b170f` | 2026-09-11 | docs: 更新识别优化模块命令路径 | pipeline/texopt |
| `ee58eda` | 2026-09-11 | refactor: 归类流水线基础与运行模块 | pipeline/tests, pipeline/texopt |
| `d4fe513` | 2026-09-11 | fix: 同步监控脚本目录和定时任务 | pipeline/texopt |
| `9d35d9e` | 2026-09-12 | fix: 修复重构后测试兼容与流水线依赖 | README.md, agent.md, pipeline/docker, pipeline/tests, pipeline/texopt |
| `bc372d9` | 2026-09-12 | fix: 补齐测试镜像重构模块依赖 | pipeline/docker |
| `d0e67f7` | 2026-09-12 | fix: 完善测试包路径映射 | pipeline/docker |
| `3d0859d` | 2026-09-12 | fix: 在最终输出阶段统一归一化 TeX | pipeline/texopt |
| `0a23cc7` | 2026-09-12 | fix: 自动修复统一多一列表格规格 | pipeline/texopt |
| `634e071` | 2026-09-12 | fix: 在语法修复后校正表格列规格 | pipeline/texopt |
| `4d8ccfa` | 2026-09-12 | fix: 修复页面边界未闭合结构 | pipeline/texopt |
| `c0771f2` | 2026-09-12 | fix: 处理文本模式数学符号编译错误 | pipeline/tests, pipeline/texopt |
| `8858b5a` | 2026-09-12 | fix: 限制数学符号修复仅作用于文本模式 | pipeline/tests, pipeline/texopt |
| `c293b77` | 2026-09-12 | fix: 修复紧邻数字的数学符号转换 | pipeline/tests, pipeline/texopt |
| `fe20c52` | 2026-09-12 | fix: 转义文本模式上标符号 | pipeline/texopt |
| `a641663` | 2026-09-12 | fix: 转义中文前的异常反斜杠 | pipeline/tests, pipeline/texopt |
| `d1cb282` | 2026-09-12 | fix: 处理文本模式温度符号 | pipeline/texopt |
| `2b52e24` | 2026-09-12 | fix: 修复独立换行命令编译错误 | pipeline/texopt |
| `98b737a` | 2026-09-12 | fix: 扩展独立换行命令匹配范围 | pipeline/texopt |
| `d9422ab` | 2026-09-12 | fix: 在写盘前完成最终 TeX 规范化 | pipeline/texopt |
| `6aa1f0c` | 2026-09-12 | fix: 移除过度补括号的页面修复 | pipeline/texopt |
| `d84cafa` | 2026-09-12 | fix: 稳健修复宽型仪器表列规格 | pipeline/tests, pipeline/texopt |
| `26d96f3` | 2026-09-12 | fix: 修复视觉输出未闭合的表单包装 | pipeline/tests, pipeline/texopt |
| `99bcf46` | 2026-09-12 | fix: 补齐字段行包装的安全闭合 | pipeline/tests, pipeline/texopt |
| `c656d4d` | 2026-09-12 | fix: 修复重复列规格缺失闭合括号 | pipeline/tests, pipeline/texopt |
| `0213c16` | 2026-09-12 | fix: 保留手写字段中的行内数学片段 | pipeline/tests, pipeline/texopt |
| `2296052` | 2026-09-12 | fix: 同步重构入口并保留手写数学片段 | .dockerignore, pipeline/docker, pipeline/tests, pipeline/texopt, scripts/resume-none, scripts/run-sequential-queue.py, scripts/test_sequential_queue.py |
| `d108ee9` | 2026-09-12 | fix: 统一 TeX 数学值与识别证据比较 | Lexoid, pipeline/tests, pipeline/texopt |
| `3d8c7ae` | 2026-09-12 | fix: 兼容视觉字段中的换行转义值 | pipeline/tests, pipeline/texopt |
| `d2bacbc` | 2026-09-12 | fix: 放宽识别值差异并保持结构校验 | pipeline/tests, pipeline/texopt |
| `653b104` | 2026-09-12 | fix: 稳定修复复杂表格与数学片段编译 | pipeline/tests, pipeline/texopt |
| `638b878` | 2026-09-12 | fix: 保留失败升级页的原始 TeX | docs/ARCHITECTURE.md, docs/superpowers, pipeline/texopt |
| `0a9c47b` | 2026-09-12 | docs: 修正监控命令路径说明 | pipeline/texopt |
| `c0e6bc3` | 2026-09-12 | fix: 转义手写字段中的文字反斜杠 | pipeline/tests, pipeline/texopt |
| `d842e0a` | 2026-09-14 | fix: 修复数学模式边界与队列协调 | Lexoid, docs/KNOWN_ISSUES.md, docs/issues, pipeline/tests, pipeline/texopt |
| `282435a` | 2026-09-14 | fix: 转义正文井号并保护支持宏参数 | pipeline/tests, pipeline/texopt |
| `643f9a1` | 2026-09-14 | docs: 记录表格井号编译问题 | docs/KNOWN_ISSUES.md, docs/issues |
| `19d281d` | 2026-09-14 | fix: 修复跨页残留数学模式导致的公式编译错误 | pipeline/tests, pipeline/texopt |
| `e0c72c2` | 2026-09-14 | fix: 收紧跨页数学模式修复范围 | docs/issues, pipeline/tests, pipeline/texopt |
| `87cfb1f` | 2026-09-15 | fix: 支持人工确认批次完成状态 | pipeline/tests, pipeline/texopt |
| `6f43559` | 2026-09-15 | feat: add reusable batch Excel exporters with precise date formatting | scripts/batch_excel, scripts/resume-none, scripts/run-sequential-queue.py, scripts/test_sequential_queue.py, scripts/update-lock.sh |
| `afb0eb9` | 2026-09-16 | fix: 修复日报扫描数据库连接未关闭的问题 | docs/KNOWN_ISSUES.md, docs/issues, pipeline/tests, pipeline/texopt |
| `ccfc805` | 2026-09-16 | fix: 修复表格横线前误插空行导致的竖线断裂 | docs/issues, pipeline/tests, pipeline/texopt |
| `68fcc02` | 2026-09-16 | feat: 增加批次导出中文标签和步骤总览 | scripts/batch_excel |
| `a4c19e1` | 2026-09-16 | fix: group full-page field reviews and cap repeated calls | README.md, docs/KNOWN_ISSUES.md, docs/issues, pipeline/tests, pipeline/texopt, scripts/smoke-reconcile-page.py |
| `b205a02` | 2026-09-17 | docs：完整流水线设计文档 | docs/PIPELINE_DESIGN.md |
| `bb94df3` | 2026-09-17 | chore: 更新页首方向保护子模块 | Lexoid |
| `11ec6ac` | 2026-09-17 | feat: 日报增加按页调差费用 | pipeline/tests, pipeline/texopt |
| `454fb50` | 2026-09-23 | fix: 延长识别和阶段默认超时 | README.md, pipeline/tests, pipeline/texopt |
| `ff1689d` | 2026-09-23 | feat: 增加四容器小时吞吐费用统计 | scripts/hourly-throughput.py |
| `f6e6ecb` | 2026-09-23 | docs: 补充批次启动监控要求 | agent.md |
| `a7e02b4` | 2026-09-23 | revert: 删除四容器小时统计代码 | scripts/hourly-throughput.py |
| `f46e761` | 2026-09-23 | docs: 增加Linux服务器迁移设计与实施方案 | docs/superpowers |
| `4d4a83b` | 2026-09-23 | docs: 调整服务器迁移数据范围 | docs/superpowers |
| `379740a` | 2026-09-23 | build: 增加可复现的AMD64运行时镜像 | .dockerignore, docker-compose.yml, pipeline/docker, pipeline/tests, scripts/verify-runtime-image.sh |
| `e4be063` | 2026-09-23 | build: 统一运行时和工作镜像构建流程 | README.md, scripts/build-images.sh, scripts/image-manifest.sh |
| `ad7d2e4` | 2026-09-23 | feat: 增加Linux定时监控服务 | deploy/systemd, pipeline/docker, pipeline/tests, scripts/install-systemd-monitoring.sh |
| `ac269ea` | 2026-09-23 | feat: 增加Overleaf批次打包工具 | README.md, pipeline/docker, pipeline/tests, scripts/package-overleaf-batch.py |
| `c444d69` | 2026-09-23 | chore: 统一AGENT文档文件名 | AGENT.md, agent.md |
| `011f3fe` | 2026-09-23 | build: 支持配置服务器依赖镜像源 | README.md, docker-compose.yml, pipeline/docker |
| `ebbb417` | 2026-09-23 | fix: 同步运行时依赖修复 | Lexoid |
| `4396956` | 2026-09-23 | build: 缓存服务器依赖下载 | pipeline/docker |
| `9b2b07d` | 2026-09-23 | fix: 同步Python兼容依赖 | Lexoid |
| `e989779` | 2026-09-23 | fix: 同步模型提供器隔离修复 | Lexoid |
| `c6d7b75` | 2026-09-23 | refactor: 支持配置监控部署根目录 | README.md, deploy/systemd, pipeline/tests, scripts/install-systemd-monitoring.sh |
| `c9e728b` | 2026-09-23 | fix: 修复Linux监控状态库权限 | README.md, pipeline/tests, scripts/install-systemd-monitoring.sh |
| `4bf95aa` | 2026-09-23 | feat: 增加Overleaf批次自动发布 | AGENT.md, README.md, deploy/systemd, docs/superpowers, pipeline/tests, scripts/install-overleaf-publisher.sh, scripts/install-systemd-monitoring.sh, scripts/overleaf_publish.py, scripts/package-overleaf-batch.py, scripts/publish_completed_overleaf.py |
| `d9bd956` | 2026-09-23 | style: 清理systemd文件格式 | deploy/systemd |
| `4e0998e` | 2026-09-24 | feat: 增加容器异常邮件告警 | README.md, deploy/systemd, docs/PIPELINE_DESIGN.md, pipeline/tests, pipeline/texopt, scripts/install-systemd-monitoring.sh |
| `9b28524` | 2026-09-25 | feat: 增加服务器队列接续与监控同步 | AGENT.md, README.md, deploy/systemd, pipeline/tests, scripts/install-queue-dispatcher.sh, scripts/install-server-monitor-sync.sh, scripts/queue_dispatch.py, scripts/sync-server-monitor.sh |
| `c5f53da` | 2026-09-25 | fix: 修复队列接续容器缺失判断 | pipeline/tests, scripts/queue_dispatch.py |
| `49af35b` | 2026-09-25 | fix: 修复跨行字段注释和空白页编译 | pipeline/tests, pipeline/texopt |
| `27804cb` | 2026-09-26 | feat: 增加服务器日报分界同步 | AGENT.md, README.md, pipeline/tests, pipeline/texopt, scripts/install-server-daily-sync.sh, scripts/sync-server-daily.sh |
| `7bd6c0d` | 2026-09-26 | feat: API异常改为六档指数重启 | AGENT.md, README.md, pipeline/tests, pipeline/texopt |
| `3109248` | 2026-09-28 | fix: 修复已发布任务的队列完成判定 | pipeline/tests, pipeline/texopt, scripts/queue_dispatch.py |
| `de6c250` | 2026-09-28 | fix: 补全表格宏所需的TeX依赖 | pipeline/tests, pipeline/texopt |
| `df32637` | 2026-09-28 | fix: 完善监控完成判定和邮件投递校验 | pipeline/tests, pipeline/texopt |
| `e9ce8b7` | 2026-09-28 | fix: 修复多人编辑时的Overleaf批次发布 | pipeline/tests, scripts/overleaf_publish.py, scripts/publish_completed_overleaf.py |
| `881f288` | 2026-09-29 | fix: 明确表格单元格换行规则 | docs/issues, pipeline/texopt |
| `be2fa38` | 2026-09-29 | chore: 更新 Lexoid 规则提示词子模块 | Lexoid |
| `e62fcb9` | 2026-09-30 | feat: 切换 gpt-6-sol 并保护 Overleaf 已有批次 | Lexoid, README.md, docs/PIPELINE_DESIGN.md, pipeline/tests, pipeline/texopt, scripts/overleaf_publish.py, scripts/run-sequential-queue.py |
| `08cf971` | 2026-09-30 | fix: Overleaf 仅新增文件并支持上传失败重试 | README.md, pipeline/tests, scripts/overleaf_publish.py |
| `a2c393d` | 2026-09-30 | fix: 统一两个 Overleaf 项目的待审核发布目录 | AGENT.md, README.md, docs/superpowers, pipeline/tests, scripts/install-overleaf-publisher.sh |
| `260910b` | 2026-09-30 | refactor: 统一从配置文件读取运行参数和发布目录 | README.md, deploy/overleaf.example.json, docker-compose.yml, pipeline/tests, scripts/install-overleaf-publisher.sh, scripts/overleaf_publish.py, scripts/queue_dispatch.py, scripts/run-sequential-queue.py |
| `8847bcc` | 2026-10-01 | fix: preserve heading braces and scientific notation in generated TeX | pipeline/tests, pipeline/texopt |
| `945a23a` | 2026-10-01 | fix: keep margin annotations inside page bounds and reject clipped exports | pipeline/tests, pipeline/texopt |
| `57dce52` | 2026-10-01 | fix: preserve verbatim records and start signatures after panels on new paragraphs | pipeline/tests, pipeline/texopt |
| `1d658fb` | 2026-10-02 | fix(layout): wrap field URLs and separate signatures after font switches | pipeline/tests, pipeline/texopt |
| `6fd01e3` | 2026-10-02 | fix(layout): bound outer resizebox height to prevent clipped tables | pipeline/tests, pipeline/texopt |
| `5e4995e` | 2026-10-02 | fix(tex): exclude preamble definitions from body math repair | pipeline/tests, pipeline/texopt |
| `0e41217` | 2026-10-02 | fix(workers): separate form paragraphs and notify every restart attempt | README.md, pipeline/tests, pipeline/texopt |
| `d8ae582` | 2026-10-03 | fix(layout): bound nested panels and remove negative line indents | pipeline/tests, pipeline/texopt |
| `10a6727` | 2026-10-03 | fix: unify layout containment and retry all overflowing pages | Lexoid, docs/KNOWN_ISSUES.md, docs/PIPELINE_DESIGN.md, docs/issues, pipeline/tests, pipeline/texopt, scripts/replay-layout.py |
| `af96b48` | 2026-10-08 | fix: prevent layout clipping and bound daily ledger transfers | docs/KNOWN_ISSUES.md, docs/PIPELINE_DESIGN.md, docs/issues, pipeline/tests, pipeline/texopt, scripts/sync-server-daily.sh |
| `ea96c83` | 2026-10-08 | fix: render scientific symbols and microliter units with TeX commands | docs/issues, pipeline/tests, pipeline/texopt |
| `bf5d988` | 2026-10-08 | feat: add native macOS and Linux build and monitoring commands | README.md, deploy/monitoring.example.json, deploy/platforms, docker-compose.yml, pipeline/tests, scripts/build-images.sh, scripts/image-manifest.sh, scripts/monitor.py, scripts/verify-runtime-image.sh |
| `8001674` | 2026-10-08 | docs: 更新跨平台监控协作与服务器观察模式说明 | AGENT.md, README.md |
| `4bbc09f` | 2026-10-09 | feat: add isolated PDF-to-TeX web workbench | .gitignore, README.md, deploy/web.example.json, docs/WEB_DESIGN.md, scripts/web.py, web/README.md, web/__init__.py, web/backend, web/frontend, web/requirements.txt, web/tests |
| `0f40b7a` | 2026-10-09 | fix: preserve deskew geometry across recognition and field crops | Lexoid, docs/issues, pipeline/tests, pipeline/texopt |
| `e454a97` | 2026-10-09 | fix：PDF预览缺失中文 | web/backend, web/frontend |
| `1b3f6dc` | 2026-10-17 | fix: 统一复选框垂直对齐并补充排版回归测试 | Lexoid, docs/KNOWN_ISSUES.md, docs/issues, pipeline/tests, pipeline/texopt |
| `29647fd` | 2026-10-20 | refactor: 简化工作台下载与重新编译逻辑 | web/backend |
| `2254403` | 2026-10-20 | refactor: 集中模型修复的接受重试与回退政策 | pipeline/tests, pipeline/texopt |
| `f24eb66` | 2026-10-20 | refactor: 封装版面编译验收与有限重试 | pipeline/tests, pipeline/texopt |
| `0682e83` | 2026-10-20 | perf: 后台增量汇总进度并让网页读取持久快照 | web/README.md, web/backend, web/frontend, web/tests |

## Lexoid

共 36 项；HEAD：`5bf1a4efbe322390256feb1301c3d3d57827899a`。

本地检索引用：
```text
cc806f3cc5463f1123858f135db07325ef16a166 refs/heads/feat/linux-server-migration
5bf1a4efbe322390256feb1301c3d3d57827899a refs/heads/main
5bf1a4efbe322390256feb1301c3d3d57827899a refs/remotes/origin/HEAD
cc806f3cc5463f1123858f135db07325ef16a166 refs/remotes/origin/feat/linux-server-migration
5bf1a4efbe322390256feb1301c3d3d57827899a refs/remotes/origin/main
```

| 提交 | 日期 | 主题 | 变更范围 |
| --- | --- | --- | --- |
| `3ad2425` | 2026-09-05 | init | .agents/skills, .dockerignore, .env_example, .gitignore, CHANGELOG.md, CODE_OF_CONDUCT.md, DOCKER.md, Dockerfile, FORMKIT.md, LICENSE, Makefile, README.md, TEXOPT_REQUIREMENTS.md, assets/logo.png, docker-compose.yml, docs/.nojekyll, docs/Makefile, docs/api.rst, docs/benchmark.rst, docs/cli.rst, docs/conf.py, docs/contributing.rst, docs/index.rst, docs/installation.rst, docs/make.bat, docs/requirements.txt, docs/research, docs/superpowers, examples/example_notebook.ipynb, examples/example_notebook_colab.ipynb, examples/example_notebook_latex.ipynb, examples/example_notebook_reference_highlight.ipynb, examples/example_notebook_schema.ipynb, formkit/__init__.py, formkit/__main__.py, formkit/extract.py, formkit/llm.py, formkit/merge.py, formkit/prompts.py, formkit/render.py, formkit/review.py, formkit/schema.py, lexoid.sh, lexoid/__main__.py, lexoid/api.py, lexoid/cli.py, lexoid/core, poetry.lock, prompts/latex_synctex_optimization_prompt.md, pyproject.toml, requirements-formkit.txt, skills/lexoid-cli, skills/lexoid-python, tests/api_cost_mapping.json, tests/benchmark.py, tests/benchmark_utils.py, tests/env_template, tests/plot_document_results.py, tests/recreate_results_csv.py, tests/results.csv, tests/test_cli.py, tests/test_conversion_utils.py, tests/test_formkit.py, tests/test_latex_page_writer.py, tests/test_latex_template.py, tests/test_latex_value_ids.py, tests/test_model_providers.py, tests/test_parser.py, tests/test_recognition_cache.py, tests/test_recognition_models.py, tests/test_recognition_rendering.py, tests/test_web_parsing.py, tests/update_benchmarks.py |
| `8141695` | 2026-09-06 | feat: add hybrid PDF recognition workflow | .env_example, docs/api.rst, docs/cli.rst, docs/superpowers, formkit/__main__.py, formkit/extract.py, formkit/llm.py, lexoid.sh, lexoid/api.py, lexoid/cli.py, lexoid/core, pyproject.toml, scripts/audit_u2_outputs.py, scripts/benchmark_text.py, scripts/prefetch_models.py, scripts/prepare_comparison.py, scripts/prepare_u2_queue.py, scripts/render_comparison.py, scripts/run_comparison.py, scripts/score_comparison.py, scripts/validate_hybrid.py, scripts/validate_pagination_samples.py, tests/conftest.py, tests/fixtures, tests/test_cli.py, tests/test_hybrid_latex_api.py, tests/test_latex_value_ids.py, tests/test_lexoid_environment_models.py, tests/test_model_providers.py, tests/test_model_telemetry.py, tests/test_paddle_adapters.py, tests/test_paddle_runtime.py, tests/test_recognition_service.py, tests/test_recognition_vision.py |
| `80155fe` | 2026-09-07 | fix: retry transient vision connection errors | lexoid/core, tests/test_recognition_service.py |
| `3e2c30f` | 2026-09-07 | fix: avoid retries for recoverable field metadata errors | lexoid/core, tests/test_recognition_service.py |
| `910efe7` | 2026-09-07 | 修复：表格行坐标冲突仅记录警告并保留字段 | lexoid/core, tests/test_recognition_service.py, tests/test_recognition_vision.py |
| `0a4c034` | 2026-09-07 | 优化：流水线处理逐页检查并共享模型并发额度 | lexoid/api.py, lexoid/core, tests/test_recognition_cache.py, tests/test_recognition_service.py |
| `4afa7a6` | 2026-09-07 | 修复：约束跨行单元格继承所在列宽 | lexoid/core, tests/test_latex_value_ids.py |
| `02c0bc2` | 2026-09-07 | 优化：Sol 视觉识别默认关闭推理并记录参数与缓存模式 | lexoid/api.py, lexoid/core, tests/test_hybrid_latex_api.py, tests/test_model_providers.py, tests/test_recognition_cache.py, tests/test_recognition_vision.py |
| `90adfdb` | 2026-09-08 | 修复模型连接失败暂停与识别缓存恢复 | lexoid/core, tests/test_recognition_service.py |
| `bc08cc6` | 2026-09-09 | 强化表格独立列与跨行合并单元格识别约束 | lexoid/core |
| `ebeebec` | 2026-09-09 | 省略实验照片和曲线图重绘并保留图外业务数据 | lexoid/core |
| `680c89d` | 2026-09-09 | 保留实验数据图空框并更新视觉识别提示词 | lexoid/core |
| `b338715` | 2026-09-11 | 清理 Lexoid 历史试验与样例 | docs/research, docs/superpowers, examples/example_notebook.ipynb, examples/example_notebook_colab.ipynb, examples/example_notebook_latex.ipynb, examples/example_notebook_reference_highlight.ipynb, examples/example_notebook_schema.ipynb, scripts/benchmark_text.py, scripts/prepare_comparison.py, scripts/render_comparison.py, scripts/run_comparison.py, scripts/score_comparison.py, scripts/validate_pagination_samples.py, tests/plot_document_results.py, tests/recreate_results_csv.py, tests/results.csv, tests/update_benchmarks.py |
| `3cced4b` | 2026-09-11 | docs: 删除旧 GPU 与 OCR 方案说明 | DOCKER.md, FORMKIT.md, README.md, skills/lexoid-cli, skills/lexoid-python |
| `c2fc7d4` | 2026-09-11 | docs: 删除旧版本变更记录 | CHANGELOG.md |
| `f7e6a77` | 2026-09-11 | docs: 移除失效示例链接 | README.md |
| `2e7e432` | 2026-09-11 | refactor: 移除未使用的模型依赖 | pyproject.toml |
| `747a7f0` | 2026-09-11 | docs: 移除无关模型平台标识 | README.md |
| `712486f` | 2026-09-11 | refactor: 清理旧样例与无用脚本 | lexoid.sh, poetry.lock, tests/test_recognition_service.py |
| `7ad8060` | 2026-09-11 | refactor: 删除旧文档与无关测试 | docs/.nojekyll, docs/Makefile, docs/api.rst, docs/benchmark.rst, docs/cli.rst, docs/conf.py, docs/contributing.rst, docs/index.rst, docs/installation.rst, docs/make.bat, docs/requirements.txt, tests/api_cost_mapping.json, tests/benchmark.py, tests/benchmark_utils.py, tests/test_cli.py, tests/test_parser.py, tests/test_web_parsing.py |
| `b845f6e` | 2026-09-11 | refactor: 删除 FormKit PDF 转 JSON 功能 | formkit/__init__.py, formkit/__main__.py, formkit/extract.py, formkit/llm.py, formkit/merge.py, formkit/prompts.py, formkit/render.py, formkit/review.py, formkit/schema.py, pyproject.toml, requirements-formkit.txt, tests/test_formkit.py, tests/test_lexoid_environment_models.py |
| `0cffff4` | 2026-09-11 | refactor: 删除未使用的 Lexoid 脚本目录 | scripts/audit_u2_outputs.py, scripts/prefetch_models.py, scripts/prepare_u2_queue.py, scripts/validate_hybrid.py |
| `d246807` | 2026-09-11 | refactor: 移除旧自动选模型目录 | lexoid/api.py, lexoid/core |
| `c5f430a` | 2026-09-11 | fix: 修复视觉识别结果中的 TeX 反斜杠解析 | lexoid/core, tests/test_recognition_vision.py |
| `38696ce` | 2026-09-11 | chore: 统一独立镜像命名 | docker-compose.yml |
| `cc0b050` | 2026-09-12 | fix: 补齐 Lexoid 包入口以稳定测试导入 | lexoid/__init__.py, lexoid/core |
| `022440a` | 2026-09-14 | fix: 收敛视觉识别配置与服务处理 | TEXOPT_REQUIREMENTS.md, lexoid/core |
| `1139dab` | 2026-09-17 | fix: 增加页首方向复核保护表格方向 | lexoid/core, tests/test_recognition_rendering.py |
| `73e4442` | 2026-09-23 | fix: 补充绘图运行时依赖 | poetry.lock, pyproject.toml |
| `a2d2415` | 2026-09-23 | fix: 固定Python兼容解析依赖 | poetry.lock, pyproject.toml |
| `a8742c0` | 2026-09-23 | fix: 延迟加载可选模型提供器 | lexoid/core |
| `5b14b33` | 2026-09-29 | feat: 约束表格单元格换行边界 | lexoid/core, prompts/latex_synctex_optimization_prompt.md, tests/test_recognition_vision.py |
| `49343b4` | 2026-09-30 | fix: 支持 gpt-6-sol 视觉推理配置 | lexoid/api.py, tests/test_hybrid_latex_api.py |
| `cc806f3` | 2026-10-03 | fix: constrain recognized layouts to their containing columns | lexoid/core |
| `3ba2ed1` | 2026-10-09 | fix: deskew ruled tables before visual recognition | lexoid/core, tests/test_recognition_models.py, tests/test_recognition_rendering.py, tests/test_recognition_service.py |
| `5bf1a4e` | 2026-10-17 | fix: 修复复选框尺寸与文字垂直对齐 | lexoid/core |

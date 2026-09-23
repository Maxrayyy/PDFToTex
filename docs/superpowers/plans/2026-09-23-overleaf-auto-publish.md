# Overleaf 自动发布实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将服务器上新完成的 U1、U3 批次目录可靠地自动发布到两个既有 Overleaf 项目。

**Architecture:** 发布器负责单个批次的确定性目录同步、Git 提交和推送；扫描器只负责从完成队列中发现待发布批次。服务器 JSON 配置保存目录和 remote 映射，受限凭据文件保存 token，systemd timer 每两分钟驱动扫描器。

**Tech Stack:** Python 3 标准库、Git、systemd、pytest

**Spec:** `docs/superpowers/specs/2026-09-23-overleaf-auto-publish-design.md`

## Global Constraints

- U1 发布到项目的 `待审核/<批次号>`；U2 已完成，不启用自动发布。
- U3 发布到项目的 `U3_tex—待审核/20260808/<批次号>`。
- 只替换当前批次目录，不能删除或覆盖项目内其他目录。
- token 不进入 Git、日志、命令参数或 remote URL。
- 首次启用不得自动上传历史批次。
- 推送成功后才记录成功账本。

---

### Task 1: 单批次发布器

**Files:**
- Create: `scripts/overleaf_publish.py`
- Create: `pipeline/tests/test_overleaf_publish.py`

**Interfaces:**
- Consumes: JSON 项目配置、分类、批次目录和 Git 可执行文件。
- Produces: `publish_batch(config_path, unit, batch_dir, queue_status=None)` 和命令行入口。

- [x] 编写失败测试，覆盖目标目录映射、排除规则、旧批次文件清理、其他目录保护、无变化跳过和 push 失败不记账。
- [x] 运行测试并确认由于发布器不存在而失败。
- [x] 实现配置校验、目录哈希、文件同步、项目锁、Git 拉取/提交/推送和 JSONL 账本。
- [x] 运行发布器测试并确认通过。
- [x] 纳入本功能统一提交。

### Task 2: 完成队列扫描器

**Files:**
- Create: `scripts/publish-completed-overleaf.py`
- Modify: `pipeline/tests/test_overleaf_publish.py`

**Interfaces:**
- Consumes: `data/workers/queues/*.status.json`、`enabled_after` 和 Task 1 发布器。
- Produces: 一次扫描中需要发布的 `(unit, batch_dir, queue_status)` 列表。

- [x] 编写失败测试，覆盖启用时间、整批成功门槛、分类识别、多批次去重和失败状态忽略。
- [x] 运行测试并确认因扫描接口不存在而失败。
- [x] 实现只读状态扫描和逐批调用。
- [x] 运行测试并确认通过。
- [x] 纳入本功能统一提交。

### Task 3: Linux 安装与调度

**Files:**
- Create: `deploy/systemd/pdftotex-overleaf-publish.service`
- Create: `deploy/systemd/pdftotex-overleaf-publish.timer`
- Create: `scripts/install-overleaf-publisher.sh`
- Modify: `pipeline/tests/test_systemd_units.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: 部署根目录、U1/U3 两个 Overleaf remote 和通过标准输入提供的 token。
- Produces: `/srv/pdftotex/overleaf/config.json`、Git 工作树、权限 `0600` 的凭据及启用的 timer。

- [x] 编写失败测试，验证 unit 运行用户、两分钟间隔、配置占位符和安装脚本不含 token。
- [x] 运行测试并确认失败。
- [x] 实现 systemd 文件及幂等安装脚本。
- [x] 运行测试并确认通过。
- [x] 纳入本功能统一提交。

### Task 4: 文档、服务器安装和真实验证

**Files:**
- Modify: `README.md`
- Modify: `AGENT.md`

**Interfaces:**
- Consumes: 前三项完成的软件和用户提供的既有 Overleaf 项目。
- Produces: 可操作说明、服务器安装结果和一次不破坏远端内容的真实推送验证。

- [x] 更新配置、手工补传、状态查询、日志和 token 轮换说明。
- [x] 运行发布器测试、systemd 测试、`git diff --check` 和仓库测试。
- [x] 将代码同步到服务器并以受限文件安装 token。
- [x] 克隆 U1、U3 项目，核对目标父目录，不发布历史批次。
- [ ] 用一个明确指定的批次执行真实同步，检查远端 commit 和目标目录。当前没有指定补传批次，留待首个新完成队列触发，避免污染 Overleaf 历史。
- [x] 检查 timer、日志、账本和凭据权限。
- [x] 纳入本功能统一提交。

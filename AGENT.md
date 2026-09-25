# 仓库协作约定

转译链路中的已知顽固问题和处理规则见 [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md)，修改识别、回退或表格逻辑前先核对。

- 提交前检查对应仓库的 `git status` 和 `git diff`。
- 一次提交只包含当前任务相关文件。
- 提交信息使用 `<type>: <中文简述>`，不添加 AI 或代理署名。
- 未经明确要求，只创建本地提交，不执行 `push`、强制推送或历史改写。

## 常用命令

修改 Lexoid 依赖后刷新锁文件：

```bash
./scripts/update-lock.sh
```

检查 Poetry 配置：

```bash
.venv-poetry/bin/poetry check -C Lexoid
```

构建运行镜像：

```bash
docker compose build worker
```

构建并运行测试：

```bash
docker compose --profile test build tests
docker compose --profile test run --rm tests
```

查看容器：

```bash
docker compose ps
docker ps -a --filter 'name=pdftotex'
```

单次运行 PDF 队列：

```bash
docker compose run --rm worker
```

## 启动批次转译

每次启动或重启批次转译容器后，必须同时启动实时监控。监控会在所有任务结束后自动卸载，不得假定上一批次的监控仍在运行。

```bash
MONITOR_CONFIG="$(pwd)/../data/monitoring/realtime/config.json"
MONITOR_SERVICE="gui/$(id -u)/com.lexiod.worker-watch.realtime"

if launchctl print "$MONITOR_SERVICE" >/dev/null 2>&1; then
  launchctl kickstart -k "$MONITOR_SERVICE"
else
  python3 pipeline/texopt/monitoring/worker_watch.py install \
    --config "$MONITOR_CONFIG"
fi
```

批次启动只有在以下条件全部满足后才算完成：

- `docker inspect` 显示批次容器处于 `running`。
- `launchctl print "$MONITOR_SERVICE"` 能找到实时监控服务。
- `../data/monitoring/realtime/latest.md` 已刷新，并包含当前容器名。

如果首次监控结果显示容器已退出，应继续检查退出原因和自动重启状态，不得只报告“监控已启动”。

单次监控检查：

```bash
python3 pipeline/texopt/monitoring/worker_watch.py once \\
  --config "$(pwd)/../data/monitoring/realtime/config.json"
```

查看监控摘要：

```bash
cat ../data/monitoring/realtime/latest.md
```

## Overleaf 发布

Ubuntu 服务器在成功完成整批队列后，由 `pdftotex-overleaf-publish.timer` 每两分钟自动检查并发布。当前只启用 U1 和 U3：U1 放入项目的 `待审核/<批次号>`，U3 放入 `U3_tex—待审核/20260808/<批次号>`；U2 已完成，不注册到自动发布配置。启动服务器批次时必须同时确认自动发布 timer 和两个监控 timer 均为 `active`：

```bash
systemctl is-active \
  pdftotex-queue-dispatch.timer \
  pdftotex-overleaf-publish.timer \
  pdftotex-realtime-monitor.timer \
  pdftotex-daily-stats.timer
```

服务器连续执行多个批次时，`data/operations/queue-dispatch.json` 只列仍需处理的 U1/U3
清单，并按既定顺序排列。`pdftotex-queue-dispatch.timer` 每两分钟为每个单元补充下一批；
状态不完整或异常退出时必须停在当前批次，不得跳过失败清单。

手工补传一个批次：

```bash
sudo -u pdftotex env HOME=/srv/pdftotex/overleaf/home \
  python3 /srv/pdftotex/PDFToTex/scripts/overleaf_publish.py \
  --config /srv/pdftotex/overleaf/config.json \
  --unit U1 \
  --batch-dir /srv/pdftotex/data/optimized/U1/批次数据/<批次号>
```

不得把 Overleaf token 写入仓库、命令参数、remote URL 或日志。token 只允许保存在服务器 `/srv/pdftotex/overleaf/home/.git-credentials`，权限必须为 `0600`。发布异常时查看：

```bash
journalctl -u pdftotex-overleaf-publish.service -n 100 --no-pager
tail -n 20 /srv/pdftotex/overleaf/publish-ledger.jsonl
```

## 每次优化后的必做验证

任何识别、提示词、表格或 TeX 优化修改，都必须在报告中记录实际结果后才能完成：

```bash
python3 -m compileall -q Lexoid/lexoid pipeline/texopt
docker compose run --rm --no-deps tests
```

对实际生成的 TeX，还必须执行结构检查和 XeLaTeX 编译；涉及版面或表格时，追加 PDF 渲染抽查和 SyncTeX/几何验证。验证失败时不得重建生产容器或宣称优化完成，应保留失败日志和输入文件到 `../data/fix/<任务名>/`。

每次任务完成后，必须针对对应容器和对应 PDF 页面执行一次真实回归：从该容器读取最终 TeX/PDF，渲染代表性页面并检查输出，而不是只依赖单元测试或日志。页面检查结果、容器名称和产物路径必须记录在任务日志中。

提交前检查：

```bash
git status --short
git diff --check
git -C Lexoid status --short
git -C pipeline status --short
```

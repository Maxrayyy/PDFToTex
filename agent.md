# 仓库协作约定

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

单次监控检查：

```bash
python3 lexiod-pipeline/files/worker_watch.py once \\
  --config "$(pwd)/../data/monitoring/realtime/config.json"
```

查看监控摘要：

```bash
cat ../data/monitoring/realtime/latest.md
```

提交前检查：

```bash
git status --short
git diff --check
git -C Lexoid status --short
git -C lexiod-pipeline status --short
```

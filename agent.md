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

单次监控检查：

```bash
python3 pipeline/texopt/monitoring/worker_watch.py once \\
  --config "$(pwd)/../data/monitoring/realtime/config.json"
```

查看监控摘要：

```bash
cat ../data/monitoring/realtime/latest.md
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

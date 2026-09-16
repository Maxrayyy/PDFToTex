# 日报扫描耗尽文件句柄

## 2026-09-15 现场

`data/monitoring/daily/daily.md` 停留在 00:43:14。
LaunchAgent `com.lexiod.daily-stats` 仍按 600 秒执行，但退出码为 1。
`runner.error.log` 报 `OSError: [Errno 24] Too many open files`，
最后失败在读取价格表；价格表本身不是故障源。

## 根因与复现

`monitoring/daily_stats.py` 的 `completed_jobs` 使用
`with sqlite3.connect(...) as db`。SQLite 连接的上下文管理器只处理事务，
不关闭连接；文件句柄释放依赖垃圾回收。单轮扫描数据库较多时，
尚未释放的连接及关联文件会耗尽句柄。

使用现有 163 个状态库只读复现：在独立 Python 进程中禁用垃圾回收、
将 `RLIMIT_NOFILE` 软限制设为 256，然后逐个调用 `completed_jobs`。
旧代码出现 `sqlite3.OperationalError: unable to open database file`；
修复后同样条件下全部 163 个库读取成功。限制只作用于该验证进程。

## 修复与验证

用 `contextlib.closing` 包裹连接，保证正常读取和查询异常时均立即关闭，
保持只读访问和原有记账逻辑不变。新增两个回归用例，保留真实连接引用，
验证成功和异常路径退出后均无法再使用连接；修复前均失败。
日报及费用统计测试共 19 项通过；测试镜像需通过 `PYTHONPATH` 加载修改后的源码，
仅挂载 `/src` 不会替换镜像中已安装的 Python 包。

通过已有 LaunchAgent 的 `launchctl kickstart gui/501/com.lexiod.daily-stats`
触发刷新，2026-09-15 17:47:30 成功，退出码 0，调度间隔仍为 600 秒。
补计 31 份完成任务，累计 173 份、9986 源页。未重建账本、删除历史记录，
也未重启转换容器；日报程序运行在宿主机，不需要构建 Docker 镜像。

刷新仍报告一项数据核对警告：`A37Z201202605030/S22C-726080610560.pdf`
对应的发布 TeX 与完成任务校验值不一致。该文件此前有人工版式修改，
本次保留校验规则及警告，不将其作为日报程序运行失败，也不修改原文件来消除警告。

## 排查提示

不能用提高句柄限制或强制垃圾回收替代资源关闭。检查 launchd 服务时，
需在实际用户 GUI 域读取状态；沙箱内 `launchctl list` 查不到服务，
不能据此判断服务已卸载。一次性任务在成功退出后显示 `not running` 是正常状态，
应同时检查最后退出码、调度间隔和日报更新时间。

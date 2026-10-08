# 日报同步传输挂起

2026-10-03：本地 daily.md 停在 10 月 2 日 23:34，服务器日报已正常更新至 10 月 3 日。launchd 同步任务仍在运行，其 scp/ssh 子进程已持续约 9 小时 39 分钟，导致后续 120 秒定时触发无法启动新的同步。

根因：旧脚本只配置 ConnectTimeout=15，该选项限制建连阶段，不能限制已建立连接后的传输。SSH ServerAliveInterval 默认 0，没有检测失联连接的保活机制。

修复：将下载纳入 server_daily_sync.py，subprocess.run 为完整传输设置总超时；增加 SSH 保活（15 秒，最多 2 次无响应）。总超时由 daily/config.json 的 server_sync.transfer_timeout_seconds 配置，默认 90 秒。下载使用临时目录，只有完整且通过校验的远端账本才能替换本地账本。超时会清理临时文件并退出，下一次 launchd 调度可重试。

恢复：停止挂起的 launchd 任务，确认旧进程退出，执行新脚本补同步，再重新加载每 120 秒执行的任务。手动同步和 launchd 均成功，10 月 3 日行恢复为 19 份 PDF、988 源页（当时快照，后续继续增长）。

验证：test_server_daily_sync.py 共 5 项通过，覆盖挂起传输的超时参数、失败时本地账本不变及临时目录清理、成功同步新增日期、无效账本和重复 ID 拒绝。

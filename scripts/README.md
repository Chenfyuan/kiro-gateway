# 运维脚本

## cleanup_logs.sh —— 统计库日常维护

装在每台 kiro-gateway EC2 的 `/opt/kiro-gateway/cleanup_logs.sh`，root crontab
`0 18 * * *`（UTC，= 北京时间 02:00）。日志追加到 `/var/log/kiro-cleanup.log`。

安装（走 SSM，注意脚本内容要 base64 传，直接内联会被引号吃掉）：

```bash
B64=$(base64 -i scripts/cleanup_logs.sh | tr -d '\n')
# SSM 里：printf '%s' "$B64" | base64 -d > /opt/kiro-gateway/cleanup_logs.sh && chmod +x ...
# crontab 用临时文件写，别用 ( crontab -l; echo ) | crontab -，无已有 crontab 时会写空
```

做三件事，顺序有讲究：

1. **删 `request_logs` 超期行**（默认 7 天），分批 500 行提交。一次性删光会把整个
   删除操作攒成一个巨大事务，WAL 反而暴涨。`token_usage` 表始终不动。
2. **`wal_checkpoint(TRUNCATE)`** 把 WAL 收回主库。这是现在真正回收空间的一步。
3. **按需 VACUUM**：仅当空闲页 ≥2000 且占比 ≥25%、且磁盘剩余 ≥1.2 倍库大小时才做。

为什么用容器里的 python3 而不是宿主机 `sqlite3`：宿主自带的是 **3.7.17（2013 年）**，
不支持 `wal_checkpoint(TRUNCATE)`（需 ≥3.8.8），对 WAL 完全无能为力。容器里是 3.46.1。

### 这版脚本修掉的三个真实故障（2026-09，xiaomei `i-00023cf077a081f77`）

- **WAL 独占 11G，旧脚本视而不见。** 旧脚本只会 DELETE + VACUUM 主库，而当时 14G 里
  有 11G 在 WAL 上。
- **无条件 VACUUM 撞满盘。** VACUUM 需要约等于库大小的临时空间，9-20 / 9-21 两晚在
  14G 库上直接 `Error: database or disk is full`，清理连续两天完全没生效。故现在 VACUUM
  前先查剩余空间，宁可跳过也不要炸。
- **7 天保留策略对这台机器早已失效。** 9-12 → 9-21 库从 216M 涨到 14G，而每晚
  `deleted` 只有几十行——涨的不是老数据，是当天新写入的报文体。真正的修法在写入侧
  （`LOG_SUCCESS_BODY=false`，成功请求不再存报文），删行只是兜底。

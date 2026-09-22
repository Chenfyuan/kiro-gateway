#!/bin/bash
# 每日维护 kiro-gateway 统计库 /opt/kiro-gateway/data/token_usage.db。
#
# 做三件事：删 request_logs 中超期的行、把 WAL checkpoint 回主库、必要时 VACUUM。
# token_usage 表始终保留不动（纯统计，很小）。时间基准 UTC，与库内 timestamp 一致。
#
# 为什么不用宿主机的 sqlite3：宿主自带的是 3.7.17（2013 年），不支持
# wal_checkpoint(TRUNCATE)（需 >= 3.8.8）。而 WAL 才是现在的主要增长点——
# 2026-09 这台机器上 WAL 独占 11G，旧脚本对此完全无能为力，只会反复 VACUUM 整个
# 14G 主库直到磁盘满。所以统一走容器里的 python3（sqlite 3.46.1）。
#
# 另外自 2026-09-22 起（LOG_SUCCESS_BODY=false）成功请求不再存报文，主库增长已经
# 很慢，删行不再是主要手段；VACUUM 因此改成按需触发，不再每天无条件重写整库。
set -uo pipefail

CONTAINER=kiro-gateway
DB=/app/data/token_usage.db          # 容器内路径
DB_HOST=/opt/kiro-gateway/data/token_usage.db
LOG=/var/log/kiro-cleanup.log
RETAIN_DAYS=7

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

exec >> "$LOG" 2>&1

echo "[$(ts)] cleanup start; db=$(ls -lh "$DB_HOST" 2>/dev/null | awk '{print $5}') wal=$(ls -lh "$DB_HOST-wal" 2>/dev/null | awk '{print $5}') disk=$(df -h / | awk 'NR==2{print $4" free"}')"

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  # 容器没跑就没人写库，库也不会涨，跳过即可；下次容器起来后照常清理。
  echo "[$(ts)] container $CONTAINER not running, skipped"
  exit 0
fi

docker exec -i "$CONTAINER" env RETAIN_DAYS="$RETAIN_DAYS" python3 - <<'PY'
import os, sqlite3, time

DB = "/app/data/token_usage.db"
RETAIN_DAYS = int(os.environ.get("RETAIN_DAYS", "7"))
PAGE = 4096

def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def log(msg):
    print("[{}] {}".format(stamp(), msg), flush=True)

conn = sqlite3.connect(DB, timeout=600, isolation_level=None)
conn.execute("PRAGMA busy_timeout=600000")

# 1) 删超期行。分批提交，避免一个巨大事务把 WAL 顶起来（旧脚本就是这么把 WAL 撑到 11G 的）。
cutoff = "datetime('now','-{} days')".format(RETAIN_DAYS)
deleted = 0
while True:
    cur = conn.execute(
        "DELETE FROM request_logs WHERE id IN ("
        "  SELECT id FROM request_logs WHERE timestamp < {} LIMIT 500)".format(cutoff)
    )
    if cur.rowcount <= 0:
        break
    deleted += cur.rowcount
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

remaining = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]

# 2) WAL 归位。这是现在真正回收空间的一步。
wal = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
if wal and wal[0] != 0:
    log("WARNING wal_checkpoint busy={}, WAL not truncated (有长事务在读?)".format(wal[0]))

# 3) 按需 VACUUM：空闲页占比高才值得重写整库，且必须有足够临时空间
#    （VACUUM 需要约等于库大小的空闲磁盘——9/20、9/21 两次 "database or disk is full"
#     就是在 14G 库上无条件 VACUUM 撞上满盘）。
page_count = conn.execute("PRAGMA page_count").fetchone()[0]
freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
db_bytes = page_count * PAGE
st = os.statvfs("/app/data")
free_bytes = st.f_bavail * st.f_frsize
ratio = (freelist / page_count) if page_count else 0.0

if freelist < 2000 or ratio < 0.25:
    log("vacuum skipped: freelist={} ({:.0%} of {} pages), not worth rewriting".format(
        freelist, ratio, page_count))
elif free_bytes < db_bytes * 1.2:
    log("vacuum SKIPPED for safety: db={:.1f}MB but only {:.1f}MB free (need ~1.2x)".format(
        db_bytes / 1e6, free_bytes / 1e6))
else:
    t0 = time.time()
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # VACUUM 是经 WAL 重写的，之后必须再收一次
    log("vacuum done in {:.0f}s, page_count {} -> {}".format(
        time.time() - t0, page_count, conn.execute("PRAGMA page_count").fetchone()[0]))

check = conn.execute("PRAGMA quick_check").fetchone()[0]
size_mb = conn.execute("PRAGMA page_count").fetchone()[0] * PAGE / 1e6
log("deleted={} remaining={} db={:.1f}MB integrity={}".format(deleted, remaining, size_mb, check))
conn.close()
PY

rc=$?
echo "[$(ts)] cleanup end rc=$rc; db=$(ls -lh "$DB_HOST" 2>/dev/null | awk '{print $5}') wal=$(ls -lh "$DB_HOST-wal" 2>/dev/null | awk '{print $5}') disk=$(df -h / | awk 'NR==2{print $4" free"}')"

# 日志自身也别无限长
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  tail -2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exit 0

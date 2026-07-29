# Phase 1.1 — kiro-gateway 状态盘点

> 目的:摸清所有跨请求持久化的状态,给 Phase 1 无状态化改造做设计输入。
> 结论:**7 类持久状态,5 类需要迁到共享存储,2 类不用迁**。
> 已核对代码路径:2026-07-29,基于 main 分支 HEAD `102f1a2`。

---

## 0. 一图流概览

```
                                进程 A                         进程 B
                             ┌──────────┐                   ┌──────────┐
                             │ config   │ PROXY_API_KEY 缓存 │ config   │
                             │ (module) │  ← ≠ →            │ (module) │
                             └──────────┘                   └──────────┘
                             ┌──────────────────────────────────────┐
共享存储需要覆盖 ↓             │       AccountManager (per-process)     │
                             │ _accounts, _model_to_accounts,        │
                             │ _current_account_index,               │
                             │ _dirty ── ~10s ──→ state.json         │
                             │                                        │
                             │ 每 account:                             │
                             │   KiroAuthManager                      │
                             │   _access_token / _refresh_token      │
                             │   _expires_at  ── refresh ──→ 上游 API  │
                             │                  ↓ 写                  │
                             │           kiro-auth-token-*.json      │
                             └──────────────────────────────────────┘
                             ┌──────────────────────────────────────┐
                             │ UsageTracker → token_usage.db (SQLite)│
                             │ RequestLogger → request_logs (同 DB) │
                             └──────────────────────────────────────┘
```

红字部分完全在 per-process 内存里,**多实例部署时每个进程各自一份,只在磁盘上偶尔碰面 → 就是 refresh token 被烧、用量统计打架的根源**。

---

## 1. 状态清单(按迁移优先级排序)

| # | 状态 | 存储介质 | 冲突风险 | 迁移目标 | 迁移难度 |
|---|---|---|---|---|---|
| 1 | Kiro auth token (per account) | `data/kiro-auth-token-*.json` 或 SQLite `auth_kv` | ★★★ 烧 refresh token | Redis + 分布式锁 | ★★★ 高 |
| 2 | 账号池运行时状态 | `data/state.json` | ★★ 计数/round-robin 打架 | Redis(hash + counter) | ★★ 中 |
| 3 | 用量统计 | `data/token_usage.db` (SQLite) | ★★ 双写打架 | Postgres | ★★ 中 |
| 4 | 请求日志 | 同上 `request_logs` 表 | ★★ 双写打架 | Postgres(同库) | ★ 低 |
| 5 | 账号清单 | `data/credentials.json` | ★ admin 并发改动 | Postgres 表(强一致) | ★ 低 |
| 6 | 网关自身 admin key | `data/api_key.txt` | ★ 轮转不即时同步 | Secrets Manager | ★ 低 |
| 7 | Debug 日志 artifacts | `debug_logs/*` | ⓞ 只在 debug 模式,不共享无所谓 | 保留本地 | 无需迁 |

**优先级说明**:1 是必须彻底解决的正确性问题;2-4 是数据完整性;5-6 是运维体验;7 不需要动。

---

## 2. 详细分析

### 2.1 Kiro auth token —— **头号难点**

**代码位置**:
- 读:`kiro/auth.py:180` (SQLite) / `kiro/auth.py:183` (JSON) 在 `KiroAuthManager.__init__` 里
- 写:`kiro/auth.py:517` `_save_credentials_to_file()`,非原子(`open('w')` + `json.dump`,没有 tmp+rename)
- 触发:`_refresh_token_kiro_desktop:742`,`_refresh_token_aws_sso_oidc:867`,以及 `get_access_token:951, 1005` 里的 profileArn 回填。所有 refresh 都会命中。

**当前保护**:每个 `KiroAuthManager` 实例一个 `asyncio.Lock`(`auth.py:170`)。仅限进程内。

**双进程失败模式**(agent 描述得很清晰,原话不改):

> 1. 两个进程都发现 `expiresAt` 快到,各自拿 asyncio lock,同时用**同一个** refresh token POST 到 `prod.<region>.auth.desktop.kiro.dev/refreshToken`。
> 2. Kiro server 只认第一个,返回新 refresh token 给赢家;输家收到 400 `invalid_grant`。
> 3. 赢家用 `open('w')` 写文件时如果和输家的读撞上,读方会拿到 0 字节或不完整 JSON。
> 4. 输家的进程内存 `_refresh_token` 是旧的(已作废),下次请求还是它 → **该账号在该进程里永久坏掉,直到重启**。JSON 模式没有自愈路径(SQLite 模式在 400 时会重读一次,`auth.py:957`,但也只挡一次)。

**迁移方案要点**:

- 存储 → Redis Hash: `kiro:token:<account_id>` = {access_token, refresh_token, expires_at, profile_arn}
- 刷新路径必须走**分布式锁**(细节见文档 Phase 1.4):
  ```
  lock_key = kiro:refresh-lock:<account_id>
  SET lock_key <worker_id> NX PX 30000
    ↓ 拿到 → 从 Redis 读最新 token → 二次检查是否真的还需要刷 → 调 Kiro → 写回 Redis → 释放锁(Lua 原子)
    ↓ 没拿到 → 短 sleep(100ms) → 从 Redis 读最新 → 若已被别人刷则直接用
  ```
- 每次 `get_access_token` 都**先看 Redis 时间戳**,不能只信进程内存缓存,否则赢家刷新后输家还用旧 access_token(access_token 一般也有 1h TTL)。可以缓存 access_token 但要标注拉取时的 expires_at,过期回 Redis 拿。

### 2.2 账号池运行时状态 `state.json`

**代码位置**:
- 数据结构见 agent report Section B。核心字段:`current_account_index`(全局 round-robin 光标)、`accounts[<id>].failures`、`accounts[<id>].stats.total_requests`、`accounts[<id>].current_usage` 等。
- 写触发:15 处 `_dirty=True` 标记(`account_manager.py:504, 661, 764, 801, 929, 966, 973, 984, 995, 1000, 1031, 1042, 1056, 1236, 1254`)+ 定时 flusher `save_state_periodically`(每 10s)+ 关停时最终 flush。
- 读:**只在启动时 `load_state()` 读一次**,之后完全在内存操作。

**双进程失败模式**:
- 每个进程有自己的内存副本。定时 flush 是**最后写的赢**——A 进程的 `total_requests=100` 被 B 进程的 `total_requests=80` 覆盖回 80。
- `current_account_index` 各自跑各自的 round-robin,两个进程会**独立**地把请求撒给账号 1、2、3……,不协调。

**迁移方案要点**:

- 存 Redis Hash + counter:
  - `kiro:accounts:<id>` hash 存 `failures/current_usage/quota/health` 等。
  - `kiro:current-index` counter,用 `INCR % N` 实现共享 round-robin。
  - `stats.total_requests` 用 `HINCRBY` 保证并发累加正确。
- `_dirty` + 定时 flush 的模式**取消**,改成**每次 mutation 立刻写 Redis**(反正 Redis 操作是微秒级)。
- 启动时不再"读一次 state.json",而是**按需读 Redis**,读到就是最新。

### 2.3 用量统计 + 请求日志(token_usage.db + request_logs)

**代码位置**:
- 两个类共用一个 SQLite 文件,各自 `sqlite3.connect(..., check_same_thread=False)` + WAL(`kiro/usage_tracker.py:37`, `kiro/request_logger.py:31`)。
- 写入是 `INSERT`,`asyncio.Lock` 保护(单进程 OK)。请求路径里有的直接 `await`,有的 `asyncio.create_task` fire-and-forget。
- 读取只在 admin 页面(`routes_admin.py:544, 553, 562`)。

**双进程失败模式**:
- SQLite WAL 允许多读单写。第二个进程的 INSERT 会拿到 `database is locked`。
- 更严重:如果这两台机器共享的 volume 是 NFS/EFS(未来 Fargate on EFS 可能),SQLite WAL **在网络文件系统上不安全**,会静默损坏。

**迁移方案要点**:

- 建 RDS Postgres,把两张表按 SQLite 现有 schema 迁过去。
- 需要改的 SQL(agent 已列):
  - `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL PRIMARY KEY`
  - `DATETIME DEFAULT CURRENT_TIMESTAMP` → `TIMESTAMPTZ DEFAULT NOW()`
  - `DATE(timestamp)` → `timestamp::date`
  - 去掉 `PRAGMA journal_mode/synchronous`
  - 去掉运行时 `ALTER TABLE ADD COLUMN` 兼容(`request_logger.py:60-64`),改成正儿八经的 migration(alembic)
- 连接管理:改用 `asyncpg` 或 `psycopg[async]` + 连接池;不再是单连接单锁。
- 现有的 `asyncio.Lock` 都可以去掉,数据库自带并发控制。
- **好消息**:代码里没用 `INSERT OR REPLACE`、`AUTOINCREMENT` 之外的 SQLite-only 语法,迁移比较干净。

### 2.4 账号清单 `credentials.json`

**代码位置**:
- 读:`main.py:229, 363, 367, 387` 启动时;`account_manager.py:247` 里 `load_credentials()`。
- 写:`main.py:416, 450`(启动时一次性从 `.env` 迁移);`account_manager.py:1263` `_save_credentials_config`,被 `add_account:1192, 1200` 和 `remove_account:1234` 调用。**非原子**,`open('w') + json.dump`。

**双进程失败模式**:
- admin 并发调用 add/remove 会 lost update。
- 中间态被另一个进程读到会 `JSONDecodeError`。

**迁移方案要点**:

- 迁 Postgres 一张 `kiro_accounts` 表:`id, path, type, refresh_token, profile_arn, region, added_at, disabled` 等。
- 现有 `credentials.json` 的"path 指向另一个文件"这种间接引用可以直接展平到 `refresh_token` 列——因为 token 文件本身已经迁到 Redis(见 2.1),不再有独立文件。
- 启动时读 Postgres 而不是 JSON。
- Admin add/remove 用 `INSERT`/`DELETE`,不再有 read-modify-write。

### 2.5 网关自身 admin key `api_key.txt`

**代码位置**:
- 读:`kiro/config.py:103-106`,进程启动时读一次到模块全局 `PROXY_API_KEY`。
- 写:`kiro/config.py:118` `set_proxy_api_key()`,只被 `routes_admin.py:679` `update_api_key` endpoint 调用。

**双进程失败模式**:
- 管理员通过一个进程 PATCH 改 key,其他进程的内存里还是旧 key,继续接受旧 key、拒绝新 key。
- `routes_admin.py:303` 有一段"自己给自己发 request 同步"的兜底代码,但它连的是 `localhost:$SERVER_PORT`——**多进程/多实例时只碰到自己**,别人的进程都同步不到。

**迁移方案要点**:

- 挪到 **AWS Secrets Manager**,一个 secret `kiro-gateway/prod/proxy-api-key`。
- 每次校验时读 Secret(可加 30s 内存缓存),或订阅 Secrets Manager 的 ROTATE 通知。
- Admin 通过 API 改 key = 调 Secrets Manager 更新那个 secret;所有实例下一次校验时自动拿到新值。

### 2.6 Debug logs

- `kiro/debug_logger.py:244, 340, 354, 393` 写 `debug_logs/*`,只在 `DEBUG_MODE=on` 时启用。
- **不需要跨实例共享**,每个实例写自己的 debug 记录就好。迁 Phase 2 到 Fargate 后可以选:
  1. 完全走 CloudWatch(已有 log group),这些 debug 日志用一个特殊 prefix 输出到 stdout。
  2. 挂 EFS 卷共享(**不推荐**,增加运维负担)。

推荐方案 1。这一步在 Phase 1 里不用动,Phase 2 时统一改成 stdout 输出即可。

---

## 3. 环境变量清单(auth 相关)

| 变量 | 用途 | Phase 1 后是否保留 |
|---|---|---|
| `PROXY_API_KEY` | 网关 admin key 默认值 | 保留(仅用于首次 bootstrap 到 Secrets Manager) |
| `REFRESH_TOKEN` | 首次 bootstrap 时写入 credentials.json | 保留(仅 bootstrap 用) |
| `PROFILE_ARN` | AWS SSO Profile ARN | 保留 |
| `KIRO_CREDS_FILE` | 从本地 kiro 客户端 credentials.json 迁移 | ⚠️ **将失效**,因为不再读本地文件 |
| `KIRO_CLI_DB_FILE` | 从 kiro-cli SQLite 迁移 | ⚠️ **将失效** |
| `KIRO_REGION` | Kiro API region | 保留 |
| `SQLITE_READONLY` | 只读模式 | 移除(SQLite 完全废弃) |
| `ACCOUNT_SYSTEM` | 新旧账号系统开关 | 移除,永久走"新" |
| `ACCOUNTS_CONFIG_FILE` | credentials.json 路径 | 移除,改成 `DATABASE_URL` |
| `ACCOUNTS_STATE_FILE` | state.json 路径 | 移除,改成 `REDIS_URL` |

**新增**:
- `REDIS_URL` — ElastiCache endpoint
- `DATABASE_URL` — RDS Postgres
- `PROXY_API_KEY_SECRET_ARN` — Secrets Manager secret ARN
- `STORAGE_BACKEND=file|redis+postgres` — 灰度开关,file 走老逻辑用于回滚

---

## 4. 现有 asyncio.Lock 一览(改造时要清理或替换)

| Lock | 位置 | Phase 1 后 |
|---|---|---|
| `KiroAuthManager._lock` (per account) | `auth.py:170` | 替换为 Redis 分布式锁 |
| `AccountManager._lock` | `account_manager.py` (类内多处) | 大部分可移除(改依赖 Redis 原子操作),少量保留用于内存缓存一致性 |
| `UsageTracker.lock` | `usage_tracker.py` | 移除,让 Postgres 处理并发 |
| `RequestLogger.lock` | `request_logger.py` | 移除 |

---

## 5. FastAPI 生命周期改动清单

启动时需要做的事(比现在多几步):
1. `RedisClient.init(REDIS_URL)`
2. `PostgresPool.init(DATABASE_URL)`
3. `SecretsManagerClient.init()` + 首次预取 `PROXY_API_KEY`
4. `AccountManager.load_from_postgres()`(替代 `load_credentials + load_state`)
5. 现有的 `save_state_periodically` **删除**——不再有周期性 flush,所有写操作即时到 Redis。
6. `health_check_periodically` 保留,但用分布式锁保证只有一个进程在跑(Redis `SETNX` + TTL,竞选出 leader)。

关停时:
1. 关连接池
2. **不需要**再 flush state 到磁盘

---

## 6. 灰度切换设计

用 `STORAGE_BACKEND` 环境变量控制:

```
STORAGE_BACKEND=file           # 老逻辑,现有生产就是这个,零风险
STORAGE_BACKEND=redis+postgres # 新逻辑,写共享存储
STORAGE_BACKEND=file,dual-write=redis+postgres  # 双写模式,做迁移验证
```

灰度路径:
1. 先在 dev 环境用 `file,dual-write=...` 跑 3-7 天,对比两边的 state / usage 数据一致性。
2. 切 dev 到 `redis+postgres`,再跑 3 天。
3. Prod 上先 `file,dual-write=...` 跑几天(此时只有一个实例,Redis 没有真正参与决策,只是被写)。
4. Prod 切 `redis+postgres`。
5. 稳定 1 周后可以起第二个实例(这才真正验证了无状态)。

---

## 7. Phase 1 落地任务拆分(下一步给 Phase 1.2 做参考)

按依赖顺序:

- **1.2 抽 storage 接口层**(纯 refactor,不动业务)
  - 定义 `TokenStore` / `AccountRegistry` / `UsageStore` / `AdminKeyStore` 四个接口
  - 现有 File/SQLite 实现改成 `FileTokenStore` / `JsonAccountRegistry` / `SQLiteUsageStore` / `FileAdminKeyStore`
  - `STORAGE_BACKEND=file` 时行为与现在完全一致
  - **验收**:所有测试通过,prod 用 `STORAGE_BACKEND=file` 部署无异常

- **1.3 建 Redis + Postgres 基础设施**(纯 infra)
  - ElastiCache Redis(cache.t4g.micro,单节点,`kiro-gateway-prod` VPC 内)
  - RDS Postgres(db.t4g.micro,单节点)
  - 更新 `.env` 加 `REDIS_URL` / `DATABASE_URL`
  - 网关容器还是老代码,只是能连上而已(靠 healthcheck 验证)

- **1.4 写 Redis + Postgres 实现类**(核心代码)
  - `RedisTokenStore`:含分布式锁
  - `RedisAccountRegistry`(其实主要是 state,accounts 元数据在 Postgres)
  - `PostgresAccountRegistry`
  - `PostgresUsageStore`
  - `SecretsManagerAdminKeyStore`
  - **重点单测**:
    - 两个并发 `refresh_token` 只有一个真正调 Kiro
    - 持锁进程崩溃 30s 后另一进程能刷
    - Redis 断连时的降级路径(拒绝新请求还是 fail-open?)

- **1.5 数据迁移脚本**
  - `token_usage.db` → Postgres:一次性 dump/load
  - `state.json` → Redis:一次性 SETNX 写入
  - `kiro-auth-token-*.json` → Redis:一次性 HSET
  - 迁移前完整备份 SQLite 到 S3

- **1.6 灰度切换到 prod**
  - dev 环境 dual-write 跑 3 天
  - dev 切 redis+postgres 3 天
  - prod dual-write 3 天
  - prod 切 redis+postgres
  - 起第二个 prod 实例做真实多实例验证

---

## 8. 待你决策的关键点

- **Postgres 是新建实例还是复用 ai-console 那个?** ai-console 已经在用一个 Postgres(见 CLAUDE.md,本地 dev 是 `aiconsole-pg`)。生产上如果同一个账号已经有 RDS,可以复用,建一个 `kiro_gateway` schema 隔离表;否则起新的更清晰。
- **Redis 一开始单节点省 $6/月,还是直接主从?** 单节点挂了 → 所有实例 token 刷新失败(现有请求还能跑,因为 access_token 有 1h 缓存;但过了 1h 会陆续失败)。可以先单节点,加个 CloudWatch alarm 起来后再升。
- **`credentials.json` 迁到 Postgres 后,那个"path 指向另一个文件"的 indirection 要保留吗?** 我倾向直接展平——refresh_token 直接存 accounts 表列。这样 admin 加账号一步到位,不用再维护两层。除非有兼容第三方的诉求(比如 `KIRO_CREDS_FILE` 从别的路径迁进来),否则移除更干净。
- **是否顺手把 `debug_logs` 也改成 stdout?** 建议是,但可以推迟到 Phase 2 做。

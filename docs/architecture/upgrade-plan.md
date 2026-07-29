# kiro-gateway prod 生产级架构升级方案

> 目标：支持多实例并行、滚动更新时不影响正在处理的请求，同时保证 Kiro refresh token 不被并发刷新失效、用量统计不双写、账号池调度不重复分配。

日期：2026-07-29
作者/评审：linwj44

---

## 1. 现状盘点

### 1.1 实际运行状态

- **实例**：单机 `i-0f5d6c7d8121f5a87`（t3.small，2 vCPU / 2 GB），docker 直接 `docker run` 起容器，容器名 `kiro-gateway`，镜像 `kiro-gateway-kiro-gateway`。
- **入口**：`kiro-gateway-prod-lb`（ALB, us-east-1）→ target group `kiro-gateway-prod-tg` → 上面唯一一台 EC2 的 :8000。
- **负载**：CPU 0.12%、内存 43MB / 1.9GB，机器容量远未打满。
- **日志**：已接入 CloudWatch Logs `/kiro-gateway/prod`，保留 7 天（[[project-kiro-gateway-cloudwatch-logs]]）。

### 1.2 本地状态盘点（`/opt/kiro-gateway/data/`）

| 文件 | 内容 | 是否跨实例可共享 |
|---|---|---|
| `api_key.txt` | 网关自己的 admin key（静态） | 可共享（内容不变） |
| `credentials.json` | 初始 credentials 配置 | 可共享（静态） |
| `kiro-auth-token-49089184.json` | Kiro 账号 A 的 access/refresh token | ❌ **强共享冲突源** |
| `kiro-auth-token-6a810667.json` | Kiro 账号 B 的 access/refresh token | ❌ **强共享冲突源** |
| `state.json` | 账号池调度状态（round-robin 计数、冷却等） | ⚠️ 冲突 |
| `token_usage.db` (SQLite) | 用量统计 | ⚠️ 冲突（SQLite 不能多进程写） |

关键结论：**Kiro refresh token 是一次性的**，多实例并发刷新时先赢的那台会把 refresh token 换成新值，后来的那台带着老 token 去换，直接被上游作废——整个账号失效直到手动重置。这是当前"多实例"部署最致命的一个坑，任何架构升级都必须优先解决。

### 1.3 当前发布方式的问题

- 部署命令是在机器上直接 `git pull origin main && docker build && docker stop && docker run`——**中断服务几十秒**，且任何合入 main 的东西立刻进 prod，没有版本锁定和回滚能力。
- 没有健康检查感知：`docker stop` 直接杀，正在处理的流式请求全部断掉。

---

## 2. 目标

按优先级从高到低：

1. **正确性**：多实例并行时 refresh token / 用量统计 / 账号池调度不冲突。
2. **可用性**：滚动更新时不掉请求，长连接（流式）优雅关闭。
3. **可伸缩性**：能按流量水平扩展实例数，不再靠升机型。
4. **可运维性**：发布/回滚/查日志/看指标都通过标准 AWS 工具，不再 SSH 或 SSM 手动执行部署命令。
5. **成本可控**：整体月度成本不显著上涨，最好因资源利用率提升而下降。

---

## 3. 目标架构（终态）

```
┌────────────────────────────────────────────────────────────────────┐
│ Route 53 / 现有域名                                                 │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │  kiro-gateway-prod-lb  │  (ALB, 保留)
                    │  健康检查 /health       │
                    │  绝对超时 6xx s (SSE)   │
                    │  deregistration        │
                    │  delay 60 s            │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │ target group           │
                    │ kiro-gateway-prod-tg   │
                    │ target type = ip       │
                    └───────────┬────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
      ┌───────▼──────┐  ┌───────▼──────┐  ┌──────▼───────┐
      │ Fargate task │  │ Fargate task │  │ Fargate task │  ...
      │ kiro-gateway │  │ kiro-gateway │  │ kiro-gateway │
      │ (无状态)     │  │ (无状态)     │  │ (无状态)     │
      └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
             │                 │                 │
             └────────┬────────┴────────┬────────┘
                      │                 │
             ┌────────▼────────┐  ┌─────▼─────┐
             │ ElastiCache     │  │ RDS       │
             │ Redis           │  │ Postgres  │
             │                 │  │           │
             │ - Kiro tokens   │  │ token_    │
             │ - refresh 分布式 │  │ usage 表  │
             │   锁            │  │           │
             │ - 调度 state    │  │           │
             └─────────────────┘  └───────────┘

     ECR (镜像仓库) ──► ECS 服务定义 ──► 滚动更新
```

**关键选型说明**：

- **ECS on Fargate**，不上 EKS：现在总共 4 个环境规模不足以摊薄 EKS 控制面 + 团队学习成本。Fargate 一条 `aws ecs update-service` 就能滚动、日志/指标/告警一条龙接入。
- **ElastiCache Redis 单节点**（起步）→ 主从（生产稳定后）：refresh token 和调度 state 都在这里，用 `SETNX` + TTL 做分布式锁保护 token 刷新。
- **RDS Postgres**（或 Aurora Serverless v2 小规模）承接 `token_usage.db`：SQLite 天然不支持多进程写入，必须迁走。
- **ALB 保留，target type 从 `instance` 改成 `ip`**：Fargate 任务只能作为 IP target 注册。
- **不用 ASG on EC2**：状态迁移做完后 EC2 的所有优势都消失，Fargate 更省心。

---

## 4. 迁移路线（4 个阶段，每一步都可独立发布 + 回滚）

### Phase 0 — 发布流程标准化（先做，1-2 天，无架构变化）

**动机**：把"机器上 git pull 后 docker build"这条最脆弱的路径先干掉，为后面所有阶段铺路。

- 建 ECR 仓库 `kiro-gateway`。
- GitHub Actions（或 GitLab CI）在 push `main` 时 build 镜像、打 `sha-<7位>` + `latest` 两个 tag，push 到 ECR。
- 现有 EC2 部署命令改成：拉指定 tag 镜像 → `docker stop && docker run`（去掉 `git pull` 和 `docker build`）。
- 发布记录一律走 tag，回滚就是 `docker run kiro-gateway:sha-abc1234`。

**验证**：能用 tag 部署 + 回滚到上个 tag。

**回滚方案**：任何时候恢复回原来的 `git pull` 脚本即可，不影响运行时。

### Phase 1 — kiro-gateway 无状态化（核心，5-10 天）

**动机**：这是所有后续步骤的前置。不做完这一步，加任何实例都会出错误性问题。

#### 1.1 抽象 storage 层

- 在 kiro-gateway 代码里定义 `TokenStore` / `StateStore` / `UsageStore` 三个接口。
- 现有实现是 `FileTokenStore` / `FileStateStore` / `SQLiteUsageStore`，保留、不删。
- 新增 `RedisTokenStore` / `RedisStateStore` / `PostgresUsageStore`。
- 通过环境变量 `STORAGE_BACKEND=file|redis+postgres` 切换。

#### 1.2 refresh token 并发刷新的分布式锁

**这是最容易踩坑的地方**，方案要写在文档里而不是留给实现者临场发挥：

```
锁 key：  kiro:refresh-lock:<account_id>
锁值：    <task_id>-<uuid>   （便于观察/排错）
获取：    SET key val NX PX 30000   （30s 超时防死锁）
持有：    刷新期间续期，如需要
释放：    仅当值匹配时 DEL（Lua 脚本原子）

流程：
  1. token 快过期 → 尝试拿锁
  2. 拿到 → 调 Kiro 换新 refresh token → 写回 Redis → 释放锁
  3. 没拿到 → 短暂 sleep（100ms）后从 Redis 读最新 token 重试
```

**测试用例**（必须写）：
- 两个实例同时 detect token 过期，只有一个真正调用 Kiro。
- 持有锁的实例崩溃，30s 后另一实例能拿到锁继续刷新。
- 刷新失败（网络等）不写 Redis，锁自动过期，下次重试。

#### 1.3 用量统计迁移

- `token_usage.db` → Postgres 表（schema 保持一致）。
- 迁移脚本：一次性把 SQLite 内容 dump 出来，导入 Postgres。
- 双写过渡期（可选）：新代码同时写 SQLite 和 Postgres，稳定 1 周后停 SQLite 读。

#### 1.4 本地文件挂载可以摘掉

- 无状态化完成后 `-v /opt/kiro-gateway/data:/app/data` 只剩 `api_key.txt` 和静态 `credentials.json`。前者迁到 Secrets Manager，后者打进镜像。

**验证**：单 EC2 部署，storage 全走 Redis + Postgres，业务功能与原本 file-based 一致；再手动同机器起两个实例（不同端口），指向同一 Redis/Postgres，观察 refresh token 只被换一次。

**回滚方案**：`STORAGE_BACKEND=file` 一键回退到原逻辑。

### Phase 2 — 底座迁 ECS Fargate（3-5 天）

**动机**：无状态化完成后，运行环境从 EC2 改成 Fargate，才能享受滚动更新、自动扩缩容。

- 建 ECS cluster `kiro-gateway-prod`。
- Task definition：
  - CPU 512 / Memory 1024（先给足 headroom，看指标调）。
  - Container image 从 ECR 拉 tag（Phase 0 已备好）。
  - Secrets 从 Secrets Manager 注入（.env 里那些）。
  - CloudWatch log driver（沿用 `/kiro-gateway/prod` log group，stream 名带 task id 自动区分）。
- Service：
  - Desired count = 2 起（跨 AZ）。
  - **健康检查宽限期 60s**，让容器有时间冷启动接 Redis。
  - **Deregistration delay = 60s**（ALB 层），保证正在处理的请求收尾。
  - **Rolling update**：`minimumHealthyPercent=100`, `maximumPercent=200`——总是先起新的确认健康，再摘旧的，任意时刻至少有 desired 数量的健康实例。
- ALB target group：`target-type=ip`，端口 8000，健康检查 `/health` 每 30s 一次，2 次失败摘除。
- 旧 EC2 保留 1-2 周作 fallback，DNS 或 target group 切换即可。

**滚动更新的真实行为**（用户最关心的部分）：

```
初始：       [v1] [v1]               desired=2, running=2
发布 v2：
  step 1：   [v1] [v1] [v2(starting)]   running=3, healthy=2 (v1×2)
  step 2：   [v1] [v1] [v2(healthy)]    healthy=3
  step 3：   [v1] [v1(draining)] [v2]   摘掉一个 v1，等它排空连接（60s deregistration）
  step 4：   [v1] [v2(starting-2)] [v2] 再起一个 v2
  step 5：   [v1(draining)] [v2] [v2]   摘掉最后一个 v1
  step 6：   [v2] [v2]                  完成
```

整个过程 ALB 后端始终有 ≥2 个健康实例接流量。正在处理的 HTTP 请求在 draining 期间会走完，新请求路由到新版本。**流式（SSE）请求需要 ALB idle timeout 覆盖到你们最长回复时间**，见下面注意事项。

**验证**：`aws ecs update-service --force-new-deployment` 触发一次滚动，`hey`/`wrk` 打压力观察不掉请求。

**回滚**：`aws ecs update-service --task-definition <上一版本 arn>` 秒级回滚。

### Phase 3 — 高可用/弹性能力（1-2 天）

- **Auto Scaling**：ECS service level metric，CPU > 60% 时 scale out（每次 +1，冷却 3 分钟），CPU < 20% 且 count > 2 时 scale in。
- **多 AZ**：Fargate task 自动分布到多个 AZ subnet，ALB 也 cross-zone。
- **Alarms**：`5xx > 1%`、`TargetResponseTime p99 > 5s`、`UnhealthyHostCount > 0` 各接一个 SNS → 飞书/邮件。
- **Redis 备份**：ElastiCache 开自动快照，7 天保留。
- **回滚 runbook**：写清楚 3 种回滚场景（业务 bug、Redis 宕、Postgres 宕）分别怎么办。

---

## 5. 关键决策与注意事项

### 5.1 SSE / 长连接的 ALB idle timeout

kiro-gateway 大量走**流式响应**（agent-router → kiro-gateway → Kiro 后端 → 流回来）。默认 ALB idle timeout 60s，长回复会被切断。

**行动**：
- 把 ALB idle timeout 拉到 **600s**（大部分模型响应上限），实际用量看日志找 p99 之后再调。
- Fargate task 的 `StopTimeout` 至少设 **120s**，让 SIGTERM 后有时间把当前 SSE 流收尾。
- kiro-gateway 代码要监听 SIGTERM，收到后拒绝新请求但让存量流走完（如果目前没做，Phase 1 顺手补上）。

### 5.2 refresh token 锁的死角

- **锁值一定要放 task id + uuid**：单纯用固定值 + `DEL` 会误删别人拿的锁。用 `SET key val NX PX ...` + Lua `if get(key)==val then del(key) end` 释放。
- **锁 TTL 要大于 Kiro token 换取的最坏 latency + 网络抖动余量**：目前观察下来 Kiro 换 token 一般 <2s，锁 TTL 给 30s 足够，别小气。
- **失败重试要 backoff**：连续 3 次拿不到锁就放弃这次请求（回上层 503），不要一直重试撑爆 Redis 连接。

### 5.3 Secrets 管理

- `.env` 里那些 refresh token / API key / VPN proxy 密码，从 SSM Parameter Store 或 Secrets Manager 注入，不要放镜像里也不要放 task definition 明文。
- IAM role 只给对应 Secret 的 `secretsmanager:GetSecretValue`，用资源级 ARN 限死。

### 5.4 xiaomei / test / dev 环境不同步升级

本文档只覆盖 prod。xiaomei/test/dev 可以稍晚跟上，但注意：

- **不要复用同一份 Redis / Postgres**：数据隔离要靠不同实例或不同 key prefix + 不同 database。
- CloudWatch stream 命名建议改成 `kiro-gateway-<env>-<task-id>`，写死会跟 Phase 2 冲突。

---

## 6. 迁移过程中如何"不影响正在运行的实例"

用户明确要求的这一点，落地要靠三个东西同时到位：

1. **蓝绿 / canary 部署方式**：Phase 2 完成后，ECS 的 rolling update 天然做到"新的 healthy 之前不摘旧的"。
2. **ALB 层 deregistration delay ≥ 60s**：确保被摘的实例上正在处理的请求走完。
3. **应用层 graceful shutdown**：容器收到 SIGTERM 后 stop accepting new requests，让存量请求走完再退出。Fargate 默认给 30s，StopTimeout 拉到 120s 更稳。

Phase 0/1/3 的操作**不涉及重启 prod 容器**（除了 Phase 1 上线时一次），过程中现有容器持续服务。Phase 2 切流用 DNS 或 target group 灰度：

```
step 1: 保留旧 target group (EC2)，同时把新 target group (Fargate) 注册到同一个 ALB listener，权重 10%/90%。
step 2: 观察 10 分钟，指标正常 → 提到 50/50。
step 3: 再观察 → 100/0，摘掉旧 EC2 target group。
step 4: 保留旧 EC2 待命 1-2 周，稳定后 terminate。
```

任意一步指标异常，权重打回 0/100 就是全量回滚。

---

## 7. 成本估算（月度，us-east-1 大致价格）

| 项目 | 现状 | 目标 | 差额 |
|---|---|---|---|
| EC2 t3.small × 1（prod） | ~$15 | $0（terminate） | -$15 |
| ALB | ~$18 | ~$18（保留） | $0 |
| Fargate 2 task × 0.5 vCPU × 1GB | — | ~$30 | +$30 |
| ElastiCache Redis cache.t4g.micro | — | ~$12 | +$12 |
| RDS Postgres db.t4g.micro | — | ~$15 | +$15 |
| CloudWatch Logs (7 天保留) | ~$1 | ~$2 | +$1 |
| Secrets Manager (~5 个 secret) | — | ~$2 | +$2 |
| **合计** | **~$34** | **~$79** | **+$45** |

多花约 $45/月，换来 HA + 滚动更新 + 水平扩展能力。如果对成本敏感，Redis/Postgres 可以先用**单节点最小规格**跑，等业务真的需要才升 HA。

---

## 8. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| Kiro refresh token 迁移过程中失效 | 中 | 全部账号需人工重置 | Phase 1 用双写模式 + 手动备份现有 token；先在 dev 跑 3 天 |
| Fargate 冷启动时 Redis 未就绪，健康检查失败 | 低 | 部署慢一点 | health check 宽限期 60s；应用启动时对 Redis 做 retry |
| SSE 请求在滚动更新时被截断 | 中 | 客户端看到 stream 中断 | deregistration delay + StopTimeout + 应用层 graceful shutdown 三重保证 |
| Redis 单点故障 | 低 | 短暂全站不可用 | 后期升 Redis 主从；应用层对 Redis 故障 fail-open（可选） |
| Postgres 单点故障 | 低 | 用量统计不可写但业务能继续 | 用量层做 buffer，Postgres 恢复后 replay |
| 老 SQLite 数据丢失 | 低 | 历史用量报表缺失 | Phase 1 迁移前完整备份 SQLite 到 S3 |

---

## 9. 时间线预估

| 阶段 | 预估工时 | 依赖 |
|---|---|---|
| Phase 0（ECR + CI 发布） | 2 人日 | 无 |
| Phase 1（无状态化） | 8-12 人日 | Phase 0 |
| Phase 2（迁 Fargate） | 3-5 人日 | Phase 1 |
| Phase 3（HA + 告警） | 2 人日 | Phase 2 |
| **总计** | **约 3 周** | 顺序执行；Phase 0 可与 dev/test 环境同步验证 |

---

## 10. 待你确认的关键点

在开工之前，有几点决策需要你拍板：

1. **状态存储选型是否用 ElastiCache + RDS**？如果你们其他服务已经在用 AWS 别的方案（例如 DynamoDB），可以复用。
2. **Redis / RDS 一开始起单节点省钱，还是直接上多 AZ 主从**？取决于对 prod 短暂不可用的容忍度。
3. **发布流程用 GitHub Actions 还是 GitLab CI**？看 kiro-gateway 仓库在哪。
4. **老 EC2 保留多久作 fallback**？建议 2 周，成本可控。
5. **本文档要不要拆成"设计文档"+"执行 runbook"两份**？现在是合在一起的，如果团队大可以拆。

我可以基于确认结果继续做下面任意一件事：
- 把 Phase 0 具体到 GitHub Actions workflow yaml。
- 把 Phase 1 的 `TokenStore` 接口和 Redis 实现写出来（如果告诉我 kiro-gateway 用什么语言/框架）。
- 把 Phase 2 的 ECS task definition + service definition 出 Terraform / CloudFormation。

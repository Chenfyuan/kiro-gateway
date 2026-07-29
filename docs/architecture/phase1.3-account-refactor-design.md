# Phase 1.3 — AccountManager 拆分设计

> 目的:把 `kiro/account_manager.py` 里目前**读、写、调度、缓存**四合一的 God
> Class 拆开,让存储层可以在 Phase 1.4 一次性替换为 Redis + Postgres,而**调度
> 层完全不用改**。
>
> 前置:[[project-kiro-gateway-phase1-state-inventory]] 已识别 4 类冲突源。
> 参考:[[project-kiro-gateway-cloudwatch-logs]] 里对 prod 部署形式的约束。

---

## 1. 为什么 Phase 1.2b 的"薄门面 wrap"策略在这里不适用

在 Phase 1.2b-1(AdminKeyStore)和 1.2b-2(UsageStore)里,我们用了一种"wrap
现有类,通过属性 hoist 保持调用方不改"的策略,收效很好。但对 `AccountManager`
不适用,原因:

| 维度 | AdminKey / UsageStore | AccountManager |
|---|---|---|
| 存储访问点 | 集中在 2-3 处(get/set 或 record) | **分散在 1000+ 行代码里** |
| 存储内容 | 纯 I/O(key 字符串、SQLite 行) | I/O + 内存对象(Account) + 调度逻辑混在一起 |
| 与业务逻辑耦合 | 独立(纯持久化组件) | 深度耦合(`_initialize_account`、`get_next_account`、`report_success`/`_failure` 都会顺手改状态) |
| 现有类是否值得保留 | 是 —— 只是换存储后端 | **不是** —— Account 里既存元数据也存运行时状态,应当拆开 |

Wrap `AccountManager` 只会做出一层空转的适配层,反而增加复杂度。**正确的做法
是重构它**。

---

## 2. 目标模型

把 `Account` dataclass 里的字段按**责任**拆成三组:

```
Account (当前) ──┬─ 元数据(改动频率低,持久化):
                 │     id, auth_type, disabled, nickname, email
                 │
                 ├─ 认证凭据(每 1h 换一次,分布式锁保护):
                 │     access_token, refresh_token, expires_at,
                 │     profile_arn, region, client_id, client_secret
                 │     (对应现在 KiroAuthManager 里那份)
                 │
                 └─ 运行时状态(高频改,可容忍轻微延迟一致):
                       failures, last_failure_time,
                       current_usage, usage_limit, quota_updated_at,
                       last_health_check_at, last_health_status,
                       stats.{total_requests, successful, failed},
                       models_cached_at
```

这三组分别对应 storage 层的三个组件:

| 组 | 存储组件 | Phase 1.4 后端 |
|---|---|---|
| 元数据 | `AccountRegistry` | Postgres 表 `kiro_accounts` |
| 认证凭据 | `TokenStore` | Redis Hash `kiro:token:<id>` + 分布式锁 |
| 运行时状态 | `AccountRuntimeStore`(新,原设计里没有) | Redis Hash `kiro:runtime:<id>` |

之所以把"运行时状态"独立成一个组件,是因为它的**读写模式**跟另外两个完全不
同:

- 元数据: 冷,几乎只在启动读、admin 改。
- 认证凭据: 每 1h 由持锁进程更新一次,读多写少。
- 运行时状态: 每次请求都可能改 `stats.total_requests`,每次失败改 `failures`,
  健康检查改 `last_health_check_at`。它是**唯一需要 Redis `HINCRBY` 之类原子
  计数器**的组件。

如果都合到 `AccountRegistry` 里,读元数据也得读 Redis,读 Redis 又要 fallback
到 Postgres...结构会很别扭。分开更清爽。

---

## 3. 新的 AccountManager 长什么样

**保留**:
- `get_next_account()` 的调度算法(round-robin / sticky / weighted)
- `report_success()` / `report_failure()` 的 circuit-breaker 逻辑
- 内存缓存(`self._accounts`)—— 但要改成**只对元数据缓存**,不再缓存运行时
  状态。

**移除**:
- `load_credentials()` 里 190+ 行的 credentials.json 解析、文件系统扫描
- `load_state()` / `_save_state()` / `save_state_periodically()` —— 由
  `AccountRuntimeStore` 接管
- `add_account()` / `remove_account()` 里的文件 I/O —— 由 `AccountRegistry`
  + `TokenStore` 接管
- 每次改字段都 `self._dirty = True` 的模式 —— 改成"每次 mutation 立刻写 Redis"

**新增**:
- 构造函数注入 3 个 store,不再直接持有文件路径
- 启动时 `await registry.list_accounts()` 建内存字典,不再自己读 JSON

伪代码:

```python
class AccountManager:
    def __init__(
        self,
        registry: AccountRegistry,
        tokens: TokenStore,
        runtime: AccountRuntimeStore,
    ):
        self._registry = registry
        self._tokens = tokens
        self._runtime = runtime
        self._accounts: dict[str, Account] = {}  # 只装元数据 + 调度器需要的引用
        self._current_account_index: int = 0     # 内存 cache,权威值在 runtime

    async def start(self):
        for record in await self._registry.list_accounts():
            self._accounts[record.id] = Account(id=record.id, auth_type=record.auth_type, ...)
        # 不再 load_state —— 需要用的时候现查 runtime

    async def get_next_account(self, model: str) -> Account:
        # 调度算法完全保留
        # 但读 failures/quota 时不查内存,查 self._runtime
        ...

    async def report_failure(self, account_id: str, error: Exception):
        await self._runtime.increment_failure(account_id)   # HINCRBY
        await self._runtime.set_last_failure_time(account_id, time.time())
        # 没有 self._dirty,没有 _save_state

    async def add_account(self, credentials: dict) -> str:
        record, initial_token = build_from_credentials(credentials)
        await self._registry.add_account(record, initial_token)  # 原子事务
        self._accounts[record.id] = Account(id=record.id, ...)
        return record.id
```

---

## 4. AccountRuntimeStore 接口(新)

加到 `kiro/storage/interfaces.py`:

```python
class AccountRuntimeStore(abc.ABC):
    """Per-account runtime state (counters, health, quota).

    Read/write patterns differ enough from AccountRegistry to warrant a
    separate abstraction — this state changes on every request, whereas
    AccountRecord metadata barely changes.
    """

    @abc.abstractmethod
    async def get_state(self, account_id: str) -> AccountRuntimeState: ...

    @abc.abstractmethod
    async def increment_stat(
        self, account_id: str, field: str, by: int = 1
    ) -> None:
        """Atomic increment for total_requests / successful_requests / failed_requests /
        failures. HINCRBY on Redis; UPSERT on Postgres file backend."""

    @abc.abstractmethod
    async def set_failure(
        self, account_id: str, failures: int, at: float
    ) -> None:
        """After circuit-breaker decides a state transition."""

    @abc.abstractmethod
    async def clear_failure(self, account_id: str) -> None: ...

    @abc.abstractmethod
    async def set_health(
        self, account_id: str, status: str, at: float
    ) -> None: ...

    @abc.abstractmethod
    async def set_quota(
        self, account_id: str, current: float, limit: float, at: float
    ) -> None: ...

    @abc.abstractmethod
    async def next_round_robin_index(self) -> int:
        """Return the next global round-robin index. On Redis this is an
        atomic INCR + modulo — that's what fixes the "two processes hand out
        the same account" bug."""
```

对应的 `AccountRuntimeState` 值对象只装数据,不装行为:

```python
@dataclass(frozen=True)
class AccountRuntimeState:
    failures: int = 0
    last_failure_time: float = 0.0
    current_usage: Optional[float] = None
    usage_limit: Optional[float] = None
    quota_updated_at: float = 0.0
    last_health_check_at: float = 0.0
    last_health_status: Optional[str] = None
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
```

---

## 5. TokenStore 契约要收紧

Phase 1.2a 里 `TokenStore.refresh_lock` 已经定义了正确的语义,不改。但要把
**读旧 token → 决定是否真的还需要刷 → 调 Kiro → 写新 token** 这一整段做进
`TokenStore` 里,而不是把每一步暴露给 `AccountManager` / `KiroAuthManager`,
避免调用方少走一步就变成 refresh race。

新增一个高层方法:

```python
class TokenStore(abc.ABC):
    ...
    @abc.abstractmethod
    async def get_or_refresh(
        self,
        account_id: str,
        refresh_fn: Callable[[KiroToken], Awaitable[KiroToken]],
        expiring_soon_threshold: float = 600,
    ) -> KiroToken:
        """Return a fresh access token, refreshing if within
        ``expiring_soon_threshold`` seconds of expiry.

        Correctness contract:
          1. Read current token from persistent storage (NOT process cache).
          2. If not expiring soon → return it.
          3. Else acquire ``refresh_lock(account_id)``.
             a. Got it → re-read (peer may have refreshed while we waited),
                if now fresh → return it. Otherwise call refresh_fn(old),
                save the result, return it.
             b. Didn't get it → sleep briefly, loop back to step 1. Give up
                after N tries and raise.
        """
```

`KiroAuthManager.get_access_token()` 就变成 20 行:构造 `refresh_fn` 闭包(封
装 Kiro Desktop / AWS SSO OIDC 的差异),交给 `store.get_or_refresh` 处理。

**这个方法是 Phase 1 全部 refactor 的价值所在**。它把"避免烧 refresh token"
的复杂性从 8 个 caller 收拢到 1 个实现里,谁也不用记得先加锁再重读。

---

## 6. 拆分 Phase 1.3 的执行子步骤

| 步骤 | 说明 | 预估 |
|---|---|---|
| 1.3.1 | 在 `interfaces.py` 加 `AccountRuntimeStore` + `AccountRuntimeState`,`TokenStore` 加 `get_or_refresh` | 0.5 天 |
| 1.3.2 | 抽 `Account` dataclass 成 `AccountMetadata`(纯元数据)+ 运行时数据不再放 dataclass 里,靠 `runtime.get_state(id)` 现查 | 0.5 天 |
| 1.3.3 | 把 `AccountManager` 里读/写 credentials.json + state.json 的 200+ 行删掉,改成调 stores。删掉 `_dirty` + `save_state_periodically`。 | 2-3 天 |
| 1.3.4 | 把 `KiroAuthManager.get_access_token()` 里的加锁 + 读 token + 判断过期 + 调 refresh + 写文件这一大段 collapse 成 `store.get_or_refresh(id, self._refresh_fn)` | 1-2 天 |
| 1.3.5 | 单元测试:两个 concurrent get_or_refresh 只调一次 upstream;持锁进程崩溃后另一个能接管;concurrent report_failure 的计数一致 | 1 天 |

**总计**:约 5-7 人日。**这是 Phase 1 里最重的一部分**,做完之后 Phase 1.4
(接 Redis + Postgres)就是纯 infra 活了。

---

## 7. 灰度切换策略(为 Phase 1.4 提前想好)

新的 `AccountManager` 依赖 3 个 store。File 后端里,这 3 个 store 内部还是读
写文件(跟现在等价),Redis 后端里就是 Redis。切换方式:

```
STORAGE_BACKEND=file             # 现状,零变化
STORAGE_BACKEND=redis+postgres   # 目标态

# Phase 1.6 时,可以用一个专门的开关做 shadow read:
STORAGE_BACKEND=file,shadow-read=redis+postgres
```

`shadow-read` 让每次读同时查 file 和 Redis,对比不一致时告警,但用 file 的值
决策。跑几天后再切 primary。

---

## 8. Phase 1.2b-3 / 1.2b-4 目前的处理

- **1.2b-3 AccountRegistry**: **占位**,`file_backend.py` 里保持 `NotImplementedError`。
- **1.2b-4 TokenStore**: **占位**,同上。

Phase 1.2 就此结束(1.2a + 两个已完成的 wrap)。真正的重构在 Phase 1.3。

这个决策的核心理由:**做了 wrap 也无价值** —— wrap 出来的接口在 file 后端下就
是"多绕一圈到原来的代码",等 Phase 1.3 重构时又要重新拆一遍。不如省下这一趟。

---

## 9. 待你拍板的关键决策(承 Phase 1.1 §8)

之前列的 4 个还没定:

1. **Postgres 复用 ai-console 那个还是新建?** — 这个 Phase 1.4 前必须定。倾向:复用,建 `kiro_gateway` schema 隔离。
2. **Redis 单节点还是主从?** — 单节点可上;monitoring + 主从升级留到后面。
3. **`credentials.json` 迁 Postgres 后展平 refresh_token?** — 强烈推荐展平,消除 file indirection。
4. **`debug_logs` 改 stdout?** — 推迟到 Phase 2(不影响 Phase 1)。

外加 Phase 1.3 引入的新决策:

5. **`AccountRuntimeStore` 是独立组件还是折进 `AccountRegistry`?** — 我的推荐是独立,理由见 §2。但如果你觉得组件太多,合并也可以,代价是接口方法数从 3 变成 8。

---

## 10. 附:哪些代码在 Phase 1.3 会有实质变更

- `kiro/account_manager.py` — 从 1200+ 行降到约 400 行(主要是删)。
- `kiro/auth.py` — 从 1000+ 行降到约 500 行(把并发/重试/持久化逻辑挪到 storage)。
- `main.py::lifespan` — 减少显式 tracker/logger/state 初始化,统一调
  `storage.start()` / `storage.close()`。
- `kiro/storage/interfaces.py` — 加 `AccountRuntimeStore` + `TokenStore.get_or_refresh`。
- `kiro/storage/file_backend.py` — 真实装现在占位的 Registry/TokenStore/Runtime。
- **tests/** — 补 3-5 个新的并发场景测试。

调用方(routes_openai / routes_anthropic / routes_admin)基本不改。

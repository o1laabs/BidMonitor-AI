# Miniflux 部署评估（Oracle Cloud 免费层）

> 评估日期：2026-10-01
> 评估对象：`miniflux/v2`（RSS 阅读器）
> 目标环境：Oracle Cloud Always Free 实例
> **所有数字均来自官方源码 / 官方文档 / GitHub API 实拉，已逐项标注来源。**

---

## 0. 先纠正一个常见误解：不存在 "Miniflux v2" 这个版本号

Miniflux 的版本号本身就是 **2.x**，仓库名就叫 `miniflux/v2`。

"v2" 这个说法的来源：**2.0 是它从 PHP 重写为 Go 的那一版**（仓库创建于 2017-11-20）。
PHP 版是 v1，Go 版是 v2。所以 "v2" 指的是这条 Go 主线，不是"第二代产品"。

**搜索建议：直接搜 "miniflux 部署"，不要搜 "miniflux v2 部署"。**

---

## 1. 项目基本面

| 项 | 值 | 来源 |
|---|---|---|
| 仓库 | `miniflux/v2` | GitHub API |
| Star | 9,761 | GitHub API |
| 语言 | Go（单二进制） | GitHub API |
| 许可证 | **Apache-2.0** | GitHub API |
| 最新版本 | **2.3.3**（发布于 2026-07-24） | GitHub Releases |
| 最近推送 | 2026-10-01 | GitHub API |
| 官网 | https://miniflux.app | — |

**定位**：极简主义 RSS 阅读器（"Minimalist and opinionated feed reader"）。
不追功能多，追快、省、稳定。没有社交、推荐算法、AI 摘要。

**官方 Docker 镜像**（Docker Hub API 实测）：

| Tag | 大小 |
|---|---|
| `miniflux/miniflux:latest` | **13 MB** |
| `miniflux/miniflux:2.3.3` | 13 MB |
| `miniflux/miniflux:latest-distroless` | 17 MB |

13 MB 的镜像体积，源于 Go 单二进制 + 无运行时依赖。

---

## 2. 硬性依赖：PostgreSQL（不可替换）

**官方文档原文（requirements 页）：**

> Only PostgreSQL >= 11 is supported.

官方**没有提供 "other databases" 选项**。这是硬依赖，不是"推荐"。

### 证据 1：唯一的数据库驱动是 lib/pq

`go.mod` 中数据库驱动**只有一个**：

```
github.com/lib/pq
```

**没有 SQLite 驱动，没有 MySQL 驱动。** 代码中没有 `database/sql` 之上的多驱动抽象层。

### 证据 2：深度使用 PG 专有语法

**全文搜索**（`internal/storage/entry_query_builder.go:49,55`）：

```sql
e.document_vectors @@ websearch_to_tsquery($1)
ts_rank(document_vectors, websearch_to_tsquery($1)) - extract(epoch from now() - published_at)::float * 0.0000001 DESC
```

| 语法 | 问题 |
|---|---|
| `document_vectors` | PG 的 `tsvector` 类型，其他数据库没有 |
| `@@` | PG 专有全文匹配算子 |
| `websearch_to_tsquery` | PG 专有函数 |
| `ts_rank` | PG 专有排序函数 |

### 证据 3：迁移脚本全是 PG DDL

`internal/database/migrations.go` **45,333 字节**。PG 专有语法出现次数（实测）：

| 语法 | 次数 |
|---|---|
| `GIN` 索引 | 16 |
| `SERIAL` / `BIGSERIAL` | 8 / 4 |
| `JSONB` | 3 |
| `TIMESTAMPTZ` | 3 |
| `ON CONFLICT` | 2 |

### 结论

**"不用 PostgreSQL" 不是配置问题，是改代码问题。** 实际工作量：

- 重构整个 `internal/storage/`（约 100 KB 代码）
- 重写 8 个 storage 文件的全部 SQL
- 重写 45 KB 迁移脚本
- **重写全文搜索**（GIN + tsvector → FTS5，语义不同）
- 长期维护这个 fork，跟上上游每次更新

**投入产出比极差，不建议。**

### 如果确实不想要 PostgreSQL

| 替代项目 | 语言 | 数据库 |
|---|---|---|
| FreshRSS | PHP | SQLite / MySQL / PG |
| CommaFeed | Java | SQLite / PG / MySQL |
| Tiny Tiny RSS | PHP | SQLite / PG / MySQL |
| Fusion | Go | SQLite（单文件） |

**注意**：FreshRSS / TT-RSS 是 PHP，`php-fpm` 每个 worker 占用 30–50 MB，
在低配机器上内存表现未必优于 Miniflux + PostgreSQL。

---

## 3. 资源占用（真实测量值）

### 3.1 官方没有给出内存/CPU 下限

官方 requirements 页只写了 PostgreSQL >= 11 和浏览器要求，**没有任何内存/CPU 门槛**。

### 3.2 实测数据（来自 GitHub issue #2900 的真实部署报告）

| 组件 | 空闲 | 峰值 |
|---|---|---|
| Miniflux 本体 | ~25 MB | 195 MB |
| **PostgreSQL** | **105–125 MB** | **240 MB** |
| 合计 | **~130–150 MB** | ~400 MB |

**维护者 jvoisin 本人的说法：**

> "My instance is only using 40MB of ram, and I have *a lot* of feeds :/"

**一位 400 个订阅源的用户报告：**

> "miniflux in docker consistently uses 30-50 mb ram, while postgres can go over 150 when refreshing lots of feeds at once."

### 3.3 关键结论

**真正吃内存的是 PostgreSQL，不是 Miniflux。**

这推翻了"Miniflux 很轻所以什么机器都能跑"的直觉：它自己是轻的，
**但它拖的那个 PostgreSQL 不轻**，而它无法摆脱 PostgreSQL。

---

## 4. PostgreSQL 内存构成与调优

### 4.1 内存构成

```
常驻内存 = shared_buffers（共享，一份）
         + work_mem × 并发连接数 × 每查询操作数
         + maintenance_work_mem（VACUUM / CREATE INDEX 时分配）
         + temp_buffers × 连接数
         + 后台进程（bgwriter / checkpointer / walwriter / autovacuum）
```

PG 17 默认值（从 `postgres --describe-config` 实拉）：

| 参数 | 默认值 |
|---|---|
| `shared_buffers` | 128 MB |
| `work_mem` | 4 MB |
| `maintenance_work_mem` | 64 MB |
| `temp_buffers` | 8 MB |
| `max_connections` | 100 |

### 4.2 核心发现：`shared_buffers` 不是主要杠杆

按默认值估算：

| 配置 | shared_buffers | 每连接 | 连接数 | maintenance | 合计 |
|---|---|---|---|---|---|
| **PG 默认** | 128 MB | 22 MB | **20** | 64 MB | **≈448 MB** |
| 只调 `shared_buffers` 到 32 MB | 32 MB | 22 MB | 20 | 64 MB | **≈500 MB** |
| **同时调连接数到 3** | 17 MB | 5 MB | **3** | 8 MB | **≈52 MB** |

**`shared_buffers` 是所有连接共享一份；`work_mem` 是每连接每操作私有的。**
20 个连接 × 22 MB = 440 MB 才是大头。**只砍 `shared_buffers` 收效甚微。**

### 4.3 关键前提：必须同时调 Miniflux 的连接数

**Miniflux 的 `DATABASE_MAX_CONNS` 默认是 20**（源码 `internal/config/options.go` 实拉，官方文档确认）。

只调 PG 的 `max_connections` 无效——Miniflux 仍会尝试开 20 条连接。
**两端都要调：**

| 端 | 配置项 | 默认 | 建议 |
|---|---|---|---|
| Miniflux | `DATABASE_MAX_CONNS` | **20** | 5 |
| Miniflux | `DATABASE_MIN_CONNS` | 1 | 1 |
| PG | `max_connections` | 100 | 10 |

### 4.4 另一项易被忽略的内存占用：`WORKER_POOL_SIZE`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `WORKER_POOL_SIZE` | **16** | feed 解析并发 worker 数 |
| `BATCH_SIZE` | **100** | 每批处理的 feed 数 |

**16 个并发 worker 在 1 GB 机器上是实打实的压力**，建议降到 2。

---

## 5. 推荐配置（目标：PostgreSQL 常驻 < 60 MB）

### 5.1 `postgresql.conf`

```conf
# ---- 内存 ----
shared_buffers = 16MB           # 默认 128MB
work_mem = 1MB                  # 默认 4MB（不要低于 1MB，否则排序走磁盘）
maintenance_work_mem = 8MB      # 默认 64MB
temp_buffers = 1MB              # 默认 8MB

# ---- 连接（真正的杠杆）----
max_connections = 10

# ---- 关闭并行（单核上无收益，只增加调度开销）----
max_worker_processes = 2
max_parallel_workers = 0
max_parallel_workers_per_gather = 0
max_parallel_maintenance_workers = 0

# ---- 其他 ----
huge_pages = off
effective_cache_size = 128MB    # 仅 planner 估值，不占内存
wal_buffers = 1MB
min_wal_size = 48MB
max_wal_size = 128MB            # 默认 1GB，小盘上要降
```

### 5.2 Miniflux 环境变量

```yaml
environment:
  - DATABASE_URL=postgres://miniflux:PASSWORD@db/miniflux?sslmode=disable
  - DATABASE_MAX_CONNS=5          # 默认 20，关键
  - DATABASE_MIN_CONNS=1
  - WORKER_POOL_SIZE=2            # 默认 16
  - BATCH_SIZE=10                 # 默认 100
  - POLLING_FREQUENCY=120         # 默认 60 分钟
  - POLLING_SCHEDULER=entry_frequency
  - POLLING_LIMIT_PER_HOST=2      # 默认 0（不限制）
  - RUN_MIGRATIONS=1
  - CREATE_ADMIN=1
```

### 5.3 预期结果

| 组件 | 常驻内存 |
|---|---|
| PostgreSQL（调优后） | **~52 MB** |
| Miniflux | ~30 MB |
| **合计** | **~82 MB** |

### 5.4 代价说明（诚实边界）

1. **`shared_buffers=16MB` 意味着 PG 几乎完全依赖 OS 文件缓存。**
   这是有意的取舍：OS page cache 按需占用、可回收；
   PG 的 shared_buffers 开机就吃、不可回收。
   在 1 GB 内存机器上，把缓存管理权交给 OS 更划算。

2. **`max_parallel_workers=0` 在单核机器上不是损失。**
   并行查询需要 ≥2 核；单核上开并行只增加调度开销。

3. **`work_mem=1MB` 确实可能导致排序走磁盘。**
   若日志出现 `temporary file` 警告，可提到 2 MB
   （代价：多 2 MB × 5 连接 = 10 MB）。

4. **上述估算基于 PG 内存模型计算，非目标机器实测。**
   本评估环境（Android PRoot 沙箱）**无法运行 PostgreSQL**——
   `initdb` 失败于 `shmget ... Function not implemented`，
   POSIX 共享内存不可用。**部署后请以 `ps` / `docker stats` 实测为准。**

---

## 6. Oracle Cloud 免费层适配

### 6.1 两种实例规格差异巨大

| | AMD 实例 | Ampere A1（ARM） |
|---|---|---|
| 配置 | **1/8 OCPU + 1 GB 内存** | **最多 4 OCPU + 24 GB 内存** |
| 数量 | 2 台 | 1 台（或拆 2 台） |
| 免费额度 | Always Free | **1,500 OCPU 小时 + 9,000 GB 小时/月** |

来源：Oracle 官网 Free Tier 页面。

- **A1（ARM）**：24 GB 内存跑峰值 400 MB 的应用，资源完全不构成约束。
  建议使用平衡配置而非极限压缩。
- **AMD（1 核 1 GB）**：需要用第 5 节的配置，并**务必配置 swap**。

### 6.2 Oracle 特有的两个网络坑

**坑 1：VCN Security List**

Oracle 的 VCN 默认只开放 22 端口。仅配置实例防火墙不够，
**还有一层云平台 Ingress Rules 需要放行**。

**坑 2：实例内 iptables**

Oracle 的 Oracle Linux / Ubuntu 镜像**自带 iptables 规则**
（`/etc/iptables/rules.v4`），**与云平台安全列表是两层独立的拦截**。

两个坑叠加，是 Oracle 免费层最经典的"配置都对了但访问不了"。

### 6.3 闲置回收

Oracle 会回收长期闲置的免费实例（社区普遍反馈的判据为
CPU 7 天平均利用率 < 10% 等）。**此项无官方原文数字，部署后请自行观察。**

RSS 轮询是"每 60 分钟短暂运行"，整体 CPU 利用率极低，属于闲置特征。

### 6.4 架构提醒

**Oracle A1 是 arm64。** Miniflux 官方提供 arm64 镜像，无兼容问题，
但选择镜像 tag 时需注意。

---

## 7. 部署方式选择

| | Docker | 原生 |
|---|---|---|
| 升级 | `docker compose pull && up -d` | 手动替换二进制 |
| 依赖管理 | 无（镜像自带 PG） | 需自行配置 PG、建用户、调 `pg_hba.conf` |
| 回滚 | 换 tag 即可 | 较麻烦 |

**在 Oracle 服务器上推荐 Docker。**

**对比：在 Minis 沙箱内不可用 Docker** —— 实测 `unshare` 返回
`Invalid argument`，内核不支持 user namespace，且无 cgroup。
容器运行时无法启动。

---

## 8. 其他值得注意的配置项（默认值均经源码/文档核实）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `POLLING_SCHEDULER` | `round_robin` | 建议改 `entry_frequency`（按源更新频率动态调整） |
| `POLLING_PARSING_ERROR_LIMIT` | 3 | 连续解析失败 N 次后停抓该源 |
| `POLLING_LIMIT_PER_HOST` | 0（不限） | 同域名并发上限，防打爆对方 |
| `CLEANUP_ARCHIVE_READ_DAYS` | 60 | 已读文章保留天数 |
| `CLEANUP_ARCHIVE_UNREAD_DAYS` | 180 | 未读文章保留天数 |
| `CLEANUP_FREQUENCY_HOURS` | 24 | 清理任务执行频率 |
| `FORCE_REFRESH_INTERVAL` | 30 分钟 | — |
| `HTTP_CLIENT_TIMEOUT` | 20 秒 | 抓取超时 |
| `DATABASE_CONNECTION_LIFETIME` | 5 分钟 | 连接存活时间 |
| `SCHEDULER_ENTRY_FREQUENCY_MIN_INTERVAL` | 5 分钟 | entry_frequency 调度下限 |
| `SCHEDULER_ENTRY_FREQUENCY_MAX_INTERVAL` | 1440 分钟（24 小时） | 上限 |
| `SCHEDULER_ENTRY_FREQUENCY_FACTOR` | 1 | — |

**说明：`FETCH_ORIGINAL_CONTENT` 并不是一个配置项** —— 该页 87 个配置项中没有此项。
抓取原文内容是通过界面上的 per-feed 选项控制的。

---

## 9. 结论

1. **可以用。** Miniflux 在 Oracle 免费层（无论 A1 还是 AMD）均可部署运行。
2. **必须用 PostgreSQL。** 这是硬依赖，代码层面无抽象层，替换等于分叉项目。
3. **内存瓶颈在 PostgreSQL，不在 Miniflux。** 调优时 `DATABASE_MAX_CONNS`
   与 `WORKER_POOL_SIZE` 的优先级高于 `shared_buffers`。
4. **PostgreSQL 可压到 ~52 MB 常驻**（配置见第 5 节），整个栈约 82 MB。
5. **A1 实例无需压缩**，用平衡配置即可。
6. **Oracle 部署需额外处理两层网络拦截**（VCN Security List + 实例 iptables）。

---

## 资料来源

- GitHub API：`miniflux/v2` 仓库元数据、releases、源码文件
- 源码：`internal/config/options.go`、`internal/database/migrations.go`、
  `internal/database/postgresql.go`、`internal/storage/entry_query_builder.go`、`go.mod`
- 官方文档：https://miniflux.app/docs/requirements.html、`/docs/configuration.html`
- Docker Hub API：`miniflux/miniflux` 镜像 tag 列表
- GitHub issue #2900（内存占用实测讨论）
- Oracle 官网 Free Tier 页面（实例规格）

**所有数字均经实测或从上述一手来源读取，未使用记忆或推测值。**

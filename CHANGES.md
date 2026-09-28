# 本 fork 的改动

## 为什么改

上游 `src/ai_guard.py` 硬编码了一个第三方 API 中转端点
（`https://cc.honoursoft.cn`），并把默认模型设为
`claude-sonnet-4-5-20250929-thinking`。

这带来两个问题：

1. **数据流向不透明** —— 判断 prompt 里包含使用者的公司业务描述
   和招标项目标题，全部会发往该域名。
2. **该端点的协议实现从未生效** —— 上游用 `'honoursoft' in base_url`
   判断是否走「Claude 原生协议」来构造请求体，但请求头始终是
   `Authorization: Bearer`（OpenAI 风格），响应也始终按
   `choices[0].message.content` 解析。也就是说那个分支即使命中该域名，
   构造出的 Anthropic 格式请求体也无法被正确解析 —— 是死代码。

## 改了什么

文件：`src/ai_guard.py`

| # | 改动 | 解决的问题 |
|---|---|---|
| 1 | `base_url` 改为配置项 + 环境变量，默认值为中立的通用占位符 | 不再默认把数据发给第三方 |
| 2 | `api_key` 支持环境变量（`BIDMONITOR_AI_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`） | 避免密钥明文入库 |
| 3 | 协议由显式的 `api_format` 决定，不再靠域名猜测 | 消除死代码；域名与协议解耦 |
| 4 | 补齐 **Anthropic 原生协议**支持：`x-api-key` 头、`anthropic-version` 头、`/v1/messages` 路径、`system` 顶层参数、`content[].text` 响应解析 | 原实现的 Claude 分支从未真正可用 |
| 5 | 新增 `fail_open` 配置（默认 `true`，保持上游行为） | 失败时放行/拦下由使用者显式决定，而非写死 |
| 6 | 4xx 不再重试（配置错误重试无意义），仅网络异常重试 | 避免无效等待 |
| 7 | 空响应给出明确提示（reasoning 模型 `max_tokens` 过小时 content 为空） | 原来会静默解析失败 |
| 8 | 新增 `get_stats()`，统计 AI 实际决策比例 | 可判断 AI 层到底生效了几成 |

## 兼容性

- **配置文件格式不变**，旧配置仍可加载。
- **默认行为不变**：`enable` 默认 `false`，`fail_open` 默认 `true`。
- 原来依赖 `base_url` 硬编码实现的用法，需要补一项 `base_url` 配置。

## v1.2.0 新增：快照表 + 内容感知去重

文件：`src/database/storage.py`、`src/monitor_core.py`

### 背景：上游的两个形态性缺陷

1. **只存 URL 不存原文** —— 招标公告经常被修改或下架，只留一个链接，
   事后无法证明"当时的公告是这么写的"。
2. **`md5(url)` 去重会跳过更正公告** —— `BidInfo.unique_id` 仅由 URL 生成，
   于是「同一 URL、内容已更新」的更正/延期公告会被判定为"已存在"而静默跳过。
   这在招投标场景是致命的：**延期公告往往比原公告更关键。**

### 改动

**新增 `bid_snapshots` 表**（每次抓取都留痕，构成时间序列）：

| 字段 | 用途 |
|---|---|
| `url_id` | URL 维度的标识，把同一公告的各版本串起来 |
| `content` / `content_hash` | 正文及其归一化哈希 |
| `raw_html` | 原始 HTML（可选，用于留证） |
| `http_status` | 抓取时的响应状态 |
| `fetched_at` | 抓取时间 |
| `fetch_reason` | `new` / `changed` / `unchanged` |

**去重逻辑升级为内容感知**：

- `content_hash()` 做**空白归一化**后再哈希 —— 排版变化不会被误判为内容更新。
- `BidInfo.unique_id`：`content` 为空时退化为 `md5(url)`（**与旧库行为完全一致**）；
  非空时为 `md5(url + content_hash)`。
- **新增 `save_or_update()`**，返回三种状态：

  | 返回 | 含义 | 处理 |
  |---|---|---|
  | `"new"` | 首次出现 | 入库 + 通知 |
  | `"changed"` | 同 URL、内容已变 | **作为新版本入库 + 通知** |
  | `"unchanged"` | 同 URL、内容未变 | 只记快照，不打扰 |

**兼容性**：新增 `url_id` 列，旧库启动时自动 `ALTER TABLE` 迁移并回填
（历史记录的 `url_id` 取原 `unique_id`）。所有原有方法签名不变。

## 验证

改动经端到端测试（Mock 双协议服务端 + 真实 HTTP 往返）：

- OpenAI 协议：请求 `POST /v1/chat/completions`，头 `Authorization: Bearer`，
  `system` 位于 messages 首位 → 正确解析 `choices[0].message.content` ✅
- Anthropic 协议：请求 `POST /v1/messages`，头 `x-api-key` +
  `anthropic-version: 2023-06-01`，`system` 为顶层参数 →
  正确解析 `content[].text` ✅
- 401 响应：1 次请求即返回，不做无效重试 ✅
- `fail_open` 两种取值均按预期放行/拦下 ✅

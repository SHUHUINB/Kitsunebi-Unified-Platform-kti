# Kitsunebi 统一平台 · Kitsunebi Unified Platform

> **一套把「抓包 / 改包 / 代理授权 / 卡密」四件事收进同一个入口的自托管平台。**
> Charles Proxy 抓包 + CCProxy 适配层改写 + 一花卡密 + FastAPI/Vue3 统一管理台，
> 外加一个 **MCP 入口**，让 AI agent 能直接读包、改包、重放、导出。

[English introduction → `README.en.md`](README.en.md)

---

## 1. 这是什么

自建一套「手机 / 终端 → 代理 → 目标服务」的完整链路，并把**观察与干预**都做成可控的：

- **看得到**：所有经过代理的 TCP / TLS / HTTP / UDP 流量都被记录进环形缓冲，可筛选、可看完整 hex、可看分层结构。
- **改得动**：断点（Hold）挂住单个包，人工放行 / 丢弃 / 改写；或挂自动改写规则（过滤器改写）批量替换字节。
- **发得出**：重放历史包，或从零构造一个新包直接发出去。
- **管得住**：卡密（授权码）的发放、续期、服务器绑定、账号管理，数据层重写进了平台，不再依赖 PHP。
- **给得出**：`/mcp` 把上面这些能力暴露成 17 个 MCP 工具，AI agent 可以像人一样操作同一套链路 —— **走的是同一条代码路径，不是另做一套**。

一句话：**四套系统，一个入口，一条链路。**

---

## 2. 四个组成部分

| 组件 | 作用 | 端口 | systemd 单元 / 容器 |
|---|---|---|---|
| **花瓶 Charles Proxy** | 抓包 / 证书 / 上游代理，4.5.6 headless（自带 JRE，自包含） | `18888` HTTP · `18889` SOCKS5 | `charles-headless.service` |
| **CCProxy 适配层** | 转发路径上的抓包点 + WPE 引擎（记录 / 改写 / 断点 / 重放 / 导出） | `8888` HTTP · `8889` SOCKS5 · `8893` 管理 API · `40000-40063` UDP 中继 | `ccpx.service` + `ccpx-capture.service` |
| **一花卡密（Kitsunebi 卡密）** | 卡密 / 应用 / 服务器 / 账号体系（数据层已重写进平台） | 历史 `127.0.0.1:8081`（PHP，可选） | docker `yihua-app` + `yihua-db` |
| **统一平台（本仓库主体）** | 管理台 + API + MCP，把上面三者收成一个入口 | `8890`（含 `/mcp`） | `rewrite.service` |

> **抓包点在哪**：在**适配层的转发路径**上（TCP CONNECT 隧道、明文 HTTP、UDP SOCKS5 中继）。
> **不注入任何目标进程** —— 不需要 root 权限去读写别的进程内存，也不依赖被观测应用配合。

---

## 3. 架构

```
                    ┌─────────────────────────────────────────────┐
   终端 / 手机  ───▶│  CCProxy 适配层  8888 HTTP / 8889 SOCKS5    │
   (Kitsunebi /     │  ├─ 转发路径上的抓包点 → 环形缓冲(4000)      │
    浏览器 / 脚本)   │  ├─ 断点 Hold / 自动改写 / 重放 / 构造发送   │
                    │  └─ UDP 中继 40000-40063（含孤儿包自愈）     │
                    └───────────────┬─────────────────────────────┘
                                    │ 上游链式转发
                                    ▼
                    ┌─────────────────────────────────────────────┐
                    │  花瓶 Charles Proxy   18888 / 18889         │
                    │  （根证书签发 · 上游代理 · 会话归档）        │
                    └───────────────┬─────────────────────────────┘
                                    ▼
                              目标服务

                    ┌─────────────────────────────────────────────┐
   浏览器 / agent ──▶│  统一平台  8890                             │
                    │  FastAPI  /api/v1/{auth,overview,charles,    │
                    │           ccpx,wpe,kami}                    │
                    │  MCP      /mcp  （17 个工具，JSON-RPC 2.0）  │
                    │  设备侧   /ok /socks /ruleset.conf /ca.crt  │
                    │           （无登录，注册在 SPA 通配之前）     │
                    │  Vue3 SPA （同源托管）                       │
                    └──────────┬──────────────────┬───────────────┘
                               │ 管理 API         │ 数据层
                               ▼                  ▼
                     适配层 8893 / Charles webif   SQLite（卡密 / 账号 / 会话）
```

三条不可动摇的约束（写在代码里，不是写在文档里）：

1. **读接口不改状态** —— 看列表、看状态永远不会顺手把什么东西改掉。
2. **破坏性操作有安全网** —— 清空缓冲前先落盘，安全网没就位就中止。
3. **不粉饰** —— 上游非 200、参数缺失、结果被截断，全部如实上报；**绝不返回一个「看起来正常」的空结果**。

---

## 4. 统一平台

### 4.1 后端（FastAPI）

- 8 个业务域路由，共 **88 条路由**，外加 `/healthz`、`/_frontend`、SPA 回退：

  | 前缀 | 条数 | 说明 |
  |---|---|---|
  | `/api/v1/auth` | 5 | 登录 / 登出 / 当前用户 / 改密 |
  | `/api/v1/overview` | 1 | 总览聚合（四套系统健康度） |
  | `/api/v1/charles` | 14 | 花瓶状态 / 配置读写 / 证书 / 授权 |
  | `/api/v1/ccpx` | 8 | 适配层状态 / 规则 / 日志 / 重启 |
  | `/api/v1/wpe` | 16 | 封包列表 / 详情 / 改写 / 断点 / 重放 / 导出导入 |
  | `/api/v1/kami` | 25 | 卡密 / 应用 / 服务器 / 账号 / 兑换 |
  | `/mcp` | 3 | MCP 入口（POST / GET-SSE / DELETE） |
  | `device`（空前缀） | 16 | 设备侧无登录资源，见 §4.3 |

- **分层清晰**：`config`（环境）→ `security`（口令/JWT）→ `db` / `models` / `schemas` → `upstream`（对适配层与 Charles 的客户端）→ `deps`（鉴权收口）→ `routers`。
- **不重复实现语义**：平台不猜、不补、不美化上游结果；所有 WPE 操作都翻译成一条适配层请求转发出去。

### 4.2 前端（Vue3 + Vite）

- 依赖只有 `vue` / `vue-router`，状态管理用 `reactive` 手写，不引入 Pinia。
- 6 个页面：`Login` / `Overview` / `Wpe` / `Ccpx` / `Charles` / `Kami`。
- WPE 页面拆成 7 个组件：`PacketTable` / `PacketDetail` / `HexDump` / `HoldPanel` / `RewritePanel` / `ExportPanel` / `SendPanel`。
- 品牌名集中在 `src/brand.js` 一个常量，改名字只改一处。

### 4.3 设备侧（无登录）入口

客户端（手机上的 Kitsunebi、脚本、浏览器）需要的资源，**故意不要求登录** —— 因为客户端没有会话：

```
GET /ok                       健康探针
GET /socks                    返回 SOCKS5 配置片段
GET /ruleset  /ruleset.conf   分流规则集
GET /ca.crt /ca.cer /ca.pem   根证书（PEM / DER）
GET /charles-ca.{crt,cer,pem} 花瓶根证书
GET /ca.mobileconfig          iOS 描述文件
GET /ca.qr.svg                证书下载二维码
GET /portal                   用户门户
```

两条容易踩的坑，已经处理并写在代码注释里：

- 这些路由**必须注册在 SPA 通配之前**，否则会被兜成 `index.html` —— 客户端拿到一段 HTML 当证书，报错莫名其妙。
- **来源 IP 只读 socket 对端，绝不读 `X-Forwarded-For`** —— 否则任何人都能伪造来源。

---

## 5. WPE 封包编辑器

### 5.1 能力

| 能力 | 说明 |
|---|---|
| 列表 | 方向 / 协议 / 目标 / HEX 子串 / 应用层 / 长度区间 / 只看断点队列；`since` 看更新、`before` 看更旧 |
| 详情 | 元数据 + **完整 hex** + ascii + 是否截断 + 人工改写 / 规则改写的处置标记 |
| 结构解析 | HTTP / TLS / DNS / SOCKS5 / MQTT 的字段级 `off` / `len`，结构面板据此高亮 |
| 断点（Hold） | 挂住命中的包，人工 `forward` / `drop` / `modify` |
| 自动改写 | 规则表整表覆盖；`find_replace` 字节替换 或 `set_hex` 整包替换；返回 `rejected` 逐条给出被拒原因 |
| 重放 / 构造发送 | 重放历史包（可换载荷 / 目标 / 协议 / 次数），或从零构造 TCP / UDP 包 |
| 导出 / 导入 | 内存分页导出，或落盘成 `json` / `txt` 不受响应体积限制；导入的包一律打 `imported` 标记 |
| 清空 | 只清缓冲，**不动**改写规则与断点设置 |

### 5.2 几条必须知道的约束

- **列表里的 hex 是 512 字符预览**（`hex_full_len` 是字符数，不是字节数）。要完整 hex 走详情接口。
- **单次列表上限 2000**（`min(limit, 2000)`）。要取更旧的一页，把返回里的 `oldest_id` 当 `before` 再调一次；`has_more=false` 表示已到最旧。
- **环形缓冲上限 4000 条**，超出后最旧的被覆盖。
- **断点队列的唯一可信来源是「只看断点」这个筛选** —— 适配层不在封包上打 `hold` 标记，别指望在普通列表里靠一个字段判断。
- **无条件改写会被拒收**。规则至少填一个条件（`dir` / `proto` / `target` / `hex` / `app` / `min_len` / `max_len`）—— 无条件改写会改掉每一个包。

---

## 6. MCP 入口（`/mcp`）

把 WPE 的全部能力做成 **17 个 MCP 工具**，协议 JSON-RPC 2.0，`protocolVersion` 支持 `2025-06-18 / 2025-03-26 / 2024-11-05`。

| 工具 | 作用 |
|---|---|
| `wpe_stat` | 总览：捕获数 / 缓冲占用 / 断点状态 / 改写命中 / UDP 中继账本 |
| `wpe_list` | 条件筛选 + `since` / `before` 双游标翻页 |
| `wpe_get` | 单个包的完整 hex + 分层结构 |
| `wpe_struct` | 只看分层结构 |
| `wpe_rewrite_get` / `wpe_rewrite_set` | 读 / 写自动改写规则 |
| `wpe_hold_get` / `wpe_hold_set` | 读 / 写断点状态与规则 |
| `wpe_release` | 放行 / 丢弃 / 改写一个被断住的包 |
| `wpe_replay` | 重放历史包 |
| `wpe_send` | 从零构造并发送 |
| `wpe_export` | 内存导出（带续导游标） |
| `wpe_export_save` / `wpe_export_list` / `wpe_export_download` | 落盘导出 / 列出历史 / 取回内容 |
| `wpe_import` / `wpe_clear` | 导入 / 清空缓冲 |

设计上的三个决定：

- **`/mcp` 不鉴权**（MCP 客户端没法先登录），但它吐出的**大文件下载地址自带一次性令牌**：与登录同密钥签发，绑死文件名 + `scope` + 30 分钟 TTL。换个文件名就失效。
- 工具执行失败走 `result.isError = true`，**不是 JSON-RPC error** —— 后者是协议层错误，客户端会当成连接坏了。
- 结果超过单次上限时**如实标 `truncated`** 并给出下一步（落盘导出 / 翻页），而不是悄悄截断装作完整。

---

## 7. 一键部署

```bash
# 全链路：四套系统一次装完（Charles 授权 + 根证书 + 适配层 + 一花 + 平台）
sudo bash deploy/fullchain.sh --public-host <你的公网IP> \
     --license-name <授权名> --license-key <授权码>

# 只看计划，不动系统
sudo bash deploy/fullchain.sh --plan
```

- `deploy/fullchain.sh` —— 全链路安装器，`--plan` 空跑 / `--skip` / `--only` 分段执行。
- `deploy/deploy.sh` —— 只更新平台（备份 → 上传 → 三方 md5 → 远端语法检查 → 重启 → 冒烟）。
- `deploy/switchover.sh` / `rollback.sh` —— 端口切换与回滚。**切换顺序不能反**：新后端改到目标端口并停 → 停旧服务 → 起新后端。
- `deploy/oneclick.sh` / `oneclick_deploy.py` —— 交互式一键部署。
- 环境变量集中在 `rewrite.env`（模板见 `deploy/rewrite.env.example`），**代码里不写死 IP / 端口 / 凭据**。

### 部署纪律（违反过就翻车）

- 先备份 → 上传后**三方 md5 比对** → 远端 `py_compile` / `node --check` → 重启 → 冒烟。
- 改 Charles 配置**必须「停 → 改 → 起」**：Charles 退出时会把内存里的配置回写磁盘，运行中改文件等于白改。
- 文件属主必须对（适配层 `ccpx:ccpx`、平台 `ubuntu:ubuntu`）。`root cp` 会改属主，服务随后读不到。
- 同步目录时必须排除运行期数据目录与虚拟环境。

---

## 8. 回归测试

七组测试全部在真实服务器上跑，**403 PASS / 0 FAIL**：

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `_test_wpe_struct.py` | 适配层结构解析 / hex 工具 | 93 PASS / 0 FAIL |
| `_test_wpe_udp_adopt.py` | UDP 孤儿包自愈关联生命周期 | 25 PASS / 0 FAIL |
| `_test_wpe_export_live.py` | 导出全链路（内存 / 落盘 / 完整 hex） | 48 PASS / 0 FAIL |
| `_test_wpe_udp_live.py` | UDP 中继实链路 | 12 PASS / 0 FAIL |
| `_test_wpe_console_live.py` | 平台侧导出与下载接口 | 30 PASS / 0 FAIL |
| `_test_mcp_live.py` | MCP 17 工具在线行为 | 65 PASS / 0 FAIL |
| `_test_mcp_wpe.py` | MCP 离线自检（路径必须落在真实路由表） | 130 PASS / 0 FAIL |
| `_smoke.py` | 后端冒烟 | 85 PASS / 0 FAIL |

测试脚本里刻意保留下来的几条经验（都对应真实踩过的坑）：

- **断言必须「上游感知」**：把「上游必死 / 必活」写死，另一侧必然假红。
- **断言缺失 = 假绿之源**：`None` 静默传下去只在最远处现形；`all()` 对空集恒真 = 空跑；**绿色的假证据比红色的还坏**。
- **路由表要从源码现抽**，不要手抄常量 —— 手抄的会随源码漂移，测试全绿而线上 404。
- **取响应头要大小写无关**：服务端发的是原始大小写，直接按大写取会「取不到」，被误读成功能缺陷。

---

## 9. 目录结构

```
.
├── README.md / README.en.md      中英文介绍（本文件 / English）
├── SECURITY.md                   敏感信息处理说明（务必先读）
├── adapter/                      CCProxy 适配层
│   ├── ccproxy_adapter.py        抓包点 + WPE 引擎（单文件，~191 KB）
│   ├── ccpx.service / config.example.json
│   └── e2e_api.sh / e2e_proxy.sh / mktoken.sh / setup.sh
├── platform/
│   ├── backend/app/              FastAPI：config/security/db/models/schemas/
│   │                             upstream/deps/routers
│   ├── backend/_smoke.py         后端冒烟（85 项）
│   ├── frontend/                 Vue3 + Vite（src / dist / 构建配置）
│   └── deploy/                   部署脚本 + systemd 单元 + env 模板
│       └── vendor/               第三方件（**大二进制不入库**，见 vendor/README.md）
├── docs/                         架构设计、部署报告、根因分析等交付文档
├── screenshots/                  界面截图
└── tools/
    ├── _srv.py                   SSH/SFTP 极简通道（run / sudo / put / get）
    ├── tests/                    七组回归测试
    └── probes/                   对账探针（透传对账 / 零丢失闭环 / MCP 工具面）
```

---

## 10. 已知限制（照实写，不粉饰）

- **`wpe_release` 的正向路径未做端到端验证** —— 需要制造一个真实断点，会卡住真实用户的连接最长 30 秒，所以留在维护窗口做。
- **iOS `.mobileconfig` 未签名** —— 没有 Apple 开发者证书；未签名描述文件在 iOS 上需要手动信任。
- **一花 PHP 后端（8081）未下线** —— 数据层已重写进平台，PHP 属于可选残留，建议并行观察一段时间后再停。
- **历史密码哈希迁移**：平台能校验旧的 PBKDF2 记录，便于平滑迁移，但迁移完成后建议统一重设。
- 适配层的 TLS 解密开关默认保守；不安装根证书时 HTTPS 会报证书错误，这是预期行为，不是 bug。

---

## 11. 许可与致谢

- 本仓库的**自有代码**（平台前后端、适配层、部署脚本、测试）为自托管项目源码。
- **Charles Proxy 为商业软件**，本仓库**不包含**其发行包，仅提供安装脚本与授权注入方式；使用需自行取得授权。
- 第三方组件（FastAPI、Vue3、Vite、MySQL 等）遵循各自许可证。

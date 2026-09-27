# 上线部署说明（_rewrite → 203.0.113.10）

> 权威源：`_deploy/ccpx/_console_server.py`（旧管理台，md5 `1796539c78e9bbaaff137adb87e15184`）
> 与 `_deploy/ccpx/ccproxy_adapter.py`（适配层，md5 `c771a091e02b92ede36b0e72dfbcca83`，**不重写**）。
> 本目录是新栈（FastAPI + Vue3）的部署件。

## 零、一键部署（推荐，一条命令）

```bash
# 本地 Windows（工程内，带 paramiko 的解释器）
cd _rewrite/deploy
C:/Users/MR/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe \
    oneclick_deploy.py                # 打包 -> 上传 -> 远端校验 -> 部署 -> 探活
```

可选参数：
- `--fresh` 重新生成 JWT 密钥与管理员口令
- `--switch` 部署完直接切到 8890（停掉旧 charles-console）
- `--purge-old` **删除**旧 charles-console 的代码与单元（先备份到 `/opt/_archive/`）
- `--no-upload` 只打包，用于本地检查包内容

`--switch --purge-old` 一次跑完就是「部署 + 切换 + 清场」。`--purge-old` 有前置检查：
必须已经跑在 8890 上、且 `/ruleset.conf` 拉得到，否则中止（删掉之后没有回头路）。

它做四件事，任何一步失败都会**明确停在那里**：

1. **打包** —— `backend/` + `frontend/dist/` + `deploy/` + `VERSION`，输出到 `_rewrite/dist_pack/`。
   `backend/var/`（SQLite 库）、`__pycache__`、`node_modules`、`.data_smoke*`、`*.tgz` 一律排除。
   **带上 `var/` 就等于把线上数据库覆盖成本地空库**，所以这条排除是硬约束不是建议。
   打包时还会把 `.sh` / `.service` 的 **CRLF 归一成 LF**（见下面「四个真跑才现形的坑」）。
2. **上传** —— SFTP，比字节数确认完整。
3. **远端校验包结构**（`deploy/oneclick.sh` 第 2 步）—— 缺目录/缺文件就中止，
   **此时线上什么都没变**。少了这一步，一个残缺包会让 `rsync --delete` 把线上代码删空。
4. **交给 `deploy.sh`** —— 建 venv、装依赖、**迁移设备侧资源**、落 systemd 单元、
   同步前端 dist、`compileall`、起服务、冒烟。

首次部署（`/opt/rewrite/rewrite.env` 不存在）会**自动**走 `--fresh` 生成强口令 ——
绝不把模板里的 `CHANGE_ME` 带上线。口令只打印一次，记下来。

> ★ `REWRITE_ADMIN_PASS` **只在首次建库时生效**。库已存在时改 `.env` 里的值是无效的，
> 脚本会明确提示这一点（`deploy.sh` 检测 `var/platform.db` 是否存在）。

### 手工部署（一键脚本不可用时）

```bash
mkdir -p /tmp/rewrite_src/{backend,frontend,deploy}
tar xzf /tmp/rewrite_backend.tgz -C /tmp/rewrite_src              # 展开成 backend/
tar xzf /tmp/rewrite_dist.tgz   -C /tmp/rewrite_src/frontend      # 展开成 dist/
bash /tmp/rewrite_src/deploy/deploy.sh                            # 落到 8903 暂存
```

```bash
# 迁移一花存量卡密（先 dry-run）
sudo /opt/rewrite/venv/bin/python /tmp/_migrate_yihua.py
sudo /opt/rewrite/venv/bin/python /tmp/_migrate_yihua.py --apply

# 切换 8890
bash /tmp/rewrite_src/deploy/switchover.sh
#    出问题：bash /tmp/rewrite_src/deploy/rollback.sh
```

## 一、已实机验证（服务器 + 真适配层，非本机盲跑）

| 项 | 结果 | 判据 |
|---|---|---|
| 后端契约冒烟 | **75 PASS / 0 FAIL / 0 SKIP** | `_smoke.py`，SKIP 是「前端未构建」已消除 |
| WPE 忠实透传对账 | **27 PASS / 0 FAIL** | 与适配层 `/wpe/*` 逐字段比对 |
| export→clear→import 零丢失闭环 | **19 PASS / 0 FAIL** | 73 条往返后完整 hex 多重集一字不差 |
| MCP 17 工具 | **20 PASS / 0 FAIL** | 含 `wpe_clear`/`wpe_import` 经 MCP 往返 |
| **根证书一键安装** | **30 PASS / 0 FAIL** | `_probe_ca.py`：`/ca` 指纹 == 磁盘实算、`.mobileconfig` 是 `x-apple-aspen-config` 且内嵌 DER 逐字节相等、UUID 由指纹派生（幂等） |
| **用户门户 + 公开应用列表 + 卡密删除** | **46 PASS / 0 FAIL** | `_probe_portal.py`：门户无登录可达、`apps/public` 只回两列且不含出口/凭据、停用应用不出现、三个 client 动作走通、管理端仍 401、删卡密可用、测试数据全清 |
| **合计** | **217 PASS / 0 FAIL** | systemd 托管下复跑，六支探针全绿 |
| **切换 8890 后全量复跑** | **217 PASS / 0 FAIL** | 六支探针目标改为 `127.0.0.1:8890` 重跑，全绿；外网 `curl` 实测 `/` `/portal` `/ca` `/healthz` `/mcp(POST)` 全 200 |
| 前端静态托管 | ✅ | `/` 200、深链 `/wpe` 200 回退、`/api/v1/nope` **404 不回 HTML** |
| 一花迁移 | ✅ dry-run / apply / 幂等 三态 | `+30 day`→720h、`+1 day`→24h |

**「一个包都不放过」的硬证据**：最大单包 **65536 字节**，在列表里 `hex_trunc=true`
（`hex_full_len=131072`），而 `GET /api/v1/wpe/packets/{id}` 取回 **恰好 131072 字符
= 65536 字节**，与适配层逐字段一致。

### 本轮补掉的三处「有接口但用户走不通」

| 缺口 | 原来 | 现在 |
|---|---|---|
| 根证书 | 只有 `/ca.crt` `/ca.pem` **下载**，iOS 上拿到文件也装不进去 | `/ca` 引导页（分平台步骤 + 指纹核对 + 扫码）、`/ca.mobileconfig` 一键安装、`/ca.qr.svg` |
| 一花用户门户 | 只有管理台，**没有最终用户页面** | `/portal`（注册/充值/查询，无登录）+ `GET /api/v1/kami/apps/public` |
| 卡密删除 | 一花 `ajax.php?act=delkami` 有，新栈**漏了** | `POST /api/v1/kami/delete`（批量，一花同款文案）+ `DELETE /api/v1/kami/{id}`（单条）+ 前端勾选/逐行删除 |

**设备侧新增端点（全部无登录，设备侧硬约束）**

| 路径 | 说明 |
|---|---|
| `/ca` | 根证书安装引导页（iPhone / Android / Windows / macOS 四平台） |
| `/ca.mobileconfig`（同 `/charles-ca.mobileconfig`） | iOS / macOS 配置描述文件，`Content-Type: application/x-apple-aspen-config` |
| `/ca.qr.svg` | 二维码 SVG（`?what=ca` 引导页 / `?what=ok` 检测页） |
| `/portal` | 用户自助门户（注册 / 充值 / 查询） |

**★ 四处「真跑才现形」的坑**

1. **`Template.substitute()` 撞上 JS 里的 `$`** —— 门户模板的 JS 用了
   `var $=function(id){...}` 简写，而 `string.Template` 把每个 `$` 当占位符，
   一个 `$('r-go')` 就让整页 **500**。修法：该模板不需要服务端插值（数据全走
   `fetch`），改成普通字符串常量。`_CA_TPL` 保留 `Template` 是因为它真的插
   `$cards`，且已确认模板里没有别的裸 `$`。
2. **前端改了 HTML 没改 JS** —— 注册表单的应用下拉框删掉了，但 handler 里还留着
   `$('r-app').value`。元素不存在 → `TypeError` → **按钮点了没反应**。
   后端接口正常、页面 200，只有真在浏览器里点一下才会发现。
3. **探针自己的测试账号是持久的** —— 第一版用固定用户名 `probeportal1`，
   第二次跑 `insert` 就撞适配层的「账号已经存在」→ 两条假红。
   修法：用户名带随机后缀 + 跑完删适配层账号。**假红比没红更坏**，
   它会让人去改本来就对的代码。
4. **`deploy.sh` 被写成 CRLF 行尾** —— Windows 上编辑后整个文件 208 行都是 `\r\n`，
   Linux 上 `set -euo pipefail\r` 报 `set: pipefail: invalid option name`，
   看着像脚本语法错，实际是行尾。**`bash -n` 本地检查是过的**（本地就是 Windows
   bash），只有真传到 Linux 跑才炸。
   修法两处：打包时按扩展名归一化（`oneclick_deploy.py` 的 `_add_tree`）+
   `oneclick.sh` 解包后就地兜底一遍。

   ★ 顺带在修这个坑时**又踩了一个同类的**：兜底检查写成
   `CRLF_HIT=$(find ... | wc -l)`，而脚本开头是 `set -euo pipefail` ——
   `grep -l` 无匹配返回 1，`find -exec +` 把它带出来，pipefail 取「最后一个非零
   退出码」→ 赋值语句失败 → `set -e` 直接退出。表现是「校验通过后什么都没发生
   就退出 1」。改成 `while read` 循环，退出码只来自 `find` 且被进程替换吞掉。

**★ 还有一个跟删除有关的坑**

**`ruleset_path` 的默认值原来指向旧管理台目录** —— 删 `/opt/charles-console/`
会让 `/ruleset.conf` 在删目录那一刻**静默 404**，而这条是手机客户端要拉的，
断了的表现是「客户端连不上」，从管理台这边完全看不出跟它有关。
修法：默认值改到 `/opt/rewrite/ruleset.conf`，并在 `deploy.sh` 加一道
**幂等迁移**（新位置不存在且旧位置存在就搬过来，属主 `ubuntu:ubuntu` 0644）。

### 真链路跑出来的两个真 bug（本机永远触发不了）

1. **`expire_at` naive/aware 比较崩溃** —— SQLite 不存时区，写进去的 aware UTC
   读回来是 naive，`k.expire_at < utcnow()` 抛 `TypeError`。
   本机上游 502 时 `expire_at` 一直是 NULL，短路了 `and`，所以从没暴露；
   上线第一次真核销就会让 `/kami/list`、`/kami/stats` 500。
   修法：`models.py` 加 `TZDateTime` TypeDecorator，在**类型层**统一往返 aware UTC
   （7 个列一次修完，不是逐调用点打补丁）。
2. **`KamiServer` 的 `UniqueConstraint(host,port)`** 让重复建出口回 409 ——
   这是**正确**行为；错的是冒烟脚本没断言 `srv_id`，`None` 一路静默传下去，
   最后在 `insert` 那步表现成「-3 服务器通信出现问题」，看着像上游挂了。

## 二、有意差异（**不是漏了**）

| 项 | 处理 | 原因 |
|---|---|---|
| **「一键部署注册授权」（软件授权 / 激活 / license）** | **未内置，也不打算内置** | 这是本项目**自有**的重写，不是分发出去的商业软件 —— 没有 license 校验、没有激活码、没有授权服务器。`REWRITE_ALLOW_ANON` 是「免登录」开关，不是授权机制，两者不要混为一谈。真需要授权能力时再说，不假装现在有。 |
| Charles 的「注册授权」 | **不适用** | 线上跑的是 `charles-headless`（`--headless` 无 GUI），全程不开界面、不做 license 校验；D1 技术栈决策也没引入 Charles SDK。 |
| `/api/ccpx/yh/**`（一花会话桥） | **有意移除** | 一花会话 id 从密码派生（`md5(user.pass.password_hash)`），整条链按 D2「PHP 立即下线」作废。新栈的卡密域是 `/api/v1/kami/**` |
| 一花 `del` 客户端分支 | **不开放** | 它要求 POST 里带 CCProxy 管理员账号密码 —— 等于把凭据摊在客户端 |
| 一花后台登录页 / 图形验证码 `code.php` | **有意移除** | 用户明确要求免登录、统一入口。管理端改由 `/api/v1/auth/login` + JWT |
| 设备绑定 | **不实现** | 一花没有这个概念，不发明 |
| 卡密「停用」 | 保留为扩展态 | 一花只有「用过/没用过」。新栈 `Kami.enabled` 是显式布尔 |
| `type=insert` 的密码 | 用户自填 | 一花原样语义。旧 `_rewrite` 实现是「系统发号」，那是**错的**契约，已废弃 |
| 删卡密**不回收**代理端账号 | 如实标注 | 一花也不回收。删卡密只删记录，账号还能连 —— 所以前端 confirm 会点名「已激活的卡有 N 张，对应账号不会被回收」。要真回收得去 CCProxy 面板删账号 |

## 三、一花原始缺陷在新实现里消失

| 一花缺陷 | 新实现 |
|---|---|
| `state` 检查与写回之间无事务 → 并发核销同一张卡**双开账号** | `_claim()` 用条件 UPDATE 的 `rowcount` 做原子占位 |
| 只在成功时写 `state=1`，失败不烧卡 | 占位提前了，所以配了对称的 `_release()`；冒烟有断言「失败后卡密回到 unused」 |
| 「账号状态」开关**只能开不能关**（`function.php:397-399` 缺 else） | `Kami.enabled` 是显式布尔，可关 |
| 每次核销拉 `/account` 全量 HTML 再正则解析 | 走适配层 `/account`，同一份真相但不在业务代码里解析 HTML |
| `times` 是 varchar 存的 PHP 相对串 | 存整数 `duration_hours`；迁移脚本负责转换，解析不出按 24h 兜底**并点名** |

## 四、当前线上入口与凭据（2026-09-27 切换后实机验证）

**入口（外网可达，已 curl 实测）**

| 用途 | 地址 | 需要登录 |
|---|---|---|
| 管理台（统一入口） | `http://203.0.113.10:8890/` | ✅ 是 |
| 用户自助门户 | `http://203.0.113.10:8890/portal` | ❌ 否（最终用户用） |
| 根证书安装引导 | `http://203.0.113.10:8890/ca` | ❌ 否（手机用） |
| iOS 一键装证书 | `http://203.0.113.10:8890/ca.mobileconfig` | ❌ 否 |
| 连通性检测页 | `http://203.0.113.10:8890/ok` | ❌ 否 |
| Kitsunebi 规则集 | `http://203.0.113.10:8890/ruleset.conf` | ❌ 否 |
| MCP（WPE 工具） | `http://203.0.113.10:8890/mcp`（POST） | 否 |
| 健康检查 | `http://203.0.113.10:8890/healthz` | ❌ 否 |
| 设备侧清单 | `http://203.0.113.10:8890/_device` | ❌ 否 |

**凭据（全部实机登录/调用验证过，不是从配置文件抄的）**

| 用途 | 账号 | 口令 | 验证方式 |
|---|---|---|---|
| **管理台** | `admin` | `Admin-ChangeMe-2026` | `POST /api/v1/auth/login` 返回 `isAdmin:true` + token |
| 适配层管理 API（8893） | `admin` | `Adapter-Token-ChangeMe` | `GET /status` → `{"ok":true,"accounts":3}` |
| 代理账号（HTTP 8888 / SOCKS5 8889） | `TestPass1!` | `Ssh-ChangeMe-2026` | 走 8888 取 `m.baidu.com` → HTTP 200 |

> ★ 管理台口令仍是 smoke 阶段的弱口令。`REWRITE_ADMIN_PASS` **只在首次建库时生效**，
> 现在改 `.env` 里的值是**无效的**。要换成强口令：登录管理台 → 改密码。
> （`REWRITE_ALLOW_ANON=false` 当前生效，所以管理台必须登录。）

**旧管理台的处置**

| 项 | 状态 |
|---|---|
| `/opt/charles-console/`（3.8 MB） | **已删除** |
| `charles-console.service` | **已删除**（`inactive` + 单元文件不存在） |
| 8890 监听者 | 现在是新栈 `rewrite.service` |
| 8903 | 已无监听（完全迁走） |
| 备份 | `/opt/_archive/charles-console_20260927-202313.tar.gz`（936921 字节）+ `.service_20260927-202313` |
| 回滚 | `sudo tar xzf /opt/_archive/charles-console_20260927-202313.tar.gz -C /opt && sudo cp /opt/_archive/charles-console.service_20260927-202313 /etc/systemd/system/charles-console.service && sudo systemctl daemon-reload && sudo systemctl start charles-console` |

> 旧管理台里那个管理员账号 `SHUHUI`（`2675326937@qq.com`）随目录一起进了备份包。
> 新栈**没有**这个账号，只有 `admin`。要恢复它得从备份里取 `users.json`。

## 五、未执行 / 残留缺口（如实标注）

1. **`wpe_release` 正向路径未验证** —— 当前断点队列为空（`holding=0`）。
   要造真断点得设一条能命中实时流量的规则，会把真实用户连接卡住最长
   `HOLD_TIMEOUT=30s`。**线上不做**。已验的是失败路径（不存在的 id →
   `{"ok":false,"err":"封包 #999999999 不在断点队列"}`，工具级报错，非 500）。
   想验就挑维护窗口：
   ```bash
   # 设一条窄规则 -> 等命中 -> release -> 复位
   curl -s -X POST .../wpe/hold -d '{"on":true,"rule":{"target":"<端口>","dir":"s2c"}}'
   ```
2. **`.mobileconfig` 未签名** —— 没有 Apple 开发者证书，iOS 会标「未验证」。
   能装，但描述文件页面会显示红色「未验证」字样。要消掉得买证书走
   `codesign` / `openssl smime` 签名。**当前如实告知用户，不假装签过名。**
3. **一花 PHP（8081）未下线** —— 按 D2 要下线，但建议先并行跑几天。
   `yihua-app` 容器仍在跑（`127.0.0.1:8081`，仅本机可达）。
   命令见 `switchover.sh` 尾部注释。
4. **`/opt/ccpx/exports` 清理未生效** —— 规则在 `ccpx-exports.conf`，
   需 `sudo cp` 到 `/etc/tmpfiles.d/` 并跑一次 `systemd-tmpfiles --clean`。
   现状：13 个文件约 30 MB，未到危险线。
5. **`wpe_hold_set` 只做了幂等写** —— 用当前值原样写回，证明写通路可用
   且不改线上行为；没有真的改规则。
6. **当前 73 条缓冲是导入副本** —— 闭环测试的副作用：字节与实时包一致，
   但被标记为 `imported` + 「非实时流量」。新流量会继续追加；要恢复成纯实时：
   ```bash
   curl -s -X DELETE -H "Authorization: Bearer $T" http://127.0.0.1:8890/api/v1/wpe/packets
   ```
   历史已在 `/opt/ccpx/exports/wpe-20260927-*.json` 落盘。
7. **管理台口令是 smoke 阶段的弱口令**（`Admin-ChangeMe-2026`）——
   要在管理台界面里改，改 `.env` 无效（`REWRITE_ADMIN_PASS` 只在首次建库生效）。

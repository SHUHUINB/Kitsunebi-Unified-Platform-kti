# CCProxy 协议适配层（一花卡密系统后端）

一花卡密系统（ccproxy 系）说的是 **CCProxy 管理协议**，不是通用 HTTP。
本服务把那套协议实现出来，让发卡 / 计费 / 查询逻辑一行都不用改。

## 端口模型（复刻真 CCProxy）

真 CCProxy 的**管理页面和 HTTP 代理共用同一个端口**（默认 808）。
一花的 `server_list.cport` 同时被当成「通讯端口」（fsockopen 打 `/account`）
和「代理端口」（用户侧连同一个口），所以这里也合流：

| 端口 | 作用 | 鉴权 |
|---|---|---|
| 8893 | 主端口：`/account`、`/status` 走管理面；其余走 HTTP 代理 | 管理面 `admin:<管理密码>`；代理面 `<账号>:<密码>` |
| 8892 | SOCKS5 代理 | `<账号>:<密码>`（RFC1929） |

**一花的 `server_list.cport` 填 8893。**

## 一花侧对接

后台 → 服务器列表 → 填：

- 服务器IP：`203.0.113.10`
- 通讯端口(cport)：`8893`
- 通讯账号：`admin`
- 通讯密码：`Adapter-Token-ChangeMe`

## 出口（花瓶链路）

用户流量默认**链到本机花瓶** `127.0.0.1:8888`，所以抓包 / 远程映射 / 验证页仍然生效。
两条路走法不一样，这是踩过坑之后定下来的：

| 客户端请求 | 走法 | 为什么 |
|---|---|---|
| 普通 HTTP（`GET http://host/path`） | **直连花瓶 8888，把绝对 URI 请求行原样喂进去** | 这才是正向代理语义。如果先 `CONNECT host:80` 建裸隧道，花瓶就不再解析 HTTP，抓包失效；而且把绝对 URI 喂给隧道对面的源站，实测拿回 502 |
| CONNECT（HTTPS 等） | 让花瓶做 `CONNECT host:port`，隧道打通后双向透传 | 花瓶的 SSL 代理就是这么工作的 |
| CONNECT 但花瓶回非 200 | **回落直连目标**（`fallback_direct`） | 花瓶拒收不该等于用户断网 |

相关开关（`config.json`）：

```json
"chain_to_charles": true,      // false = 全部直连，不碰花瓶
"fallback_direct": true,       // 花瓶拒收 CONNECT 时回落直连
"charles_bypass_ports": [],    // 这些目标端口不走花瓶，例如 [443] 避开证书固定的客户端
```

每条代理请求都会在日志里留下 `-> charles` 或 `-> direct`，
运维时一眼能看出这条流量到底有没有过花瓶（过了才谈得上抓包）：

```bash
sudo tail -f /opt/ccpx/adapter.log | grep -E '\-> (charles|direct)'
```

注意花瓶配置里 `sslLocations` 为空 = **对所有目标做 SSL 解密**。
遇到证书固定的客户端会握手失败，把对应端口加进 `charles_bypass_ports`。

## 运维

```bash
systemctl status ccpx
systemctl restart ccpx
journalctl -u ccpx -n 100 --no-pager
tail -f /opt/ccpx/adapter.log

# 账号库（SQLite）
sqlite3 /opt/ccpx/accounts.db "select username,password,enabled,datetime(expire_at,'unixepoch','localtime') from accounts;"

# 协议自检
/usr/bin/python3 /opt/ccpx/ccproxy_adapter.py --selftest

# 状态
curl -s -u admin:Adapter-Token-ChangeMe http://127.0.0.1:8893/status
```

## 协议约束（改代码前必读）

一花用 9 条正则解析 `/account` 的返回，源码里没有 DOTALL：

1. **每个 `<input>` 必须独占一行** —— `.*` 不跨行，挤一行只会解析出 1 个账号。
2. **`name="X"` 与 `value=` 之间必须有两个空格** —— 正则 `name="X" .* value=` 里
   `.*` 可空但要吃掉 1 个字符。单空格 → 0 命中。
3. **复选框 `checked` 的位置是硬编码下标** —— 剥掉 `<>/` 后必须从下标
   46（enable）/ 51（usepassword、autodisable）开始，否则开关全反。

改动字面量后**必须**先跑 `--selftest`，它把这三点都锁住了。

## 到期语义

账号库只存事实，不存派生状态：`expire_at` 是唯一真源，`enabled` 只表示
「手工禁用」。每次渲染时现算：`enabled AND now <= expire_at` 才勾选 `enable`。
所以「到期真实断开」不需要任何定时任务。

## 防火墙

8893 / 8892 需要对外可达（用户要连）。管理面密码务必改掉默认值。

**腾讯云安全组必须放行 8892 / 8893/TCP。** 只放行安全组是不够的——
实测从公网访问未放行的端口一律 `Connection timed out`（不是 refused，也不是 502），
容器/公网侧都连不上。放行后验证：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -u admin:Adapter-Token-ChangeMe \
  http://203.0.113.10:8893/account      # 期望 200
```

## 容器内访问的坑（一花跑在 Docker 里）

一花容器访问宿主机的**公网 IP** `203.0.113.10:8893` 会超时——
这是 NAT 回环（hairpin）不被支持，不是适配层的问题。
`server_list.ip` 要填 **docker 网关** `172.20.0.1`（用 `docker network inspect`
确认网关地址），从容器里就能通。终端用户侧不受影响：客户端是自己填服务器地址的，
`server_list.ip` 只用于后台 → CCProxy 的 fsockopen。

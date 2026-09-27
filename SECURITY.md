# 安全说明 / Security Notes

**先读这一页再部署。** 本仓库是从一个**正在运行的实例**里整理出来的，所有真实凭据
都在导出时被替换成了占位值。下面写清楚**替换了什么**、**排除了什么**、以及**你部署前必须做什么**。

**Read this page before deploying.** This repository was exported from a **live
instance**. Every real credential was replaced with a placeholder during export. Below is
exactly **what was replaced**, **what was excluded**, and **what you must do before deploying**.

---

## 1. 被替换的值 / Values replaced

| 类型 | 占位值 | 出现位置 |
|---|---|---|
| 服务器公网 IP | `203.0.113.10`（RFC 5737 文档网段） | 脚本、配置、文档 |
| SSH 口令 | `Ssh-ChangeMe-2026` | `tools/_srv.py` 默认值、部署文档 |
| 平台管理员口令 | `Admin-ChangeMe-2026` | 部署脚本、文档 |
| 适配层管理令牌 | `Adapter-Token-ChangeMe` | 适配层配置样例、SQL、探针 |
| 数据库 root 口令 | `MySQL-Root-ChangeMe` | 迁移脚本、文档 |
| 旧管理台令牌 | `Console-Token-ChangeMe` | 文档 |
| Charles 授权码 / 注册名 | `0000000000000000` / `your-license-name` | env 模板注释、文档 |
| 测试账号口令 | `TestPass1!` | 测试脚本与探针 |

替换是**脚本化**完成的（`_build_repo.py`，随本仓库工作流保留在项目侧），并且导出后
**重新扫过一遍**，上述每一个原值在产物里的命中数都是 **0**。

> 为什么值得这么麻烦：手拷一定会漏一个，**漏一个就等于把线上主机交出去**。

---

## 2. 有意排除的内容 / Deliberately excluded

| 内容 | 原因 |
|---|---|
| Charles Proxy 4.5.6 发行包（约 54 MB） | **商业软件**。在公开仓库里再分发不合适；`fullchain.sh` 会按需获取或由你提供 |
| 一花 `yihua-deploy.tar.gz` | 含完整库结构与数据，不适合入库 |
| `__pycache__` / `node_modules` / `dist_pack` / `var` / `.data_smoke*` / `*.bak` | 生成物与运行期数据 |
| 旧管理台 `_console_server.py` 及其前端 | 已被平台版取代，留着只会误导后来人 |
| 逆向分析工作目录（样本、IDA 数据库、固件、工具链等） | 与本平台无关，不属于本仓库范围 |

第三方件目录 `platform/deploy/vendor/` 保留了**目录结构与 systemd 单元**，
但大二进制不在库里 —— 见该目录下的 `README.md`。

---

## 3. 部署前必须做 / Mandatory before deploying

```bash
# 1) 生成 JWT 密钥（留空会退化成进程级临时密钥，重启即全部会话失效）
openssl rand -base64 48

# 2) 换掉管理员口令（模板里的 CHANGE_ME_strong_password 必须替换）
#    改 rewrite.env 里的 REWRITE_ADMIN_PASS
#    ★ 注意：仅在首次建库时生效；库已存在时改这里无效，请用界面改密码。

# 3) 换掉适配层管理令牌（adapter/config.example.json → config.json）
# 4) 换掉数据库 root 口令
# 5) 把 REWRITE_ALLOW_ANON 保持为 false —— true 等于任何人可匿名访问全部接口
```

**另外两条建议（本实例的实际教训）：**

- **不要长期使用弱口令。** 本项目历史上出现过「口令已写进 env 却一直没换」的情况；
  部署脚本只在新装时生成强口令，**库已存在时改 env 无效**，必须走界面或直接改库。
- **只放行必要端口。** 适配层管理 API（8893）应限制来源；云安全组不要图省事全开。

---

## 4. 报告问题 / Reporting

发现本仓库里仍有未脱敏的内容，请开 issue 说明文件与行号。
**请勿**在 issue 里粘贴任何真实凭据。

If you find anything still un-redacted, open an issue with the file and line number.
**Do not** paste real credentials into an issue.

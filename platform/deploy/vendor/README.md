# vendor/ —— 第三方件 / Third-party artifacts

`fullchain.sh` 会从这个目录取「非本仓库自有」的文件。目录结构**与脚本预期一致**，
但**大二进制不入库**（原因见下）。

```
vendor/
├── charles/
│   └── charles-headless.service           ← 已在库（本项目的 systemd 单元）
│       charles-proxy-<ver>-linux.tar.gz   ← ★ 未入库，需自行提供
├── ccpx/
│   ├── ccproxy_adapter.py                 ← 已在库（本项目自有代码）
│   ├── ccpx.service
│   └── ccpx-capture.service
└── yihua/
        yihua-deploy.tar.gz                ← ★ 未入库，需自行提供
```

---

## 1. 缺的两个文件 / The two missing files

### `vendor/charles/charles-proxy-<ver>-linux.tar.gz`

**为什么不在库里：** Charles Proxy 是**商业软件**。在公开仓库里再分发它的发行包
不合适，也会让本仓库的许可状态变得含混。单元文件与授权注入逻辑（本项目自有）都在库里。

**怎么补上，任选其一：**

```bash
# A. 你自己有安装包 —— 直接放进来
cp /path/to/charles-proxy-4.5.6-linux.tar.gz vendor/charles/

# B. 部署时临时指定（不落进 vendor）
sudo bash deploy/fullchain.sh --charles-tgz /tmp/charles-proxy-4.5.6-linux.tar.gz \
     --public-host <你的公网IP> --license-name <名> --license-key <码>

# C. 从一台已装好的机器上打包（本实例就是这么做的）
sudo tar czf charles-proxy-4.5.6-linux.tar.gz -C /opt charles-proxy
```

脚本找包的顺序：`--charles-tgz` 参数 → `vendor/charles/charles*.tar.gz`。
两个都没有时会明确报错并给出提示，不会静默跳过。

**授权：** 你自己取得。注入方式是把 `<registrationConfiguration>` 的
`name` / `key` 通过 `--license-name` / `--license-key` 传进去；
留空则**不注入**，只如实读取现有注册状态。

### `vendor/yihua/yihua-deploy.tar.gz`

**为什么不在库里：** 它含完整的数据库结构与业务数据，属于实例数据而非源码。

**怎么补上：** 从你自己的实例上打包：

```bash
sudo tar czf yihua-deploy.tar.gz -C /opt yihua
```

缺失时 `fullchain.sh` 会报「没有一花部署包」并给出路径，同样不会静默跳过。

> 说明：一花的**数据层已经重写进平台**（见 `platform/backend/app/routers/kami.py`），
> PHP 属于可选残留。不用一花这套时，`--skip yihua` 跳过即可。

---

## 2. 一条设计取舍 / One deliberate trade-off

`vendor/` 里同时存在 `ccpx/ccproxy_adapter.py` 与仓库根部的 `adapter/ccproxy_adapter.py`，
**两者是同一份文件**，md5 应一致。

- `adapter/` 是给人读的位置（单独一份组件）。
- `vendor/ccpx/` 是给脚本用的位置（`fullchain.sh` 只认这里）。

之所以不做软链，是因为 Windows 检出（`core.symlinks=false`）会把软链变成普通文本文件，
脚本静默拿到一个「不是 Python 的文件」，报错还很难懂。多一份拷贝，换一个跨平台不会翻车的部署。

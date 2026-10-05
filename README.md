# Host Monitor for Kindle（墨水屏主机资源看板）

把 **本机（或远端机器）的 CPU / 内存 / 磁盘 / 网络 / TOP 进程**，实时投到
**Kindle / KOReader 墨水屏**上，做成一个**常驻看板**。

这是 [RC-APC/workbuddy_monitor.koplugin](https://github.com/RC-APC/workbuddy_monitor.koplugin)
的改造版：原版把 WorkBuddy 积分/任务投到 Kindle，本仓库把 PC 端的数据源和
渲染器整个换成「主机资源监控」，Kindle 端插件只改了名字、手势和兜底文本——
**架构不变**：PC 端跑一个仅标准库（+Pillow）的 HTTP 桥，Kindle 端装 KOReader
插件，每 3 分钟拉一张 8 位灰度 PNG。

> 适用场景：你有一台吃灰的 Kindle，想当个永远亮着的「服务器状态屏」——
> 不点亮手机、不打开电脑，翻一眼就知道宿主机 CPU 多少、内存吃没吃满、
> 磁盘还剩多少。

## 功能特性

- **常驻看板**：Kindle 上常驻显示，每 **3 分钟**自动刷新。
- **指标**：CPU 总占用 + 每核柱状图 + load1/5/15（+ 温度，若内核暴露）；
  内存/Swap 分段条；磁盘各分区（最多 4 行）；网络上下行速率 + 60 点下行
  迷你走势；TOP 5 进程（CPU% + 内存 MB）；运行时长。
- **双配色**：`theme=dark` 黑底白字 / `theme=light` 白底黑字，均为墨水屏安全灰度。
- **阈值告警条**：内存 ≥90%、磁盘 ≥90%、CPU ≥95%、Swap ≥50%、
  load1 ≥ 2×核数，任一命中就在封面顶部画全宽反色告警条（和原版「登录失效」
  同款最强视觉提示）。
- **锁屏壁纸**：可选，看板每次刷新把最新一张复制进 `hb_ss/cover.png`，
  KOReader 锁屏指向该文件夹即自动跟随。
- **离线兜底**：桥不可达时保留最后一张好图并静默重试；彻底失败时
  KOReader 原生文字板（纯 Lua 渲染，无图也能读）。
- **远端监控（可选）**：被监控机器上跑一个 20 行的 `res_report.py`
  agent 往桥里推数据，Kindle 端 `config.txt` 加一行 `host=名字` 即可看
  那台机器。

## 架构

```
 被监控机器（可以是桥所在机器，也可以是别的机器）
        │  res_report.py（可选）每30s POST /report
        ▼
  ┌─────────────────────────────┐
  │  PC 桥  res-bridge.py       │  监听 0.0.0.0:8865（仅标准库 + Pillow）
  │  · 采样线程 每5s 读 /proc   │  （Linux 纯 stdlib；装了 psutil 自动用 psutil）
  │  · /status.json             │
  │  · /cover.png  ← res_cover.py 渲染 8 位灰度 PNG
  └─────────┬──────────────────┘
            │  WiFi 局域网
            ▼
  ┌─────────────────────────────┐
  │  Kindle KOReader            │  host_monitor.koplugin
  │  · 常驻看板（每3分钟拉图）   │
  │  · 锁屏壁纸（hb_ss/）        │
  └─────────────────────────────┘
```

## 目录结构

```
koreader-host-monitor/
├── host_monitor.koplugin/   # Kindle 端 KOReader 插件
│   ├── main.lua             #   插件主体（看板/手势/锁屏，上游 fork）
│   ├── config.txt           #   第1行填桥地址；可选 theme=、host=
│   └── _meta.lua
├── res-bridge.py            # 电脑端桥（HTTP 服务 + 采样线程）
├── res_collect.py           # 资源采集（/proc 纯 stdlib；psutil 可选增强）
├── res_cover.py             # PNG 渲染（Pillow）
├── res_report.py            # 远端 agent（可选：被监控机器上跑）
├── deploy_to_kindle.py      # 部署插件到 Kindle（delete-first + 校验）
└── assets/                  # 示例封面（深/浅/告警态）
```

## 安装

### 1. 电脑端桥（Linux / macOS / Windows，Python 3.8+，需 Pillow）

```bash
pip install pillow            # 唯一硬依赖；可选：pip install psutil
python3 res-bridge.py         # 监听 0.0.0.0:8865
```

- **Linux**：没装 psutil 也没关系，走 `/proc` 纯标准库后端。
- **macOS / Windows**：请装 psutil（`/proc` 后端仅 Linux 可用）。
- 可选环境变量：`RES_PORT`（默认 8865）、`RES_TOKEN`（简单 Bearer 鉴权）、
  `RES_SAMPLER_SEC`（采样间隔，默认 5s）。
- 防火墙放行 8865（桥只监听、不发起连接；Kindle 禁 ICMP，ping 不通正常）。

### 2. Kindle 端插件

把 Kindle 用 USB 挂到电脑（Linux 是一个挂载点，Windows 通常是盘符），然后：

```bash
KINDLE_MOUNT=/media/you/Kindle python3 deploy_to_kindle.py
KINDLE_MOUNT=F:/ python3 deploy_to_kindle.py        # Windows
python3 deploy_to_kindle.py --once                  # 只试一次
```

插件会出现在 KOReader 的「插件」菜单里。**装完重启一次 KOReader。**
（插件落在 `koreader/plugins/host_monitor.koplugin/`。）

### 3. 配置桥地址

编辑 `host_monitor.koplugin/config.txt` 第 1 行（部署前改好再 deploy，
或者用插件菜单「设置桥地址」改）：

```
http://192.168.137.1:8865      # 电脑开热点时（网关）
http://192.168.1.20:8865       # 同路由器时，填 PC 的 LAN IP
```

> 同一 WiFi 下的 IP 是 DHCP 动态分配的，重连 / 休眠唤醒 / 路由器重启后可能变化；
> 变了就重新查 IP、改这里、重新部署。插件菜单里改过的地址优先级高于 config.txt。

### 4. 绑定手势（可选但推荐）

KOReader：`设置 → 手势 → 添加手势 → 画一个手势 → 动作列表`里选：
- **Host 常驻看板**（再触发一次即退出，相当于开关）
- **Host 退出常驻看板**
- **Host 设置休眠壁纸**

### 5. 锁屏壁纸（可选）

插件菜单「锁屏封面文件 → 查看位置与说明」会显示 `hb_ss/cover.png` 的绝对路径。
在 KOReader：`设置 → 屏幕 → 锁屏类型 = 随机图片`，`锁屏图片文件夹 =` 上面的 `hb_ss`。
点过「设置休眠壁纸」后，看板每次刷新都会把最新一张复制进去。

## 监控另一台机器（可选）

被监控机器上（Linux 即可，纯标准库；mac/Win 需 psutil）：

```bash
# 30 秒推一次（默认），前台跑；建议 nohup / systemd / supervisor 保活
nohup python3 res_report.py http://192.168.1.20:8865 --host vm2 &

# 或者用 cron 每分钟推一次（--once 只推一条就退出）
* * * * * python3 /opt/res_report.py http://192.168.1.20:8865 --host vm2 --once
```

然后 Kindle 端 `config.txt` 加一行：

```
host=vm2
```

看板右上角 host 框里就是 vm2，数据是它推过来的；桥自己那台机器的数据
仍然在 `/status.json`（不带 `?host=`）里，两条线互不影响。
报告超过 5 分钟没刷新时，封面上的 LIVE 状态会显示 `STALE` 反色块，
不会再静默地拿旧数装实时。

## 告警阈值

| 条件 | 告警文案 |
|---|---|
| 内存使用 ≥ 90% | `MEM 93% >= 90%` |
| 任一磁盘 ≥ 90% | `DISK /home 91% >= 90%` |
| CPU ≥ 95% | `CPU 97% SATURATED` |
| Swap 使用 ≥ 50% | `SWAP 61%` |
| load1 ≥ 2 × 核数 | `LOAD 9.3 HIGH (cores=4)` |

阈值在 `res_collect.py` 顶部（`ALARM_*` 常量），改一处生效。
命中任一条件时封面顶部出现全宽反色告警条，最多同时列 3 条。

## 隐私与凭据

桥默认只监听局域网；可选 `RES_TOKEN` 做简单 Bearer 鉴权。数据全部是
本机/局域网内的机器指标，不出局域网。`res_bridge.log` 只记录请求来源 IP。

## 常见问题

**Q：封面图一直不更新？**
A：先看桥目录 `res_bridge.log` 里有没有 `REQ from <Kindle IP>`。没有说明
WiFi/热点不通（Kindle 禁 ICMP，ping 不通是正常的）；有说明链路通，
看同一行的 `COVER ok` 还是 `COVER FAIL`。

**Q：时间戳冻在旧时间？**
A：常驻看板每 3 分钟主动重拉图，`SYNC` 时间戳跟着变；若不变，多半是桥没
收到请求（见上）。

**Q：Kindle 上点不动 / 只能重启？**
A：常驻期间插件会暂停自动待机（`auto_suspend_timeout_seconds` 置 0），
若你的 KOReader 没有 autosuspend 插件则无此操作；退出看板（点任意处/
滑动/翻页键/菜单）后恢复。

**Q：非 Linux 机器上桥起不来？**
A：`res_collect.py` 的 `/proc` 后端仅限 Linux；macOS/Windows 先
`pip install psutil`。

## License

[MIT](LICENSE) — 本仓库为 fork，原插件与桥的作者是 RC-APC。

# 安装、开机恢复与升级

## 1. 前提与只读启动

目标：Linux（systemd 用户服务）、Python 3.11+、可运行的 NVIDIA 驱动。建议使用独立普通用户管理矿工和大屏。核心无需 pip 依赖，`curl` 用于交易所公共接口诊断，`openssl` 用于部分可选更新签名校验。

```bash
python3 scripts/configure.py init
python3 scripts/configure.py discover
MINING_MONITOR_ALLOW_EMPTY_COINS=1 python3 app/mining_monitor.py
```

本地核心只监听 127.0.0.1:8091。配置/历史均位于用户 XDG 目录，不写进源码。此时控制接口关闭。没有矿工配置时看到“未挖矿”是正常结果。显卡身份被保存到本机配置，不能提交 GitHub。

## 2. 先验证矿工，再绑定

从矿工官方渠道安装，先在终端验证收款地址、矿池与有效份额。程序不为你创建钱包或交易所账户。按 [配置说明](CONFIGURATION.md) 建立已有 systemd 用户服务和日志绑定，再运行：

```bash
python3 app/mining_monitor.py --check-config
python3 app/rig_control.py --config "$HOME/.config/miner-dashboard/rig-control.json" --state-dir "$HOME/.local/state/miner-dashboard/rig-control" --check-config
```

逐卡切换需要每个配置单元只占用一个 UUID。多卡共用矿工支持监看，逐卡切换会拒绝；按[逐卡配置指南](PER_GPU.md)迁移为独立服务后即可网页逐卡启停。

## 3. 用户服务

在克隆目录中以挖矿用户执行：

```bash
python3 scripts/install_user.py --enable-controls
sudo loginctl enable-linger "$USER"
systemctl --user status miner-dashboard.service
```

省略 `--enable-controls` 为只读。开启控制后仍需原有同 UID、Origin、CSRF 和 rig 白名单检查。不要让其他不可信程序共享此账户。

脚本会拒绝覆盖现有 unit。手动改 `~/.config/systemd/user/miner-dashboard.service` 后运行 `systemctl --user daemon-reload` 与 `systemctl --user restart miner-dashboard.service`。矿工服务单独 enable，且必须设置 `Restart=on-failure`、`KillMode=control-group` 和有限 `TimeoutStopSec`，防止退出/关机等待遗留进程。示例见 [service 模板](../examples/miner.service.example)。不要使用循环无限后台启动多个矿工的脚本。

## 4. 可选 root 助手

以下只在 Linux 矿机操作。**先阅读脚本再执行 sudo**。一次选择所需助手；程序复制到 `/opt/miner-dashboard/app`，固定 root 拥有且不可由大屏用户修改。已经存在的安装目录会拒绝覆盖。

先在普通用户目录核对发布commit和代码，再用独立root只读副本执行。不要把root服务指向可写clone。以下命令在已审阅的clone根执行；若stage已存在先核对旧内容，不重复覆盖：

```bash
sudo install -d -m 700 /root/miner-dashboard-release
sudo cp -R app scripts requirements-rgb.txt /root/miner-dashboard-release/
sudo chown -R root:root /root/miner-dashboard-release
sudo chmod -R go-w /root/miner-dashboard-release
sudo python3 /root/miner-dashboard-release/scripts/install_system.py --user "$USER" --tuning
# 首次同次按需增加 --rgb、--exchange。
```

安装器拒绝源符号链接/未知文件/非root拥有/可写路径，按源码文件白名单读取；每个目标存在或为符号链接都会在写入前拒绝，文件以排他方式创建。失败时回收本次已写文件，不覆盖旧部署。交换所的未使用系统账户/空凭据目录可能保留，需操作者检查。

只安装文件，默认不启用任何系统助手。

### 功耗/核心频率

```bash
sudo systemctl enable --now gpu-tune.socket
sudo systemctl enable miner-dashboard-tuning-restore.service
```

大屏通过受限 socket 写入，权限由 `/etc/miner-dashboard/hardware.json` 的 allowed_users 与 socket 所有者共同控制。`/var/lib/gpu-tune/state.json` 保存 UUID 对应的功耗/频率。大屏保存的参数才会在启动恢复；未设置的卡保持驱动出厂策略。没有隐含 280W/260W 默认值。

第一次启用可执行 `sudo /usr/local/sbin/gpu-tune apply` 检查结果。开机恢复单元在指定用户管理器之前运行；若驱动尚未就绪可能失败，需看 journal 并针对自己的驱动安装增加等待/依赖，不能把 unit enabled 当作成功。更换显卡后 UUID 变化，需要重新登记，未知卡不会继承另一张卡的设置。

主板风扇默认无映射。只有核对 hwmon 芯片、物理风扇与 PWM 通道后才能手动填写 fan_groups。不要照抄别人主板的通道；RGB 不调风扇转速。GPU 功耗/频率应在显卡支持范围内，接头发热需要停机检查供电路径。

### RGB / SafeTrade

分别见 [RGB](RGB.md) 和 [交易所](EXCHANGE.md)。这两项不要求启用功耗助手。

## 5. 远程入口

见 [HTTPS 与 SSH](REMOTE_ACCESS.md)。外网只代理认证网关 8092，核心 8091 保持回环。账号密码由操作者交互创建，没有默认密码。

## 6. 真实重启验收

完成配置后，选择可接受中断的时间执行系统正常重启。验证：

1. SSH 恢复；`loginctl show-user "$USER" -p Linger` 显示 yes。
2. `systemctl --user is-active` 检查大屏、网关和每个矿工；查看 journal 排除重复实例和反复重启。
3. `nvidia-smi` 中每张 UUID 对应实际卡；功耗上限 readback 与保存值一致；矿工计算进程覆盖目标卡。
4. 等待新算力采样与本次启动后的新 accepted 份额，最好再看独立矿池 worker 页面。
5. 外网用新浏览器访问 HTTPS：未登录不能读 API，登录后能看数据并执行已启用的可逆控制。
6. RGB 颜色与开关恢复，并现场目视确认呼吸。SDK 缓存或 ACK 只能证明已发指令。

来电自启另需 BIOS/UEFI 的 Restore AC Power Loss 设置和现场断电测试；网络搬迁另验 DNS/TLS/隧道/路由器。不通过时按 [排错](ARCHITECTURE.md) 逐层检查。

## 7. 升级 / 回退 / 卸载

升级前：保存当前 commit，备份私有配置、历史及系统助手配置到私有目录。查看 `git diff 旧commit..新commit`，运行测试，再重启大屏。大屏重启不应重启矿工。

root 助手升级：停相关助手、把旧 `/opt/miner-dashboard` 和 `/usr/local/sbin/gpu-tune` 私有备份，检查新候选后用 root 替换所需代码，保持 root:root、目录755/文件644/可执行助手755。不要将 root unit 指向普通用户可写的 Git clone 或虚拟环境。RGB 虚拟环境也必须 root 拥有。失败时恢复备份再启服务，不回灌旧钱包或功率历史。

卸载：停用并删除本项目 user units；停用 `gpu-tune.socket`、tuning-restore、exchange、rgb 等已安装单位，移除相应 unit/助手/opt 目录，daemon-reload。不会自动删除矿工、用户私有配置或交易所凭据；由操作者按需处理。若启用了 linger，要先核对其他用户服务再决定关闭。

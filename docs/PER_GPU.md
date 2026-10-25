# 每张显卡独立启动与停止

在“挖矿控制”中，每张已绑定的卡有 **启动本卡 / 停止本卡**。停止只停止该UUID的独立矿工服务、禁用其开机启动；其他卡服务和功耗设置保持原状。再次启动沿用上次选择的币种，并启用其开机启动。多币种卡第一次需要选择币种，之后会记住。

停止的是软件矿工，显卡仍然上电。更换供电线、插拔显卡前仍需正常关机断电。

## 为何需要独立服务

一个进程同时挖几张卡时，杀掉它会连带停止其他卡。该配置会在网页显示原因并禁止误操作。使用下方生成器建立一张卡一个进程/用户服务后，就能真正独立启停，不通过把功耗降到很低来伪装停矿。

生成器不会下载矿工、不索要交易所API、不修改功率/风扇、不启停任何服务。它只创建本机私有配置和独立systemd用户服务。支持WildRig的PCI设备选择，以及原生支持GPU UUID参数的矿工；其他矿工可手动登记已有独立服务。

## 1. 准备自己的私有矿工配置

复制 `examples/per-gpu-profile.example.json` 到源码目录以外，例如 `~/.config/miner-dashboard/per-gpu-profile.json`，设600权限，再填写**自己已验证的矿工可执行文件、矿池和收款地址**：

```bash
mkdir -p "$HOME/.config/miner-dashboard"
cp examples/per-gpu-profile.example.json "$HOME/.config/miner-dashboard/per-gpu-profile.json"
chmod 600 "$HOME/.config/miner-dashboard/per-gpu-profile.json"
```

示例选择WildRig：`selector: wildrig`。argv是参数数组，不经过shell。不要填写 `-d/--gpu-list`、`--opencl-platforms`、固定API端口或公共日志路径，它们需要逐卡隔离。启动器会：

1. 以UUID查NVIDIA当前PCI身份；GPU临时索引变化不会改变目标卡。
2. 使用同一已解析的矿工可执行文件列出NVIDIA设备，通过唯一PCI bus匹配矿工编号。
3. 只传入该卡 `--gpu-list`，允许multiple-instance。
4. 若卡缺失、PCI映射歧义或卡上已有计算任务，拒绝启动该进程，不杀其他任务。启动后监督进程每秒核对本unit的实际GPU使用；卡掉线、PCI身份变化、无法观测或进程用到其他卡时，终止本unit自己创建的子进程组。开机自启也走同一路径。

设备枚举格式变化时会拒绝猜卡号。纯数字busID可能有进制歧义（如10与PCI 0a/10），同时匹配两张卡时直接拒绝；启动期间库存变化也拒绝。数字选择器仍有初始化时序窗口；一秒监督用于发现和终止错卡使用，不能证明任意瞬间零误用。运行中不要热插显卡或重载驱动，换卡需先停止矿工/关机。只提供busID的矿工遇到同bus多GPU时，也会拒绝。另一个 `selector: uuid` 模式需要矿工本身支持UUID，argv内用一个独立 `{device}` 参数，启动时替换成目标UUID。先按矿工官方说明验证其选择器。

profile文件虽为600，argv仍可能被本机ps、/proc或同UID程序读取。不要把交易所API key、真实密码或敏感token放进argv；示例只使用钱包/worker与无敏感意义的矿池占位口令x。需要私密口令时使用矿工支持的600配置文件机制。

示例没有功耗/超频参数。希望保持大屏保存的功耗时，不要另加矿工重置超频/功率的参数。默认生成TH/s日志模板；使用其他算法/矿工时要按实际日志修改rate.unit和正则。

## 2. 生成计划并安装私有文件

先用 `nvidia-smi --query-gpu=index,uuid,name,pci.bus_id --format=csv` 核对每张实体卡，再把真实UUID代入命令。下方全零UUID是占位：

```bash
python3 scripts/setup_per_gpu.py \
  --profile "$HOME/.config/miner-dashboard/per-gpu-profile.json" \
  --symbol PRL \
  --uuid GPU-00000000-0000-0000-0000-000000000001 \
  --uuid GPU-00000000-0000-0000-0000-000000000002
```

默认只输出计划。核对后再添加 `--write`：创建两份独立用户unit、rig-control.json和coins.d/PRL.json，不启动/enable矿工。每个unit和日志路径由UUID摘要生成，映射保存在私有配置中。配置、unit为600，日志目录700。

已有目标会拒绝覆盖。对现有多卡部署升级，先把原rig-control.json和对应coins.d文件备份到私有目录并移出目标路径，**保留自己的报价/账户/网络feed和CPU/其他币种配置**。生成后把这些配置合并回去，并运行两种校验：

```bash
python3 app/mining_monitor.py --check-config
python3 app/rig_control.py --config "$HOME/.config/miner-dashboard/rig-control.json" \
  --state-dir "$HOME/.local/state/miner-dashboard/rig-control" --check-config
```

新coins配置使用 `gpu_uuid_services` 与 `rate.log_by_uuid`。监控每次按当前UUID解析索引，独立日志即使都写GPU0也不会串到同一张卡。

## 3. 从共享矿工迁移

在可接受短暂停矿的窗口，先停用旧的共享矿工及会自动复活它的healthcheck/timer。使用你自己的实际unit名称，不照抄别人的服务名。

```bash
systemctl --user disable --now YOUR_OLD_SHARED_MINER.service
# 同时核对/停用相关自动恢复timer；不要停CPU矿工或其他应用。
systemctl --user daemon-reload
systemctl --user restart miner-dashboard.service
```

大屏已按INSTALL.md开启 `MINER_DASHBOARD_CONTROLS=1` 后，在“挖矿控制”逐卡点击“启动本卡”。不需要每次创建unit。普通单卡停启只影响选中卡；**初次由共享进程改为独立进程存在一次迁移中断**。

已有独立单卡unit时，可手工合并rig与coin配置，无需此生成器。保留原持久功耗助手、网页认证、SSH和RGB；它们与矿工服务拆分独立。

## 4. 验收与回退

- 每张GPU计算进程应归属各自unit；等待新的算力和有效份额，不能只验active。
- 停一张卡：该unit inactive/disabled、该卡没有它的计算进程；另外各卡unit的MainPID、算力和份额继续更新。
- 再启动：仅该unit重新运行/enabled，功耗readback仍为该UUID保存值。
- 重启大屏或重新创建控制器后，停挖状态和上次币种仍在；整机重启按各unit的enable/disable恢复。真实主板来电启动需另行现场验收。
- 配置失败：先停用本次生成的独立unit，再恢复原私有rig/coins配置和原共享unit的启动选择，reload并启动原矿工；绝不让新旧矿工同时抢卡。

演示模式可实际点击逐卡按钮，但只改变隔离的合成状态，不启动硬件。仓库CI另用3个真实systemd服务、逐卡监督器和模拟矿工子进程，验证停/开一张卡时另外两个监督器及矿工PID均不变；NVIDIA库存/计算遥测为合成fixture，这项验证不冒充真实矿池份额或硬件冷启动。

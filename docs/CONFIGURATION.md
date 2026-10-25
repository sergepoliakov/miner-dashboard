# 绑定 GPU、矿工、矿池和行情

## 配置目录

默认 `~/.config/miner-dashboard/`，可用 `MINER_DASHBOARD_CONFIG` 覆盖。历史默认 `~/.local/state/miner-dashboard/`，可用 `MINER_DASHBOARD_STATE` 覆盖。凭据不放进 clone。

| 文件 | 用途 |
| --- | --- |
| coins.d/PRL.json 等 | 币种、用户服务、日志解析、可选矿池/网络/行情字段 |
| rig-control.json | UUID → 币种 → systemd unit；允许管理的 unit、更新适配器 |
| gpu-display.json | 固定显示编号与名字，不随 NVML 临时索引变动 |
| market-public.json | 仅关注、未在挖的币种行情（数组）；不需要钱包 |
| active-gpus.json | 可选 excluded_gpu_uuids 与 exclusion_reasons；只影响显示，不切断供电 |
| rgb.json | RGB enabled/color，程序原子保存；不可配置呼吸周期 |
| mining-profiles.json | 可选收款适配器白名单，包含本机矿工字段/服务登记；私有600文件 |
| web-auth.json | 密码哈希、会话签名随机量；私有600文件 |

先运行 `configure.py init`。`discover` 会发现当前 GPU 并创建布局；已有布局会拒绝覆盖。手动编辑 number/name，将 UUID 对应到实际安装位置。不应凭风扇/卡号猜物理身份。插槽、卡序改变后重新核对 `nvidia-smi --query-gpu=index,uuid,name,pci.bus_id --format=csv`。新增卡需显式加入配置；没有自动认领任意卡的默认规则。

## 每张显卡独立启停

推荐按[逐卡配置指南](PER_GPU.md)运行setup_per_gpu.py，为每张卡建立独立unit和UUID遥测映射。下方bind只用于已存在服务的基础配置。新格式gpu_uuid_services/log_by_uuid会自动随索引变化解析；旧gpu_services/log_by_gpu索引映射仍保留兼容。

## 快速绑定已有服务

以**一个 UUID 一个 unit**为推荐起点。示例里的 UUID 全零为占位，必须换成本机实值，只存本机文件：

```bash
python3 scripts/configure.py bind \
  --symbol PRL \
  --service your-gpu-miner.service \
  --uuid GPU-00000000-0000-0000-0000-000000000001 \
  --log /var/tmp/example-miner.log
```

该命令不会启动矿工，也不会覆盖已有绑定。生成的日志正则是示例 TH/s 格式，**必须按自己的矿工日志修改并核对**。多卡/多币后续手动合并 `gpus`、`managed_services`、`coins.d`，相同卡不能被多个活动矿工占用。配置校验命令见安装指南。

`rate` 必需：绝对 log 路径、unit、gpu_regex（命名组 gpu/value）、total_regex（value）、table_start_regex。支持 max_age_seconds、max_trailing_lines。只露一张卡时日志可能总写 GPU0，使用 gpu_map 把日志编号映射到 NVML index。不同币种不可混用 TH/s、MH/s 或 Sol/s。

**稳定身份有不同职责：**功耗状态与显示排序使用 UUID。矿工选择卡的参数也应绑定 UUID，确认该矿工支持；日志侧 gpu_indices/gpu_map 使用 NVML 索引，换槽/加卡/驱动重排后要重新核对并更新，不可把旧日志索引当物理身份。未验证映射前不要通过网页切币。

只启用已管理服务；不要把他人的训练/绘图服务加入白名单。多卡共用一个矿工时逐卡切换会拒绝，防止误停其他卡；需要网页独立启停时先按逐卡指南迁移。CPU NOID 单独登记为 `kind: cpu`、`service: noid-cpu-miner.service`、`gpu_indices: []`；控制脚本可安装为 `~/.local/bin/noid-cpu-control`（将 app/noid_cpu_control.py 复制并 chmod755），矿工本身另行安装。

## 收款地址与矿池

钱包/交易所充值地址先由你核对网络、币种、最低充值、Memo/Tag 与矿池付款规则。矿工私有配置负责收款，**绑定 API 不会替你修改矿工收款地址**。没有真实地址的示例不能启动挖矿。

监控可用 `address_source: {"file": "/绝对路径/私有矿工.json", "json_path": "wallet"}` 从本机私有文件读取地址，再将它代入矿池 HTTPS URL 的 `{address}`。可用 `split` 去掉特定 worker 后缀，需按矿工格式核对。该监控会向你配置的矿池发送地址；不要把含地址的 URL 或配置公开。示例不含任何真实地址。

支持的 feed 类型：price、pool、account、earnings、daily、network。通用 JSON 路径如 `data.price`；数组路径/专门 earnings parser 以 `app/mining_monitor.py` 的 validate_config、parse_* 为准。每个 feed 必须含 HTTPS url、source 和对应字段路径；过期会显示不可用，不把失败当零余额。网络收益需要哈希单位、奖励、出块时间/难度、费率；手填协议参数要附来源与日期。不是所有矿池 schema 都已适配。

## 行情与成本

`examples/market-public.example.json` 是无账户的关注币种示例，复制到私有配置目录后可修改。报价 ID/币种名必须对照官方源，避免同名币；免费行情 API 可能有频控。需要更多币种时逐个增加数组项，先验证 API 返回与字段路径。接入实际在挖项目时，在对应 coins.d 配置中增加相同 feed。

电价：`MINER_ELECTRICITY_CNY_PER_KWH`（默认0.6元/kWh，仅示例）。主机余耗默认估算90W，可在源码常量调整；没有整机电表时明确标为估算。账面 credit、矿池待付、交易所余额和真实到账分开显示。短期理论收益不可当已到账收入。

## 更新适配器

rig-control.json 的 miners 默认空，不会自动更新未知程序。源码实现 WildRig GitHub、QTC、NOID 等有界下载/校验逻辑；启用前阅读对应 `_update_config` schema，填写自己的 versions_dir/current_link/healthcheck 等绝对路径并核验供应方。签名公钥 SHA256 pin 属于公开信任根，不应当作秘密删掉或随意绕过。第三方矿工不在仓库内，不默认接受其许可条款。

## 收款地址与 worker

按币种选择钱包、保存草稿和显式应用的界面，以及私有适配器的完整配置见 [钱包地址与矿工身份](PAYOUTS.md)。未登记矿工只保存草稿；SafeTrade API 继续独立绑定。

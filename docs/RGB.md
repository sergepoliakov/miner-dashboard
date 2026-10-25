# RGB 颜色与固定呼吸

网页只允许改 `enabled` 和 `color`（#RRGGBB）。呼吸固定7.5秒、20%底光，所有目标共用时间相位。开关/颜色原子保存；服务重启读取原状态。不会改变风扇转速，也不提供改变周期、亮度或静态模式的接口。

## 依赖和选择设备

从 [OpenRGB 官方](https://openrgb.org/) 安装适合硬件的版本，先本机确认设备可识别并支持 Direct。SDK只监听127.0.0.1:6742，不开放公网。设备探测及I2C访问要按官方硬件支持说明操作，不能假定所有品牌都支持。

首次助手安装加 `--rgb`。创建 root 拥有的可选虚拟环境：

```bash
sudo python3 -m venv /opt/miner-dashboard/rgb-venv
sudo /opt/miner-dashboard/rgb-venv/bin/pip install --require-hashes --no-deps -r /root/miner-dashboard-release/requirements-rgb.txt
```

编辑 root拥有、不可被普通用户写的 `/etc/miner-dashboard/rgb-hardware.json`：

- state_file：挖矿用户私有配置目录里的 rgb.json 绝对路径。
- device_names：OpenRGB实际识别的目标完整名称列表。默认空列表，守护不会对任何设备写入。
- msi_trio_uuid：默认null；仅下文原生适配器需要。

不自动重设ARGB灯珠数量，不自动发现并写所有总线，不循环保存硬件flash。启动前先在大屏保存一次颜色/开关，然后：

```bash
sudo systemctl enable --now miner-dashboard-rgb.service
sudo journalctl -u miner-dashboard-rgb.service -n 30
```

需要已有的 openrgb.service；若实际unit名称不同，修改依赖。root助手和venv必须root拥有，不能从用户可写目录导入代码。默认脚本在读取不到有效状态时关灯；上次有效状态读取失败时保留上次有效值。

## MSI GAMING X TRIO 特例

OpenRGB SDK缓存可能显示颜色已改变，实物仍静态。可选原生路径只允许：NVIDIA PCI `10de:2704`、微星子系统 `1462:5111`、名称 `MSI GeForce RTX 4080 Gaming X Trio`。同时核对配置UUID→PCI地址→SDK I2C位置→sysfs祖先，任何不匹配拒绝。

确认**自己的实物型号匹配**后才填写本机 msi_trio_uuid，不能使用示例/别人的UUID。每帧按STATIC后RGB发送，间隔至少120ms，不循环Save/flash，不做每帧IDLE复位。该路径仍属于特定硬件兼容实现，不泛化到其他微星卡；不支持时保持默认null。

协议核对来源见 [第三方说明](../THIRD_PARTY.md)。实际灯效必须现场目视：缓慢平滑呼吸、低点有微光、风扇/显卡同相；`/run/miner-rgb-status.json`的ACK只证明指令已经发送，不能证明灯珠真的执行。先服务重启，再在可接受停矿的窗口做整机重启验收。

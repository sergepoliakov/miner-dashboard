# 第三方来源与分发边界

本仓库自身代码以 GNU GPL v3.0-only 提供，Copyright (C) 2026 sergepoliakov。许可证全文见 LICENSE。来源为作者自用矿机管理代码的脱敏导出；原有私人配置、Git历史、现场照片和账户资料没有导入。

| 项目 / 来源 | 用途 | 分发情况 |
| --- | --- | --- |
| [OpenRGB](https://openrgb.org/) | 可选硬件灯控与SDK服务 | 不捆绑二进制；操作者独立安装并遵守其GPL许可证 |
| [OpenRGB-Python](https://pypi.org/project/openrgb-python/) / [源码](https://github.com/jath03/openrgb-python) | 可选RGB SDK Python客户端，GPLv3 | 仅hash固定的requirements，不复制依赖源码 |
| [OpenRGB MSI v2 控制器](https://github.com/CalcProgrammer1/OpenRGB/blob/8e16de2/Controllers/MSIGPUController/MSIGPUv2Controller/RGBController_MSIGPUv2.cpp) | MSI寄存器/不能从硬件回读颜色的协议核对 | 原生适配器有此来源参考，不包含完整第三方控制器 |
| [SignalRGB Lovelace协议对照](https://github.com/foresterfx/SignalRGB-Plugins/blob/main/SignalRGB%20Default%20Plugins%20%5Bmodified%5D/MSI_Lovelace_Series_GPU.js) | 仅核对STATIC/RGB寄存器及次序 | 不捆绑或执行SignalRGB插件；本仓库Python代码独立实现 |
| [SafeTrade example-client](https://github.com/safetrade-exchange/example-client) | API路径、HMAC协议形状参考 | 不捆绑密钥/账户，不直接执行其示例交易 |
| [WildRig](https://github.com/andru-kun/wildrig-multi) / [INVminer](https://github.com/getrigeos/INVminer-Release) / [QuanPool](https://www.quanpool.com/) | 按配置可选的矿工更新/日志接口 | 无矿工二进制，矿工许可证/开发费由供应方决定 |
| [NVIDIA](https://docs.nvidia.com/deploy/nvidia-smi/) | nvidia-smi遥测与调节 | 不捆绑驱动或CUDA |
| [Gitleaks](https://github.com/gitleaks/gitleaks) | 开源前secret扫描 | 发布开发工具，不捆绑扫描器 |

官方项目、交易所和矿池没有为本项目背书。各商标归其所有者。收益与算力DEMO数据为合成值，不引用真实账户。

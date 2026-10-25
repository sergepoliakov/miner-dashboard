# 绑定 SafeTrade 交易所

当前只有 **SafeTrade** 适配器。Binance、OKX 等没有实现；矿工钱包绑定与交易所 API 绑定是两件独立的事。可以只看矿工/行情，完全不接交易所。收款地址在[矿池与收款](PAYOUTS.md)设置；选择 SafeTrade 地址来源只是标注，不会绑定 API。

实现参考 [SafeTrade 官方 API 说明](https://safetrade.com/api) 与 [官方 example-client](https://github.com/safetrade-exchange/example-client)。BASE为 `https://safe.trade/api/v2`，签名为 nonce + API key 的 HMAC-SHA256。交易所接口、交易对和地区可用性会变化，需以官方当时的账户页面/API为准。

**验证状态：**本版完成签名、凭据权限、Unix broker、金额/交易对/余额校验和一次性订单预览的模拟测试。尚未完成真实账户数据/真实资金卖单验收；原测试环境曾被上游 Cloudflare 拦截。不能把“API已绑定”或mock通过当作真实下单成功。不通时不会把未知余额当零，也不绕过交易所访问控制。

## 1. 自己创建最小权限 API key

在自己的 SafeTrade 账户安全/API管理中操作（具体菜单以官方页面为准）：

- 先仅授予查询账户/余额/订单需要的读取权限。
- **不启用提现权限**。本项目没有创建提现、修改收款人或转账操作。
- 若交易所支持来源 IP限制，填写实际出口；不要使用别人给的凭据。
- 密钥只在本机隐藏输入；网页界面不接收/返回明文 key/secret。

矿池直接打到交易所充值地址前，还需独立核对币种网络、充值是否开放、最低金额、Memo/Tag和矿池付款频次。API绑定不会完成这项核对，也不改变矿工收款地址。

## 2. 安装隔离 broker

先按 [安装文档第4节](INSTALL.md) 建立并审阅root拥有的stage。在首次系统助手安装时选择 `--exchange`（可与 --tuning/--rgb 同次）：

```bash
sudo python3 /root/miner-dashboard-release/scripts/install_system.py --user "$USER" --exchange
sudo python3 /root/miner-dashboard-release/scripts/exchange_credentials.py
sudo systemctl enable --now miner-dashboard-exchange.service
```

脚本创建专用 `safetrade-api` 用户，凭据保存 `/var/lib/safetrade-api/credentials.json`，目录700/文件600，归专用用户所有。socket `/run/safetrade-api/api.sock` 仅允许配置的本机用户组访问；普通大屏进程无法读取凭据。该组应只包含受信任用户。root代码位于 `/opt/miner-dashboard/app`，不要指向用户可写 clone。

若已有 `/opt/miner-dashboard`，安装器会拒绝覆盖。按 INSTALL.md 的升级方式把缺少的单元加入，保留已有配置，不能用删除生产目录来规避检查。

为用户大屏服务添加 drop-in：

```ini
# ~/.config/systemd/user/miner-dashboard.service.d/exchange.conf
[Service]
Environment=MINING_MONITOR_EXCHANGE=1
```

创建父目录后保存，执行 `systemctl --user daemon-reload` 与 `systemctl --user restart miner-dashboard.service`。网关不持有交易所密钥。默认账户刷新约每5分钟；先核对余额、币种和订单字段与交易所网页一致。

默认直接验证TLS访问。若自己需要合法的 SOCKS5 出口，broker 可选 `SAFETRADE_SOCKS_PORT`（loopback端口）；默认0不使用。不要上传代理凭据/节点信息。公共行情诊断与broker出口不同时可能结果不同，按实际账户响应判断。

## 3. 默认只读与可选限价卖出

默认 broker 的 `MINER_DASHBOARD_ALLOW_TRADING=0`；核心同样默认禁止提交，前端禁用卖出表单。即使key本身有交易权限，默认配置也会在调用任何订单API前拒绝卖单。

只有操作者先完成真实账户只读核验、明确理解交易权限后，才自行：

1. 在交易所为专用key开放所需现货交易权限，仍禁提现。
2. 在 **系统 broker unit** drop-in 与 **用户 monitor unit** drop-in 同时设置 `MINER_DASHBOARD_ALLOW_TRADING=1`。
3. 确保核心的 `MINER_DASHBOARD_CONTROLS=1`；分别daemon-reload并重启broker、monitor。
4. 用操作者自行决定的最小测试数量做预览，核对交易对/数量/价格/金额，再亲自在页面确认。以交易所订单ID、订单记录、成交记录核验。

支持集合目前限定 `prlusdt / prlbtc / tscusdt / tscbtc`，且必须在交易所实时返回中存在。不可用/下架时拒绝，不能强行把同名币当同一资产。仅**限价卖出**，不支持买入、自动策略或提现。一次性预览令牌、正十进制、实时可用余额和交易对检查仍然保留；没有自动卖币守护程序。

## 4. 撤销与排错

停止broker和monitor的exchange刷新，去交易所撤销key，再由操作者清理私有凭据。仓库不提供凭据下载或导出接口。

- `credentials_missing`：凭据未配置；不要把它复制到源码目录。
- 文件权限错误：确认专用用户拥有600文件，不能改644。
- 401/403：核对官方API权限、时间、出口与上游策略；日志不打印签名或secret。
- `endpoint_unverified`：没有确认可用端点，不能推断账户为空。
- `trading_disabled`：默认只读在生效；不需要交易时保持如此。

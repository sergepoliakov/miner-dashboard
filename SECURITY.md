# 安全与发布脱敏

## 默认边界

- 核心/演示/网关都只监听loopback；公网只接HTTPS认证网关。
- 核心默认只读；写入要求显式开关、同UID、Origin/CSRF及操作白名单。
- 交易所默认关闭，broker默认只读，不实现提现；凭据专用用户600文件，不回传浏览器。
- root助手与其Python代码/venv/root硬件配置不可由大屏用户修改。用户可写输入仅为受限JSON/Unix请求。
- 不含默认账号密码、SSH钥匙、矿池钱包、交易所key、隧道token、真实机器配置、GPU身份或生产Git历史。

## 开源包如何脱敏

从允许的源码白名单重新建立独立仓库，运行配置与历史不导入。路径/身份改为XDG和本地硬件profile。文档示例用保留的example.com、全零合成GPU身份与占位地址。图片由独立DEMO生成，显式显示DEMO标记，未使用生产截图打码。公开Git作者使用GitHub noreply地址。

`python3 scripts/check_privacy.py` 检查常见私有路径、内网地址、GPU身份、密钥模式与运行文件。它不替代secret扫描器或人工审图。发布时还应使用 [Gitleaks](https://github.com/gitleaks/gitleaks) 对工作树与Git历史运行 `gitleaks dir . --redact`、`gitleaks git . --redact`；只查看不含原文的定位报告。

.gitignore仅作辅助，显式force-add仍可提交敏感文件。不要复制自己的config、state、备份、原始日志、浏览器profile或钱包进入clone。新增示例要用合成数据，新图必须重跑DEMO。提交前核对 `git diff --cached` 和追踪文件列表。

## 报告问题

请使用仓库 GitHub **Security → Report a vulnerability** 的私有漏洞报告（如维护者已开启）。不要在公开issue粘贴真实凭据或漏洞利用所需的敏感材料。若误公开凭据，先在对应服务撤销/轮换，再处理Git历史；删当前文件不能撤销已泄露秘密。

首版目标为可信个人矿机管理，不提供多租户隔离、企业RBAC或互联网暴露认证。其他同UID进程、root或宿主机失陷超出网页会话的保护范围。

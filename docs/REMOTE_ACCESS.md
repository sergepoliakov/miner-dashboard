# 账号密码、HTTPS 与 SSH

核心 8091 的 GET 遥测没有网页登录鉴权，仅用于受信本机或 SSH 转发；写接口另有同 UID/Origin/CSRF 检查。不要把核心端口直接暴露到公网或普通局域网。

## 创建登录并安装网关

以挖矿用户运行，密码在终端隐藏输入，无默认密码：

```bash
python3 scripts/configure.py web-auth --username admin
python3 scripts/install_user.py --host miner.example.com --web-only
sudo loginctl enable-linger "$USER"
```

以上假设已按INSTALL.md安装核心服务；`--web-only`只新增网关。尚未安装核心时可改用 `--host miner.example.com --enable-controls` 同时安装两个服务。安装脚本拒绝覆盖已有unit。网关监听127.0.0.1:8092，`MINER_DASHBOARD_HOST` 必须与实际 HTTPS 域名一致，校验 Host 与 Origin。上游核心/网关以同一个普通用户运行。

在自己的域名创建 DNS 并部署 HTTPS 反向代理。例如已安装的 Caddy 可用：

```caddyfile
miner.example.com {
    reverse_proxy 127.0.0.1:8092
}
```

也可使用自己的 Cloudflare Tunnel，把域名源指向8092，并保留应用账号密码。仓库没有任何 Tunnel token、Zone/Account ID 或生产域名。Tunnel/Access 的账户配置由你自己完成，不会自动移除其他安全策略。不要代理8091，也不要公开 OpenRGB SDK6742或Unix socket。

Secure/HttpOnly/SameSite=Strict 的 `__Host-` cookie 需要 HTTPS；HTTP 直连网关不是远程部署方式。登录会话存本机 SQLite，可跨网关重启，14天到期；换设备首次登录仍需密码。反向代理要正确保留原始 Host。登录限制只用于减缓尝试，不是通用反爬/防攻击服务。

## 没有公网入口时

使用已正确配置的 SSH：

```bash
ssh -L 8091:127.0.0.1:8091 your-user@your-host
```

浏览器打开 http://127.0.0.1:8091，只在 SSH 隧道内使用。SSH 连接不等于网页核心的同 UID 写权限一定可用，具体取决于目标 sshd 转发进程 UID与 `/proc/net/tcp` 可见性。失败时使用 HTTPS 网关，不关闭 UID检查。

SSH 是操作系统服务，与大屏分开部署。密码或密钥由你选择并维护，仓库不修改 sshd、不生成/收集 SSH 凭据。搬迁到新网络后，需要 VPN/SSH中继/Tunnel等可达方案；家庭内网IP不会在外网自然可达。

## 验证

未登录访问 `/api/status` 应401；错误 Host400；错误 Origin 的 POST403；登录后应看到新鲜采样。不要只验首页200。设置颜色等可逆动作应回读生效，再核对矿工无意外重启。若浏览器跨域或跨站，使用正确域名，避免放宽 Origin/CSRF 来解决配置错误。

密码重置：停止网关，把旧 web-auth.json 私有备份并移出原路径，再交互运行 configure.py web-auth。新 session_key 会使旧 cookie 失效；删除旧 sessions.sqlite3 后重启网关。不要在 issue/日志中贴哈希、cookie、盐或签名密钥。

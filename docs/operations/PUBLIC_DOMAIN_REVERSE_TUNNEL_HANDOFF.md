# 本地服务通过云服务器和域名对外发布：SSH 反向隧道与 Nginx 操作手册

**文档用途**：让读者只凭本文，就能理解“域名 → 云服务器 Nginx → SSH 反向隧道 → 本地 HTTP 服务”的原理、现有实例的连接方式，以及如何在同一台云服务器上为自己的服务建立独立入口。读者不需要了解现有网站的业务或代码。本文记录的新增配置是实施范例，不表示新服务已经部署；共享服务器的资源分配和变更由其管理员负责。

**当前链路（2026-10-24）**：本网站正式链路为中继 `18101` → Mac `8101`；原 `18100` → `8100` 回退链路已于 2026-10-24 退役。`18100`、`18101` 均不分配给新服务；第 3 节起的新服务示例使用 `18102`，仍须实际核实和分配。本网站自身的运维命令见[部署访问入口](DEPLOYMENT_ACCESS.md)。

**现场核验**：中继端（Nginx、sshd、证书、软件版本、主机指纹）为 2026-09-26 读回值，其后仅 Nginx 上游于 2026-10-18 由 `18100` 改为 `18101`；Mac 端隧道参数为 2026-10-24 读回值。实施前按本文命令重新核验；示例域名、用户名和端口均须替换为管理员实际分配的值。

**交接包内容**：交接 ZIP（`public-domain-reverse-tunnel-handoff-20260926.zip`）根目录含本文与中继 `root` 管理私钥 `finlab-key.pem`。ZIP 本身未加密，只交给获授权的同事并经受控渠道传递，按含私钥的凭据包保管；停止授权时由中继管理员撤销对应公钥并按实际信任范围轮换。该私钥只用于配置中继，不用于新服务的长期反向隧道（新隧道按第 4.3 节创建独立账号和密钥），也不放入 Git、公开下载地址、聊天正文或服务器上的交接文件。

**适用范围**：本地 HTTP 服务运行在 macOS 或 Linux，主机能主动通过 SSH 连接公网 Ubuntu 中继；公网以 HTTPS 提供访问。按文中可选配置支持 WebSocket。Windows 的服务管理命令，以及原始 TCP、UDP、gRPC 等非普通 HTTP 场景不在本文范围。任意应用的安装、业务鉴权和数据备份仍由该应用负责人完成；本文负责把已经可在本地访问的应用安全地转发到域名。

**完成标准**：新域名正确解析并通过可信 HTTPS 证书访问；云服务器上的新反向端口只监听 `127.0.0.1`；本地应用、云服务器反向端口、公网域名三处探针逐段成功；本地隧道重启后能自行恢复；原有域名仍正常。按本文第 4 节的顺序实施，第 5 节验收，失败时按第 6 节定位和回退。

## 1. 一张图看懂这套方法

```text
浏览器
  │  HTTPS：域名、443
  ▼
公网中继 ECS：Nginx，负责 TLS、域名/路径路由、访问边界
  │  HTTP：127.0.0.1:<中继反向端口>，不对公网监听
  ▼
公网中继 ECS：sshd 创建的反向端口 listener
  ║  已认证、加密的 SSH 连接；由本地电脑主动连向 ECS
  ▼
本地电脑：ssh -R 进程
  │  HTTP：127.0.0.1:<本地应用端口>
  ▼
本地应用进程及其本地数据
```

核心是把两个问题分开：**Nginx 决定哪个公网请求可以进入哪一个服务；SSH `-R` 负责把中继上的一个仅本机可访问端口连回本地应用。** 本地电脑只需能主动连出 SSH，不必有公网 IP，也不必把应用端口暴露给互联网。请求和响应沿同一条连接往返。DNS 只把域名指向中继，不会直接指向本地电脑；TLS 在中继 Nginx 结束，Nginx 到本地应用的中间段由 SSH 加密。

这不是把云服务器变成应用宿主。中继端只负责 Nginx、sshd、证书与日志；应用仍由本地电脑负责。中继、隧道、本地应用任何一段不可用，公网访问都会受影响。TLS 在中继终止，所以**中继管理员和中继进程属于可接触明文 HTTP 的信任边界**；SSH 保护的是中继到本地电脑这一段。若应用要求连中继都无法接触明文，需要另行设计端到端加密，不应把本方案误认为端到端 TLS。

## 2. 现有实例：地址、文件和行为

下表是本网站在这台中继上的现有配置，供理解链路和排查时对照。新服务不能复用它的 SSH 密钥、反向端口或 Nginx `server_name`。

| 位置 | 现场值与作用 |
| --- | --- |
| 对外入口 | `https://bond.finailab.cn/bond-factor-lab/` |
| DNS | `bond.finailab.cn` 的 A 记录指向 `101.132.143.185` |
| 公网中继 ECS | `101.132.143.185`，Ubuntu 24.04；Nginx 监听 80/443 |
| 中继 Nginx 站点 | `/etc/nginx/sites-enabled/bond-factor-lab` → `/etc/nginx/sites-available/bond-factor-lab` |
| Nginx 上游 | `127.0.0.1:18101`；由中继 `sshd` 监听，只开放 loopback |
| 本地电脑的反向隧道 | `ssh -B en1 -N -T ... -R 127.0.0.1:18101:127.0.0.1:8101 root@101.132.143.185`；Mac LaunchAgent `com.bond-factor-lab.cutover-candidate.ssh-tunnel` 管理 |
| 本地 HTTP 服务 | Mac 的 `127.0.0.1:8101`；由本地进程提供，不安装在中继 ECS |
| TLS | Nginx 使用 `/etc/letsencrypt/live/bond.finailab.cn/{fullchain,privkey}.pem`；Certbot 续期任务由中继 `certbot.timer` 管理 |

Mac 发起的是 SSH **反向**转发：中继端 `18101` 映射到 Mac 的 `8101`。中继 `GatewayPorts no`，且命令显式使用 `127.0.0.1:18101`；因此公网只能进入 Nginx 的 80/443，不能直接连反向端口。中继 `sshd` 允许 TCP forwarding，使用公钥登录并关闭密码登录。现有连接以 `root` 运行是现场事实；新服务仍应由管理员分配独立 SSH 身份。

Nginx 根据 HTTPS 请求的域名选中 `bond.finailab.cn` 的 `server` 后，只把精确列出的页面、静态资源和 API 交给上游；未知路径拒绝。转发前把 `/bond-factor-lab` 前缀去掉，例如公网 `/bond-factor-lab/api/health` 到本地服务时是 `/api/health`。`rewrite ... break` 在当前 location 内改写请求路径，随后 `proxy_pass` 把改写后的路径送往上游。80 端口将正常 HTTP 请求重定向到 HTTPS。

路径转发的关键语义可压缩为以下片段；这里只展示请求路径转换，省略了站点的 TLS、请求头和其他路由：

```nginx
upstream local_service { server 127.0.0.1:18101; }
location = /bond-factor-lab/api/health {
    rewrite ^/bond-factor-lab(/.*)$ $1 break;
    proxy_pass http://local_service;
}
```

隧道使用 `ExitOnForwardFailure=yes`、`StrictHostKeyChecking=yes`、`IdentitiesOnly=yes`、15 秒 SSH 存活探测、10 秒连接超时；`-B en1` 让 SSH 绑定实际联网接口，避免经过本机 VPN/TUN。LaunchAgent 用 `RunAtLoad`、`KeepAlive` 和 10 秒节流拉起断开的 SSH 进程，plist 位于 `/Users/qiyo3/Library/LaunchAgents/com.bond-factor-lab.cutover-candidate.ssh-tunnel.plist`；第 4.6 节有完整的新服务范例。LaunchAgent 属于登录用户的 GUI domain；电脑休眠、关机、用户会话消失时不能把 `KeepAlive` 理解为 24 小时可用保证。

本网站的 Mac 连接中继时使用：

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes \
  -o UserKnownHostsFile=/Users/macstudio0/.ssh/known_hosts \
  -i /Users/macstudio0/.ssh/finlab-key.pem root@101.132.143.185
```

同事收到 ZIP 后，在自己的 macOS/Linux 电脑上将它放到当前目录，解压到仅自己可读的目录。先按第 4.3 节与管理员核对服务器主机指纹并登记 `known_hosts`，再运行：

```bash
umask 077
mkdir -p "$HOME/relay-handoff"
chmod 700 "$HOME/relay-handoff"
unzip ./public-domain-reverse-tunnel-handoff-20260926.zip -d "$HOME/relay-handoff"
chmod 600 "$HOME/relay-handoff/finlab-key.pem"
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes \
  -i "$HOME/relay-handoff/finlab-key.pem" root@101.132.143.185
```

如果同事已将 ZIP 解压到别处，`-i` 应指向其实际保存的 `finlab-key.pem`。这是服务器管理登录，具有高权限；不要把它放进新服务的 launchd/systemd 隧道任务。

## 3. 在同一中继上增加一个本地服务时先确定什么

以下采用独立子域名作示例，因为它与服务器上已有的网站路径隔离，新增 Nginx `server` 后无需改动该网站。**示例值不是已分配资源**：把 `service.example.com`、`18102`、`8080`、`relay_service` 和密钥路径替换为资源负责人确认的值。`example.com` 只是文档示例，不能用于实际证书申请。

新子域名的示例不去掉路径前缀：浏览器请求 `https://service.example.com/health`，本地应用收到的仍是 `/health`。若一定要在现有域名下使用 `/new-service/` 子路径，必须在现有站点增加不冲突的 location，并逐项处理前缀剥离、静态资源 URL、跳转、Cookie Path 和认证 Origin；这会修改共享站点，因此本文选择独立子域名作为完整操作路径。

| 待确认项 | 示例 | 通过条件 |
| --- | --- | --- |
| 新服务域名 | `service.example.com` | 同事有该域名 DNS 修改权限；A 记录可指向 `101.132.143.185`；不与现有站点重名 |
| 中继反向端口 | `18102` | `ss -lntp` 确认未占用；登记该端口归属；只绑定中继 `127.0.0.1` |
| 本地应用端口 | `8080` | 应用在本地 `127.0.0.1:8080` 可访问，健康检查有确定响应 |
| SSH 身份 | `relay_service` 与独立 Ed25519 密钥 | 中继管理员创建/授权；同事持有自己的私钥；主机密钥指纹经可信渠道核对 |
| 对外路由 | 页面、资源、API 的清单 | 确定哪些路径/方法公开、哪些需要登录；不要把管理或调试接口顺带放出 |
| HTTPS 证书 | 新域名单独的 Let's Encrypt 证书 | 证书文件存在、域名匹配、验证通过、自动续期可测试 |
| 本地主机在线条件 | 不休眠的 Mac/其他主机 | 应用进程和隧道进程有各自的守护方式、日志和恢复负责人 |
| 流量与资源 | 预计并发、响应时长、上传大小、WebSocket 数 | 中继管理员确认带宽、连接数、Nginx 限制与现有网站共存；应用需上传时另核对 `client_max_body_size`，不能靠转发自动支持任意大文件 |

这类方案的权限边界是：**DNS 控制者**创建记录，**中继管理员**分配端口、账号、Nginx 站点和证书，**本地服务负责人**管理应用与 SSH 隧道。保存现有 Nginx 配置、证书与端口清单后再改共享服务器；生产站点有在途流量，`nginx reload` 虽不重启应用，仍须经过共享入口的变更安排。

## 4. 实施步骤：独立子域名 + 独立反向隧道

以下命令是给获授权的执行者使用的范例，分“本地电脑”和“公网中继 ECS”两端。每一步先检查输出，再做下一步；**本次编写文档没有执行这些新服务配置命令**。

### 4.1 本地应用先只在 loopback 工作

在同事的本地电脑上，把应用配置为监听 `127.0.0.1:8080`，并用应用自己的健康路径检查：

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
curl --fail --silent --show-error http://127.0.0.1:8080/health
```

本地电脑若是 Linux，第一条监听检查可改用 `ss -lntp | grep ':8080 '`；后面的 HTTP 检查相同。

不要为了隧道把应用改成 `0.0.0.0`。如果应用自身需要登录、存储、环境变量或静态资源，先在本地完成验证；反向代理不会自动修复这些问题。应用生成绝对 URL、重定向、Cookie 或 WebSocket 时，必须按它自己的部署方式配置外部域名、HTTPS、可信代理和路径。

### 4.2 申请独立 DNS 与中继资源

DNS 管理者新增 `service.example.com A 101.132.143.185`，并在实际使用的递归解析器和权威 DNS 上确认生效：

```bash
dig +noall +answer service.example.com A
```

获授权的中继管理员先在 `101.132.143.185` 核对现有站点、端口及 SSH 设置，确认示例端口 `18102` 可用。若使用本交接包中的管理密钥，先按第 2 节解压、限制权限并核对主机指纹，再通过 `root` 登录；也可使用管理员另行分配的管理身份。下列服务器命令在已登录的会话内运行：

```bash
ss -lntp | grep ':18102 ' || true
readlink /etc/nginx/sites-enabled/bond-factor-lab
sudo nginx -t
sudo sshd -T | grep -E '^(allowtcpforwarding|gatewayports|permitlisten|passwordauthentication) '
```

管理员在改共享 sshd 或启用新 Nginx 站点前，保存可恢复的现场基线；时间戳使多次操作不会互相覆盖：

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
sudo cp -a /etc/ssh/sshd_config "/root/sshd_config.before-service-relay.$STAMP"
sudo sh -c "nginx -T > /root/nginx-before-service-relay.$STAMP.txt 2>&1"
```

记录该时间戳和新站点的文件名；`nginx -T` 保存的是当时实际包含的配置，不是新服务文件。它可能包含其他站点信息，应只供中继管理员保存，别放入发给同事的公开压缩包。

`18102` 没有 listener 才能分配；`grep` 无输出也要检查端口登记，不能仅凭当下空闲抢占。中继云安全组和主机防火墙只需放行该站点所需的公网 80/443，以及受控 SSH 入口；**不要开放 `18102` 到公网**。DNS 和云安全组由控制台实际读回，本文不能代替控制台权限；未获授权的同事由中继管理员代办服务器操作。

### 4.3 给隧道单独的账号与公钥

同事在自己的本地电脑生成密钥，并通过安全渠道把**公钥**交给中继管理员：

```bash
ssh-keygen -t ed25519 -a 64 -N '' -f "$HOME/.ssh/service_relay_ed25519" -C 'service-relay'
chmod 600 "$HOME/.ssh/service_relay_ed25519"
ssh-keygen -y -f "$HOME/.ssh/service_relay_ed25519"   # 只导出公钥
```

此示例创建**独立、无口令的隧道专用密钥**，因为后续 `BatchMode=yes` 的 launchd/systemd 服务不能在无人值守时等待输入口令。其安全边界依赖本地私钥仅该用户可读、服务器账号/公钥只允许指定反向端口；这把密钥泄露时的权限限制在该服务。若团队要求带口令密钥，须先配置并验证该守护进程实际可访问的 SSH agent/密钥托管方式，再使用 `BatchMode=yes`；仅在终端里手动 `ssh-add` 成功，不证明 launchd/systemd 启动时能读取该 agent。

新生成的隧道私钥只保留在同事电脑上；不要放在仓库、聊天或服务器上。首次连接前，从服务器管理员的可信渠道取得 SSH **主机公钥指纹**并核对 `known_hosts`；不要用 `StrictHostKeyChecking=no` 跳过。

本手册编写时，通过已有可信 SSH 会话读取中继主机的 Ed25519 公钥指纹，并与现有运维机 `known_hosts` 逐项比对，二者一致：

```text
101.132.143.185 ED25519 SHA256:VRVKAzYjBWf7i8qE0tTdoF18ALlFWgwQY3Yvk3Fl1pU
```

同事首次连接前，应再向服务器管理员核对这个指纹。若管理员确认未更换主机密钥，可在**同事自己的电脑**上执行以下首次登记步骤；`ssh-keyscan` 只负责取回候选公钥，不能单独证明它可信：

```bash
if [ -f "$HOME/.ssh/known_hosts" ]; then ssh-keygen -F 101.132.143.185 -f "$HOME/.ssh/known_hosts"; fi
HOSTKEY_TMP=$(mktemp)
ssh-keyscan -T 5 -t ed25519 101.132.143.185 > "$HOSTKEY_TMP"
ssh-keygen -lf "$HOSTKEY_TMP"
# 目视确认输出的 SHA256 指纹与管理员确认的值完全相同后，才继续登记。
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
cat "$HOSTKEY_TMP" >> "$HOME/.ssh/known_hosts"
chmod 600 "$HOME/.ssh/known_hosts"
rm "$HOSTKEY_TMP"
```

若指纹不同，停止连接，交由管理员核实是否有合法换钥；不要删除旧记录后直接接受新钥。登记成功后，前文的 `StrictHostKeyChecking=yes` 才能在首次 SSH 会话中正常工作。临时文件中只有候选公钥，不含私钥；指纹不匹配时也应删除该文件。
若 `ssh-keygen -F` 已显示同一正确指纹，可跳过追加 `known_hosts`，避免重复记录。

服务器管理员创建独立登录账号，在其 `~/.ssh/authorized_keys` 放入同事公钥，并把该 key 限制到分配的远端监听端口。在当前 Ubuntu 中继上，可先检查 `id relay_service` 确定账号不存在，再执行：

```bash
sudo adduser --disabled-password --gecos '' relay_service
sudo install -d -m 700 -o relay_service -g relay_service /home/relay_service/.ssh
sudoedit /home/relay_service/.ssh/authorized_keys
sudo chown relay_service:relay_service /home/relay_service/.ssh/authorized_keys
sudo chmod 600 /home/relay_service/.ssh/authorized_keys
```

`authorized_keys` 每条公钥独占一行。以下为该行的格式示意，`ssh-ed25519 AAAA...` 必须替换成同事的真实公钥；`command="/usr/bin/false"` 阻止以该 key 执行远程命令，而 `ssh -N` 的隧道不请求远程命令：

```text
restrict,port-forwarding,permitlisten="127.0.0.1:18102",command="/usr/bin/false" ssh-ed25519 AAAA... service-relay
```

再用仅适用于该账号的 `sshd_config` `Match User` 约束为**仅远端转发**，并限制 `PermitListen 127.0.0.1:18102`。可由管理员审查后放在 sshd 配置末尾；`Match` 之后的选项只属于该匹配块，不能把原有全局选项误放进去：

```text
Match User relay_service
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:18102
    GatewayPorts no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
```

`restrict` 默认关闭多数 SSH 功能，`port-forwarding` 为这条 key 重新允许转发；`permitlisten` 将远端监听限制在 `127.0.0.1:18102`。账号级 `AllowTcpForwarding remote` 又把方向限定为 `-R`。更改共享 sshd 前后用 `sudo sshd -t` 和 `sudo sshd -T -C user=relay_service,host=<CLIENT_HOST>,addr=<CLIENT_IP>` 核对生效设置，并通过原有管理账号保留第二条可用 SSH 会话；确认通过后在当前 Ubuntu 中继执行 `sudo systemctl reload ssh`，再分别复测原有管理连接和新账号的隧道。不要改现有网站登录账号的权限。`-R` 的监听地址与两处 `permitlisten` 必须精确一致。如果管理员采用不同的账号隔离方式，仍须达到“独立身份、只允许指定 loopback 端口”的结果。

### 4.4 先手工证明 SSH 反向端口可用

在同事本地运行下面的命令。它不启动新应用，也不向公网开放端口；前台保持运行供测试，完成后 `Ctrl-C` 结束，再交给守护进程。

```bash
ssh -N -T -o BatchMode=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
  -o ConnectTimeout=10 -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes \
  -i "$HOME/.ssh/service_relay_ed25519" \
  -R 127.0.0.1:18102:127.0.0.1:8080 \
  relay_service@101.132.143.185
```

中继管理员在另一会话里确认 listener 属于 `sshd`、只绑定 loopback，并直接测试应用：

```bash
ss -lntp | grep ':18102 '
curl --fail --silent --show-error http://127.0.0.1:18102/health
```

如果 `ssh` 立刻退出，查端口冲突、账号/key/主机密钥、`AllowTcpForwarding`/`PermitListen` 和本地到中继的网络；如果 SSH 保持运行而中继 `curl` 失败，先查本地应用是否仍在 `127.0.0.1:8080`。`-N` 不打开远端 shell，`-T` 不申请终端，`ExitOnForwardFailure` 只保证**转发建立时**失败就退出，不能替代应用健康检查。

### 4.5 为新域名取得单独证书

中继当前已安装 Nginx 1.24.0、Certbot 2.9.0，`certbot.timer` 处于计划状态。现有 `bond.finailab.cn` 证书使用 Certbot Nginx 插件；新域名可采用独立 webroot 方式申请证书，避免让证书工具自动重写现有站点。以下示例由中继管理员在**新站点**中分两阶段实施；先核对 DNS、80 端口可达，并备份现有 Nginx 生效配置。

先在新站点 `/etc/nginx/sites-available/service-example` 写入只有 HTTP 的配置，并启用该文件：

```nginx
server {
    listen 80;
    server_name service.example.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        try_files $uri =404;
    }

    location / {
        return 301 https://service.example.com$request_uri;
    }
}
```

```bash
sudo install -d -m 755 /var/www/letsencrypt/.well-known/acme-challenge
sudo ln -s /etc/nginx/sites-available/service-example /etc/nginx/sites-enabled/service-example
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/letsencrypt -d service.example.com
sudo certbot certificates
```

`ln -s` 只在目标文件尚不存在且已确认名字不冲突时执行。这里选择 Certbot 的 HTTP-01/webroot 方法：签发机构会访问 `http://service.example.com/.well-known/acme-challenge/<令牌>`，所以新域名必须已解析到此中继，外部必须能访问其 80 端口。证书申请会写入中继证书状态，不影响本地应用数据。证书文件应位于 `/etc/letsencrypt/live/service.example.com/`；不要复制或复用已有网站的 TLS 私钥。

证书签发后，在**同一个新站点文件**保留上述 HTTP 块并添加 HTTPS 块。下面是只暴露 `/` 与 `/health` 的最小基线；同事应据自己应用的路由清单追加明确允许的页面、静态资源和 API，不要不经审查就加一个把所有路径放出的 `location / { proxy_pass ...; }`：

```nginx
server {
    listen 443 ssl;
    server_name service.example.com;

    ssl_certificate     /etc/letsencrypt/live/service.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/service.example.com/privkey.pem;

    location / { return 403; }

    location = / {
        limit_except GET HEAD { deny all; }
        proxy_pass http://127.0.0.1:18102;
        proxy_http_version 1.1;
        proxy_set_header Host service.example.com;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_connect_timeout 3s;
        proxy_read_timeout 30s;
    }

    location = /health {
        limit_except GET HEAD { deny all; }
        proxy_pass http://127.0.0.1:18102;
        proxy_http_version 1.1;
        proxy_set_header Host service.example.com;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_connect_timeout 3s;
        proxy_read_timeout 30s;
    }
}
```

`/health` 若暴露内部细节，应改为只返回安全状态，或不对公网开放。登录 API 需额外处理请求方法、体大小、限流与会话 Cookie；WebSocket 需在对应路径单独配置 Upgrade 头和超时。需要受保护的功能必须由应用或入口鉴权，Nginx 的基础反向代理本身不会自动提供登录机制。每增加一条路由，都在新站点中明确其 URL、允许的方法、是否需要登录和上游目标；不要把已有网站的 Nginx 文件整体复制过来。

**仅复制上面的 HTTPS 示例时，页面 `/` 和 `/health` 能到本地，其余路径都会返回 403。** 这正是默认拒绝策略，但多数真实页面还需要 CSS、JS、图片和 API。新增前列一张路径清单：

| 本地应用路径 | 对外规则选择 | 验收例子 |
| --- | --- | --- |
| `/assets/` 内都是可公开静态文件 | 单独允许此前缀，限 GET/HEAD | 页面实际引用的 CSS、JS、图片均为 200 |
| `/api/status` 仅公开这一条 API | 增加 `location = /api/status`，按 API 的方法限制 | 允许的方法成功，其他方法拒绝 |
| `/api/` 包含需要登录的功能 | 先完成应用鉴权，再决定逐条放行或放行整个前缀 | 匿名请求被拒绝，登录后预期请求成功 |
| 单页应用使用多个前端路由 | 只有确认整站路径都可按应用自身权限处理时，才把默认拒绝改成整站代理 | 直接打开深层 URL、刷新、退回均正常 |

例如确认 `/assets/` 全是公开静态文件后，可在同一个 HTTPS `server` 内追加以下 location。它不剥离 `/assets/` 前缀，本地服务仍收到原始路径：

```nginx
location ^~ /assets/ {
    limit_except GET HEAD { deny all; }
    proxy_pass http://127.0.0.1:18102;
    proxy_http_version 1.1;
    proxy_set_header Host service.example.com;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Connection "";
}
```

若服务使用 WebSocket，在**对应的、已确认可公开的路径**单独添加升级配置。例如本地 WebSocket 路径是 `/ws` 时，可以把下面的 location 放入同一个 HTTPS `server`；鉴权仍由应用负责：

```nginx
location = /ws {
    proxy_pass http://127.0.0.1:18102;
    proxy_http_version 1.1;
    proxy_set_header Host service.example.com;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1h;
}
```

这里的 `1h` 只是示例空闲超时，要按应用心跳和连接要求决定；WebSocket 还需在真实浏览器中完成升级、收发消息及断线重连检查。每次扩展路由后重新 `nginx -t`、按窗口 reload，再用真实页面的网络请求验证，不以首页单次 200 代替资源/API 验收。

中继管理员在检查新站点与原站点的 `server_name`、端口和路由无冲突后，执行 `sudo nginx -t`，通过后在约定窗口 `sudo systemctl reload nginx`。再确认两个域名各自仍走正确上游。

现有 Certbot timer 只负责定期尝试续期；webroot 证书续期后，还需让 Nginx 重新读取证书。管理员先检查 `/etc/letsencrypt/renewal-hooks/deploy/` 中有没有同类 hook，避免重复；若没有，可在该目录新增一个独立可执行文件，例如 `90-reload-nginx`，内容如下：

```sh
#!/bin/sh
set -eu
/usr/sbin/nginx -t
/bin/systemctl reload nginx
```

将文件设为 `root:root`、权限 `755`。该目录的 hook 会在**任意** Certbot 证书成功续期后执行，因而会 reload 共享 Nginx；管理员应确认这与服务器已有的续期策略相容。用 `sudo certbot renew --dry-run --cert-name service.example.com` 验证 ACME 挑战路径；普通 dry-run **不会运行 deploy hook**。若要在约定窗口连 hook 一起测试，追加 `--run-deploy-hooks`，它会真实执行 reload，并使用当前正式证书。还要核对 `systemctl list-timers --all certbot.timer` 的计划状态与证书实际到期日。不要把一次证书签发当作长期续期验收。

### 4.6 把已验证的隧道交给本地守护进程

Mac 上可用 LaunchAgent 管理隧道，并为同事自己的服务使用**独立 label、密钥、远端端口和日志**。下面是一份完整示例，假定本地用户名为 `alice`；保存到 `~/Library/LaunchAgents/com.example.service-relay.plist` 前，替换用户名、账号、密钥、端口和 label，并用 `plutil -lint` 验证。不要覆盖已有网站的 LaunchAgent。`~/Library/Logs` 由该用户持有，供此任务写自己的日志。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.service-relay</string>
  <key>WorkingDirectory</key><string>/Users/alice</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string><string>-T</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>ServerAliveInterval=15</string>
    <string>-o</string><string>ServerAliveCountMax=2</string>
    <string>-o</string><string>ConnectTimeout=10</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>StrictHostKeyChecking=yes</string>
    <string>-o</string><string>IdentitiesOnly=yes</string>
    <string>-i</string><string>/Users/alice/.ssh/service_relay_ed25519</string>
    <string>-R</string><string>127.0.0.1:18102:127.0.0.1:8080</string>
    <string>relay_service@101.132.143.185</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/Users/alice/Library/Logs/service-relay.log</string>
  <key>StandardErrorPath</key><string>/Users/alice/Library/Logs/service-relay.err</string>
</dict>
</plist>
```

在同事的 Mac 用户会话中检查安装和加载，不要再保留 4.4 的前台 SSH，否则会有两个进程争抢同一远端端口：

```bash
plutil -lint "$HOME/Library/LaunchAgents/com.example.service-relay.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.example.service-relay.plist"
launchctl print "gui/$(id -u)/com.example.service-relay"
```

`bootstrap` 是有副作用的首次加载命令，确认该 label 没有已加载实例后执行。看到 `state = running` 和 ssh PID，再去中继核对唯一的 `127.0.0.1:18102` listener。以后修改 plist 时，先保存旧文件，结束这个 label 的已加载实例，再检查新文件并重新加载；这会短暂中断隧道，按维护窗口执行：

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.example.service-relay.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.example.service-relay.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.example.service-relay.plist"
launchctl print "gui/$(id -u)/com.example.service-relay"
```

不要对已经加载的 label 直接重复 `bootstrap`，也不要以手工第二条 `ssh -R` 代替故障排查。plist 文件更新、`plutil` 通过，都不能单独证明隧道已运行；还须核对中继 listener 和 HTTP 响应。

### 4.7 若本地电脑是 Linux

Linux 上可用 systemd 管理同一条 SSH 命令。假定本地账号也是 `alice`，私钥为 `/home/alice/.ssh/service_relay_ed25519`，已为该账号核对并保存中继主机密钥。管理员可新建 `/etc/systemd/system/service-relay.service`；示例文件如下：

```ini
[Unit]
Description=Reverse SSH tunnel for service.example.com
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=alice
ExecStart=/usr/bin/ssh -N -T -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 -o ConnectTimeout=10 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -i /home/alice/.ssh/service_relay_ed25519 -R 127.0.0.1:18102:127.0.0.1:8080 relay_service@101.132.143.185
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

文件归 root 所有，改成实际用户名、密钥和端口；`BatchMode=yes` 要求该 service 能非交互地使用密钥，且该用户的 `known_hosts` 已包含经核对的中继主机密钥。停止 4.4 的前台 SSH 后，在本地 Linux 执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now service-relay.service
systemctl status service-relay.service --no-pager
journalctl -u service-relay.service -n 50 --no-pager
```

接着仍按第 5 节验证中继 `127.0.0.1:18102` 与公网域名。`active` 只能证明进程在运行，不能证明应用响应正确。

## 5. 从内到外验收，而不是只看域名能打开

每个探针都证明一段链路，依次执行，记录时间、目标端口和响应。`--noproxy '*'` 可排除操作电脑上的 HTTP 代理，让公网请求真正直连域名所解析的入口。

| 探针位置 | 命令示例 | 说明 |
| --- | --- | --- |
| 同事本地 | `curl -fsS http://127.0.0.1:8080/health` | 应用本身存活；不证明 SSH/Nginx |
| 公网中继本机 | `curl -fsS http://127.0.0.1:18102/health` | SSH 反向端口能到本地应用；不证明 DNS/TLS |
| 公网客户端 | `curl --noproxy '*' -fsS https://service.example.com/health` | DNS、TLS、Nginx、SSH、应用整条链路成功；必须正常校验证书，别用 `-k` 掩盖错误 |

补充检查：

```bash
dig +noall +answer service.example.com A
curl --noproxy '*' -sS -o /dev/null -w '%{http_code} %{remote_ip} %{ssl_verify_result}\n' \
  https://service.example.com/health
curl --noproxy '*' -I http://service.example.com/
curl --noproxy '*' -sS -o /dev/null -w '%{http_code}\n' \
  https://service.example.com/not-allowed
```

期望：域名解析到已分配中继；HTTPS 证书校验为 0；被允许的路径得到应用预期状态；HTTP 跳 HTTPS；未允许的路径按站点规则拒绝。再用正常账号验收真实页面、静态资源、登录/注销、必要 API、重定向、Cookie 和浏览器刷新；`/health` 200 不代表完整使用流程可用。还要确认已有网站的 `https://bond.finailab.cn/bond-factor-lab/api/health` 保持 200，确保新站点没有干扰共享中继上的现有入口。

## 6. 故障定位、日常维护与恢复边界

| 现象 | 先查哪里 | 常见原因与处理方向 |
| --- | --- | --- |
| 本地 `8080` 不通 | 应用进程/日志 | 应用未启动、只监听别的地址、健康路径错；先在本地修复 |
| 本地通，中继 `18102` 不通 | 本地 SSH 日志、`launchctl print`、中继 `ss -lntp`/sshd 日志 | 隧道未建立、端口占用、主机密钥或账号限制不匹配、电脑休眠/网络代理阻断 |
| 中继 `18102` 通，公网不通 | DNS、证书、Nginx 站点与错误日志、云安全组 | 域名指错、443/80 不可达、站点未加载、location 不匹配、TLS 过期 |
| 公网 502 | Nginx error log 与中继端口 | Nginx 访问不到反向端口或上游断开；不要先改 DNS |
| 公网 504 | Nginx timing、应用处理时长 | 上游超时或处理慢；先定位应用/隧道，不盲目放宽超时 |
| 公网 413 | Nginx `client_max_body_size` 与上传需求 | 请求体超过入口限制；确认应用和安全边界后只为该服务设置合适上限 |
| 公网 401/403 | 应用会话、Nginx 白名单与方法 | 可能是预期鉴权/拒绝；不要靠关闭认证“修复” |
| HTTPS 好但静态资源/跳转坏 | 浏览器网络面板、应用 external URL、Nginx path | 子路径未适配、绝对 URL/Origin/Cookie 路径错误；独立子域名可降低此类冲突 |

中继端只读观察可用 `sudo nginx -t`、`systemctl status nginx`、`ss -lntp`、`journalctl -u ssh`、Nginx error/access log；本地 Mac 用 `launchctl print gui/$(id -u)/<LABEL>` 和自己应用/隧道的日志。日志不得收集私钥、登录密码、会话 token 或完整敏感请求。公网还应监控证书剩余有效期、Nginx 5xx、隧道进程与关键业务页面；只监控进程存在不足以证明应用可用。

恢复时按故障段处理：应用故障恢复应用；隧道故障先检查是否已有活跃 SSH 和唯一端口 owner，避免第二条 `-R`；Nginx 故障恢复**该新站点**的上一已验证配置并通过 `nginx -t` 后 reload。共享入口的修改要同时验证现有 `bond.finailab.cn`。DNS、证书、账号与私钥由各自负责人回滚；不要用关闭校验、把 `18102` 开到公网或改已有网站的隧道来绕过故障。

若刚启用的新站点造成问题，先由管理员确认 `/etc/nginx/sites-enabled/service-example` 确实指向本次创建的新文件，再仅撤下这个启用链接；保留站点原文件、证书和日志供排查。示例命令如下，执行前替换为实际的新站点文件名：

```bash
readlink /etc/nginx/sites-enabled/service-example
if [ "$(readlink /etc/nginx/sites-enabled/service-example)" = /etc/nginx/sites-available/service-example ]; then
    sudo rm /etc/nginx/sites-enabled/service-example
fi
sudo nginx -t && sudo systemctl reload nginx
```

随后验证已有域名仍健康，新域名按预期停止转发。若问题是新增的 sshd `Match User`，保持原有管理员 SSH 会话在线，从第 4.2 节保存的备份恢复**经过核对的原配置**，依次 `sudo sshd -t`、`sudo systemctl reload ssh`，再复测原管理连接；不要在失去唯一管理会话后盲目重启 sshd。仅撤下 Nginx 新站点不会自动删除已签发证书或服务器账号，清理这些资源须另行核对归属。

## 7. 交付给下一位同事的记录清单

完成新服务时记录：域名与 DNS 负责人、中继 IP、分配的远端 loopback 端口、本地应用端口、SSH 账号与**公钥指纹**（不记录私钥）、Nginx 新站点文件/启用链接、证书 lineage 与续期方式、本地守护任务 label、应用及隧道日志位置、路由/鉴权清单、三点探针的最近成功时间，以及恢复责任人。配置变更前后保存 Nginx 生效配置摘要和原站点的健康结果。

为新服务保存自己的配置副本和运行记录；本文的现有实例只说明一种经过现场核对的转发方式，不能代替新域名、新隧道和新应用的实测验收。

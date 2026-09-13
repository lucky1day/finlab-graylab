# 登录与账户管理合同

**文档状态**：`CURRENT`

本文维护独立登录、账户、会话、权限和认证恢复合同。两机各用自己的认证数据，不依赖其他前端系统，
不复制账户、密码哈希、会话或审计。运行版本见[当前状态](../CURRENT_STATUS.md)；地址、Origin 和配置位置见
[访问入口](../operations/DEPLOYMENT_ACCESS.md)，release/schema 恢复见[部署手册](../../deploy/README.md)。

## 范围与角色

角色只有 `admin` 和 `user`。两者均可登录/退出、查看全部 Dashboard、修改本人密码及可选姓名/机构；
只有管理员可查看/创建用户，修改登录名、资料、角色、状态及重置密码。
网站管理员不因此获得算法激活、预测写库、迁移、补数、调度或服务控制权。
角色每次由后端数据库核验，前端不能提交可被信任的角色。

不提供自行注册、邮箱/手机验证码、网页找回密码、第三方登录/SSO、MFA、用户组或方案级权限、
会话/审计自动归档任务。账户与审计不物理删除；认证与算法业务的模块边界见下文。

## 账户生命周期

用户名为 3–32 个小写英文字母、数字、点、下划线或连字符，首字符为字母/数字；创建前转小写并校验唯一性，
改名重新执行同一校验。账户内部身份始终为不可变 `t_auth_users.id`，不使用用户名做关联主键。
停用账户仍占用原名，改名后旧名可复用。

| 动作 | 数据与事务要求 | 会话影响 |
|---|---|---|
| 管理员创建 | 指定用户名、初始密码、角色，可选姓名/机构；状态 active，must_change_password=false | 首次登录不强制改密 |
| 改用户名 | 正常/停用用户均可修改；记录前后值；原 ID 不变 | 撤销目标用户全部会话，以新名重登 |
| 本人改密 | 校验当前密码，写新哈希；不要求周期性改密 | 撤销本人全部会话，包含当前会话 |
| 管理员重置密码 | 新哈希、must_change_password=false、审计与会话撤销同一事务提交 | 撤销目标用户全部会话，不强制再改密 |
| 改角色 | 后端验证角色与管理员保护，提交审计 | 撤销目标用户全部会话 |
| 停用 | 只改状态并记录操作者/时间，不删除账户 | 立即撤销目标用户全部会话 |
| 恢复 | 仅恢复可登录状态，不清历史审计；可继续原密码或另行重置 | 旧会话不恢复，必须重新登录 |
| 编辑资料 | 本人和管理员可改可选姓名/机构；管理员账户编辑可原子修改资料/角色/状态，不接收密码 | 涉及改名、改角色、停用时按上述规则撤销 |

`is_protected_admin` 仅保护初始管理员，不能通过页面/API 改名、降级或停用，不引入第三种角色。
管理员不能停用或降级自己；任何事务（含并发管理员操作）都不能让有效管理员数量降为零。
受保护管理员的离线密码恢复另见下文，其会话撤销范围比普通管理员重置更广。

## 密码、会话与请求安全

### 密码

密码为 6–128 字符，至少含 ASCII 大写、小写和数字，特殊字符可选。使用 Argon2id；参数不得低于
[OWASP 密码存储基线](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)，
并在目标环境验证登录延迟/资源占用。支持粘贴和密码管理器，不提供延长会话的“记住我”。

密码、哈希、原始 token 和原始认证请求体不得进入日志、审计 detail、API 响应或应用管理的浏览器存储。
管理员只能设置密码，不能读取用户旧密码/哈希。自动填充由浏览器管理，使用 autocomplete=username/current-password，
应用不通过自定义脚本保存密码。

### 会话

每次成功登录创建新的随机 token 和独立会话；浏览器只持原 token，数据库只存 SHA-256 摘要。
允许多浏览器并发登录，每条会话创建后固定 12 小时绝对过期，不续期。
Cookie 为：

```text
__Host-bfl-session=<opaque-token>; Secure; HttpOnly; SameSite=Strict; Path=/
```

不设置 Domain，不将 token 放入 URL、localStorage、sessionStorage 或其他应用持久存储。
每次受保护请求核验会话过期/撤销、用户状态和当前角色。普通退出只撤销当前会话；账户变更的撤销范围见生命周期表。
已撤销或过期会话是终态，账户恢复不得清除 revoked_at、延长 expires_at 或恢复旧浏览器登录。
参考 [OWASP 会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。

### 请求来源

状态变更仅接受 JSON POST，禁止 GET 写入。严格核验部署目标对应的可信 Origin 和 Sec-Fetch-Site，
拒绝缺失或不匹配请求；SameSite=Strict 仅为纵深防御。固定 Origin 值只维护于访问入口，
`BFL_AUTH_TRUSTED_ORIGIN` 的私有配置方式见部署手册。ECS 的 localhost HTTP 例外不推广到其他入口，Cookie 仍为 Secure。
参考 [OWASP CSRF](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)。

## 页面与交互

沿用单页应用，登录和用户管理不新增独立 HTML 路由；各机路径前缀见访问入口。
加载时先查询会话，期间显示中性加载态，不闪现业务数据或提前请求 Dashboard。有效会话进入因子实验室，
无效会话进入登录。受保护请求返回 401、退出或身份切换时清除 Dashboard/用户内存状态和失效 Cookie，
返回登录页，浏览器后退不得重新展示受保护数据。

- 登录页沿用米白、深色文字及红金强调，仅显示标识、用户名、密码、显隐按钮、登录按钮和
  “账号由管理员创建；如需重置密码，请联系管理员”。支持回车，提交中禁重复；不存在、密码错或停用统一
  “用户名或密码错误”，不能用于枚举账户。已登录访问登录视图直接回到因子实验室。
- 右上角只显示用户名，无头像。菜单仅“个人资料”“修改密码”“退出登录”；用户管理是管理员主导航。
  支持键盘、Escape/点击外部关闭、aria-expanded、焦点进入与返回。
- 改密对话框包含密码规则提示、当前/新/确认密码、取消和确认；成功后按生命周期表撤销会话返回登录。
- 管理员列表显示用户名、可选姓名/机构、角色、状态、创建时间及操作。编辑表单不含密码；
  “更多”仅提供密码操作，自己的账户复用本人改密；不扩展删除、冻结或代理人等能力。
- 新增用户表单包含用户名、可选姓名/机构、初始密码和角色。停用必须二次确认，恢复行为按生命周期表。

## API 与访问边界

以下均为 Backend 内部路径，公网前缀与连接方式见访问入口；Nginx 仅放行精确页面/API，不开放泛化 `/api/`。
页面和静态资源、最小 health 可匿名加载；Summary/Detail 均要求登录，匿名 401、登录后 200。

| 方法 | 路径 | 权限与作用 |
|---|---|---|
| POST | `/api/auth/login` | 匿名，验证并创建会话 |
| POST | `/api/auth/logout` | 已登录，撤销当前会话 |
| GET | `/api/auth/me` | 可匿名调用；无有效会话返回 401 |
| POST | `/api/auth/change-password` | 已登录，本人改密 |
| POST | `/api/auth/update-profile` | 已登录，本人资料 |
| GET | `/api/admin/users` | admin，用户列表 |
| POST | `/api/admin/users` | admin，创建用户 |
| POST | `/api/admin/users/change-username` | admin，改登录名 |
| POST | `/api/admin/users/change-role` | admin，改角色 |
| POST | `/api/admin/users/reset-password` | admin，重置目标密码 |
| POST | `/api/admin/users/change-status` | admin，停用/恢复 |
| POST | `/api/admin/users/update-profile` | admin，目标资料 |
| POST | `/api/admin/users/edit` | admin，原子编辑资料/权限/状态，不接收密码 |

状态码：200 成功、201 创建成功、401 无有效会话、403 权限不足、409 用户名/管理员保护等冲突、422 输入非法。
普通用户不渲染管理入口，但隐藏界面不能替代后端 API 的 admin 检查。

## 存储与模块边界

物理 schema 唯一来源为 [Migration 022](../../migrations/022_authentication.sql)和
[Migration 023](../../migrations/023_auth_user_profiles.sql)及后续相关迁移；迁移操作统一走部署手册。
三表各自职责：

- `t_auth_users`：稳定账户身份、资料、密码哈希、角色/状态和受保护管理员标记；
  `must_change_password` 兼容保留为 false，可选姓名/机构空值为 NULL。
- `t_auth_sessions`：user_id、唯一 token_hash、创建/绝对过期/撤销时间，按上述终态规则处理。
- `t_auth_audit_logs`：已提交成功的管理员账户变更及初始管理员初始化/离线重置，insert-only；
  保留 actor/target、受控 event_type/detail、request_id。登录、me、用户查询和普通退出不重复写审计表，
  必要请求结果进入结构化日志。审计无页面修改/删除能力。

认证时间按 UTC 写入，展示转换 Asia/Shanghai。模块分工：routes 处理 HTTP/Cookie；service 承担认证、
权限和生命周期；security 负责哈希、令牌及恒定时间比较；repository 是上述三表唯一写入口。
算法 scheduler/backtest/updater 不写认证表；认证模块不读写算法、Registry、prediction、Actual、backtest、
DataBridge 或调度状态，repository 不写业务/源数据表。

## 初始管理员与离线恢复

唯一入口为 [`scripts/manage_auth_admin.py`](../../scripts/manage_auth_admin.py)。Migration 只创建 schema，
不植入默认凭据。取得目标环境授权后，先完成[手工 CLI 环境绑定](../../deploy/README.md#手工-harness-的目标环境绑定)
并核对数据库身份；密码仅从目标机固定 owner-only
secret 读取，位置见访问入口；不通过参数、环境变量、shell history、日志、文档或 Git 承载密码。

- `--initialize` 仅在用户表为空时创建固定用户名 **admin**、role=admin、is_protected_admin=true；
  不接受 username 参数，非空表拒绝。密码必须满足统一规则，首次登录不强制改密。
- `--reset-protected-admin` 只重置唯一受保护且 active 的管理员，不接受任意目标用户；
  更新哈希并保持 must_change_password=false，**同一事务撤销所有 admin 角色账户的现有会话**并写审计。
  这是离线故障恢复影响范围，不能按普通“重置某用户”估算；重置后所有管理员均须重新登录。
- 两种模式均须显式提供 `--expected-database-name` 和 `--expected-server-uuid`；从对应机只读 identity
  核验取得，不复制另一机值。secret 必须是运行用户拥有的真实普通文件，权限 0400/0600；读取拒绝链接和替换竞态。

认证故障恢复必须保持上述访问边界：可封闭入口返回 503，不能退回匿名 Dashboard。
release/schema 兼容检查和 lockdown site 按[部署恢复](../../deploy/README.md#回滚与认证部署)处理。

## 验收

按变更影响选择现有安全、API、前端、迁移及事务合同检查，并在每个授权环境的独立认证数据中核验：

1. 匿名与普通用户的访问边界、页面中性加载、401 清空及后退不泄露；认证不能改变 Dashboard 统计/日期合同。
2. 生命周期表的全部操作、目标会话撤销、终态不复活，以及并发最后管理员和受保护管理员保护。
3. 密码/令牌不泄露、提交成功审计不可改、并发会话各自 12 小时绝对过期。
4. 离线入口拒绝错误身份、非空初始化、非法密码/secret，核对固定 admin 与全管理员会话撤销范围。
5. ECS 仍只监听 loopback、拒绝未列出 API 方法，仅经既有 SSH 入口验收；不因认证测试开放端口、修改公网
   Nginx 或同步另一机账户。生产迁移、release 和服务动作按部署手册核验权限及恢复边界。

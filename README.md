# DooTask 独立 FastAPI 系统

修改日期：2026-10-12。源 `dootask` 只读；全新数据库、全新账号，不导入旧密码或 Token。不依赖 doo.so、原厂商业席位或设备授权，不伪造官方授权，不运行 PHP 业务或更新器。保留适用 AGPL、版权、第三方 notice 与对应源码义务。

**已完成真实隔离运行、核心回归及生产前端构建交付；不是326业务全量验收通过。** 历史334业务保留326入口，8个厂商授权入口退役，222个框架/继承候选拒绝。实际执行记录见 `docs/2026101219-后端真实运行与回归验收-feat.md` 与 `docs/evidence/qa-backend/`；前端 `public` 已由真实Vite构建提升，不再使用旧制品。逐条业务/外部依赖未覆盖仍明确为NOT_RUN/BLOCKED，匿名入口拒绝不等于业务成功。

**部署安全门禁：仅允许loopback/受控隔离环境、可信合成内容验收；不可信多人内网及公网部署NO_GO，不是production ready。** 1223独立复核确认实际可达的Mermaid/TinyMCE及原始HTML信任边界仍未关闭；升级静态制品、第三方LTS许可与必要净化均需要额外授权和真实跨用户回归。见 `docs/2026101223-前端运行依赖风险复核-feat.md`，不可拿基本业务通过或npm聚合数量替代安全决定。

## 独立 QA 启动

本机 Windows CPython 3.12 `.venv` 的37个锁定runtime包已用于真实运行；安装来源与官方PyPI hash见 `docs/evidence/integration/`。本批未新增Python依赖。不要重复创建已有venv或对已初始化的库执行init-db。

新检出环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\run.ps1 secrets
```

锁为 Windows amd64 / CPython 3.12；PGPy sdist 的隔离构建工具链不在 runtime lock 内。`.runtime/runtime.env`、`redis.conf` 为既有本机私密配置，不提交。新机器须按 `docs/evidence/acquisition/runtime-plan.json` 创建随机密码与相匹配 Redis 配置；不可用空密码替代。`secrets` 拒绝覆盖已有 `.runtime/backend.env`。

在 `dootask-fastapi` 根目录执行。此主机WSL在没有前台Linux进程时会退出，即使Docker/systemd仍有服务；先在一个独立终端保持 `.\run.ps1 runtime-hold`，不要关闭它，再在第二个终端启动隔离服务：

```powershell
# 终端1，保持运行：
.\run.ps1 runtime-hold
# 终端2：
.\run.ps1 runtime-up
.\run.ps1 secrets # 仅 backend.env 尚不存在时
.\run.ps1 init-db
.\run.ps1 serve -ListenHost 127.0.0.1 -Port 18080
```

`runtime-up` 使用WSL `Ubuntu-22.04`、项目 `dootask-fastapi-isolated`、独立volumes、MariaDB `127.0.0.1:13306`、Redis `127.0.0.1:16379`；本次健康检查、端口映射及同卷down/up重启已实测。不连接既有3306/6379或旧业务库；`runtime-down`不删除volumes。`runtime-hold`只保持本WSL进程存活，不停止任何已有服务。正式宿主默认 `frontend/`，无需设置候选FRONTEND_ROOT。

`init-db` **仅空新库**，按 `schema.sql → security.sql → outbox.sql → domains/files.sql → domains/accounts.sql → schema_seed.sql` 执行；反射业务/安全/上传/下载/账号初始化状态表。MariaDB DDL 隐式提交，失败需保留日志并仅恢复专用新库，不能声称自动原子回滚。此命令不是生产升级迁移器，不能对已有库重复执行。

### 合成用户入口

初次初始化应仅开放 loopback，由 QA 在 `/login` 使用原注册流程，或发送 `POST /api/users/login` JSON：

```json
{"type":"reg","email":"qa-owner@example.test","password":"ChangeMe-Unique-Pass79!"}
```

首次本地 human 通过数据库单例锁取得管理员身份；之后注册不提升权限。无需原厂 license。尊重注册开关/邀请/邮箱/LDAP策略：若开启邮箱验证，返回 `ret=0,data.code=email` 但合法账号、验证记录和 SMTP outbox 已提交，不签发会话；配置真实 SMTP、完成验证后再登录，不能将该响应当事务回滚或伪邮件成功。所有普通 ApiError 仍回滚。创建另外两个本地测试用户用于对象权限检查。不要公开未完成首管理员注册的端口。

示例密码仅说明格式，部署时换为唯一随机密码；原注册邮箱长度最多32字符、密码UTF-8最多32字节的约束保留。当前隔离库已通过真实并发HTTP注册产生唯一首管理员，私密凭据仅留本机 `.runtime/qa-accounts.json`，不写入报告或Git；再次注册不会变成管理员。一次错误登录可触发验证码，自动化不能把随后的无验证码拒绝误报成改密失效。

### 验收命令

在 PowerShell 先加载运行环境（不会启动服务）：

```powershell
foreach ($file in @('.runtime/runtime.env','.runtime/backend.env')) {
  foreach ($line in [IO.File]::ReadAllLines((Join-Path $PWD $file))) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
      [Environment]::SetEnvironmentVariable($Matches[1],$Matches[2],'Process')
    }
  }
}
$env:DOOTASK_TEST_EMAIL='<synthetic account email>'
$env:DOOTASK_TEST_PASSWORD='<synthetic account current password>'
.\.venv\Scripts\python.exe -m unittest backend.tests_shared backend.templates.tests_host
.\.venv\Scripts\python.exe -m tests.test_accounts_contract
.\.venv\Scripts\python.exe -m backend.tests_files
.\.venv\Scripts\python.exe -m backend.tests_projects
.\.venv\Scripts\python.exe -m backend.tests_messaging
.\.venv\Scripts\python.exe -m backend.tests_integration
$env:DOOTASK_INTEGRATION_TOKEN='<isolated synthetic administrator token>'
.\.venv\Scripts\python.exe -m backend.tests_integration --database
$env:DOOTASK_PROJECT_TEST_TOKENS='["<admin token>","<member token>","<outsider token>"]'
.\.venv\Scripts\python.exe -m backend.tests_projects --integration
.\.venv\Scripts\python.exe -m backend.tests_messaging --users <admin-id> <member-id> <outsider-id>
.\.venv\Scripts\python.exe -c "from backend.tests_integration import scheduler_checks; scheduler_checks()"
```

**fresh顺序约束：** `backend.tests_independent`必须在init-db后、任何注册前执行，并设置 `DOOTASK_TEST_INDEPENDENT=1`；设置 `DOOTASK_TEST_BASE_URL=http://127.0.0.1:18080`可通过真实HTTP并发注册，`DOOTASK_TEST_ACCOUNTS_FILE`只能指向私密忽略目录。它会保留两个新账号并重置普通成员测试密码，当前版本把随机合成凭据仅存指定私密文件；不能在当前非空库重跑或清空未知库来迁就它。之后再运行上面的共享/域/真实DB检查。可重放入口为 `python -m tests.backend_core_acceptance [--readback]`、`python -m tests.backend_persistence_snapshot <label>`（当前合成fixture IDs）、`python -m tests.backend_inventory_acceptance`、`python -m tests.ws_crash_acceptance`；均使用本机私密合成账号状态与隔离库，crash场景只创建并终止专用18081进程。

仍未验证/缺真实服务：SMTP与邮件验证、LDAP、Manticore/embedding历史权限索引、AI/SSE成功流、外部appstore安装器/hooks、Office/nginx/预览、LibreOffice、Agora/推送/人脸等。Manticore无本地镜像/程序，官方Docker Hub实际拉取超时；不是缺用户密码。`/appstore/api/v1/internal/installed`现由独立后端认证后读取真实本地安装registry与package菜单，空新环境诚实返回零安装，浏览器不再因该入口404报错；不伪造外部商店、安装或hook成功。APP_KEY未配置的onboard/offboard outbox仍保留失败并退避，不是商业授权门禁。核心push/message/ZIP通知均已真实消费，观察窗口没有未注册kind重试。完整边界见1219及前端/协议验收报告。

## 安全与可靠性边界

- 业务共享 SQLAlchemy Connection；显式事务和持久 outbox。进程 lifespan 安装 jobs/scopes 后启动唯一每进程 worker，shutdown 发 stop 并等待当前事务完成再释放引擎。唯一考勤提醒为 `checkin.remind`。
- 原JSON/form/multipart、5MiB blob分片、Response/空body/raw JSON、大小写/双下划线与 `assistant/match-elements` 保留；兼容原Axios实际 `OPTIONS, application/json` 媒体头并拒绝歧义多媒体类型。`dootask-token` header/query优先于Office body JWT；POST-only handoff使用真实HTTP method。
- HTTP/login/WS/DAV 统一停用检查；Token 不来自 Cookie。短 webhook service token 无交互设备、最多一小时，不能建立 WS；撤销/改密/禁用继续有效。TLS、可信反向代理 IP 与日志脱敏仍由部署方配置。
- WebSocket outbox完成、ASGI发送完成、客户端 receipt 为三个不同状态。成功 send 后记 `transport_sent_at`，不伪 ACK，不因原客户端丢 msgId 每15秒无限重投；发送失败保留未发记录重连补送，ACL撤销单独 discarded 不堵塞后续 tombstone。send 后落库前崩溃可能重复；send 完成不保证浏览器业务处理，重连原消费者刷新持久业务状态。没有 exactly-once 承诺。
- 静态宿主最后注册，拒绝 PHP、dotfiles、配置/源码/秘密；`/health` 仅进程存活，不是数据库 readiness。`/api/*`、DAV、根 desktop publish 按源免 CSRF，其余 Index 写方法校验签名 Cookie/token。

## 本地版本部署与回滚

不使用原 PHP 在线升级器。每次交付保存 Git commit、依赖锁、前端构建 manifest/hash、DDL/迁移版本；在隔离新环境先完成回归。停写并等待 worker 当前事务，备份专用 MariaDB、storage、PGP_STORAGE_KEY 与配置；备份须加密限权并做恢复演练。部署新代码及其匹配前端制品，只有审核过的显式版本迁移可更新已有新系统库；当前只有 fresh-init，升级有 schema 变化时不得直接覆盖运行。切换反向代理前验证登录、对象权限、WS、下载及持久化。失败回切上一完整代码/制品并恢复与之匹配的数据库和 storage 快照；禁止跨版本混用 schema 或对用户旧库执行清库。

## 许可证与对应源码

原许可证与版权保留在 `frontend/LICENSE` 等路径。公开网络部署应显著提供与实际版本一致的 Corresponding Source（原前端、修改、安装/构建文件），配置 `CORRESPONDING_SOURCE_URL`；改写为 Python 不改变现有许可证义务。官方安装包/ASAR、含上游签名私钥归档仅隔离取证，不提交、不公开；本项目凭据不提交。

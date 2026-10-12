# DooTask 独立 FastAPI 系统

修改日期：2026-10-12。源 `dootask` 只读；全新数据库、全新账号，不导入旧密码或 Token。不依赖 doo.so、原厂商业席位或设备授权，不伪造官方授权，不运行 PHP 业务或更新器。保留适用 AGPL、版权、第三方 notice 与对应源码义务。

**当前为实现整合、尚未运行验收。** 历史 334 业务中 326 入口已按显式域表接入，8 个厂商授权入口（含system/license）按用户决定退役；222 个框架/继承候选仍拒绝。独立部署元数据放在既有system/setting，不保留死授权路由。注册、登录、领域功能的代码安装不等于行为已验证。用户已批准移除原厂授权前端页面/菜单；该源码切片已改变，`frontend/public` 仍是旧制品，须下一批集中构建、像素/真实 UI 验证后才能交付，不宣称独立 UI 已完成。

## 独立 QA 启动

本批没有运行 build、tests、lint、formatter、smoke、服务或数据库初始化。已有 Windows CPython 3.12 项目 `.venv` 已安装全部 37 个锁定 runtime 包，新增包安装来源与官方 PyPI hash 交叉见 `docs/evidence/integration/`。勿重复创建已存在 venv。

新检出环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\run.ps1 secrets
```

锁为 Windows amd64 / CPython 3.12；PGPy sdist 的隔离构建工具链不在 runtime lock 内。`.runtime/runtime.env`、`redis.conf` 为既有本机私密配置，不提交。新机器须按 `docs/evidence/acquisition/runtime-plan.json` 创建随机密码与相匹配 Redis 配置；不可用空密码替代。`secrets` 拒绝覆盖已有 `.runtime/backend.env`。

在 `dootask-fastapi` 根目录，由 QA 依次执行：

```powershell
.\run.ps1 runtime-up
.\run.ps1 secrets # 仅 backend.env 尚不存在时
.\run.ps1 init-db
.\run.ps1 serve -ListenHost 127.0.0.1 -Port 18080
```

`runtime-up` 使用 WSL `Ubuntu-22.04`、项目 `dootask-fastapi-isolated`、独立 volumes、MariaDB `127.0.0.1:13306`、Redis `127.0.0.1:16379`。不连接已有用户库或 3306/6379。映射及镜像来源仍须 QA 实测，Docker image ID 的静态存在不是运行通过。`runtime-down` 不删除 volumes。

`init-db` **仅空新库**，按 `schema.sql → security.sql → outbox.sql → domains/files.sql → domains/accounts.sql → schema_seed.sql` 执行；反射业务/安全/上传/下载/账号初始化状态表。MariaDB DDL 隐式提交，失败需保留日志并仅恢复专用新库，不能声称自动原子回滚。此命令不是生产升级迁移器，不能对已有库重复执行。

### 合成用户入口

初次初始化应仅开放 loopback，由 QA 在 `/login` 使用原注册流程，或发送 `POST /api/users/login` JSON：

```json
{"type":"reg","email":"qa-owner@example.test","password":"Replace-With-A-Unique-Strong-Password"}
```

首次本地 human 通过数据库单例锁取得管理员身份；之后注册不提升权限。无需原厂 license。尊重注册开关/邀请/邮箱/LDAP策略：若开启邮箱验证，返回 `ret=0,data.code=email` 但合法账号、验证记录和 SMTP outbox 已提交，不签发会话；配置真实 SMTP、完成验证后再登录，不能将该响应当事务回滚或伪邮件成功。所有普通 ApiError 仍回滚。创建另外两个本地测试用户用于对象权限检查。不要公开未完成首管理员注册的端口。

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
.\.venv\Scripts\python.exe -m unittest backend.tests_shared backend.templates.tests_host tests.test_accounts_contract
.\.venv\Scripts\python.exe -m backend.tests_files
.\.venv\Scripts\python.exe -m backend.tests_projects
.\.venv\Scripts\python.exe -m backend.tests_messaging
.\.venv\Scripts\python.exe -m backend.tests_integration
$env:DOOTASK_INTEGRATION_TOKEN='<isolated synthetic administrator token>'
.\.venv\Scripts\python.exe -m backend.tests_integration --database
```

各检查的实时数据库前提另见检查文件；项目三用户 token 和消息三用户 ID 检查必须执行，不以纯函数检查代替。完整验收矩阵、外部服务前提及安全切换见 `docs/2026101217-全域整合与运行准备-feat.md`。

## 安全与可靠性边界

- 业务共享 SQLAlchemy Connection；显式事务和持久 outbox。进程 lifespan 安装 jobs/scopes 后启动唯一每进程 worker，shutdown 发 stop 并等待当前事务完成再释放引擎。唯一考勤提醒为 `checkin.remind`。
- 原 JSON/form/multipart、5MiB blob 分片、Response/空 body/raw JSON、大小写/双下划线与 `assistant/match-elements` 保留。`dootask-token` header/query 优先于 Office body JWT。POST-only handoff 使用真实 HTTP method。
- HTTP/login/WS/DAV 统一停用检查；Token 不来自 Cookie。短 webhook service token 无交互设备、最多一小时，不能建立 WS；撤销/改密/禁用继续有效。TLS、可信反向代理 IP 与日志脱敏仍由部署方配置。
- WebSocket outbox完成、ASGI发送完成、客户端 receipt 为三个不同状态。成功 send 后记 `transport_sent_at`，不伪 ACK，不因原客户端丢 msgId 每15秒无限重投；发送失败保留未发记录重连补送，ACL撤销单独 discarded 不堵塞后续 tombstone。send 后落库前崩溃可能重复；send 完成不保证浏览器业务处理，重连原消费者刷新持久业务状态。没有 exactly-once 承诺。
- 静态宿主最后注册，拒绝 PHP、dotfiles、配置/源码/秘密；`/health` 仅进程存活，不是数据库 readiness。`/api/*`、DAV、根 desktop publish 按源免 CSRF，其余 Index 写方法校验签名 Cookie/token。

## 本地版本部署与回滚

不使用原 PHP 在线升级器。每次交付保存 Git commit、依赖锁、前端构建 manifest/hash、DDL/迁移版本；在隔离新环境先完成回归。停写并等待 worker 当前事务，备份专用 MariaDB、storage、PGP_STORAGE_KEY 与配置；备份须加密限权并做恢复演练。部署新代码及其匹配前端制品，只有审核过的显式版本迁移可更新已有新系统库；当前只有 fresh-init，升级有 schema 变化时不得直接覆盖运行。切换反向代理前验证登录、对象权限、WS、下载及持久化。失败回切上一完整代码/制品并恢复与之匹配的数据库和 storage 快照；禁止跨版本混用 schema 或对用户旧库执行清库。

## 许可证与对应源码

原许可证与版权保留在 `frontend/LICENSE` 等路径。公开网络部署应显著提供与实际版本一致的 Corresponding Source（原前端、修改、安装/构建文件），配置 `CORRESPONDING_SOURCE_URL`；改写为 Python 不改变现有许可证义务。官方安装包/ASAR、含上游签名私钥归档仅隔离取证，不提交、不公开；本项目凭据不提交。

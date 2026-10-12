# Windows 环境、时间与独立验收基线

## 交付边界与证据等级

本报告为 G1 调查及交付准备，不是迁移实现完成报告。恢复读取了 `history://RuntimeOwner`：其记录只含调查调用，无可复用终稿；本报告结论来自本次重新采样。源目录 `D:/rpa/202610/dootask` 只读，新目录 `D:/rpa/202610/dootask-fastapi` 独立初始化 Git。未运行应用、源 dev server、build、lint、tests 或 formatter；未安装系统工具、连接业务数据库、修改系统时间或推送 Git。

- **事实**：工具直接返回的读数、版本、文件及 Git 身份。
- **推断**：根据这些事实提出的补齐路径，不代表已经实施或兼容性通过。
- **未知**：真实服务可用性、业务功能行为、前端与源码构建一致性、完整迁移覆盖率，不能用计划代替运行结果。
- 代码检索首先调用 codegraph，返回源目录无索引；遵守提示使用内置文件工具，没有创建索引。
- 文档日期使用本次实际采样日期，PM 管理文档原文件名保留历史创建日期，不把历史读数当同步校时。

## 1. 本机与两独立 HTTPS 时间交叉采样

### 本机事实

`docs/evidence/runtime/local-clock.json` 保留 PowerShell 原命令、起止与完整输出：

- 本地 ISO：`2026-10-12T12:39:44.5411748+08:00`。
- 同一 DateTime 的 UTC：`2026-10-12T04:39:44.5411748Z`。
- 时区：`China Standard Time`，UTC+08:00，不支持夏令时。
- OS：`Microsoft Windows NT 10.0.26200.0`。

### HTTPS 事实

两个不同运营方 HTTPS 站点并发发起 HEAD；使用系统默认 HTTPS 证书验证，无关闭证书验证、无携带账号。请求加 `Cache-Control: no-cache` 和 `Pragma: no-cache`，但请求指令不能保证中间缓存一定重新校时。完整 URL、请求/响应头、状态、重定向最终 URL、起止 UTC、单调计时耗时与算式结果分别落盘：

| 时间源 | 请求开始 UTC | 收到响应结束 UTC | HTTP Date | RTT 观测 | Date − 本机中点 |
|---|---|---|---|---:|---:|
| [Cloudflare](https://www.cloudflare.com/) | 04:40:16.591660 | 04:40:18.909096 | 2026-10-12 04:40:28 GMT | 2.3190324 s | +10.249622 s |
| [Microsoft](https://www.microsoft.com/) | 04:40:16.592659 | 04:40:17.427440 | 2026-10-12 04:40:27 GMT | 0.8349889 s | +9.989951 s |

以上日期均为 2026-10-12。证据：`time-www.cloudflare.com.json`、`time-www.microsoft.com.json`。Cloudflare 响应 `Cache-Control: no-store`；Microsoft 此响应未提供 Cache-Control/Age/X-Cache。没有响应 Age 并不能证明没有缓存。Cloudflare 公开站点自动设置的 Cookie 值已明确脱敏，未删除 Date/cache 证据。

**精度限制与结论**：HTTP Date 只有整秒精度，并非可信 NTP 校准；产生 Date 的边缘节点、代理、缓存和不对称延迟均影响读数。在假设 Date 在请求区间生成、整秒向下取整且无缓存时，名义偏移区间为 Cloudflare `[+9.090904,+12.408340] s`、Microsoft `[+9.572560,+11.407341] s`。两个来源读数一致显示约 +10 秒偏移，但这些假设未获独立证明，因此只能说“响应 Date 领先本机采样约 10 秒”，不能宣布本机误差已被精确测定或已经同步。未改系统时钟；验收日志统一保存 UTC、原时区与此偏移 caveat，不能以毫秒级日志顺序证明跨机因果。

## 2. 真实工具发现与版本

证据：`tool-discovery.json`（PowerShell Get-Command）、`tool-versions.json`、`additional-python-versions.json`，均记录命令、起止、退出码、stdout/stderr。

| 工具 | 本次直接执行/发现结果 | 含义 |
|---|---|---|
| PATH Python | Python 3.10.9 | 不应把 PATH 默认等同唯一解释器 |
| PATH Python pip | 22.3.1，Python 3.10 | 未升级全局包 |
| `py -3.11` | Python 3.11.9 | 已存在，版本命令成功 |
| `py -3.12` | Python 3.12.10；pip 25.0.1 | 可作为隔离环境候选，不代表依赖组合已验证 |
| `py -3.13` | Python 3.13.7 | launcher 默认列为 3.13；本次版本命令成功 |
| `py -0p` 额外条目 | Astral CPython 3.12.9 路径 | 只列出，未单独执行；不能与上行 3.12.10 混淆 |
| Node | v22.23.1 | Vite 2/Vue 2 旧依赖在此 Node 上兼容性尚未执行验证 |
| npm | 11.4.1（`cmd.exe /d /c npm.cmd --version`） | Get-Command 默认 npm 是 npm.ps1；版本采样使用 cmd shim |
| Git | 2.47.1.windows.1 | 可完成新目录本地初始化/提交 |
| PHP / Composer | PATH 未发现 | 无真实版本可报；不是“全盘证明未安装” |
| Docker | PATH 与标准 Docker Desktop CLI 路径未发现 | daemon/WSL 可用性未知；未启动/安装 |
| MySQL / MariaDB / Redis server/CLI | PATH 未发现 | 不代表远端或容器服务不存在；未探测真实数据服务 |

有限补查位置含 `C:/php/php.exe`、`C:/xampp/php/php.exe`、`C:/Program Files/PHP*/php.exe`、`C:/ProgramData/ComposerSetup/bin/composer*`，均无匹配；不是全盘扫描。不得伪造缺失工具版本，也不应先装新 Python：已有 3.12 可供后续项目级 venv 方案使用。

## 3. 源身份、缺失与现有制品

### Git 与子模块

证据：`source-git.json`。源仓库真实存在，顶层为源路径；HEAD `6f111cd97dcbdaea23bf28768cd15dfa38bcae63`，提交主题 `release: v1.9.66`，提交时间 `2026-10-10T09:50:29Z`。`status --porcelain=v1 --untracked-files=no` 输出为空，只证明本次检查未发现 tracked 差异，不涵盖 untracked 文件。源 Git 命令使用 `--no-optional-locks` 与禁用 fsmonitor，未 checkout/reset/stage/update-submodule。

`.gitmodules:1-6` 的两个目录均为空，`git submodule status` 的 `-` 前缀证明未初始化，index 仍有精确 gitlink：

| 子模块 | pin | 官方声明 URL |
|---|---|---|
| resources/drawio | `88ee1f5ab4d40603a8f3aaa0d9d6dc3794c6b3f0` | https://github.com/jgraph/drawio.git |
| resources/mobile | `c5c5c100e742c302ae914df882af6210c80582d7` | https://github.com/kuaifan/dootask-app.git |

不能将“子模块未取回”写成“整个源 Git 缺失”。源只读前提下，后续只能在目标/独立目录按 pin 取回；本次未下载，也未验证这些远端 commit 可取回。

### 依赖与构建产物

- `node_modules`、`vendor` 不存在；`composer.lock` 存在。`package-lock.json`、`yarn.lock`、`pnpm-lock.yaml` 未发现；源 `.gitignore:50-52` 明确忽略 package-lock.json，缺锁并非可用任意版本自动补齐的证明。
- `package.json:3-11` 表明版本 1.9.66、构建入口 `./cmd prod`；`vite.config.js:11-18,126-143` 表明构建经 cmd 入口、主入口 `resources/assets/js/app.js`、产物为 `public/js/build`、manifest 位于 public，而非默认假设的 `public/build`。
- `public/manifest.json` 与 `public/js/build` **实际存在**。manifest 的 file/css/assets 引用去重后共 266 个，逐一读取字节并计算 SHA-256，缺失 0 个。记录 `source-artifact-hashes.json`。因此不能把“没有 node_modules”误判为“没有可检查前端制品”。
- 266 个引用存在不证明未列出的动态资源、子应用、运行服务齐全，更不证明这些制品由当前源码构建或每页面可用。源与制品的 REA 对照由对应成员负责，本报告不冒充 REA 证据。
- 本次引用源码逐文件原始字节 SHA-256 在 `source-input-hashes.json`，覆盖 AGENTS/CLAUDE、模块/忽略/文本规则、package/composer/lock、compose、Vite 与 manifest，便于检验后续契约是否引用同一版本。

### 原部署依赖的因果意义

`docker-compose.yml:2-38,40-117` 采用 PHP Swoole 8.4、nginx:alpine、redis:alpine、mariadb:10.7.3、dootask/appstore:0.6.0，并有 DB、共享数据和 Docker socket 依赖。`composer.json:10-43` 则声明 PHP ^8.3、Laravel ^13.0 和实际扩展清单。镜像声明不是本机运行版本；不能用“Python 已安装”推出此整套业务即可运行。FastAPI 迁移也不应把 PHP 缺失当必须保留 PHP 代理的理由；PHP 运行环境主要是后续获得可信旧系统 golden 行为的一个条件。

本次进一步直接读取 `app/Module/Interface/DooSo.php:22-44`：FFI 绑定 `/usr/lib/doo/doo.so`，声明 initialize/license/userCreate/tokenEncode/tokenDecode/md5s/PGP 等 ABI，并使用 `/var/www` 工作路径。这不仅是可任选 Python 包替代的接口名字：旧密码、Token、注册和授权语义必须有真实制品/行为证据才能确认。原文件原始字节哈希另见 `native-boundary-hash.json`。本职责无可用 Docker CLI，也未探测 daemon/WSL/镜像缓存，不声称缓存一定为空。协议成员交付位于 `docs/2026101204-官方协议版本与许可核验-feat.md`，其官方镜像获取渠道不等于已经拿到固定 digest、ABI 或独立二进制授权；后端成员负责制品获取合同。验收新增硬门禁：固定镜像 digest/制品哈希、平台 ABI、合法使用/分发边界及旧认证数据兼容证据均须明确，不能任意换 JWT/bcrypt 或绕许可冒充迁移。

## 4. 可自动补齐与真正先决条件

| 类别 | 事实与后续行动 | 当前边界 |
|---|---|---|
| 可本地完成 | 独立 Git、UTF-8/LF、secret/runtime 忽略规则 | 本批已实施，不触碰源 |
| 无须安装系统 Python | 可用 `py -3.12 -m venv .venv` 创建目标项目隔离环境 | 本批未创建 venv/未装依赖；须以协议成员最终版本契约锁定依赖 |
| 可在许可/网络满足后自动取回 | 目标/独立目录按 gitlink pin 获取 drawio/mobile；安装锁定 Python/JS 依赖 | 不修改源；不能在前端无锁时声称原构建已可复现 |
| 真实缺少的运行条件 | 未获得可运行的隔离 DB/Redis、旧系统基准服务及授权测试账号；PATH 无 PHP/Composer/Docker 等工具 | 需要明确后续可用本地容器、授权测试服务或等价隔离运行方案；不得连接真实用户库或假装已经具备 |
| 真实缺少的外部条件 | 授权、在线许可、应用商店、嵌入编辑器及其他全量台账所需外部服务的合法测试配置/凭据 | 留 BLOCKED，不用假 API/空返回伪装成功；不绕过授权 |
| 迁移门禁 | 完整功能/路由/模型/事件/文件/外部依赖台账、同版本协议及安全差异审批 | 本报告提供验收合同，不以本批未实现冒充完成，也不缩成 CRUD |

## 5. 独立验收合同（全部 NOT_RUN）

机器合同：`docs/evidence/runtime/acceptance-contract.json`。执行人必须独立于被验实现，待实现冻结及 PM 授权后集中运行一次；本批绝不运行。合同不是测试结果，也不是业务占位代码。

1. **全量对账**：每个源义务带唯一 ID、源码行号/哈希、前端入口、有效路由/WS、角色、持久实体、副作用、迁移实现位置、用例和证据。前端、后端、运行台账按 ID/count 全部对齐；缺项、BLOCKED、NOT_RUN 都阻止“全部完成”。动态 public 分派需逐项归类，文档 API 数不能替代真实 reachability，旧漏洞不作为必须复制的兼容要求。
2. **真实 UI**：真实 Chromium 逐入口打开、操作、直链刷新、弹窗/布局/主题/语言对照；同视口同账号状态截图，同时保留控制台、资源与网络证据。单截图、HTTP 200、页面能打开均不足。
3. **登录和权限**：真实登录/错误密码/过期/注销/撤销及全部认证入口；匿名、所有者、成员、管理员、无关用户矩阵覆盖 API、WS、文件直接地址和对象 ID 越权。验证拒绝后无数据泄漏、副作用和事件泄漏。
4. **持久化重启**：逐持久实体通过真实 UI/API 创建/更新/删除，记录 ID/数据库/文件状态；停止并重启本次拥有的 FastAPI 与存储服务，保留相同隔离卷；新浏览器会话和新进程重新读写。事务失败不留半写，不能以内存字典替代真实存储。
5. **API/WS**：逐有效路由覆盖实际方法、参数载体、分页/排序/筛选、错误与 ret/msg/data 等精确形状；WS 用两真实账号客户端验证认证、订阅、心跳、重连、顺序、离线/未读与拒绝路径。保留帧、响应、数据库关联，时间容差预先声明。
6. **文件及异步/集成**：真实非空文件上传、分块、下载哈希、预览、限制、取消、Unicode、删除、分享/WebDAV（若台账包含）、重启和越权；逐后台任务、通知、缓存、多 worker 与外部集成验证副作用。缺授权服务明确阻塞，不 mock PASS。
7. **真实性与交付**：记录进程命令行、端口、网络依赖与源码/制品哈希，证明业务由 FastAPI 执行而非转发 PHP；遵守许可报告、保留许可证/版权与修改日期及对应源码获取机制。合法外部集成与借旧 PHP 代执行业务要明确区分。
8. **安全与清理**：仅用隔离合成账号/DB/上传目录，原始敏感 traces 受控保存，提交件脱敏；命令退出码、运行时间、重启过程、截图/请求/WS/持久状态逐用例关联。清理只针对执行人拥有的测试资源，禁止全局终止或误删用户数据。

本批观察证据仅支撑环境及文件事实。上面任何业务用例通过数均为零，未宣称任何应用行为通过。

## 6. Git 初始化与提交边界

- `git-initialization.json` 记录新目录 `git init --initial-branch=main` 成功，初始目录原本无 .git。
- 根 `.gitattributes` 用 `text=auto eol=lf` 保留二进制，并为主要文本类型声明 UTF-8；不转换源目录、不重格式化源代码。
- 根 `.gitignore` 忽略环境凭据、私钥、依赖、缓存、数据库和运行上传/log；`docs/evidence/runtime/` 刻意可提交，既有前端发行资产不被总括忽略。规则不能取代人工脱敏。
- 初始提交仅显式列入已落盘 PM 管理文件 `docs/2026101001-迁移管理与证据索引-feat.md`、恢复管理文件 `docs/2026101201-中断恢复与分工跟踪-feat.md` 的当时快照、根两规则、本报告及本人列明的 runtime 证据。不使用 `git add .`，不 stage 其他成员正在写的 docs/evidence 区域。
- 使用单次 `git -c user.name=Automation -c user.email=automation@localhost commit`，不改全局身份。初始 commit 完成后向 PM 返回完整 hash；commit 自身 hash 不可能自包含进其内容，精确回执由本次工具输出及最终交接给出。
- 未设置远端、未 push。PM 后续更新管理文档和其他成员交付不包含在这个环境初始快照中，需后续独立集成任务提交。

## 7. 证据索引

所有下列文件均位于 `docs/evidence/runtime/`：

- `local-clock.json`：本机 ISO/UTC/时区/OS 原输出。
- `time-www.cloudflare.com.json`、`time-www.microsoft.com.json`：独立 HTTPS 头与时差采样，Cookie 唯一脱敏项。
- `tool-discovery.json`、`tool-versions.json`、`additional-python-versions.json`：工具查找及直接版本执行。
- `source-git.json`：源 HEAD、tracked status、子模块 pin、提交事实。
- `filesystem-observations.json`：文件工具缺失/存在观测和有限搜索边界；文件工具未提供秒级采样时间，诚实只记录日期。
- `source-input-hashes.json`、`source-artifact-hashes.json`：源码输入与 manifest 引用制品原始字节哈希。
- `acceptance-contract.json`：独立验收义务，全部 NOT_RUN。
- `native-boundary-hash.json`：直接观察的 DooSo FFI 边界源码原始字节哈希；不代表原生制品已获得。
- `git-initialization.json`：目标仓库初始化原始执行证据。

**终态**：本职责 G1 调查、验收合同与初始版本管理准备完成；全量迁移实现和运行验收尚未执行，必须保留真实阻塞，不宣布 G3 通过。

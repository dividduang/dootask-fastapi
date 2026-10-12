# FastAPI共享后端与新库合同

修改/交付日期：2026-10-12。源提交 `6f111cd97dcbdaea23bf28768cd15dfa38bcae63`；源目录全程只读。用户已选择全新数据库/账号，旧 md5s/Token互通不属于本轮要求。本报告是共享实施批次交付，不是全系统完成或验收证明。

## 1. 实施结果与不可夸大的边界

实际FastAPI代码、最终MariaDB DDL、项目venv/依赖锁、传输/身份/PGP/HTML静态宿主已经落盘。**全部运行检查 NOT_RUN**，没有启动服务、执行应用build/lint/test/formatter/smoke/UI。

本批注册10个原业务handler：登录、退出、当前用户、改密、密钥交换、Token过期/刷新、注册状态、验证码需要状态/图片/JSON。登录handler的注册分支保留官方许可门禁：没有可验证许可时返回 `ret=0,data.code=license_unavailable`，不会创建用户，也没有CLI初始化账号绕过。现有合法新账号可进入实际登录/持久设备/个人项目/改密事务代码；**由于缺合法账号/许可验证，不能宣称注册→登录完整路径已实测或已解除阻塞**。

逐项覆盖见 `docs/evidence/implementation/endpoint-coverage.json`：334业务中9项代码已实现未验证、1项登录含受阻分支、324项未注册；222意外继承候选明确拒绝。不以所有schema已落为334接口完成。后续四域正在独立实施，其活动文件不属于本次共享提交或覆盖统计。

## 2. 机器合同与准确源码绑定

- `backend-shared-contract.json`：`FROZEN_IMPLEMENTED_NOT_VERIFIED`；每个现存导出签名、Actor/RequestContext、真实表、事务/route/存储边界。没有以计划签名冒充已实现符号。
- `shared-source-review.json`：本次核心源码SHA256、审读行范围、PGP/认证/许可事实与未决材料。
- `schema-review.json / schema-migrations.json / schema-models.json / schema-seeds.json / schema-coverage.json`：221 migration up、77模型文件逐项来源、DDL/索引/数据回填/外部作用分类。
- `host-contract.json`：原Blade/manifest/router/安全注入证据、模板导出、拒绝规则；其中host子任务头像404状态已由父任务 `backend/avatar.py`实际实现覆盖，最终以主app注册与本报告为准。
- `dependency-lock-evidence.json / pip-*-report.json`：实际安装来源URL、精确版本、哈希及官方PyPI交叉。

重要源码：UsersController:75–176、293–416、620–644、2673–2698；User:380–431、746–841、989–1013；UserDevice:201–311；WebApi:22–80；DooSo:319–410；Project:654–732；WebSocketDialog:996–1019。Hash和范围见机器表，不凭熟悉Laravel而猜协议。

## 3. MariaDB最终schema与真实事务

`backend/schema.sql`含最终 **77表、716列、258索引（含主键）、8外键**，依221迁移的顺序最终效果整理；不是把up/down混合当生产快照。77模型文件实际为75持久模型+AbstractModel+clearHelper。历史字段重命名/索引/软删除/毫秒时间戳、附件stored generated cursor均保留。

`schema_seed.sql`只写5项settings默认值和空历史附件回填完成标记，共6行；不复制demo用户/示例项目/任务、不伪造system-functions应用安装。模型session.fillable的userid没有迁移列，未臆造。schema报告准确分开旧数据回填（新空库无历史）与外部安装（需后续真实执行）。

`security.sql`另加5安全表：auth_credentials、auth_sessions、auth_pgp_keys、auth_limits、auth_captchas。scrypt完整hash在独立255字符列，不塞源50字符password。原users IP列明确扩45兼容IPv6；其余基础schema独立保真。Token主体48随机字节URL-safe，仅SHA256入auth_sessions；源device MD5仅作设备/友盟关联，不能认证。

选择同步SQLAlchemy2 Core/PyMySQL，在线请求在threadpool中执行阻塞SQL/加密；业务接受同一个Connection贯穿事务。`db.transaction()`成功commit、异常rollback。账号行锁串行化登录/改密/设备管理；认证获得行锁后重读session以避免等待期间撤销竞态。验证码一次消费/限流独立事务，不能被错误密码回滚撤销。

`backend.manage init-db`拒绝非空库且检查MariaDB身份；DDL隐式commit如实说明，不承诺DDL原子性。业务表通过启动reflection导出`db.tables`，不建立77个无行为包装类。无SQLite、无PHP代理。

## 4. 请求、认证、PGP实际行为

ContextVar隔离request_id/language/platform/version/fd/token/ip/actor/pgp，新增headers字典提供sharekey/原header存在性给后续域，不把header userid当可信身份。退出finally清理；后台任务不得捕获请求上下文作为长期身份。

输入支持query、JSON、urlencoded、multipart以及源0.25.48前旧form分支；本共享身份body上限2MiB，后续真实上传域必须提供独立流式入口/配额，不应把此身份解析器强套大文件。保留Route::any七方法与method__action别名，但只从显式表选择处理分支。unknown接口Content-Type严格等于application/json时ret错误，否则404+X-Error-Message-Base64；没有422替换业务错误。

numeric ret/msg/data保留[]/对象等形状；ret=-1用于身份过期、未登录、禁用。读取原frontend/language/translate.json翻译既有消息；新增未登记消息回落明确中文，不编假翻译。

登录实际更新login_num/last_ip/last_at/line_ip/line_at、持久会话/设备/过期；设备上限200，超限撤销旧记录；logout幂等撤销device/session/友盟alias。改密验证旧密码、生成新scrypt/身份代次、撤销全部旧设备、签发新token。移动info和token/expire在剩余不足ceil(days/3)时刷新。

PGP header**不是JSON**，实际是`encrypt_type=pgp;encrypt_id=...;client_type=pgp;client_key=...`。公钥`-/_/$`恢复为`+/斜线/换行`；payload encrypted移除/恢复PGP armor。服务端实际PGPy RSA3072 key、AES256消息；客户端curve25519来自原OpenPGP.js，互操作仍待独立浏览器验收。私钥AES-GCM由必须配置的PGP_STORAGE_KEY加密存DB，季度过期；无debug关闭加密取巧。密码scrypt32768/r8/p1、独立盐、常量时间比较。

个人项目初始化同事务写project、owner、Default列、project聊天室、群主成员、project日志；检查包含soft-deleted个人项目，避免登录重复创建。它不是完整project.create API，没有对搜索/WS/外部hook伪称投递。已有全员群的注册需要真实群消息/webhook能力，当前明确不伪造其成功。

## 5. HTML/静态/资源

原前端不改，由`backend.host.install_host`最后注册宿主；真实manifest的app.js与css[0]注入。main保留原DOM、locale/title/loginLogo/debug/version/origin/homeUrl/apiUrl、CSRF meta/window与安全JSON转义；systemInfo仅来自真实Settings，未伪造运营人数、授权或在线状态。二级desktop/download/report/email模板均移植，业务数据必填；受信HTML需业务传Markup，普通字符串自动转义。

静态缺失返回404，不SPA替代；PHP、dotfiles、web.config、源码/配置不可公开。深链接白名单来自实际router，API/DAV/WS/special与静态分离。CSRF签名cookie供未来cookie身份入口明确验证，现有token API保留源豁免语义、不会通过cookie认证。

Index main/version/health/api/default有实际行为。avatar另按原过滤规则/原Source Han Sans字体实际渲染PNG；为稳定新库行为改为名字确定性dark颜色（源随机持久缓存），记录为有意差异而非像素等价；size限制16–512防资源滥用。其余Index特殊入口不回退SPA，留后续Files域。模板测试源码存在但未运行。

## 6. 依赖与隔离环境

项目`.venv`使用现有CPython3.12.10；pip实际解析安装28 runtime包。FastAPI0.143.0、Pydantic2.14.1、Uvicorn0.54.0、SQLAlchemy2.0.54、PyMySQL1.2.3、PGPy0.6.0、Jinja2 3.1.6、Pillow12.3.0、Redis6.4.0、PyYAML6.0.3等精确全量见requirements.lock。每个安装发行文件SHA256均与对应官方PyPI metadata匹配（28/28）；下载实际经过本机默认清华镜像，来源如实记录，不声称全由官方主机直接下载。

PGPy从sdist由pip构建wheel是依赖安装过程，不是运行应用build；build-isolation工具链尚未作为独立可复现build lock承诺。所有包安装不是安全审计或运行通过。曾一次相对python.exe调用无法定位，改为已存在绝对路径后安装成功；无重复环境创建、无全局修改。

RuntimeAcquire已独立配置MariaDB11.7.2与Redis7.4.1已存镜像，127.0.0.1:13306/16379、独立容器项目/volumes、随机私密凭据；新服务未启动。Redis有密码，后续域必须读取REDIS_URL，不采用无密码默认。

应DomainMessaging请求追加真实配置字段APPSTORE_ROOT/APPSTORE_URL/APP_KEY/AI_URL及PyYAML依赖；APP_KEY默认空不是授权。应DomainAccounts请求RequestContext.headers字典已真实填充。其余新域接入由下一唯一owner完成。

## 7. 许可真实阻塞，不把源码可查项写未知

已知：中央URL `https://appstore.dootask.com/api/v1/license/`，email/send、login、login/confirm、trial、renew完整payload/response可从OnlineLicense读取；指纹sn/macs/url/version、issue字段license/snapshot/instance_token均已整理。HTTP code200不是license已通过验签，源applyIssue仍调用Doo.licenseSave；User.reg先检查people/sn/mac，再userCreate原生限制。

真正缺失：有权使用的非PHP验签接口/SDK，或**公开验签公钥**、签名/编码格式、有效/过期/超额的合法签发样本；新Python实例受支持SN/硬件绑定规则；用户数/期限/IP/域名/邮箱政策及合法初始化路径；测试entitlement账号/邮件码访问或对应实例的离线授权。没有请求供应方签名私钥，没有把官方价格页推断成可自签3人授权。RuntimeAcquire的新制品调查另见1209及acquisition/native-acquisition-ledger.json。

注册的完整状态写入代码已与校验组织在一起，但当前fail-closed门禁使成功路径不可达；这是明确外部前提，不包装为“注册已完成”。不新增CLI账号来制造通过。旧md5s/token不再阻塞，全新内部安全实现已写。

## 8. 分工、Git与后续唯一集成责任

本任务实际有两个互斥子切片，均已交付退出、无活动子成员：

| 完整agent ID | 独占文件 | 结果 |
|---|---|---|
| BackendShared.SchemaDerive | backend/schema.sql、schema_metadata.json、schema_seed.sql；schema-*.json | completed，221/77逐项审查，NOT_RUN |
| BackendShared.HostAdapt | backend/host.py、backend/templates/；host-contract.json | completed，实际模板/宿主，NOT_RUN |

父BackendShared拥有其余共享代码/配置/文档与唯一Git写入。后续DomainAccounts/Projects/Messaging/Files已由PM单独派工；本提交**不stage**其backend/domains、jobs.py/realtime.py/outbox.sql或活动领域证据。

下一集成owner须：合并明确route mapping，加载域SQL/outbox.sql，显式注册jobs/scope，接`worker(settings, asyncio.Event)`及停止等待；接Accounts真实LDAP/SMTP函数；将必要协作创建事件接真实持久投递而非打空sink。域代码存在不等于主app已接入。Main与各域已收到边界。

G1、1207/1209及PM已完成文档证据可精确stage；保留真实39MB级REA JSON，禁止误当垃圾删去。官方大包/ASAR/子模块原归档不stage，原归档含上游android.jks保持隔离；frontend现存12317文件按交付manifest精确stage，私钥排除。源frontend/.gitattributes含`* text=auto`会覆盖根规则，因此本仓`.git/info/attributes`也写最高优先级frontend/** -text，确保本次入库原字节。该本地属性不随clone传播，后续保真工作树也需设置同等最高优先级规则；不能只凭根.gitattributes宣称所有checkout永远保字节。

## 9. 留给独立验收的实际检查

本批执行过的只有源码/哈希/官方metadata取证、venv创建和依赖安装。未连接MariaDB/Redis，未执行任何应用检查。可运行检查文件为backend/tests_shared.py、backend/templates/tests_host.py；合法新账号缺失时状态检查明确SKIP而非PASS。

独立阶段须先隔离空库应用DDL并反射对账，再做PGP实际浏览器双向、注册许可有效/过期/超额、密码/设备/注销并发、认证上下文隔离、事务回滚、静态拒绝/深链/注入、真实UI和各域全链副作用。完整切换还要求全部334业务与Index/DAV/WS/SSE/外部许可实现及验证；本批没有宣称满足该终态。

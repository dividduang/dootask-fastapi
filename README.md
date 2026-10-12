# DooTask FastAPI 新系统

修改日期：2026-10-12。源 `dootask` 只读；新数据库、新账号，不兼容旧密码或旧 Token。本项目保留原前端与许可证，不使用 PHP 业务代理。

**当前交付是共享层，不是全部 334 接口完成。** 原件前端在 `frontend/`；MariaDB schema、身份/传输/PGP/静态宿主已落代码但尚未运行验收。公开注册因尚无可验证的官方许可材料而明确拒绝；没有演示账号、假授权或绕限制的 bootstrap CLI。其他领域文件可能正在独立实施，尚未接入主应用，不能据文件存在宣称可用。

## 项目级启动（由独立验收执行）

Windows PowerShell，已有 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\run.ps1 secrets
```

依赖锁为 CPython 3.12 / Windows amd64 实际解析版本与发行文件 SHA-256；28 个包的哈希已与官方 PyPI metadata 交叉，安装不等于应用通过。PGPy 0.6.0 从 sdist 安装，pip 构建隔离工具不属于应用 runtime lock；严格离线重建还需单独冻结工具链。没有升级全局 Python。

MariaDB/Redis 隔离部署合同见 `docs/2026101209-隔离运行依赖与原生制品获取-feat.md` 及 `docs/evidence/acquisition/runtime-plan.json`。`.runtime/compose.yaml` 固定已取得 image ID、独立 volumes、端口 13306/16379；`.runtime/runtime.env` 和 Redis 密码配置是本机私密材料，不提交。**不要使用用户已有 3306/6379 容器或数据库。** 新环境需按运行合同合法重建私密配置；不能把缺失凭据替换为空密码。

数据库实际启动并健康后，使用专用全新空库：

```powershell
.\run.ps1 init-db
.\run.ps1 serve -ListenHost 127.0.0.1 -Port 18080
```

`init-db` 顺序执行 `schema.sql`、`security.sql`、`schema_seed.sql`，拒绝非空库。MariaDB DDL 会隐式提交，故初始化失败不能谎称整体回滚；只对独立专用库按日志恢复，绝不删除用户库。正常业务在真实 InnoDB 事务内。

`run.ps1` 读取 `.runtime/runtime.env`、`.runtime/backend.env`，后者 `secrets` 命令只创建不覆盖，保存 CSRF_SECRET/PGP_STORAGE_KEY。PGP_STORAGE_KEY 丢失会使现存服务端PGP密钥不可解密；私密目录限服务账号读取。Token身份不来自Cookie；HTTPS仍是必需部署条件。直接运行绑定loopback且不信任代理头；生产反向代理必须显式设置可信来源/IP。关闭 access log 防止原GET token/password进入URL日志；代理也必须脱敏。

## 真实模块边界

- `backend.db.transaction()` 提供共享 `Connection`；业务函数接同一连接，不自行提交；`db.tables` 启动时反射77业务表及5安全表。新schema原迁移221项/模型77项逐条映射证据可查。
- 原 `ret/msg/data`、header token/dootask-token、language/platform/version/fd、GET/form/JSON、真实OpenPGP包络；显式接口表拒绝反射/继承暴露。未实现业务不注册，不空返回成功。
- 新 scrypt 密码独立安全表；随机不透明Token仅存SHA256；源device hash仅作MD5关联，不作认证；过期/注销/改密撤销实际落库。个人项目/负责人/Default列/聊天室/成员/日志在同一事务创建。
- `frontend/public` 原manifest宿主保留注入及深链接，禁止PHP、配置、源码暴露；不存在的静态资源不回退HTML。二级模板必须由真实业务数据渲染。
- `/health` 仅进程存活，不是DB readiness；`/version`按源重定向系统version接口，本批该系统端点未实现。

精确可调用接口：`docs/evidence/implementation/backend-shared-contract.json`。逐556条（334业务+222意外候选）状态：`endpoint-coverage.json`。注册许可具体所需的合法服务接口、公开验签材料、实例绑定与有效/过期/超额样本列于 `shared-source-review.json`；不能用HTTP返回的license字符串直接认定已通过本地验签。

## 后续集成与验收

四领域目前独立实施；下一唯一集成owner负责显式API映射、域SQL、LDAP/SMTP身份接入、真实jobs/realtime生命周期、DAV/外部代理。共享层没有伪造事件投递成功；`jobs.py/outbox.sql`等后续文件不能在未接入时视为已发消息。所有代码状态均 **NOT_RUN**，未运行 build/lint/test/formatter/smoke/UI。

独立验收可先执行：

```powershell
.\.venv\Scripts\python.exe -m unittest backend.tests_shared backend.templates.tests_host
```

MariaDB状态检查需要 `DOOTASK_TEST_EMAIL`/`DOOTASK_TEST_PASSWORD` 指定**合法初始化**的新测试账号，否则明确跳过；不通过测试造许可。还必须做真实OpenPGP.js双向互操作、并发设备撤销/改密/事务失败、HTML资源与真实UI等检查；具体矩阵见1202/1203/1205报告。

## 许可证与源码

原许可证及版权保留在 `frontend/LICENSE` 等原路径，派生修改按适用AGPL条件交付；不存在“改成Python自动变MIT”。公开网络部署前须显著提供匹配实际部署版本的完整Corresponding Source（含原前端、修改、安装/构建文件），并完成未知二进制/外部许可审查。官方安装包/ASAR及含上游私钥的原始归档只保留隔离取证，不提交/不由Web公开；Android公开测试签名私钥按安全排除台账未复制入交付树。

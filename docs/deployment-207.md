# 207 Docker 部署交付记录

日期：2026-10-15。目标：内部编号 207 的 Ubuntu 22.04.5 amd64 服务器。本文为公开脱敏交付记录；实际连接地址、主机名和 SSH 用户保留在私有运维记录中。

## 部署结果

- 目录：`/opt/dootask-fastapi`
- Compose 项目：`dootask-fastapi`
- 源码基线：`ce02354f0b81aa0309f572685b7ff2a9ba17d3e1`，加本次 Docker 部署文件；此镜像构建于后续源码清理和 GitHub 提交之前。
- 应用镜像：`dootask-fastapi:ce02354-20261015`
- 应用镜像 ID：`sha256:0694ae0168707bdf169c1d5dc0f6cb4c1ce44e17e7259ed4c6af3b06cad07dfe`
- 应用、MariaDB、Redis 均为 `running / healthy`。
- 应用端口：`127.0.0.1:18080`；数据库及 Redis 不发布宿主端口。
- 服务配置 `restart: unless-stopped`；主机 Docker systemd 服务为 `enabled`。
- 数据卷：`dootask-fastapi_mariadb-data`、`dootask-fastapi_redis-data`、`dootask-fastapi_app-data`。
- 新系统使用全新数据库和本地账号，未迁移旧 DooTask 账号或数据。

固定的上游镜像摘要：

```text
python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
mariadb@sha256:fcc7fcd7114adb5d41f14d116b8aac45f94280d2babfbbb71b4782922ee6d8d4
redis@sha256:bb142a9c18ac18a16713c1491d779697b4e107c22a97266616099d288237ef47
```

207 直连 PyPI 发生 TLS EOF，构建改用 `https://pypi.tuna.tsinghua.edu.cn/simple`；37 个 runtime 包保留原版本，安装仍使用官方 PyPI 分发文件的 SHA256 校验。PGPy sdist 的隔离构建工具不属于 runtime 锁，不能将 runtime 锁描述为完整构建工具链锁。

## 访问与账号

在工作站开启并保持 SSH 隧道：

```sh
ssh -N -L 18080:127.0.0.1:18080 <ssh-user>@<server-host>
```

浏览器打开 `http://127.0.0.1:18080/login`。

管理员：`admin@example.test`。随机密码保存在服务器 `/opt/dootask-fastapi/.runtime/docker/admin-credentials.json`，权限为 `0600`。服务器 `.env` 同为 `0600`。在已有 root SSH 会话查看：

```sh
cat /opt/dootask-fastapi/.runtime/docker/admin-credentials.json
```

验证过程通过正常注册创建了该管理员、一个普通合成用户和样例项目、任务、文件。私密验证状态保存在应用数据卷的 `/data/deployment-state.json`。没有将密码或会话令牌写入本报告、Git 或终端日志。

## 已完成验证

1. 新 MariaDB 执行全部六个初始化 SQL 文件，初始化器保留拒绝非空库的保护。
2. readiness 同时验证 HTTP、已初始化数据库表和带密码的 Redis。
3. 真实 HTTP 注册、管理员身份、登录、用户信息、首页/登录页、实际构建的 JS/CSS 资源。
4. 真实项目和任务创建/读取，文件上传/下载并校验 SHA256。
5. 普通外部用户访问项目与文件被拒绝。
6. 真实已认证 WebSocket 打开并核对用户身份。
7. 退出登录后令牌失效。
8. `backend.tests_shared` 和 `backend.templates.tests_host`：8 个测试通过，包括真实数据库账号状态测试，无跳过。
9. 账号 contract、文件、项目、消息、integration 默认边界检查通过，`pip check` 通过。未将默认 pure checks 计为完整数据库业务验收。
10. 三个容器全部 `--force-recreate`，保留原卷；重新登录并读回原项目、任务、文件字节，权限和 WebSocket 再次通过。日志包含 `PERSISTENCE_PASS`。
11. 最终宿主请求 `/health` 返回 `ok`，三个容器均健康。

服务器日志：

```text
/opt/dootask-fastapi-build.log
/opt/dootask-fastapi-final-build.log
/opt/dootask-fastapi-start.log
/opt/dootask-fastapi-validation.log
/opt/dootask-fastapi-regression.log
/opt/dootask-fastapi-persistence.log
```

## 已知边界

沿用根 README 的受控隔离部署范围，未开放公网或不可信多人内网。已知前端内容信任边界风险仍未在本次部署中修复。SMTP、LDAP、Office、AI、搜索及外部应用商店集成未在本次配置或验收。

实际应用日志可见 `APP_KEY未配置，无法通知应用商店`，对应 onboarding outbox 的失败/退避；核心业务和容器 readiness 已通过，不代表外部应用商店可用。本次不是全部 326 个业务入口或生产就绪验收。

## 运维

```sh
cd /opt/dootask-fastapi
docker compose ps
docker compose logs --tail=100 app
docker compose restart app
docker compose exec -T app python -m deploy.smoke
```

后续启动直接 `docker compose up -d --wait`；不要重复初始化数据库，不要运行 `down -v`。升级前一起备份数据库、应用卷和私密配置（尤其 `PGP_STORAGE_KEY`）。通用部署说明见 `deploy/README.md`。

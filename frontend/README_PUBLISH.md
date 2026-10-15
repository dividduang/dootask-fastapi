# 前端构建与桌面发布

## Web 构建

使用 Node.js 20+，在 `frontend/` 目录执行：

```sh
npm ci
npm run build
```

构建直接调用本地 Vite，更新 `public/manifest.json` 与 `public/js/build/`。FastAPI 使用这些制品和 `backend/templates/*.html` 提供页面。构建无需启动数据库、Docker 或旧后端，也不创建或修改后端配置。

`npm start` 启动仅监听 loopback 的 Vite 资源开发服务器，默认端口为 5173，可通过 `APP_DEV_PORT` 调整。当前 FastAPI 宿主只加载构建制品，不接入 Vite HMR；联调页面需重新执行 `npm run build`，然后访问 FastAPI 地址。

可选前端配置见 `.env.example`；不要在前端配置中存放后端密码或令牌。

## Electron 桌面端

另外在 `frontend/electron/` 安装依赖：

```sh
cd electron
npm ci
cd ..
npm run electron
```

按交互提示选择桌面目标。`npm run electron -- dev` 打开本机 FastAPI 地址，默认端口为 18080，可用 `APP_PORT` 调整。桌面打包、公证与发布仍需要对应平台工具链和真实发布配置。

- `APPLEID`、`APPLEIDPASS`：macOS 公证。
- `CSC_LINK`、`CSC_KEY_PASSWORD`：代码签名。
- `GITHUB_TOKEN`、`GITHUB_REPOSITORY`：GitHub 发布。
- R2 存储发布配置由 `electron/lib/r2.js` 读取。

凭据只通过私有环境配置提供。自动发布需要另外配置 CI。

## 命令兼容与运行范围

`node cmd build` / `node cmd prod`、`node cmd dev` 和 `node cmd electron` 是上述命令的兼容入口；在支持 shebang 的系统也可执行 `./cmd`。旧 Laravel 安装、更新、迁移和数据库维护命令已移除；FastAPI 部署与备份流程见 [deploy/README.md](../deploy/README.md)。历史迁移报告和上游知识库中的旧操作说明不适用于当前分支。

Android/iOS 原生工程、EEUI/Weex SDK 及对应构建和上传命令也已移除。手机浏览器仍使用同一套 Web 页面。需要恢复旧工程时，可从 Git 历史查阅。

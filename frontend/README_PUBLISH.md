# 发布

## 准备工作

1. 添加环境变量 `APPLEID`、`APPLEIDPASS` 用于公证
2. 添加环境变量 `CSC_LINK`、`CSC_KEY_PASSWORD` 用于签名
3. 添加环境变量 `GITHUB_TOKEN`、`GITHUB_REPOSITORY` 用于发布到GitHub（GitHub Actions 发布不需要）
4. 添加环境变量 `PUBLISH_KEY` 用于发布到私有服务器

## 发布版本

> 翻译、版本号、更新日志改由 `dootask-release` 技能完成（见 `.claude/skills/dootask-release/`）。

```shell
npm run build       # 编译前端
```

说明：

- 执行 `npm run build` 作用是生成网页端；
- 本仓库保留 Windows、Mac 的 Electron 构建脚本；自动发布需要另外配置 CI 工作流。
- 如果想手动生成客户端执行 `./cmd electron` 根据提示选择操作。


## 移动端范围

当前 FastAPI 分支已移除 Android/iOS 原生工程、EEUI/Weex SDK 及对应构建和上传命令。手机浏览器仍使用同一套 Web 页面。需要恢复原生 App 构建时，可从 Git 历史恢复完整移动端工程。

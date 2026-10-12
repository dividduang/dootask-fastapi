# Dockerfile 项目

用于集中维护一组 Docker 镜像定义，并通过 GitHub Actions 自动完成多架构构建与推送。构建流程依赖 `.github/workflows/build.yml` 触发，调用 `build.sh` 脚本扫描包含 Dockerfile 的目录并逐一构建镜像。

## 构建与更新说明

- `build.sh list`：列出所有包含 `Dockerfile` 或 `*.Dockerfile` 的目录，供工作流生成构建矩阵。
- `build.sh build <目录> [Dockerfile]`：根据目录下的配置构建镜像。支持通过 `config.ini` 自定义仓库名、标签及是否强制覆盖。
- 向仓库添加新镜像时，在根目录下创建一个新文件夹，并放置对应的 `Dockerfile`（可选多个变体），必要时提供 `config.ini` 与私有资源。
- 更新现有镜像时，直接修改对应目录内容，工作流会在推送至 `main` 分支后自动构建并推送。

## 项目结构

```ini
.
├── build.sh                     # 构建脚本，供工作流和本地调用
├── .github/workflows/build.yml  # GitHub Actions 构建流程定义
├── README.md
└── <镜像目录>/                   # 每个目录对应一个镜像定义
    ├── Dockerfile                 # 主 Dockerfile
    ├── *.Dockerfile               # 可选的变体 Dockerfile
    └── config.ini                 # 可选的镜像配置
```

### config.ini 支持参数

- `imageName`：最终推送到 Docker Hub 的镜像名称（默认 `kuaifan/<目录>`）。
- `imageTag`：构建时使用的标签（默认 `latest`），若存在变体 Dockerfile，标签前会自动添加变体前缀。
- `imageForce`：是否强制构建并跳过已有镜像检查，可设为 `yes` 或 `no`（默认 `no`）。

示例：

```ini
imageName=kuaifan/custom-image
imageTag=v1.2.3
imageForce=yes
```

# DooTask 应用工具库

[![npm version](https://img.shields.io/npm/v/@dootask/tools.svg?style=flat-square)](https://www.npmjs.com/package/@dootask/tools)
[![npm downloads](https://img.shields.io/npm/dm/@dootask/tools.svg?style=flat-square)](https://www.npmjs.com/package/@dootask/tools)
[![license](https://img.shields.io/npm/l/@dootask/tools.svg?style=flat-square)](https://github.com/dootask/tools/blob/main/LICENSE)

这是一个为 DooTask 应用开发设计的工具库，提供了一系列实用的 API，用于与主应用进行交互。它封装了微前端通信、用户数据获取、页面交互等多种功能，让您的 DooTask 应用开发变得简单高效。

## 特点

- **类型支持** - 完善的 TypeScript 类型定义，提供智能提示
- **异步 API** - 所有方法都返回 Promise，支持异步操作
- **强大功能** - 提供完整的用户、系统信息和交互方法

## 安装

```bash
npm install @dootask/tools --save
```

## 使用方法

### 基本使用

直接引入并使用，所有方法都是异步的：

```typescript
import {
  appReady,
  isMicroApp,
  getThemeName,
  getUserInfo,
  openWindow,
  closeApp,
  UnsupportedError,
  // 更多API...
} from "@dootask/tools"

// 等待应用准备就绪
try {
  await appReady()
  console.log("应用已准备就绪")
} catch (error) {
  if (error instanceof UnsupportedError) {
    console.log("当前环境不支持微前端模式")
  }
}

// 检查是否在微前端环境中
const isMicro = await isMicroApp()
if (isMicro) {
  console.log("当前在微前端环境中运行")

  try {
    // 获取当前主题
    const theme = await getThemeName()
    console.log("当前主题：", theme)

    // 获取用户信息
    const user = await getUserInfo()
    console.log("用户信息：", user)

    // 打开新窗口（需要在 Electron 环境中）
    await openWindow({
      name: "my-window",
      url: "https://example.com",
    })

    // 关闭当前应用
    await closeApp()
  } catch (error) {
    if (error instanceof UnsupportedError) {
      console.log("当前操作在此环境下不支持")
    }
  }
}
```

## API 文档

### 应用状态相关

| 函数名             | 参数 | 返回值             | 异常               | 说明                                   |
| ------------------ | ---- | ------------------ | ------------------ | -------------------------------------- |
| `appReady()`       | -    | `Promise<void>`    | `UnsupportedError` | 应用准备就绪的 Promise 对象            |
| `isMicroApp()`     | -    | `Promise<boolean>` | -                  | 检查当前是否在微前端环境中运行         |
| `isEEUIApp()`      | -    | `Promise<boolean>` | -                  | 检查是否为 EEUI 应用（App 客户端）     |
| `isElectron()`     | -    | `Promise<boolean>` | -                  | 检查是否为 Electron 应用（电脑客户端） |
| `isMainElectron()` | -    | `Promise<boolean>` | -                  | 检查是否为主 Electron 窗口             |
| `isSubElectron()`  | -    | `Promise<boolean>` | -                  | 检查是否为子 Electron 窗口             |

### 用户和系统信息

| 函数名              | 参数 | 返回值                                      | 异常               | 说明                                         |
| ------------------- | ---- | ------------------------------------------- | ------------------ | -------------------------------------------- |
| `getThemeName()`    | -    | `Promise<string>`                           | `UnsupportedError` | 获取当前主题名称                             |
| `getUserId()`       | -    | `Promise<number>`                           | `UnsupportedError` | 获取当前用户 ID，0 表示未登录                |
| `getUserToken()`    | -    | `Promise<string>`                           | `UnsupportedError` | 获取当前用户 Token                           |
| `getUserInfo()`     | -    | `Promise<DooTaskUserInfo>`                  | `UnsupportedError` | 获取当前用户信息对象                         |
| `getBaseUrl()`      | -    | `Promise<string>`                           | `UnsupportedError` | 获取基础 URL                                 |
| `getSystemInfo()`   | -    | `Promise<DooTaskSystemInfo>`                | `UnsupportedError` | 获取系统信息对象                             |
| `getWindowType()`   | -    | `Promise<string>`                           | `UnsupportedError` | 获取页面类型，可能的值为 'popout' 或 'embed' |
| `getLanguageList()` | -    | `Promise<{[key: DooTaskLanguage]: string}>` | `UnsupportedError` | 获取语言列表                                 |
| `getLanguageName()` | -    | `Promise<DooTaskLanguage>`                  | `UnsupportedError` | 获取当前语言名称                             |
| `getSafeArea()`     | -    | `Promise<DooTaskSafeArea>`                  | `UnsupportedError` | 获取移动端安全距离                           |

### 应用控制

| 函数名                        | 参数                      | 返回值                | 异常               | 说明                                                                 |
| ----------------------------- | ------------------------- | --------------------- | ------------------ | -------------------------------------------------------------------- |
| `backApp()`                   | -                         | `Promise<void>`       | `UnsupportedError` | 返回上一页，返回到最后一个页面时会关闭应用                           |
| `closeApp(destroy?: boolean)` | `destroy?: boolean`       | `Promise<void>`       | `UnsupportedError` | 关闭当前应用，destroy 为 true 时销毁应用                             |
| `interceptBack(callback)`     | `callback: () => boolean` | `Promise<() => void>` | `UnsupportedError` | 设置应用关闭前的回调，返回 true 可阻止关闭。返回一个可注销监听的函数 |
| `nextZIndex()`                | -                         | `Promise<number>`     | -                  | 获取下一个可用的模态框 z-index                                       |

### 窗口操作

| 函数名                  | 参数                          | 返回值          | 异常               | 说明                                           |
| ----------------------- | ----------------------------- | --------------- | ------------------ | ---------------------------------------------- |
| `popoutWindow(params?)` | `params?: PopoutWindowParams` | `Promise<void>` | `UnsupportedError` | 应用窗口独立显示                               |
| `openWindow(params)`    | `params: OpenWindowParams`    | `Promise<void>` | `UnsupportedError` | 打开新窗口（只在 isElectron 环境有效）         |
| `openTabWindow(url)`    | `url: string`                 | `Promise<void>` | `UnsupportedError` | 在新标签页打开 URL（只在 isElectron 环境有效） |
| `openAppPage(params)`   | `params: OpenAppPageParams`   | `Promise<void>` | `UnsupportedError` | 打开应用页面（只在 isEEUIApp 环境有效）        |

### 用户交互

| 函数名                    | 参数                          | 返回值                              | 异常                             | 说明                                         |
|---------------------------|-------------------------------|-------------------------------------|----------------------------------|----------------------------------------------|
| `selectUsers(params)`     | `params: SelectUsersParams`   | `Promise<number[]>`                 | `UnsupportedError`               | 选择用户，可通过多种配置自定义选择器         |
| `requestAPI(params)`      | `params: requestParams`       | `Promise<ApiSuccess>`               | `UnsupportedError` \| `ApiError` | 请求服务器 API                              |
| `fetchUserBasic(userid)`  | `userid: number \| number[]`  | `Promise<DooTaskUserBasicInfo[]>`   | `UnsupportedError` \| `ApiError` | 查询用户基本信息                            |
| `setCapsuleConfig(config)`| `config: CapsuleConfig`       | `Promise<void>`                     | `UnsupportedError`               | 设置胶囊配置                                 |

### 提示框

| 函数名                  | 参数                             | 返回值             | 异常               | 说明           |
| ----------------------- | -------------------------------- | ------------------ | ------------------ | -------------- |
| `modalSuccess(message)` | `message: string \| ModalParams` | `Promise<void>`    | `UnsupportedError` | 弹出成功提示框 |
| `modalError(message)`   | `message: string \| ModalParams` | `Promise<void>`    | `UnsupportedError` | 弹出错误提示框 |
| `modalWarning(message)` | `message: string \| ModalParams` | `Promise<void>`    | `UnsupportedError` | 弹出警告提示框 |
| `modalInfo(message)`    | `message: string \| ModalParams` | `Promise<void>`    | `UnsupportedError` | 弹出信息提示框 |
| `modalConfirm(message)` | `message: string \| ModalParams` | `Promise<boolean>` | `UnsupportedError` | 弹出确认提示框 |
| `modalAlert(message)`   | `message: string`                | `Promise<void>`    | `UnsupportedError` | 弹出系统提示框 |

### 消息框

| 函数名                    | 参数              | 返回值          | 异常               | 说明         |
| ------------------------- | ----------------- | --------------- | ------------------ | ------------ |
| `messageSuccess(message)` | `message: string` | `Promise<void>` | `UnsupportedError` | 弹出成功消息 |
| `messageError(message)`   | `message: string` | `Promise<void>` | `UnsupportedError` | 弹出错误消息 |
| `messageWarning(message)` | `message: string` | `Promise<void>` | `UnsupportedError` | 弹出警告消息 |
| `messageInfo(message)`    | `message: string` | `Promise<void>` | `UnsupportedError` | 弹出信息消息 |

### 打开特定窗口

| 函数名                          | 参数               | 返回值         | 异常               | 说明                                       |
| ------------------------------- | ------------------ | -------------- | ------------------ | ------------------------------------------ |
| `openDialog(dialogId)`          | `dialogId: number` | `Promise<any>` | `UnsupportedError` | 打开对话框                                 |
| `openDialogNewWindow(dialogId)` | `dialogId: number` | `Promise<any>` | `UnsupportedError` | 打开对话框（新窗口，仅支持 Electron 环境） |
| `openDialogUserid(userid)`      | `userid: number`   | `Promise<any>` | `UnsupportedError` | 打开对话框（指定用户）                     |
| `openTask(taskId)`              | `taskId: number`   | `Promise<any>` | `UnsupportedError` | 打开任务                                   |
| `downloadUrl(url)`              | `url: string`      | `Promise<any>` | `UnsupportedError` | 下载文件                                   |

### 扩展功能

| 函数名                                   | 参数                                    | 返回值         | 异常               | 说明                              |
| ---------------------------------------- | --------------------------------------- | -------------- | ------------------ | --------------------------------- |
| `callExtraA(methodName, ...args)`        | `methodName: string, ...args: any[]`    | `Promise<any>` | `UnsupportedError` | 调用 $A 上的额外方法              |
| `callExtraStore(actionName, ...payload)` | `actionName: string, ...payload: any[]` | `Promise<any>` | `UnsupportedError` | 调用 $store.dispatch 上的额外方法 |
| `callExtraEmitter(eventName, ...args)`   | `eventName: string, ...args: any[]`     | `Promise<any>` | `UnsupportedError` | 调用 emitter.emit 上的额外方法    |

### PopoutWindowParams

```typescript
interface PopoutWindowParams {
  title?: string // 窗口标题
  titleFixed?: boolean // 窗口标题是否固定
  width?: number // 窗口宽度
  height?: number // 窗口高度
  minWidth?: number // 窗口最小宽度
  url?: string // 自定义访问地址，如果为空则打开当前页面
}
```

### OpenWindowParams

```typescript
interface OpenWindowParams {
  name?: string // 窗口唯一标识
  url?: string // 访问地址
  force?: boolean // 是否强制创建新窗口，而不是重用已有窗口
  config?: WindowConfig // 窗口配置
}
```

### OpenAppPageParams

```typescript
interface OpenAppPageParams {
  title?: string // 页面标题
  titleFixed?: boolean // 窗口标题是否固定
  url?: string // 访问地址
}
```

### SelectUsersParams

```typescript
interface SelectUsersParams {
  value?: string | number | Array<any> // 已选择的值，默认值: []
  uncancelable?: Array<any> // 不允许取消的列表，默认值: []
  disabledChoice?: Array<any> // 禁止选择的列表，默认值: []
  projectId?: number // 指定项目ID，默认值: 0
  noProjectId?: number // 指定非项目ID，默认值: 0
  dialogId?: number // 指定会话ID，默认值: 0
  showBot?: boolean // 是否显示机器人，默认值: false
  showDisable?: boolean // 是否显示禁用的，默认值: false
  multipleMax?: number // 最大选择数量
  title?: string // 弹窗标题
  placeholder?: string // 搜索提示
  showSelectAll?: boolean // 显示全选项，默认值: true
  showDialog?: boolean // 是否显示会话，默认值: false
  onlyGroup?: boolean // 仅显示群组，默认值: false
}
```

### requestParams

```typescript
interface requestParams {
  url: string // 请求地址
  method?: string // 请求方式
  data?: any // 请求数据
  timeout?: number // 请求超时时间
  header?: any // 请求头
  spinner?: boolean // 是否显示加载动画
}
```

### ModalParams

```typescript
interface ModalParams {
  title: string // 标题
  content?: string // 内容
  width?: number // 宽度
  okText?: string // 确定按钮文本
  cancelText?: string // 取消按钮文本
  scrollable?: boolean // 是否可滚动
  closable?: boolean // 是否可关闭
}
```

## 使用示例

### 检测运行环境

```typescript
import { appReady, isMicroApp, isElectron, isEEUIApp, UnsupportedError } from "@dootask/tools"

// 使用 try-catch 处理异常
try {
  await appReady()
  console.log("应用已准备就绪")
} catch (error) {
  if (error instanceof UnsupportedError) {
    console.log("当前环境不支持微前端模式")
  }
}

// 环境检测方法不会抛出异常，而是返回 boolean
const isMicro = await isMicroApp()
if (isMicro) {
  console.log("在微前端环境中运行")

  // 检测是否为Electron应用
  const isElectronEnv = await isElectron()
  if (isElectronEnv) {
    console.log("在Electron环境中运行")
  }

  // 检测是否为EEUI应用
  const isEEUI = await isEEUIApp()
  if (isEEUI) {
    console.log("在EEUI应用环境中运行")
  }
} else {
  console.log("不在微前端环境中运行")
}
```

### 应用关闭拦截

```typescript
import { interceptBack, modalConfirm } from "@dootask/tools"

let hasUnsavedChanges = true

// 设置应用关闭前的回调
const unsubscribe = interceptBack(async data => {
  if (hasUnsavedChanges) {
    // 如果有未保存的数据，则阻止关闭
    if (await modalConfirm("有未保存的数据，确定要关闭吗？")) {
      // 用户确认关闭，可以执行保存操作
      saveData()
      return false // 允许关闭
    } else {
      return true // 阻止关闭
    }
  }
  return false // 没有未保存的数据，允许关闭
})

// 取消监听
// unsubscribe();
```

### 选择用户

```typescript
import { selectUsers } from "@dootask/tools"

// 选择用户
selectUsers({
  value: [], // 已选择的值
  projectId: 123, // 指定项目ID
  title: "选择成员", // 弹窗标题
  showSelectAll: true, // 显示全选项
}).then(result => {
  console.log("选择的用户：", result)
})

// 选择群组
selectUsers({
  value: [],
  onlyGroup: true, // 仅显示群组
  showBot: false, // 不显示机器人
}).then(result => {
  console.log("选择的群组：", result)
})
```

### 弹出窗口和页面

```typescript
import { popoutWindow, openWindow, openTabWindow, openAppPage, isElectron, isEEUIApp } from "@dootask/tools"

// 将当前页面作为独立窗口显示
popoutWindow()

// 将当前页面作为独立窗口显示（自定义窗口信息）
popoutWindow({
  title: "独立窗口", // 窗口标题
  width: 1000, // 窗口宽度
  height: 700, // 窗口高度
  minWidth: 800, // 窗口最小宽度
})

// 在Electron环境中打开新窗口
isElectron().then(isElectron => {
  if (isElectron) {
    openWindow({
      name: "my-window-id", // 窗口唯一标识
      url: "https://example.com", // 访问地址
      force: false, // 是否强制创建新窗口，而不是重用已有窗口
      config: {
        title: "标题", // 窗口标题
        titleFixed: true, // 窗口标题是否固定
        width: Math.min(window.screen.availWidth, 1200), // 窗口宽度
        height: Math.min(window.screen.availHeight, 800), // 窗口高度
      },
    })

    // 在新标签页打开URL
    openTabWindow("https://example.com")
  }
})

// 在EEUI环境中打开应用页面
isEEUIApp().then(isEEUI => {
  if (isEEUI) {
    openAppPage({
      title: "标题", // 页面标题
      titleFixed: true, // 窗口标题是否固定
      url: "https://example.com", // 访问地址
    })
  }
})
```

### 请求服务器 API

```typescript
import { requestAPI } from "@dootask/tools"

// 请求服务器API
requestAPI({
  url: "users/info", // 访问接口路径，接口文档请查看 https://你的域名/docs/index.html
}).then(res => {
  console.log(res)
})
```

### 显示提示框

```typescript
import { modalSuccess, modalError, modalWarning, modalInfo, modalAlert } from "@dootask/tools"

// 显示成功提示
modalSuccess("操作成功！")

// 显示错误提示
modalError("操作失败！")

// 显示警告提示
modalWarning("请注意！")

// 显示信息提示
modalInfo("提示信息")

// 显示系统提示框
modalAlert("系统消息")

// 使用复杂参数
modalSuccess({
  title: "成功",
  content: "操作已完成",
  width: 400,
})
```

### 弹出消息框

```typescript
import { messageSuccess, messageError, messageWarning, messageInfo } from "@dootask/tools"

// 弹出成功消息
messageSuccess("操作成功！")

// 弹出错误消息
messageError("操作失败！")

// 弹出警告消息
messageWarning("请注意！")

// 弹出信息消息
messageInfo("提示信息")
```

## 示例项目

我们提供了一个完整的示例项目，展示如何在 Vue 3 + Vite 项目中使用 `@dootask/tools`：

### 查看示例

```bash
# 进入示例目录
cd example

# 安装依赖
npm install

# 启动开发服务器
npm run dev
```

示例项目包含以下功能演示：

- **应用状态检测** - 检测微前端环境、获取用户信息、主题、语言等
- **窗口管理** - 打开独立窗口、新窗口等操作
- **用户交互** - 用户选择器、API 请求等
- **应用控制** - 关闭应用、返回操作等
- **提示框** - 各种类型的提示框演示

## 注意事项

1. **环境检测与异常处理**: 这个库会自动检测是否在微前端环境中运行。如果不在微前端环境中，标注了 `UnsupportedError` 的方法将抛出 `UnsupportedError` 异常，而环境检测方法（如 `isMicroApp`、`isElectron` 等）会返回 `false`。

2. **异步操作**: 所有方法都是异步的，返回 Promise 对象，需要使用 `await` 或 `.then()` 来处理结果。建议使用 try-catch 语句捕获 `UnsupportedError` 异常。

3. **应用初始化**: 在使用任何方法之前，建议先调用 `appReady()` 确保应用已准备就绪。

4. **环境限制**: 某些方法只在特定环境中有效（如 `openWindow` 只在 Electron 环境中有效），使用前请检查运行环境。

5. **扩展调用**: 如果你希望调用 `$A` 上的方法，可以使用 `callExtraA` 方法。

6. **示例学习**: 建议先运行示例项目了解各种功能的使用方法。

## 贡献和反馈

如果你在使用中发现任何问题，或者有改进建议，欢迎在 GitHub 仓库提交 Issue 或 Pull Request。

## 后端服务

DooTask 提供多语言后端 SDK，方便在服务端调用同一套 API：

- [Go](server/go)
- [Python](server/python)
- [Node](server/node)

## 许可证

MIT

# B站视频下载器

一个用于 AstrBot 的 B站视频下载插件，通过调用 [BBDown](https://github.com/nilaoda/BBDown) 命令行工具来下载B站视频。

## 功能特性

- 🎬 通过QQ命令直接下载B站视频
- 🍪 支持多种Cookie格式自动解析
- ⚙️ 可配置的下载选项
- 📁 自定义下载路径
- 🎯 支持选择清晰度、分P等高级选项

## 安装要求

1. 安装 [BBDown](https://github.com/nilaoda/BBDown) 并确保 `BBDown` 命令在系统PATH中可用
2. 确保 AstrBot 已正确安装并运行

## 使用方法

### 查看帮助

```
/bili-help
```

显示所有可用命令和帮助信息。

### 基本下载

```
/bili <视频URL>
```

示例：
```
/bili https://www.bilibili.com/video/BV1qt4y1X7TW
```

### 设置Cookie

为了下载需要登录的视频，需要设置Cookie：

```
/bili-cookie <cookie字符串>
```

### 测试Cookie

测试Cookie是否有效：

```
/bili-test-cookie [cookie字符串]
```

如果不提供cookie参数，则测试当前配置的cookie。测试成功后会显示用户信息。

Cookie支持多种格式，程序会自动识别：

1. **浏览器格式**（推荐）：
   ```
   SESSDATA=xxx; DedeUserID=xxx; bili_jct=xxx
   ```

2. **Netscape格式**：
   从浏览器导出的cookie文件格式

3. **JSON格式**：
   ```json
   {"SESSDATA": "xxx", "DedeUserID": "xxx"}
   ```

4. **纯文本格式**：
   ```
   SESSDATA=xxx
   DedeUserID=xxx
   ```

### 设置配置

通过命令设置各种配置项：

```
/bili-set <配置项> <值>
```

可用配置项：
- `bbdown_path` - BBDown可执行文件路径
- `download_path` - 下载保存路径
- `quality` - 默认清晰度（8K/4K/1080P60/1080P/720P60/720P/480P/360P，留空表示自动）
- `danmaku` - 是否下载弹幕（true/false 或 是/否）
- `subtitle` - 是否下载字幕（true/false 或 是/否）
- `single_pattern` - 单个视频命名格式
- `multi_pattern` - 分P视频命名格式

示例：
```
/bili-set download_path ./videos
/bili-set quality 1080P
/bili-set danmaku true
/bili-set single_pattern <视频标题>[<清晰度>]
```

### 查看配置

```
/bili-config
```

### 查看命名格式参数

```
/bili-naming
```

查看所有可用的文件命名格式参数，用于自定义文件命名。

## 配置说明

配置文件位于 `data/config/bilidownloader_config.json`

```json
{
  "bbdown_path": "BBDown",
  "download_path": "./downloads",
  "cookie": "",
  "default_options": {
    "quality": "",
    "download_danmaku": false,
    "download_subtitle": true
  }
}
```

### 配置项说明

- `bbdown_path`: BBDown可执行文件的路径，如果BBDown在PATH中，直接写 "BBDown" 即可
- `download_path`: 视频下载保存路径
- `cookie`: 默认使用的Cookie（可通过命令设置）
- `default_options`: 默认下载选项
  - `quality`: 默认清晰度（空表示不指定）
  - `download_danmaku`: 是否下载弹幕
  - `download_subtitle`: 是否下载字幕
- `naming`: 文件命名格式配置
  - `single_video_pattern`: 单个视频文件命名格式（默认：`<videoTitle>[<dfn>]`）
  - `multi_video_pattern`: 分P视频文件命名格式（默认：`<videoTitle>/[P<pageNumberWithZero>]<pageTitle>[<dfn>]`）
  
  使用 `/bili-naming` 命令查看所有可用参数。

## 命令列表

### 下载相关
- `/bili` - 下载B站视频（别名：`/bilibili`, `/b站`, `/B站`）

### 配置相关
- `/bili-set` - 设置插件配置（别名：`/bilibili-set`, `/b站设置`, `/B站设置`）
- `/bili-config` - 查看当前配置（别名：`/bilibili-config`, `/b站配置`, `/B站配置`）

### Cookie相关
- `/bili-cookie` - 设置B站Cookie（别名：`/bilibili-cookie`, `/b站cookie`, `/B站cookie`）
- `/bili-test-cookie` - 测试Cookie是否有效（别名：`/bilibili-test-cookie`, `/b站测试cookie`, `/B站测试cookie`, `/测试cookie`）

### 命名格式相关
- `/bili-naming` - 查看文件命名格式可用参数（别名：`/bilibili-naming`, `/b站命名`, `/B站命名`）

### 帮助
- `/bili-help` - 显示所有可用命令和帮助信息（别名：`/bilibili-help`, `/b站帮助`, `/B站帮助`, `/bili帮助`）

💡 提示：使用 `/bili-help` 可以查看所有命令的详细说明

## 注意事项

1. 首次使用前请确保已安装BBDown
2. 下载需要登录的视频时，请先设置Cookie
3. Cookie会保存在配置文件中，请注意安全
4. 下载大文件可能需要较长时间，请耐心等待

## 相关链接

- [BBDown项目地址](https://github.com/nilaoda/BBDown)
- [AstrBot文档](https://astrbot.app)

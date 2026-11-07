# DLsite 收藏比对工具

一个在本机运行的 DLsite 收藏／搜索结果管理工具：扫描本地文件夹名中的 RJ 号，与 DLsite 搜索结果或收藏夹比对，找出本地尚未保存的作品。

这是非官方第三方工具，与 DLsite 无关。程序本身不提供作品下载，也不保存或上传你的本地媒体文件。

## 主要功能

- 扫描多个本地目录，只根据**文件夹名**中的 RJ 号建立索引。
- 抓取 DLsite 搜索结果、收藏夹等列表页，支持自动翻页。
- 合并日本主站、翻译站及不同语言版本的 RJ 号；任一版本在本地存在即判为已下载。
- 保存多份比对记录，可单独刷新、重命名或删除。
- 未下载、已下载、收藏、忽略分标签展示，支持分页、排序、筛选及 CSV 导出。
- 作品与社团的忽略、作品收藏均为全局设置，自动同步到所有比对记录。
- 支持单个作品重新检测本地状态，无需刷新整份结果。
- 本地库列表、下载时间、个人评分、DLsite 官方评分导入及本地库 CSV 导出。
- 可选检测 alist-public、anime-sharing、asmr.one 是否出现对应 RJ 号的搜索结果。
- 增量目录扫描和资源结果缓存，减少重复遍历与网络请求。

## 运行环境

- Windows 为主要测试环境。
- Python 3.9 或更高版本，仅使用标准库，无需安装 Python 第三方包。
- Node.js 为可选组件，仅用于 `npm run dev` 启动器和前端回归测试。
- 访问 DLsite 需要可用的网络环境；可在界面中设置直连或 HTTP 代理。

## 快速开始

### 方式一：直接用 Python

在项目目录执行：

```bash
python server.py
```

然后打开：

```text
http://localhost:7100/
```

Windows 也可以双击 `启动工具.bat`。如果希望无命令行窗口运行，可在安装并配置好 Python 后使用 `后台启动.vbs`。

### 方式二：通过 npm 启动

已安装 Node.js 时，可直接执行，无需 `npm install`：

```bash
npm run dev
```

如需指定端口：

```bash
python server.py --port 7100
# 或
npm run dev -- --port 7100
```

服务默认只监听 `127.0.0.1`。不要通过 `--host 0.0.0.0` 或端口转发把它暴露到局域网或公网，因为界面可以读取本地库信息、保存设置并打开本地文件夹。

## 第一次使用

1. 展开「目录与代理设置」。
2. 填入一个或多个本地作品目录，每行一个。
3. 根据网络情况选择代理：`auto` 自动检测、`none` 直连，或填写 `http://127.0.0.1:端口`。
4. 点击「保存设置」。
5. 点击「新建比对」，粘贴 DLsite 搜索结果页或收藏夹链接并开始比对。

每次新建比对或刷新旧结果时，都会自动重新扫描本地目录、更新翻译版 RJ 对照，再执行比对。

### 比对收藏夹

收藏夹等登录页面需要 DLsite Cookie。界面中的「如何获取 DLsite Cookie？」提供了分步说明。

**Cookie 相当于登录凭据。** 它只应保存在自己电脑的 `config.json` 中，不要发送给他人、截图公开或提交到 Git。公开仓库已经通过 `.gitignore` 排除该文件。

## 数据与隐私

所有数据默认保存在项目目录。除你主动发起的 DLsite 查询和资源站搜索所需的网络请求外，本工具不会把本地索引、比对记录或设置上传到第三方服务。运行后可能生成：

- `config.json`：本地目录、代理、并发数和可选 Cookie。
- `local_index.json`、`scan_cache.json`：本地索引及增量扫描缓存。
- `results/`：比对记录。
- `global_ignore.json`、`global_fav.json`：全局忽略与收藏清单。
- `ratings.json`、`ratings_manual.json`：评分与手动评分标记。
- `editions_cache.json`、`translate_info.json`、`rj_alias.json`、`maker_cache.json`、`resource_cache.json`：作品信息和资源检测缓存。

发布或分享代码时，请只分享源码，不要分享上述个人数据。仓库内的 `.gitignore` 已排除它们。

## 回归测试

```bash
python -m unittest discover -s tests -p "test_*.py" -v
node tests/state_sync_test.js
```

Windows 也可双击 `运行回归测试.bat`。测试使用内存模拟数据，不联网，也不读取或修改真实比对记录。

## 项目结构

```text
dlsite-compare/
├── server.py              # 后端、抓取、解析与比对逻辑
├── rescheck.py            # 可选资源站搜索检测
├── server.js              # 可选的 npm / Node 启动器
├── package.json
├── web/index.html         # 无构建步骤的单页前端
├── tests/                 # 离线回归测试
├── config.example.json    # 空配置示例
├── CHANGELOG.md
└── PUBLISHING.md          # GitHub 发布步骤
```

## 已知限制

- 文件夹名没有 RJ 号时无法自动匹配。
- DLsite 或资源站页面结构变化后，解析器可能需要更新。
- 资源检测只表示站点搜索中是否出现对应 RJ 号，不保证内容有效，也不提供下载功能。
- 请控制请求频率，并遵守相关网站的服务条款、robots 规则及适用法律。
- 个别站点可能要求登录、人机验证或限制自动请求；检测失败时应以人工访问结果为准。

## 许可证

本项目采用 [MIT License](LICENSE)。在保留版权与许可证声明的前提下，允许他人使用、复制、修改、合并、发布、分发、再许可及销售软件副本；软件按“原样”提供，不附带担保。

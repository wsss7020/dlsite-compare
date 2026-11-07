# 发布到 GitHub

建议先建**私有仓库**检查一次页面和文件，确认没有个人信息后再改为 Public。只发布本目录 `dlsite-compare-release`，不要上传原始工作目录。

## 发布前确认

- [ ] `config.json` 不存在于发布目录；个人 Cookie 不得出现在任何文件中。
- [ ] 不含 `results/`、本地索引、收藏／忽略清单、评分和缓存。
- [ ] 不含自己的本地目录、Windows 用户名或 Kimi Work 专用路径。
- [ ] Python 与 Node 回归测试通过。
- [ ] 决定是否添加 LICENSE；没有许可证不等于允许他人自由使用。
- [ ] README 中的功能、限制和第三方站点说明符合当前版本。

## 方法一：使用 Git 命令行（推荐）

### 1. 在 GitHub 新建空仓库

打开 GitHub，点击右上角 `+` → `New repository`：

- Repository name：`dlsite-compare`
- Description：`Compare DLsite search results and favorites with a local RJ-number library`
- 初次建议选 Private。
- 不要勾选自动创建 README、`.gitignore` 或 LICENSE，避免与本地文件冲突。

### 2. 在本目录打开终端并执行

把 `<你的用户名>` 换成真实 GitHub 用户名：

```bash
git init
git add .
git status
git commit -m "Initial public release"
git branch -M main
git remote add origin https://github.com/<你的用户名>/dlsite-compare.git
git push -u origin main
```

执行 `git status` 后应看到准备提交的是源码、文档、测试和 `.gitignore`，不应看到 `config.json` 或任何个人 JSON 数据。Git 要求提交前配置姓名和邮箱；邮箱可使用 GitHub 提供的 noreply 地址。

如果 GitHub 提示身份验证，按提示使用浏览器授权或 Personal Access Token；不要把密码或 Token 写进代码和文档。

### 3. 检查并公开

在 GitHub 仓库页面逐个确认 README、`.gitignore` 和源码内容。若是 Private，可进入：

`Settings` → `General` → 最下方 `Danger Zone` → `Change repository visibility` → `Make public`

## 方法二：只用 GitHub 网页

1. 按上面的要求新建空仓库。
2. 打开仓库，选择 `Add file` → `Upload files`。
3. 上传本发布目录中的文件，并保持 `web/`、`tests/` 目录结构。
4. 确认隐藏文件 `.gitignore` 也已上传。
5. 填写提交说明 `Initial public release` 并提交。

网页上传适合首次发布；后续持续维护更推荐 Git 命令行或 GitHub Desktop。

## 创建 v1.0.0 Release

仓库推送完成后：

1. 打开仓库右侧 `Releases` → `Create a new release`。
2. 点击 `Choose a tag`，输入 `v1.0.0` 并选择创建新标签，目标分支为 `main`。
3. Release title 填 `DLsite 收藏比对工具 v1.0.0`。
4. 在说明中概括核心功能、运行方法和已知限制。
5. 通常无需另传 ZIP：GitHub 会自动生成 Source code (zip / tar.gz)。
6. 先保存草稿并检查，确认后点击 `Publish release`。

可使用的 Release 简介：

```markdown
首个公开发布版本。

- DLsite 搜索结果／收藏夹与本地 RJ 号比对
- 日本主站、翻译站及多语言版本归并
- 持久化比对记录、全局收藏／忽略、本地库管理
- 排序、分页、CSV 导出、个人评分
- 可选资源站搜索检测

运行：Python 3.9+ 执行 `python server.py`，然后访问 `http://localhost:7100/`。

详见 README。使用前请备份个人数据；不要公开 config.json、Cookie 或比对记录。
```

## 后续更新

```bash
git add .
git status
git commit -m "描述本次修改"
git push
```

如果修改了版本号，再为对应提交创建新的标签和 Release。每次推送前都应检查 `git status` 和实际差异，确保个人配置与运行数据没有被加入暂存区。

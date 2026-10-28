# GitHub 源码同步规范

## 1. 目标

GitHub 只作为**源码镜像**，不承载运行环境、数据库、构建产物和发布安装包。
Gitee `origin` 保持不变；GitHub 使用独立 remote，建议命名为 `github`。

## 2. 同步边界

| 保留 | 排除 |
| --- | --- |
| `backend/app/**`、路由、测试 | `backend/venv/**`、`__pycache__/**`、`*.pyc` |
| `frontend/src/**`、`public/**`（下载包除外） | `frontend/dist*/**`、`node_modules/**` |
| `package.json`、锁文件、依赖清单 | `backend/instance/**`、`state/**` |
| `ops/**` 配置模板 | `.env*`（`.env.example` 除外）、密钥 |
| 小型文档、图片和测试夹具 | 数据库、日志、`.bak*` 部署备份 |
| 小型必需二进制（需单独评审） | APK/EXE、模型、媒体、压缩包、磁盘镜像 |

默认单文件上限为 20 MiB。GitHub 对单文件超过 100 MiB 会直接拒绝，
50 MiB 会给出大文件警告；本仓库把阈值前移到 20 MiB。

安装包、大型文档和媒体文件应放到 GitHub Releases、对象存储或内部制品库，
不要为了“方便克隆”而塞进源码历史。确需版本化的少量大文件应单独评审 Git LFS。

## 3. 安装本地保护

```powershell
cd E:\cloud-phone
pwsh -NoProfile -File scripts\install-git-hooks.ps1
```

安装后：

- `pre-commit` 检查暂存文件的路径和大小；
- `pre-push` 只对 GitHub remote 检查待推送历史中的路径和 blob 大小；
- 普通 Gitee 推送不会被 GitHub 专用历史审计阻断，但提交仍受源码卫生规则约束。

Git 钩子入口是兼容 Git 的最小 `sh` 启动器，实际检查逻辑统一由 PowerShell 7
的 `scripts/git-policy.ps1` 执行。在 Linux/macOS 上首次跟踪钩子文件后，再运行一次
安装脚本即可更新可执行位。

### GitHub Desktop 专用说明

GitHub Desktop 会把**已经跟踪**的文件继续显示为修改；`.gitignore` 不会把文件从
索引中移除。它也不能保证执行命令行钩子，因此不能用钩子代替索引清理。

在 Desktop 中提交前运行一次：

```powershell
cd E:\cloud-phone
pwsh -NoProfile -File scripts\github-desktop-prepare.ps1 -DryRun
pwsh -NoProfile -File scripts\github-desktop-prepare.ps1
```

应用后脚本只暂存明确路径，不提交：

- 把已跟踪的运行数据、构建产物和发布包从索引移除，本地文件不删除；
- 暂存本次 GitHub 规则文件；
- 保持其他未跟踪文件不变。

随后回到 GitHub Desktop，刷新仓库，检查变更列表并提交。若暂存区已有内容，
脚本会拒绝执行，避免混入用户正在准备的提交。

也可以手工审计：

```powershell
pwsh -NoProfile -File scripts\git-policy.ps1 -Mode ref -Rev HEAD
pwsh -NoProfile -File scripts\git-policy.ps1 -Mode ref -Rev github-source
```

## 4. 首次上传：干净快照

当前 `服务器122` 历史包含 `.env`、venv、数据库和约 69 MiB npm 缓存。
普通推送到 GitHub 会导致敏感文件泄露和仓库膨胀，因此禁止直接推送该分支。

先提交本次源码改动，确保**已跟踪文件工作区干净**：

```powershell
cd E:\cloud-phone
git status --short --branch
git diff --name-only
pwsh -NoProfile -File scripts\create-github-snapshot.ps1 -DryRun
pwsh -NoProfile -File scripts\create-github-snapshot.ps1
```

脚本从当前 `HEAD` 构造只含源码的树，写入独立的 `github-source` 分支：

- 不切换当前分支；
- 不删除工作区文件；
- 不修改当前暂存区；
- 后续再次运行会以旧 `github-source` 为父提交，保留干净的镜像历史。

确认快照内容：

```powershell
git log --oneline --decorate -5 github-source
git ls-tree -r --name-only github-source | Select-String 'venv|__pycache__|\.env|frontend/dist|\.apk$|\.exe$'
pwsh -NoProfile -File scripts\git-policy.ps1 -Mode ref -Rev github-source
```

## 5. 配置并推送 GitHub

先创建 GitHub 空仓库，再使用实际 URL：

```powershell
git remote add github https://github.com/<owner>/<repo>.git
git fetch github --prune
git push -u github github-source:main
```

禁止使用：

```text
git push --mirror github
git push --all github
git push --force github
```

若 GitHub 的 `main` 已存在，不要强推；先确认是否需要合并或重新建立空仓库。

## 6. 日常同步

```powershell
cd E:\cloud-phone

# Gitee 源码工作流照旧
git status --short --branch
git fetch origin --prune
git diff --stat origin/服务器122...HEAD

# 验证并生成新的 GitHub 源码快照
pwsh -NoProfile -File scripts\create-github-snapshot.ps1 -DryRun
pwsh -NoProfile -File scripts\create-github-snapshot.ps1
pwsh -NoProfile -File scripts\git-policy.ps1 -Mode ref -Rev github-source

# 只推一个分支
git push github github-source:main
```

发布安装包、前端 dist、数据库快照继续通过现有 122 部署工具处理，不进入 Git。

## 7. 安全提醒

当前历史已经跟踪了多个 `.env` 文件和备份。即使新的 `github-source` 分支不含这些
文件，也不要直接推送旧历史。若原仓库曾公开访问，应把其中的数据库口令、SSH 凭据、
API Token 等视为已经泄露并完成轮换；从 Git 历史删除不能撤回已经被访问过的数据。

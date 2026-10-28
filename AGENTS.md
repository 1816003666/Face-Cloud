# E:\cloud-phone 仓库规则

## Git 远端

- `origin` 是 Gitee：`git@gitee.com:gcy246/cloud-phone.git`
  （网页 https://gitee.com/gcy246/cloud-phone ）。日常代码更新提交后直接推到 `master`。
  认证走本机 SSH key，`ssh -T git@gitee.com` 可自检；HTTPS 已失效，不要改用 HTTPS。
- GitHub 使用独立 remote（建议命名 `github`），不得替换或误推 `origin`。
- 禁止 `git push --mirror`、`git push --all`、force push。

## GitHub 同步内容

- 只同步可重建项目的源码、测试、配置模板和必要的小型文档/资产。
- 不同步 `.env`、密钥、venv、`node_modules`、数据库、日志、备份、`state`、
  构建产物 `frontend/dist*`、APK/EXE、模型、媒体和压缩包。
- 单文件上限为 20 MiB；超过时停止并人工决定是否使用 Git LFS。

## 操作约束

- PowerShell 统一使用 `pwsh` 7.x。
- 修改前运行 `git status --short --branch`，不清理用户未提交改动。
- 只暂存明确路径；禁止 `git add -A`、`git add .`。
- GitHub 首次上传必须使用 `scripts/create-github-snapshot.ps1` 的干净快照，
  不得直接推送包含历史 `.env`/venv 的 `服务器122` 分支。
- GitHub Desktop 不能作为唯一防线：它会把已跟踪文件继续显示为改动。
  使用 Desktop 提交前先运行 `scripts/github-desktop-prepare.ps1`，再审查暂存区。
- 详细流程见 `GITHUB-SYNC.md`。

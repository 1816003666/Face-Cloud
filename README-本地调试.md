# E:\cloud-phone 本地调试环境（源自 192.168.9.122，2026-10-09 搭建）

> GitHub 源码同步、超大文件限制和保护钩子见 `GITHUB-SYNC.md`。

## 一、一句话结论

**前端 UI + 后端 API + 数据库这三层与 122 上完全一致**（同源码、同 dist、同数据快照，
15 个接口逐一比对结果相同，静态资源与真机截图字节数一致，投屏 WebSocket 本地实测出流）。
**依赖板子硬件的部分（Docker/redroid 容器、mediamtx、systemd/nginx、ws-scrcpy 独立服务、
ffmpeg 转封装）本地不等价**，需要的话要单独补，见第四节。

## 二、怎么启动 / 停止

```powershell
# 启动（后端 Flask 8003 + 同源托管 frontend\dist）
powershell -ExecutionPolicy Bypass -File E:\cloud-phone\start-backend.ps1

# 浏览器打开
#   http://127.0.0.1:8003/          （本机）
#   http://192.168.9.105:8003/      （同网段其他机器也能开）

# 依赖重装（一般不用）
powershell -ExecutionPolicy Bypass -File E:\cloud-phone\install-deps.ps1
```

启动脚本用的是 `backend\venv\Scripts\python.exe run_stable.py`，日志在
`backend\local-server.out.log` / `local-server.err.log`。

## 三、目录结构

```
E:\cloud-phone\
  backend\            122 的后端全量源码（app/ 19 个蓝图、run_stable.py、.env、requirements-local.txt）
    instance\         本地库 local_cloud_phone.db（= 板子库的一致性快照）+ 板子原始 db 副本
    resources\        板子上的 APK（ilauncher / xbrowser / CloudPhoneSettings）
    venv\             本地 Python 3.11 虚拟环境（已装好依赖）
  frontend\dist\      线上构建产物（58 个 chunk，含 servers/udid/投屏补丁）+ 历史 dist.bak*
  ws-scrcpy\          投屏网页版源码（无 node_modules）+ scrcpy-server.jar
  ops\etc\            板子的 nginx / systemd 配置（参考用，不会在 Windows 生效）
  state\              板子运行快照（端口/进程/表结构/sqlite 备份等）
  board-scripts\      板子 /home/firefly 下的运维小脚本
  start-backend.ps1 / install-deps.ps1
```

工具链在另一个目录（`E:\cloudphone-codex`）：
`tools\sync122.py`（同步/回推 122）、`tools\db122.py`（查库）、
`tools\compare_local_vs_board.py`（本地 vs 板子逐接口对比）、`tools\ws_stream_probe.py`（投屏探测）。

## 四、和 122 一模一样 / 不一样的地方

| 能力 | 本地表现 | 说明 |
| --- | --- | --- |
| 前端页面、路由、chunk、样式 | ✅ 一致 | 同一份 dist，`index-_Ywt-qHm.js` 本地与板子都是 231365 字节 |
| 后端 API（15 个只读接口逐一比对） | ✅ 一致 | health/auth/me/devices/server-ips/servers/metrics/alerts/groups/plans/users/audit/streams/rotation 全部 200 且形态与数字相同 |
| 登录 / JWT / 权限 | ✅ 一致 | 沿用板子 .env 的 SECRET_KEY / JWT_SECRET / 种子账号 |
| 数据库内容 | ✅ 一致（快照） | 264 设备 / 2 服务器 / 6 用户 / 200 条审计页；之后本地改动只影响本地副本 |
| 真机截图 `/api/devices/<id>/screenshot` | ✅ 一致 | 本机 adb 已连真机：设备 61/62 截图字节数与板子完全相同（11118 / 10310） |
| 投屏 `/ws/devices/<id>/stream` | ✅ 可用 | 本地实测 227 帧 / 2.58 MB / 14 s，H.264 Annex-B（起始码+SPS），与板子同一条代码路径 |
| 转发流页面 `/api/streams*` | ⚠️ 半可用 | 配置项一致；真实状态来自板子的 mediamtx API（只监听 127.0.0.1），本地拿不到 → 显示 waiting |
| Docker/redroid 容器管理 | ⚠️ 走 SSH | 代码是通过 SSH 到 `SSH_HOST`（本 .env 指向 192.168.9.122）执行 docker，本机没 docker 也能用；但会操作**板子的**容器 |
| ws-scrcpy（8000/8100）、mediamtx、nginx、socat | ❌ 无 | 这些是 Linux 服务/二进制，Windows 上不启动；平台自带投屏不依赖它们 |
| fMP4 转封装（`format=fmp4`） | ❌ 缺 ffmpeg | `stream.py` 找 `backend\bin\ffmpeg.exe` 或 PATH；放一个 Windows ffmpeg 进去即可 |
| systemd/开机自启 | ❌ 无 | 用 start-backend.ps1 手动启动；关窗口/重启机器就停 |

## 五、注意事项

1. **不要本地和板子同时对同一台设备做投屏/操作**：设备端的 scrcpy-server/`screenrecord`
   是独占的，两边同时开会互相踢掉（今天实测到一次板子侧流被本地占用后中断）。
2. 本机 `adb` 在 `D:\platform-tools\adb.exe`（`.env` 里 `ADB_PATH`），已经连着
   `36.156.109.209:59xx` 和 `127.0.0.1:360xx` 一批设备，所以本地也能直接操作真机。
3. 本机环境变量里有 `HTTP_PROXY=http://127.0.0.1:9`（占位代理），Python 的
   urllib / websocket-client 访问 `192.168.9.122` 会被它挡住 —— 对比脚本里已显式禁用代理，
   你自己写脚本时记得加 `NO_PROXY` 或用 `ProxyHandler({})`。
4. 本地库 `local_cloud_phone.db` 是**快照副本**：本地跑出来的审计/指标/告警只写本地，
   不会回写板子；想重置就重新复制 `state\15-cloud_phone.snapshot.db`。
5. 板子 `.env` 里 `STREAM_*` 指向公网 `112.4.135.254`（板子的端口映射），
   所以本地实例的"转发流"配置看起来和板子一致，只是拿不到实时状态。
6. 前端要改只能改 `frontend\dist\assets\*.js`（线上没有 vite 源码），改完强刷；
   要同步回板子用 `python tools\sync122.py push <本地> <远端>`（自动备份 + md5 校验）。

## 六、常用命令

```powershell
# 本地 vs 板子 逐接口对比 / 截图对比
python E:\cloudphone-codex\tools\compare_local_vs_board.py --shot 61

# 投屏探测（本地 / 板子）
E:\cloud-phone\backend\venv\Scripts\python.exe E:\cloudphone-codex\tools\ws_stream_probe.py --device 62
E:\cloud-phone\backend\venv\Scripts\python.exe E:\cloudphone-codex\tools\ws_stream_probe.py --device 62 --base http://192.168.9.122:8003

# 查本地库
python E:\cloudphone-codex\tools\db122.py --db E:\cloud-phone\backend\instance\local_cloud_phone.db tables

# 从板子刷新镜像（再按需重铺到 E:\cloud-phone）
python E:\cloudphone-codex\tools\sync122.py pull code ops state
```

## 七、今天验证过的证据（可复现）

- 15 个只读接口：本地与板子返回值/状态码/条目数全部相同（含 264 台设备、2 台服务器、6 个用户、7 个套餐）。
- 静态资源：`/assets/index-_Ywt-qHm.js` 两边都是 231365 字节，`index.html` 651 字节。
- 真机截图：设备 61 = 11118 字节、设备 62 = 10310 字节（本地 vs 板子一致）。
- 投屏：本地 `/ws/devices/61/stream` 14 秒收到 227 帧 2.58 MB，H.264 Annex-B。
- 定时任务与设备轮换在库里都是 disabled（`scheduled_tasks.enabled=0`、`rotation_config.enabled=0`），
  本地实例不会自动对真机做批量操作；但仍会写本地指标/告警数据。

## 八、本地改代码 → 上传板子（2026-10-09 已跑通并验证）

用 `E:\cloudphone-codex\tools\deploy122.py`，一条命令搞定"对比 → 备份 → 上传 → 校验 →
板上试跑 → 重启生效 → 失败自动回滚"：

```powershell
# 0) 先看板子上有没有别人比你新的改动（防止把你的旧版本盖上去）
python tools\deploy122.py pull              # 预览：板子比本地新/多的文件
python tools\deploy122.py pull --apply      # 确认后拉回本地（旧文件存 *.bak_local_*）

# 1) 本地改完，看差异
python tools\deploy122.py diff              # 后端
python tools\deploy122.py diff --frontend   # 前端 dist

# 2) 先推到板子的 staging 目录，用 systemd 瞬态服务在 18004 起一次自检
python tools\deploy122.py deploy --target staging
#    → 上传 + 远端 py_compile + 启动 + /api/health + 登录读设备列表，全程不碰线上服务

# 3) 确认没问题，再更新线上（会自动备份、重启、健康检查，失败自动回滚）
python tools\deploy122.py deploy --target live --restart

# 4) 前端改动：只推 dist，不需要重启，浏览器 Ctrl+Shift+R
python tools\deploy122.py frontend

# 5) 出问题回滚（按部署记录）
python tools\deploy122.py rollback --restart
python tools\deploy122.py status            # 看线上/暂存/差异总览
```

安全约束（工具内置）：

| 项目 | 行为 |
| --- | --- |
| 上传范围 | 排除 `.env`（本地这份是 Windows 专用，传上去会搞坏板子）、`venv/`、`instance/`、`uploads/`、`resources/`、`__pycache__`、`*.log/*.pyc/*.bak*` |
| 备份 | 每个被覆盖的文件先存成 `<文件名>.bak_deploy_<时间戳>` |
| 校验 | 上传后逐文件 md5 比对；`.py` 用板子的 venv 跑 `py_compile` |
| 生效 | 只有 `--restart` 才重启 `cloudphone.service`；否则明确提示"进程仍跑旧代码" |
| 回滚 | 重启后 60 秒内 `/api/health` 不通 → 自动还原全部文件 + 重启 + 再验一次 |
| 留痕 | 每次部署写 `E:\cloudphone-codex\mirror\122\deploys\<时间>.json`（文件/md5/备份路径/结果） |

今天实测的证据：

- 本地给 `/api/health` 加标记 → `diff` 精确识别 1 个文件 → 上传 md5 一致 → 远端 `py_compile` 通过
  → staging 在板子 18004 端口起来，返回 `{"backend":"redroid","build":"local-deploy-test-2","status":"ok"}`，
  登录后能读到 264 台设备；还原后同样的流程让标记消失。**线上服务全程没重启**（PID 未变、NRestarts=0）。
- 期间发现板子 `app/stream.py` 在 07:25 被改过（同事修了 `_normalize_sc` 边界多插 2 字节导致的花屏），
  用 `pull --apply` 拉回本地做基线，避免下次部署把别人的修复覆盖掉。

还需要注意：

1. 线上生效要重启后端，**会有 3~10 秒中断**，正在投屏的浏览器会掉线——挑没人在用的时间做，
   或先用 `python tools\deploy122.py status` 看板子负载/日志。
2. 改 `.env`、装新依赖、改数据库结构属于另一类变更：`.env` 要在板子上直接改（本地版本不通用）；
   新依赖要在板子的 venv 里 `pip install`；`models.py` 加字段靠 `schema_guard`/`create_app` 启动时
   自动 `ALTER TABLE`，但删列/改类型要手工迁移。
3. 前端没有源码，改的是 `frontend\dist\assets\*.js` 构建产物；不要 `npm build`。
4. 想做"更正式的发布体系"，还差：把 `E:\cloud-phone` 纳入 git 做基线 + 每次部署留 diff、加一组
   API 冒烟用例（可以直接复用 `tools\compare_local_vs_board.py`）、以及数据库迁移脚本。

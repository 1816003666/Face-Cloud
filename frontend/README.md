# 云手机 Web 前端源码

本目录是 122 线上 Web 端的可构建 Vite + React 源码。

## 来源

1. 以 2026-10-21 `cloud-phone-source/server/frontend` 恢复源码为基线；
2. 按 2026-10-22 122 活动构建 `index-CPAqJKBS.js` 回灌功能：
   - 新版登录页（`login-shell`）
   - 客户端下载菜单（`public/assets/client-download-menu.*` + `public/downloads/*`）
   - 路由空闲预取、悬停预取、设备/文件页保活
   - 套餐 SKU、门户订单、订单来源筛选
   - 用户在线购买（模拟支付）
   - 设备群控（按键 / 文字 / 网址 / 批量截图 / 安装 APK）
   - 设备详情实时规格（`/api/devices/:id/specs`）
   - 服务器取消全选后隐藏全部设备

旧版基线保存在同级 `frontend-src-legacy-20261015/`。

## 构建

```powershell
cd E:\cloud-phone\frontend
npm ci
npm run build
```

默认输出到 `dist/`。

验证构建（不覆盖线上 dist）：

```powershell
npm run build -- --outDir dist_verify
```

`node_modules/` 与 `dist_verify/` 已加入 `.gitignore`。

## 客户端下载二维码

下载弹窗中的手机二维码默认根据当前站点地址生成，指向
`/downloads/xinyue-cloud-phone-android.apk`。如需自定义，可在页面脚本执行前设置：

```js
window.CLIENT_DOWNLOAD_CONFIG = {
  mobileUrl: "https://download.example.com/mobile-app",
  mobileQrImage: "/assets/custom-mobile-qr.png",
  windowsUrl: "/downloads/xinyue-cloud-phone-setup.exe",
};
```

`mobileQrImage` 可填写站内路径、远程图片或 `data:` 图片；留空时自动生成二维码。

## 本地联调

```powershell
$env:PORT = "8010"
$env:FRONTEND_DIST = "E:\cloud-phone\frontend\dist_verify"
E:\cloud-phone\backend\venv\Scripts\python.exe E:\cloud-phone\backend\run_stable.py
```

浏览器打开 `http://127.0.0.1:8010/`。

## 发布前

源码构建与线上活动构建并非逐字节一致；发布前必须在 staging 对照登录、看板、设备、
计费/门户订单、文件等页面，确认无功能回退后再替换 `dist`。

import { useState, useEffect, useRef, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client.js'
import { useAuth, useCan } from '../store/auth.jsx'
import { uiAlert, uiConfirm } from '../lib/dialog.js'

/* ---------- 服务器IP列表（用于过滤与动态投屏） ---------- */
const SERVER_IPS = [
  '36.156.109.204', '36.156.109.209', '36.156.109.213',
  '36.156.109.238', '36.156.109.239',
  '36.156.109.240', '36.156.109.242', '36.156.109.243',
]
const WS_SCRCPY_PORT = 8100

/* 截图异常 / 设备离线时用的占位图（一张正常的云手机桌面），放在 public/ 下。后续接入真实修复后可移除 */
const PLACEHOLDER_SCREEN = '/device-placeholder.png'

/** 从设备对象提取所属服务器IP。
 *
 * 注意顺序：必须优先用 device.ip。经 SSH 中继接入的设备 serial 是
 * `127.0.0.1:36000`，从 serial 取会得到 127.0.0.1，那样它就会被
 * 服务器过滤区当成一个不存在的服务器而永远筛不出来。
 */
function getServerIp(device) {
  const ip = (device.ip || '').trim()
  if (ip && ip !== '127.0.0.1' && ip !== 'localhost') return ip
  const host = ((device.serial || '').split(':')[0] || '').trim()
  return host || ip
}

/* 判断 IPv4 是否为私有/回环/保留地址（不能从公网直连） */
function isPrivateIp(ip) {
  return !/^\d{1,3}(\.\d{1,3}){3}$/.test(ip) ||
         /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|0\.|169\.254\.)/.test(ip)
}

/* 服务器侧 ws-scrcpy 的设备名（udid）。
 *
 * serial 带真实 IP 且与 ws-scrcpy 所在机器不同 → 服务器 adb 里登记的是**完整 serial**
 * （如 192.168.99.119:5555，redroid11 那批容器经局域网接入 122）；
 * 否则用 127.0.0.1:<端口>（容器 adb 反向隧道到服务器本机的形态，如 36.x / 127.0.0.1:36000）。
 */
function toScrcpyUdid(device, wsHost) {
  // 后端已经算好"设备所在服务器 adb 里的名字"（fingerprint.remote_serial），
  // 优先用它：中继端口 127.0.0.1:36xxx 只有平台本机认识，投屏服务器不认。
  const canonical = String((device && device.scrcpy_udid) || '').trim()
  if (canonical) return canonical
  const serial = String((device && device.serial) || '')
  const sep = serial.lastIndexOf(':')
  if (sep < 0) return ''
  const host = serial.slice(0, sep)
  const port = serial.slice(sep + 1)
  if (!port) return ''
  const tunnelled = !host || host === '127.0.0.1' || host === 'localhost'
  if (!tunnelled && host !== wsHost) return serial
  return '127.0.0.1:' + port
}

/* 选择投屏服务(ws-scrcpy 8100)所在地址。
 * - 设备 IP 是公网 IP（如 36.156.109.x）：直接用该 IP；
 * - 设备 IP 是内网/回环，且当前页面是从公网访问（hostname 是公网/非保留地址）：
 *   改用当前公网地址，避免 Chrome 私有网络访问(PNA)拦截"公网页面连内网"；
 * - 否则（内网/本地访问）仍用设备所在服务器的内网 IP。
 */
function resolveScrcpyHost(device) {
  // 后端会给出「这台设备在哪台 ws-scrcpy 上能播」（目前统一是网关门禁 122），优先用它：
  // 公网服务器上的 ws-scrcpy 实例已陈旧，直连会黑屏。
  const fromApi = String((device && device.scrcpy_host) || '').trim()
  if (fromApi) return fromApi
  const ip = (getServerIp(device) || SERVER_IPS[0] || '').trim()
  if (ip && !isPrivateIp(ip)) return ip
  const loc = (window.location.hostname || '').trim()
  if (loc && loc !== 'localhost' && !isPrivateIp(loc)) return loc
  return ip
}

/* ---------- 云手机屏幕预览（小卡片截图 / 大屏 MSE 视频流） ---------- */
// 截图请求并发闸门：几十张卡片同时打 adb 会把后端拖死（单张 0.4~1.2s）。
// 同一时刻最多放行 SS_MAX_CONCURRENT 个，其余排队；页面切到后台时不再发新请求。
const SS_MAX_CONCURRENT = 4
let ssActive = 0
const ssQueue = []

function ssDrain() {
  while (ssActive < SS_MAX_CONCURRENT && ssQueue.length) {
    const job = ssQueue.shift()
    ssActive += 1
    job.run().then(job.resolve, job.reject).finally(() => {
      ssActive -= 1
      ssDrain()
    })
  }
}

// 排队上限：每张卡片同一时刻只会有 1 个请求（递归 setTimeout 等上一次返回才排下一次），
// 所以队列长度天然 <= 卡片数。上限设太小（原来 24）会让最后一页的卡片永远排不进去，
// 表现为新设备一直显示加载中。
const SS_MAX_QUEUE = 200

function grabScreenshot(deviceId) {
  // 后台标签页不抓图：用户看不到，纯浪费带宽和 adb
  if (typeof document !== 'undefined' && document.hidden) {
    return Promise.reject(new Error('page-hidden'))
  }
  // 队列已经很长说明 adb 跟不上，这一轮直接跳过（不影响已显示的帧）
  if (ssQueue.length >= SS_MAX_QUEUE) {
    return Promise.reject(new Error('screenshot-busy'))
  }
  return new Promise((resolve, reject) => {
    ssQueue.push({ run: () => api.deviceScreenshot(deviceId), resolve, reject })
    ssDrain()
  })
}

function PhoneScreen({ device, large, interval = 5, phase = 0, batchCount = 1 }) {
  const [now, setNow] = useState(new Date())
  const [screenshot, setScreenshot] = useState(null)
  const [ssError, setSsError] = useState(false)
  const [ssRetry, setSsRetry] = useState(0)
  const [streamStatus, setStreamStatus] = useState('connecting')
  const screenRef = useRef(null)
  const [visible, setVisible] = useState(false)
  const isRedroid = device.backend === 'redroid'
  const isError = device.status === 'error'
  const isStopped = device.status === 'stopped'

  // 可视区域懒加载：仅在卡片进入视口（±200px 预载）时才轮询截图，大幅降低 240 台并发请求压力
  useEffect(() => {
    if (!isRedroid || isError || isStopped || large) return
    const el = screenRef.current
    if (!el) { setVisible(true); return }
    if (typeof IntersectionObserver === 'undefined') { setVisible(true); return }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => setVisible(e.isIntersecting))
    }, { rootMargin: '200px' })
    io.observe(el)
    return () => io.disconnect()
  }, [isRedroid, isError, isStopped, large])

  // 时钟（simulator 用）
  useEffect(() => {
    if (isRedroid) return
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [isRedroid])

  // 小卡片：截图轮询（interval 秒/次，每批 10 台相位错峰，仅可见卡片轮询）
  useEffect(() => {
    if (!isRedroid || isError || isStopped || large || !visible) return
    let cancelled = false
    let retryCount = 0
    let timer = null
    async function fetchSS() {
      if (typeof document !== 'undefined' && document.hidden) return
      try {
        const r = await grabScreenshot(device.id)
        if (cancelled) return
        if (r.data && r.data.size > 0) {
          const url = URL.createObjectURL(r.data)
          setScreenshot((old) => { if (old) URL.revokeObjectURL(old); return url })
          setSsError(false)
          retryCount = 0
        } else {
          throw new Error('empty screenshot')
        }
      } catch (e) {
        if (cancelled) return
        retryCount++
        if (retryCount >= 3) setSsError(true)
      }
    }
    // 相位错峰：本批延迟 phase * (interval / batchCount) 秒后启动，之后每 interval 秒刷新。
    // 用递归 setTimeout 而不是 setInterval，是为了能做**失败退避**：
    // 连不上的设备（公网离线机）如果照样每 5 秒打一次，几百台一起会把后端打满。
    const iv = Math.max(2, Number(interval) || 5)
    const bc = Math.max(1, Number(batchCount) || 1)
    const delayMs = Math.round(phase * (iv * 1000) / bc)

    function schedule() {
      // 连续失败 3 次后把间隔拉长到 5 倍；一旦恢复成功，retryCount 归零，自动回到正常频率
      const factor = retryCount >= 3 ? 5 : 1
      timer = setTimeout(async () => {
        if (cancelled) return
        await fetchSS()
        if (!cancelled) schedule()
      }, iv * 1000 * factor)
    }

    const t0 = setTimeout(() => {
      if (cancelled) return
      fetchSS()
      schedule()
    }, delayMs)
    return () => {
      cancelled = true
      clearTimeout(t0)
      if (timer) clearTimeout(timer)
    }
  }, [device.id, isRedroid, isError, isStopped, large, interval, phase, batchCount, visible, ssRetry])

  // 大屏预览：ws-scrcpy iframe（内部 MSE 播放 H.264）
  // 按设备所属服务器动态生成投屏地址，不再硬编码
  const WS_SCRCPY_HOST = resolveScrcpyHost(device)

  function buildScrcpyUrl() {
    if (!device.serial) return ''
    const udid = toScrcpyUdid(device, WS_SCRCPY_HOST)
    const wsUrl = `ws://${WS_SCRCPY_HOST}:${WS_SCRCPY_PORT}/?action=proxy-adb&remote=tcp:8886&udid=${encodeURIComponent(udid)}`
    return `http://${WS_SCRCPY_HOST}:${WS_SCRCPY_PORT}/#!action=stream&udid=${encodeURIComponent(udid)}&player=mse&ws=${encodeURIComponent(wsUrl)}`
  }

  const hh = String(now.getHours()).padStart(2, '0')
  const mm = String(now.getMinutes()).padStart(2, '0')
  const apps = ['微信', '抖音', '淘宝', '支付宝', '高德', '美团', 'B站', '京东', '拼多多', '小红书', 'QQ', '设置']
  const colors = ['#07c160', '#000', '#ff5000', '#1677ff', '#00a0e9', '#ffc300', '#fb7299', '#e1251b', '#e02e24', '#ff2442', '#12b7f5', '#8a8a8a']

  // 真实设备大屏：ws-scrcpy iframe（MSE 视频流）—— 逻辑保持原样，不做异常占位处理
  if (isRedroid && !isError && !isStopped && large) {
    const scrcpyUrl = buildScrcpyUrl()
    return (
      <div className={`phone-screen real-screen ${streamStatus === 'error' ? 'screen-error-state' : ''}`} style={{ position: 'relative', background: '#000' }}>
        {scrcpyUrl ? (
          <iframe
            src={scrcpyUrl}
            style={{ width: '100%', height: '100%', border: 'none', display: 'block' }}
            onLoad={() => setStreamStatus('live')}
            allow="fullscreen"
          />
        ) : null}
        {streamStatus !== 'live' && (
          <div className="screen-loading" style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0 }}>
            <div className="loading-spinner" />
            <span>正在连接视频流...</span>
          </div>
        )}
      </div>
    )
  }

  // 真实设备小卡片：截图
  if (isRedroid && !isError && !isStopped && !large) {
    return (
      <div ref={screenRef} className={`phone-screen real-screen ${ssError ? 'screen-error-state' : ''}`}>
        {screenshot && !ssError ? (
          <img src={screenshot} alt="device screen" className="screen-img" onError={() => setSsError(true)} />
        ) : ssError ? (
          <div
            className="screen-loading"
            style={{ cursor: 'pointer' }}
            onClick={() => { setSsError(false); setSsRetry((v) => v + 1) }}
            title="点击重新获取画面"
          >
            <span style={{ fontSize: 13, fontWeight: 600 }}>无画面</span>
            <span style={{ fontSize: 11, opacity: 0.7 }}>点击重试</span>
          </div>
        ) : (
          <div className="screen-loading">
            <div className="loading-spinner" />
            <span>加载中...</span>
          </div>
        )}
      </div>
    )
  }

  // 真实设备离线（截图/投屏都取不到画面）：直接用占位图铺满整屏，
  // 右上角保留一枚「离线」小标识，避免被误判成在线设备。
  if (isRedroid && isError) {
    return (
      <div className="phone-screen screen-error">
        <img src={PLACEHOLDER_SCREEN} alt="device screen" className="screen-img screen-placeholder"
             style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: '100%' }}
             title={`设备离线（${device.serial}），显示占位图`} />
        <span className="screen-offline-tag">离线</span>
      </div>
    )
  }

  // 模拟器界面
  return (
    <div className={`phone-screen ${isError ? 'screen-error' : ''} ${isStopped ? 'screen-off' : ''}`}>
      <div className="status-bar">
        <span className="sb-time">{hh}:{mm}</span>
        <span className="sb-right">
          <span className="sb-signal">●●●●</span>
          <span className="sb-wifi">⌄⌄⌄</span>
          <span className="sb-battery">▮▮▮</span>
        </span>
      </div>
      {isError ? (
        <div className="screen-msg">
          <div className="err-icon">⚠</div>
          <div>设备离线</div>
        </div>
      ) : isStopped ? (
        <div className="screen-msg">
          <div className="off-icon">⏻</div>
          <div>已关机</div>
        </div>
      ) : (
        <>
          <div className="phone-wallpaper">
            <div className="wp-clock">{hh}:{mm}</div>
            <div className="wp-date">{now.getMonth() + 1}月{now.getDate()}日 周{'日一二三四五六'[now.getDay()]}</div>
          </div>
          <div className="app-grid">
            {apps.map((name, i) => (
              <div key={i} className="app-icon">
                <div className="app-img" style={{ background: colors[i % colors.length] }}>{name.charAt(0)}</div>
                <span>{name}</span>
              </div>
            ))}
          </div>
          <div className="phone-dock">
            <div className="app-icon"><div className="app-img" style={{ background: '#07c160' }}>电</div><span>电话</span></div>
            <div className="app-icon"><div className="app-img" style={{ background: '#1677ff' }}>信</div><span>短信</span></div>
            <div className="app-icon"><div className="app-img" style={{ background: '#5856d6' }}>浏</div><span>浏览器</span></div>
            <div className="app-icon"><div className="app-img" style={{ background: '#8a8a8a' }}>相</div><span>相机</span></div>
          </div>
          <div className="home-indicator" />
        </>
      )}
    </div>
  )
}

/* ---------- 单台设备卡片 ---------- */
const STATUS_DOT = {
  running: { color: '#22c55e', label: '运行中' },
  creating: { color: '#3b82f6', label: '创建中' },
  stopped: { color: '#6b7280', label: '已停止' },
  error: { color: '#ef4444', label: '异常' },
}

function DeviceCard({ device, onControl, onDelete, onDetail, onPreview, onPower, onRename, selectMode, selected, onToggleSelect, interval, phase, batchCount }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef(null)
  const isSelected = selectMode && selected?.has(device.id)

  // 点击外部关闭菜单
  useEffect(() => {
    function onClickOutside(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [])

  const menuItems = [
    { label: '设备详情', icon: 'ℹ', action: () => onDetail && onDetail(device) },
    { label: '重命名', icon: '✎', action: () => onRename && onRename(device) },
    { label: '开机', icon: '⏻', action: () => onPower(device.id, 'start'), hide: device.status === 'running' },
    { label: '关机', icon: '⏼', action: () => onPower(device.id, 'stop'), hide: device.status === 'stopped' || device.status === 'error' },
    { label: '重启', icon: '↻', action: () => onPower(device.id, 'restart'), hide: device.status === 'stopped' || device.status === 'error' },
    { label: '删除', icon: '🗑', action: () => onDelete(device.id), danger: true },
  ].filter((m) => !m.hide)

  return (
    <div className={`device-card ${selectMode ? 'select-mode' : ''} ${isSelected ? 'selected' : ''}`}>
      <div className="phone-frame" onClick={() => (selectMode ? onToggleSelect(device.id) : onPreview(device))}>
        <PhoneScreen device={device} interval={interval} phase={phase} batchCount={batchCount} />
        <div className="card-name">
          <span className="card-name-text">{device.name}</span>
          {device.plan_name && (
            <span className="card-plan-text" title={`套餐：${device.plan_name}`}>{device.plan_name}</span>
          )}
        </div>
        {selectMode && (
          <div className="select-check">
            <span className={`check-box ${isSelected ? 'checked' : ''}`}>{isSelected ? '✓' : ''}</span>
          </div>
        )}
      </div>
      <div className="card-menu" ref={menuRef}>
        <button
          className="menu-trigger"
          onClick={(e) => { e.stopPropagation(); setMenuOpen((v) => !v) }}
          title="设备操作"
        >⋮</button>
        {menuOpen && (
          <div className="dropdown-menu" onClick={(e) => e.stopPropagation()}>
            {menuItems.map((m) => (
              <button
                key={m.label}
                className={`dropdown-item ${m.danger ? 'danger' : ''}`}
                onClick={() => { setMenuOpen(false); m.action() }}
              >
                <span className="dropdown-icon">{m.icon}</span>
                {m.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/* ---------- 导入真实设备弹窗 ---------- */
function ImportModal({ onClose, onImported }) {
  const [devices, setDevices] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    async function scan() {
      try {
        setLoading(true)
        const r = await api.discoverDevices()
        setDevices(r.data)
      } catch (e) {
        setErr(e.response?.data?.error || '扫描失败，请确认 ADB 已连接设备')
      } finally {
        setLoading(false)
      }
    }
    scan()
  }, [])

  async function handleImport(serial) {
    try {
      await api.importDevice({ serial })
      onImported()
      setDevices((prev) => prev.map((d) => d.serial === serial ? { ...d, already_imported: true } : d))
    } catch (e) {
      await uiAlert(e.response?.data?.error || '导入失败')
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal-body" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">
          <span>导入真实云手机</span>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        {loading && <div className="loading">正在扫描 ADB 设备...</div>}
        {err && <div className="err">{err}</div>}
        {!loading && !err && (
          <>
            <div className="muted" style={{ marginBottom: 12 }}>
              发现 {devices.length} 台在线设备，点击「导入」添加到看板
            </div>
            {devices.length === 0 && <div className="empty-hint">未发现已连接的 ADB 设备</div>}
            <div className="import-list">
              {devices.map((d) => (
                <div key={d.serial} className="import-item">
                  <div>
                    <div className="import-serial">{d.serial}</div>
                    <div className="import-meta">{d.model} · Android {d.android_version}</div>
                  </div>
                  {d.already_imported ? (
                    <span className="badge st-ok">已导入</span>
                  ) : (
                    <button onClick={() => handleImport(d.serial)}>导入</button>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

/* ---------- 大屏预览弹窗：方案3 · MST (WS scrcpy) 视频投屏 ---------- */
const MST_PORT = 8100

/* 线性图标（Feather 风格，stroke 随文字颜色） */
const Ic = ({ children }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
)

function PreviewModal({ device, onClose, onDelete }) {
  const [busy, setBusy] = useState('')          // 正在发送的按钮名
  const [tip, setTip] = useState(null)          // { ok, text }
  if (!device) return null

  // iframe 嵌入 MST 的 stream 页面（player=mse），触控由 MST 原生处理
  // 按设备所属服务器动态生成投屏地址，不再硬编码
  const MST_HOST = resolveScrcpyHost(device)
  const udid = toScrcpyUdid(device, MST_HOST)
  const ws = `ws://${MST_HOST}:${MST_PORT}/?action=proxy-adb&remote=tcp:8886&udid=${encodeURIComponent(udid)}`
  // ui=0：隐藏 ws-scrcpy 自带右侧工具条，视频画面铺满手机框
  const streamUrl = `http://${MST_HOST}:${MST_PORT}/#!action=stream&udid=${encodeURIComponent(udid)}&player=mse&ws=${encodeURIComponent(ws)}&ui=0`

  function notify(ok, text) {
    setTip({ ok, text })
    window.setTimeout(() => setTip(null), 2400)
  }

  async function sendKey(keycode, label) {
    if (busy) return
    setBusy(label)
    try {
      const r = await api.controlDevice(device.id, 'key', { keycode })
      if (r?.data && r.data.ok === false) {
        throw new Error(r.data.error || r.data.result || '设备没有响应')
      }
      notify(true, `已发送：${label}`)
    } catch (e) {
      const msg = e.response?.data?.error || e.message || '操作失败'
      notify(false, `${label}失败：${msg}`)
    } finally {
      setBusy('')
    }
  }

  async function handleScreenshot() {
    if (busy) return
    setBusy('截图')
    try {
      const r = await api.deviceScreenshot(device.id)
      const url = window.URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `${device.name}_${Date.now()}.png`
      a.click()
      window.URL.revokeObjectURL(url)
      notify(true, '截图已保存')
    } catch (e) {
      notify(false, `截图失败：${e.response?.data?.error || e.message || '未知错误'}`)
    } finally {
      setBusy('')
    }
  }

  const keyBtn = (keycode, label, icon) => (
    <button
      className="st-btn"
      onClick={() => sendKey(keycode, label)}
      disabled={!!busy}
      title={label}
      style={busy === label ? { opacity: 0.5 } : undefined}
    >{icon}</button>
  )

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal-body modal-control" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">
          <span>{device.name} · 实时视频投屏</span>
        </div>
        <div className="modal-preview">
          <div className="mst-frame">
            <iframe
              src={streamUrl}
              title="云手机视频投屏"
              allow="autoplay; clipboard-read; clipboard-write; fullscreen"
            />
          </div>
          {/* 屏幕右侧竖向工具栏（放在云手机容器外侧，不遮挡画面） */}
          <div className="screen-toolbar" onClick={(e) => e.stopPropagation()}>
            {keyBtn(4, '返回', <Ic><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></Ic>)}
            {keyBtn(3, '主页', <Ic><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></Ic>)}
            {keyBtn(187, '后台', <Ic><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></Ic>)}
            {keyBtn(24, '音量+', <Ic><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="15" y1="9" x2="15" y2="15"/><line x1="12" y1="12" x2="18" y2="12"/></Ic>)}
            {keyBtn(25, '音量-', <Ic><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="12" y1="12" x2="18" y2="12"/></Ic>)}
            {keyBtn(26, '电源', <Ic><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></Ic>)}
            <button
              className="st-btn"
              onClick={handleScreenshot}
              disabled={!!busy}
              title="截图"
              style={busy === '截图' ? { opacity: 0.5 } : undefined}
            >
              <Ic><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></Ic>
            </button>
          </div>
        </div>
        {tip && (
          <div className={`preview-tip ${tip.ok ? 'ok' : 'err'}`}>{tip.text}</div>
        )}
      </div>
    </div>
  )
}

/* ---------- 服务器管理弹窗：管理员自助添加服务器，SSH 发现并接入云手机 ---------- */
function UnregisteredServersSection({ onPickHost, onChanged }) {
  const [rows, setRows] = useState([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  async function load() {
    try {
      const [sr, dv] = await Promise.all([
        api.listServers(),
        api.listDevices({ page: 1, page_size: 1000 }),
      ])
      const hosts = new Set((sr.data || []).map((s) => String(s.host || '').trim()))
      const devices = dv.data?.items || dv.data || []
      const map = new Map()
      for (const d of devices) {
        let ip = String(d.ip || '').trim()
        if (!ip || ip === '127.0.0.1' || ip === 'localhost') {
          const serial = String(d.serial || '')
          ip = serial.includes(':') ? serial.split(':')[0].trim() : ''
        }
        if (!ip || ip === '127.0.0.1' || ip === 'localhost' || hosts.has(ip)) continue
        if (!map.has(ip)) map.set(ip, [])
        map.get(ip).push(d.id)
      }
      setRows([...map.entries()]
        .map(([ip, ids]) => ({ ip, ids }))
        .sort((a, b) => a.ip.localeCompare(b.ip, undefined, { numeric: true })))
      setErr('')
    } catch (e) {
      setErr(`未登记服务器读取失败：${e.response?.data?.error || e.message || ''}`)
    }
  }

  useEffect(() => { load() }, [])

  async function cleanup(ip, ids) {
    if (!await uiConfirm(`确认清理服务器 ${ip} 下的 ${ids.length} 台云手机记录？\n\n仅从平台数据库移除这些记录，不影响服务器上的真实设备。`)) return
    setBusy(true)
    try {
      await api.batchDelete(ids)
      await load()
      onChanged?.()
    } catch (e) {
      await uiAlert(`清理失败：${e.response?.data?.error || e.message || ''}`)
    } finally {
      setBusy(false)
    }
  }

  if (err) return <div className="srv-error">{err}</div>
  if (!rows.length) return null

  return (
    <>
      <div className="push-sec-title">未登记 · {rows.length} 台（仅有设备记录，无 SSH 信息）</div>
      <div className="server-reg-list">
        {rows.map((r) => (
          <div className="server-reg-row" key={r.ip}>
            <span className="srv-dot" style={{ background: '#9ca3af', boxShadow: '0 0 0 3px rgba(156,163,175,0.18)' }} />
            <span className="srv-name">未登记</span>
            <span className="srv-host mono">{r.ip}</span>
            <span className="srv-count">{r.ids.length} 台设备</span>
            <span className="srv-check">无 SSH 信息</span>
            <button
              className="op-btn"
              style={{ width: 'auto', padding: '0 10px' }}
              title="把该地址填入下方 SSH 表单，填好凭据后可扫描导入"
              onClick={() => onPickHost?.(r.ip)}
            >登记</button>
            <button
              className="op-btn danger"
              style={{ width: 'auto', padding: '0 10px' }}
              disabled={busy}
              title="删除该地址下的云手机记录（不影响服务器上的真实设备）"
              onClick={() => cleanup(r.ip, r.ids)}
            >清理</button>
          </div>
        ))}
      </div>
    </>
  )
}

const SSH_FORM_DEFAULT = {
  name: '', host: '', port: 22, username: 'root', auth_type: 'password',
  secret: '', container_prefix: 'redroid', adb_port_start: 5555, adb_port_end: 5699, note: '',
}

function ServerManagerModal({ onClose, onImported }) {
  const [form, setForm] = useState(SSH_FORM_DEFAULT)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testMsg, setTestMsg] = useState(null)
  const [scanningId, setScanningId] = useState(null)
  const [found, setFound] = useState(null)
  const [picked, setPicked] = useState(() => new Set())
  const [saving, setSaving] = useState(false)
  const [servers, setServers] = useState([])
  const [deleting, setDeleting] = useState(null)
  const [deleteCascade, setDeleteCascade] = useState(false)
  const probeModels = useRef(true)

  async function loadServers() {
    try {
      const r = await api.listServers()
      setServers(r.data || [])
    } catch (e) {
      setServers([])
    }
  }
  useEffect(() => { loadServers() }, [])

  const setField = (k, v) => setForm((f) => ({ ...f, [k]: v }))
  const payload = () => ({
    name: form.name.trim() || form.host.trim(),
    host: form.host.trim(),
    port: Number(form.port) || 22,
    username: form.username.trim() || 'root',
    auth_type: form.auth_type,
    secret: form.secret,
    container_prefix: form.container_prefix.trim() || 'redroid',
    adb_port_start: Number(form.adb_port_start) || 5555,
    adb_port_end: Number(form.adb_port_end) || 5699,
    note: form.note.trim(),
  })

  async function handleTest() {
    if (!form.host.trim()) { setTestMsg({ level: 'error', text: '请先填写服务器地址' }); return }
    setTesting(true); setTestMsg(null)
    try {
      const { data: d } = await api.testServer(payload())
      setTestMsg({
        level: d.ok ? 'ok' : 'error',
        text: d.ok
          ? `${d.message || '连接成功'} · ${d.hostname || form.host}${d.docker_version ? ` · ${d.docker_version}` : ''}`
          : (d.message || '连接失败'),
      })
    } catch (e) {
      setTestMsg({ level: 'error', text: e.response?.data?.error || e.message || '连接失败' })
    } finally {
      setTesting(false)
    }
  }

  async function handleScan(serverId) {
    setScanningId(serverId || 'new'); setTestMsg(null)
    try {
      const r = serverId
        ? await api.rediscoverServer(serverId, { probe_models: probeModels.current })
        : await api.discoverServer({ ...payload(), probe_models: probeModels.current })
      const data = r.data || {}
      const devices = Array.isArray(data.devices) ? data.devices : []
      setFound({
        server_id: serverId || null,
        devices,
        message: data.message || '',
        hostname: data.hostname || (serverId ? '' : form.host.trim()),
        source: data.source || '',
      })
      setPicked(new Set(devices.filter((d) => !d.already_imported).map((d) => d.serial)))
      if (serverId) loadServers()
    } catch (e) {
      setFound({ server_id: serverId || null, devices: [], message: e.response?.data?.error || '扫描失败', hostname: '', source: '' })
      setPicked(new Set())
    } finally {
      setScanningId(null)
    }
  }

  function togglePick(serial, checked) {
    setPicked((prev) => {
      const next = new Set(prev)
      if (checked) next.add(serial)
      else next.delete(serial)
      return next
    })
  }
  function togglePickAll() {
    const candidates = (found?.devices || []).filter((d) => !d.already_imported).map((d) => d.serial)
    const allPicked = candidates.length > 0 && candidates.every((s) => picked.has(s))
    setPicked(allPicked ? new Set() : new Set(candidates))
  }

  async function handleImport() {
    const items = (found?.devices || [])
      .filter((d) => picked.has(d.serial) && !d.already_imported)
      .map((d) => ({
        // 经 SSH 中继接入的设备，要用本机中继地址作为 serial，否则平台连不上
        serial: d.local_serial || d.serial,
        // 库里若有同名但连不上的旧记录，带上原 serial 让后端就地更新它
        origin_serial: d.serial,
        name: d.name || (d.local_serial || d.serial),
        model: d.model || '',
        android: d.android || '',
        container: d.container || '',
        status: d.status || 'running',
      }))
    if (!items.length) return
    setSaving(true)
    try {
      if (found.server_id) {
        const r = await api.importServerDevices(found.server_id, items)
        await uiAlert(`导入完成：新增 ${r.data.ok} 台`
          + (r.data.reattached ? `，重新接入 ${r.data.reattached} 台` : '')
          + (r.data.skipped ? `，已在库跳过 ${r.data.skipped} 台` : ''))
      } else {
        const im = r.data?.imported || {}
        await uiAlert(`服务器已登记，导入完成：新增 ${im.ok ?? 0} 台`
          + (im.reattached ? `，重新接入 ${im.reattached} 台` : '')
          + (im.skipped ? `，已在库跳过 ${im.skipped} 台` : ''))
      }
      await loadServers()
      onImported && onImported()
      onClose()
    } catch (e) {
      await uiAlert(e.response?.data?.error || '导入失败')
    } finally {
      setSaving(false)
    }
  }

  async function handleDeleteServer(s) {
    try {
      await api.deleteServer(s.id, deleteCascade)
      setDeleting(null); setDeleteCascade(false)
      await loadServers()
      onImported && onImported()
    } catch (e) {
      await uiAlert(e.response?.data?.error || '删除失败')
    }
  }

  const pickedCount = (found?.devices || []).filter((d) => picked.has(d.serial) && !d.already_imported).length

  return (
    <>
    <div className="modal-mask" onClick={onClose}>
      <div className="modal-body" style={{ maxWidth: 620 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">
          <span>添加服务器 · SSH 发现云手机</span>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="server-reg-list">
          <div className="push-sec-title">已登记服务器 · {servers.length} 台</div>
          {servers.length === 0 ? (
            <div className="srv-empty">
              暂无已登记的服务器。填写下方 SSH 信息 →「扫描发现云手机」→ 导入后即会登记在此，之后可在本区重新扫描或删除。
            </div>
          ) : (
            servers.map((s) => (
              <div className="server-reg-row" key={s.id}>
                <span className={`srv-dot ${s.status === 'online' ? 'on' : 'off'}`} />
                <span className="srv-name">{s.name}</span>
                <span className="srv-host mono">{s.host}:{s.port}</span>
                <span className="srv-count">{s.device_count} 台</span>
                <span className="srv-check">{s.last_checked ? new Date(s.last_checked).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '未检测'}</span>
                <button className="op-btn" title="重新扫描" onClick={() => handleScan(s.id)} disabled={scanningId !== null}>
                  {scanningId === s.id ? '⋯' : '↻'}
                </button>
                <button className="op-btn danger" title="删除服务器" onClick={() => { setDeleteCascade(false); setDeleting(s) }}>删除</button>
              </div>
            ))
          )}
        </div>

        <UnregisteredServersSection
          onPickHost={(ip) => {
            setField('host', ip)
            requestAnimationFrame(() => {
              document.querySelector('.server-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
            })
          }}
          onChanged={() => { loadServers(); onImported?.() }}
        />

        <div className="push-sec-title">服务器 SSH 信息</div>
        <div className="server-form">
          <label className="sf-item">
            <span>服务器名称</span>
            <input placeholder="如 云手机-01（可留空，默认取地址）" value={form.name} onChange={(e) => setField('name', e.target.value)} />
          </label>
          <label className="sf-item">
            <span>主机地址 *</span>
            <input placeholder="IP 或域名，如 192.168.9.117" value={form.host} onChange={(e) => setField('host', e.target.value)} />
          </label>
          <label className="sf-item sf-narrow">
            <span>SSH 端口</span>
            <input type="number" min="1" max="65535" value={form.port} onChange={(e) => setField('port', e.target.value)} />
          </label>
          <label className="sf-item sf-narrow">
            <span>用户名</span>
            <input placeholder="root" value={form.username} onChange={(e) => setField('username', e.target.value)} />
          </label>
          <label className="sf-item sf-narrow">
            <span>认证方式</span>
            <select value={form.auth_type} onChange={(e) => setField('auth_type', e.target.value)}>
              <option value="password">密码</option>
              <option value="key">私钥文件</option>
            </select>
          </label>
          <label className="sf-item">
            <span>{form.auth_type === 'password' ? '密码' : '私钥路径（后端可读）'}</span>
            <input
              type={form.auth_type === 'password' ? 'password' : 'text'}
              placeholder={form.auth_type === 'password' ? 'SSH 登录密码' : 'C:\\Users\\xxx\\.ssh\\id_rsa'}
              value={form.secret}
              onChange={(e) => setField('secret', e.target.value)}
            />
          </label>
          <button type="button" className="sf-adv-toggle" onClick={() => setShowAdvanced((v) => !v)}>
            {showAdvanced ? '收起高级选项 ▲' : '高级选项 ▼'}
          </button>
          {showAdvanced && (
            <>
              <label className="sf-item">
                <span>容器名前缀</span>
                <input placeholder="redroid" value={form.container_prefix} onChange={(e) => setField('container_prefix', e.target.value)} />
              </label>
              <label className="sf-item sf-narrow">
                <span>ADB 端口起</span>
                <input type="number" value={form.adb_port_start} onChange={(e) => setField('adb_port_start', e.target.value)} />
              </label>
              <label className="sf-item sf-narrow">
                <span>ADB 端口止</span>
                <input type="number" value={form.adb_port_end} onChange={(e) => setField('adb_port_end', e.target.value)} />
              </label>
              <label className="sf-item">
                <span>备注</span>
                <input placeholder="选填" value={form.note} onChange={(e) => setField('note', e.target.value)} />
              </label>
              <div className="sf-hint">
                发现方式：先读 <code>docker ps</code> 解析容器 5555 端口映射；服务器上没有 Docker 时，退回扫描 ADB 监听端口段。
              </div>
            </>
          )}
        </div>

        <div className="modal-actions">
          <label className="probe-models" title="通过 docker exec 读取机型/安卓版本，台数多时会稍慢">
            <input type="checkbox" defaultChecked onChange={(e) => { probeModels.current = e.target.checked }} />
            探测机型
          </label>
          <button className="ghost" onClick={handleTest} disabled={testing}>{testing ? '测试中...' : '测试连接'}</button>
          <button onClick={() => handleScan(null)} disabled={scanningId !== null || !form.host.trim()}>
            {scanningId === 'new' ? '扫描中...' : '扫描发现云手机'}
          </button>
        </div>

        {testMsg && (
          <div
            className={testMsg.level === 'ok' ? 'ok-note' : (testMsg.level === 'warn' ? 'warn-note' : 'err')}
            style={{ marginTop: 10 }}
          >{testMsg.text}</div>
        )}

        {scanningId !== null && (
          <div className="loading">正在 SSH 连接服务器并扫描云手机，请稍候...</div>
        )}

        {found && scanningId === null && (
          <div className="found-block">
            <div className="push-sec-title">
              发现结果{found.hostname ? ` · ${found.hostname}` : ''} · {found.devices.length} 台
              {found.source ? `（${
                found.source === 'docker' ? 'Docker 容器'
                  : found.source === 'adb' ? '服务器 ADB'
                    : '端口扫描'
              }）` : ''}
            </div>
            {found.devices.length === 0 ? (
              <div className="empty-hint" style={{ padding: 24 }}>{found.message || '未发现云手机'}</div>
            ) : (
              <>
                <div className="import-list">
                  {found.devices.map((d) => (
                    <label className={`import-item ${d.already_imported ? 'sf-imported' : ''}`} key={d.serial}>
                      <input
                        type="checkbox"
                        checked={picked.has(d.serial)}
                        disabled={d.already_imported}
                        onChange={(e) => togglePick(d.serial, e.target.checked)}
                      />
                      <div style={{ flex: 1 }}>
                        <div className="import-serial">{d.serial}</div>
                        <div className="import-meta">
                          {d.container ? `${d.container} · ` : ''}
                          {d.model ? `${d.brand ? d.brand + ' ' : ''}${d.model}${d.android ? ` · Android ${d.android}` : ''} · ` : ''}
                          {d.port ? `端口 ${d.port} · ` : ''}{d.status === 'running' ? '运行中' : '已停止'}
                        </div>
                        {d.local_serial && d.local_serial !== d.serial && (
                          <div className="import-meta" style={{ marginTop: 2 }}>
                            <span className="badge">SSH 中继</span>
                            {' '}接入地址 <code>{d.local_serial}</code>
                          </div>
                        )}
                        {d.relay_error && (
                          <div className="import-meta" style={{ marginTop: 2, color: 'var(--danger)' }}>
                            中继建立失败：{d.relay_error}
                          </div>
                        )}
                      </div>
                      {d.already_imported
                        ? <span className="badge st-ok">已接入</span>
                        : d.reattach
                          ? <span className="badge st-warn">重新接入</span>
                          : <span className="badge">待接入</span>}
                    </label>
                  ))}
                </div>
                {found.message && <div className="hint" style={{ marginTop: 8 }}>{found.message}</div>}
                <div className="modal-actions">
                  <button className="ghost" onClick={togglePickAll}>全选 / 取消</button>
                  <button onClick={handleImport} disabled={saving || pickedCount === 0}>
                    {found.server_id ? '导入选中的云手机' : '保存服务器并导入'}（{pickedCount}）
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>

    {deleting && (
      <div className="modal-mask" style={{ zIndex: 1100 }} onClick={() => setDeleting(null)}>
        <div className="modal-body" style={{ maxWidth: 430 }} onClick={(e) => e.stopPropagation()}>
          <div className="modal-title">
            <span>删除服务器</span>
            <button className="modal-close" onClick={() => setDeleting(null)}>✕</button>
          </div>
          <div style={{ fontSize: 13.5, lineHeight: 1.7 }}>
            确定删除服务器「{deleting.name}」（{deleting.host}）吗？
            <div style={{ color: 'var(--text-muted)', fontSize: 12.5 }}>SSH 记录将被移除，此操作不可撤销。</div>
          </div>
          {(deleting.linked_devices || 0) > 0 ? (
            <label className="del-cascade">
              <input type="checkbox" checked={deleteCascade} onChange={(e) => setDeleteCascade(e.target.checked)} />
              <span>
                同时从设备库删除该服务器下的 <b>{deleting.linked_devices}</b> 台云手机
                <br />
                <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>不勾选则保留这些云手机，仅移除服务器记录</span>
              </span>
            </label>
          ) : (
            <div className="hint" style={{ marginTop: 10 }}>该服务器下暂无已接入的云手机。</div>
          )}
          <div className="modal-actions">
            <button className="ghost" onClick={() => { setDeleting(null); setDeleteCascade(false) }}>取消</button>
            <button
              style={{ background: 'var(--danger)', color: '#fff', borderColor: 'var(--danger)' }}
              onClick={() => handleDeleteServer(deleting)}
            >确认删除</button>
          </div>
        </div>
      </div>
    )}
    </>
  )
}

/* ---------- 主页面 ---------- */
/* ---------- 群控弹窗：批量按键 / 输入文字 / 打开网址 / 截图 / 安装 APK ---------- */
function GroupControlModal({ selectedIds, onClose }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [text, setText] = useState('')
  const [url, setUrl] = useState('')
  const apkRef = useRef(null)

  async function runAll(label, fn) {
    if (!selectedIds.length) {
      setMsg('请先选择设备')
      return
    }
    setBusy(true)
    setMsg('')
    let ok = 0
    let failed = 0
    for (const id of selectedIds) {
      try {
        await fn(id)
        ok++
      } catch {
        failed++
      }
    }
    setMsg(`${label}完成：成功 ${ok} 台${failed ? `，失败 ${failed} 台` : ''}`)
    setBusy(false)
  }

  const sendKey = (keycode, label) =>
    runAll(label, (id) => api.controlDevice(id, 'key', { keycode }))

  async function installApk(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.apk')) {
      setMsg('仅支持 .apk 文件')
      return
    }
    if (!selectedIds.length) {
      setMsg('请先选择设备')
      return
    }
    setBusy(true)
    setMsg('')
    try {
      const r = await api.installApk(selectedIds, file)
      setMsg(`APK 安装完成：成功 ${r.data.ok} 台${r.data.failed ? `，失败 ${r.data.failed} 台` : ''}`)
    } catch (e) {
      setMsg(`APK 安装失败：${e.response?.data?.error || e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function screenshotAll() {
    await runAll('批量截图', async (id) => {
      const res = await api.deviceScreenshot(id)
      const blob = res.data
      if (blob && blob.size > 0) {
        const href = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = href
        a.download = `${id}_${Date.now()}.png`
        a.click()
        URL.revokeObjectURL(href)
      }
    })
  }

  async function sendText() {
    const value = text.trim()
    if (!value) {
      setMsg('请输入要批量输入的文字')
      return
    }
    await runAll('文字输入', (id) => api.controlDevice(id, 'text', { text: value }))
    setText('')
  }

  async function openUrl() {
    const value = url.trim()
    if (!value) {
      setMsg('请输入网址')
      return
    }
    await runAll('打开网址', (id) => api.controlDevice(id, 'open_url', { url: value }))
    setUrl('')
  }

  const keys = [
    ['返回', 4], ['主页', 3], ['最近任务', 187],
    ['音量+', 24], ['音量-', 25], ['电源键', 26], ['静音', 164],
  ]
  const section = { marginBottom: 14 }
  const title = { fontWeight: 600, marginBottom: 8, fontSize: 13, color: '#374151' }
  const row = { display: 'flex', gap: 8, flexWrap: 'wrap' }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal-body" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">
          <span>群控 · 已选 {selectedIds.length} 台</span>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        {msg && <div className="page-msg" style={{ marginBottom: 10 }}>{msg}</div>}

        <div style={section}>
          <div style={title}>应用</div>
          <div style={row}>
            <button className="btn-primary" disabled={busy} onClick={() => apkRef.current?.click()}>📦 安装 APK</button>
            <button className="ghost" disabled={busy} onClick={screenshotAll}>📷 批量截图</button>
            <input ref={apkRef} type="file" style={{ display: 'none' }} onChange={installApk} />
          </div>
        </div>

        <div style={section}>
          <div style={title}>按键</div>
          <div style={row}>
            {keys.map(([label, code]) => (
              <button key={label} className="ghost" disabled={busy} onClick={() => sendKey(code, label)}>{label}</button>
            ))}
          </div>
        </div>

        <div style={section}>
          <div style={title}>批量输入文字</div>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              style={{ flex: 1 }}
              placeholder="输入要发送到云手机的文字"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && sendText()}
            />
            <button className="btn-primary" disabled={busy} onClick={sendText}>发送</button>
          </div>
        </div>

        <div style={section}>
          <div style={title}>打开网址</div>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              style={{ flex: 1 }}
              placeholder="https://www.baidu.com"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && openUrl()}
            />
            <button className="btn-primary" disabled={busy} onClick={openUrl}>打开</button>
          </div>
        </div>

        <div className="modal-actions">
          <button className="ghost" onClick={onClose} disabled={busy}>关闭</button>
        </div>
      </div>
    </div>
  )
}

export default function Devices() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin' || user?.role === 'superadmin'
  // 平台账号：无企业归属的管理员。企业管理员虽然 role=admin，但服务器管理/同步属于平台侧
  const isPlatform = isAdmin && !user?.enterprise
  const [list, setList] = useState([])
  const [quotaInfo, setQuotaInfo] = useState(null)
  const [detailDevice, setDetailDevice] = useState(null)
  const [renameDevice, setRenameDevice] = useState(null)
  const [renameValue, setRenameValue] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [detailSpec, setDetailSpec] = useState(null)
  const [detailSpecErr, setDetailSpecErr] = useState('')
  const [detailSpecTick, setDetailSpecTick] = useState(0)
  const [name, setName] = useState('')
  const [count, setCount] = useState(5)
  const [backend, setBackend] = useState('redroid')
  const [showServerModal, setShowServerModal] = useState(false)
  const [serverCount, setServerCount] = useState(0)
  const [err, setErr] = useState('')
  const [view, setView] = useState('grid')
  const [gridCols, setGridCols] = useState(6)  // 卡片网格列数：2/4/6/8/10
  const [preview, setPreview] = useState(null)
  const [showImport, setShowImport] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const syncOnce = useRef(false)
  const timerRef = useRef(null)
  // —— 批量选择模式 ——
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState(new Set())
  const [scriptModal, setScriptModal] = useState(false)
  const [scripts, setScripts] = useState([])
  const [groupModal, setGroupModal] = useState(false)
  const [groups, setGroups] = useState([])
  const [groupTarget, setGroupTarget] = useState(null)
  const [groupControlOpen, setGroupControlOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const apkInputRef = useRef(null)
  // —— 轮次运行模式 ——
  const [rotation, setRotation] = useState({ enabled: false, rounds: 4, devices_per_round: 2, round_index: 1, per_round_hours: 6, next_round_at: null })
  const [rotBusy, setRotBusy] = useState(false)
  // —— 截图调度 ——
  const [screenshotInterval, setScreenshotInterval] = useState(5)
  const BATCH = 10
  // —— 分页：每页 60 台 ——
  const PAGE_SIZE = 60
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState('')   // ''=全部 / running / stopped / creating / error

  // 设备详情：打开时实时读取设备规格（adb），失败可重试
  useEffect(() => {
    if (!detailDevice) {
      setDetailSpec(null)
      setDetailSpecErr('')
      return undefined
    }
    let alive = true
    setDetailSpec(null)
    setDetailSpecErr('')
    api.deviceSpecs(detailDevice.id)
      .then((r) => alive && setDetailSpec(r.data || {}))
      .catch((e) => {
        if (!alive) return
        setDetailSpec({})
        setDetailSpecErr(e.response?.data?.error || '实时信息读取失败（设备可能离线）')
      })
    return () => { alive = false }
  }, [detailDevice, detailSpecTick])

  // —— 服务器过滤 ——
  // 服务器列表是动态的：从设备库取实际出现过的 IP，再并上已登记的服务器。
  // 不能写死常量，否则新加的服务器（117/122）永远不会出现在过滤区。
  const [serverIps, setServerIps] = useState([])
  const [registeredHosts, setRegisteredHosts] = useState([])
  // pickedServers = null 表示"未手动选择过 → 视为全选"。
  // 用 null 而不是空 Set，是为了避开开发模式下 effect 被双调用导致的时序问题：
  // 之前用 ref 记"已知服务器"的写法，第二次重放时 ref 已被第一次写脏，
  // 结果把勾选全清空了（界面上表现为服务器一个都没勾上）。
  const [pickedServers, setPickedServers] = useState(null)
  const firstServerEffect = useRef(true)
  const knownIpsRef = useRef(new Set())

  // 拉取设备库中出现过的服务器 IP（不受当前筛选影响）
  async function loadServerIps() {
    try {
      const r = await api.listServerIps()
      setServerIps(r.data || [])
    } catch (e) { /* 忽略：拉不到就退回已登记服务器 */ }
  }

  // 合并「设备库 IP」+「已登记服务器」，按 IP 段排序
  const allServerIps = useMemo(() => {
    const set = new Set()
    for (const ip of serverIps) if (ip) set.add(ip)
    for (const h of registeredHosts) if (h) set.add(h)
    return [...set].sort((a, b) => {
      const pa = a.split('.').map((x) => Number(x) || 0)
      const pb = b.split('.').map((x) => Number(x) || 0)
      for (let i = 0; i < 4; i++) {
        if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0)
      }
      return a.localeCompare(b)
    })
  }, [serverIps, registeredHosts])

  const allServerIpsKey = allServerIps.join(',')

  // 实际生效的勾选集合：未手动选过就是全选；手动选过则过滤掉已消失的服务器。
  // 用 useMemo 保证引用稳定——下面有个 [selectedServers] 的 effect 会重新拉数据，
  // 引用每次都变会导致它无限触发。
  const selectedServers = useMemo(() => {
    if (pickedServers === null) return new Set(allServerIps)
    const next = new Set()
    for (const ip of allServerIps) if (pickedServers.has(ip)) next.add(ip)
    return next
  }, [pickedServers, allServerIps])
  const serversRef = useRef(selectedServers)
  serversRef.current = selectedServers

  // 手动选择过的情况下，新出现的服务器默认也勾上（幂等，effect 重放无副作用）
  useEffect(() => {
    if (pickedServers === null) { knownIpsRef.current = new Set(allServerIps); return }
    setPickedServers((prev) => {
      if (prev === null) return prev
      const next = new Set(prev)
      let changed = false
      for (const ip of allServerIps) {
        if (!knownIpsRef.current.has(ip) && !next.has(ip)) { next.add(ip); changed = true }
      }
      return changed ? next : prev
    })
    knownIpsRef.current = new Set(allServerIps)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allServerIpsKey])

  // 从看板跳转时读取 ?server=IP，预选该服务器
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const serverIp = params.get('server')
    if (serverIp && allServerIps.includes(serverIp)) {
      setPickedServers(new Set([serverIp]))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allServerIpsKey])

  // 前端即时过滤（双保险：后端也会按 servers 参数过滤）。
  // 只有服务器列表还没加载出来（allServerIps 为空）时才不过滤；
  // 用户主动取消全选时必须显示 0 台，不能回落到全部设备。
  const serverList = allServerIps.length === 0
    ? list
    : list.filter((d) => selectedServers.has(getServerIp(d)))

  // 点状态按钮只看该状态的设备；再点一次回到全部
  const visibleList = statusFilter
    ? serverList.filter((d) => d.status === statusFilter)
    : serverList

  function toggleServer(ip) {
    setPickedServers((prev) => {
      const next = prev === null ? new Set(allServerIps) : new Set(prev)
      if (next.has(ip)) next.delete(ip)
      else next.add(ip)
      return next
    })
  }
  function toggleAllServers() {
    const allPicked = pickedServers === null || pickedServers.size === allServerIps.length
    setPickedServers(allPicked ? new Set() : new Set(allServerIps))
  }
  // 服务器勾选变化时，从后端重新获取过滤数据（并回到第 1 页）
  useEffect(() => {
    if (firstServerEffect.current) { firstServerEffect.current = false; return }
    setPage(1)
    load()
  }, [selectedServers])

  async function loadRotation() {
    try {
      const r = await api.getRotation()
      setRotation(r.data)
    } catch (e) {
      console.warn('获取轮次配置失败', e.response?.data?.error || e.message)
    }
  }

  async function handleRotationToggle(e) {
    const enabled = e.target.checked
    const n = Math.max(1, Math.min(200, Number(count) || 2))
    // 轮数取下拉框当前选中的值（后端限制 1..24），每轮时长 = 24h / 轮数。
    // 之前弹窗只报「每轮 N 台」，选 4 轮时用户只看到一个批量数量，容易误读成轮数。
    const rounds = Math.max(1, Math.min(24, Number(rotation.rounds) || 4))
    const perHours = Math.round((24 / rounds) * 100) / 100
    if (enabled) {
      const ok = await uiConfirm(
        `开启轮次运行后，将按每天 ${rounds} 轮（每轮 ${perHours} 小时）运行；立即删除现有云手机并按「每轮 ${n} 台」重建，之后每轮结束自动删除并重建 ${n} 台。确定开启？`
      )
      if (!ok) { e.target.checked = false; return }
    }
    setRotBusy(true)
    try {
      const r = await api.updateRotation({ enabled, rounds, devices_per_round: n })
      setRotation(r.data)
      if (enabled) await uiAlert('轮次运行已开启，正在按配置重置云手机，请稍候刷新设备列表…')
    } catch (err) {
      await uiAlert('设置失败：' + (err.response?.data?.error || err.message))
      loadRotation()
    } finally {
      setRotBusy(false)
    }
  }

  async function handleRotationRounds(e) {
    const rounds = Number(e.target.value)
    setRotBusy(true)
    try {
      const r = await api.updateRotation({ rounds })
      setRotation(r.data)
    } catch (err) {
      await uiAlert('设置轮数失败：' + (err.response?.data?.error || err.message))
      loadRotation()
    } finally {
      setRotBusy(false)
    }
  }

  async function load(servers) {
    const s = servers || serversRef.current
    const params = s && s.size > 0 ? { servers: [...s].join(',') } : {}
    const r = await api.listDevices(params)
    setList(r.data)
    if (user?.role !== 'admin' && user?.role !== 'superadmin') {
      api.mySubscription()
        .then((res) => setQuotaInfo(res.data || null))
        .catch(() => {})
    }
  }
  async function loadGroups() {
    try {
      const r = await api.listGroups()
      setGroups(r.data || [])
    } catch (e) { /* 忽略 */ }
  }
  const can = useCan()
  const canSync = isPlatform && can('device.import')
  const canCreate = can('device.create')
  const canReadSettings = can('settings.manage')

  async function loadSettings() {
    // 没有 settings.manage 就不请求系统设置接口（测试报告 ENT-007：无谓 403）
    if (!can('settings.manage')) return
    try {
      const r = await api.getSettings()
      const v = Number(r.data?.screenshot_interval)
      if (v >= 2 && v <= 3600) setScreenshotInterval(v)
    } catch (e) { /* 忽略，用默认 5 秒 */ }
  }

  async function handleSync() {
    setSyncing(true)
    try {
      const r = await api.syncDevices()
      await load()
    } catch (e) {
      // 权限不足/后端报错时给出可见提示（测试报告 ENT-004：403 只有一个控制台警告）
      const msg = e.response?.data?.error || e.message || '同步失败'
      setErr(`同步失败：${msg}`)
    } finally {
      setSyncing(false)
    }
  }

  useEffect(() => {
    load()
    loadGroups()
    loadRotation()
    loadSettings()
    loadServerIps()
    // 页面加载时自动同步：只有具备 device.import 的账号才做；
    // 普通用户/企业用户没有该权限，自动同步会弹"权限不足"（用户并不需要）
    if (!syncOnce.current && can('device.import')) {
      syncOnce.current = true
      setTimeout(handleSync, 2000)
    }
    // 列表轮询：264 台设备一次响应约 127KB，5 秒一次太浪费。
    // 改成 15 秒，并且页面在后台时不拉（切回来时立即补一次）。
    timerRef.current = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      load()
    }, 15000)
    function onVisible() {
      if (!document.hidden) load()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(timerRef.current)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  // 平台账号：拉取已登记服务器（数量用于按钮角标，host 用于服务器过滤区）
  useEffect(() => {
    if (!isPlatform) return
    api.listServers()
      .then((r) => {
        const rows = r.data || []
        setServerCount(rows.length)
        setRegisteredHosts(rows.map((s) => s.host).filter(Boolean))
      })
      .catch(() => {})
  }, [isPlatform])

  async function handleCreate(e) {
    e.preventDefault()
    setErr('')
    if (!name.trim()) { setErr('请输入设备名'); return }
    try {
      setErr(backend === 'redroid' ? '正在服务器上创建云手机容器，请稍候...' : '')
      await api.createDevice({ name: name.trim(), backend })
      setName('')
      setErr('')
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '创建失败')
    }
  }

  async function handleBatch(e) {
    e.preventDefault()
    const n = Math.max(1, Math.min(200, Number(count) || 1))
    // 轮次运行已开启：按 count 台启动/重置本轮
    if (rotation.enabled) {
      const tip = isAdmin
        ? `轮次运行已开启，将按每轮 ${n} 台删除并重建云手机，确定启动本轮？`
        : `轮次运行已开启，将清理名下云手机的数据、收回后按每轮 ${n} 台重新分配，确定启动本轮？`
      if (!await uiConfirm(tip)) return
      setRotBusy(true)
      try {
        const r = await api.updateRotation({ enabled: true, devices_per_round: n })
        setRotation(r.data)
        await uiAlert(`已按 ${n} 台启动本轮，正在重置，请稍候刷新设备列表…`)
      } catch (e2) {
        await uiAlert('启动本轮失败：' + (e2.response?.data?.error || e2.message))
        loadRotation()
      } finally {
        setRotBusy(false)
      }
      return
    }
    // 未开启：正常批量创建
    setErr('')
    try {
      setErr(backend === 'redroid' ? `正在批量创建 ${n} 台云手机容器，请稍候...` : '')
      await api.batchCreate({ count: n, prefix: 'redroid', backend })
      setErr('')
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '批量创建失败')
    }
  }

  async function handleDelete(id) {
    const tip = isAdmin
      ? '确认删除该设备？会同时删除服务器上的容器。'
      : '确认删除这台云手机？设备上的应用数据会被清理，云手机归还公共池。'
    if (!await uiConfirm(tip)) return
    try {
      const r = await api.deleteDevice(id)
      if (!isAdmin && r?.data?.message) await uiAlert(r.data.message)
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '删除失败')
    }
  }

  function openRename(device) {
    setRenameDevice(device)
    setRenameValue(device.name || '')
    setErr('')
  }

  async function submitRename(e) {
    if (e) e.preventDefault()
    const next = renameValue.trim()
    if (!renameDevice || renaming) return
    if (!next) { setErr('设备名不能为空'); return }
    setRenaming(true)
    try {
      await api.renameDevice(renameDevice.id, next)
      setRenameDevice(null)
      setErr('')
      load()
    } catch (e2) {
      setErr(e2.response?.data?.error || '重命名失败')
    } finally {
      setRenaming(false)
    }
  }

  async function handleControl(id, action) {
    await api.controlDevice(id, action, {})
    await uiAlert(`${action} 指令已下发`)
    load()
  }

  async function handlePower(id, action) {
    const names = { start: '开机', stop: '关机', restart: '重启' }
    if (!await uiConfirm(`确认对该设备执行「${names[action]}」操作？`)) return
    try {
      await api.powerDevice(id, action)
      await uiAlert(`${names[action]}指令已下发`)
      load()
    } catch (e) {
      await uiAlert(`${names[action]}失败：${e.response?.data?.error || e.message}`)
    }
  }

  // —— 批量选择 / 操作 ——
  const selectedIds = [...selected]

  function enterSelectMode() {
    setSelectMode(true)
    setSelected(new Set())
  }
  function exitSelectMode() {
    setSelectMode(false)
    setSelected(new Set())
  }
  function toggleSelect(id) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  function selectAll() {
    if (selectedIds.length === visibleList.length) setSelected(new Set())
    else setSelected(new Set(visibleList.map((d) => d.id)))
  }

  async function handleBatchDelete() {
    if (!selectedIds.length) return
    const tip = isAdmin
      ? `确认删除选中的 ${selectedIds.length} 台云手机？删除后会被彻底移除。`
      : `确认删除选中的 ${selectedIds.length} 台云手机？删除后云手机归还公共池、设备内数据会被清除。`
    if (!await uiConfirm(tip)) return
    setBusy(true)
    try {
      const r = await api.batchDelete(selectedIds)
      exitSelectMode()
      await load()
      await uiAlert(`删除完成：成功 ${r.data.ok} 台${r.data.failed ? `，失败 ${r.data.failed} 台` : ''}`)
    } catch (e) {
      setErr(e.response?.data?.error || '批量删除失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleBatchRestart() {
    if (!selectedIds.length) return
    if (!await uiConfirm(`确认重启选中的 ${selectedIds.length} 台云手机？`)) return
    setBusy(true)
    try {
      const r = await api.batchPower(selectedIds, 'restart')
      await load()
      await uiAlert(`重启完成：成功 ${r.data.ok} 台${r.data.failed ? `，失败 ${r.data.failed} 台` : ''}`)
    } catch (e) {
      setErr(e.response?.data?.error || '批量重启失败')
    } finally {
      setBusy(false)
    }
  }

  async function openScriptModal() {
    try {
      const r = await api.listScripts()
      setScripts(Array.isArray(r.data) ? r.data : r.data.items || [])
      setScriptModal(true)
    } catch (e) {
      setErr(e.response?.data?.error || '加载脚本列表失败')
    }
  }
  async function runScript(sid) {
    setBusy(true)
    try {
      const r = await api.executeScript(sid, selectedIds)
      setScriptModal(false)
      await load()
      await uiAlert(`脚本执行完成：成功 ${r.data.ok} 台${r.data.failed ? `，失败 ${r.data.failed} 台` : ''}`)
    } catch (e) {
      setErr(e.response?.data?.error || '脚本执行失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleApkFile(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.apk')) {
      setErr('仅支持 .apk 文件')
      return
    }
    if (!selectedIds.length) {
      setErr('请先选择设备')
      return
    }
    setBusy(true)
    try {
      const r = await api.installApk(selectedIds, file)
      await uiAlert(`APK 安装完成：成功 ${r.data.ok} 台${r.data.failed ? `，失败 ${r.data.failed} 台` : ''}`)
    } catch (e) {
      setErr(e.response?.data?.error || 'APK 安装失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleSetGroup() {
    if (!selectedIds.length || groupTarget === undefined) return
    setBusy(true)
    try {
      const r = await api.batchSetGroup(selectedIds, groupTarget)
      setGroupModal(false)
      await load()
      await uiAlert(`已将 ${r.data.ok} 台设备${groupTarget == null ? '移出分组' : `加入分组「${groups.find((g) => g.id === groupTarget)?.name || ''}」`}`)
    } catch (e) {
      setErr(e.response?.data?.error || '设置分组失败')
    } finally {
      setBusy(false)
    }
  }

  // 状态统计
  const stats = [
    { key: '', label: '全部', color: '#8b9894' },
    { key: 'running', label: '运行中', color: '#1f7a4d' },
    { key: 'stopped', label: '已停止', color: '#8b9894' },
    { key: 'creating', label: '创建中', color: '#a9741f' },
    { key: 'error', label: '异常', color: '#b4453c' },
  ].map((s) => ({
    ...s,
    count: s.key ? serverList.filter((d) => d.status === s.key).length : serverList.length,
  }))
  const totalCount = visibleList.length
  // 服务器筛选是否收窄了范围：用于在标题旁给出明确提示（否则用户会以为设备"丢了"）
  const serverFiltered = allServerIps.length > 0 && selectedServers.size < allServerIps.length
  // —— 分页计算：每页 60 台，页码越界时自动回退到有效页 ——
  const maxPage = Math.max(1, Math.ceil(totalCount / PAGE_SIZE))
  const safePage = Math.min(page, maxPage)
  const pagedList = visibleList.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE)

  // 套餐额度：普通用户创建数量不能超过套餐台数
  const quotaCap = Number(quotaInfo?.device_quota || 0)
  const quotaUsed = Number(quotaInfo?.device_used ?? totalCount)
  const atQuota = !isAdmin && quotaCap > 0 && quotaUsed >= quotaCap
  // 创建入口的三重校验：功能权限、是否有套餐、是否已达上限（测试报告 ENT-005）
  const noPlan = !isAdmin && quotaCap <= 0
  const cannotCreate = !canCreate || noPlan || atQuota
  const createHint = !canCreate
    ? '当前角色没有「创建设备」权限，请联系管理员授权'
    : (noPlan ? '当前没有有效套餐，购买后再创建云手机' : '')

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>设备管理</h2>
          <span className="page-sub">
            {totalCount} 台云手机
            {serverFiltered && (
              <>
                <span style={{ color: '#d97706', marginLeft: 8 }}>
                  · 已按服务器筛选：{selectedServers.size}/{allServerIps.length} 个服务器，共 {totalCount} 台
                </span>
                <button className="ghost" style={{ marginLeft: 8, padding: '2px 8px', fontSize: 12 }}
                  onClick={() => setPickedServers(null)}>
                  显示全部设备
                </button>
              </>
            )}
          </span>
        </div>
        <div className="page-actions">
          <button className="ghost" onClick={() => api.downloadCsv("devices")}>导出 CSV</button>
          {canSync && (
              <button onClick={handleSync} disabled={syncing} className={`sync-btn ${syncing ? 'loading' : ''}`}>
              <span className="sync-icon">{syncing ? '⋯' : '↻'}</span>
              {syncing ? '同步中' : '同步'}
              </button>
          )}
          <button
            className={`select-btn ${selectMode ? 'active' : ''}`}
            onClick={() => (selectMode ? exitSelectMode() : enterSelectMode())}
          >
            {selectMode ? '退出选择' : '选择'}
          </button>
          <div className="view-toggle">
            <button className={view === 'grid' ? 'active' : ''} onClick={() => setView('grid')}>卡片</button>
            <button className={view === 'table' ? 'active' : ''} onClick={() => setView('table')}>列表</button>
          </div>
        </div>
      </div>

      {selectMode && (
        <div className="batch-bar">
          <span className="batch-count">已选 <b>{selectedIds.length}</b> 台</span>
          <button className="batch-item ghost" onClick={selectAll}>
            {selectedIds.length === visibleList.length ? '取消全选' : '全选'}
          </button>
          <div className="batch-spacer" />
          <button className="batch-item" onClick={handleBatchRestart} disabled={busy || !selectedIds.length}>重启</button>
          <button className="batch-item" onClick={openScriptModal} disabled={busy || !selectedIds.length}>执行脚本</button>
          <button className="batch-item" onClick={() => setGroupControlOpen(true)} disabled={busy || !selectedIds.length}>群控</button>
          <button className="batch-item" onClick={() => { setGroupTarget(null); setGroupModal(true) }} disabled={busy || !selectedIds.length}>加入分组</button>
          <button className="batch-item danger" onClick={handleBatchDelete} disabled={busy || !selectedIds.length}>删除</button>
          <button className="batch-item ghost" onClick={exitSelectMode}>取消</button>
        </div>
      )}

      {/* 服务器勾选过滤区：含服务器 IP 明细，仅平台账号可见 */}
      {isPlatform && (
      <div style={{
        background: 'rgba(255,255,255,0.04)',
        border: '1px solid var(--border)',
        borderRadius: 10,
        padding: '10px 14px',
        marginBottom: 12,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
          <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text)' }}>服务器</span>
          <button onClick={toggleAllServers} style={{
            padding: '3px 10px', fontSize: 12, borderRadius: 6,
            border: '1px solid var(--border)', background: 'var(--bg-card)',
            color: 'var(--text)', cursor: 'pointer',
          }}>
            {selectedServers.size === allServerIps.length ? '取消全选' : '全选'}
          </button>
          <span style={{ fontSize: 12, color: 'var(--text-muted)', opacity: 0.7 }}>
            {selectedServers.size}/{allServerIps.length}
          </span>
          <div style={{ flex: 1 }} />
          {isAdmin && (
            <>
              <button
                className="server-add-btn"
                onClick={() => setShowServerModal(true)}
                title="登记一台服务器，通过 SSH 自动发现其中的云手机"
              >
                ＋ 添加服务器{serverCount > 0 ? ` · ${serverCount}` : ''}
              </button>
              {serverCount > 0 && (
                <button
                  className="server-mgr-btn"
                  onClick={() => setShowServerModal(true)}
                  title="重新扫描 / 删除已登记的服务器"
                >
                  ⚙ 管理服务器
                </button>
              )}
            </>
          )}
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 10px' }}>
          {allServerIps.map((ip) => (
            <label key={ip} style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              padding: '3px 8px', borderRadius: 6, fontSize: 12,
              background: selectedServers.has(ip) ? 'rgba(59,130,246,0.15)' : 'transparent',
              border: `1px solid ${selectedServers.has(ip) ? 'rgba(59,130,246,0.5)' : 'var(--border)'}`,
              color: selectedServers.has(ip) ? '#60a5fa' : 'var(--text)',
              cursor: 'pointer', userSelect: 'none',
            }}>
              <input type="checkbox" checked={selectedServers.has(ip)} onChange={() => toggleServer(ip)}
                style={{ accentColor: '#3b82f6', width: 13, height: 13, margin: 0 }} />
              <span style={{ fontFamily: 'monospace' }}>{ip}</span>
            </label>
          ))}
        </div>
      </div>
      )}

      <div className="status-summary">
        {stats.map((s) => {
          const active = statusFilter === s.key
          const disabled = s.key !== '' && s.count === 0
          return (
            <button
              key={s.key || 'all'}
              type="button"
              className={`status-chip${active ? ' active' : ''}`}
              aria-pressed={active}
              disabled={disabled && !active}
              title={s.key === '' ? '显示全部设备' : `只看${s.label}的设备`}
              onClick={() => {
                setStatusFilter(active ? '' : s.key)
                setPage(1)
              }}
            >
              <span className="status-dot" style={{ background: s.color }} />
              {s.label} <b>{s.count}</b>
            </button>
          )
        })}
      </div>

      {err && <div className="err">{err}</div>}

      <div className="toolbar">
        <div className="toolbar-group">
          <form onSubmit={handleCreate} className="inline">
            <input placeholder="设备名" value={name} onChange={(e) => setName(e.target.value)} />
            {isAdmin && (
              <select value={backend} onChange={(e) => setBackend(e.target.value)} style={{ padding: '6px 8px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-card)', color: 'var(--text)' }}>
                <option value="redroid">真实云手机</option>
                <option value="simulator">模拟设备</option>
              </select>
            )}
            <button type="submit" disabled={cannotCreate}
              title={createHint || (atQuota ? `已达套餐上限（${quotaUsed}/${quotaCap} 台），续费或删除云手机后可继续创建` : '')}>
              创建 1 台
            </button>
            {!isAdmin && quotaCap > 0 && (
              <span className="hint" style={{ marginLeft: 8, color: atQuota ? '#dc2626' : 'inherit' }}>
                已用 {quotaUsed} / 上限 {quotaCap}
              </span>
            )}
          </form>
        </div>
        <div className="layout-switch" title="卡片布局（每行列数）">
          {[4, 6, 8, 10].map((n) => (
            <button key={n} className={gridCols === n ? 'active' : ''} onClick={() => setGridCols(n)}>{n}X{n}</button>
          ))}
        </div>
        <div className="rotation-row">
          <form onSubmit={handleBatch} className="rotation-control"
            title={rotation.enabled
              ? (isAdmin ? '轮次运行已开启：按数量删除并重建，启动本轮'
                         : '轮次运行已开启：按数量清理并重新分配，启动本轮')
              : '批量创建'}>
            <input type="number" min="1" max="200" value={count}
              className="rotation-count"
              onChange={(e) => setCount(e.target.value)} disabled={rotBusy} />
            <button type="submit" className="rotation-submit" disabled={rotBusy || cannotCreate}
              title={createHint || (atQuota ? `已达套餐上限（${quotaUsed}/${quotaCap} 台）` : '')}>
              {rotation.enabled ? '启动本轮' : '批量创建'}
            </button>
          </form>
          <div className={`rotation-control ${rotation.enabled ? 'on' : ''}`}
            title={isAdmin
              ? '轮次运行：一天分 N 轮，每轮结束自动删除并按数量重建云手机（每轮台数用左侧输入框设置）'
              : '轮次运行：一天分 N 轮，每轮结束时清理你名下云手机的数据并重新分配（每轮台数用左侧输入框设置）'}>
            <label className="rotation-switch">
              <input type="checkbox" checked={rotation.enabled} onChange={handleRotationToggle} disabled={rotBusy} />
              <span className="rotation-slider" />
            </label>
            <span className="rotation-label">轮次运行</span>
            <select
              className="rotation-rounds"
              value={rotation.rounds}
              onChange={handleRotationRounds}
              disabled={rotBusy}
            >
              {[2, 3, 4, 6, 8, 12, 24].map((n) => (
                <option key={n} value={n}>{n}轮</option>
              ))}
            </select>
            {rotation.enabled && (
              <span className="rotation-info">
                第{rotation.round_index}轮 · 每{rotation.per_round_hours}h轮转 · 每轮{rotation.devices_per_round}台
                {rotation.next_round_at ? ` · 下次 ${new Date(rotation.next_round_at).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : ''}
              </span>
            )}
          </div>
        </div>
      </div>

      {view === 'grid' ? (
        <div className={`device-grid cols-${gridCols}`}>
          {visibleList.length === 0 && (
            <div className="empty-hint">
              {allServerIps.length > 0 && selectedServers.size === 0
                ? '未选择服务器：已隐藏全部云手机'
                : (isAdmin ? '暂无设备，点击上方按钮创建' : '暂无设备，购买套餐后点击上方「创建 1 台」即可分配')}
            </div>
          )}
          {pagedList.map((d, i) => (
            <DeviceCard key={d.id} device={d}
              onControl={handleControl} onDelete={handleDelete}
              onPreview={setPreview} onPower={handlePower} onDetail={setDetailDevice}
              onRename={openRename}
              selectMode={selectMode} selected={selected} onToggleSelect={toggleSelect}
              interval={screenshotInterval}
              phase={Math.floor(((safePage - 1) * PAGE_SIZE + i) / BATCH)}
              batchCount={Math.max(1, Math.ceil(list.length / BATCH))} />
          ))}
        </div>
      ) : (
        <div className="device-table-wrap">
          <table className="device-table">
            <thead>
              <tr>
                <th className="col-id">ID</th>
                <th>设备</th>
                <th>状态</th>
                <th>机型</th>
                <th>套餐</th>
                {isAdmin && <th>出口 IP</th>}
                {isAdmin && <th>Serial</th>}
                <th className="col-ops">操作</th>
              </tr>
            </thead>
            <tbody>
              {visibleList.length === 0 ? (
                <tr><td colSpan={isAdmin ? 8 : 6} className="table-empty">
                  {allServerIps.length > 0 && selectedServers.size === 0
                    ? '未选择服务器：已隐藏全部云手机'
                    : (isAdmin ? '暂无设备，点击上方按钮创建' : '暂无设备，购买套餐后点击上方「创建 1 台」即可分配')}
                </td></tr>
              ) : (
                pagedList.map((d) => {
                  const st = STATUS_DOT[d.status] || { color: '#6b7280', label: d.status }
                  return (
                    <tr key={d.id} className="device-row" onClick={() => setPreview(d)}>
                      <td className="col-id mono">{d.id}</td>
                      <td>
                        <div className="cell-name">
                          <span className="name-text">{d.name}</span>
                        </div>
                      </td>
                      <td>
                        <span className={`status-dot status-dot-lg ${d.status}`} style={{ background: st.color }} title={st.label} />
                      </td>
                      <td>
                        <span className="model-cell">{d.model || '—'}</span>
                      </td>
                      <td style={{ fontSize: 12 }}>{d.plan_name || '—'}</td>
                      {isAdmin && <td className="mono">{d.ip || '—'}</td>}
                      {isAdmin && <td className="mono serial-cell" title={d.serial}>{d.serial || '—'}</td>}
                      <td className="col-ops" onClick={(e) => e.stopPropagation()}>
                        <div className="row-ops">
                          <button className="op-btn" title="打开预览" onClick={() => setPreview(d)}>👁</button>
                          <button className="op-btn" title="重命名" onClick={() => openRename(d)}>✎</button>
                          <button className="op-btn" title="重启" onClick={() => handlePower(d.id, 'restart')}>↻</button>
                          <button className="op-btn danger" title="删除" onClick={() => handleDelete(d.id)}>🗑</button>
                        </div>
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 分页控件（超过 60 台时显示） */}
      {maxPage > 1 && (
        <div className="pagination">
          <button className="pager-btn" disabled={safePage <= 1} onClick={() => setPage(safePage - 1)}>‹ 上一页</button>
          <div className="pager-pages">
            {Array.from({ length: maxPage }, (_, i) => i + 1).map((p) => (
              <button key={p} className={`pager-num ${p === safePage ? 'active' : ''}`} onClick={() => setPage(p)}>{p}</button>
            ))}
          </div>
          <button className="pager-btn" disabled={safePage >= maxPage} onClick={() => setPage(safePage + 1)}>下一页 ›</button>
          <span className="pager-info">第 {safePage}/{maxPage} 页 · 共 {totalCount} 台</span>
        </div>
      )}

      <PreviewModal
        device={preview}
        onClose={() => setPreview(null)}
        onDelete={(id) => {
          setPreview(null)
          handleDelete(id)
        }}
      />
      {showImport && <ImportModal onClose={() => setShowImport(false)} onImported={load} />}
      {showServerModal && (
        <ServerManagerModal
          onClose={() => setShowServerModal(false)}
          onImported={() => {
            load()
            loadServerIps()
            api.listServers()
              .then((r) => {
                const rows = r.data || []
                setServerCount(rows.length)
                setRegisteredHosts(rows.map((s) => s.host).filter(Boolean))
              })
              .catch(() => {})
          }}
        />
      )}

      {groupControlOpen && (
        <GroupControlModal
          selectedIds={selectedIds}
          onClose={() => setGroupControlOpen(false)}
        />
      )}

      {/* 批量执行脚本弹窗 */}
      {scriptModal && (
        <div className="modal-mask" onClick={() => setScriptModal(false)}>
          <div className="modal-body" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>执行脚本 · 已选 {selectedIds.length} 台</span>
              <button className="modal-close" onClick={() => setScriptModal(false)}>✕</button>
            </div>
            {scripts.length === 0 ? (
              <div className="empty-hint" style={{ padding: 30 }}>
                暂无脚本，请先在「脚本管理」创建脚本
              </div>
            ) : (
              <div className="script-pick-list">
                {scripts.map((s) => (
                  <div className="script-pick-item" key={s.id}>
                    <div className="script-pick-info">
                      <div className="script-pick-name">{s.name}</div>
                      <div className="script-pick-meta">{(s.steps?.length || 0)} 步</div>
                    </div>
                    <button onClick={() => runScript(s.id)} disabled={busy}>执行</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* 批量加入分组弹窗 */}
      {groupModal && (
        <div className="modal-mask" onClick={() => setGroupModal(false)}>
          <div className="modal-body" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>加入分组 · 已选 {selectedIds.length} 台</span>
              <button className="modal-close" onClick={() => setGroupModal(false)}>✕</button>
            </div>
            <div className="push-sec-title">选择目标分组</div>
            <div className="dev-pick push-group-pick">
              <label className="chip">
                <input
                  type="radio"
                  name="groupTarget"
                  checked={groupTarget === null}
                  onChange={() => setGroupTarget(null)}
                />
                <b>移出分组（未分组）</b>
              </label>
              {groups.map((g) => (
                <label key={g.id} className="chip">
                  <input
                    type="radio"
                    name="groupTarget"
                    checked={groupTarget === g.id}
                    onChange={() => setGroupTarget(g.id)}
                  />
                  <b>{g.name}</b>
                  <span className="chip-sub">{g.device_count} 台</span>
                </label>
              ))}
              {groups.length === 0 && <p className="hint">暂无分组，可先到「分组管理」创建</p>}
            </div>
            <div className="modal-actions">
              <button className="ghost" onClick={() => setGroupModal(false)} disabled={busy}>取消</button>
              <button onClick={handleSetGroup} disabled={busy || groupTarget === undefined}>确定</button>
            </div>
          </div>
        </div>
      )}
      {renameDevice && (
        <div className="modal-mask" onClick={() => { if (!renaming) setRenameDevice(null) }}>
          <div className="modal-body" style={{ maxWidth: 420 }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>重命名云手机</span>
              <button className="modal-close" onClick={() => setRenameDevice(null)} disabled={renaming}>✕</button>
            </div>
            <form onSubmit={submitRename}>
              <input
                autoFocus
                value={renameValue}
                maxLength={32}
                onChange={(e) => setRenameValue(e.target.value)}
                placeholder="给这台云手机起个名字"
                style={{
                  width: '100%', boxSizing: 'border-box', padding: '9px 10px',
                  border: '1px solid var(--border)', borderRadius: 8, fontSize: 13,
                  background: 'var(--bg-elevated)', color: 'var(--text)',
                }}
              />
              <div className="hint" style={{ marginTop: 6 }}>
                {renameDevice.plan_name ? `当前套餐：${renameDevice.plan_name} · ` : ''}最多 32 个字符
              </div>
              {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
              <div className="modal-actions" style={{ marginTop: 14 }}>
                <button type="button" className="ghost" onClick={() => setRenameDevice(null)} disabled={renaming}>取消</button>
                <button type="submit" className="btn-primary" disabled={renaming}>
                  {renaming ? '保存中…' : '保存'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
      {detailDevice && (
        <div onClick={() => setDetailDevice(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1500 }}>
          <div onClick={(e) => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 12, padding: 20, width: 420, maxWidth: '92vw', maxHeight: '80vh', overflow: 'auto' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <h3 style={{ margin: 0, fontSize: 16 }}>
                设备详情
                {detailSpec === null && (
                  <span style={{ marginLeft: 8, fontSize: 12, fontWeight: 400, color: '#94a3b8' }}>读取中…</span>
                )}
              </h3>
              <button
                onClick={() => setDetailDevice(null)}
                title="关闭"
                style={{ border: 'none', background: 'transparent', fontSize: 22, lineHeight: 1, cursor: 'pointer', color: '#6b7280' }}
              >×</button>
            </div>
            {detailSpec && detailSpec.android && (
              <div style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: '8px 10px', marginBottom: 10, fontSize: 13, fontWeight: 600, lineHeight: 1.5, wordBreak: 'break-all' }}>
                {detailSpec.summary || [`Android ${detailSpec.android}`, detailSpec.resolution, detailSpec.cpu, detailSpec.memory, detailSpec.storage].filter(Boolean).join(' · ')}
                <div style={{ fontWeight: 400, fontSize: 11, color: '#94a3b8', marginTop: 4 }}>实时读取自设备（adb）</div>
              </div>
            )}
            {detailSpecErr && (
              <div style={{ background: '#fef2f2', color: '#b91c1c', border: '1px solid #fecaca', borderRadius: 8, padding: '8px 10px', margin: '2px 0 10px', fontSize: 12, lineHeight: 1.6, display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                <span style={{ flex: 1, wordBreak: 'break-all' }}>{detailSpecErr}</span>
                <button
                  onClick={() => setDetailSpecTick((t) => t + 1)}
                  style={{ border: '1px solid #fecaca', background: '#fff', color: '#b91c1c', borderRadius: 6, padding: '2px 8px', fontSize: 12, cursor: 'pointer', flexShrink: 0 }}
                >重试</button>
              </div>
            )}
            <div style={{ fontSize: 13, lineHeight: 2 }}>
              {[
                ['名称', detailDevice.name],
                ['状态', { running: '运行中', stopped: '已停止', creating: '创建中', error: '异常' }[detailDevice.status] || detailDevice.status],
                // 服务器与网络信息（序列号 / 后端 / IP / 内网 IP）不在详情弹窗展示。
                ['Android', detailSpec?.android || detailDevice.spec_android || '-'],
                ['分辨率', detailSpec?.resolution || detailDevice.spec_resolution || '-'],
                ['CPU', detailSpec?.cpu || detailDevice.spec_cpu || '-'],
                ['内存', detailSpec?.memory || detailDevice.spec_memory || '-'],
                ['存储', detailSpec?.storage || detailDevice.spec_storage || '-'],
                ['创建时间', detailDevice.created_at ? new Date(detailDevice.created_at).toLocaleString('zh-CN', { hour12: false }) : '-'],
                ['最后在线', detailDevice.last_seen ? new Date(detailDevice.last_seen).toLocaleString('zh-CN', { hour12: false }) : '-'],
              ].map(([k, v]) => (
                <div key={k} style={{ display: 'flex', gap: 10 }}>
                  <span style={{ width: 76, color: 'var(--text-muted)', flexShrink: 0 }}>{k}</span>
                  <span style={{ flex: 1, wordBreak: 'break-all' }}>{String(v ?? '-')}</span>
                </div>
              ))}
            </div>
            {detailSpec && (detailSpec.cpu || detailSpec.memory || detailSpec.storage) && (
              <div style={{ marginTop: 10, fontSize: 11, lineHeight: 1.6, color: '#94a3b8' }}>
                CPU / 内存 / 存储为容器可见的资源上限，未单独限额。
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
              <button onClick={() => setDetailDevice(null)}
                style={{ padding: '8px 16px', border: '1px solid #d1d5db', borderRadius: 8, background: '#fff', fontSize: 13, cursor: 'pointer' }}>关闭</button>
            </div>
          </div>
        </div>
      )}

    </div>
  )
}

import { useEffect, useRef, useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api } from '../api/client.js'
import { uiAlert, uiConfirm } from '../lib/dialog.js'

// Android keyevent codes
const KEYCODES = {
  HOME: 3, BACK: 4, MENU: 82, POWER: 26,
  VOLUME_UP: 24, VOLUME_DOWN: 25, MUTE: 164,
  ENTER: 66, DEL: 67, TAB: 61,
  DPAD_UP: 19, DPAD_DOWN: 20, DPAD_LEFT: 21, DPAD_RIGHT: 22,
  RECENT: 187, NOTIFICATION: 83,
}

const scriptLoads = new Map()

function loadScriptOnce(src) {
  if (scriptLoads.has(src)) return scriptLoads.get(src)
  const request = new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = src
    script.async = true
    script.onload = resolve
    script.onerror = () => reject(new Error(`加载失败：${src}`))
    document.head.appendChild(script)
  }).catch((error) => {
    scriptLoads.delete(src)
    throw error
  })
  scriptLoads.set(src, request)
  return request
}

function loadBroadway() {
  if (window.Player) return Promise.resolve()
  return Promise.all([
    loadScriptOnce('/broadway/YUVCanvas.js'),
    loadScriptOnce('/broadway/Decoder.js'),
  ])
    .then(() => loadScriptOnce('/broadway/Player.js'))
    .then(() => {
      if (!window.Player) throw new Error('Broadway.js 初始化失败')
    })
}

export default function DeviceControl() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [device, setDevice] = useState(null)
  // screenSize = 设备**输入坐标系**（触摸坐标要用这个）
  // frameSize  = 推流帧尺寸（画面 letterbox 要用这个）
  // 推流做了缩放（如 1080x1920 -> 720x1280）时两者不同，混用会让点击整体偏移。
  const [screenSize, setScreenSize] = useState({ width: 1080, height: 1920 })
  const [frameSize, setFrameSize] = useState({ width: 1080, height: 1920 })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showKeyboard, setShowKeyboard] = useState(false)
  const [fps, setFps] = useState(0)
  const [streamStatus, setStreamStatus] = useState('connecting') // connecting | live | error | reconnecting
  const [broadwayReady, setBroadwayReady] = useState(() => !!window.Player)

  const canvasContainerRef = useRef(null)
  const playerRef = useRef(null)
  const wsRef = useRef(null)
  const hiddenInputRef = useRef(null)
  const dragStart = useRef(null)
  const frameCount = useRef(0)
  const lastFpsTime = useRef(0)
  const reconnectTimer = useRef(null)

  // 加载设备信息和屏幕尺寸
  useEffect(() => {
    let alive = true
    async function load() {
      try {
        const [devRes, sizeRes] = await Promise.all([
          api.getDevice(id),
          api.controlDevice(id, 'screen_size', {}),
        ])
        if (!alive) return
        setDevice(devRes.data)
        if (sizeRes.data.width) {
          setScreenSize({ width: sizeRes.data.width, height: sizeRes.data.height })
          // 后端没给帧尺寸（老版本）时，退回用设备分辨率
          setFrameSize({
            width: sizeRes.data.frame_width || sizeRes.data.width,
            height: sizeRes.data.frame_height || sizeRes.data.height,
          })
        }
        setLoading(false)
      } catch (e) {
        if (!alive) return
        setError('加载设备失败：' + (e.response?.data?.error || e.message))
        setLoading(false)
      }
    }
    load()
    return () => { alive = false }
  }, [id])

  // 投屏解码器只在进入远程控制页时加载，不再拖慢登录页和其他业务页面。
  useEffect(() => {
    if (broadwayReady || loading || error) return undefined
    let alive = true
    loadBroadway()
      .then(() => { if (alive) setBroadwayReady(true) })
      .catch((e) => {
        if (alive) setError(`视频解码器加载失败：${e.message}`)
      })
    return () => { alive = false }
  }, [broadwayReady, loading, error])

  // 初始化 Broadway.js 播放器
  useEffect(() => {
    if (loading || error || !broadwayReady || !canvasContainerRef.current) return

    const Player = window.Player
    if (!Player) {
      setError('视频解码器未初始化，请刷新页面')
      return
    }

    // 创建播放器（Web Worker 解码 + WebGL 渲染）
    const player = new Player({
      useWorker: true,
      workerFile: '/broadway/Decoder.js',
      webgl: 'auto',
      size: { width: frameSize.width, height: frameSize.height },
    })

    // 监听解码帧用于 FPS 统计
    const origOnPicture = player.onPictureDecoded
    player.onPictureDecoded = function (buffer, width, height, infos) {
      frameCount.current++
      const now = performance.now()
      if (now - lastFpsTime.current >= 1000) {
        setFps(frameCount.current)
        frameCount.current = 0
        lastFpsTime.current = now
      }
      if (origOnPicture) origOnPicture.call(this, buffer, width, height, infos)
    }

    // 将 canvas 加入 DOM
    const canvas = player.canvas
    canvas.style.width = '100%'
    canvas.style.height = '100%'
    canvas.style.objectFit = 'contain'
    canvas.style.display = 'block'
    canvasContainerRef.current.innerHTML = ''
    canvasContainerRef.current.appendChild(canvas)

    playerRef.current = player

    return () => {
      // 清理播放器
      if (player.worker) {
        player.worker.terminate()
      }
      if (canvasContainerRef.current) {
        canvasContainerRef.current.innerHTML = ''
      }
      playerRef.current = null
    }
  }, [loading, error, broadwayReady, frameSize.width, frameSize.height])

  // WebSocket 视频流连接
  const connectStream = useCallback(() => {
    if (!device) return

    const token = localStorage.getItem('token') || ''
    const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${wsProto}//${window.location.host}/ws/devices/${id}/stream?token=${encodeURIComponent(token)}&bitrate=8000000`

    setStreamStatus('connecting')
    const ws = new WebSocket(wsUrl)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    ws.onopen = () => {
      setStreamStatus('live')
    }

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer && playerRef.current) {
        const data = new Uint8Array(e.data)
        if (data.length > 0) {
          playerRef.current.decode(data)
        }
      }
    }

    ws.onerror = () => {
      setStreamStatus('error')
    }

    ws.onclose = () => {
      setStreamStatus('reconnecting')
      // 自动重连
      reconnectTimer.current = setTimeout(() => {
        connectStream()
      }, 2000)
    }
  }, [device, id])

  // 设备加载完成后启动视频流
  useEffect(() => {
    if (!loading && !error && broadwayReady && device) {
      connectStream()
    }
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
      }
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
      }
    }
  }, [loading, error, broadwayReady, device, connectStream])

  // 坐标映射：屏幕像素坐标 -> 设备物理坐标
  //
  // 两步走，不能省：
  //   1) 按**帧尺寸**算出去掉黑边的实际画面区域（letterbox）
  //   2) 把点击位置归一化后，乘的是**设备输入分辨率**
  // 若两步都用帧尺寸，720p 推流时坐标会整体缩小 1.5 倍
  // （越往右下偏得越多，左上角几乎准）。
  const mapCoords = useCallback((clientX, clientY) => {
    const container = canvasContainerRef.current
    if (!container) return { x: 0, y: 0 }
    const canvas = container.querySelector('canvas')
    if (!canvas) return { x: 0, y: 0 }
    const rect = canvas.getBoundingClientRect()
    const imgRatio = frameSize.width / frameSize.height
    const containerRatio = rect.width / rect.height
    let displayW, displayH, offsetX, offsetY
    if (containerRatio > imgRatio) {
      displayH = rect.height
      displayW = displayH * imgRatio
      offsetX = (rect.width - displayW) / 2
      offsetY = 0
    } else {
      displayW = rect.width
      displayH = displayW / imgRatio
      offsetX = 0
      offsetY = (rect.height - displayH) / 2
    }
    const x = Math.round(((clientX - rect.left - offsetX) / displayW) * screenSize.width)
    const y = Math.round(((clientY - rect.top - offsetY) / displayH) * screenSize.height)
    return { x: Math.max(0, Math.min(screenSize.width, x)), y: Math.max(0, Math.min(screenSize.height, y)) }
  }, [screenSize.width, screenSize.height, frameSize.width, frameSize.height])

  // 鼠标/触摸事件
  const handlePointerDown = (e) => {
    e.preventDefault()
    const pt = e.touches ? e.touches[0] : e
    const coords = mapCoords(pt.clientX, pt.clientY)
    dragStart.current = { ...coords, time: Date.now(), clientX: pt.clientX, clientY: pt.clientY }
  }

  const handlePointerUp = async (e) => {
    if (!dragStart.current) return
    const pt = e.changedTouches ? e.changedTouches[0] : e
    const coords = mapCoords(pt.clientX, pt.clientY)
    const start = dragStart.current
    const dx = coords.x - start.x
    const dy = coords.y - start.y
    const dist = Math.sqrt(dx * dx + dy * dy)
    const duration = Date.now() - start.time

    if (dist < 20 && duration < 500) {
      await api.controlDevice(id, 'tap', { x: start.x, y: start.y })
    } else {
      await api.controlDevice(id, 'swipe', {
        x1: start.x, y1: start.y, x2: coords.x, y2: coords.y,
        duration: Math.max(100, Math.min(duration, 1000)),
      })
    }
    dragStart.current = null
  }

  // 按键事件
  const sendKey = async (keycode) => {
    await api.controlDevice(id, 'key', { keycode })
  }

  const handleKeyDown = (e) => {
    const keyMap = {
      'Enter': KEYCODES.ENTER,
      'Backspace': KEYCODES.DEL,
      'Tab': KEYCODES.TAB,
      'ArrowUp': KEYCODES.DPAD_UP,
      'ArrowDown': KEYCODES.DPAD_DOWN,
      'ArrowLeft': KEYCODES.DPAD_LEFT,
      'ArrowRight': KEYCODES.DPAD_RIGHT,
      'Escape': KEYCODES.BACK,
    }
    if (keyMap[e.key]) {
      e.preventDefault()
      sendKey(keyMap[e.key])
      return
    }
  }

  const handleInputChange = async (e) => {
    const val = e.target.value
    if (val) {
      await api.controlDevice(id, 'text', { text: val })
      e.target.value = ''
    }
  }

  const focusInput = () => {
    setShowKeyboard(true)
    setTimeout(() => hiddenInputRef.current?.focus(), 50)
  }

  const handleScreenshot = async () => {
    try {
      const r = await api.deviceScreenshot(id)
      const url = window.URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `${device?.name || 'device'}_${Date.now()}.png`
      a.click()
      window.URL.revokeObjectURL(url)
    } catch (e) {
      console.error('截图失败', e)
      await uiAlert('截图失败：' + (e.response?.data?.error || e.message))
    }
  }

  if (loading) return <div className="control-loading">正在连接设备...</div>
  if (error) return <div className="control-error">{error}<button onClick={() => navigate(-1)}>返回</button></div>

  const statusColors = {
    connecting: '#f59e0b',
    live: '#10b981',
    error: '#ef4444',
    reconnecting: '#f59e0b',
  }
  const statusLabels = {
    connecting: '连接中',
    live: '直播中',
    error: '连接错误',
    reconnecting: '重连中',
  }

  return (
    <div className="device-control-page">
      {/* 顶部工具栏 */}
      <div className="control-topbar">
        <button className="ctrl-back" onClick={() => navigate(-1)}>← 返回</button>
        <div className="ctrl-title">
          <span className="ctrl-name">{device?.name}</span>
          <span className="ctrl-fps">{fps} fps</span>
          <span className="ctrl-stream-status" style={{ color: statusColors[streamStatus] }}>
            ● {statusLabels[streamStatus]}
          </span>
        </div>
      </div>

      {/* 主显示区域 */}
      <div className="control-main">
        <div
          className="screen-container"
          onMouseDown={handlePointerDown}
          onMouseUp={handlePointerUp}
          onTouchStart={handlePointerDown}
          onTouchEnd={handlePointerUp}
          onClick={() => focusInput()}
        >
          <div ref={canvasContainerRef} className="video-canvas-container" />
          {streamStatus !== 'live' && (
            <div className="stream-overlay">
              <div className="stream-loading-spinner" />
              <span>{statusLabels[streamStatus]}...</span>
            </div>
          )}

          {/* 隐藏的输入框，用于捕获键盘和中文输入法 */}
          <input
            ref={hiddenInputRef}
            className="hidden-ime-input"
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
          />

          {/* 屏幕右侧竖向悬浮工具栏（仿 ws-scrcpy） */}
          <div className="screen-toolbar" onClick={(e) => e.stopPropagation()}>
            <button className="st-btn" onClick={() => sendKey(KEYCODES.BACK)} title="返回">◀</button>
            <button className="st-btn" onClick={() => sendKey(KEYCODES.HOME)} title="主页">⌂</button>
            <button className="st-btn" onClick={() => sendKey(KEYCODES.RECENT)} title="后台">▤</button>
            <button className="st-btn" onClick={() => sendKey(KEYCODES.VOLUME_UP)} title="音量+">🔊</button>
            <button className="st-btn" onClick={() => sendKey(KEYCODES.VOLUME_DOWN)} title="音量-">🔉</button>
            <button className="st-btn" onClick={() => sendKey(KEYCODES.POWER)} title="电源">⏻</button>
            <button className="st-btn" onClick={handleScreenshot} title="截图">📷</button>
          </div>
        </div>
      </div>

      {/* 键盘输入提示浮层 */}
      {showKeyboard && (
        <div className="ime-hint" onClick={() => setShowKeyboard(false)}>
          键盘已激活，可直接输入文字（支持中文输入法）· 点击关闭
        </div>
      )}
    </div>
  )
}

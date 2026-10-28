import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client.js'
import { loadBroadway } from '../lib/broadway.js'

/* 云手机实时投屏组件（WebSocket 视频流 + Broadway 解码 + canvas 渲染）
 *
 * 只负责「显示画面」和「把点击/滑动换算成设备坐标」，
 * 具体要做什么（直接下发控制指令、或者录成任务步骤）由调用方通过 onAction 决定。
 *
 * onAction({ type: 'tap', x, y })
 * onAction({ type: 'swipe', x1, y1, x2, y2, duration })
 */
export default function DeviceStream({ deviceId, onAction, interactive = true, className = '' }) {
  const [screenSize, setScreenSize] = useState({ width: 1080, height: 1920 })
  const [frameSize, setFrameSize] = useState({ width: 1080, height: 1920 })
  const [status, setStatus] = useState('connecting')   // connecting | live | error | reconnecting
  const [error, setError] = useState('')
  const [ready, setReady] = useState(() => !!window.Player)

  const containerRef = useRef(null)
  const playerRef = useRef(null)
  const wsRef = useRef(null)
  const dragRef = useRef(null)
  const reconnectTimer = useRef(null)

  // 1) 设备输入分辨率 + 推流帧尺寸
  useEffect(() => {
    let alive = true
    setStatus('connecting')
    setError('')
    api.controlDevice(deviceId, 'screen_size', {}).then((r) => {
      if (!alive || !r.data) return
      if (r.data.width) {
        setScreenSize({ width: r.data.width, height: r.data.height })
        setFrameSize({
          width: r.data.frame_width || r.data.width,
          height: r.data.frame_height || r.data.height,
        })
      }
    }).catch((e) => {
      if (alive) setError(`获取屏幕尺寸失败：${e.response?.data?.error || e.message}`)
    })
    return () => { alive = false }
  }, [deviceId])

  // 2) 解码器按需加载
  useEffect(() => {
    if (ready) return undefined
    let alive = true
    loadBroadway()
      .then(() => { if (alive) setReady(true) })
      .catch((e) => { if (alive) setError(`视频解码器加载失败：${e.message}`) })
    return () => { alive = false }
  }, [ready])

  // 3) 初始化播放器
  useEffect(() => {
    if (!ready || !containerRef.current) return undefined
    const Player = window.Player
    if (!Player) return undefined

    const player = new Player({
      useWorker: true,
      workerFile: '/broadway/Decoder.js',
      webgl: 'auto',
      size: { width: frameSize.width, height: frameSize.height },
    })
    const canvas = player.canvas
    canvas.style.width = '100%'
    canvas.style.height = '100%'
    canvas.style.objectFit = 'contain'
    canvas.style.display = 'block'
    containerRef.current.innerHTML = ''
    containerRef.current.appendChild(canvas)
    playerRef.current = player

    return () => {
      try { player.worker?.terminate() } catch { /* ignore */ }
      if (containerRef.current) containerRef.current.innerHTML = ''
      playerRef.current = null
    }
  }, [ready, frameSize.width, frameSize.height])

  // 4) 视频流
  const connect = useCallback(() => {
    const token = localStorage.getItem('token') || ''
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/devices/${deviceId}/stream?token=${encodeURIComponent(token)}&bitrate=8000000`
    setStatus('connecting')
    const ws = new WebSocket(url)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws
    ws.onopen = () => setStatus('live')
    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer && playerRef.current) {
        const data = new Uint8Array(e.data)
        if (data.length > 0) playerRef.current.decode(data)
      }
    }
    ws.onerror = () => setStatus('error')
    ws.onclose = () => {
      setStatus('reconnecting')
      reconnectTimer.current = setTimeout(connect, 2000)
    }
  }, [deviceId])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
      }
    }
  }, [connect])

  // 5) 坐标映射：先按帧尺寸去黑边，再乘设备输入分辨率
  const mapCoords = useCallback((clientX, clientY) => {
    const canvas = containerRef.current?.querySelector('canvas')
    if (!canvas) return { x: 0, y: 0 }
    const rect = canvas.getBoundingClientRect()
    const imgRatio = frameSize.width / frameSize.height
    const boxRatio = rect.width / rect.height
    let displayW; let displayH; let offsetX; let offsetY
    if (boxRatio > imgRatio) {
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
    return {
      x: Math.max(0, Math.min(screenSize.width, x)),
      y: Math.max(0, Math.min(screenSize.height, y)),
    }
  }, [screenSize.width, screenSize.height, frameSize.width, frameSize.height])

  function onPointerDown(e) {
    if (!interactive) return
    e.preventDefault()
    const p = mapCoords(e.clientX, e.clientY)
    dragRef.current = { ...p, time: Date.now() }
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }

  function onPointerUp(e) {
    if (!interactive || !dragRef.current) return
    const start = dragRef.current
    dragRef.current = null
    const p = mapCoords(e.clientX, e.clientY)
    const dx = p.x - start.x
    const dy = p.y - start.y
    const dist = Math.sqrt(dx * dx + dy * dy)
    const duration = Date.now() - start.time
    if (dist < 20 && duration < 700) {
      onAction?.({ type: 'tap', x: start.x, y: start.y })
    } else {
      onAction?.({
        type: 'swipe',
        x1: start.x, y1: start.y, x2: p.x, y2: p.y,
        duration: Math.max(100, Math.min(duration, 1000)),
      })
    }
  }

  const statusText = {
    connecting: '连接中…',
    live: '',
    error: '画面中断',
    reconnecting: '重连中…',
  }[status]

  return (
    <div className={`device-stream ${className}`}>
      <div
        ref={containerRef}
        className="device-stream-canvas"
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        style={{ cursor: interactive ? 'crosshair' : 'default' }}
      />
      {error && <div className="device-stream-hint">{error}</div>}
      {!error && statusText && <div className="device-stream-hint">{statusText}</div>}
    </div>
  )
}

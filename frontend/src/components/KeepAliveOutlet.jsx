import { useRef } from 'react'
import { useLocation, useOutlet } from 'react-router-dom'

// 设备页/文件页切换时保留已渲染的内容（与线上 B0 组件一致）。
const KEEP_ALIVE = new Set(['/devices', '/files'])

export default function KeepAliveOutlet() {
  const outlet = useOutlet()
  const { pathname } = useLocation()
  const cache = useRef(new Map())

  if (KEEP_ALIVE.has(pathname) && !cache.current.has(pathname)) {
    cache.current.set(pathname, outlet)
  }

  return (
    <>
      {[...cache.current.entries()].map(([path, node]) => (
        <div key={path} style={{ display: path === pathname ? 'block' : 'none' }}>
          {node}
        </div>
      ))}
      {!KEEP_ALIVE.has(pathname) && outlet}
    </>
  )
}

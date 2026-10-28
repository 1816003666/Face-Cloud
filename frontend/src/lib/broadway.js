// Broadway.js（H.264 软解 + WebGL 渲染）按需加载。
// 从 DeviceControl.jsx 抽出来，任务录制弹窗也要用同一套播放器。

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

export function loadBroadway() {
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

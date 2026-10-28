// 复制到剪贴板。
//
// 注意：局域网通过 http://192.168.x.x:8003 访问时不是安全上下文，
// navigator.clipboard 是 undefined，所以必须有 textarea + execCommand 兜底，
// 否则「复制密码」这类按钮在真实使用环境里会静默失效。
export async function copyText(text) {
  const value = String(text ?? '')
  if (!value) return false

  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch {
    /* 落到下面的兜底 */
  }

  try {
    const ta = document.createElement('textarea')
    ta.value = value
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-1000px'
    ta.style.left = '0'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    ta.setSelectionRange(0, value.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

export default copyText

// 统一弹窗：确认 / 提示
// 目的：干掉浏览器原生 confirm()/alert()（灰白方块、和平台风格完全不一致），
// 换成项目自己的 .modal-mask / .modal-body 样式，保证全站一致。
//
// 用法：
//   if (!(await uiConfirm('确认删除？'))) return
//   await uiAlert('已保存')
//   if (!(await uiConfirm(msg, { title: '确认退单', danger: true, confirmText: '退单' }))) return

let layer = null
let activeResolve = null

function ensureLayer() {
  if (layer && document.body.contains(layer)) return layer
  layer = document.createElement('div')
  layer.className = 'ui-dialog-layer'
  document.body.appendChild(layer)
  return layer
}

function teardown() {
  if (layer) {
    layer.remove()
    layer = null
  }
  activeResolve = null
  document.removeEventListener('keydown', onKeyDown, true)
}

function onKeyDown(e) {
  if (!layer) return
  if (e.key === 'Escape') {
    e.preventDefault()
    finish(false)
  } else if (e.key === 'Enter') {
    const el = layer.querySelector('[data-ui-dialog-confirm]')
    if (el) {
      e.preventDefault()
      finish(true)
    }
  }
}

function finish(value) {
  const resolve = activeResolve
  teardown()
  if (resolve) resolve(value)
}

function open(message, opts = {}) {
  // 同一个时刻只允许一个弹窗：上一个直接按「取消」收掉
  if (activeResolve) finish(false)

  const {
    title = '请确认',
    danger = false,
    confirmText = '确定',
    cancelText = '取消',
    withCancel = true,
  } = opts

  const root = ensureLayer()
  root.innerHTML = ''

  const mask = document.createElement('div')
  mask.className = 'modal-mask ui-dialog-mask'

  const body = document.createElement('div')
  body.className = 'modal-body ui-dialog-body'
  body.setAttribute('role', 'dialog')
  body.setAttribute('aria-modal', 'true')

  const head = document.createElement('div')
  head.className = 'modal-title'
  const titleEl = document.createElement('span')
  titleEl.textContent = title
  head.appendChild(titleEl)
  body.appendChild(head)

  const text = document.createElement('div')
  text.className = 'ui-dialog-message'
  text.textContent = String(message ?? '')
  body.appendChild(text)

  const actions = document.createElement('div')
  actions.className = 'modal-actions ui-dialog-actions'

  if (withCancel) {
    const cancel = document.createElement('button')
    cancel.type = 'button'
    cancel.className = 'ghost'
    cancel.textContent = cancelText
    cancel.addEventListener('click', () => finish(false))
    actions.appendChild(cancel)
  }

  const ok = document.createElement('button')
  ok.type = 'button'
  ok.dataset.uiDialogConfirm = '1'
  if (danger) ok.className = 'danger-solid'
  ok.textContent = confirmText
  ok.addEventListener('click', () => finish(true))
  actions.appendChild(ok)

  body.appendChild(actions)
  mask.appendChild(body)
  root.appendChild(mask)

  mask.addEventListener('click', (e) => {
    if (e.target === mask) finish(false)
  })

  document.addEventListener('keydown', onKeyDown, true)
  // 焦点落在主按钮上，回车即可确认
  window.setTimeout(() => ok.focus(), 0)

  return new Promise((resolve) => {
    activeResolve = resolve
  })
}

// 按文案自动挑标题/按钮文案，避免 27 个调用点都要手写一遍参数
function inferConfirmStyle(text) {
  if (/退单|退款/.test(text)) return { title: '确认退单', confirmText: '退单', danger: true }
  if (/删除|清理|移除|销毁|停用|注销/.test(text)) return { title: '确认删除', confirmText: '删除', danger: true }
  if (/重启|关机|停止/.test(text)) return { title: '请确认', confirmText: '确定', danger: false }
  return { title: '请确认', confirmText: '确定', danger: false }
}

export function uiConfirm(message, opts = {}) {
  const text = String(message ?? '')
  return open(text, { ...inferConfirmStyle(text), ...opts, withCancel: true })
}

export function uiAlert(message, opts = {}) {
  return open(message, { title: '提示', confirmText: '知道了', ...opts, withCancel: false })
}

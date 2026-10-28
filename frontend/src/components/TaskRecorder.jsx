import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client.js'
import DeviceStream from './DeviceStream.jsx'

/* 录制器：在弹窗里实时看云手机画面，把点击/滑动/输入/按键录成序列任务步骤。
 *
 * 画面是 WebSocket 实时投屏（DeviceStream），点击/滑动坐标由投屏组件换算成设备坐标；
 * 每次操作既真实下发到云手机，也同时录成任务步骤。
 */

const KEY_LABELS = { '3': 'HOME', '4': 'BACK', '187': '多任务' }

function stepLabel(s) {
  if (s.action === 'tap') return `点击 (${s.x}, ${s.y})`
  if (s.action === 'swipe') return `滑动 (${s.x1}, ${s.y1}) → (${s.x2}, ${s.y2})`
  if (s.action === 'text') return `输入「${s.text}」`
  if (s.action === 'key') return `按键 ${KEY_LABELS[String(s.keycode)] || s.keycode}`
  if (s.action === 'wait') return `等待 ${s.seconds} 秒`
  if (s.action === 'open_url') return `打开 ${s.url}`
  return s.action
}

export default function TaskRecorder({ devices, onClose, onSaved, canCreate }) {
  const running = devices.filter((d) => d.status === 'running')
  const [devId, setDevId] = useState(running[0]?.id || devices[0]?.id || '')
  const [steps, setSteps] = useState([])
  const [text, setText] = useState('')
  const [waitSec, setWaitSec] = useState(2)
  const [name, setName] = useState('')
  const [scheduleType, setScheduleType] = useState('once')
  const [intervalSec, setIntervalSec] = useState(86400)
  const [targets, setTargets] = useState([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const dragRef = useRef(null)

  // 录制用的那台设备默认就是执行设备，用户不用再勾一次
  useEffect(() => {
    if (devId) setTargets((prev) => (prev.length ? prev : [Number(devId)]))
  }, [devId])

  // 投屏组件把点击/滑动换算成设备坐标后回调这里：
  // 一边真实下发到云手机，一边记成任务步骤。
  function handleAction(a) {
    if (busy) return
    if (a.type === 'tap') {
      send('tap', { x: a.x, y: a.y }, { action: 'tap', x: a.x, y: a.y })
    } else if (a.type === 'swipe') {
      send('swipe', { x1: a.x1, y1: a.y1, x2: a.x2, y2: a.y2, duration: a.duration },
        { action: 'swipe', x1: a.x1, y1: a.y1, x2: a.x2, y2: a.y2, duration: a.duration })
    }
  }

  async function send(action, body, step) {
    setBusy(true)
    setErr('')
    try {
      await api.controlDevice(devId, action, body)
      if (step) setSteps((s) => [...s, step])
    } catch (e) {
      setErr(`执行失败：${e.response?.data?.error || e.message}`)
    } finally {
      setBusy(false)
    }
  }

  function removeStep(i) {
    setSteps((s) => s.filter((_, idx) => idx !== i))
  }

  function moveStep(i, dir) {
    setSteps((s) => {
      const next = [...s]
      const j = i + dir
      if (j < 0 || j >= next.length) return s
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  }

  async function save() {
    setErr('')
    if (!canCreate) {
      setErr('当前账号没有创建任务的权限')
      return
    }
    if (!name.trim()) {
      setErr('请填写任务名')
      return
    }
    if (steps.length === 0) {
      setErr('还没有录制任何操作')
      return
    }
    if (targets.length === 0) {
      setErr('请选择要执行的云手机')
      return
    }
    setBusy(true)
    try {
      await api.createTask({
        name: name.trim(),
        action: 'sequence',
        params: { steps },
        device_ids: targets,
        schedule_type: scheduleType,
        interval_seconds: Number(intervalSec),
        cron_expr: '',
        enabled: true,
        max_retries: 0,
      })
      onSaved?.()
    } catch (e) {
      setErr(e.response?.data?.error || '保存失败')
    } finally {
      setBusy(false)
    }
  }

  if (devices.length === 0) {
    return (
      <div className="modal-mask" onClick={onClose}>
        <div className="modal-body recorder-modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 420 }}>
          <div className="modal-title">
            <span>录制创建任务</span>
            <button className="modal-close" onClick={onClose}>关闭</button>
          </div>
          <p className="hint" style={{ lineHeight: 1.9, margin: '8px 0 18px' }}>
            当前账号还没有可用的云手机，录制需要一个能看到的设备。
            <br />
            先去「我的套餐」购买套餐，再到「设备管理」创建一台云手机，然后回来录制。
          </p>
          <div className="modal-actions">
            <button className="ghost" onClick={onClose}>知道了</button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal-body recorder-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">
          <span>录制创建任务</span>
          <button className="modal-close" onClick={onClose}>关闭</button>
        </div>

        <div className="recorder-grid">
          {/* 左：云手机画面 */}
          <div className="recorder-left">
            <div className="recorder-bar">
              <select value={devId} onChange={(e) => setDevId(e.target.value)}>
                {devices.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} #{d.id}{d.status === 'running' ? '' : `（${d.status}）`}
                  </option>
                ))}
              </select>
            </div>

            <div className="recorder-screen">
              <DeviceStream deviceId={devId} onAction={handleAction} />
            </div>

            <div className="recorder-controls">
              <button type="button" className="ghost" disabled={busy}
                onClick={() => send('key', { keycode: 3 }, { action: 'key', keycode: 3 })}>HOME</button>
              <button type="button" className="ghost" disabled={busy}
                onClick={() => send('key', { keycode: 4 }, { action: 'key', keycode: 4 })}>返回</button>
              <button type="button" className="ghost" disabled={busy}
                onClick={() => send('key', { keycode: 187 }, { action: 'key', keycode: 187 })}>多任务</button>
            </div>

            <div className="recorder-inputs">
              <input
                placeholder="要输入的文字（先点输入框再点设备里的输入位置）"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              <button type="button" className="ghost" disabled={busy || !text}
                onClick={async () => {
                  await send('text', { text }, { action: 'text', text })
                  setText('')
                }}>输入文字</button>
            </div>
            <div className="recorder-inputs">
              <input
                type="number" min="1" max="10" value={waitSec}
                onChange={(e) => setWaitSec(e.target.value)}
                style={{ width: 80 }}
              />
              <button type="button" className="ghost"
                onClick={() => setSteps((s) => [...s, { action: 'wait', seconds: Number(waitSec) || 1 }])}>
                插入等待
              </button>
              <span className="hint">提示：直接点画面=点击，按住拖动=滑动</span>
            </div>
          </div>

          {/* 右：步骤与保存 */}
          <div className="recorder-right">
            <div className="recorder-steps-head">
              <span>录制步骤 <b>{steps.length}</b> 步</span>
              {steps.length > 0 && (
                <button type="button" className="ghost" onClick={() => setSteps([])}>清空</button>
              )}
            </div>
            <ol className="recorder-steps">
              {steps.map((s, i) => (
                <li key={`${s.action}-${i}`}>
                  <span className="recorder-step-text">{i + 1}. {stepLabel(s)}</span>
                  <span className="recorder-step-ops">
                    <button type="button" className="ghost" onClick={() => moveStep(i, -1)}>↑</button>
                    <button type="button" className="ghost" onClick={() => moveStep(i, 1)}>↓</button>
                    <button type="button" className="danger" onClick={() => removeStep(i)}>删</button>
                  </span>
                </li>
              ))}
              {steps.length === 0 && <li className="hint">还没有步骤：在左边画面点一下就会录下「点击」</li>}
            </ol>

            <div className="recorder-form">
              <label className="task-field">
                <span>任务名</span>
                <input placeholder="如：每日签到" value={name} onChange={(e) => setName(e.target.value)} />
              </label>
              <label className="task-field">
                <span>调度</span>
                <select value={scheduleType} onChange={(e) => setScheduleType(e.target.value)}>
                  <option value="once">单次</option>
                  <option value="interval">周期</option>
                  <option value="cron">Cron</option>
                </select>
              </label>
              {scheduleType === 'interval' && (
                <label className="task-field">
                  <span>间隔</span>
                  <select value={intervalSec} onChange={(e) => setIntervalSec(Number(e.target.value))}>
                    <option value={3600}>每 1 小时</option>
                    <option value={21600}>每 6 小时</option>
                    <option value={43200}>每 12 小时</option>
                    <option value={86400}>每天</option>
                    <option value={604800}>每周</option>
                  </select>
                </label>
              )}
            </div>

            <div className="task-devices">
              <div className="task-devices-head">
                <span>执行设备　已选 <b>{targets.length}</b> 台（录制用的那台已默认勾选）</span>
                <button type="button" className="ghost"
                  onClick={() => setTargets(devices.map((d) => d.id))}>全选</button>
                <button type="button" className="ghost" onClick={() => setTargets([])}>清空</button>
              </div>
              <div className="task-devices-list recorder-dev-list">
                {devices.map((d) => (
                  <label key={d.id} className="task-device">
                    <input
                      type="checkbox"
                      checked={targets.includes(d.id)}
                      onChange={(e) => setTargets((prev) => (e.target.checked
                        ? [...prev, d.id]
                        : prev.filter((x) => x !== d.id)))}
                    />
                    <span>{d.name}</span>
                    <span className="hint">#{d.id}</span>
                  </label>
                ))}
              </div>
            </div>

            {err && <div className="err">{err}</div>}

            <div className="modal-actions">
              <button className="ghost" onClick={onClose}>取消</button>
              <button className="accent" disabled={busy} onClick={save}>
                {busy ? '处理中…' : `保存为任务（${steps.length} 步）`}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

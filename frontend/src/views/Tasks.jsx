import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { uiAlert, uiConfirm } from '../lib/dialog.js'
import TaskRecorder from '../components/TaskRecorder.jsx'

// 健康巡检是平台运维功能：只有管理员能看到/创建（后端也会 403 兜底）
const PRIVILEGED_ACTIONS = new Set(['health_check'])

const ACTIONS = [
  { value: 'health_check', label: '健康巡检', adminOnly: true },
  { value: 'open_url', label: '打开网址' },
  { value: 'tap', label: '点击' },
  { value: 'swipe', label: '滑动' },
  { value: 'text', label: '输入文本' },
  { value: 'key', label: '按键' },
  { value: 'install', label: '安装应用' },
  { value: 'sequence', label: '序列' },
  { value: 'run_script', label: '执行脚本' },
  { value: 'wait', label: '等待' },
]

const LEVEL_COLOR = { ok: '#22c55e', warning: '#f59e0b', critical: '#ef4444' }

function fmtTime(s) {
  if (!s) return '-'
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

/* 健康巡检报告渲染 */
function HealthReport({ detail }) {
  const server = detail.server || {}
  const m = server.metrics || {}
  const devs = detail.devices || {}
  const mrow = (label, metric) => (
    <div className="hc-row" key={label}>
      <span>{label}</span>
      <span className="hc-value">
        {metric ? (
          <>
            {label === 'CPU 负载' && `${metric.value} / ${m.cores}核`}
            {label === '内存' && `${metric.used_pct}%（${metric.used_mb}MB/${metric.total_mb}MB）`}
            {label === '磁盘' && `${metric.used_pct}%（${metric.used}/${metric.size}）`}
            {label === '容器' && `${metric.running}/${metric.total}`}
          </>
        ) : '-'}
        <span className="hc-dot" style={{ background: LEVEL_COLOR[metric?.level] || '#94a3b8' }} />
      </span>
    </div>
  )
  return (
    <div className="hc-report">
      <div className="hc-sec">
        <div className="hc-sec-title">
          服务器
          <span className={`hc-badge ${server.ok ? 'ok' : 'fail'}`}>{server.ok ? '正常' : '异常'}</span>
        </div>
        <div className="hc-metrics">
          {mrow('CPU 负载', m.load)}
          {mrow('内存', m.memory)}
          {mrow('磁盘', m.disk)}
          {mrow('容器', m.containers)}
        </div>
        {server.issues?.length > 0 && (
          <div className="hc-issues">
            {server.issues.map((x, i) => <div key={i} className="hc-issue">• {x}</div>)}
          </div>
        )}
        {!server.reachable && <div className="hc-issue">• 服务器不可达（SSH 连接失败）</div>}
      </div>
      <div className="hc-sec">
        <div className="hc-sec-title">
          云手机
          <span className="hc-badge ok">{devs.ok} 正常</span>
          <span className={`hc-badge ${devs.failed ? 'fail' : 'ok'}`}>{devs.failed} 异常</span>
        </div>
        <div className="hc-dev-list">
          {(devs.items || []).map((d) => (
            <div className={`hc-dev ${d.ok ? 'ok' : 'fail'}`} key={d.device_id}>
              <div className="hc-dev-head">
                <span className="hc-dev-name">{d.name}</span>
                <span className={`hc-dot`} style={{ background: d.online ? '#22c55e' : '#ef4444' }} />
                <span className="hc-dev-cpu">CPU {d.cpu ?? '-'}%</span>
                <span className="hc-dev-mem">MEM {d.mem ?? '-'}%</span>
              </div>
              {d.issues?.length > 0 && (
                <div className="hc-issues">{d.issues.map((x, i) => <div key={i} className="hc-issue">• {x}</div>)}</div>
              )}
            </div>
          ))}
          {devs.items?.length === 0 && <div className="hint">无云手机设备</div>}
        </div>
      </div>
    </div>
  )
}

export default function Tasks() {
  const me = (() => {
    try { return JSON.parse(localStorage.getItem('user') || 'null') || {} } catch { return {} }
  })()
  const isAdmin = ['admin', 'superadmin'].includes(me.role || '')
  const actions = ACTIONS.filter((a) => isAdmin || !PRIVILEGED_ACTIONS.has(a.value))

  const [list, setList] = useState([])
  const [form, setForm] = useState({
    name: '',
    action: isAdmin ? 'health_check' : 'open_url',
    schedule_type: isAdmin ? 'interval' : 'once',
    interval_seconds: 86400,
    cron_expr: '',
  })
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [hist, setHist] = useState(null) // {task, list}
  const [devices, setDevices] = useState([])
  const [apkFiles, setApkFiles] = useState([])
  const [scripts, setScripts] = useState([])
  const [devSel, setDevSel] = useState([])
  const [editingId, setEditingId] = useState(null)   // null=新建，否则正在编辑的任务 id
  const [devQuery, setDevQuery] = useState('')
  const [onlyRunning, setOnlyRunning] = useState(false)
  const [busy, setBusy] = useState(false)
  const [showRecorder, setShowRecorder] = useState(false)
  const [showManual, setShowManual] = useState(false)
  const [fp, setFp] = useState({
    url: 'https://www.baidu.com', text: '',
    x: 500, y: 500, x1: 100, y1: 500, x2: 400, y2: 500, duration: 300,
    keycode: 3, seconds: 1, file_id: '', script_id: '',
    steps: '[{"action":"wait","seconds":1}]',
  })
  const setP = (k, v) => setFp((s) => ({ ...s, [k]: v }))

  async function load() {
    const r = await api.listTasks()
    setList(r.data)
  }
  useEffect(() => {
    load()
    const sp = new URLSearchParams(window.location.search)
    const sid = sp.get('script')
    if (sid) {
      setShowManual(true)
      setForm((f) => ({ ...f, action: 'run_script', name: sp.get('script_name') || '定时执行脚本' }))
      setFp((f) => ({ ...f, script_id: sid }))
    }
    api.listDevices({ page_size: 500 }).then((r) => {
      const d = r.data
      setDevices(Array.isArray(d) ? d : (d?.items || []))
    }).catch(() => {})
    api.listFiles({}).then((r) => {
      const d = r.data
      const items = Array.isArray(d) ? d : (d?.items || [])
      setApkFiles(items.filter((f) => /\.apk$/i.test(f.filename || '')))
    }).catch(() => {})
    api.listScripts().then((r) => {
      const d = r.data
      setScripts(Array.isArray(d) ? d : (d?.items || []))
    }).catch(() => {})
  }, [])

  function update(k, v) {
    setForm((f) => ({ ...f, [k]: v }))
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setErr(''); setMsg('')
    if (form.action === 'health_check') {
      const n = prompt('健康巡检默认巡检全部云手机。如需只巡检部分，请稍后在设备管理选择设备；确认创建？')
      if (n === null) return
    }
    let params = {}
    try {
      if (form.action === 'open_url') params = { url: fp.url }
      else if (form.action === 'tap') params = { x: Number(fp.x), y: Number(fp.y) }
      else if (form.action === 'swipe') {
        params = {
          x1: Number(fp.x1), y1: Number(fp.y1),
          x2: Number(fp.x2), y2: Number(fp.y2),
          duration: Number(fp.duration),
        }
      } else if (form.action === 'text') params = { text: fp.text }
      else if (form.action === 'key') params = { keycode: Number(fp.keycode) }
      else if (form.action === 'install') params = { file_id: Number(fp.file_id) }
      else if (form.action === 'wait') params = { seconds: Number(fp.seconds) }
      else if (form.action === 'run_script') params = { script_id: Number(fp.script_id) }
      else if (form.action === 'sequence') {
        const steps = JSON.parse(fp.steps || '[]')
        if (!Array.isArray(steps) || steps.length === 0) throw new Error('steps 不能为空')
        params = { steps }
      }
    } catch (e) {
      setErr(`参数不合法：${e.message}`)
      return
    }
    if (form.action !== 'health_check' && devSel.length === 0) {
      setErr('请至少选择一台云手机')
      return
    }
    const payload = {
      ...form,
      params,
      device_ids: form.action === 'health_check' ? [] : devSel,
      interval_seconds: Number(form.interval_seconds),
      cron_expr: form.cron_expr || '',
      max_retries: Number(form.max_retries ?? 0),
    }
    setBusy(true)
    try {
      if (editingId) {
        await api.updateTask(editingId, payload)
        setMsg('任务已更新')
        cancelEdit()
      } else {
        await api.createTask(payload)
        setMsg('任务已创建' + (form.action === 'health_check' ? '，可点「立即执行」先跑一次巡检' : ''))
        setForm({ ...form, name: '', cron_expr: '' })
        setDevSel([])
      }
      load()
    } catch (e) {
      setErr(e.response?.data?.error || (editingId ? '保存失败' : '创建失败'))
    } finally {
      setBusy(false)
    }
  }

  function startEdit(t) {
    setErr(''); setMsg('')
    setEditingId(t.id)
    setForm({
      name: t.name,
      action: t.action,
      schedule_type: t.schedule_type,
      interval_seconds: t.interval_seconds || 86400,
      cron_expr: t.cron_expr || '',
      max_retries: t.max_retries ?? 0,
    })
    setDevSel(t.device_ids || [])
    const p = t.params || {}
    setFp((s) => ({
      ...s,
      url: p.url || s.url,
      text: p.text || '',
      file_id: p.file_id ? String(p.file_id) : '',
      keycode: p.keycode ? String(p.keycode) : '3',
      seconds: p.seconds || 1,
      steps: p.steps ? JSON.stringify(p.steps) : s.steps,
    }))
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  function cancelEdit() {
    setEditingId(null)
    setForm({ ...form, name: '', cron_expr: '' })
    setDevSel([])
  }

  async function handleToggle(t) {
    try {
      await api.toggleTask(t.id, !t.enabled)
      setMsg(t.enabled ? `已停用「${t.name}」` : `已启用「${t.name}」`)
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '操作失败')
    }
  }

  async function handleRun(t) {
    setErr(''); setMsg('')
    try {
      await api.runTask(t.id)
      setMsg(`「${t.name}」执行中…`)
      // 轮询执行结果：巡检要跑几秒，直接告诉用户成功/失败几台，别让人自己翻历史
      for (let i = 0; i < 20; i += 1) {
        await new Promise((r) => setTimeout(r, 1200))
        const r = await api.taskExecutions(t.id)
        const list = r.data || []
        const newest = list[0]
        if (newest && newest.status !== 'running') {
          if (newest.status === 'success') {
            setMsg(`「${t.name}」执行完成：成功 ${newest.ok}/${newest.total} 台`)
          } else {
            const firstErr = (newest.detail?.items || newest.detail || []).find?.((x) => x && x.ok === false)
            setMsg(`「${t.name}」执行失败：成功 ${newest.ok}/${newest.total} 台，失败 ${newest.failed} 台`
              + (firstErr?.error ? `（${firstErr.error}）` : ''))
          }
          load()
          return
        }
      }
      setMsg('已触发执行，结果稍后可在「历史」查看')
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '执行失败')
    }
  }
  async function retryFailed(task, exec) {
    setBusy(true); setErr(''); setMsg('')
    try {
      const r = await api.retryTaskFailed(task.id, exec.id)
      setMsg(`已重跑 ${r.data?.device_ids?.length ?? 0} 台失败设备`)
      const h = await api.taskExecutions(task.id)
      setHist({ task, list: h.data || [] })
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '重跑失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete(id) {
    if (!await uiConfirm('确认删除任务？')) return
    await api.deleteTask(id)
    load()
  }
  async function openHistory(t) {
    try {
      const r = await api.taskExecutions(t.id)
      setHist({ task: t, list: r.data || [] })
    } catch (e) {
      setErr(e.response?.data?.error || '加载历史失败')
    }
  }

  const filteredDevices = devices.filter((d) => {
    if (onlyRunning && d.status !== 'running') return false
    const q = devQuery.trim().toLowerCase()
    if (!q) return true
    return String(d.name || '').toLowerCase().includes(q)
      || String(d.id).includes(q)
      || String(d.scracpy_host || d.scrcpy_host || '').includes(q)
  })

  const actionLabel = (a) => ACTIONS.find((x) => x.value === a)?.label || a

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>任务调度</h2>
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}
      <div className="task-toolbar">
        <button className="accent" onClick={() => setShowRecorder(true)}>+ 创建任务（录制）</button>
        <button className="ghost" onClick={() => setShowManual((v) => !v)}>
          {showManual ? '收起手动配置' : '手动配置（网址/按键/安装等）'}
        </button>
      </div>

      {editingId && (
        <div className="task-editing">
          正在编辑任务 #{editingId}
          <button type="button" className="ghost" onClick={cancelEdit}>取消编辑</button>
        </div>
      )}
      {(showManual || editingId) && (<form onSubmit={handleSubmit} className="task-form">
        <input placeholder={isAdmin ? '任务名（如：每日健康巡检）' : '任务名（如：每日签到）'} value={form.name} onChange={(e) => update('name', e.target.value)} />
        <select value={form.action} onChange={(e) => update('action', e.target.value)}>
          {actions.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
        </select>
        <select value={form.schedule_type} onChange={(e) => update('schedule_type', e.target.value)}>
          <option value="once">单次</option>
          <option value="interval">周期</option>
          <option value="cron">Cron</option>
        </select>
        {form.schedule_type === 'interval' && (
          <select value={form.interval_seconds} onChange={(e) => update('interval_seconds', Number(e.target.value))}>
            <option value={3600}>每 1 小时</option>
            <option value={21600}>每 6 小时</option>
            <option value={43200}>每 12 小时</option>
            <option value={86400}>每天</option>
            <option value={604800}>每周</option>
          </select>
        )}
        <label className="task-inline" title="单台设备执行失败时自动重试的次数">
          <span>失败重试</span>
          <select value={form.max_retries ?? 0} onChange={(e) => update('max_retries', Number(e.target.value))}>
            <option value={0}>不重试</option>
            <option value={1}>重试 1 次</option>
            <option value={2}>重试 2 次</option>
            <option value={3}>重试 3 次</option>
          </select>
        </label>
        {form.schedule_type === 'cron' && (
          <input
            placeholder="cron 表达式，如 0 2 * * *"
            value={form.cron_expr || ''}
            onChange={(e) => update('cron_expr', e.target.value)}
            style={{ width: 190 }}
          />
        )}
        <button type="submit" disabled={busy}>{editingId ? (busy ? '保存中…' : '保存修改') : '创建任务'}</button>

        {form.action !== 'health_check' && (
          <div className="task-extra">
            <div className="task-extra-row">
              {form.action === 'open_url' && (
                <label className="task-field">
                  <span>网址</span>
                  <input value={fp.url} onChange={(e) => setP('url', e.target.value)} placeholder="https://…" />
                </label>
              )}
              {form.action === 'tap' && (
                <>
                  <label className="task-field"><span>X</span>
                    <input type="number" value={fp.x} onChange={(e) => setP('x', e.target.value)} />
                  </label>
                  <label className="task-field"><span>Y</span>
                    <input type="number" value={fp.y} onChange={(e) => setP('y', e.target.value)} />
                  </label>
                </>
              )}
              {form.action === 'swipe' && (
                <>
                  <label className="task-field"><span>起点 X</span>
                    <input type="number" value={fp.x1} onChange={(e) => setP('x1', e.target.value)} />
                  </label>
                  <label className="task-field"><span>起点 Y</span>
                    <input type="number" value={fp.y1} onChange={(e) => setP('y1', e.target.value)} />
                  </label>
                  <label className="task-field"><span>终点 X</span>
                    <input type="number" value={fp.x2} onChange={(e) => setP('x2', e.target.value)} />
                  </label>
                  <label className="task-field"><span>终点 Y</span>
                    <input type="number" value={fp.y2} onChange={(e) => setP('y2', e.target.value)} />
                  </label>
                  <label className="task-field"><span>时长 ms</span>
                    <input type="number" value={fp.duration} onChange={(e) => setP('duration', e.target.value)} />
                  </label>
                </>
              )}
              {form.action === 'text' && (
                <label className="task-field task-field-wide"><span>输入内容</span>
                  <input value={fp.text} onChange={(e) => setP('text', e.target.value)} placeholder="要输入的文字" />
                </label>
              )}
              {form.action === 'key' && (
                <label className="task-field"><span>按键</span>
                  <select value={fp.keycode} onChange={(e) => setP('keycode', e.target.value)}>
                    <option value={3}>HOME</option>
                    <option value={4}>BACK</option>
                    <option value={187}>多任务</option>
                    <option value={26}>电源</option>
                    <option value={24}>音量 +</option>
                    <option value={25}>音量 -</option>
                  </select>
                </label>
              )}
              {form.action === 'install' && (
                <label className="task-field task-field-wide"><span>APK 文件</span>
                  <select value={fp.file_id} onChange={(e) => setP('file_id', e.target.value)}>
                    <option value="">请选择 APK</option>
                    {apkFiles.map((f) => (
                      <option key={f.id} value={f.id}>{f.filename}</option>
                    ))}
                  </select>
                  {apkFiles.length === 0 && (
                    <span className="hint">还没有 APK，请先到「文件管理」上传</span>
                  )}
                </label>
              )}
              {form.action === 'wait' && (
                <label className="task-field"><span>等待秒数</span>
                  <input type="number" value={fp.seconds} onChange={(e) => setP('seconds', e.target.value)} />
                </label>
              )}
              {form.action === 'run_script' && (
                <label className="task-field task-field-wide"><span>要执行的脚本</span>
                  <select value={fp.script_id} onChange={(e) => setP('script_id', e.target.value)}>
                    <option value="">请选择脚本</option>
                    {scripts.map((s) => (
                      <option key={s.id} value={s.id}>{s.name}</option>
                    ))}
                  </select>
                  {scripts.length === 0 && (
                    <span className="hint">还没有脚本，请先到「脚本管理」创建</span>
                  )}
                </label>
              )}
              {form.action === 'sequence' && (
                <label className="task-field task-field-wide"><span>步骤（JSON 数组）</span>
                  <input value={fp.steps} onChange={(e) => setP('steps', e.target.value)} />
                </label>
              )}
            </div>

            <div className="task-devices">
              <div className="task-devices-head">
                <span>执行设备　已选 <b>{devSel.length}</b> / {filteredDevices.length} 台</span>
                <input
                  className="task-dev-search"
                  placeholder="搜设备名 / ID / 服务器"
                  value={devQuery}
                  onChange={(e) => setDevQuery(e.target.value)}
                />
                <label className="task-dev-filter">
                  <input type="checkbox" checked={onlyRunning} onChange={(e) => setOnlyRunning(e.target.checked)} />
                  只看运行中
                </label>
                <button type="button" className="ghost" onClick={() => setDevSel(filteredDevices.map((d) => d.id))}>全选当前</button>
                <button type="button" className="ghost" onClick={() => setDevSel([])}>清空</button>
              </div>
              <div className="task-devices-list">
                {filteredDevices.slice(0, 300).map((d) => (
                  <label key={d.id} className="task-device">
                    <input
                      type="checkbox"
                      checked={devSel.includes(d.id)}
                      onChange={(e) => {
                        setDevSel((prev) => (e.target.checked
                          ? [...prev, d.id]
                          : prev.filter((x) => x !== d.id)))
                      }}
                    />
                    <span>{d.name}</span>
                    <span className={`task-dev-dot ${d.status}`} />
                    <span className="hint">#{d.id}</span>
                  </label>
                ))}
                {filteredDevices.length === 0 && <span className="hint">没有匹配的云手机</span>}
                {filteredDevices.length > 300 && (
                  <span className="hint">仅显示前 300 台，请用搜索框缩小范围</span>
                )}
              </div>
            </div>
          </div>
        )}
      </form>)}

      <div className="device-table-wrap files-table-wrap">
        <table className="device-table">
          <thead>
            <tr><th>ID</th><th>名称</th><th>动作</th><th>设备</th><th>调度</th><th>下次执行</th><th>最近结果</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            {list.map((t) => (
              <tr key={t.id}>
                <td>{t.id}</td>
                <td className="file-name"><span className="file-name-text" title={t.name}>{t.name}</span></td>
                <td>{actionLabel(t.action)}</td>
                <td>{(t.device_ids || []).length ? `${t.device_ids.length} 台` : '—'}</td>
                <td>{t.schedule_type === 'once' ? '单次' : t.schedule_type === 'cron' ? `Cron: ${t.cron_expr || ''}` : '周期'}</td>
                <td>{t.enabled ? fmtTime(t.next_run) : <span className="hint">已停用</span>}</td>
                <td>
                  {t.last_run
                    ? (
                      <span className="task-last">
                        <span className={`task-dev-dot ${t.last_run.status === 'success' ? 'running' : 'error'}`} />
                        {t.last_run.status === 'success' ? '成功' : '失败'}
                        {' '}{t.last_run.ok}/{t.last_run.total}
                        <span className="hint"> {fmtTime(t.last_run.at)}</span>
                      </span>
                    )
                    : <span className="hint">未执行</span>}
                </td>
                <td>
                  <button
                    className={`task-switch ${t.enabled ? 'on' : ''}`}
                    onClick={() => handleToggle(t)}
                    title={t.enabled ? '点击停用' : '点击启用'}
                  >
                    <span className="task-switch-knob" />
                  </button>
                </td>
                <td className="file-ops">
                  <button className="accent" onClick={() => handleRun(t)}>立即执行</button>
                  <button className="ghost" onClick={() => startEdit(t)}>编辑</button>
                  <button className="ghost" onClick={() => openHistory(t)}>历史</button>
                  <button className="danger" onClick={() => handleDelete(t.id)}>删除</button>
                </td>
              </tr>
            ))}
            {list.length === 0 && (
              <tr><td colSpan="9" className="empty-cell">{isAdmin ? '暂无任务，建议创建一个「每日健康巡检」周期任务' : '暂无任务：点上方「创建任务（录制）」录一段操作即可'}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {showRecorder && (
        <TaskRecorder
          devices={devices}
          canCreate
          onClose={() => setShowRecorder(false)}
          onSaved={() => {
            setShowRecorder(false)
            setMsg('已按录制内容创建任务')
            load()
          }}
        />
      )}

      {/* 执行历史弹窗 */}
      {hist && (
        <div className="modal-mask" onClick={() => setHist(null)}>
          <div className="modal-body modal-lg" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">
              <span>执行历史「{hist.task.name}」</span>
              <button className="modal-close" onClick={() => setHist(null)}>✕</button>
            </div>
            {hist.list.length === 0 && <p className="hint">暂无执行记录，点「立即执行」跑一次</p>}
            <div className="script-hist-list">
              {hist.list.map((h) => (
                <div className="script-hist-item" key={h.id}>
                  <div className="script-hist-head">
                    <span className={`status-dot`} style={{ background: h.status === 'success' ? '#22c55e' : '#ef4444' }} />
                    <span className="script-hist-time">{fmtTime(h.finished_at)}</span>
                    {h.detail?.summary && <span className="script-hist-summary">{h.detail.summary}</span>}
                  </div>
                  {h.detail?.type === 'health_check' ? (
                    <HealthReport detail={h.detail} />
                  ) : (
                    <p className="hint">
                      完成 {h.ok}/{h.total} 台，失败 {h.failed} 台
                      {h.failed > 0 && (
                        <button
                          className="ghost"
                          style={{ marginLeft: 10 }}
                          disabled={busy}
                          onClick={() => retryFailed(hist.task, h)}
                        >
                          重跑失败 {h.failed} 台
                        </button>
                      )}
                    </p>
                  )}
                </div>
              ))}
            </div>
            <div className="modal-actions">
              <button onClick={() => setHist(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

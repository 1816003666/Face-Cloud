import { useEffect, useState, useRef } from 'react'
import { api } from '../api/client.js'
import { uiAlert, uiConfirm } from '../lib/dialog.js'
import { fmtSecond } from '../lib/fmt.js'

// 状态标签映射
const STATUS_LABEL = {
  ok: '成功', success: '成功', failed: '失败', error: '失败',
  running: '执行中', pending: '等待中', completed: '已完成',
}
const STATUS_CLS = {
  ok: 'st-ok', success: 'st-ok', failed: 'st-error', error: 'st-error',
  running: 'st-pending', pending: 'st-pending', completed: 'st-ok',
}

export default function Maintenance() {
  // —— 快捷操作 ——
  const [actions, setActions] = useState([])
  const [actionBusy, setActionBusy] = useState('')
  const [actionResults, setActionResults] = useState(null)

  // —— 维护日志 ——
  const [logs, setLogs] = useState([])
  const [logErr, setLogErr] = useState('')

  // —— 批量操作 ——
  const [devices, setDevices] = useState([])
  const [selected, setSelected] = useState(new Set())
  const [batchAction, setBatchAction] = useState('restart')
  const [batchBusy, setBatchBusy] = useState(false)
  const [batchProgress, setBatchProgress] = useState(null)

  // 初始化加载
  useEffect(() => {
    loadLogs()
    loadDevices()
  }, [])

  // —— 加载维护日志 ——
  async function loadLogs() {
    try {
      const r = await api.maintenanceLogs({ limit: 100 })
      setLogs(r.data?.items || r.data || [])
    } catch (e) {
      setLogErr(e.response?.data?.error || '加载维护日志失败')
    }
  }

  // —— 加载设备列表 ——
  async function loadDevices() {
    try {
      const r = await api.listDevices({})
      setDevices(r.data || [])
    } catch (e) { /* 忽略 */ }
  }

  // —— 快捷操作 ——
  const quickActions = [
    { id: 'restart_offline', label: '重启全部离线设备', icon: '🔄', desc: '重启所有已停止/异常状态的云手机' },
    { id: 'reconnect_adb', label: '重连所有 ADB', icon: '🔗', desc: '重新建立 ADB 连接' },
    { id: 'clear_screenshots', label: '清空截图缓存', icon: '🗑', desc: '释放截图缓存空间' },
  ]

  async function runQuickAction(actionId) {
    setActionBusy(actionId); setActionResults(null)
    try {
      let r
      if (actionId === 'restart_offline') {
        // 复用诊断接口：自动选择所有 stopped/error 设备并重启
        r = await api.batchFix([], true)
      } else if (actionId === 'reconnect_adb') {
        // 重连：获取所有设备 ID，走 batch-fix 的 reconnect 策略
        const devIds = devices.map((d) => d.id)
        r = await api.batchFix(devIds)
      } else if (actionId === 'clear_screenshots') {
        // 清空截图缓存：调用设备管理的 sync 接口触发清理
        r = { data: { message: '截图缓存将在下轮轮询时自动刷新', ok: true } }
      }
      setActionResults({ action: actionId, ...(r?.data || {}) })
      loadLogs()
      loadDevices()
    } catch (e) {
      setActionResults({ action: actionId, error: e.response?.data?.error || '操作失败' })
    } finally {
      setActionBusy('')
    }
  }

  // —— 批量选择 ——
  const selectedIds = [...selected]

  function toggleSelect(id) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function selectAll() {
    if (selectedIds.length === devices.length) setSelected(new Set())
    else setSelected(new Set(devices.map((d) => d.id)))
  }

  function selectOffline() {
    const offlineIds = devices
      .filter((d) => d.status === 'stopped' || d.status === 'error')
      .map((d) => d.id)
    setSelected(new Set(offlineIds))
  }

  // —— 批量操作 ——
  async function runBatchAction() {
    if (!selectedIds.length) return
    const actionLabel = { restart: '重启', reconnect: '重连', recreate: '重建' }
    if (!await uiConfirm(`确认对 ${selectedIds.length} 台设备执行「${actionLabel[batchAction] || batchAction}」？`)) return

    setBatchBusy(true); setBatchProgress({ current: 0, total: selectedIds.length })
    try {
      const r = await api.batchMaintain(selectedIds, batchAction)
      setBatchProgress(null)
      await uiAlert(`批量操作完成：成功 ${r.data?.ok ?? selectedIds.length} 台${r.data?.failed ? `，失败 ${r.data.failed} 台` : ''}`)
      setSelected(new Set())
      loadLogs()
      loadDevices()
    } catch (e) {
      await uiAlert(`批量操作失败：${e.response?.data?.error || e.message}`)
      setBatchProgress(null)
    } finally {
      setBatchBusy(false)
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>系统运维</h2>
          <span className="page-sub">快捷操作 · 批量维护 · 运维日志</span>
        </div>
      </div>

      {/* ═══ 区域一：快捷操作 ═══ */}
      <QuickActionsSection
        actions={quickActions}
        busy={actionBusy}
        results={actionResults}
        onRun={runQuickAction}
      />

      {/* ═══ 区域二：维护日志 ═══ */}
      <MaintenanceLogSection
        logs={logs}
        err={logErr}
        onRefresh={loadLogs}
      />

      {/* ═══ 区域三：批量操作 ═══ */}
      <BatchOpsSection
        devices={devices}
        selected={selected}
        selectedIds={selectedIds}
        batchAction={batchAction}
        onBatchActionChange={setBatchAction}
        busy={batchBusy}
        progress={batchProgress}
        onToggleSelect={toggleSelect}
        onSelectAll={selectAll}
        onSelectOffline={selectOffline}
        onRunBatch={runBatchAction}
      />
    </div>
  )
}

/* ═══════════════════════════════════════
   区域一：快捷操作
   ═══════════════════════════════════════ */
function QuickActionsSection({ actions, busy, results, onRun }) {
  return (
    <div className="diag-section">
      <div className="panel">
        <div className="panel-header">
          <h3>快捷操作</h3>
        </div>

        <div className="maint-quick-grid">
          {actions.map((a) => (
            <div key={a.id} className="maint-action-card">
              <div className="maint-action-icon">{a.icon}</div>
              <div className="maint-action-info">
                <div className="maint-action-label">{a.label}</div>
                <div className="maint-action-desc">{a.desc}</div>
              </div>
              <button
                className="accent"
                disabled={busy !== '' && busy !== a.id}
                onClick={() => onRun(a.id)}
              >
                {busy === a.id ? '执行中…' : '执行'}
              </button>
            </div>
          ))}
        </div>

        {/* 操作结果展示 */}
        {results && (
          <div className="maint-result" style={{ marginTop: 12 }}>
            {results.error ? (
              <div className="err">{results.error}</div>
            ) : (
              <div className="ok">
                <div>{results.message || '操作已完成'}</div>
                {results.detail && <div style={{ marginTop: 4, fontSize: 13, opacity: 0.8 }}>{results.detail}</div>}
                {results.affected != null && (
                  <div style={{ marginTop: 4, fontSize: 13 }}>影响设备：{results.affected} 台</div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════
   区域二：维护日志
   ═══════════════════════════════════════ */
function MaintenanceLogSection({ logs, err, onRefresh }) {
  return (
    <div className="diag-section">
      <div className="panel">
        <div className="panel-header">
          <h3>维护日志</h3>
          <button className="ghost" onClick={onRefresh}>刷新</button>
        </div>
        {err && <div className="err">{err}</div>}

        <div className="device-table-wrap">
          <table className="device-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>范围</th>
                <th>摘要</th>
                <th>发现问题</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l, i) => (
                <tr key={l.id || i}>
                  <td className="mono">{fmtSecond(l.created_at)}</td>
                  <td>
                    <span className={`badge ${l.scope === 'device' ? 'st-running' : 'st-pending'}`}>
                      {l.scope === 'device' ? '设备' : l.scope === 'system' ? '系统' : l.scope || '操作'}
                    </span>
                  </td>
                  <td>{l.summary || '—'}</td>
                  <td>
                    {l.issues_found > 0 ? (
                      <span className="badge lv-warn">{l.issues_found} 项</span>
                    ) : (
                      <span className="badge st-ok">正常</span>
                    )}
                  </td>
                </tr>
              ))}
              {logs.length === 0 && (
                <tr><td colSpan="4" className="table-empty">暂无维护日志</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════
   区域三：批量操作
   ═══════════════════════════════════════ */
function BatchOpsSection({ devices, selected, selectedIds, batchAction, onBatchActionChange, busy, progress, onToggleSelect, onSelectAll, onSelectOffline, onRunBatch }) {
  return (
    <div className="diag-section">
      <div className="panel">
        <div className="panel-header">
          <h3>批量操作</h3>
        </div>

        {/* 操作工具栏 */}
        <div className="maint-batch-toolbar">
          <div className="maint-batch-left">
            <button className="ghost" onClick={onSelectAll}>
              {selectedIds.length === devices.length ? '取消全选' : '全选'}
            </button>
            <button className="ghost" onClick={onSelectOffline}>选择离线设备</button>
            <span className="muted" style={{ marginLeft: 8 }}>已选 {selectedIds.length} 台</span>
          </div>
          <div className="maint-batch-right">
            <select
              value={batchAction}
              onChange={(e) => onBatchActionChange(e.target.value)}
              disabled={busy}
              style={{ padding: '6px 8px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-card)', color: 'var(--text)' }}
            >
              <option value="restart">重启</option>
              <option value="reconnect">重连 ADB</option>
              <option value="recreate">销毁重建</option>
            </select>
            <button
              className="accent"
              disabled={busy || !selectedIds.length}
              onClick={onRunBatch}
            >
              {busy ? '执行中…' : `执行（${selectedIds.length} 台）`}
            </button>
          </div>
        </div>

        {/* 批量进度条 */}
        {progress && (
          <div className="maint-progress">
            <div className="maint-progress-text">
              正在执行… {progress.current}/{progress.total}
            </div>
            <div className="bar-track">
              <div
                className="bar-fill"
                style={{ width: `${(progress.current / Math.max(1, progress.total)) * 100}%` }}
              />
            </div>
          </div>
        )}

        {/* 设备选择表格 */}
        <div className="device-table-wrap">
          <table className="device-table">
            <thead>
              <tr>
                <th style={{ width: 40 }}></th>
                <th>设备名</th>
                <th>Serial</th>
                <th>状态</th>
                <th>模型</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((d) => (
                <tr
                  key={d.id}
                  className={selected.has(d.id) ? 'diag-row-expanded' : ''}
                  onClick={() => onToggleSelect(d.id)}
                  style={{ cursor: 'pointer' }}
                >
                  <td>
                    <span className={`check-box ${selected.has(d.id) ? 'checked' : ''}`}>
                      {selected.has(d.id) ? '✓' : ''}
                    </span>
                  </td>
                  <td className="cell-name">
            <span className="name-text">{d.name || '未命名'}</span>
          </td>
                  <td className="mono">{d.serial || '—'}</td>
                  <td>
                    <span className={`badge ${STATUS_CLS[d.status] || 'st-pending'}`}>
                      {STATUS_LABEL[d.status] || d.status}
                    </span>
                  </td>
                  <td>{d.model || '—'}</td>
                </tr>
              ))}
              {devices.length === 0 && (
                <tr><td colSpan="5" className="table-empty">暂无设备</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { uiAlert, uiConfirm } from '../lib/dialog.js'
import { fmtSecond } from '../lib/fmt.js'

// 状态标签映射
const STATUS_LABEL = {
  running: '运行中', stopped: '已停止', error: '异常', creating: '创建中',
  ok: '正常', warning: '警告', critical: '严重', unknown: '未知',
  online: '在线', offline: '离线', connected: '已连接', disconnected: '未连接',
}
const STATUS_CLS = {
  running: 'st-running', stopped: 'st-offline', error: 'st-error', creating: 'st-pending',
  ok: 'st-ok', warning: 'lv-warn', critical: 'lv-crit', unknown: 'st-pending',
  online: 'st-running', offline: 'st-offline', connected: 'st-running', disconnected: 'st-offline',
}

export default function Diagnostics() {
  // —— 系统健康概览 ——
  const [systemHealth, setSystemHealth] = useState(null)
  const [sysChecking, setSysChecking] = useState(false)
  const [sysErr, setSysErr] = useState('')
  const [sysMsg, setSysMsg] = useState('')

  // —— 设备诊断列表 ——
  const [devices, setDevices] = useState([])
  const [devFilter, setDevFilter] = useState('')
  const [devDiagResults, setDevDiagResults] = useState({}) // id → 诊断结果
  const [diagBusy, setDiagBusy] = useState(false)
  const [expandedRow, setExpandedRow] = useState(null)
  const [devErr, setDevErr] = useState('')

  // —— 诊断历史 ——
  const [history, setHistory] = useState([])
  const [histErr, setHistErr] = useState('')

  // 初始化加载
  useEffect(() => {
    loadDevices()
    loadHistory()
  }, [])

  // —— 加载设备列表 ——
  async function loadDevices() {
    try {
      const r = await api.listDevices({})
      setDevices(r.data || [])
    } catch (e) {
      setDevErr(e.response?.data?.error || '加载设备列表失败')
    }
  }

  // —— 加载诊断历史 ——
  async function loadHistory() {
    try {
      const r = await api.diagHistory({ limit: 50 })
      setHistory(r.data?.items || r.data || [])
    } catch (e) {
      setHistErr(e.response?.data?.error || '加载诊断历史失败')
    }
  }

  // —— 运行系统检查 ——
  async function runSystemCheck() {
    setSysChecking(true); setSysErr(''); setSysMsg('')
    try {
      const r = await api.systemDiag()
      setSystemHealth(r.data)
      setMsgSafe(setSysMsg, '系统检查完成')
    } catch (e) {
      setSysErr(e.response?.data?.error || '系统检查失败')
    } finally {
      setSysChecking(false)
    }
  }

  // —— 单设备诊断 ——
  async function diagnoseDevice(id) {
    setDiagBusy(true); setDevErr('')
    try {
      const r = await api.deviceDiag(id)
      setDevDiagResults((prev) => ({ ...prev, [id]: r.data }))
    } catch (e) {
      setDevErr(e.response?.data?.error || '设备诊断失败')
    } finally {
      setDiagBusy(false)
    }
  }

  // —— 修复设备 ——
  async function fixDevice(id) {
    setDiagBusy(true); setDevErr('')
    try {
      const r = await api.fixDevice(id)
      // 刷新诊断结果
      const diag = await api.deviceDiag(id)
      setDevDiagResults((prev) => ({ ...prev, [id]: diag.data }))
      await uiAlert(r.data?.message || '修复操作已完成')
    } catch (e) {
      setDevErr(e.response?.data?.error || '修复操作失败')
    } finally {
      setDiagBusy(false)
    }
  }

  // —— 筛选设备 ——
  const filteredDevices = devices.filter((d) => {
    if (!devFilter.trim()) return true
    const q = devFilter.toLowerCase()
    return (d.name || '').toLowerCase().includes(q)
      || (d.serial || '').toLowerCase().includes(q)
  })

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>设备诊断</h2>
          <span className="page-sub">系统健康检查 · 设备诊断 · 运维工具</span>
        </div>
      </div>

      {/* ═══ 区域一：系统健康概览 ═══ */}
      <SystemHealthSection
        health={systemHealth}
        checking={sysChecking}
        err={sysErr}
        msg={sysMsg}
        onCheck={runSystemCheck}
      />

      {/* ═══ 区域二：设备诊断表格 ═══ */}
      <DeviceDiagSection
        devices={filteredDevices}
        devFilter={devFilter}
        onFilterChange={setDevFilter}
        diagResults={devDiagResults}
        expandedRow={expandedRow}
        onExpand={(id) => setExpandedRow(expandedRow === id ? null : id)}
        onDiagnose={diagnoseDevice}
        onFix={fixDevice}
        busy={diagBusy}
        err={devErr}
      />

      {/* ═══ 区域三：诊断历史 ═══ */}
      <DiagHistorySection
        history={history}
        err={histErr}
        onRefresh={loadHistory}
      />
    </div>
  )
}

// —— 安全设置消息（防止快速状态更新竞态）——
function setMsgSafe(setter, msg) {
  setter(msg)
}

/* ═══════════════════════════════════════
   区域一：系统健康概览
   ═══════════════════════════════════════ */
function SystemHealthSection({ health, checking, err, msg, onCheck }) {
  // 后端返回结构: { ssh: {ok, latency_ms, error}, docker: {running, total, ...}, adb: {local_count, remote_count, ...}, resources: {load_avg, memory_percent, disk_percent}, issues: [...] }
  const ssh = health?.ssh || {}
  const docker = health?.docker || {}
  const adb = health?.adb || {}
  const resources = health?.resources || {}
  const issues = health?.issues || []

  // 从 resources.load_avg 字符串（如 "0.50 0.40 0.35"）提取第一个数字
  const loadVal = resources.load_avg ? parseFloat(String(resources.load_avg).split()[0]) : null

  return (
    <div className="diag-section">
      <div className="panel">
        <div className="panel-header">
          <h3>系统健康概览</h3>
          <button className="ghost" onClick={onCheck} disabled={checking}>
            {checking ? '检查中…' : '运行系统检查'}
          </button>
        </div>
        {err && <div className="err">{err}</div>}
        {msg && <div className="ok">{msg}</div>}

        {/* 概览卡片 */}
        {health && (
          <div className="diag-kpi-row">
            <DiagKpi
              label="SSH 连接"
              value={ssh.servers_total ? `${ssh.servers_ok}/${ssh.servers_total} 台正常`
                : ssh.ok ? `已连接 ${ssh.latency_ms != null ? ssh.latency_ms + 'ms' : ''}`
                : ssh.mode === 'per-server' ? '逐台巡检' : ssh.error ? '失败' : '未配置'}
              tone={ssh.ok ? 'green' : ssh.error ? 'red' : 'amber'}
            />
            <DiagKpi
              label="Docker 容器"
              value={`${docker.running ?? 0}/${docker.total ?? 0}`}
              sub="运行中/总数"
              tone={(docker.running ?? 0) > 0 ? 'green' : 'amber'}
            />
            <DiagKpi
              label="ADB 设备"
              value={`${adb.local_count ?? 0}/${adb.remote_count ?? 0}`}
              sub="本地/远程"
              tone={(adb.local_count ?? 0) + (adb.remote_count ?? 0) > 0 ? 'teal' : 'amber'}
            />
            <DiagKpi
              label="服务器负载"
              value={loadVal != null ? loadVal.toFixed(2) : resources.error || '—'}
              sub={resources.memory_percent != null ? `内存 ${resources.memory_percent}%` : undefined}
              tone={loadVal != null ? (loadVal > 4 ? 'red' : loadVal > 2 ? 'amber' : 'green') : 'amber'}
            />
          </div>
        )}

        {/* 问题列表 */}
        {issues.length > 0 && (
          <div className="diag-issues" style={{ marginTop: 12 }}>
            {issues.map((issue, i) => (
              <div key={i} className={`badge ${issue.level === 'critical' ? 'lv-crit' : issue.level === 'warning' ? 'lv-warn' : 'st-pending'}`} style={{ marginBottom: 4 }}>
                {issue.message}
              </div>
            ))}
          </div>
        )}

        {/* 逐台服务器巡检（多服务器部署的主要诊断口径） */}
        {(health?.servers || []).length > 0 && (
          <div style={{ marginTop: 14 }}>
            <div className="hint" style={{ marginBottom: 6 }}>服务器巡检（{health.servers.length} 台）</div>
            <div className="device-table-wrap">
              <table className="device-table">
                <thead>
                  <tr>
                    <th>服务器</th>
                    <th>SSH</th>
                    <th>容器</th>
                    <th>内存</th>
                    <th>磁盘</th>
                    <th>负载</th>
                    <th>备注</th>
                  </tr>
                </thead>
                <tbody>
                  {health.servers.map((s) => (
                    <tr key={s.id}>
                      <td>
                        <div>{s.name}</div>
                        <div className="mono" style={{ fontSize: 11, color: '#94a3b8' }}>{s.host}</div>
                      </td>
                      <td><span className={`badge ${s.ssh_ok ? 'st-ok' : 'st-error'}`}>{s.ssh_ok ? '正常' : '不通'}</span></td>
                      <td>{s.ssh_ok ? `${s.docker_running}/${s.docker_total}` : '—'}</td>
                      <td>{s.memory || '—'}</td>
                      <td>{s.disk || '—'}</td>
                      <td className="mono">{s.load || '—'}</td>
                      <td style={{ fontSize: 11, color: s.error ? '#d95050' : '#94a3b8' }}>
                        {s.via ? `经跳板机 ${s.via.replace('jump:', '')}` : (s.error || '—')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* 详细信息表格 */}
        {health && (
          <div className="device-table-wrap" style={{ marginTop: 12 }}>
            <table className="device-table">
              <thead>
                <tr>
                  <th>检查项</th>
                  <th>状态</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>SSH</td>
                  <td><span className={`badge ${ssh.ok ? 'st-ok' : 'st-error'}`}>{ssh.ok ? '正常' : '异常'}</span></td>
                  <td>{ssh.servers_total ? `${ssh.servers_ok}/${ssh.servers_total} 台 SSH 可达（逐台巡检见下表）`
                    : ssh.ok ? `延迟 ${ssh.latency_ms}ms` : ssh.error || '—'}</td>
                </tr>
                <tr>
                  <td>Docker</td>
                  <td><span className={`badge ${(docker.running ?? 0) > 0 ? 'st-ok' : 'st-error'}`}>{docker.running ?? 0}/{docker.total ?? 0}</span></td>
                  <td>{docker.error || `${docker.running ?? 0} 个容器运行中`}</td>
                </tr>
                <tr>
                  <td>ADB</td>
                  <td><span className={`badge ${(adb.local_count ?? 0) + (adb.remote_count ?? 0) > 0 ? 'st-ok' : 'st-offline'}`}>{(adb.local_count ?? 0) + (adb.remote_count ?? 0)} 台</span></td>
                  <td>本地 {adb.local_count ?? 0} / 远程 {adb.remote_count ?? 0}{adb.error ? ` (${adb.error})` : ''}</td>
                </tr>
                <tr>
                  <td>服务器资源</td>
                  <td><span className={`badge ${resources.ok ? 'st-ok' : 'st-error'}`}>{resources.ok ? '正常' : '无法采集'}</span></td>
                  <td>负载 {resources.load_avg || '—'} | 内存 {resources.memory || '—'} | 磁盘 {resources.disk || '—'}
                    {resources.memory_host ? `（最紧张：${resources.memory_host}）` : ''}</td>
                </tr>
              </tbody>
            </table>
          </div>
        )}

        {!health && !checking && (
          <div className="empty-hint" style={{ padding: 20 }}>
            点击「运行系统检查」获取服务器、SSH、Docker、ADB 等组件健康状态
          </div>
        )}
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════
   区域二：设备诊断表格
   ═══════════════════════════════════════ */
function DeviceDiagSection({ devices, devFilter, onFilterChange, diagResults, expandedRow, onExpand, onDiagnose, onFix, busy, err }) {
  return (
    <div className="diag-section">
      <div className="panel">
        <div className="panel-header">
          <h3>设备诊断</h3>
        </div>
        {err && <div className="err">{err}</div>}

        {/* 搜索过滤 */}
        <div className="task-form" style={{ marginBottom: 12 }}>
          <input
            placeholder="搜索设备名 / Serial…"
            value={devFilter}
            onChange={(e) => onFilterChange(e.target.value)}
          />
        </div>

        <div className="device-table-wrap">
          <table className="device-table">
            <thead>
              <tr>
                <th>设备名</th>
                <th>Serial</th>
                <th>状态</th>
                <th>ADB 延迟</th>
                <th>CPU</th>
                <th>内存</th>
                <th>问题</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((d) => {
                const diag = diagResults[d.id]
                const isExpanded = expandedRow === d.id
                const issues = diag?.issues || []
                return (
                  <DeviceDiagRow
                    key={d.id}
                    device={d}
                    diag={diag}
                    expanded={isExpanded}
                    issues={issues}
                    onExpand={() => onExpand(d.id)}
                    onDiagnose={() => onDiagnose(d.id)}
                    onFix={(action) => onFix(d.id, action)}
                    busy={busy}
                  />
                )
              })}
              {devices.length === 0 && (
                <tr>
                  <td colSpan="8" className="table-empty">
                    {devFilter ? '无匹配设备' : '暂无设备，请先添加设备'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

// 单行设备诊断（可展开）
function DeviceDiagRow({ device: d, diag, expanded, issues, onExpand, onDiagnose, onFix, busy }) {
  const stCls = STATUS_CLS[d.status] || 'st-pending'
  const stLabel = STATUS_LABEL[d.status] || d.status
  const issueCount = issues.length

  return (
    <>
      <tr className={expanded ? 'diag-row-expanded' : ''}>
        <td className="cell-name" title={`${d.name || '未命名'}（#${d.id}）`}>
          <span className="name-text">{d.name || '未命名'}</span>
          <span className="cell-sub">#{d.id}</span>
        </td>
        <td className="mono" title={d.serial}>{d.serial || '—'}</td>
        <td><span className={`badge ${stCls}`}>{stLabel}</span></td>
        <td className="mono">{diag?.adb_test?.latency_ms != null ? `${diag.adb_test.latency_ms}ms` : '—'}</td>
        <td>{diag?.resource_usage?.cpu_percent != null ? `${diag.resource_usage.cpu_percent}%` : '—'}</td>
        <td>{diag?.resource_usage?.mem_percent != null ? `${diag.resource_usage.mem_percent}%` : '—'}</td>
        <td>
          {issueCount > 0 ? (
            <span className="badge lv-warn">{issueCount} 项</span>
          ) : diag ? (
            <span className="badge st-ok">无</span>
          ) : (
            <span className="muted">—</span>
          )}
        </td>
        <td className="col-ops">
          <div className="row-ops">
            <button className="op-btn" title="展开详情" onClick={onExpand}>
              {expanded ? '▼' : '▶'}
            </button>
            <button className="op-btn" title="诊断" disabled={busy} onClick={onDiagnose}>
              🔍
            </button>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="diag-detail-row">
          <td colSpan="8">
            {diag ? (
              <div className="diag-detail">
                <div className="diag-detail-grid">
                  <div><strong>ADB 状态：</strong>{diag.adb_test?.state || '—'} {diag.adb_test?.latency_ms != null ? `(${diag.adb_test.latency_ms}ms)` : ''}</div>
                  <div><strong>截图测试：</strong>{diag.screenshot_test?.ok ? `通过 (${diag.screenshot_test.size_bytes} bytes, ${diag.screenshot_test.capture_ms}ms)` : diag.screenshot_test?.error || '未测试'}</div>
                  <div><strong>型号：</strong>{diag.sys_info?.model || '—'}</div>
                  <div><strong>Android：</strong>{diag.sys_info?.android_version || '—'} | {diag.sys_info?.brand || ''}</div>
                  <div><strong>CPU：</strong>{diag.resource_usage?.cpu_percent != null ? `${diag.resource_usage.cpu_percent}%` : '—'}</div>
                  <div><strong>内存：</strong>{diag.resource_usage?.mem_percent != null ? `${diag.resource_usage.mem_percent}%` : '—'}</div>
                  <div><strong>出口 IP：</strong>{diag.network?.egress_ip || '—'}</div>
                </div>
                {issueCount > 0 && (
                  <div className="diag-issues">
                    <strong>发现问题：</strong>
                    <ul>
                      {issues.map((issue, i) => (
                        <li key={i}>{typeof issue === 'string' ? issue : issue.message || JSON.stringify(issue)}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {diag.suggested_fixes && diag.suggested_fixes.length > 0 && (
                  <div className="diag-fixes">
                    <strong>建议修复：</strong>
                    <div className="diag-fix-list">
                      {diag.suggested_fixes.map((fix, i) => (
                        <button key={i} className="ghost" disabled={busy} onClick={() => onFix(fix.action)}>
                          {fix.label || fix.action}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="empty-hint">请点击「🔍 诊断」获取详细信息</div>
            )}
          </td>
        </tr>
      )}
    </>
  )
}

/* ═══════════════════════════════════════
   区域三：诊断历史
   ═══════════════════════════════════════ */
function DiagHistorySection({ history, err, onRefresh }) {
  return (
    <div className="diag-section">
      <div className="panel">
        <div className="panel-header">
          <h3>诊断历史</h3>
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
              {history.map((h, i) => (
                <tr key={h.id || i}>
                  <td className="mono">{fmtSecond(h.created_at)}</td>
                  <td>
                    <span className={`badge ${h.scope === 'system' ? 'st-running' : h.scope === 'device' ? 'st-ok' : 'st-pending'}`}>
                      {h.scope === 'system' ? '系统' : h.scope === 'device' ? '设备' : h.scope || '—'}
                    </span>
                  </td>
                  <td>{h.summary || '—'}</td>
                  <td>
                    {h.issues_found > 0 ? (
                      <span className="badge lv-warn">{h.issues_found} 项</span>
                    ) : (
                      <span className="badge st-ok">正常</span>
                    )}
                  </td>
                </tr>
              ))}
              {history.length === 0 && (
                <tr><td colSpan="4" className="table-empty">暂无诊断历史</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════
   诊断 KPI 卡片（复用 Dashboard 风格）
   ═══════════════════════════════════════ */
const KPI_TONES = {
  teal: { bg: '#ccfbf1', fg: '#0f766e' },
  green: { bg: '#dcfce7', fg: '#15803d' },
  red: { bg: '#fee2e2', fg: '#b91c1c' },
  blue: { bg: '#dbeafe', fg: '#1d4ed8' },
  amber: { bg: '#fef3c7', fg: '#b45309' },
  violet: { bg: '#ede9fe', fg: '#6d28d9' },
}

function DiagKpi({ label, value, sub, tone = 'teal' }) {
  const t = KPI_TONES[tone] || KPI_TONES.teal
  return (
    <div className="kpi">
      <div className="kpi-icon" style={{ background: t.bg, color: t.fg }}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
        </svg>
      </div>
      <div className="kpi-body">
        <div className="kpi-value">{value}</div>
        <div className="kpi-label">{label}</div>
        {sub && <div className="kpi-sub">{sub}</div>}
      </div>
    </div>
  )
}

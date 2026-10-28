import { useEffect, useState } from 'react'
import { api } from '../api/client.js'
import { uiAlert, uiConfirm } from '../lib/dialog.js'

const STATUS_LABEL = { stopped: '已停止', running: '运行中', error: '异常' }

export default function VpcLines() {
  const [items, setItems] = useState([])
  const [vpcs, setVpcs] = useState([])
  const [form, setForm] = useState({ name: '', host: '', port: '1080', username: '', password: '' })
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    const [lines, nets] = await Promise.all([api.listVpcLines(), api.adminVpcs()])
    setItems(lines.data)
    setVpcs(nets.data)
  }
  useEffect(() => { load() }, [])

  async function handleCreate(e) {
    e.preventDefault()
    setErr(''); setMsg('')
    try {
      await api.createVpcLine({ ...form, port: Number(form.port) })
      setForm({ name: '', host: '', port: '1080', username: '', password: '' })
      setMsg('线路已添加，请先测试再运行，然后到客户网络指定出口')
      load()
    } catch (ex) {
      setErr(ex.response?.data?.error || '添加失败')
    }
  }

  async function run(fn, okText) {
    setBusy(true); setErr(''); setMsg('')
    try {
      const r = await fn()
      const ip = r.data?.last_egress_ip
      setMsg(ip && (okText === '测试完成' || okText === '已运行') ? `${okText}，出口 IP ${ip}` : (r.data?.hint || okText || '完成'))
      await load()
    } catch (ex) {
      setErr(ex.response?.data?.error || '操作失败')
      load()
    } finally {
      setBusy(false)
    }
  }

  async function handleDeployDev() {
    setBusy(true); setErr(''); setMsg('')
    try {
      await api.deployVpcDevProxy()
      setMsg('本机测试代理已就绪（127.0.0.1:1080）。请点测试再运行。出口 IP 通常与直连相同。')
      await load()
    } catch (ex) {
      setErr(ex.response?.data?.error || '部署失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>出口线路</h2>
        </div>
      </div>
      <p className="hint">SOCKS5 出口，供客户 VPC 指定。一条线路可给多个客户网络使用。ADB / 投屏不走代理。安全级别仍只记录，不在这里落地。</p>
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}

      <div className="file-ops" style={{ marginBottom: 12 }}>
        <button type="button" className="ghost" disabled={busy} onClick={handleDeployDev}>部署本机测试代理</button>
      </div>

      <form onSubmit={handleCreate} className="task-form">
        <input placeholder="线路名" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input placeholder="SOCKS5 IP" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
        <input placeholder="端口" type="number" min="1" max="65535" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} />
        <input placeholder="用户名（可选）" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        <input placeholder="密码（可选）" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        <button type="submit" disabled={busy}>添加线路</button>
      </form>

      <div className="device-table-wrap files-table-wrap">
        <table className="device-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>名称</th>
              <th>SOCKS5</th>
              <th>状态</th>
              <th>出口 IP</th>
              <th>客户网络</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((x) => (
              <tr key={x.id}>
                <td>{x.id}</td>
                <td className="file-name"><span className="file-name-text">{x.name}</span></td>
                <td className="cell-mono">{x.host}:{x.port}{x.username ? ` · ${x.username}` : ''}</td>
                <td><span className={`badge st-${x.status}`}>{STATUS_LABEL[x.status] || x.status}</span></td>
                <td className="cell-mono">{x.last_egress_ip || '—'}</td>
                <td>{x.vpc_count} 个</td>
                <td className="file-ops">
                  <button className="ghost" disabled={busy} onClick={() => run(() => api.testVpcLine(x.id), '测试完成')}>测试</button>
                  {x.status === 'running'
                    ? <button className="ghost" disabled={busy} onClick={() => run(() => api.stopVpcLine(x.id), '已停止')}>停止</button>
                    : <button className="accent" disabled={busy} onClick={() => run(() => api.startVpcLine(x.id), '已运行')}>运行</button>}
                  <button className="danger" disabled={busy} onClick={async () => {
                    if (!(await uiConfirm('确认删除该线路？'))) return
                    run(() => api.deleteVpcLine(x.id), '已删除')
                  }}>删除</button>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr><td colSpan="7" className="empty-cell">暂无线路。添加 SOCKS5 后测试通过再运行。</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <h3 style={{ marginTop: 24 }}>客户网络</h3>
      <p className="hint">只有运行中的线路可以指定。客户在客户端只能看到结果，不能自己改出口。</p>
      <div className="device-table-wrap files-table-wrap">
        <table className="device-table">
          <thead>
            <tr>
              <th>客户</th>
              <th>网络</th>
              <th>网段</th>
              <th>云机</th>
              <th>出口线路</th>
            </tr>
          </thead>
          <tbody>
            {vpcs.map((v) => (
              <tr key={v.id}>
                <td>{v.owner_name || '—'}</td>
                <td>{v.name}</td>
                <td className="cell-mono">{v.cidr}</td>
                <td>{v.device_count} 台</td>
                <td>
                  <select
                    value={v.vpc_line_id || ''}
                    disabled={busy}
                    onChange={(e) => run(
                      () => api.setVpcLine(v.id, e.target.value ? Number(e.target.value) : null),
                      e.target.value ? '已指定出口' : '已解除出口',
                    )}
                  >
                    <option value="">未指定</option>
                    {items.filter((x) => x.status === 'running' || x.id === v.vpc_line_id).map((x) => (
                      <option key={x.id} value={x.id}>{x.name}（{x.last_egress_ip || x.status}）</option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
            {vpcs.length === 0 && (
              <tr><td colSpan="5" className="empty-cell">还没有客户开通网络。</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

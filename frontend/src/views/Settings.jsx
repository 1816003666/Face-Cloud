import { useEffect, useState } from 'react'
import { api } from '../api/client.js'

const FIELDS = [
  { key: 'alert_offline_seconds', label: '离线判定秒数', desc: '运行中设备超过该秒数无心跳即判定离线（默认 120s）', unit: '秒' },
  { key: 'alert_cpu_limit', label: '云手机 CPU 阈值', desc: 'CPU 使用率超过该值触发资源告警（默认 90%）', unit: '%' },
  { key: 'alert_mem_limit', label: '云手机内存阈值', desc: '内存使用率超过该值触发资源告警（默认 90%）', unit: '%' },
  { key: 'alert_webhook_url', label: '告警通知 Webhook', desc: '产生告警时推送该地址（支持飞书/企业微信机器人或自建接口），留空则不通知' },
  { key: 'screenshot_interval', label: '截图刷新间隔', desc: '设备卡片截图轮询间隔，每批 10 台错峰调度（默认 5 秒）', unit: '秒' },
]

export default function Settings() {
  const [form, setForm] = useState({})
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [saving, setSaving] = useState(false)
  const [lan, setLan] = useState(null)
  const [copied, setCopied] = useState('')

  async function load() {
    try {
      const r = await api.getSettings()
      setForm(r.data)
    } catch (e) {
      setErr(e.response?.data?.error || '加载失败')
    }
    try {
      const r = await api.getLanInfo()
      setLan(r.data)
    } catch (e) { /* 拿不到就不显示这一块 */ }
  }
  useEffect(() => { load() }, [])

  function copy(text) {
    navigator.clipboard?.writeText(text)
    setCopied(text)
    setTimeout(() => setCopied(''), 1500)
  }

  async function handleSave(e) {
    e.preventDefault()
    setSaving(true); setErr(''); setMsg('')
    try {
      const payload = {}
      for (const f of FIELDS) {
        if (f.key === 'alert_webhook_url') payload[f.key] = (form[f.key] || '').trim()
        else payload[f.key] = Number(form[f.key] || 0)
      }
      await api.updateSettings(payload)
      setMsg('设置已保存，调度器将自动按新阈值生效')
      load()
    } catch (e) {
      setErr(e.response?.data?.error || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          <h2>系统设置</h2>
        </div>
      </div>
      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}

      {lan && (lan.urls?.length > 0 || lan.all_urls?.length > 0) && (
        <div className="settings-section" style={{ marginBottom: 16 }}>
          <div className="settings-section-title">局域网访问</div>
          <p className="settings-desc" style={{ marginBottom: 10 }}>
            同一内网的其他电脑或手机，用下面的地址即可打开本平台。
            {/* 只有本机确实有与云手机同网段的地址时才这么建议；
                否则（本机与云手机跨网段）会误导用户去选一个他根本连不上的地址 */}
            {lan.prefix && lan.urls?.length && lan.urls.length < (lan.all_urls?.length || 0)
              ? <>优先用与云手机同网段（<code>{lan.prefix}x</code>）的那个；手机若连的是 WiFi，选 WiFi 网段那个。</>
              : '选与访问设备（电脑/手机）同一网段的地址；不确定就逐个试。'}
          </p>
          {(lan.urls?.length ? lan.urls : lan.all_urls).map((url) => (
            <div className="lan-row" key={url}>
              <span style={{ fontFamily: 'monospace' }}>{url}</span>
              <button type="button" className="lan-btn" onClick={() => copy(url)}>
                {copied === url ? '已复制' : '复制'}
              </button>
              <button type="button" className="lan-btn" onClick={() => window.open(url, '_blank')}>
                打开
              </button>
            </div>
          ))}
          {lan.all_urls?.length > 1 && lan.urls?.length !== lan.all_urls?.length && (
            <p className="settings-desc" style={{ marginTop: 8 }}>
              其它网段：{lan.all_urls.join('、')}
            </p>
          )}
          <p className="settings-desc" style={{ marginTop: 8 }}>{lan.firewall_hint}</p>
        </div>
      )}

      <form onSubmit={handleSave} className="settings-form">
        <div className="settings-section">
          <div className="settings-section-title">告警与巡检阈值</div>
          {FIELDS.map((f) => (
            <div className="settings-row" key={f.key}>
              <label className="settings-label">
                <span>{f.label}</span>
                {f.unit && <em>{f.unit}</em>}
              </label>
              <input
                type={f.key === 'alert_webhook_url' ? 'text' : 'number'}
                className={f.key === 'alert_webhook_url' ? 'settings-webhook' : 'settings-input'}
                placeholder={f.key === 'alert_webhook_url' ? 'https://...' : ''}
                value={form[f.key] ?? ''}
                onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
              />
              <p className="settings-desc">{f.desc}</p>
            </div>
          ))}
        </div>

        <div className="settings-actions">
          <button type="submit" disabled={saving}>{saving ? '保存中…' : '保存设置'}</button>
        </div>
      </form>
    </div>
  )
}

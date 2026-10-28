import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client.js'
import { uiAlert, uiConfirm } from '../lib/dialog.js'

const TYPE_META = {
  image: { label: '图片', cls: 'image' },
  apk:   { label: 'APK',  cls: 'apk' },
  doc:   { label: '文档', cls: 'doc' },
  other: { label: '其他', cls: 'other' },
}
const IMG_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico']
const DOC_EXT = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'md', 'csv']
const TEXT_EXT = ['txt', 'md', 'log', 'json', 'xml', 'csv', 'sh', 'py', 'js', 'ts', 'html', 'css', 'ini', 'conf', 'properties', 'cfg', 'yml', 'yaml', 'env', 'sql', 'bat', 'ps1', 'kt', 'java', 'c', 'h', 'cpp']

function fileType(name) {
  const ext = (name.split('.').pop() || '').toLowerCase()
  if (IMG_EXT.includes(ext)) return 'image'
  if (ext === 'apk') return 'apk'
  if (DOC_EXT.includes(ext)) return 'doc'
  return 'other'
}

function fmtSize(n) {
  if (n == null) return '-'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

function fmtTime(s) {
  if (!s) return '-'
  const d = new Date(s)
  return d.toLocaleString('zh-CN', { hour12: false })
}

export default function Files() {
  const [items, setItems] = useState([])
  const [devices, setDevices] = useState([])
  const [groups, setGroups] = useState([])
  const [stats, setStats] = useState({ total_files: 0, total_size: 0, by_type: {} })
  const [typeFilter, setTypeFilter] = useState('all')
  const [selection, setSelection] = useState(new Set())   // 选中的文件 id
  const [preview, setPreview] = useState(null)            // {id,filename,type,url,size}
  const [modal, setModal] = useState(null)                 // {mode:'push'|'install', ids:[...]}
  const [pushDev, setPushDev] = useState(new Set())       // 推送/安装弹窗勾选的设备
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const fileRef = useRef(null)
  // 云手机文件预览
  const [fsDevice, setFsDevice] = useState('')
  const [fsPath, setFsPath] = useState('/sdcard/')
  const [fsItems, setFsItems] = useState([])
  const [fsLoading, setFsLoading] = useState(false)
  const [fsErr, setFsErr] = useState('')
  const [fsPreview, setFsPreview] = useState(null) // {name,type,content,url}

  async function load() {
    const r = await api.listFiles({ page: 1, page_size: 200 })
    setItems(r.data.items || [])
  }
  async function loadStats() {
    try {
      const r = await api.fileStats()
      setStats(r.data)
    } catch (e) { /* 忽略 */ }
  }
  async function loadDevices() {
    const r = await api.listDevices({ page: 1, page_size: 300 })
    setDevices(r.data.items || r.data || [])
  }
  async function loadGroups() {
    try {
      const r = await api.listGroups()
      setGroups(r.data || [])
    } catch (e) { /* 忽略 */ }
  }
  useEffect(() => { load(); loadStats(); loadDevices(); loadGroups() }, [])

  // 选择云手机、打开目录、返回上级后自动加载云手机目录
  useEffect(() => {
    if (fsDevice) loadFs()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsDevice, fsPath])

  const groupMap = Object.fromEntries((groups || []).map((g) => [g.id, g.name]))
  const groupDevices = (gid) => devices.filter((d) => d.group_id === gid)

  const filtered = typeFilter === 'all' ? items : items.filter((f) => fileType(f.filename) === typeFilter)
  const selCount = selection.size
  const allChecked = filtered.length > 0 && filtered.every((f) => selection.has(f.id))

  function toggleAll() {
    const next = new Set(selection)
    if (allChecked) filtered.forEach((f) => next.delete(f.id))
    else filtered.forEach((f) => next.add(f.id))
    setSelection(next)
  }
  function toggleOne(id) {
    const next = new Set(selection)
    next.has(id) ? next.delete(id) : next.add(id)
    setSelection(next)
  }

  async function handleUpload(e) {
    e.preventDefault()
    setErr(''); setMsg('')
    const file = fileRef.current?.files?.[0]
    if (!file) return setErr('请选择文件')
    try {
      await api.uploadFile(file)
      setMsg('上传成功')
      fileRef.current.value = ''
      load(); loadStats()
    } catch (e) {
      setErr(e.response?.data?.error || '上传失败')
    }
  }

  async function handleDelete(id) {
    if (!await uiConfirm('确认删除该文件？')) return
    try {
      await api.deleteFile(id)
      load(); loadStats()
    } catch (e) {
      setErr(e.response?.data?.error || '删除失败')
    }
  }

  async function handleBatchDelete() {
    if (!selCount) return
    if (!await uiConfirm(`确认删除选中的 ${selCount} 个文件？`)) return
    setBusy(true); setErr(''); setMsg('')
    try {
      for (const id of selection) await api.deleteFile(id)
      setMsg(`已删除 ${selCount} 个文件`)
      setSelection(new Set())
      load(); loadStats()
    } catch (e) {
      setErr(e.response?.data?.error || '批量删除失败')
    } finally {
      setBusy(false)
    }
  }

  function openModal(mode, ids) {
    setPushDev(new Set())
    setModal({ mode, ids })
  }

  async function handleModalConfirm() {
    const devIds = [...pushDev]
    if (!devIds.length) return setErr('请至少选择一台设备')
    setBusy(true); setErr(''); setMsg('')
    const isInstall = modal.mode === 'install'
    let okCount = 0
    try {
      for (const fid of modal.ids) {
        const r = isInstall ? await api.installFile(fid, devIds) : await api.pushFile(fid, devIds)
        okCount += r.data.ok || 0
      }
      setMsg(`${isInstall ? '安装' : '推送'}完成：成功 ${okCount}/${devIds.length} 台`)
      setModal(null)
      setSelection(new Set())
      load()
    } catch (e) {
      setErr(e.response?.data?.error || (isInstall ? '安装失败' : '推送失败'))
    } finally {
      setBusy(false)
    }
  }

  function toggleDev(id) {
    const next = new Set(pushDev)
    next.has(id) ? next.delete(id) : next.add(id)
    setPushDev(next)
  }

  // ---- 云手机文件预览 ----
  async function loadFs() {
    if (!fsDevice) return
    setFsLoading(true); setFsErr('')
    try {
      const r = await api.deviceFs(fsDevice, fsPath)
      setFsItems(r.data.items || [])
    } catch (e) {
      setFsErr(e.response?.data?.error || '目录加载失败')
    } finally {
      setFsLoading(false)
    }
  }
  function onFsDeviceChange(e) {
    setFsDevice(e.target.value)
    setFsPath('/sdcard/')
    setFsItems([])
    setFsErr('')
  }
  function enterFsDir(p) {
    setFsPath(p.endsWith('/') ? p : p + '/')
  }
  function goFsUp() {
    const p = fsPath.replace(/\/+$/, '')
    if (!p) return setFsPath('/')
    const idx = p.lastIndexOf('/')
    setFsPath(idx <= 0 ? '/' : p.slice(0, idx + 1))
  }
  async function openFsPreview(item) {
    const ext = (item.name.split('.').pop() || '').toLowerCase()
    if (IMG_EXT.includes(ext)) {
      try {
        const r = await api.deviceFsFile(fsDevice, item.path)
        const url = URL.createObjectURL(r.data)
        setFsPreview({ name: item.name, type: 'image', url })
      } catch (e) {
        setErr('图片加载失败')
      }
    } else if (TEXT_EXT.includes(ext)) {
      try {
        const r = await api.deviceFsRead(fsDevice, item.path)
        setFsPreview({ name: item.name, type: 'text', content: r.data.content, truncated: r.data.truncated })
      } catch (e) {
        setErr('文本读取失败')
      }
    } else {
      setFsPreview({ name: item.name, type: 'other' })
    }
  }
  async function downloadFsFile(item) {
    try {
      const r = await api.deviceFsFile(fsDevice, item.path)
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = url; a.download = item.name; a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setErr('下载失败')
    }
  }
  function toggleGroup(gid) {
    const ids = groupDevices(gid).map((d) => d.id)
    if (!ids.length) return
    const next = new Set(pushDev)
    const allOn = ids.every((id) => next.has(id))
    ids.forEach((id) => (allOn ? next.delete(id) : next.add(id)))
    setPushDev(next)
  }

  async function openPreview(f) {
    const t = fileType(f.filename)
    if (t === 'image') {
      try {
        const r = await api.getFileBlob(f.id)
        const url = URL.createObjectURL(r.data)
        setPreview({ id: f.id, filename: f.filename, type: t, url, size: f.size })
      } catch (e) {
        setErr('图片加载失败')
      }
    } else {
      setPreview({ id: f.id, filename: f.filename, type: t, url: null, size: f.size })
    }
  }

  const typeTabs = [
    ['all', '全部', stats.total_files],
    ['image', '图片', stats.by_type?.image?.count || 0],
    ['apk', 'APK', stats.by_type?.apk?.count || 0],
    ['doc', '文档', stats.by_type?.doc?.count || 0],
    ['other', '其他', stats.by_type?.other?.count || 0],
  ]
  const totalSize = stats.total_size || 0
  const typePct = (t) => (totalSize ? Math.round(((stats.by_type?.[t]?.size || 0) / totalSize) * 100) : 0)
  const colorOf = { image: '#3b82f6', apk: '#22c55e', doc: '#f59e0b', other: '#94a3b8' }

  return (
    <div>
      <div className="page-header files-head">
        <div className="page-title">
          <h2>文件管理</h2>
        </div>
        <form onSubmit={handleUpload} className="inline">
          <input type="file" ref={fileRef} />
          <button type="submit" disabled={busy}>上传文件</button>
        </form>
      </div>

      {/* 存储概览 */}
      <div className="files-stats">
        <div className="fs-block">
          <span className="fs-num">{stats.total_files}</span>
          <span className="fs-label">文件总数</span>
        </div>
        <div className="fs-block">
          <span className="fs-num">{fmtSize(totalSize)}</span>
          <span className="fs-label">总大小</span>
        </div>
        <div className="fs-block fs-legend">
          {['image', 'apk', 'doc', 'other'].map((t) => (
            <span key={t} className="fs-legend-item">
              <i style={{ background: colorOf[t] }} />{TYPE_META[t].label} {stats.by_type?.[t]?.count || 0} 个 · {fmtSize(stats.by_type?.[t]?.size || 0)}（{typePct(t)}%）
            </span>
          ))}
        </div>
        <div className="fs-bar">
          {['image', 'apk', 'doc', 'other'].map((t) => (
            <i key={t} style={{ width: `${typePct(t)}%`, background: colorOf[t] }} />
          ))}
        </div>
      </div>

      {err && <div className="err">{err}</div>}
      {msg && <div className="ok">{msg}</div>}

      {/* 类型筛选 + 批量操作 */}
      <div className="files-toolbar">
        <div className="layout-switch" style={{ flexWrap: 'wrap' }}>
          {typeTabs.map(([k, label, n]) => (
            <button key={k} className={typeFilter === k ? 'active' : ''} onClick={() => setTypeFilter(k)}>
              {label}{n > 0 ? ` (${n})` : ''}
            </button>
          ))}
        </div>
        {selCount > 0 && (
          <div className="files-batch">
            <span className="batch-count">已选 {selCount} 个</span>
            <button onClick={() => openModal('push', [...selection])} disabled={busy}>批量推送</button>
            {[...selection].some((id) => items.find((f) => f.id === id)?.filename.toLowerCase().endsWith('.apk')) && (
              <button className="accent" onClick={() => openModal('install', [...selection].filter((id) => items.find((f) => f.id === id)?.filename.toLowerCase().endsWith('.apk')))} disabled={busy}>批量安装</button>
            )}
            <button className="danger" onClick={handleBatchDelete} disabled={busy}>批量删除</button>
            <button className="ghost" onClick={() => setSelection(new Set())} disabled={busy}>取消</button>
          </div>
        )}
      </div>

      {/* 文件列表 */}
      <div className="device-table-wrap files-table-wrap">
        <table className="device-table">
          <thead>
            <tr>
              <th className="col-check"><input type="checkbox" checked={allChecked} onChange={toggleAll} /></th>
              <th>文件名</th>
              <th>类型</th>
              <th>大小</th>
              <th>上传时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((f) => {
              const t = fileType(f.filename)
              const meta = TYPE_META[t]
              return (
                <tr key={f.id} className={selection.has(f.id) ? 'row-selected' : ''}>
                  <td className="col-check">
                    <input type="checkbox" checked={selection.has(f.id)} onChange={() => toggleOne(f.id)} />
                  </td>
                  <td className="file-name">
                    <span className={`file-type-badge ${meta.cls}`}>{meta.label}</span>
                    <span className="file-name-text" title={f.filename}>{f.filename}</span>
                  </td>
                  <td><span className={`file-type-tag t-${meta.cls}`}>{meta.label}</span></td>
                  <td>{fmtSize(f.size)}</td>
                  <td>{fmtTime(f.created_at)}</td>
                  <td className="file-ops">
                    <button className="ghost" onClick={() => openPreview(f)}>预览</button>
                    <button className="ghost" onClick={() => api.downloadFile(f.id, f.filename)}>下载</button>
                    <button className="ghost" onClick={() => openModal('push', [f.id])}>推送</button>
                    {t === 'apk' && (
                      <button className="accent" onClick={() => openModal('install', [f.id])}>安装</button>
                    )}
                    <button className="danger" onClick={() => handleDelete(f.id)}>删除</button>
                  </td>
                </tr>
              )
            })}
            {filtered.length === 0 && (
              <tr><td colSpan="6" className="empty-cell">暂无{typeFilter !== 'all' ? TYPE_META[typeFilter].label : ''}文件，点击上方上传</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 云手机文件预览 */}
      <div className="files-devfs" style={{ marginTop: 18 }}>
        <div className="files-head" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>云手机文件预览</h3>
          <select value={fsDevice} onChange={onFsDeviceChange} style={{ padding: '6px 10px', borderRadius: 6, border: '1px solid #d0d7de', background: '#fff' }}>
            <option value="">请选择云手机（在线）</option>
            {devices.filter((d) => d.status === 'running').map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>
          <button className="ghost" onClick={loadFs} disabled={!fsDevice || fsLoading}>刷新</button>
        </div>
        {fsDevice && (
          <div className="device-table-wrap files-table-wrap" style={{ marginTop: 6 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <button className="ghost" onClick={goFsUp} disabled={!fsPath || fsPath === '/'}>↑ 上级</button>
              <code style={{ background: '#f1f3f5', padding: '3px 8px', borderRadius: 4, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{fsPath}</code>
              {fsLoading && <span className="hint">加载中…</span>}
              {fsErr && <span className="err" style={{ margin: 0 }}>{fsErr}</span>}
            </div>
            <table className="device-table">
              <thead>
                <tr><th>名称</th><th>类型</th><th>大小</th><th>权限</th><th>修改时间</th><th>操作</th></tr>
              </thead>
              <tbody>
                {fsItems.map((item) => (
                  <tr key={item.path}>
                    <td className="file-name">
                      <span className={`file-type-badge ${item.is_dir ? 'doc' : 'other'}`}>{item.is_dir ? '目录' : '文件'}</span>
                      <span className="file-name-text" title={item.name}>{item.is_dir ? '📁 ' : ''}{item.name}</span>
                    </td>
                    <td>{item.is_dir ? '目录' : '文件'}</td>
                    <td>{item.is_dir ? '-' : fmtSize(item.size)}</td>
                    <td style={{ color: '#64748b', fontSize: 12 }}>{item.perms}</td>
                    <td>{item.mtime}</td>
                    <td className="file-ops">
                      {item.is_dir ? (
                        <button className="ghost" onClick={() => enterFsDir(item.path)}>打开</button>
                      ) : (
                        <>
                          <button className="ghost" onClick={() => openFsPreview(item)}>预览</button>
                          <button className="ghost" onClick={() => downloadFsFile(item)}>下载</button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
                {!fsLoading && fsItems.length === 0 && <tr><td colSpan="6" className="empty-cell">目录为空</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 推送/安装弹窗 */}
      {modal && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal-body files-modal" onClick={(e) => e.stopPropagation()}>
            <h3>{modal.mode === 'install' ? `安装到设备（${modal.ids.length} 个 APK）` : `推送到设备（${modal.ids.length} 个文件）`}</h3>
            <p className="hint">已选 {pushDev.size} 台设备，可先按分组快速选择，再单独微调</p>

            {groups.length > 0 && (
              <>
                <div className="push-sec-title">按分组选择</div>
                <div className="dev-pick push-group-pick">
                  {groups.map((g) => {
                    const ids = groupDevices(g.id).map((d) => d.id)
                    const on = ids.length > 0 && ids.every((id) => pushDev.has(id))
                    return (
                      <label key={g.id} className="chip">
                        <input type="checkbox" checked={on} onChange={() => toggleGroup(g.id)} />
                        <b>{g.name}</b>
                        <span className="chip-sub">{ids.length} 台</span>
                      </label>
                    )
                  })}
                </div>
              </>
            )}

            <div className="push-sec-title">单独选择设备</div>
            <div className="dev-pick push-dev-pick">
              {devices.length === 0 && <p className="hint">暂无设备，请先在设备管理创建</p>}
              {devices.map((d) => (
                <label key={d.id} className="chip">
                  <input
                    type="checkbox"
                    checked={pushDev.has(d.id)}
                    onChange={() => toggleDev(d.id)}
                  />
                  {d.name}
                  <span className="chip-sub">{groupMap[d.group_id] || '未分组'}</span>
                </label>
              ))}
            </div>
            <div className="modal-actions">
              <button className="ghost" onClick={() => setModal(null)} disabled={busy}>取消</button>
              <button onClick={handleModalConfirm} disabled={busy}>
                {modal.mode === 'install' ? '安装' : '推送'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 预览弹窗 */}
      {preview && (
        <div className="modal-mask" onClick={() => { setPreview(null); if (preview.url) URL.revokeObjectURL(preview.url) }}>
          <div className="modal-body files-preview" onClick={(e) => e.stopPropagation()}>
            <h3>{preview.filename}</h3>
            {preview.type === 'image' && preview.url ? (
              <div className="preview-img-wrap"><img src={preview.url} alt={preview.filename} /></div>
            ) : (
              <div className="preview-meta">
                <span className={`file-type-badge ${TYPE_META[preview.type].cls}`}>{TYPE_META[preview.type].label}</span>
                <p>该类型暂不支持在线预览，可点击下载查看。</p>
                <p>大小：{fmtSize(preview.size)}</p>
              </div>
            )}
            <div className="modal-actions">
              <button className="ghost" onClick={() => { setPreview(null); if (preview.url) URL.revokeObjectURL(preview.url) }}>关闭</button>
              <button onClick={() => api.downloadFile(preview.id, preview.filename)}>下载</button>
            </div>
          </div>
        </div>
      )}

      {/* 云手机文件预览弹窗 */}
      {fsPreview && (
        <div className="modal-mask" onClick={() => { setFsPreview(null); if (fsPreview.url) URL.revokeObjectURL(fsPreview.url) }}>
          <div className="modal-body files-preview" onClick={(e) => e.stopPropagation()}>
            <h3>{fsPreview.name}</h3>
            {fsPreview.type === 'image' && fsPreview.url ? (
              <div className="preview-img-wrap"><img src={fsPreview.url} alt={fsPreview.name} /></div>
            ) : fsPreview.type === 'text' ? (
              <div className="devfs-text" style={{ maxHeight: '60vh', overflow: 'auto', background: '#0f172a', color: '#e2e8f0', padding: 12, borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all', fontFamily: 'Consolas, Menlo, monospace', fontSize: 13 }}>
                {fsPreview.content || '(空文件)'}
                {fsPreview.truncated && <p className="hint" style={{ color: '#94a3b8' }}>（内容较大，仅显示前 150KB）</p>}
              </div>
            ) : (
              <div className="preview-meta">
                <p>该类型不支持在线预览，可点击下载查看。</p>
              </div>
            )}
            <div className="modal-actions">
              <button className="ghost" onClick={() => { setFsPreview(null); if (fsPreview.url) URL.revokeObjectURL(fsPreview.url) }}>关闭</button>
              <button onClick={() => { if (fsPreview.url) { const a = document.createElement('a'); a.href = fsPreview.url; a.download = fsPreview.name; a.click(); URL.revokeObjectURL(fsPreview.url); setFsPreview(null) } }} disabled={!fsPreview.url}>下载</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

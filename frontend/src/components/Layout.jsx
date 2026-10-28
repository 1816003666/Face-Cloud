import { Suspense, useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth, useCan } from '../store/auth.jsx'
import KeepAliveOutlet from './KeepAliveOutlet.jsx'
import {
  prefetchAllRoutes,
  prefetchRoute,
  prefetchRouteData,
} from '../lib/routePrefetch.js'

const ROLE_RANK = { viewer: 1, operator: 2, admin: 3, superadmin: 4 }
const isElectron = typeof window !== 'undefined' &&
  (!!window.electronAPI || /Electron/i.test(navigator.userAgent || ''))

// 极简线性图标（16x16，stroke）
const Icon = ({ d }) => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
    <path d={d} />
  </svg>
)

const NAV_ICONS = {
  dashboard: 'M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z',
  devices: 'M5 2h14a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm7 18h.01',
  tasks: 'M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11',
  files: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8',
  scripts: 'M16 18l6-6-6-6 M8 6l-6 6 6 6',
  groups: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M23 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75',
  users: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
  audit: 'M12 8v4l3 3 M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z',
  alerts: 'M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z M12 9v4 M12 17h.01',
  billing: 'M20 12V8H6a2 2 0 0 1 0-4h12v4 M4 6v12a2 2 0 0 0 2 2h14v-4h-10a2 2 0 0 1 0-4h10',
  // 下面三个之前和别的菜单复用同一图标，导致侧边栏看起来"图标重复"
  lines: 'M6 3v10a4 4 0 0 0 4 4h8 M18 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4z M6 5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  account: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M20 8v6 M17 11h6',
  diag: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z M12 8v5 M12 16h.01',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z',
  org: 'M9 3h6v4H9z M3 17h6v4H3z M15 17h6v4h-6z M12 7v4 M6 17v-2h12v2',
  companies: 'M3 21h18 M6 21V7l6-4 6 4v14 M9 9h.01 M15 9h.01 M9 13h.01 M15 13h.01 M9 17h.01 M15 17h.01',
}

export default function Layout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const isAdmin = ROLE_RANK[user?.role] >= ROLE_RANK.admin
  // 平台管理员 = 管理员且不属于任何企业（可跨企业）
  const isPlatform = isAdmin && !user?.enterprise

  // 空闲预热：只预取"最常用的几个 + 这个账号进得去的"，其余靠鼠标悬停时再拉。
  // 以前是全量 14 个，首屏会顺带下 450KB 用不到的页面。
  useEffect(() => {
    const common = ['/dashboard', '/devices', '/tasks']
    const adminExtra = ['/files', '/scripts', '/groups']
    prefetchAllRoutes(isAdmin ? [...common, ...adminExtra] : common)
  }, [isAdmin])

  function handleLogout() {
    logout()
    navigate('/login')
  }

  function warmRoute(path, withData = false) {
    prefetchRoute(path).catch(() => {})
    if (withData) prefetchRouteData(path, { delay: 160 })
  }

  const navGroups = [
    {
      // 个人用户和企业用户都有的基础功能（不按权限隐藏，数据范围在接口层控制）
      title: '设备运营',
      items: [
        { to: '/dashboard', label: isPlatform ? '数据看板' : '我的套餐', icon: NAV_ICONS.dashboard },
        { to: '/devices', label: '设备管理', icon: NAV_ICONS.devices },
        { to: '/tasks', label: '任务调度', icon: NAV_ICONS.tasks },
      ],
    },
    {
      title: '资源管理',
      items: [
        { to: '/files', label: '文件管理', icon: NAV_ICONS.files },
        { to: '/scripts', label: '脚本管理', icon: NAV_ICONS.scripts },
        { to: '/groups', label: '分组管理', icon: NAV_ICONS.groups },
      ],
    },
    {
      // 企业用户比个人用户多的就是这一块：部门与角色
      title: '我的组织',
      admin: true,
      enterpriseOnly: true,
      items: [
        { to: '/org', label: '部门与角色', icon: NAV_ICONS.org },
      ],
    },
    {
      // 平台账号专属：跨企业/全局管理能力（企业管理员看不到，个人用户也看不到）
      title: '企业管理',
      admin: true,
      platformOnly: true,
      items: [
        { to: '/users', label: '用户管理', icon: NAV_ICONS.account },
        { to: '/enterprises', label: '企业列表', icon: NAV_ICONS.companies },
        { to: '/billing', label: '计费管理', icon: NAV_ICONS.billing },
      ],
    },
    {
      // 平台侧：告警、审计、线路、系统设置都是运维视角，企业管理员看不到
      title: '系统管理',
      admin: true,
      items: [
        ...(isPlatform ? [
          { to: '/alerts', label: '告警中心', icon: NAV_ICONS.alerts },
          { to: '/audit', label: '审计日志', icon: NAV_ICONS.audit },
          { to: '/vpc-lines', label: '出口线路', icon: NAV_ICONS.lines },
          { to: '/settings', label: '系统设置', icon: NAV_ICONS.settings },
        { to: '/diagnostics', label: '诊断修复', icon: NAV_ICONS.diag },
        ] : []),
      ],
    },
  ]

  // 没有权限的整组直接过滤掉，不留空壳（否则侧边栏会出现空行）
  const can = useCan()
  const visibleGroups = navGroups
    .map((g) => ({ ...g, items: (g.items || []).filter((it) => can(it.perm)) }))
    .filter((g) => {
      if (g.admin && !isAdmin) return false
      if (g.enterpriseOnly && !user?.enterprise) return false
      if (g.platformOnly && !isPlatform) return false
      if (g.roles && !g.roles.includes(user?.role)) return false
      return g.items.length > 0
    })

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="logo">
          <span className="logo-mark" />
          <span>薪约云手机</span>
        </div>
        <nav>
          {visibleGroups.map((group) => (
            <div className="nav-group" key={group.title}>
              <div className="nav-group-title">{group.title}</div>
              {group.items.map((item) => (
                (() => {
                  const active = location.pathname === item.to
                    || (item.to !== '/dashboard' && location.pathname.startsWith(item.to + '/'))
                  return (
                    <button
                      key={item.to}
                      type="button"
                      aria-current={active ? 'page' : undefined}
                      onMouseEnter={() => warmRoute(item.to, true)}
                      onFocus={() => warmRoute(item.to, true)}
                      onPointerDown={() => prefetchRouteData(item.to)}
                      onClick={(e) => {
                        navigate(item.to)
                        // 鼠标点击后主动失焦：否则 :focus 会让窄屏侧边栏一直保持展开
                        if (e.detail > 0) e.currentTarget.blur()
                      }}
                      className={`nav-item${active ? ' active' : ''}`}>
                      <Icon d={item.icon} />
                      <span>{item.label}</span>
                    </button>
                  )
                })()
              ))}
            </div>
          ))}
        </nav>
        <div className="user-box">
          <div className="user-chip">
            <span className="user-avatar">{(user?.display_name || user?.username || '?').slice(0, 1).toUpperCase()}</span>
            <span className="user-meta">
              <span className="user-name">{user?.display_name || user?.username}</span>
              <span className="user-role">{user?.role}</span>
            </span>
          </div>
          {!isElectron && (
            <a
              className="client-download-btn"
              href="/downloads/xinyue-cloud-phone-setup.exe"
              download
              title="选择客户端下载平台"
              aria-label="选择客户端下载平台"
            >
              <span className="client-download-icon" aria-hidden="true">
                <Icon d="M12 3v12m0 0 4-4m-4 4-4-4M5 19h14" />
              </span>
              <span className="client-download-copy">
                <strong>下载客户端</strong>
                <small>手机与电脑端</small>
              </span>
              <span className="client-download-arrow" aria-hidden="true">›</span>
            </a>
          )}
          <button className="logout-btn" onClick={handleLogout}>
            <Icon d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4 M16 17l5-5-5-5 M21 12H9" />
            <span className="btn-text">退出登录</span>
          </button>
        </div>
      </aside>
      <main className="content">
        <Suspense fallback={(
          <div className="content-loading" role="status" aria-live="polite">
            <span className="content-loading-bar" aria-hidden="true" />
            <span>正在加载页面…</span>
          </div>
        )}>
          <KeepAliveOutlet />
        </Suspense>
      </main>
    </div>
  )
}

import { Navigate } from 'react-router-dom'
import { useAuth } from '../store/auth.jsx'

// 未登录 → 跳登录页；登录后回跳原目标
export default function ProtectedRoute({ children }) {
  const { isAuthed, ready } = useAuth()
  if (!ready) return <div className="loading">加载中…</div>
  if (!isAuthed) return <Navigate to="/login" replace />
  return children
}

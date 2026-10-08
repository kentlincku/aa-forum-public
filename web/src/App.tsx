import { useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { token } from './api'
import Shell from './components/Shell'
import Login from './pages/Login'
import Rooms from './pages/Rooms'
import Dashboard from './pages/Dashboard'
import Agents from './pages/Agents'
import Skills from './pages/Skills'
import Files from './pages/Files'

export default function App() {
  const [signedIn, setSignedIn] = useState(() => !!token.get())
  useEffect(() => {
    const out = () => setSignedIn(false)
    window.addEventListener('aaf-signed-out', out)
    return () => window.removeEventListener('aaf-signed-out', out)
  }, [])
  if (!signedIn) return <Login onSignedIn={() => setSignedIn(true)} />
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="rooms" element={<Rooms />} />
        <Route path="rooms/:roomId" element={<Rooms />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="agents" element={<Agents />} />
        <Route path="agents/:roleId" element={<Agents />} />
        <Route path="skills" element={<Skills />} />
        <Route path="skills/:skillId" element={<Skills />} />
        <Route path="files" element={<Files />} />
        <Route path="*" element={<Navigate to="/rooms" replace />} />
      </Route>
    </Routes>
  )
}

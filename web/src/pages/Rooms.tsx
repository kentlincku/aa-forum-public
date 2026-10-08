import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { listAgents, listRooms, type Agent, type Room } from '../api'
import { useI18n } from '../i18n'
import RoomList from './rooms/RoomList'
import Thread from './rooms/Thread'
import { usePoll } from './rooms/shared'

export default function Rooms() {
  const { t } = useI18n()
  const { roomId } = useParams()
  const nav = useNavigate()
  const [rooms, setRooms] = useState<Room[] | null>(null)
  const [agents, setAgents] = useState<Agent[]>([])
  const [showMembers, setShowMembers] = useState(() => localStorage.getItem('aaf-members') !== 'off')
  const current = roomId ? Number(roomId) : null

  const refreshRooms = useCallback(() => { listRooms().then(setRooms).catch(() => {}) }, [])
  usePoll(refreshRooms, 3000, [])
  usePoll(() => { listAgents().then(setAgents).catch(() => {}) }, 9000, [])

  const sorted = useMemo(() => [...(rooms ?? [])].sort((a, b) => (b.pinned ?? 0) - (a.pinned ?? 0) || b.id - a.id), [rooms])
  const firstActive = sorted.find(r => r.state === 'active' || r.state === 'frozen')
  useEffect(() => { if (!current && firstActive) nav(`/rooms/${firstActive.id}`, { replace: true }) }, [current, firstActive, nav])
  const room = sorted.find(r => r.id === current) ?? null

  return (
    <>
      <RoomList rooms={rooms && sorted} agents={agents} onChanged={refreshRooms} />
      {room
        ? <Thread key={room.id} room={room} agents={agents} showMembers={showMembers} onRoomChanged={refreshRooms}
            toggleMembers={() => setShowMembers(v => { localStorage.setItem('aaf-members', v ? 'off' : 'on'); return !v })} />
        : <div className="grid flex-1 place-items-center text-mute">{rooms === null ? t('common.loading') : t('rooms.pick')}</div>}
    </>
  )
}

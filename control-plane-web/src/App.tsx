import { useEffect, useState } from 'react'

const api = '/api/v1/control-plane/dashboard'

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section style={{padding:20,borderRadius:16,background:'#111827',color:'#fff',marginBottom:16}}>
      <h2>{title}</h2>
      {children}
    </section>
  )
}

export default function App() {
  const [data, setData] = useState<any>(null)

  useEffect(() => {
    fetch(api).then((r)=>r.json()).then(setData).catch(()=>setData({error:'Gateway unavailable'}))
  }, [])

  if (!data) return <main>Loading MOSS Control Plane...</main>

  return (
    <main style={{fontFamily:'system-ui',padding:24,background:'#030712',minHeight:'100vh'}}>
      <h1 style={{color:'#fff'}}>MOSS Control Plane</h1>
      <Card title="Devices">
        <p>{data.devices?.count ?? 0} connected</p>
        <pre>{JSON.stringify(data.devices?.items ?? [], null, 2)}</pre>
      </Card>
      <Card title="Mission Center">
        <pre>{JSON.stringify(data.missions, null, 2)}</pre>
      </Card>
      <Card title="Planner / Memory / Integration">
        <pre>{JSON.stringify({planner:data.planner,memory:data.memory,integrations:data.integrations}, null, 2)}</pre>
      </Card>
      <Card title="Events">
        <pre>{JSON.stringify(data.events, null, 2)}</pre>
      </Card>
    </main>
  )
}

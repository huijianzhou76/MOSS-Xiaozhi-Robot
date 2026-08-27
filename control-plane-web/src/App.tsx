import { useEffect, useState } from 'react'

const api = '/api/v1/control-plane/dashboard'

export default function App() {
  const [data, setData] = useState<any>(null)

  useEffect(() => {
    fetch(api)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData({error: 'Gateway unavailable'}))
  }, [])

  if (!data) return <main>Loading MOSS Control Plane...</main>

  return (
    <main style={{fontFamily: 'system-ui', padding: 24}}>
      <h1>MOSS Control Plane</h1>
      <section>
        <h2>Devices</h2>
        <p>{data.devices?.count ?? 0} connected</p>
      </section>
      <section>
        <h2>Mission</h2>
        <pre>{JSON.stringify(data.missions, null, 2)}</pre>
      </section>
      <section>
        <h2>System</h2>
        <pre>{JSON.stringify(data.integrations, null, 2)}</pre>
      </section>
    </main>
  )
}

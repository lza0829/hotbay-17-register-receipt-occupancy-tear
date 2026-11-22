import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Vaults() {
  const [rows, setRows] = useState([])
  useEffect(() => { api.vaults().then(setRows) }, [])
  return (
    <div className="page">
      <h1>片库间</h1>
      <p className="lede">片库间只承载分区容量与归属，不是文件夹树。在册数与登记闸口同一占用谓词。</p>
      <div className="card-grid">
        {rows.map(v => (
          <article className="card" key={v.id}>
            <h3>{v.name}</h3>
            <p>额定分区 {v.capacity}</p>
            <p>在册 {v.在册分区数} · 剩余可登记 {v.剩余可登记}</p>
            <p>
              {!v.active
                ? <span className="pill bad">停用</span>
                : v.是否满容
                  ? <span className="pill bad">满容</span>
                  : <span className="pill ok">可登记</span>}
            </p>
          </article>
        ))}
      </div>
    </div>
  )
}

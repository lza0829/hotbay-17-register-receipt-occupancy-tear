import { useEffect, useState } from 'react'
import { api } from '../api'

const today = () => new Date().toISOString().slice(0, 10)

export default function Partitions() {
  const [rows, setRows] = useState([])
  const [vaults, setVaults] = useState([])
  const [audits, setAudits] = useState([])
  const [receipt, setReceipt] = useState(null)
  const [msg, setMsg] = useState('')
  const [errMsg, setErrMsg] = useState('')
  const [form, setForm] = useState({
    part_key: '', title: '', release_day: today(), vault_id: 1, tier: '热档', object_prefix: 'reel/新片/',
    clip_key: '', clip_bytes: 5000000, clip_note: '正片段',
  })
  const reload = () => {
    api.partitions().then(setRows)
    api.registerAudits().then(setAudits)
    api.vaults().then(setVaults)
  }
  useEffect(() => { reload() }, [])
  async function submit(e) {
    e.preventDefault()
    setMsg('')
    setErrMsg('')
    setReceipt(null)
    try {
      const r = await api.createPartition({
        part_key: form.part_key,
        title: form.title,
        release_day: form.release_day,
        vault_id: Number(form.vault_id),
        tier: form.tier,
        object_prefix: form.object_prefix,
        clips: [{ object_key: form.clip_key || `${form.object_prefix}seg-01.bin`, bytes: Number(form.clip_bytes), note: form.clip_note }],
      })
      // 成功灯必须与占用证据同刷：证据不可重放则不得显示成功。
      if (r.证据 && !r.证据.可重放) {
        setErrMsg(`登记未成立：${r.证据.缺口}`)
        reload()
        return
      }
      setReceipt(r)
      setMsg(`登记成功 · ${r.vault_name} 在册 ${r.占用后}/${r.额定}`)
      reload()
    } catch (err) {
      setErrMsg(err.message)
      reload()
    }
  }
  const vault = vaults.find(v => Number(v.id) === Number(form.vault_id))
  return (
    <div className="page">
      <h1>分区册</h1>
      <form className="panel form" onSubmit={submit}>
        <h2>热档登记</h2>
        <label>分区键<input value={form.part_key} onChange={e => setForm({ ...form, part_key: e.target.value })} required /></label>
        <label>片名<input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} required /></label>
        <label>发行日<input type="date" value={form.release_day} onChange={e => setForm({ ...form, release_day: e.target.value })} /></label>
        <label>片库间<select value={form.vault_id} onChange={e => setForm({ ...form, vault_id: e.target.value })}>
          {vaults.map(v => <option key={v.id} value={v.id}>{v.name}（在册{v.在册分区数}/额定{v.capacity}）</option>)}
        </select></label>
        {vault?.是否满容 && <p className="err">该片库间已满容，登记将被整笔拒绝</p>}
        <label>温层<select value={form.tier} onChange={e => setForm({ ...form, tier: e.target.value })}>
          <option>热档</option><option>温档</option><option>冷档</option>
        </select></label>
        <label>对象前缀<input value={form.object_prefix} onChange={e => setForm({ ...form, object_prefix: e.target.value })} /></label>
        <label>素材对象键<input value={form.clip_key} onChange={e => setForm({ ...form, clip_key: e.target.value })} placeholder="可空则自动" /></label>
        <label>字节数<input type="number" value={form.clip_bytes} onChange={e => setForm({ ...form, clip_bytes: e.target.value })} /></label>
        <button type="submit">登记分区</button>
        {msg && <p className="msg">{msg}</p>}
        {errMsg && <p className="err">{errMsg}</p>}
      </form>
      {receipt && (
        <section className="panel">
          <h2>登记回执</h2>
          <ul className="plain">
            <li>分区键 {receipt.part_key} · 温层 {receipt.tier} · 片库间 {receipt.vault_name}</li>
            <li>占用 {receipt.占用前} → {receipt.占用后} / 额定 {receipt.额定} · 审计号 {receipt.audit_id} · {receipt.created_at}</li>
          </ul>
        </section>
      )}
      <div className="table-wrap">
        <table>
          <thead><tr><th>分区键</th><th>片名</th><th>温层</th><th>片库间</th><th>段数</th><th>状态</th><th>证据</th></tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id}>
                <td>{r.part_key}</td><td>{r.title}</td><td>{r.tier}</td><td>{r.vault_name}</td><td>{r.clip_count}</td><td>{r.status}</td>
                <td>{r.证据可重放
                  ? <span className="pill ok">四侧齐</span>
                  : <span className="pill bad" title={r.证据缺口}>{r.证据缺口}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>登记审计流水</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>编号</th><th>分区键</th><th>片库间</th><th>温层</th><th>结果</th><th>占用(前→后/额定)</th><th>原因</th><th>时间</th></tr></thead>
          <tbody>
            {audits.map(a => (
              <tr key={a.id}>
                <td>{a.id}</td>
                <td>{a.part_key}</td>
                <td>{a.vault_name ?? '—'}</td>
                <td>{a.tier}</td>
                <td>{a.result === '成功'
                  ? <span className="pill ok">成功</span>
                  : a.result === '回退'
                    ? <span className="pill bad" title={a.缺口}>回退</span>
                    : <span className="pill bad">失败</span>}</td>
                <td>{a.used_before == null ? '—' : `${a.used_before}→${a.used_after}/${a.capacity}`}</td>
                <td>{a.缺口 || a.reason || '—'}</td>
                <td>{a.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

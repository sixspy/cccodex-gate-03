import { useEffect, useState } from "react";

function loadProfile() {
  try {
    return JSON.parse(localStorage.getItem("lake.profile") || "null");
  } catch {
    return null;
  }
}

async function request(path, options = {}) {
  const token = localStorage.getItem("lake.token") || "";
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "请求没有完成");
  }
  return data;
}

export default function App() {
  const [profile, setProfile] = useState(loadProfile);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("123456");
  const [loginError, setLoginError] = useState("");
  const [view, setView] = useState("layers");
  const [dates, setDates] = useState([]);
  const [detail, setDetail] = useState(null);
  const [daycut, setDaycut] = useState(null);
  const [ruleChoice, setRuleChoice] = useState("natural");
  const [cutoffInput, setCutoffInput] = useState("23:00");
  const [settleDays, setSettleDays] = useState({});
  const [formError, setFormError] = useState("");
  const [daycutError, setDaycutError] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh(prefer) {
    const rows = await request("/api/dates");
    setDates(rows);
    const wanted = prefer || detail?.biz_date;
    const target = rows.some((row) => row.biz_date === wanted) ? wanted : rows[0]?.biz_date;
    setDetail(target ? await request(`/api/dates/${target}`) : null);
    setDaycut(await request("/api/daycut/overview"));
  }

  useEffect(() => {
    if (!profile) return;
    refresh().catch(() => {
      localStorage.removeItem("lake.token");
      localStorage.removeItem("lake.profile");
      setProfile(null);
    });
  }, [profile]);

  useEffect(() => {
    const settings = daycut?.settings;
    if (settings?.pinned) {
      setRuleChoice(settings.rule);
      if (settings.cutoff) setCutoffInput(settings.cutoff);
    }
  }, [daycut]);

  async function submitLogin(event) {
    event.preventDefault();
    setLoginError("");
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("lake.token", data.access_token);
      const next = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("lake.profile", JSON.stringify(next));
      setProfile(next);
    } catch (error) {
      setLoginError(error.message);
    }
  }

  async function buildDay() {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    const settings = detail.settings || {};
    try {
      const data = await request(`/api/dates/${detail.biz_date}/build`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rule: settings.rule, cutoff: settings.cutoff }),
      });
      setDetail(data);
      setDates(await request("/api/dates"));
    } catch (error) {
      setFormError(error.message);
      try {
        setDetail(await request(`/api/dates/${detail.biz_date}`));
        setDates(await request("/api/dates"));
      } catch {
        /* 页面保留原样 */
      }
    } finally {
      setBusy(false);
    }
  }

  async function pinSettings(event) {
    event.preventDefault();
    setBusy(true);
    setDaycutError("");
    try {
      await request("/api/daycut/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rule: ruleChoice, cutoff: cutoffInput }),
      });
      await refresh();
    } catch (error) {
      setDaycutError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function settle(scanId, day) {
    setBusy(true);
    setDaycutError("");
    try {
      await request(`/api/daycut/queue/${scanId}/settle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ biz_date: day }),
      });
      await refresh();
    } catch (error) {
      setDaycutError(error.message);
      try {
        setDaycut(await request("/api/daycut/overview"));
      } catch {
        /* 页面保留原样 */
      }
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
    setDaycut(null);
  }

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。只有字典认得出的事件才收进明细。</p>
        <form onSubmit={submitLogin}>
          <label>用户<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>口令<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <button>打开这一层</button>
          {loginError ? <p className="warn">{loginError}</p> : null}
        </form>
      </main>
    );
  }

  const queue = (daycut?.rows || []).filter((row) => row.cross);
  const settings = daycut?.settings || { pinned: false };

  return (
    <>
      <header className="bar">
        <div>
          <p className="kicker">闸口扫描 · 分层</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <span>{profile.title} {profile.username}</span>
          <button className="line" onClick={logout}>离开</button>
        </div>
      </header>
      <nav className="tabs">
        <button className={view === "layers" ? "tab on" : "tab"} onClick={() => setView("layers")}>分层构建</button>
        <button className={view === "daycut" ? "tab on" : "tab"} onClick={() => setView("daycut")}>日切</button>
      </nav>
      {view === "layers" ? (
        <main className="layout">
          <aside>
            {dates.map((row) => (
              <button
                key={row.biz_date}
                className={detail?.biz_date === row.biz_date ? "day on" : "day"}
                onClick={() => request(`/api/dates/${row.biz_date}`).then(setDetail)}
              >
                <strong>{row.biz_date}</strong>
                <em>{row.built ? "明细已收" : "尚未构建"}</em>
                <span>贴源 {row.ods_count} 条 · 明细 {row.dwd_count} 条</span>
              </button>
            ))}
          </aside>
          {detail ? (
            <section className="sheet">
              <div className="split-head">
                <h2>{detail.biz_date}</h2>
                <button
                  disabled={profile.role !== "writer" || busy || !detail.settings?.pinned}
                  onClick={buildDay}
                >
                  按字典构建明细层
                </button>
              </div>
              <p className="note">IN 进闸，OUT 出闸，RS 倒箱。箱号要是四位字母加七位数字。</p>
              {!detail.settings?.pinned ? (
                <p className="warn">日切尚未钉住，不许开始构建。</p>
              ) : (
                <p className="note">日切规则：{detail.settings.rule_name}。构建会带上这条规则，和日切设置对不上就整批不入库。</p>
              )}
              {profile.role !== "writer" ? <p className="note">核对员不能重新构建。</p> : null}
              {detail.run ? (
                <p className={detail.run.status === "rule_mismatch" ? "warn" : "note"}>
                  上次构建：收 {detail.run.accepted_count} 条 · 剔 {detail.run.rejected_count} 条 · {detail.run.built_by} · {detail.run.built_at}
                  {detail.run.status === "rule_mismatch" ? " · 日切规则不符，这批不入库" : ""}
                </p>
              ) : null}
              {formError ? <p className="warn">{formError}</p> : null}
              <div className="layers">
                <article>
                  <h3>贴源层</h3>
                  <ul>
                    {detail.ods.map((row) => (
                      <li key={row.id}>
                        <span>{row.terminal_code}</span>
                        <code>{JSON.stringify(row.raw_payload)}</code>
                        <em className="rec">
                          记在 {row.recorded_date}
                          {row.scan_note ? ` · ${row.scan_note}` : ""}
                        </em>
                        {row.cross ? (
                          <em className="crossline">
                            跨日前 {row.recorded_date} → 跨日后 {row.cut_date}（落入日 {row.natural_date}）
                          </em>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </article>
                <article>
                  <h3>明细层</h3>
                  {detail.moves.length ? (
                    <table>
                      <thead>
                        <tr><th>来源</th><th>箱号</th><th>事件</th><th>时间</th></tr>
                      </thead>
                      <tbody>
                        {detail.moves.map((row) => (
                          <tr key={`${row.source_id}-${row.event_kind}`}>
                            <td>{row.source_id}</td>
                            <td>{row.container_no}</td>
                            <td>{row.event_kind}</td>
                            <td>{row.event_at}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : <p>这一天的明细还是空的。</p>}
                  <h3>剔除</h3>
                  <ul>
                    {detail.rejects.map((row) => (
                      <li key={row.source_id}><span>来源 {row.source_id}</span><em>{row.reason}</em></li>
                    ))}
                    {!detail.rejects.length ? <li>没有被剔除的扫描</li> : null}
                  </ul>
                </article>
              </div>
            </section>
          ) : null}
        </main>
      ) : (
        <main className="daycut">
          <section className="sheet">
            <h2>日切设置</h2>
            {settings.pinned ? (
              <p className="note">
                当前规则：{settings.rule_name} · {settings.pinned_by} 钉于 {settings.pinned_at}
              </p>
            ) : (
              <p className="warn">日切尚未钉住，不许开始构建。</p>
            )}
            {profile.role === "writer" ? (
              settings.pinned && settings.locked_today ? (
                <p className="note">规则写进日切设置后，当天不许再改。</p>
              ) : (
                <form className="pinform" onSubmit={pinSettings}>
                  <label>
                    <input
                      type="radio"
                      name="daycut-rule"
                      checked={ruleChoice === "natural"}
                      onChange={() => setRuleChoice("natural")}
                    />
                    按扫描时刻落入所在自然日
                  </label>
                  <label>
                    <input
                      type="radio"
                      name="daycut-rule"
                      checked={ruleChoice === "cutoff"}
                      onChange={() => setRuleChoice("cutoff")}
                    />
                    过了日切线的扫描改记次日
                  </label>
                  {ruleChoice === "cutoff" ? (
                    <label className="cutoff">
                      日切线
                      <input
                        type="time"
                        value={cutoffInput}
                        onChange={(event) => setCutoffInput(event.target.value)}
                      />
                    </label>
                  ) : null}
                  <button disabled={busy}>{settings.pinned ? "改钉这条规则" : "钉死日切规则"}</button>
                </form>
              )
            ) : (
              <p className="note">核对员只能查看日切设置。</p>
            )}
            {daycutError ? <p className="warn">{daycutError}</p> : null}
          </section>
          <section className="sheet">
            <h2>日切列表</h2>
            <p className="note">落入日是扫描时刻所在的自然日，改记日是按钉住的规则算出的业务日。</p>
            <table>
              <thead>
                <tr><th>来源</th><th>闸口</th><th>扫描时刻</th><th>落入日</th><th>改记日</th><th>规则名</th><th>记录日</th></tr>
              </thead>
              <tbody>
                {(daycut?.rows || []).map((row) => (
                  <tr key={row.id} className={row.cross ? "cross" : ""}>
                    <td>{row.id}</td>
                    <td>{row.terminal_code}</td>
                    <td>{row.scan_at || row.scan_note || "—"}</td>
                    <td>{row.natural_date || "—"}</td>
                    <td>{row.cut_date || "—"}</td>
                    <td>{settings.pinned ? settings.rule_name : "日切未钉住"}</td>
                    <td>{row.recorded_date}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <section className="sheet">
            <h2>跨日队列</h2>
            {!settings.pinned ? <p className="note">日切尚未钉住，先钉规则再定业务日。</p> : null}
            {queue.length ? (
              <div className="queue">
                {queue.map((item) => (
                  <article className="qitem" key={item.id}>
                    <div>
                      <strong>来源 {item.id} · {item.terminal_code}</strong>
                      <span>
                        扫描时刻 {item.scan_at} · 落入日 {item.natural_date} · 改记日 {item.cut_date} · 现记 {item.recorded_date}
                      </span>
                    </div>
                    {profile.role === "writer" ? (
                      <div className="settle">
                        <input
                          type="date"
                          value={settleDays[item.id] ?? item.cut_date}
                          onChange={(event) =>
                            setSettleDays({ ...settleDays, [item.id]: event.target.value })
                          }
                        />
                        <button
                          disabled={busy}
                          onClick={() => settle(item.id, settleDays[item.id] ?? item.cut_date)}
                        >
                          定业务日
                        </button>
                      </div>
                    ) : null}
                  </article>
                ))}
              </div>
            ) : (
              <p className="note">没有待定的跨日扫描。时刻空着的扫描不进跨日队列。</p>
            )}
            <h3>日切争用记录</h3>
            <ul>
              {(daycut?.contentions || []).map((row) => (
                <li key={row.id}>
                  <span>
                    来源 {row.scan_id} · {row.terminal_code} · {row.attempted_by} 想定到 {row.wanted_date}，日切设置应记 {row.expected_date}
                  </span>
                  <em>{row.note} · {row.attempted_at}</em>
                </li>
              ))}
              {!(daycut?.contentions || []).length ? <li>没有争用记录</li> : null}
            </ul>
          </section>
        </main>
      )}
    </>
  );
}

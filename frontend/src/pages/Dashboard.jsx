import React from "react";
import { api } from "@/lib/api";
import { RefreshCw, Radio, Clock, Store as StoreIcon, Megaphone } from "lucide-react";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  PieChart, Pie, Cell, Legend, LineChart, Line,
} from "recharts";
import { FilterSelect, SearchableSelect } from "@/components/AdminFilters";

/**
 * The dashboard.
 *
 * WHAT IS DELIBERATELY NOT HERE
 *
 * No uptime, no delivery rate, no health score. Every figure is something the
 * database recorded - sessions started, minutes on air, which account did it,
 * what each shop's announcement is doing. A percentage that averages a Store
 * nobody has heard from with a Store that is fine reads as reassurance, and
 * reassurance is the one thing this product must not invent.
 *
 * WHY EVERY REPORT IS A TABLE **AND** A CHART
 *
 * A chart answers "which is biggest" at a glance and cannot answer "how many
 * exactly". Somebody who is about to act on one of these numbers - ring a
 * shop, question a colleague's hours - needs the number, not the shape. Both,
 * side by side, means neither has to pretend to be the other.
 */

//: Fixed colours per announcement state, matching the badges on the
//: Announcements console. A chart that colours "Paused" green on one screen
//: and amber on another makes the reader check the legend every time.
//: One tooltip surface for every chart on this page. Recharts defaults to a
//: white card with black text, which on a dark dashboard is a torch.
const TOOLTIP_STYLE = {
  background: "var(--surface-solid)",
  border: "1px solid var(--line)",
  borderRadius: 10,
  color: "var(--text-strong)",
  boxShadow: "var(--glass-shadow)",
};

const STATE_COLOURS = {
  PLAYING: "#059669",
  PAUSED: "#d97706",
  DUCKED: "#0284c7",
  STOPPED: "#94a3b8",
  // Red, like the badge on the Announcements table: this is the slice that
  // means somebody believes a promotion is on air and it is not.
  UNREACHABLE: "#e11d48",
};

const STATE_LABELS = {
  PLAYING: "Playing",
  PAUSED: "Paused by a person",
  DUCKED: "Standing aside for a broadcast",
  STOPPED: "Nothing chosen",
  // HQ sent a play and nothing answered. Its own slice, because folding it
  // into "Playing" is the claim this dashboard must never make - and leaving
  // it unlabelled put the raw word UNREACHABLE on the chart.
  UNREACHABLE: "Asked, no Receiver",
};

const today = () => new Date().toISOString().slice(0, 10);
const daysAgo = (count) => new Date(Date.now() - count * 86400000)
  .toISOString().slice(0, 10);

//: The periods people actually ask for. "Today" and "yesterday" are windows
//: with BOTH ends - expressing yesterday as "the last day" would silently
//: include this morning, which is the kind of quiet wrongness a dashboard
//: must not have.
const PRESETS = [
  { value: "today", label: "Today", range: () => ({ since: today(), until: today() }) },
  { value: "yesterday", label: "Yesterday",
    range: () => ({ since: daysAgo(1), until: daysAgo(1) }) },
  { value: "7", label: "Last 7 days", range: () => ({ days: 7 }) },
  { value: "30", label: "Last 30 days", range: () => ({ days: 30 }) },
  { value: "90", label: "Last 90 days", range: () => ({ days: 90 }) },
  { value: "365", label: "Last year", range: () => ({ days: 365 }) },
  { value: "custom", label: "Custom dates", range: null },
];

const REPORTS = [
  { key: "by_day", label: "By day", nameKey: "day", heading: "Day" },
  { key: "by_user", label: "By broadcaster", nameKey: "user", heading: "Broadcaster" },
  { key: "by_zone", label: "By zone", nameKey: "zone", heading: "Zone" },
  { key: "by_store", label: "By Store", nameKey: "store_name", heading: "Store" },
];

function Tile({ icon: Icon, label, value, hint, testId }) {
  return (
    <div className="glass rounded-xl p-4" data-testid={testId}>
      <div className="flex items-center gap-2 text-muted text-xs uppercase tracking-widest">
        <Icon size={14} /> {label}
      </div>
      <div className="text-3xl font-bold text-strong mt-1 tabular-nums">{value}</div>
      {hint && <div className="text-xs text-muted mt-1">{hint}</div>}
    </div>
  );
}

export default function Dashboard() {
  const [preset, setPreset] = React.useState("30");
  const [custom, setCustom] = React.useState({ since: daysAgo(7), until: today() });
  const [zone, setZone] = React.useState("");
  const [city, setCity] = React.useState("");
  const [storeId, setStoreId] = React.useState("");
  const [ownerId, setOwnerId] = React.useState("");
  const [report, setReport] = React.useState("by_day");
  const [reportSort, setReportSort] = React.useState({ column: "", dir: "asc" });
  const [options, setOptions] = React.useState({ regions: [], cities: [], stores: [] });
  const [users, setUsers] = React.useState([]);
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(true);

  const load = React.useCallback(() => {
    const chosen = PRESETS.find((entry) => entry.value === preset);
    const range = chosen?.range ? chosen.range() : custom;
    setLoading(true);
    api.get("/dashboard/summary", { params: {
      ...range,
      ...(zone ? { zone } : {}),
      ...(city ? { city } : {}),
      ...(storeId ? { store_id: storeId } : {}),
      ...(ownerId ? { owner_user_id: ownerId } : {}),
    } })
      .then(({ data: body }) => { setData(body); setError(""); })
      .catch((failure) => setError(
        failure?.response?.data?.detail
        || "The dashboard could not be loaded. Try again."))
      .finally(() => setLoading(false));
  }, [preset, custom, zone, city, storeId, ownerId]);

  React.useEffect(() => { load(); }, [load]);

  React.useEffect(() => {
    // The same scoped endpoints the other admin pages use, so a filter can
    // never offer a Zone or a Store this account may not open.
    api.get("/receivers/filter-options")
      .then(({ data: body }) => setOptions(body))
      .catch(() => {});
    api.get("/users/search", { params: { page_size: 200 } })
      .then(({ data: body }) => setUsers((body.items || []).map((row) => ({
        value: String(row.id), label: row.display_name || row.username }))))
      .catch(() => { /* an account without users.view simply gets no filter */ });
  }, []);

  const states = data?.announcements?.states || {};
  const pieTotal = Object.entries(states)
    .filter(([, count]) => count > 0)
    .reduce((sum, [, count]) => sum + count, 0);
  const pie = Object.entries(states)
    .filter(([, count]) => count > 0)
    .map(([state, count]) => ({ name: STATE_LABELS[state] || state,
                                value: count, state }));

  const activeReport = REPORTS.find((entry) => entry.key === report);
  //: Sorted in the browser, deliberately.
  //:
  //: Everywhere else sorting goes to the server, because those tables hold one
  //: page of a longer list. A report is the WHOLE answer already - the summary
  //: returns every day, broadcaster, zone and Store in the period - so
  //: ordering it here orders all of it, and a round trip would buy nothing.
  const rows = React.useMemo(() => {
    const source = data?.[report] || [];
    if (!reportSort.column) return source;
    const read = (row) => row[reportSort.column];
    return [...source].sort((left, right) => {
      const a = read(left);
      const b = read(right);
      const numeric = typeof a === "number" && typeof b === "number";
      const comparison = numeric
        ? a - b
        : String(a ?? "").toLowerCase()
            .localeCompare(String(b ?? "").toLowerCase());
      return reportSort.dir === "desc" ? -comparison : comparison;
    });
  }, [data, report, reportSort]);

  return (
    <div className="space-y-4" data-testid="dashboard-page">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-strong">Dashboard</h1>
        <p className="text-sm text-muted">
          What has been broadcast, by whom, where, and what every shop is
          playing now.
        </p>
      </div>

      {/* ---- Filters ---- */}
      <div className="glass rounded-xl p-3 flex flex-wrap items-end gap-3"
           data-testid="dashboard-filters">
        {/* The same panel as every other filter, rather than a bare select.
            One gesture to learn across the whole product beats a control that
            is special because its list happens to be short. */}
        <SearchableSelect label="Period" testId="dashboard-period"
                          placeholder="Choose a period" value={preset}
                          onChange={(value) => setPreset(value || "30")}
                          options={PRESETS.map(({ value, label }) => ({ value, label }))} />
        {preset === "custom" && (
          <>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-widest text-muted">From</span>
              <input type="date" value={custom.since} data-testid="dashboard-since"
                     onChange={(event) => setCustom((was) => ({ ...was, since: event.target.value }))}
                     className="px-2 py-1.5 border border-line-strong rounded-md text-sm" />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-widest text-muted">To</span>
              <input type="date" value={custom.until} data-testid="dashboard-until"
                     onChange={(event) => setCustom((was) => ({ ...was, until: event.target.value }))}
                     className="px-2 py-1.5 border border-line-strong rounded-md text-sm" />
            </label>
          </>
        )}
        <FilterSelect label="Zone" testId="dashboard-zone" allLabel="All Zones"
                      value={zone} onChange={setZone}
                      options={(options.regions || []).map((entry) =>
                        (typeof entry === "string" ? entry : entry.value))} />
        <FilterSelect label="City" testId="dashboard-city" allLabel="All Cities"
                      value={city} onChange={setCity}
                      options={(options.cities || []).map((entry) =>
                        (typeof entry === "string" ? entry : entry.value))} />
        <FilterSelect label="Store" testId="dashboard-store" allLabel="All Stores"
                      value={storeId} onChange={setStoreId}
                      options={(options.stores || []).map((store) => ({
                        value: String(store.id),
                        label: `${store.store_name} (${store.store_code})` }))} />
        <FilterSelect label="Broadcaster" testId="dashboard-user"
                      allLabel="Anybody" value={ownerId} onChange={setOwnerId}
                      options={users} />
        <button data-testid="dashboard-refresh" onClick={load}
                className="ml-auto inline-flex items-center gap-1 px-3 py-2 border border-line-strong rounded-md text-sm hover:bg-surface-muted">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {error && (
        <div data-testid="dashboard-error"
             className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </div>
      )}
      {loading && !data && (
        <p className="text-sm text-muted" data-testid="dashboard-loading">Loading…</p>
      )}

      {data && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tile icon={Radio} label="Broadcasts" testId="tile-broadcasts"
                  value={data.broadcasts.total}
                  hint={`${data.broadcasts.live_now} live right now`} />
            <Tile icon={Clock} label="Minutes on air" testId="tile-minutes"
                  value={data.broadcasts.minutes}
                  // The longest one gets its own line rather than an average:
                  // a broadcast nobody stopped is the failure this number
                  // exists to surface, and an average hides it by
                  // construction.
                  hint={`longest single broadcast: ${data.broadcasts.longest_minutes} min`} />
            <Tile icon={StoreIcon} label="Stores" testId="tile-stores"
                  value={data.stores.total}
                  hint={`${data.stores.online} online, ${data.stores.offline} not`} />
            {/* PLAYING counts shops HQ can actually observe. The ones it
                asked and never heard back from are their own number: folding
                them in read as "44 playing" on an estate with 2 shops online,
                and a headline figure gets believed without being checked. */}
            <Tile icon={Megaphone} label="Announcements playing"
                  testId="tile-announcements" value={states.PLAYING || 0}
                  hint={[`${states.DUCKED || 0} standing aside for a broadcast`,
                         states.UNREACHABLE
                           ? `${states.UNREACHABLE} asked, no Receiver`
                           : ""].filter(Boolean).join(" - ")} />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <section className="glass rounded-xl p-4 lg:col-span-2">
              <h2 className="font-semibold text-strong">Minutes on air, by day</h2>
              <p className="text-xs text-muted mb-2">
                Counts and minutes together: ten short interruptions and one
                long campaign are not the same day.
              </p>
              <div style={{ width: "100%", height: 240 }} data-testid="chart-by-day">
                <ResponsiveContainer>
                  <LineChart data={data.by_day}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                    <XAxis dataKey="day" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip cursor={{ fill: "rgba(148,163,184,0.12)" }}
                             contentStyle={TOOLTIP_STYLE}
                             labelStyle={{ color: "var(--text-strong)" }}
                             itemStyle={{ color: "var(--text-body)" }} />
                    <Legend />
                    <Line type="monotone" dataKey="minutes" name="Minutes"
                          stroke="#1d4ed8" strokeWidth={2} dot={{ r: 3 }} />
                    <Line type="monotone" dataKey="broadcasts" name="Broadcasts"
                          stroke="#059669" strokeWidth={2} dot={{ r: 3 }} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              {data.by_day.length === 0 && (
                <p className="text-sm text-muted" data-testid="by-day-empty">
                  Nothing has been broadcast in this period.
                </p>
              )}
            </section>

            <section className="glass rounded-xl p-4">
              <h2 className="font-semibold text-strong">What the shops are playing</h2>
              <p className="text-xs text-muted mb-2">Right now.</p>
              {/* A ring, not a disc, with the estate's size in the middle.
                  The question this answers is "how much of my estate is
                  actually playing something", and on 44 shops with one
                  exception the disc was a grey circle with a red splinter -
                  true, and unreadable. The ring gives the total somewhere to
                  live, and the list underneath gives every slice a number,
                  because a two-degree wedge cannot be read off a chart. */}
              <div className="relative" style={{ width: "100%", height: 210 }}
                   data-testid="chart-states">
                <ResponsiveContainer>
                  <PieChart>
                    {/* paddingAngle only when there is more than one slice.
                        With a single slice it cuts a wedge out of a ring that
                        is, truthfully, the whole estate - the chart drew a gap
                        where no gap exists. */}
                    <Pie data={pie} dataKey="value" nameKey="name"
                         innerRadius={62} outerRadius={88}
                         paddingAngle={pie.length > 1 ? 2 : 0}
                         stroke="none" minAngle={pie.length > 1 ? 3 : 0}
                         isAnimationActive={false}>
                      {pie.map((slice) => (
                        <Cell key={slice.state} fill={STATE_COLOURS[slice.state]} />
                      ))}
                    </Pie>
                    {/* No tooltip. The list underneath already gives every
                        slice its count and its share, permanently and without
                        hovering - and the hover card landed on top of the
                        total in the middle, hiding the one number the ring
                        exists to show. */}
                  </PieChart>
                </ResponsiveContainer>
                <div className="pointer-events-none absolute inset-0 flex flex-col
                                items-center justify-center">
                  <div className="text-2xl font-bold text-strong"
                       data-testid="states-total">{pieTotal}</div>
                  <div className="text-[11px] uppercase tracking-widest text-muted">
                    Stores
                  </div>
                </div>
              </div>

              {/* The legend, as a list with counts and shares. Recharts' own
                  legend gives colour and name only, and "Asked, no Receiver"
                  next to a two-degree wedge tells nobody how many shops that
                  is. */}
              <ul className="mt-1 space-y-1" data-testid="states-legend">
                {pie.map((slice) => (
                  <li key={slice.state}
                      data-testid={`states-legend-${slice.state}`}
                      className="flex items-center gap-2 text-sm">
                    <span className="h-2.5 w-2.5 shrink-0 rounded-sm"
                          style={{ backgroundColor: STATE_COLOURS[slice.state] }} />
                    <span className="text-body">{slice.name}</span>
                    <span className="ml-auto tabular-nums font-medium text-strong">
                      {slice.value}
                    </span>
                    <span className="w-12 text-right tabular-nums text-xs text-muted">
                      {pieTotal ? Math.round((slice.value / pieTotal) * 100) : 0}%
                    </span>
                  </li>
                ))}
              </ul>
              {pie.length === 0 && (
                <p className="text-sm text-muted" data-testid="states-empty">
                  No Store has an announcement chosen yet.
                </p>
              )}
            </section>
          </div>

          {/* ---- Reports: the same numbers as a chart AND a table ---- */}
          <section className="glass rounded-xl">
            <div className="px-4 py-3 border-b border-line flex flex-wrap items-center gap-2">
              <h2 className="font-semibold text-strong mr-auto">Reports</h2>
              {REPORTS.map((entry) => (
                <button key={entry.key}
                        onClick={() => { setReport(entry.key);
                                         setReportSort({ column: "", dir: "asc" }); }}
                        data-testid={`report-tab-${entry.key}`}
                        aria-current={report === entry.key ? "true" : undefined}
                        className={`px-3 py-1.5 rounded-md text-sm border ${
                          report === entry.key
                            ? "bg-surface-muted text-white border-slate-900"
                            : "border-line-strong text-body hover:bg-surface-muted"}`}>
                  {entry.label}
                </button>
              ))}
            </div>

            <div className="p-4 grid gap-4 lg:grid-cols-2">
              <div style={{ width: "100%", height: 320 }}
                   data-testid={`report-chart-${report}`}>
                <ResponsiveContainer>
                  <BarChart data={rows} layout="vertical" margin={{ left: 30 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                    <XAxis type="number" tick={{ fontSize: 11 }} />
                    <YAxis type="category" dataKey={activeReport.nameKey} width={150}
                           tick={{ fontSize: 11 }} />
                    <Tooltip cursor={{ fill: "rgba(148,163,184,0.12)" }}
                             contentStyle={TOOLTIP_STYLE}
                             labelStyle={{ color: "var(--text-strong)" }}
                             itemStyle={{ color: "var(--text-body)" }} />
                    <Legend />
                    <Bar dataKey="minutes" name="Minutes" fill="#1d4ed8" />
                    <Bar dataKey="broadcasts" name="Broadcasts" fill="#94a3b8" />
                  </BarChart>
                </ResponsiveContainer>
              </div>

              {/* The table is not a duplicate of the chart.
                  A chart answers "which is biggest" at a glance and cannot
                  answer "how many exactly" - and somebody about to ring a shop
                  or question a colleague's hours needs the number. */}
              <div className="overflow-x-auto max-h-80 overflow-y-auto">
                <table className="w-full text-sm" data-testid={`report-table-${report}`}>
                  <thead className="bg-surface-muted text-left text-[11px] uppercase tracking-wider text-muted sticky top-0">
                    <tr>
                      <ReportTh column={activeReport.nameKey}
                                label={activeReport.heading}
                                sort={reportSort} onSort={setReportSort} />
                      {report === "by_store" && (
                        <ReportTh column="zone" label="Zone" sort={reportSort}
                                  onSort={setReportSort} />
                      )}
                      {report === "by_zone" && (
                        <ReportTh column="stores" label="Stores" sort={reportSort}
                                  onSort={setReportSort} />
                      )}
                      <ReportTh column="broadcasts" label="Broadcasts" align="right"
                                sort={reportSort} onSort={setReportSort} />
                      <ReportTh column="minutes" label="Minutes" align="right"
                                sort={reportSort} onSort={setReportSort} />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.length === 0 && (
                      <tr><td colSpan={4} className="px-3 py-6 text-center text-muted"
                              data-testid={`report-empty-${report}`}>
                        Nothing in this period.
                      </td></tr>
                    )}
                    {rows.map((row, index) => (
                      <tr key={index} className="border-b border-line even:bg-surface-alt">
                        <td className="px-3 py-2">
                          {row[activeReport.nameKey]}
                          {report === "by_store" && (
                            <span className="block text-xs text-faint">
                              {row.store_code}
                            </span>
                          )}
                        </td>
                        {report === "by_store" && <td className="px-3 py-2">{row.zone}</td>}
                        {report === "by_zone" && <td className="px-3 py-2">{row.stores}</td>}
                        <td className="px-3 py-2 text-right tabular-nums">{row.broadcasts}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{row.minutes}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}


/**
 * A sortable heading for a report.
 *
 * Not the shared SortableTh: that one drives a server query, because those
 * tables hold one page of a longer list. A report is the whole answer
 * already, so it is ordered here.
 */
function ReportTh({ column, label, sort, onSort, align = "left" }) {
  const active = sort.column === column;
  const toggle = () => {
    if (!active) return onSort({ column, dir: "asc" });
    if (sort.dir === "asc") return onSort({ column, dir: "desc" });
    // Third click restores the order the report arrived in, which is already
    // the useful one - biggest first.
    return onSort({ column: "", dir: "asc" });
  };
  return (
    <th className="px-3 py-2" style={{ textAlign: align }}
        aria-sort={active ? (sort.dir === "desc" ? "descending" : "ascending")
                          : "none"}>
      <button type="button" onClick={toggle} data-testid={`report-sort-${column}`}
              className="inline-flex items-center gap-1 hover:text-strong">
        {label}
        <span aria-hidden="true" className={active ? "text-strong" : "text-faint"}>
          {active ? (sort.dir === "desc" ? "↓" : "↑") : "⇅"}
        </span>
      </button>
    </th>
  );
}

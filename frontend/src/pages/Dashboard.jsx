import React from "react";
import { api } from "@/lib/api";
import { RefreshCw, Radio, Clock, Store as StoreIcon, Megaphone } from "lucide-react";
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid,
  PieChart, Pie, Cell, Legend, LineChart, Line,
} from "recharts";
import { FilterSelect } from "@/components/AdminFilters";

/**
 * The dashboard.
 *
 * WHAT IS DELIBERATELY NOT HERE
 *
 * No uptime, no delivery rate, no health score. Every figure on this page is
 * something the database actually recorded - sessions started, minutes
 * broadcast, which account did it, what each shop's announcement is doing. A
 * percentage that averages a Store nobody has heard from with a Store that is
 * fine reads as reassurance, and reassurance is the one thing this product
 * must not invent.
 *
 * WHY MINUTES AND COUNTS ARE BOTH SHOWN
 *
 * Ten thirty-second interruptions and one five-minute campaign are not the
 * same working day, and a single "broadcasts" number cannot tell them apart.
 */

//: Fixed colours per announcement state, matching the badges on the
//: Announcements console. A chart that colours "Paused" green on one screen
//: and amber on another makes the reader check the legend every time.
const STATE_COLOURS = {
  PLAYING: "#059669",
  PAUSED: "#d97706",
  DUCKED: "#0284c7",
  STOPPED: "#94a3b8",
};

const STATE_LABELS = {
  PLAYING: "Playing",
  PAUSED: "Paused by a person",
  DUCKED: "Standing aside for a broadcast",
  STOPPED: "Nothing chosen",
};

function Tile({ icon: Icon, label, value, hint, testId }) {
  return (
    <div className="border border-slate-200 bg-white rounded-md p-4" data-testid={testId}>
      <div className="flex items-center gap-2 text-slate-500 text-xs uppercase tracking-widest">
        <Icon size={14} /> {label}
      </div>
      <div className="text-3xl font-bold text-slate-900 mt-1 tabular-nums">{value}</div>
      {hint && <div className="text-xs text-slate-500 mt-1">{hint}</div>}
    </div>
  );
}

export default function Dashboard() {
  const [days, setDays] = React.useState("30");
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(true);

  const load = React.useCallback(() => {
    setLoading(true);
    api.get("/dashboard/summary", { params: { days: Number(days) } })
      .then(({ data: body }) => { setData(body); setError(""); })
      .catch((failure) => setError(
        failure?.response?.data?.detail
        || "The dashboard could not be loaded. Try again."))
      .finally(() => setLoading(false));
  }, [days]);

  React.useEffect(() => { load(); }, [load]);

  const states = data?.announcements?.states || {};
  const pie = Object.entries(states)
    .filter(([, count]) => count > 0)
    .map(([state, count]) => ({ name: STATE_LABELS[state] || state,
                                value: count, state }));

  return (
    <div className="space-y-4" data-testid="dashboard-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Dashboard</h1>
          <p className="text-sm text-slate-500">
            What has been broadcast, by whom, and what every shop is playing now.
          </p>
        </div>
        <div className="flex items-end gap-2">
          <FilterSelect label="Period" testId="dashboard-days" multiple={false}
                        allLabel={null} value={days} onChange={setDays}
                        options={[{ value: "7", label: "Last 7 days" },
                                  { value: "30", label: "Last 30 days" },
                                  { value: "90", label: "Last 90 days" },
                                  { value: "365", label: "Last year" }]} />
          <button data-testid="dashboard-refresh" onClick={load}
                  className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50">
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </div>

      {error && (
        <div data-testid="dashboard-error"
             className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </div>
      )}
      {loading && !data && (
        <p className="text-sm text-slate-500" data-testid="dashboard-loading">
          Loading…
        </p>
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
            <Tile icon={Megaphone} label="Announcements playing"
                  testId="tile-announcements"
                  value={states.PLAYING || 0}
                  hint={`${states.DUCKED || 0} standing aside for a broadcast`} />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <section className="border border-slate-200 bg-white rounded-md p-4">
              <h2 className="font-semibold text-slate-900">Minutes on air, by day</h2>
              <p className="text-xs text-slate-500 mb-2">
                Counts and minutes together: ten short interruptions and one
                long campaign are not the same day.
              </p>
              <div style={{ width: "100%", height: 240 }} data-testid="chart-by-day">
                <ResponsiveContainer>
                  <LineChart data={data.by_day}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                    <XAxis dataKey="day" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Legend />
                    <Line type="monotone" dataKey="minutes" name="Minutes"
                          stroke="#1d4ed8" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="broadcasts" name="Broadcasts"
                          stroke="#059669" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              {data.by_day.length === 0 && (
                <p className="text-sm text-slate-500" data-testid="by-day-empty">
                  Nothing has been broadcast in this period.
                </p>
              )}
            </section>

            <section className="border border-slate-200 bg-white rounded-md p-4">
              <h2 className="font-semibold text-slate-900">What the shops are playing</h2>
              <p className="text-xs text-slate-500 mb-2">
                Right now, across every Store this account can see.
              </p>
              <div style={{ width: "100%", height: 240 }} data-testid="chart-states">
                <ResponsiveContainer>
                  <PieChart>
                    <Pie data={pie} dataKey="value" nameKey="name" outerRadius={90}
                         label={(entry) => `${entry.name}: ${entry.value}`}>
                      {pie.map((slice) => (
                        <Cell key={slice.state} fill={STATE_COLOURS[slice.state]} />
                      ))}
                    </Pie>
                    <Tooltip />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              {pie.length === 0 && (
                <p className="text-sm text-slate-500" data-testid="states-empty">
                  No Store has an announcement chosen yet.
                </p>
              )}
            </section>
          </div>

          <section className="border border-slate-200 bg-white rounded-md p-4">
            <h2 className="font-semibold text-slate-900">Time on air, by broadcaster</h2>
            <p className="text-xs text-slate-500 mb-2">
              Who has been speaking, and for how long.
            </p>
            <div style={{ width: "100%", height: 260 }} data-testid="chart-by-user">
              <ResponsiveContainer>
                <BarChart data={data.by_user} layout="vertical"
                          margin={{ left: 40 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis type="number" tick={{ fontSize: 11 }} />
                  <YAxis type="category" dataKey="user" width={140}
                         tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Legend />
                  <Bar dataKey="minutes" name="Minutes" fill="#1d4ed8" />
                  <Bar dataKey="broadcasts" name="Broadcasts" fill="#94a3b8" />
                </BarChart>
              </ResponsiveContainer>
            </div>
            {data.by_user.length === 0 && (
              <p className="text-sm text-slate-500" data-testid="by-user-empty">
                Nobody has broadcast in this period.
              </p>
            )}
          </section>
        </>
      )}
    </div>
  );
}

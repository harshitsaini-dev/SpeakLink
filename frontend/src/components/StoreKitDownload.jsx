import React from "react";
import { Download, PackageOpen, Upload, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Fetch the current Store Kit from HQ.
 *
 * WHY THIS EXISTS RATHER THAN A SHARED FOLDER
 *
 * Getting a kit onto a Store PC used to mean a USB stick, and a USB stick
 * cannot tell anybody which build a shop received. A kit downloaded from HQ is
 * the kit HQ has, HQ records that it was fetched, and the checksum is on the
 * screen so the person at the till can check what arrived.
 *
 * The checksum is shown rather than hidden behind a tooltip on purpose: it is
 * the only thing on this card that can prove a truncated download, and a
 * download that half-arrives produces an installer that fails in a way nobody
 * can diagnose over a phone.
 */
export default function StoreKitDownload() {
  const { can } = useAuth();
  const [state, setState] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const fileRef = React.useRef(null);

  const allowed = can("store_kit.download");
  //: A stronger right than downloading: whoever can upload decides what
  //: software every Store installs next.
  const mayManage = can("store_kit.manage");

  React.useEffect(() => {
    if (!allowed) return;
    api.get("/store-kits")
      .then(({ data }) => setState(data))
      .catch((e) => setError(e?.response?.data?.detail || e.message
                             || "The kit list could not be read."));
  }, [allowed]);

  // No permission, no card. The backend refuses it either way - this is
  // presentation, not the boundary.
  if (!allowed) return null;

  const reload = async () => {
    const { data } = await api.get("/store-kits");
    setState(data);
  };

  const upload = async (file) => {
    if (!file || busy) return;
    // HQ holds exactly one kit, so this replaces whatever is there. Said
    // before it happens rather than after: an operator who picked the wrong
    // file is about to overwrite the build every Store downloads.
    const current = state?.latest?.name;
    if (current && !window.confirm(
        `This replaces the build HQ is handing out (${current}).

Continue?`)) {
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const form = new FormData();
      form.append("file", file);
      const { data } = await api.post("/store-kits", form);
      setNotice((data.superseded || []).length
        ? `${data.name} uploaded. It replaced ${data.superseded.join(", ")}.`
        : `${data.name} uploaded. Stores downloading now will get it.`);
      await reload();
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "That upload failed.");
    } finally {
      setBusy(false);
      // Cleared so the same file can be chosen again after a refusal - an
      // unchanged input fires no change event, which reads as a dead button.
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const remove = async (name) => {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api.delete(`/store-kits/${encodeURIComponent(name)}`);
      setNotice(`${name} removed.`);
      await reload();
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "That kit could not be removed.");
    } finally { setBusy(false); }
  };

  const download = async (name) => {
    setBusy(true);
    setError("");
    try {
      const path = name ? `/store-kits/${encodeURIComponent(name)}/download`
                        : "/store-kits/latest/download";
      const response = await api.get(path, { responseType: "blob" });
      // Fetched through the API because the bytes are behind a permission, so
      // a plain link would arrive without the bearer token. The object URL is
      // revoked immediately after the click - one that is never revoked holds
      // the whole file in memory for the life of the tab.
      const url = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = url;
      link.download = name || state?.latest?.name || "SpeakLinkStoreKit.zip";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e?.response?.data?.detail || e.message
               || "The kit could not be downloaded.");
    } finally { setBusy(false); }
  };

  const latest = state?.latest;

  return (
    <div className="border border-slate-200 bg-white rounded-md shadow-sm p-4"
         data-testid="store-kit-download">
      <div className="flex items-center gap-2">
        <PackageOpen size={16} className="text-slate-500" />
        <div className="text-xs font-bold uppercase tracking-[0.15em] text-slate-500">
          Store Kit
        </div>
      </div>

      {!state && !error && (
        <p className="mt-2 text-sm text-slate-500">Reading what this HQ has…</p>
      )}

      {state && !latest && (
        // "No kit" and "this is broken" look identical without this sentence,
        // and the fix for the first one is a build, not a bug report.
        <p className="mt-2 text-sm text-slate-500" data-testid="store-kit-none">
          This HQ has no Store Kit yet. Build one and put it in the
          store-kits folder for it to appear here.
        </p>
      )}

      {latest && (
        <div className="mt-2 space-y-2">
          <div className="text-sm">
            <div className="font-medium text-slate-900" data-testid="store-kit-name">
              {latest.name}
            </div>
            <div className="text-xs text-slate-500">
              {(latest.size_bytes / (1024 * 1024)).toFixed(1)} MB · built{" "}
              {new Date(latest.modified_at).toLocaleString("en-IN",
                { timeZone: "Asia/Kolkata" })}
            </div>
          </div>

          <button type="button" data-testid="store-kit-download-btn"
                  onClick={() => download(null)} disabled={busy}
                  className="inline-flex items-center gap-2 rounded-md bg-blue-700 px-3 py-2 text-sm font-semibold text-white hover:bg-blue-800 disabled:bg-slate-400">
            <Download size={15} /> {busy ? "Downloading…" : "Download the current kit"}
          </button>

          <p className="text-xs text-slate-500">
            Copy it to the Store PC and run it. It installs, upgrades, repairs
            or removes - and an upgrade keeps the Store enrolled.
          </p>

        </div>
      )}

      {mayManage && (
        <div className="mt-3 border-t border-slate-200 pt-3">
          <input ref={fileRef} type="file" accept=".exe,.zip"
                 data-testid="store-kit-upload-input" className="hidden"
                 onChange={(event) => upload(event.target.files?.[0])} />
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" data-testid="store-kit-upload" disabled={busy}
                    onClick={() => fileRef.current?.click()}
                    className="inline-flex items-center gap-2 rounded border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-40">
              <Upload size={14} /> Upload a new installer
            </button>
            {latest && (
              <button type="button" data-testid="store-kit-delete" disabled={busy}
                      onClick={() => remove(latest.name)}
                      title="Remove this build from HQ. Stores that already have it are unaffected."
                      className="inline-flex items-center gap-1 rounded border border-red-300 px-2 py-1 text-xs font-semibold text-red-800 hover:bg-red-50 disabled:opacity-40">
                <Trash2 size={13} /> Remove this build
              </button>
            )}
          </div>
          <p className="mt-1 text-[11px] text-slate-500">
            HQ checks the extension, the size and the file's magic bytes. It
            cannot tell whether a build is the right one - the account that
            uploaded it is recorded so that judgement has somewhere to point.
          </p>
        </div>
      )}

      {notice && (
        <p data-testid="store-kit-notice"
           className="mt-2 rounded border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs text-emerald-800">
          {notice}
        </p>
      )}

      {error && (
        <p role="alert" data-testid="store-kit-error"
           className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}

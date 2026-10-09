import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";
import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowLeft,
  ArrowUp,
  Check,
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  FolderOpen,
  LayoutDashboard,
  ListOrdered,
  Loader,
  Moon,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Settings,
  Sun,
  Upload,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import * as pdfjs from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "./style.css";
pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker;

type Artifact = {
  id: string;
  name: string;
  kind: string;
  attempt: number;
  created_at: number;
  job: string;
};
type Template = {
  id: string;
  name: string;
  config: {
    page_concurrency: number;
    recognize: Model;
    reconcile: Model;
    optimise: Model;
  };
};
type Provider = {
  id: string;
  name: string;
  base_url: string;
  models: string[];
};
type Model = { provider: string; model: string };
type Job = {
  id: string;
  name: string;
  kind: string;
  status: string;
  version: number;
  attempt: number;
  restarts: number;
  batch: string;
  unit: string;
  pages: number;
  created_at: number;
  started_at?: number;
  finished_at?: number;
  error?: string;
  artifacts: Artifact[];
  config: Record<string, unknown>;
  review: string;
  next_retry_at?: number;
};
type Call = {
  call_id: string;
  stage: string;
  page?: number;
  model: string;
  status: string;
  seconds: number;
  attempt: number;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  usage_raw?: unknown;
};
type Detail = Job & {
  progress: { stage?: string; compile_pass?: number; updated_at?: number };
  completed_pages: number;
  page_rows: PageRow[];
  calls: Call[];
  usage: Record<string, number | null>;
  checks: { name: string; status: string; details?: string[] }[];
  compile_errors: string[];
  recognition_eta_seconds?: number | null;
  cost?: {
    estimated_usd_low: string | null;
    estimated_usd_high: string | null;
    unpriced_calls: number;
    priced_calls: number;
  };
};
type PageRow = {
  page: number;
  status: string;
  model?: string;
  tokens: number | null;
  stage?: string;
  error?: string;
};
type Overview = {
  jobs: Job[];
  paused: boolean;
  disk: { free: number; total: number };
  worker: { online: boolean; time?: number; error?: string };
};
const labels: Record<string, string> = {
  queued: "排队中",
  starting: "准备中",
  running: "处理中",
  retry_wait: "等待重试",
  paused: "已暂停",
  cancel_requested: "正在取消",
  cancelled: "已取消",
  failed: "失败",
  succeeded: "已完成",
  recognize: "识别",
  reconcile: "校对",
  optimise: "优化",
  compile: "编译",
  done: "完成",
  pending: "待处理",
  validating: "校验 / 待保存",
  completed: "已完成",
  passed: "通过",
  warning: "告警",
  unchecked: "未检查",
};
async function api<T = unknown>(
  url: string,
  method = "GET",
  data?: unknown,
): Promise<T> {
  const response = await fetch("/api" + url, {
    method,
    headers: data ? { "Content-Type": "application/json" } : undefined,
    body: data ? JSON.stringify(data) : undefined,
  });
  if (!response.ok) {
    const err = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw Error(err.detail || "请求失败");
  }
  return response.json();
}
const bytes = (n: number) =>
  n > 1024 ** 3
    ? (n / 1024 ** 3).toFixed(1) + " GB"
    : (n / 1024 ** 2).toFixed(1) + " MB";
const num = (n: number | null | undefined) =>
  n == null ? "未返回" : n.toLocaleString();
const title = (name: string) => name.split("/").pop();
const stamp = (t: number) =>
  new Date(t * 1000).toLocaleString("zh-CN", { hour12: false });
const latestPDF = (j: Job) =>
  [...j.artifacts].reverse().find((a) => a.kind === "pdf");
function Badge({ status }: { status: string }) {
  return (
    <span className={"badge " + status}>
      <i />
      {labels[status] || status}
    </span>
  );
}
function App() {
  const [route, setRoute] = useState(location.pathname),
    [overview, setOverview] = useState<Overview>(),
    [providers, setProviders] = useState<Provider[]>([]),
    [concurrency, setConcurrency] = useState(1),
    [message, setMessage] = useState(""),
    [dark, setDark] = useState(localStorage.getItem("theme") === "dark");
  const navigate = (path: string) => {
    sessionStorage.setItem("scroll:" + route, String(window.scrollY));
    history.pushState({}, "", path);
    setRoute(path);
  };
  useEffect(() => {
    const timer = setTimeout(
      () =>
        window.scrollTo(
          0,
          Number(sessionStorage.getItem("scroll:" + route) || 0),
        ),
      100,
    );
    return () => clearTimeout(timer);
  }, [route]);
  const refresh = async () => {
    try {
      setOverview(await api<Overview>("/overview"));
    } catch (e) {
      setMessage(String(e));
    }
  };
  const loadProviders = async () =>
    setProviders(await api<Provider[]>("/providers"));
  useEffect(() => {
    const pop = () => setRoute(location.pathname);
    window.addEventListener("popstate", pop);
    refresh();
    loadProviders().catch((e) => setMessage(String(e)));
    api<{ page_concurrency: number }>("/config").then((v) =>
      setConcurrency(v.page_concurrency),
    );
    const timer = setInterval(refresh, 2500);
    return () => {
      clearInterval(timer);
      window.removeEventListener("popstate", pop);
    };
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    localStorage.setItem("theme", dark ? "dark" : "light");
  }, [dark]);
  const perform = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      setMessage(String(e));
    }
  };
  const selected = route.startsWith("/artifacts")
    ? ""
    : route.startsWith("/jobs/")
      ? "/queue"
      : route;
  const jobs = overview?.jobs || [],
    active = jobs.find((j) =>
      ["running", "starting", "cancel_requested"].includes(j.status),
    );
  return (
    <div className="shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="/"
          onClick={(e) => {
            e.preventDefault();
            navigate("/");
          }}
        >
          <span className="brand-mark">L</span>Lexiod
          <span className="version">BETA</span>
        </a>
        <div className="workspace-label">文档工作台</div>
        <nav>
          {[
            ["/", "概览", LayoutDashboard],
            ["/queue", "上传与队列", ListOrdered],
            ["/compile", "TeX 编译", FileText],
            ["/batches", "批次与产物", FolderOpen],
            ["/settings", "模型与配置", Settings],
            ["/operations", "运行记录", RefreshCw],
          ].map(([path, text, Icon]) => (
            <button
              key={String(path)}
              className={selected === path ? "nav active" : "nav"}
              onClick={() => navigate(String(path))}
            >
              {React.createElement(Icon as typeof Settings, { size: 17 })}
              {String(text)}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="health">
            <i className={overview?.worker.online ? "online" : ""} />
            {overview?.worker.online ? "执行器在线" : "执行器离线"}
          </div>
          <small>单文件顺序处理 · 缓存可恢复</small>
          <button className="theme" onClick={() => setDark(!dark)}>
            {dark ? <Sun size={16} /> : <Moon size={16} />}{" "}
            {dark ? "浅色外观" : "深色外观"}
          </button>
        </div>
      </aside>
      <main>
        {message && (
          <div role="alert" className="toast">
            {message}
            <button aria-label="关闭提示" onClick={() => setMessage("")}>
              <X size={16} />
            </button>
          </div>
        )}
        {route.startsWith("/artifacts/") ? (
          <Preview id={route.split("/")[2]} navigate={navigate} />
        ) : route.startsWith("/jobs/") ? (
          <JobDetail
            id={route.split("/")[2]}
            navigate={navigate}
            perform={perform}
          />
        ) : route === "/operations" ? (
          <Operations />
        ) : route === "/settings" ? (
          <SettingsPage
            providers={providers}
            reload={loadProviders}
            perform={perform}
            concurrency={concurrency}
            setConcurrency={setConcurrency}
          />
        ) : route === "/compile" ? (
          <>
            <Header
              eyebrow="TEX WORKSPACE"
              title="编译 TeX"
              description="上传源文件或完整项目，直接生成 PDF。无需模型，也不消耗 Token。"
            />
            <UploadPanel
              kind="tex_compile"
              providers={providers}
              concurrency={concurrency}
              done={refresh}
              notify={setMessage}
            />
            <JobTable
              jobs={jobs.filter((j) => j.kind === "tex_compile")}
              navigate={navigate}
            />
          </>
        ) : route === "/queue" ? (
          <>
            <Header
              eyebrow="PROCESSING QUEUE"
              title="上传与队列"
              description="选择文件或文件夹，确认 PDF 清单后依次处理。"
            />
            <UploadPanel
              kind="pdf_convert"
              providers={providers}
              concurrency={concurrency}
              done={refresh}
              notify={setMessage}
            />
            <div className="section-title">
              <h2>
                文件队列 <span>{jobs.length}</span>
              </h2>
              <button
                onClick={() =>
                  perform(() =>
                    api("/queue", "POST", { paused: !overview?.paused }),
                  )
                }
              >
                {overview?.paused ? <Play size={15} /> : <Pause size={15} />}{" "}
                {overview?.paused ? "恢复队列" : "当前文件完成后暂停"}
              </button>
            </div>
            {overview?.paused && (
              <div className="notice">
                队列已暂停。恢复后将继续处理排队文件。
              </div>
            )}
            <JobTable jobs={jobs} navigate={navigate} />
          </>
        ) : route === "/batches" ? (
          <>
            <Header
              eyebrow="DOCUMENT LIBRARY"
              title="批次与产物"
              description="按批次查看来源、审核状态与编译结果。"
            />
            {[...new Set(jobs.map((j) => j.batch || "未分批次"))].map(
              (batch) => (
                <section key={batch}>
                  <div className="section-title">
                    <h2>{batch}</h2>
                    {jobs.some(
                      (j) =>
                        (j.batch || "未分批次") === batch &&
                        j.artifacts.length > 0,
                    ) && (
                      <a
                        className="button"
                        href={
                          "/api/batches/download?batch=" +
                          encodeURIComponent(batch === "未分批次" ? "" : batch)
                        }
                      >
                        <Download size={15} />
                        下载批次 ZIP
                      </a>
                    )}
                    <span className="muted">
                      {
                        jobs.filter((j) => (j.batch || "未分批次") === batch)
                          .length
                      }{" "}
                      个文件
                    </span>
                  </div>
                  <JobTable
                    jobs={jobs.filter((j) => (j.batch || "未分批次") === batch)}
                    navigate={navigate}
                  />
                </section>
              ),
            )}
            {!jobs.length && (
              <Empty text="提交第一批文件后，产物会显示在这里。" />
            )}
            <LegacyLibrary navigate={navigate} perform={perform} />
          </>
        ) : (
          <>
            <Header
              eyebrow="OVERVIEW"
              title="文档处理，一目了然"
              description="从原始 PDF 到可编辑 TeX，追踪每一页的进度与结果。"
              action={
                <button className="primary" onClick={() => navigate("/queue")}>
                  <Plus size={16} />
                  上传文件
                </button>
              }
            />
            <div className="metrics">
              <Metric
                label="正在处理"
                value={active ? "1" : "0"}
                hint="同时处理一个文件"
              />
              <Metric
                label="等待处理"
                value={String(jobs.filter((j) => j.status === "queued").length)}
                hint={overview?.paused ? "队列已暂停" : "按提交顺序执行"}
              />
              <Metric
                label="已完成"
                value={String(
                  jobs.filter((j) => j.status === "succeeded").length,
                )}
                hint="可预览 PDF 与下载产物"
              />
              <Metric
                label="可用存储"
                value={overview ? bytes(overview.disk.free) : "—"}
                hint={overview ? "共 " + bytes(overview.disk.total) : "读取中"}
              />
            </div>
            <div className="section-title">
              <h2>当前任务</h2>
              <span className="muted">每 2.5 秒更新</span>
            </div>
            {active ? (
              <div
                className="card current"
                onClick={() => navigate("/jobs/" + active.id)}
              >
                <div className="file-icon">
                  <FileText />
                </div>
                <div>
                  <h3>{title(active.name)}</h3>
                  <p>
                    {active.unit} {active.batch || "未分批次"}
                  </p>
                </div>
                <Badge status={active.status} />
                <ChevronRight size={18} />
              </div>
            ) : (
              <Empty
                text={
                  overview?.worker.online
                    ? "准备就绪，上传文件即可开始。"
                    : "执行器尚未连接，已提交的任务会安全保留。"
                }
              />
            )}{" "}
            {overview?.worker.error && (
              <div className="notice">{overview.worker.error}</div>
            )}
            <div className="section-title">
              <h2>最近任务</h2>
              <button
                className="text-button"
                onClick={() => navigate("/queue")}
              >
                查看全部 →
              </button>
            </div>
            <JobTable
              jobs={[...jobs].reverse().slice(0, 8)}
              navigate={navigate}
            />
          </>
        )}
      </main>
    </div>
  );
}
function Header({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <header>
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </header>
  );
}
function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="card metric">
      <label>{label}</label>
      <strong>{value}</strong>
      <small>{hint}</small>
    </div>
  );
}
function Empty({ text }: { text: string }) {
  return (
    <div className="card empty">
      <FileText size={26} />
      <p>{text}</p>
    </div>
  );
}
function JobTable({
  jobs,
  navigate,
}: {
  jobs: Job[];
  navigate: (path: string) => void;
}) {
  if (!jobs.length)
    return <Empty text="还没有文件。上传后，处理记录会保存在这里。" />;
  return (
    <div className="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>文件</th>
            <th>批次</th>
            <th>状态</th>
            <th>类型</th>
            <th>产物</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => {
            const pdf = latestPDF(job);
            return (
              <tr key={job.id}>
                <td>
                  <button
                    className="filename"
                    onClick={() =>
                      navigate(
                        pdf
                          ? "/artifacts/" + pdf.id + "/preview"
                          : "/jobs/" + job.id,
                      )
                    }
                  >
                    <FileText size={16} />
                    {title(job.name)}
                  </button>
                  <small className="filepath">{job.name}</small>
                </td>
                <td>
                  {job.unit} {job.batch || "—"}
                </td>
                <td>
                  <Badge status={job.status} />
                </td>
                <td>{job.kind === "tex_compile" ? "TeX 编译" : "PDF 转换"}</td>
                <td>
                  <div className="row-actions">
                    <button
                      className="text-button"
                      onClick={() => navigate("/jobs/" + job.id)}
                    >
                      详情
                    </button>
                    {pdf && (
                      <button
                        className="text-button"
                        onClick={() =>
                          navigate("/artifacts/" + pdf.id + "/preview")
                        }
                      >
                        查看 PDF
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
function ModelFields({
  value,
  onChange,
  providers,
  label,
}: {
  value: Model;
  onChange: (v: Model) => void;
  providers: Provider[];
  label: string;
}) {
  const provider = providers.find((p) => p.id === value.provider);
  return (
    <div className="model-row">
      <label>
        {label}
        <select
          aria-label={label + "供应商"}
          value={value.provider}
          onChange={(e) => onChange({ provider: e.target.value, model: "" })}
        >
          <option value="">选择供应商</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        模型
        <input
          aria-label={label + "模型"}
          list={"models-" + label}
          value={value.model}
          onChange={(e) => onChange({ ...value, model: e.target.value })}
          placeholder="选择或输入模型 ID"
        />
        <datalist id={"models-" + label}>
          {provider?.models.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
      </label>
    </div>
  );
}
function UploadPanel({
  kind,
  providers,
  concurrency,
  done,
  notify,
}: {
  kind: string;
  providers: Provider[];
  concurrency: number;
  done: () => Promise<void>;
  notify: (text: string) => void;
}) {
  const [files, setFiles] = useState<
      { file: File; path: string; selected: boolean }[]
    >([]),
    [main, setMain] = useState(""),
    [busy, setBusy] = useState(false),
    [progress, setProgress] = useState(""),
    [unit, setUnit] = useState("U1"),
    [templates, setTemplates] = useState<Template[]>([]),
    [templateName, setTemplateName] = useState(""),
    [batch, setBatch] = useState(""),
    [pages, setPages] = useState(concurrency),
    [model, setModel] = useState<Model>({ provider: "", model: "" }),
    [reconcile, setReconcile] = useState<Model>({ provider: "", model: "" }),
    [optimise, setOptimise] = useState<Model>({ provider: "", model: "" });
  const input = useRef<HTMLInputElement>(null),
    folder = useRef<HTMLInputElement>(null),
    attempt = useRef(crypto.randomUUID());
  useEffect(() => setPages(concurrency), [concurrency]);
  useEffect(() => {
    attempt.current = crypto.randomUUID();
  }, [files, main, unit, batch, pages, model, reconcile, optimise]);
  useEffect(() => {
    api<Template[]>("/templates")
      .then(setTemplates)
      .catch(() => {});
  }, []);
  const add = (list: FileList | null) => {
    if (!list) return;
    const all = Array.from(list)
      .filter((f) => kind === "tex_compile" || /\.pdf$/i.test(f.name))
      .map((file) => ({
        file,
        path: file.webkitRelativePath || file.name,
        selected: true,
      }))
      .sort((a, b) => a.path.localeCompare(b.path, "zh-CN", { numeric: true }));
    setFiles(all);
    attempt.current = crypto.randomUUID();
    const tex = all.filter((f) => /\.tex$/i.test(f.path));
    setMain(tex.length === 1 ? tex[0].path : "");
    if (tex.length > 1)
      Promise.all(
        tex.map(async (f) => ({
          path: f.path,
          isMain: /\\documentclass\s*(?:\[[^\]]*\])?\s*\{/.test(
            await f.file.slice(0, 262144).text(),
          ),
        })),
      ).then((items) => {
        const candidates = items.filter((i) => i.isMain);
        if (candidates.length === 1) setMain(candidates[0].path);
      });
    if (!all.length)
      notify(
        kind === "pdf_convert"
          ? "所选文件夹中没有 PDF 文件"
          : "所选项目中没有文件",
      );
  };
  const submit = async () => {
    setBusy(true);
    try {
      const selected = files.filter((f) => f.selected);
      if (!selected.length) throw Error("请选择文件");
      if (kind === "pdf_convert" && (!model.provider || !model.model.trim()))
        throw Error("请先选择供应商和模型");
      if (kind === "tex_compile" && !main) throw Error("请先选择主 TeX 文件");
      const ids = [];
      for (const [index, f] of selected.entries()) {
        setProgress(`校验文件 ${index + 1}/${selected.length} · ${f.path}`);
        const hasher = sha256.create();
        for (let start = 0; start < f.file.size; start += 4 * 1024 ** 2)
          hasher.update(
            new Uint8Array(
              await f.file.slice(start, start + 4 * 1024 ** 2).arrayBuffer(),
            ),
          );
        const digest = bytesToHex(hasher.digest());
        const key = "upload:" + f.path + ":" + digest;
        let record:
          { id: string; offset: number; complete: boolean } | undefined;
        const old = localStorage.getItem(key);
        if (old)
          try {
            record = await api("/uploads/" + old);
          } catch {}
        if (!record) {
          record = await api("/uploads", "POST", {
            path: f.path,
            size: f.file.size,
            sha256: digest,
          });
          localStorage.setItem(key, record!.id);
        }
        let offset = record!.offset;
        while (!record!.complete && offset < f.file.size) {
          const chunk = f.file.slice(offset, offset + 4 * 1024 ** 2);
          const res = await fetch(
            "/api/uploads/" + record!.id + "?offset=" + offset,
            { method: "PUT", body: chunk },
          );
          if (!res.ok) {
            const err = await res.json();
            throw Error(err.detail);
          }
          offset = (await res.json()).offset;
          setProgress(
            `${index + 1}/${selected.length} · ${f.path} · ${Math.round((offset / f.file.size) * 100)}%`,
          );
        }
        if (!record!.complete)
          await api("/uploads/" + record!.id + "/finish", "POST");
        ids.push(record!.id);
      }
      await api("/jobs", "POST", {
        kind,
        uploads: ids,
        main_file: main,
        unit,
        batch,
        request_id: attempt.current,
        config: {
          page_concurrency: pages,
          recognize: model,
          reconcile: reconcile.provider ? reconcile : model,
          optimise: optimise.provider ? optimise : model,
        },
      });
      setFiles([]);
      setProgress("");
      attempt.current = crypto.randomUUID();
      await done();
      notify("已加入队列，将逐个处理。");
    } catch (e) {
      notify(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card upload-panel">
      <fieldset disabled={busy}>
        <input
          ref={input}
          type="file"
          multiple
          accept={kind === "pdf_convert" ? ".pdf" : undefined}
          hidden
          onChange={(e) => add(e.target.files)}
        />
        <input
          ref={folder}
          type="file"
          multiple
          {...{ webkitdirectory: "" }}
          hidden
          onChange={(e) => add(e.target.files)}
        />
        <div
          className="dropzone"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            add(e.dataTransfer.files);
          }}
        >
          <div className="upload-icon">
            <Upload size={24} />
          </div>
          <h3>
            {kind === "pdf_convert"
              ? "将 PDF 拖到这里，或选择整个文件夹"
              : "上传 TeX 源文件或完整项目"}
          </h3>
          <p>
            {kind === "pdf_convert"
              ? "递归列出文件夹中的全部 PDF，确认清单后上传"
              : "保留图片、样式和子文件目录，选择主文件后编译"}
          </p>
          <div className="buttons">
            <button disabled={busy} onClick={() => input.current?.click()}>
              <Plus size={15} />
              选择文件
            </button>
            <button disabled={busy} onClick={() => folder.current?.click()}>
              <FolderOpen size={15} />
              选择文件夹
            </button>
          </div>
        </div>
        {files.length > 0 && (
          <>
            <div className="section-title">
              <h3>文件清单 · {files.filter((f) => f.selected).length} 个</h3>
              <span className="muted">
                {bytes(
                  files
                    .filter((f) => f.selected)
                    .reduce((n, f) => n + f.file.size, 0),
                )}
              </span>
            </div>
            <div className="file-list">
              {files.map((f, index) => (
                <div className="upload-file" key={f.path}>
                  <input
                    type="checkbox"
                    checked={f.selected}
                    disabled={busy}
                    onChange={(e) =>
                      setFiles(
                        files.map((x, i) =>
                          i === index
                            ? { ...x, selected: e.target.checked }
                            : x,
                        ),
                      )
                    }
                  />
                  <span>{f.path}</span>
                  <small>{bytes(f.file.size)}</small>
                  <button
                    aria-label={"上移 " + f.path}
                    disabled={busy || index === 0}
                    onClick={() => {
                      const next = [...files];
                      [next[index - 1], next[index]] = [
                        next[index],
                        next[index - 1],
                      ];
                      setFiles(next);
                    }}
                  >
                    <ArrowUp size={14} />
                  </button>
                </div>
              ))}
            </div>
            <div className="form-grid">
              <label>
                单元
                <select value={unit} onChange={(e) => setUnit(e.target.value)}>
                  {["U1", "U2", "U3"].map((u) => (
                    <option key={u}>{u}</option>
                  ))}
                </select>
              </label>
              <label>
                批次号
                <input
                  value={batch}
                  onChange={(e) => setBatch(e.target.value)}
                  placeholder="可选，例如 A33Z2202410007"
                />
              </label>
            </div>
            {kind === "tex_compile" ? (
              <label>
                主 TeX 文件
                <select
                  aria-label="主 TeX 文件"
                  value={main}
                  onChange={(e) => setMain(e.target.value)}
                >
                  <option value="">请选择主文件</option>
                  {files
                    .filter((f) => f.selected && /\.tex$/i.test(f.path))
                    .map((f) => (
                      <option key={f.path}>{f.path}</option>
                    ))}
                </select>
              </label>
            ) : (
              <>
                <div className="form-grid">
                  <label>
                    配置模板
                    <select
                      defaultValue=""
                      onChange={(e) => {
                        const t = templates.find(
                          (t) => t.id === e.target.value,
                        );
                        if (t) {
                          setModel(t.config.recognize);
                          setReconcile(t.config.reconcile);
                          setOptimise(t.config.optimise);
                          setPages(t.config.page_concurrency);
                        }
                      }}
                    >
                      <option value="">自定义配置</option>
                      {templates.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    保存当前配置为模板
                    <div className="inline-form">
                      <input
                        value={templateName}
                        onChange={(e) => setTemplateName(e.target.value)}
                        placeholder="模板名称"
                      />
                      <button
                        onClick={async () => {
                          try {
                            await api("/templates", "POST", {
                              name: templateName,
                              config: {
                                page_concurrency: pages,
                                recognize: model,
                                reconcile: reconcile.provider
                                  ? reconcile
                                  : model,
                                optimise: optimise.provider ? optimise : model,
                              },
                            });
                            setTemplates(await api<Template[]>("/templates"));
                            notify("模板已保存");
                          } catch (e) {
                            notify(String(e));
                          }
                        }}
                      >
                        保存
                      </button>
                    </div>
                  </label>
                </div>
                <ModelFields
                  label="识别"
                  value={model}
                  onChange={setModel}
                  providers={providers}
                />
                {!providers.length && (
                  <p className="notice">
                    请先在「模型与配置」中添加供应商和 API Key。
                  </p>
                )}
                <div className="form-grid">
                  <label>
                    识别页并发
                    <input
                      aria-label="识别页并发"
                      type="number"
                      min="1"
                      max="8"
                      value={pages}
                      onChange={(e) => setPages(Number(e.target.value))}
                    />
                    <small>一份文件内同时识别的页数</small>
                  </label>
                  <div className="field-hint">
                    文件始终按队列顺序，逐个处理。
                  </div>
                </div>
                <details>
                  <summary>高级模型配置</summary>
                  <ModelFields
                    label="校对"
                    value={reconcile}
                    onChange={setReconcile}
                    providers={providers}
                  />
                  <ModelFields
                    label="优化"
                    value={optimise}
                    onChange={setOptimise}
                    providers={providers}
                  />
                  <small>留空时使用识别模型。</small>
                </details>
              </>
            )}
            <div className="submit-row">
              <span className="muted">
                {progress || "确认顺序后，文件将加入持久队列"}
              </span>
              <button className="primary" disabled={busy} onClick={submit}>
                {busy ? (
                  <Loader className="spin" size={16} />
                ) : (
                  <Play size={16} />
                )}{" "}
                {busy ? "正在上传" : "上传并加入队列"}
              </button>
            </div>
          </>
        )}
      </fieldset>
    </section>
  );
}
function SettingsPage({
  providers,
  reload,
  perform,
  concurrency,
  setConcurrency,
}: {
  providers: Provider[];
  reload: () => Promise<void>;
  perform: (fn: () => Promise<unknown>) => Promise<void>;
  concurrency: number;
  setConcurrency: (n: number) => void;
}) {
  const [name, setName] = useState("OpenAI"),
    [base, setBase] = useState("https://api.openai.com/v1"),
    [key, setKey] = useState(""),
    [result, setResult] = useState("");
  return (
    <>
      <Header
        eyebrow="MODEL SETTINGS"
        title="模型与配置"
        description="选择供应商、配置模型与 API Key。已启动的任务保留自己的配置。"
      />
      <section className="card form-card">
        <h2>添加供应商</h2>
        <div className="form-grid">
          <label>
            供应商
            <select
              aria-label="供应商"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                setBase(
                  e.target.value === "OpenAI"
                    ? "https://api.openai.com/v1"
                    : e.target.value === "DeepSeek"
                      ? "https://api.deepseek.com/v1"
                      : "",
                );
              }}
            >
              {["OpenAI", "DeepSeek", "自定义兼容服务"].map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label>
            Base URL
            <input value={base} onChange={(e) => setBase(e.target.value)} />
          </label>
        </div>
        <label>
          API Key
          <input
            type="password"
            autoComplete="off"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="输入后仅保存在服务端"
          />
        </label>
        <div className="submit-row">
          <small>支持 OpenAI 兼容协议。识别阶段需要视觉模型。</small>
          <button
            className="primary"
            onClick={() =>
              perform(async () => {
                await api("/providers", "POST", { name, base_url: base, key });
                setKey("");
                await reload();
                setResult("供应商已保存");
              })
            }
          >
            保存供应商
          </button>
        </div>
      </section>
      <section className="card form-card">
        <h2>已配置的供应商</h2>
        {providers.map((p) => (
          <div className="provider" key={p.id}>
            <div>
              <strong>{p.name}</strong>
              <small>{p.base_url} · Key 已配置</small>
            </div>
            <button
              onClick={() =>
                perform(async () => {
                  const res = await api<{ message: string }>(
                    "/providers/" + p.id + "/test",
                    "POST",
                  );
                  setResult(res.message);
                  await reload();
                })
              }
            >
              验证连接 / 获取模型
            </button>
            <button
              aria-label={"删除 " + p.name}
              onClick={() =>
                perform(async () => {
                  await api("/providers/" + p.id, "DELETE");
                  await reload();
                })
              }
            >
              <X size={15} />
            </button>
          </div>
        ))}
        {!providers.length && (
          <p className="muted">还没有供应商，纯 TeX 编译可以直接使用。</p>
        )}
        {result && <p role="status">{result}</p>}
      </section>
      <section className="card form-card">
        <h2>默认参数</h2>
        <label>
          识别页并发
          <input
            type="number"
            min="1"
            max="8"
            value={concurrency}
            onChange={(e) => setConcurrency(Number(e.target.value))}
          />
        </label>
        <div className="submit-row">
          <small>只影响新提交的任务；同时处理文件数固定为 1。</small>
          <button
            onClick={() =>
              perform(() =>
                api("/config", "PUT", { page_concurrency: concurrency }),
              )
            }
          >
            保存默认值
          </button>
        </div>
      </section>
    </>
  );
}
function JobDetail({
  id,
  navigate,
  perform,
}: {
  id: string;
  navigate: (p: string) => void;
  perform: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [job, setJob] = useState<Detail>(),
    [rows, setRows] = useState<PageRow[]>([]),
    [tab, setTab] = useState("页面"),
    [filter, setFilter] = useState("all"),
    [logs, setLogs] = useState(""),
    [tex, setTex] = useState(""),
    [publication, setPublication] = useState<{
      enabled: boolean;
      requests: {
        id: string;
        action: string;
        status: string;
        error?: string;
        result?: unknown;
      }[];
    }>(),
    [events, setEvents] = useState<
      {
        id: number;
        event: string;
        created: number;
        phase?: string;
        status?: string;
      }[]
    >([]);
  const refresh = async () => {
    const j = await api<Detail>("/jobs/" + id);
    setJob(j);
    setRows(j.page_rows);
    setPublication(await api("/jobs/" + id + "/publication"));
  };
  useEffect(() => {
    refresh();
    const t = setInterval(() => refresh().catch(() => {}), 2500);
    return () => clearInterval(t);
  }, [id]);
  useEffect(() => {
    if (tab === "TeX") {
      const artifact = job?.artifacts.filter((a) => a.kind === "tex").at(-1);
      const url = artifact
        ? "/api/artifacts/" + artifact.id + "/file"
        : job?.kind === "tex_compile"
          ? "/api/jobs/" + id + "/source"
          : null;
      if (url)
        fetch(url)
          .then((r) => r.text())
          .then(setTex);
      else setTex("尚未生成 TeX");
    }
    if (tab === "日志") {
      const read = () => {
        api<{ text: string }>("/jobs/" + id + "/logs")
          .then((r) => setLogs(r.text))
          .catch((e) => setLogs(String(e)));
        api<typeof events>("/jobs/" + id + "/events")
          .then(setEvents)
          .catch(() => {});
      };
      read();
      const timer = setInterval(read, 2500);
      return () => clearInterval(timer);
    }
  }, [tab, id, job?.version]);
  if (!job) return <Empty text="读取任务…" />;
  const pdf = latestPDF(job),
    command = (action: string) =>
      perform(async () => {
        await api("/jobs/" + id + "/command", "POST", {
          action,
          version: job.version,
        });
        if (action === "retry") await api("/queue", "POST", { paused: false });
        await refresh();
      });
  return (
    <>
      <button className="text-button back" onClick={() => navigate("/queue")}>
        <ArrowLeft size={15} />
        返回队列
      </button>
      <Header
        eyebrow={
          job.kind === "tex_compile" ? "TEX COMPILATION" : "DOCUMENT DETAILS"
        }
        title={title(job.name) || ""}
        description={`${job.unit} / ${job.batch || "未分批次"} · ${stamp(job.created_at)}`}
        action={<Badge status={job.status} />}
      />
      <div className="buttons job-actions">
        {job.artifacts
          .filter((a) => a.kind === "tex")
          .slice(-1)
          .map((a) => (
            <a
              key={a.id}
              className="button"
              href={"/api/artifacts/" + a.id + "/file?download=true"}
            >
              <Download size={15} />
              下载 TeX
            </a>
          ))}
        {pdf && (
          <button
            className="primary"
            onClick={() => navigate("/artifacts/" + pdf.id + "/preview")}
          >
            <FileText size={16} />
            查看 PDF
          </button>
        )}
        {["failed", "cancelled", "paused"].includes(job.status) && (
          <button onClick={() => command("retry")}>
            <RefreshCw size={16} />
            重试并恢复队列
          </button>
        )}
        {job.status === "queued" && (
          <>
            <button onClick={() => command("pause")}>
              <Pause size={15} />
              暂停
            </button>
            <button onClick={() => command("first")}>
              <ArrowUp size={15} />
              移到队首
            </button>
          </>
        )}
        {["queued", "running", "starting", "paused", "retry_wait"].includes(
          job.status,
        ) && (
          <button onClick={() => command("cancel")}>
            <X size={15} />
            取消任务
          </button>
        )}
        {job.status === "succeeded" && (
          <button
            onClick={() =>
              perform(async () => {
                await api("/jobs/" + id + "/recompile", "POST", {
                  request_id: crypto.randomUUID(),
                });
                navigate("/compile");
              })
            }
          >
            重新编译
          </button>
        )}
        <a className="button" href={"/api/jobs/" + id + "/download"}>
          <Download size={15} />
          下载项目
        </a>
      </div>
      {job.error && <div className="notice error">{job.error}</div>}
      {job.next_retry_at ? (
        <div className="notice">
          第 {job.restarts} 次自动重启 · {stamp(job.next_retry_at)}
        </div>
      ) : null}
      <div className="card stage-bar">
        {(job.kind === "tex_compile"
          ? ["compile"]
          : ["recognize", "reconcile", "optimise", "compile"]
        ).map((s) => (
          <div
            key={s}
            className={
              job.progress.stage === s ? "stage current-stage" : "stage"
            }
          >
            {job.progress.stage === s ? (
              <Loader size={15} className="spin" />
            ) : (
              <span className="stage-dot" />
            )}
            {labels[s]}
            {s === "compile" && job.progress.compile_pass
              ? ` ${job.progress.compile_pass}/2`
              : ""}
          </div>
        ))}
      </div>
      <div className="metrics">
        <Metric
          label="识别完成"
          value={
            job.kind === "tex_compile"
              ? "—"
              : `${job.completed_pages} / ${rows.length}`
          }
          hint={job.kind === "tex_compile" ? "纯编译任务" : "按源 PDF 页数统计"}
        />
        <Metric
          label="输入 Token"
          value={num(job.usage.input_tokens)}
          hint={
            job.usage.input_tokens == null
              ? "已知部分 " + num(job.usage.known_input_tokens)
              : "包含重试调用"
          }
        />
        <Metric
          label="输出 Token"
          value={num(job.usage.output_tokens)}
          hint={
            job.kind === "tex_compile" ? "编译不调用模型" : "按服务返回统计"
          }
        />
        <Metric
          label="模型调用"
          value={num(job.usage.calls)}
          hint={`其中重试 ${job.usage.retry_calls || 0} 次`}
        />
      </div>
      <p className="muted">
        已运行{" "}
        {job.started_at
          ? Math.max(
              0,
              Math.round(
                ((job.finished_at || Date.now() / 1000) - job.started_at) / 60,
              ),
            ) + " 分钟"
          : "—"}{" "}
        · 费用估算：
        {job.cost?.priced_calls
          ? "$" +
            Number(job.cost.estimated_usd_low).toFixed(4) +
            "–$" +
            Number(job.cost.estimated_usd_high).toFixed(4)
          : job.kind === "tex_compile"
            ? "$0（纯编译）"
            : "暂无可用价格"}
        {job.cost?.unpriced_calls
          ? ` · ${job.cost.unpriced_calls} 次调用未计价`
          : ""}
      </p>
      {job.recognition_eta_seconds != null && (
        <p className="muted">
          识别阶段预计还需约 {Math.ceil(job.recognition_eta_seconds / 60)}{" "}
          分钟；校对、优化与编译另计。
        </p>
      )}
      <div className="tabs">
        {["页面", "TeX", "检查结果", "调用记录", "配置", "日志"].map((t) => (
          <button
            key={t}
            className={t === tab ? "selected" : ""}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === "页面" ? (
        <>
          <div className="section-title">
            <h2>源页状态</h2>
            <select
              aria-label="页状态筛选"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            >
              {[
                ["all", "全部"],
                ["running", "处理中"],
                ["failed", "失败"],
              ].map(([v, l]) => (
                <option value={v} key={v}>
                  {l}
                </option>
              ))}
            </select>
          </div>
          {job.kind === "tex_compile" ? (
            <Empty text="此任务直接编译 TeX，编译结果可在 PDF 预览中查看。" />
          ) : (
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>源页</th>
                    <th>状态</th>
                    <th>请求模型</th>
                    <th>Token</th>
                    <th>问题</th>
                  </tr>
                </thead>
                <tbody>
                  {rows
                    .filter((p) => filter === "all" || p.status === filter)
                    .map((p) => (
                      <tr key={p.page}>
                        <td>
                          <a
                            target="_blank"
                            rel="noreferrer"
                            href={"/api/jobs/" + id + "/source#page=" + p.page}
                          >
                            第 {p.page} 页 ↗
                          </a>
                        </td>
                        <td>
                          <Badge status={p.status} />
                          <small>
                            {p.stage ? labels[p.stage] || p.stage : ""}
                          </small>
                        </td>
                        <td>
                          <code>{p.model || "—"}</code>
                        </td>
                        <td>{num(p.tokens)}</td>
                        <td>{p.error || "—"}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : tab === "检查结果" ? (
        <div className="card form-card">
          {job.checks.map((c) => (
            <div className="check" key={c.name}>
              <div>
                <strong>{c.name}</strong>
                {c.details?.map((d, i) => (
                  <pre key={i}>{d}</pre>
                ))}
              </div>
              <Badge status={c.status} />
            </div>
          ))}
          {job.compile_errors.map((e, i) => (
            <pre className="error-text" key={i}>
              {e}
            </pre>
          ))}
          <p className="muted">
            编译通过不代表内容完全正确。建议对照原文检查数学符号与版面。
          </p>
        </div>
      ) : tab === "调用记录" ? (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>阶段 / 页码</th>
                <th>请求模型</th>
                <th>状态</th>
                <th>输入 / 输出</th>
                <th>耗时</th>
              </tr>
            </thead>
            <tbody>
              {job.calls.map((c) => (
                <tr key={c.call_id}>
                  <td>
                    {labels[c.stage] || c.stage} · {c.page || "文件级"}
                  </td>
                  <td>
                    <code>{c.model}</code>
                    <small>第 {c.attempt} 次尝试</small>
                  </td>
                  <td>{c.status}</td>
                  <td>
                    {num(c.usage?.input_tokens)} / {num(c.usage?.output_tokens)}
                    <details>
                      <summary>原始用量</summary>
                      <pre>{JSON.stringify(c.usage_raw, null, 2)}</pre>
                    </details>
                  </td>
                  <td>{c.seconds?.toFixed(1)}s</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!job.calls.length && (
            <p className="muted padding">
              暂无模型调用；纯编译任务不消耗 Token。
            </p>
          )}
        </div>
      ) : tab === "TeX" ? (
        <div className="card form-card">
          <h3>TeX 源码</h3>
          <pre className="tex-source">
            {tex.split("\n").map((line, i) => (
              <div id={"line-" + (i + 1)} key={i}>
                <span className="line-number">{i + 1}</span>
                {line || " "}
              </div>
            ))}
          </pre>
        </div>
      ) : tab === "配置" ? (
        <div className="card form-card">
          <h3>Overleaf 发布</h3>
          <p className="muted">
            {publication?.enabled
              ? "先拉取最新内容；仅新增文件，同名冲突停止。"
              : "未配置 Web 发布入口。现有服务器发布方式保持不变。"}
          </p>
          <div className="buttons job-actions">
            {["preflight", "publish"].map((action) => (
              <button
                key={action}
                disabled={!publication?.enabled || job.status !== "succeeded"}
                onClick={() =>
                  perform(async () => {
                    await api("/jobs/" + id + "/publication", "POST", {
                      action,
                    });
                    await refresh();
                  })
                }
              >
                {action === "preflight" ? "发布预检" : "新增到 Overleaf"}
              </button>
            ))}
          </div>
          {publication?.requests.map((r) => (
            <div key={r.id}>
              <Badge status={r.status} />
              <span> {r.action === "preflight" ? "预检" : "发布"}</span>
              {r.error && <p>{r.error}</p>}
              {r.result != null && (
                <pre>{JSON.stringify(r.result, null, 2)}</pre>
              )}
            </div>
          ))}
          {job.kind === "pdf_convert" &&
            job.attempt === 0 &&
            ["queued", "paused"].includes(job.status) && (
              <label>
                此任务的识别页并发
                <input
                  type="number"
                  min="1"
                  max="8"
                  defaultValue={Number(job.config.page_concurrency)}
                  onBlur={(e) => {
                    const n = Number(e.target.value);
                    if (n !== job.config.page_concurrency)
                      perform(async () => {
                        await api("/jobs/" + id + "/config", "PUT", {
                          version: job.version,
                          page_concurrency: n,
                        });
                        await refresh();
                      });
                  }}
                />
              </label>
            )}
          <h3>启动配置快照</h3>
          <pre>{JSON.stringify(job.config, null, 2)}</pre>
          <label>
            审核状态
            <select
              value={job.review}
              onChange={(e) =>
                perform(async () => {
                  await api("/jobs/" + id + "/review", "POST", {
                    review: e.target.value,
                  });
                  await refresh();
                })
              }
            >
              <option value="pending">待审核</option>
              <option value="approved">已审核</option>
              <option value="needs_changes">需要修改</option>
            </select>
          </label>
        </div>
      ) : (
        <div className="card form-card">
          <h3>运行事件</h3>
          {events.map((e) => (
            <p key={e.id}>
              {stamp(e.created)} · {e.event} {e.phase} {e.status}
            </p>
          ))}
          <h3>运行日志</h3>
          <pre className="log">{logs || "暂无日志"}</pre>
        </div>
      )}
    </>
  );
}
function LegacyLibrary({
  navigate,
  perform,
}: {
  navigate: (path: string) => void;
  perform: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [items, setItems] = useState<
      { id: string; unit: string; batch: string; name: string; size: number }[]
    >([]),
    [query, setQuery] = useState(""),
    [source, setSource] = useState("");
  useEffect(() => {
    api<typeof items>("/library").then(setItems);
  }, []);
  return (
    <>
      <div className="section-title">
        <h2>历史 TeX · 只读索引</h2>
        <input
          className="library-search"
          aria-label="搜索历史批次"
          placeholder="搜索批次或文件名"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {!items.length ? (
        <Empty text="在部署配置中添加历史产物目录后，可按批次查看并编译副本。" />
      ) : (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>文件</th>
                <th>来源 / 批次</th>
                <th>大小</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items
                .filter((x) =>
                  (x.unit + " " + x.batch + " " + x.name)
                    .toLowerCase()
                    .includes(query.toLowerCase()),
                )
                .map((x) => (
                  <tr key={x.id}>
                    <td>
                      <button
                        className="filename"
                        onClick={() =>
                          perform(async () =>
                            setSource(
                              await (
                                await fetch("/api/library/" + x.id + "/source")
                              ).text(),
                            ),
                          )
                        }
                      >
                        {title(x.name)}
                      </button>
                      <small>{x.name}</small>
                    </td>
                    <td>
                      {x.unit} / {x.batch}
                    </td>
                    <td>{bytes(x.size)}</td>
                    <td>
                      <button
                        onClick={() =>
                          perform(async () => {
                            const result = await api<Job[]>(
                              "/library/" + x.id + "/compile",
                              "POST",
                              { request_id: crypto.randomUUID() },
                            );
                            navigate("/jobs/" + result[0].id);
                          })
                        }
                      >
                        编译副本
                      </button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
      {source && (
        <section className="card form-card">
          <div className="section-title">
            <h3>原始 TeX（只读）</h3>
            <button onClick={() => setSource("")}>关闭</button>
          </div>
          <pre className="log">{source}</pre>
        </section>
      )}
    </>
  );
}

function Operations() {
  const [data, setData] = useState<{
    notifications_configured: boolean;
    retry_delays: number[];
    daily: {
      date: string;
      converted: number;
      compiled: number;
      pages: number;
      calls: number;
      known_tokens: number;
      unreported_calls: number;
    }[];
    reports: Record<
      string,
      { text?: string; error?: string; updated_at?: number }
    >;
    legacy_queues: {
      path: string;
      status?: string;
      error?: string;
      jobs?: { stem: string; status: string; pages: number }[];
    }[];
  }>();
  const [error, setError] = useState("");
  useEffect(() => {
    const refresh = () =>
      api<NonNullable<typeof data>>("/operations")
        .then(setData)
        .catch((e) => setError(String(e)));
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, []);
  return (
    <>
      <Header
        eyebrow="OPERATIONS"
        title="运行记录"
        description="查看 Web 任务统计、通知配置及外部队列的只读状态。"
      />
      {error && <p role="alert">{error}</p>}
      <section className="card form-card">
        <h2>自动重启与通知</h2>
        <p>邮件通知：{data?.notifications_configured ? "已配置" : "未配置"}</p>
        <p className="muted">
          临时 API 错误按 {data?.retry_delays.map((n) => n / 60).join("、")}{" "}
          分钟重试，每份文件最多 6 次。认证、内存不足和编译错误需要处理。
        </p>
      </section>
      <div className="section-title">
        <h2>Web 任务日报</h2>
        <span className="muted">独立统计，不写入现有服务器账本</span>
      </div>
      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>日期</th>
              <th>转换文件</th>
              <th>编译项目</th>
              <th>源页数</th>
              <th>模型调用</th>
              <th>已知 Token</th>
            </tr>
          </thead>
          <tbody>
            {data?.daily.map((d) => (
              <tr key={d.date}>
                <td>{d.date}</td>
                <td>{d.converted}</td>
                <td>{d.compiled}</td>
                <td>{d.pages}</td>
                <td>{d.calls}</td>
                <td>
                  {num(d.known_tokens)}
                  {d.unreported_calls > 0 ? "（部分未知）" : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="section-title">
        <h2>外部监控与日报</h2>
      </div>
      {data && Object.keys(data.reports).length === 0 && (
        <Empty text="可在部署配置中指定现有监控和日报文件，Web 仅查看，不接管任务。" />
      )}
      {data &&
        Object.entries(data.reports).map(([name, r]) => (
          <section className="card form-card" key={name}>
            <h3>{name === "monitor_snapshot" ? "服务器监控" : "服务器日报"}</h3>
            <small>{r.updated_at ? stamp(r.updated_at) : r.error}</small>
            <pre>{r.text}</pre>
          </section>
        ))}
      {data?.legacy_queues.map((q) => (
        <section className="card form-card" key={q.path}>
          <h3>外部管理 · {q.path.split("/").pop()}</h3>
          <p>{q.error || q.status}</p>
          {q.jobs?.map((j, i) => (
            <p key={i}>
              {j.stem} · {j.pages} 页 · {j.status}
            </p>
          ))}
        </section>
      ))}
    </>
  );
}

function Preview({
  id,
  navigate,
}: {
  id: string;
  navigate: (p: string) => void;
}) {
  const [artifact, setArtifact] = useState<Artifact>(),
    [job, setJob] = useState<Job>(),
    [page, setPage] = useState(1),
    [total, setTotal] = useState(0),
    [scale, setScale] = useState(1.2),
    [error, setError] = useState("");
  const canvas = useRef<HTMLCanvasElement>(null),
    document = useRef<pdfjs.PDFDocumentProxy | null>(null);
  useEffect(() => {
    setPage(1);
    setError("");
    api<Artifact>("/artifacts/" + id)
      .then((a) => {
        setArtifact(a);
        api<Job>("/jobs/" + a.job).then(setJob);
      })
      .catch((e) => setError(String(e)));
    const loading = pdfjs.getDocument("/api/artifacts/" + id + "/file");
    loading.promise
      .then((d) => {
        document.current = d;
        setTotal(d.numPages);
      })
      .catch((e) => setError(String(e)));
    return () => {
      document.current = null;
      void loading.destroy();
    };
  }, [id]);
  useEffect(() => {
    let cancelled = false;
    let task: pdfjs.RenderTask | undefined;
    const render = async () => {
      const doc = document.current;
      if (!doc || !canvas.current) return;
      const p = await doc.getPage(page);
      if (cancelled) return;
      const viewport = p.getViewport({ scale });
      const c = canvas.current;
      const ratio = devicePixelRatio || 1;
      c.width = viewport.width * ratio;
      c.height = viewport.height * ratio;
      c.style.width = viewport.width + "px";
      c.style.height = viewport.height + "px";
      task = p.render({
        canvasContext: c.getContext("2d")!,
        viewport,
        transform: [ratio, 0, 0, ratio, 0, 0],
      });
      await task.promise;
    };
    render().catch((e) => {
      if (e.name !== "RenderingCancelledException") setError(String(e));
    });
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [page, scale, total, id]);
  return (
    <>
      <button
        className="text-button back"
        onClick={() => navigate(artifact ? "/jobs/" + artifact.job : "/queue")}
      >
        <ArrowLeft size={15} />
        返回文件详情
      </button>
      <Header
        eyebrow="PDF PREVIEW"
        title={artifact?.name || "PDF 预览"}
        description={
          artifact
            ? `第 ${artifact.attempt} 次编译 · ${stamp(artifact.created_at)}`
            : "正在读取产物…"
        }
        action={
          <a
            className="button primary"
            href={"/api/artifacts/" + id + "/file?download=true"}
          >
            <Download size={16} />
            下载 PDF
          </a>
        }
      />
      {job && job.attempt !== artifact?.attempt && (
        <div className="notice">
          当前显示历史成功版本。最近任务状态：{labels[job.status]}。
        </div>
      )}
      {error ? (
        <div role="alert" className="notice error">
          无法打开 PDF：{error}
        </div>
      ) : (
        <>
          <div className="card pdf-toolbar">
            <button
              aria-label="上一页"
              disabled={page <= 1}
              onClick={() => setPage(page - 1)}
            >
              <ChevronLeft size={16} />
            </button>
            <input
              aria-label="PDF 页码"
              type="number"
              min="1"
              max={total}
              value={page}
              onChange={(e) =>
                setPage(Math.min(total, Math.max(1, Number(e.target.value))))
              }
            />
            <span>/ {total || "—"}</span>
            <button
              aria-label="下一页"
              disabled={page >= total}
              onClick={() => setPage(page + 1)}
            >
              <ChevronRight size={16} />
            </button>
            <span className="divider" />
            <button
              aria-label="缩小"
              onClick={() => setScale(Math.max(0.5, scale - 0.2))}
            >
              <ZoomOut size={16} />
            </button>
            <span>{Math.round(scale * 100)}%</span>
            <button
              aria-label="放大"
              onClick={() => setScale(Math.min(3, scale + 0.2))}
            >
              <ZoomIn size={16} />
            </button>
          </div>
          <div className={job?.kind === "pdf_convert" ? "comparison" : ""}>
            {job?.kind === "pdf_convert" && (
              <div>
                <h3>原始 PDF</h3>
                <iframe
                  className="source-pdf"
                  title="原始 PDF 对照"
                  src={"/api/jobs/" + job.id + "/source"}
                />
              </div>
            )}
            <div>
              <h3>编译结果</h3>
              <div className="pdf-surface">
                <canvas ref={canvas} />
              </div>
            </div>
          </div>
        </>
      )}
    </>
  );
}
createRoot(document.getElementById("root")!).render(<App />);

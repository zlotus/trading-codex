import {
  AlertCircle,
  ArrowUp,
  BarChart3,
  BriefcaseBusiness,
  CandlestickChart,
  Check,
  Clock3,
  Database,
  FlaskConical,
  History,
  LayoutDashboard,
  ListChecks,
  MessageSquareText,
  Plus,
  ReceiptText,
  RefreshCw,
  Search,
  ShieldCheck,
  SkipForward,
  Sparkles,
  WalletCards,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import {
  getLedgerDashboard,
  getSignalDetail,
  getSystemStatus,
  recordFill,
  skipSignal,
} from "./api";
import type {
  LedgerDashboard,
  PositionView,
  SignalDetail,
  SignalStatus,
  SignalView,
  SystemStatus,
  TrackView,
} from "./types";

type ViewId = "today" | "market" | "portfolio" | "strategies" | "research" | "data";
type AiTab = "summary" | "proposal" | "chat";
type ConnectionState = "connecting" | "online" | "offline";

interface NavItem {
  id: ViewId;
  label: string;
  icon: LucideIcon;
}

const NAV_ITEMS: NavItem[] = [
  { id: "today", label: "今日决策", icon: ListChecks },
  { id: "market", label: "市场状态", icon: BarChart3 },
  { id: "portfolio", label: "组合仓位", icon: BriefcaseBusiness },
  { id: "strategies", label: "策略管理", icon: ShieldCheck },
  { id: "research", label: "回测实验", icon: FlaskConical },
  { id: "data", label: "数据审计", icon: Database },
];

const AI_TABS: Array<{ id: AiTab; label: string }> = [
  { id: "summary", label: "摘要" },
  { id: "proposal", label: "提案" },
  { id: "chat", label: "对话" },
];

const STATUS_LABELS: Record<SignalStatus, string> = {
  active: "待执行",
  partial: "部分成交",
  filled: "已完成",
  skipped: "已跳过",
  expired: "已失效",
};

function formatShanghaiTime(value: Date): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(value);
}

function formatDeadline(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatNumber(value: string | number | null, digits = 2): string {
  if (value === null) return "--";
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return "--";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(parsed);
}

function formatMoney(value: string | null): string {
  return value === null ? "--" : `¥${formatNumber(value)}`;
}

function newIdempotencyKey(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function App() {
  const [activeView, setActiveView] = useState<ViewId>("today");
  const [aiTab, setAiTab] = useState<AiTab>("summary");
  const [query, setQuery] = useState("");
  const [now, setNow] = useState(() => new Date());
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [dashboard, setDashboard] = useState<LedgerDashboard | null>(null);
  const [ledgerLoading, setLedgerLoading] = useState(true);
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [selectedSignalId, setSelectedSignalId] = useState<string | null>(null);
  const [signalDetail, setSignalDetail] = useState<SignalDetail | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const loadStatus = async () => {
      try {
        const status = await getSystemStatus(controller.signal);
        setSystemStatus(status);
        setConnection("online");
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setConnection("offline");
      }
    };
    void loadStatus();
    const timer = window.setInterval(loadStatus, 30_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  const loadLedger = useCallback(async (signal?: AbortSignal) => {
    setLedgerLoading(true);
    try {
      const nextDashboard = await getLedgerDashboard(signal);
      setDashboard(nextDashboard);
      setLedgerError(null);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setLedgerError(error instanceof Error ? error.message : "账本读取失败");
    } finally {
      if (!signal?.aborted) setLedgerLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadLedger(controller.signal);
    const timer = window.setInterval(() => void loadLedger(controller.signal), 15_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [loadLedger]);

  useEffect(() => {
    const signals = dashboard?.signals ?? [];
    if (!signals.length) {
      setSelectedSignalId(null);
      setSignalDetail(null);
      return;
    }
    if (!selectedSignalId || !signals.some((signal) => signal.signal_id === selectedSignalId)) {
      const actionable = signals.find((signal) => ["active", "partial"].includes(signal.status));
      setSelectedSignalId((actionable ?? signals[0]).signal_id);
    }
  }, [dashboard, selectedSignalId]);

  useEffect(() => {
    if (!selectedSignalId) return;
    const controller = new AbortController();
    const loadDetail = async () => {
      try {
        const detail = await getSignalDetail(selectedSignalId, controller.signal);
        setSignalDetail(detail);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setLedgerError(error instanceof Error ? error.message : "信号读取失败");
      }
    };
    void loadDetail();
    return () => controller.abort();
  }, [dashboard?.as_of, selectedSignalId]);

  const handleLedgerMutation = async (detail: SignalDetail) => {
    setSignalDetail(detail);
    setSelectedSignalId(detail.signal.signal_id);
    await loadLedger();
  };

  const viewTitle = useMemo(
    () => NAV_ITEMS.find((item) => item.id === activeView)?.label ?? "今日决策",
    [activeView],
  );

  const submitSearch = (event: FormEvent<HTMLFormElement>) => event.preventDefault();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand" aria-label="Trading Codex">
          <span className="brand-mark"><CandlestickChart size={20} /></span>
          <span className="brand-name">Trading Codex</span>
        </div>

        <nav className="primary-nav" aria-label="主要导航">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                className={activeView === item.id ? "nav-item active" : "nav-item"}
                key={item.id}
                onClick={() => setActiveView(item.id)}
                type="button"
              >
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className={`connection connection-${connection}`}>
          {connection === "online" ? <Wifi size={15} /> : <WifiOff size={15} />}
          <span>{connection === "online" ? "本地服务正常" : connection === "offline" ? "后端离线" : "正在连接"}</span>
        </div>
      </aside>

      <main className="main-workspace">
        <header className="topbar">
          <div className="page-heading">
            <span className="eyebrow">A 股决策工作区</span>
            <h1>{viewTitle}</h1>
          </div>

          <form className="symbol-search" onSubmit={submitSearch} role="search">
            <Search size={17} />
            <input
              aria-label="搜索股票"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="代码、名称或拼音"
              value={query}
            />
          </form>

          <div className="market-clock" title="Asia/Shanghai">
            <Clock3 size={16} />
            <span>{formatShanghaiTime(now)}</span>
          </div>
        </header>

        {activeView === "today" ? (
          <TodayWorkspace
            connection={connection}
            dashboard={dashboard}
            detail={signalDetail}
            error={ledgerError}
            loading={ledgerLoading}
            onMutation={handleLedgerMutation}
            onRefresh={() => loadLedger()}
            onSelectSignal={setSelectedSignalId}
            query={query}
            selectedSignalId={selectedSignalId}
            systemStatus={systemStatus}
          />
        ) : activeView === "portfolio" ? (
          <PortfolioWorkspace dashboard={dashboard} error={ledgerError} />
        ) : (
          <EmptyWorkspace viewTitle={viewTitle} />
        )}
      </main>

      <aside className="ai-panel">
        <header className="ai-header">
          <div>
            <span className="eyebrow">受限决策层</span>
            <h2><Sparkles size={17} /> AI 协作</h2>
          </div>
          <div className="icon-actions">
            <button disabled title="暂无历史" type="button"><History size={17} /></button>
            <button disabled title="暂无决策上下文" type="button"><Plus size={17} /></button>
          </div>
        </header>

        <div className="ai-tabs" role="tablist" aria-label="AI 协作视图">
          {AI_TABS.map((tab) => (
            <button
              aria-selected={aiTab === tab.id}
              className={aiTab === tab.id ? "active" : ""}
              key={tab.id}
              onClick={() => setAiTab(tab.id)}
              role="tab"
              type="button"
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div className="ai-content">
          <div className="ai-empty">
            {aiTab === "summary" ? <Sparkles size={25} /> : aiTab === "proposal" ? <ShieldCheck size={25} /> : <MessageSquareText size={25} />}
            <strong>{aiTab === "summary" ? "暂无决策摘要" : aiTab === "proposal" ? "暂无待审提案" : "暂无对话"}</strong>
            <span>AI 服务尚未配置</span>
          </div>
        </div>

        <footer className="ai-composer">
          <button disabled title="附件尚未启用" type="button"><Plus size={18} /></button>
          <textarea aria-label="AI 对话输入" disabled placeholder="等待决策上下文" rows={1} />
          <button className="send-button" disabled title="AI 服务尚未配置" type="button"><ArrowUp size={18} /></button>
        </footer>
      </aside>
    </div>
  );
}

function TodayWorkspace({
  systemStatus,
  connection,
  dashboard,
  detail,
  selectedSignalId,
  query,
  loading,
  error,
  onSelectSignal,
  onRefresh,
  onMutation,
}: {
  systemStatus: SystemStatus | null;
  connection: ConnectionState;
  dashboard: LedgerDashboard | null;
  detail: SignalDetail | null;
  selectedSignalId: string | null;
  query: string;
  loading: boolean;
  error: string | null;
  onSelectSignal: (signalId: string) => void;
  onRefresh: () => Promise<void>;
  onMutation: (detail: SignalDetail) => Promise<void>;
}) {
  const actual = dashboard?.tracks.find((track) => track.track === "actual") ?? null;
  const signals = (dashboard?.signals ?? []).filter((signal) =>
    signal.code.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const activeSignals = dashboard?.signals.filter((signal) =>
    ["active", "partial"].includes(signal.status),
  ).length ?? 0;
  const actualPosition = detail
    ? actual?.positions.find((position) => position.code === detail.signal.code) ?? null
    : null;
  const equityDelta = dashboard?.reconciliation.equity_actual_vs_base ?? null;

  return (
    <div className="today-workspace">
      <section className="metric-strip" aria-label="今日状态">
        <Metric label="市场状态" value="等待行情" tone="neutral" />
        <Metric label="实际权益" value={formatMoney(actual?.equity ?? null)} tone="neutral" />
        <Metric label="相对基础" value={formatMoney(equityDelta)} tone="neutral" />
        <Metric label="有效信号" value={String(activeSignals)} tone="accent" />
      </section>

      <section className="component-strip" aria-label="组件状态">
        <div className="component-strip-title">
          <LayoutDashboard size={16} />
          <span>系统边界</span>
        </div>
        <div className="component-list">
          {systemStatus?.components.map((component) => (
            <span className={`component-state state-${component.state}`} key={component.key} title={component.detail}>
              <i />{component.label}
            </span>
          )) ?? <span className="component-state"><i />{connection === "offline" ? "状态不可用" : "读取中"}</span>}
        </div>
      </section>

      {error ? <div className="error-banner" role="alert"><AlertCircle size={16} />{error}</div> : null}

      <section className="signal-section">
        <header className="section-header">
          <div>
            <span className="eyebrow">09:35 决策批次</span>
            <h2>待执行信号</h2>
          </div>
          <button
            className={loading ? "icon-button is-loading" : "icon-button"}
            disabled={loading || connection === "offline"}
            onClick={() => void onRefresh()}
            title="刷新账本"
            type="button"
          ><RefreshCw size={17} /></button>
        </header>

        <div className="table-scroll">
          <table className="signal-table">
            <thead>
              <tr>
                <th>标的</th>
                <th>操作</th>
                <th>已成 / 建议</th>
                <th>目标权重</th>
                <th>参考价格</th>
                <th>失效时间</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {signals.length ? signals.map((signal) => (
                <tr
                  className={selectedSignalId === signal.signal_id ? "signal-row selected" : "signal-row"}
                  key={signal.signal_id}
                  onClick={() => onSelectSignal(signal.signal_id)}
                >
                  <td><button className="symbol-button" type="button">{signal.code}</button></td>
                  <td><span className={`side side-${signal.side}`}>{signal.side === "buy" ? "买入" : "卖出"}</span></td>
                  <td>{signal.filled_quantity.toLocaleString("zh-CN")} / {signal.suggested_quantity.toLocaleString("zh-CN")}</td>
                  <td>{formatNumber(Number(signal.target_weight) * 100)}%</td>
                  <td>¥{formatNumber(signal.reference_price)}</td>
                  <td>{formatDeadline(signal.expires_at)}</td>
                  <td><span className={`status status-${signal.status}`}>{STATUS_LABELS[signal.status]}</span></td>
                </tr>
              )) : (
                <tr className="empty-row"><td colSpan={7}>{loading ? "正在读取账本" : "暂无有效信号"}</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <div className="detail-grid">
        <section className="chart-section">
          <header className="section-header compact">
            <div>
              <span className="eyebrow">信号上下文</span>
              <h2>{detail?.signal.code ?? "未选择标的"}</h2>
            </div>
            <span className="timeframe-chip">日线</span>
          </header>
          <PriceChart detail={detail} />
        </section>

        <ExecutionPanel
          detail={detail}
          onMutation={onMutation}
          position={actualPosition}
        />
      </div>
    </div>
  );
}

function PriceChart({ detail }: { detail: SignalDetail | null }) {
  const points = detail?.price_points.filter((point) => point.execution_close !== null) ?? [];
  if (!detail || points.length < 2) {
    return (
      <div className="chart-empty">
        <CandlestickChart size={31} />
        <strong>{detail ? "价格序列不足" : "等待选择信号"}</strong>
        <span>--</span>
      </div>
    );
  }

  const width = 640;
  const height = 220;
  const padding = 24;
  const executionValues = points.map((point) => Number(point.execution_close));
  const signalValues = points.map((point) => Number(point.signal_close ?? point.execution_close));
  const allValues = [...executionValues, ...signalValues];
  const minimum = Math.min(...allValues);
  const maximum = Math.max(...allValues);
  const range = maximum - minimum || 1;
  const x = (index: number) => padding + (index / (points.length - 1)) * (width - padding * 2);
  const y = (value: number) => height - padding - ((value - minimum) / range) * (height - padding * 2);
  const executionLine = executionValues.map((value, index) => `${x(index)},${y(value)}`).join(" ");
  const signalLine = signalValues.map((value, index) => `${x(index)},${y(value)}`).join(" ");

  return (
    <div className="price-chart-wrap">
      <div className="chart-legend">
        <span><i className="legend-signal" />前复权信号价</span>
        <span><i className="legend-execution" />不复权执行价</span>
      </div>
      <svg className="price-chart" role="img" viewBox={`0 0 ${width} ${height}`} aria-label={`${detail.signal.code} 价格轨迹`}>
        <line className="chart-grid-line" x1={padding} x2={width - padding} y1={padding} y2={padding} />
        <line className="chart-grid-line" x1={padding} x2={width - padding} y1={height / 2} y2={height / 2} />
        <line className="chart-grid-line" x1={padding} x2={width - padding} y1={height - padding} y2={height - padding} />
        <polyline className="chart-line signal-line" points={signalLine} />
        <polyline className="chart-line execution-line" points={executionLine} />
      </svg>
      <div className="chart-axis-labels">
        <span>{points[0].trade_date}</span>
        <strong>¥{formatNumber(executionValues.at(-1) ?? 0)}</strong>
        <span>{points.at(-1)?.trade_date}</span>
      </div>
    </div>
  );
}

function ExecutionPanel({
  detail,
  position,
  onMutation,
}: {
  detail: SignalDetail | null;
  position: PositionView | null;
  onMutation: (detail: SignalDetail) => Promise<void>;
}) {
  const [mode, setMode] = useState<"fill" | "skip" | null>(null);
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [fees, setFees] = useState("");
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setMode(null);
    setError(null);
    setQuantity(detail ? String(detail.signal.remaining_quantity) : "");
    setPrice(detail?.signal.reference_price ?? "");
    setFees(detail?.signal.estimated_fees ?? "");
    setNote("");
    setReason("");
  }, [detail?.signal.signal_id, detail?.signal.remaining_quantity, detail?.signal.reference_price, detail?.signal.estimated_fees]);

  const actionable = detail
    ? ["active", "partial"].includes(detail.signal.status) && detail.signal.remaining_quantity > 0
    : false;

  const submitFill = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!detail) return;
    const parsedQuantity = Number(quantity);
    if (!Number.isInteger(parsedQuantity) || parsedQuantity <= 0) {
      setError("成交数量必须是正整数");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await recordFill({
        source_order_intent_id: detail.signal.order_intent_id,
        portfolio_track: "actual",
        quantity: parsedQuantity,
        price,
        fees,
        occurred_at: new Date().toISOString(),
        idempotency_key: newIdempotencyKey("fill"),
        note: note.trim() || undefined,
      });
      await onMutation(result);
      setMode(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "成交写入失败");
    } finally {
      setBusy(false);
    }
  };

  const submitSkip = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!detail || !reason.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await skipSignal(detail.signal.signal_id, {
        portfolio_track: "actual",
        reason: reason.trim(),
        occurred_at: new Date().toISOString(),
        idempotency_key: newIdempotencyKey("skip"),
      });
      await onMutation(result);
      setMode(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "信号状态写入失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="execution-section">
      <header className="section-header compact">
        <div>
          <span className="eyebrow">人工执行</span>
          <h2>成交回填</h2>
        </div>
        {mode ? <button className="icon-button" onClick={() => setMode(null)} title="关闭" type="button"><X size={16} /></button> : null}
      </header>
      <dl className="execution-values">
        <div><dt>建议数量</dt><dd>{detail?.signal.suggested_quantity.toLocaleString("zh-CN") ?? "--"}</dd></div>
        <div><dt>已成交</dt><dd>{detail?.signal.filled_quantity.toLocaleString("zh-CN") ?? "--"}</dd></div>
        <div><dt>T+1 可卖</dt><dd>{position?.sellable_quantity.toLocaleString("zh-CN") ?? "0"}</dd></div>
        <div><dt>信号状态</dt><dd>{detail ? STATUS_LABELS[detail.signal.status] : "无信号"}</dd></div>
      </dl>

      {error ? <div className="form-error" role="alert"><AlertCircle size={14} />{error}</div> : null}

      {mode === "fill" && detail ? (
        <form className="execution-form" onSubmit={submitFill}>
          <label>成交数量<input max={detail.signal.remaining_quantity} min="1" onChange={(event) => setQuantity(event.target.value)} required step="1" type="number" value={quantity} /></label>
          <label>成交价格<input min="0.001" onChange={(event) => setPrice(event.target.value)} required step="0.001" type="number" value={price} /></label>
          <label>费用<input min="0" onChange={(event) => setFees(event.target.value)} required step="0.01" type="number" value={fees} /></label>
          <label className="form-span">备注<input maxLength={1000} onChange={(event) => setNote(event.target.value)} type="text" value={note} /></label>
          <button className="primary-button form-span" disabled={busy} type="submit"><Check size={16} />确认成交</button>
        </form>
      ) : mode === "skip" && detail ? (
        <form className="execution-form" onSubmit={submitSkip}>
          <label className="form-span">跳过原因<input maxLength={500} onChange={(event) => setReason(event.target.value)} required type="text" value={reason} /></label>
          <button className="secondary-button form-span" disabled={busy || !reason.trim()} type="submit"><SkipForward size={16} />确认跳过</button>
        </form>
      ) : (
        <div className="execution-actions">
          <button className="primary-button" disabled={!actionable} onClick={() => setMode("fill")} type="button"><ReceiptText size={16} />记录成交</button>
          <button className="secondary-button" disabled={!actionable} onClick={() => setMode("skip")} type="button"><SkipForward size={16} />跳过剩余</button>
        </div>
      )}
    </section>
  );
}

function PortfolioWorkspace({ dashboard, error }: { dashboard: LedgerDashboard | null; error: string | null }) {
  const tracks = Object.fromEntries((dashboard?.tracks ?? []).map((track) => [track.track, track])) as Partial<Record<string, TrackView>>;
  const actual = tracks.actual;
  return (
    <div className="portfolio-workspace">
      <section className="track-strip" aria-label="三轨组合">
        <TrackMetric label="基础组合" track={tracks.base} />
        <TrackMetric label="AI-shadow" track={tracks.ai_shadow} />
        <TrackMetric label="实际组合" track={actual} />
      </section>
      {error ? <div className="error-banner" role="alert"><AlertCircle size={16} />{error}</div> : null}
      <section className="reconciliation-section">
        <header className="section-header">
          <div><span className="eyebrow">组合归因</span><h2>持仓对账</h2></div>
          <span className="audit-stamp"><WalletCards size={15} />append-only</span>
        </header>
        <div className="table-scroll">
          <table className="reconciliation-table">
            <thead><tr><th>标的</th><th>基础</th><th>AI-shadow</th><th>实际</th><th>实际 - 基础</th></tr></thead>
            <tbody>
              {dashboard?.reconciliation.rows.length ? dashboard.reconciliation.rows.map((row) => (
                <tr key={row.code}>
                  <td>{row.code}</td><td>{row.base_quantity}</td><td>{row.ai_shadow_quantity}</td><td>{row.actual_quantity}</td>
                  <td className={row.actual_vs_base === 0 ? "delta-flat" : row.actual_vs_base > 0 ? "delta-up" : "delta-down"}>{row.actual_vs_base > 0 ? "+" : ""}{row.actual_vs_base}</td>
                </tr>
              )) : <tr className="empty-row"><td colSpan={5}>暂无持仓事件</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
      <section className="positions-section">
        <header className="section-header"><div><span className="eyebrow">人工账本</span><h2>实际持仓</h2></div></header>
        <div className="table-scroll">
          <table className="reconciliation-table">
            <thead><tr><th>标的</th><th>总数量</th><th>T+1 可卖</th><th>平均成本</th><th>最新价格</th><th>市值</th></tr></thead>
            <tbody>
              {actual?.positions.length ? actual.positions.map((position) => (
                <tr key={position.code}><td>{position.code}</td><td>{position.quantity}</td><td>{position.sellable_quantity}</td><td>¥{formatNumber(position.average_cost)}</td><td>{formatMoney(position.last_price)}</td><td>{formatMoney(position.market_value)}</td></tr>
              )) : <tr className="empty-row"><td colSpan={6}>暂无实际持仓</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function TrackMetric({ label, track }: { label: string; track?: TrackView }) {
  return (
    <div className="track-metric">
      <span>{label}</span>
      <strong>{formatMoney(track?.equity ?? null)}</strong>
      <small>现金 {formatMoney(track?.cash ?? null)}</small>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone: "neutral" | "accent" }) {
  return <div className={`metric metric-${tone}`}><span>{label}</span><strong>{value}</strong></div>;
}

function EmptyWorkspace({ viewTitle }: { viewTitle: string }) {
  return <div className="view-empty"><LayoutDashboard size={30} /><strong>{viewTitle}</strong><span>暂无数据</span></div>;
}

export default App;

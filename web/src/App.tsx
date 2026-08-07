import {
  ArrowUp,
  BarChart3,
  BriefcaseBusiness,
  CandlestickChart,
  Clock3,
  Database,
  FlaskConical,
  History,
  LayoutDashboard,
  ListChecks,
  MessageSquareText,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Wifi,
  WifiOff,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { getSystemStatus } from "./api";
import type { SystemStatus } from "./types";

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

function App() {
  const [activeView, setActiveView] = useState<ViewId>("today");
  const [aiTab, setAiTab] = useState<AiTab>("summary");
  const [query, setQuery] = useState("");
  const [now, setNow] = useState(() => new Date());
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);

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

  const viewTitle = useMemo(
    () => NAV_ITEMS.find((item) => item.id === activeView)?.label ?? "今日决策",
    [activeView],
  );

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
  };

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
          <TodayWorkspace systemStatus={systemStatus} connection={connection} />
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
}: {
  systemStatus: SystemStatus | null;
  connection: ConnectionState;
}) {
  return (
    <div className="today-workspace">
      <section className="metric-strip" aria-label="今日状态">
        <Metric label="市场状态" value="等待数据" tone="neutral" />
        <Metric label="实际仓位" value="--" tone="neutral" />
        <Metric label="组合偏离" value="--" tone="neutral" />
        <Metric label="有效信号" value="0" tone="accent" />
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

      <section className="signal-section">
        <header className="section-header">
          <div>
            <span className="eyebrow">09:35 决策批次</span>
            <h2>待执行信号</h2>
          </div>
          <button className="icon-button" disabled title="行情尚未配置" type="button"><RefreshCw size={17} /></button>
        </header>

        <div className="table-scroll">
          <table className="signal-table">
            <thead>
              <tr>
                <th>标的</th>
                <th>操作</th>
                <th>当前 → 目标</th>
                <th>建议数量</th>
                <th>有效价格</th>
                <th>失效时间</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr className="empty-row">
                <td colSpan={7}>暂无有效信号</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <div className="detail-grid">
        <section className="chart-section">
          <header className="section-header compact">
            <div>
              <span className="eyebrow">信号上下文</span>
              <h2>未选择标的</h2>
            </div>
            <span className="timeframe-chip">日线</span>
          </header>
          <div className="chart-empty">
            <CandlestickChart size={31} />
            <strong>行情待接入</strong>
            <span>--</span>
          </div>
        </section>

        <section className="execution-section">
          <header className="section-header compact">
            <div>
              <span className="eyebrow">人工执行</span>
              <h2>成交回填</h2>
            </div>
          </header>
          <dl className="execution-values">
            <div><dt>建议数量</dt><dd>--</dd></div>
            <div><dt>已成交</dt><dd>--</dd></div>
            <div><dt>T+1 可卖</dt><dd>--</dd></div>
            <div><dt>信号状态</dt><dd>无信号</dd></div>
          </dl>
          <button className="primary-button" disabled type="button">记录成交</button>
        </section>
      </div>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone: "neutral" | "accent" }) {
  return (
    <div className={`metric metric-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EmptyWorkspace({ viewTitle }: { viewTitle: string }) {
  return (
    <div className="view-empty">
      <LayoutDashboard size={30} />
      <strong>{viewTitle}</strong>
      <span>暂无数据</span>
    </div>
  );
}

export default App;

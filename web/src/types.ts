export type ComponentState = "ready" | "not_configured" | "degraded";

export interface ComponentStatus {
  key: string;
  label: string;
  state: ComponentState;
  detail: string;
}

export interface SystemStatus {
  mode: "research";
  environment: string;
  server_time: string;
  components: ComponentStatus[];
}

export type PortfolioTrack = "base" | "ai_shadow" | "actual";
export type SignalStatus = "active" | "partial" | "filled" | "skipped" | "expired";

export interface PositionView {
  code: string;
  quantity: number;
  sellable_quantity: number;
  average_cost: string;
  last_price: string | null;
  market_value: string | null;
}

export interface TrackView {
  track: PortfolioTrack;
  cash: string;
  market_value: string | null;
  equity: string | null;
  positions: PositionView[];
}

export interface SignalView {
  signal_id: string;
  order_intent_id: string;
  decision_id: string;
  snapshot_id: string;
  portfolio_track: PortfolioTrack;
  code: string;
  side: "buy" | "sell";
  suggested_quantity: number;
  filled_quantity: number;
  remaining_quantity: number;
  reference_price: string;
  target_weight: string;
  estimated_fees: string;
  expires_at: string;
  status: SignalStatus;
  skip_reason: string | null;
}

export interface PricePoint {
  trade_date: string;
  signal_close: string | null;
  execution_close: string | null;
}

export interface SignalTrace {
  decision_id: string;
  snapshot_id: string;
  configuration_id: string;
  pipeline_version: string;
  source_payloads: string[];
  recorded_at: string;
}

export interface SignalDetail {
  signal: SignalView;
  price_points: PricePoint[];
  trace: SignalTrace;
}

export interface ReconciliationRow {
  code: string;
  base_quantity: number;
  ai_shadow_quantity: number;
  actual_quantity: number;
  actual_vs_base: number;
}

export interface LedgerDashboard {
  as_of: string;
  tracks: TrackView[];
  signals: SignalView[];
  reconciliation: {
    cash_actual_vs_base: string;
    equity_actual_vs_base: string | null;
    rows: ReconciliationRow[];
  };
}

export interface RecordFillInput {
  source_order_intent_id: string;
  portfolio_track: "actual";
  quantity: number;
  price: string;
  fees: string;
  occurred_at: string;
  idempotency_key: string;
  note?: string;
}

export interface SkipSignalInput {
  portfolio_track: "actual";
  reason: string;
  occurred_at: string;
  idempotency_key: string;
}

export type AIProposalStatus = "accepted" | "rejected" | "fallback";
export type AICompletionOutcome =
  | "succeeded"
  | "timeout"
  | "budget_exceeded"
  | "provider_error"
  | "invalid_output";
export type StrategyKind =
  | "momentum"
  | "short_term_reversal"
  | "defensive_low_volatility"
  | "cash";

export interface AIStrategyWeight {
  strategy: StrategyKind;
  weight: string;
}

export interface AITargetWeight {
  code: string;
  weight: string;
  rank: number;
}

export interface AIEvidence {
  evidence_id: string;
  claim: string;
}

export interface AIMessage {
  message_id: string;
  role: "system" | "user" | "assistant";
  content: string;
  created_at: string;
}

export interface AIRun {
  run_id: string;
  request_id: string;
  proposal_id: string;
  base_decision_id: string;
  shadow_decision_id: string;
  snapshot_id: string;
  status: AIProposalStatus;
  outcome: AICompletionOutcome;
  provider: string;
  model: string;
  prompt_version: string;
  requested_at: string;
  completed_at: string;
  cache_hit: boolean;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: string;
  summary: string;
  rationale: string;
  strategy_weights: AIStrategyWeight[];
  risk_scale: string;
  evidence: AIEvidence[];
  validation_errors: string[];
  base_target_weights: AITargetWeight[];
  shadow_target_weights: AITargetWeight[];
  messages: AIMessage[];
}

export interface AIWorkspace {
  core_state: "ready";
  provider_configured: boolean;
  latest: AIRun | null;
}

export type ComponentState = "ready" | "not_configured" | "degraded";

export interface ComponentStatus {
  key: string;
  label: string;
  state: ComponentState;
  detail: string;
}

export interface SystemStatus {
  mode: "scaffold";
  environment: string;
  server_time: string;
  components: ComponentStatus[];
}

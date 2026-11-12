export interface User {
  user_id: string;
  chat_id: string;
  username: string | null;
  first_name: string | null;
  role: string;
  status: string;
  registered_at: string;
  last_active_at: string;
}

export interface UserBalance {
  user_id: string;
  starting_balance: number;
  current_balance: number;
  realized_pnl: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  updated_at: string;
}

export interface PaperPosition {
  position_id: number;
  user_id: string;
  symbol: string;
  direction: 'BULLISH' | 'BEARISH';
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  position_size: number;
  status: 'OPEN' | 'CLOSED_TP' | 'CLOSED_SL' | 'CLOSED_MANUAL';
  entry_time: string;
  exit_time: string | null;
  exit_price: number | null;
  exit_reason: string | null;
  pnl: number;
  fee_paid: number;
  current_price: number | null;
  unrealized_pnl: number;
  r_multiple: number;
  target_progress_pct: number;
  last_price_time: string | null;
  initial_risk: number;
  last_notified_progress_milestone: number;
  last_notified_r_milestone: number;
}

export interface Candle {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_ADMIN_IDS?: string;
  TELEGRAM_WEBHOOK_SECRET?: string;
  DASHBOARD_SECRET_KEY?: string;
  DASHBOARD_URL?: string;
  DEFAULT_STARTING_BALANCE?: string;
  MAX_OPEN_POSITIONS_PER_USER?: string;
  ENVIRONMENT?: string;
}

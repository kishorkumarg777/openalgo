import axios from 'axios'
import { apiClient } from './client'

export type SettingKind = 'int' | 'float' | 'bool' | 'choice' | 'text'
export type ColumnKind = 'text' | 'num' | 'pct' | 'int'

export interface SettingChoice {
  value: string
  label: string
}

export interface SettingSpec {
  key: string
  label: string
  tooltip: string
  kind: SettingKind
  min: number | null
  max: number | null
  choices: SettingChoice[]
  group: string
}

export interface ColumnSpec {
  key: string
  label: string
  kind: ColumnKind
  tooltip: string
}

export interface StrategyMeta {
  id: string
  name: string
  description: string
  timeframes: string[]
  ui_keys: string[]
  settings_specs: SettingSpec[]
  defaults: Record<string, unknown>
  columns: ColumnSpec[]
}

export interface TimeframeMeta {
  label: string
  openalgo_interval: string
  intraday: boolean
}

export interface ScreenerMeta {
  strategies: StrategyMeta[]
  scan_specs: SettingSpec[]
  scan_defaults: Record<string, unknown>
  strategy_defaults: Record<string, Record<string, unknown>>
  timeframes: TimeframeMeta[]
  universe_modes: SettingChoice[]
  data_sources: SettingChoice[]
  nifty50_count: number
}

export interface ScanRow {
  valid: boolean
  values: Record<string, unknown>
}

export interface ScanProgress {
  done: number
  total: number
  message: string
  phase: 'init' | 'main' | 'ltf' | 'htf' | 'done'
}

export interface ScanStats {
  matches: number
  rejected: number
  scanned: number
  universe: number
  filtered: number
  short: number
  errors: number
  missing: number
  calls: number
  ltf_checked: number
  ltf_confirmed: number
  htf_checked: number
  htf_hits: number
  mtf_removed: number
  elapsed_sec: number
}

export interface ScanSnapshot {
  job_id: string
  state: 'running' | 'completed' | 'cancelled' | 'failed'
  strategy: string
  timeframe: string
  source: string
  provider: string
  config_summary: string
  columns: ColumnSpec[]
  progress: ScanProgress
  rows: ScanRow[]
  stats: ScanStats
  summary: string
  fatal: string
  missing_symbols: string[]
  error_samples: Record<string, string>
  warnings?: string[]
}

export interface ObBox {
  kind: 'Internal' | 'Swing'
  rank: number
  drawn_on_chart: boolean
  bias: 'bullish' | 'bearish'
  low: number
  high: number
  ob_time: string
  formed_time: string
  source: string
}

export interface BreakerBox {
  low: number
  high: number
  ob_time: string
  formed_time: string
  source: string
  retested: boolean
  retest_time: string
}

export interface ExplainResult {
  symbol: string
  name: string
  timeframe: string
  strategy: string
  strategy_name: string
  candles_count: number
  min_candles: number
  too_short: boolean
  provider: string
  judged_candle: {
    label: string
    ts: number
    o: number
    h: number
    l: number
    c: number
    v: number
    rsi: number | null
    avg_volume: number | null
    avg_value_cr: number | null
  }
  columns: ColumnSpec[]
  rows: ScanRow[]
  chart_settings: Record<string, unknown>
  internal_obs: ObBox[]
  swing_obs: ObBox[]
  bullish_breakers: BreakerBox[]
}

export interface StartScanRequest {
  apikey: string
  strategy: string
  timeframe: string
  scan: Record<string, unknown>
  strategy_settings: Record<string, unknown>
}

export interface ExplainRequest {
  apikey: string
  symbol: string
  strategy: string
  timeframe: string
  scan: Record<string, unknown>
  strategy_settings: Record<string, unknown>
}

export function extractScreenerError(err: unknown, fallback = 'Screener request failed'): string {
  if (axios.isAxiosError(err)) {
    const payload = err.response?.data as { message?: string; errors?: string[] } | undefined
    if (payload?.errors && payload.errors.length > 0) {
      return payload.errors.join(' | ')
    }
    if (payload?.message) return payload.message
    if (err.message) return err.message
  }
  if (err instanceof Error && err.message) return err.message
  return fallback
}

export async function getScreenerMeta(): Promise<ScreenerMeta> {
  const { data } = await apiClient.get<{ data: ScreenerMeta }>('/screener/meta')
  return data.data
}

export async function startScreenerScan(payload: StartScanRequest): Promise<ScanSnapshot> {
  const { data } = await apiClient.post<{ data: ScanSnapshot }>('/screener/start', payload)
  return data.data
}

export async function getScreenerStatus(apikey: string, jobId: string): Promise<ScanSnapshot> {
  const { data } = await apiClient.post<{ data: ScanSnapshot }>('/screener/status', {
    apikey,
    job_id: jobId,
  })
  return data.data
}

export async function stopScreenerScan(apikey: string, jobId: string): Promise<ScanSnapshot> {
  const { data } = await apiClient.post<{ data: ScanSnapshot }>('/screener/stop', {
    apikey,
    job_id: jobId,
  })
  return data.data
}

export async function explainScreenerSymbol(payload: ExplainRequest): Promise<ExplainResult> {
  const { data } = await apiClient.post<{ data: ExplainResult }>('/screener/explain', payload)
  return data.data
}

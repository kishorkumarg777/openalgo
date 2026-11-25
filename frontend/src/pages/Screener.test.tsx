import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ScanSnapshot, ScreenerMeta } from '@/api/screener'
import { TooltipProvider } from '@/components/ui/tooltip'
import { useAuthStore } from '@/stores/authStore'
import { useScreenerStore } from '@/stores/screenerStore'
import Screener from './Screener'

const mockMeta: ScreenerMeta = {
  strategies: [
    {
      id: 'smc_breaker',
      name: 'SMC Breaker Block (LuxAlgo port)',
      description: 'Bullish breaker block (+BB) screener.',
      timeframes: ['1D', '1W', '1M', '4H', '2H', '1H', '30m', '15m'],
      ui_keys: [],
      settings_specs: [
        {
          key: 'max_breaker_age',
          label: 'Breaker formed within (candles)',
          tooltip: 'Max age in candles.',
          kind: 'int',
          min: 0,
          max: 100,
          choices: [],
          group: 'Screener filters',
        },
        {
          key: 'show_invalid',
          label: 'Show rejected',
          tooltip: 'Show rejected rows.',
          kind: 'bool',
          min: null,
          max: null,
          choices: [],
          group: 'Display',
        },
      ],
      defaults: { max_breaker_age: 1, show_invalid: false },
      columns: [
        { key: 'symbol', label: 'Symbol', kind: 'text', tooltip: '' },
        { key: 'status', label: 'Status', kind: 'text', tooltip: '' },
        { key: 'last_close', label: 'Last close', kind: 'num', tooltip: '' },
      ],
    },
    {
      id: 'ob_at_open',
      name: 'OB on chosen candle time(s) (intraday)',
      description: 'Active order block on chosen intraday candle opening times.',
      timeframes: ['4H', '2H', '1H', '30m', '15m'],
      ui_keys: ['candle_time'],
      settings_specs: [
        {
          key: 'candle_time',
          label: 'OB candle time(s) (HH:MM IST)',
          tooltip: 'Comma separated times',
          kind: 'text',
          min: null,
          max: null,
          choices: [],
          group: 'Screener filters',
        },
      ],
      defaults: { candle_time: '09:15' },
      columns: [
        { key: 'symbol', label: 'Symbol', kind: 'text', tooltip: '' },
        { key: 'status', label: 'Status', kind: 'text', tooltip: '' },
      ],
    },
  ],
  scan_specs: [
    {
      key: 'lookback_candles',
      label: 'Lookback (candles)',
      tooltip: '',
      kind: 'int',
      min: 50,
      max: 2000,
      choices: [],
      group: 'Scan & Universe',
    },
  ],
  scan_defaults: {
    strategy: 'smc_breaker',
    timeframe: '1D',
    source: 'api',
    lookback_candles: 250,
    as_of_date: '',
    forming_candle: 'final',
    universe_mode: 'nifty50',
    custom_symbols: '',
    watchlist_symbols: '',
    min_price: 50,
    rsi_period: 14,
    rsi_min: 0,
    rsi_max: 65,
    avg_period: 20,
    min_avg_volume: 50000,
    min_avg_value_cr: 0,
    ltf_on: false,
    htf_on: false,
  },
  strategy_defaults: {
    smc_breaker: { max_breaker_age: 1, show_invalid: false },
    ob_at_open: { candle_time: '09:15' },
  },
  timeframes: [
    { label: '1D', openalgo_interval: 'D', intraday: false },
    { label: '1W', openalgo_interval: 'W', intraday: false },
    { label: '1H', openalgo_interval: '1h', intraday: true },
    { label: '15m', openalgo_interval: '15m', intraday: true },
  ],
  universe_modes: [
    { value: 'all_nse_eq', label: 'All NSE stocks (EQ series)' },
    { value: 'nifty50', label: 'NIFTY 50 (Quick scan)' },
    { value: 'custom', label: 'Custom ticker list' },
  ],
  data_sources: [
    { value: 'api', label: 'Broker API (Live)' },
    { value: 'db', label: 'Historify DB (Local)' },
  ],
  nifty50_count: 50,
}

const mockCompletedSnapshot: ScanSnapshot = {
  job_id: 'job-test-1',
  state: 'completed',
  strategy: 'smc_breaker',
  timeframe: '1D',
  source: 'api',
  provider: 'indmoney_batch',
  config_summary: 'Broker API | NIFTY 50 | lookback 250',
  columns: [
    { key: 'symbol', label: 'Symbol', kind: 'text', tooltip: '' },
    { key: 'status', label: 'Status', kind: 'text', tooltip: '' },
    { key: 'last_close', label: 'Last close', kind: 'num', tooltip: '' },
    { key: 'rsi', label: 'RSI(14)', kind: 'num', tooltip: '' },
  ],
  progress: { done: 50, total: 50, message: 'Scan complete', phase: 'done' },
  rows: [
    {
      valid: true,
      values: {
        symbol: 'RELIANCE',
        status: 'Fresh breaker',
        last_close: 2950.5,
        rsi: 54.2,
      },
    },
  ],
  stats: {
    matches: 1,
    rejected: 0,
    scanned: 50,
    universe: 50,
    filtered: 2,
    short: 0,
    errors: 0,
    missing: 0,
    calls: 10,
    ltf_checked: 0,
    ltf_confirmed: 0,
    htf_checked: 0,
    htf_hits: 0,
    mtf_removed: 0,
    elapsed_sec: 3.2,
  },
  summary: '1 match(es) in 50/50 stocks | 10 calls | 3 s',
  fatal: '',
  missing_symbols: [],
  error_samples: {},
}

const startMock = vi.fn(async () => mockCompletedSnapshot)
const metaMock = vi.fn(async () => mockMeta)

vi.mock('@/api/screener', async () => {
  const actual = await vi.importActual<typeof import('@/api/screener')>('@/api/screener')
  return {
    ...actual,
    getScreenerMeta: () => metaMock(),
    startScreenerScan: (...args: unknown[]) => startMock(...(args as [])),
  }
})

function renderScreener() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <Screener />
      </TooltipProvider>
    </QueryClientProvider>
  )
}

describe('Screener page & store', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useScreenerStore.getState().resetAll()
    useAuthStore.setState({
      apiKey: 'test-openalgo-key',
      user: { username: 'trader', broker: 'indmoney', isLoggedIn: true, loginTime: null },
      isAuthenticated: true,
    })
  })

  afterEach(() => {
    cleanup()
  })

  it('loads metadata, shows INDMoney 5x batch badge, and runs a scan with /trading chart link', async () => {
    renderScreener()

    expect(await screen.findByText('SMC Stock Screener')).toBeInTheDocument()
    expect(screen.getByText('INDMoney 5x Batch Mode')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Run Screener/i }))

    await waitFor(() => expect(startMock).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('RELIANCE')).toBeInTheDocument()
    expect(screen.getByText('Fresh breaker')).toBeInTheDocument()

    const chartLink = screen.getByRole('link', { name: 'RELIANCE' })
    expect(chartLink).toHaveAttribute('href', '/trading?symbol=RELIANCE&exchange=NSE')
  })

  it('switches timeframe automatically to 1H when selecting intraday-only ob_at_open', async () => {
    useScreenerStore.getState().setStrategy('ob_at_open', ['4H', '2H', '1H', '30m', '15m'])
    expect(useScreenerStore.getState().timeframe).toBe('1H')

    renderScreener()
    expect(await screen.findByText(/OB Candle Time\(s\) IST:/i)).toBeInTheDocument()
    const d1Btn = screen.getByRole('button', { name: '1D' })
    expect(d1Btn).toBeDisabled()
  })

  it('imports stock_screener config.json cleanly into the persisted store', () => {
    const res = useScreenerStore.getState().importConfigJson(
      JSON.stringify({
        scan: {
          strategy: 'smc_ob',
          timeframe: '1W',
          min_price: 120,
          rsi_max: 60,
          ltf_on: true,
        },
        strategies: {
          smc_ob: { last_at_zone: 'inside', chart_shown: 3 },
        },
      })
    )
    expect(res.ok).toBe(true)
    const st = useScreenerStore.getState()
    expect(st.strategy).toBe('smc_ob')
    expect(st.timeframe).toBe('1W')
    expect(st.scanOverrides.min_price).toBe(120)
    expect(st.scanOverrides.ltf_on).toBe(true)
    expect(st.strategyOverrides.smc_ob?.last_at_zone).toBe('inside')
  })
})

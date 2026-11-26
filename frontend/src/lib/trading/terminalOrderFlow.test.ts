/**
 * Regressions for Order Flow live-candle continuity, visible scaling defaults,
 * and indicator-legend collapse persistence across chart rebuilds.
 */
import type { Bar, DataLoadingSnapshot } from 'openalgo-charts'
import { readChartSettings } from 'openalgo-charts'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SymbolView } from './terminal'
import { TradingTerminal } from './terminal'

type InternalState = {
  chartToolsReady: Promise<void>
  sym: SymbolView
  interval: string
  ctype: string
  rawBars: Bar[]
  liveBucket: number | null
  builder: {
    current(): Bar | null
    reconcile(bar: Bar): Bar | null
  } | null
  chart: {
    indicatorLegendCollapsed(): boolean
    setIndicatorLegendCollapsed(on: boolean): void
    priceOnlyAutoScale(): boolean
    priceScaleOptions(): { marginBottom: number }
  }
  buildChart(): void
  loadIndicators(): Promise<void>
  applyDataSnapshot(snapshot: DataLoadingSnapshot): void
}

const BASE = Date.UTC(2026, 10, 25, 7, 20) / 1000 // 12:50 IST
const bars: Bar[] = Array.from({ length: 10 }, (_, i) => ({
  time: BASE + i * 60,
  open: 776 + i * 0.1,
  high: 776.6 + i * 0.1,
  low: 775.8 + i * 0.1,
  close: 776.2 + i * 0.1,
  volume: 1000 + i * 50,
}))

const terminals: TradingTerminal[] = []

beforeEach(() => {
  const context = new Proxy(
    {
      measureText: (text: string) => ({ width: text.length * 7 }),
      createLinearGradient: () => ({ addColorStop() {} }),
      getImageData: () => ({ data: new Uint8ClampedArray([0, 0, 0, 255]) }),
    },
    { get: (target, key) => target[key as keyof typeof target] ?? (() => {}) }
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context as unknown as CanvasRenderingContext2D
  )
})

afterEach(() => {
  for (const terminal of terminals.splice(0)) terminal.destroy()
  vi.restoreAllMocks()
})

function makeStorage(initial: Record<string, string> = {}) {
  const values = new Map<string, string>(Object.entries(initial))
  const storage = {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      values.set(key, value)
    }),
  }
  return { values, storage }
}

async function mount(ctype: string, storage: Pick<Storage, 'getItem' | 'setItem'>) {
  const terminal = new TradingTerminal({
    apiKey: 'test',
    wsUrl: 'ws://test.invalid',
    container: document.createElement('div'),
    legendEl: document.createElement('div'),
    storageKey: 'oa-trading-p0',
    preferences: storage,
    getTheme: () => ({ mode: 'dark', appMode: 'live' }),
    callbacks: {
      onReady() {},
      onToast() {},
      onWsState() {},
      onSymbolLoaded() {},
      onLtp() {},
    },
  })
  terminals.push(terminal)
  const state = terminal as unknown as InternalState
  state.loadIndicators = async () => {}
  state.sym = {
    symbol: 'ANTELOPUS',
    exchange: 'NSE',
    name: 'Antelopus',
    lotsize: 1,
    lots: false,
    tick: 0.05,
    freezeQty: 0,
    quoteOnly: false,
    productOptions: ['MIS', 'CNC'],
    product: 'MIS',
  }
  state.interval = '1m'
  state.ctype = ctype
  state.rawBars = bars.map((b) => ({ ...b }))
  state.buildChart()
  await state.chartToolsReady
  await new Promise((resolve) => setTimeout(resolve, 0))
  return { terminal, state }
}

describe('Order Flow terminal ergonomics and continuity', () => {
  it('persists collapsed Indicators legend across timeframe rebuilds and reloads', async () => {
    const { values, storage } = makeStorage()
    const { state } = await mount('order-flow', storage)

    expect(state.chart.indicatorLegendCollapsed()).toBe(false)
    state.chart.setIndicatorLegendCollapsed(true)
    expect(values.get('oa-trading-p0-indicator-legend-collapsed')).toBe('1')

    // Simulating a timeframe change rebuilds the chart instance from scratch.
    state.interval = '1h'
    state.buildChart()
    await state.chartToolsReady
    expect(state.chart.indicatorLegendCollapsed()).toBe(true)

    // A fresh terminal instance reading the same storage stays collapsed.
    const reloaded = await mount('order-flow', storage)
    expect(reloaded.state.chart.indicatorLegendCollapsed()).toBe(true)
  })

  it('opens Order Flow with price-only autoscale and bar-card bottom margin by default', async () => {
    const { storage: s1 } = makeStorage()
    const ofPane = await mount('order-flow', s1)
    expect(ofPane.state.chart.priceOnlyAutoScale()).toBe(true)
    expect(ofPane.state.chart.priceScaleOptions().marginBottom).toBeCloseTo(0.18, 2)

    const { storage: s2 } = makeStorage()
    const candlePane = await mount('candlestick', s2)
    expect(candlePane.state.chart.priceOnlyAutoScale()).toBe(false)
    expect(candlePane.state.chart.priceScaleOptions().marginBottom).toBeCloseTo(0.1, 2)
  })

  it('preserves a forming live 1m candle when a background history refresh lags behind', async () => {
    const { storage } = makeStorage()
    const { state } = await mount('order-flow', storage)
    const liveBar: Bar = {
      time: bars[bars.length - 1].time + 60,
      open: 777.0,
      high: 777.25,
      low: 776.95,
      close: 777.2,
      volume: 42,
    }
    state.liveBucket = liveBar.time
    state.builder = {
      current: () => ({ ...liveBar }),
      reconcile: (b) => b,
    }
    state.rawBars = [...bars, liveBar]

    // History refresh returns bars up to the previous minute (missing liveBar).
    state.applyDataSnapshot({
      request: { symbol: 'ANTELOPUS', exchange: 'NSE', interval: '1m' },
      bars: bars.map((b) => ({ ...b })),
      status: 'ready',
      historyStatus: 'idle',
      hasMore: true,
      reason: 'refresh',
      paused: false,
    })

    expect(state.rawBars.at(-1)).toMatchObject(liveBar)
  })

  it('hides underlying candlestick bodies on Order Flow so barcolor indicators cannot bleed behind zero cells', async () => {
    const { storage: s1 } = makeStorage()
    const ofPane = await mount('order-flow', s1)
    expect(readChartSettings(ofPane.state.chart as never)).toMatchObject({
      'symbol.bodyVisible': false,
      'symbol.borderVisible': false,
      'symbol.wickVisible': false,
    })

    // Quote-only indices carry no traded quantity for footprints, so keep candles visible.
    const { storage: s2 } = makeStorage()
    const idxPane = await mount('order-flow', s2)
    idxPane.state.sym = { ...idxPane.state.sym, symbol: 'NIFTY', exchange: 'NSE_INDEX', quoteOnly: true }
    idxPane.state.buildChart()
    await idxPane.state.chartToolsReady
    expect(readChartSettings(idxPane.state.chart as never)['symbol.bodyVisible']).toBe(true)
  })
})

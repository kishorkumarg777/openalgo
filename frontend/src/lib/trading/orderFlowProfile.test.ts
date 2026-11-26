import { FOOTPRINT_THEMES } from 'openalgo-charts/profile'
import { describe, expect, it, vi } from 'vitest'
import { autoOrderFlowRowTicks, createChartProfile } from './chartProfiles'
import { OrderFlowFeed } from './orderFlowFeed'
import {
  isProfileKind,
  profileDefaults,
  profileFields,
  profileValues,
  readProfileSettings,
} from './profileSettings'

const context = { tickSize: 1, exchange: 'NSE', timezone: 'Asia/Kolkata', intervalSeconds: 60 }
const newFeed = () =>
  new OrderFlowFeed({ intervalSec: 60, anchorSec: 0, tickSize: 1, rowTicks: 1, minTradeSize: 0 })
const settings = () => readProfileSettings('order-flow', {})

describe('order flow settings', () => {
  it('opens as a bid by ask profile with bar cards and auto row sizing', () => {
    expect(settings()).toMatchObject({
      kind: 'order-flow',
      displayMode: 'bidask',
      cellStyle: 'profile',
      theme: 'default',
      rowSizeMode: 'auto',
      ticksPerRow: 10,
      minTradeSize: 0,
      stackedImbalances: 3,
    })
  })

  it('keeps its own namespace, apart from the other profiles', () => {
    expect(profileDefaults('order-flow')).toHaveProperty('profiles.of.ticksPerRow')
    expect(profileDefaults('order-flow')).not.toHaveProperty('profiles.tpo.ticksPerRow')
    expect(profileDefaults('tpo')).not.toHaveProperty('profiles.of.ticksPerRow')
    expect(profileValues('order-flow', { 'profiles.tpo.ticksPerRow': 77 })).toHaveProperty(
      'profiles.of.ticksPerRow',
      10
    )
  })

  it('falls back to a safe value for anything it cannot use', () => {
    const read = readProfileSettings('order-flow', {
      'profiles.of.displayMode': 'nope',
      'profiles.of.theme': 'daylight',
      'profiles.of.ticksPerRow': 0,
      'profiles.of.minTradeSize': -5,
      'profiles.of.imbalanceRatio': 'abc',
    })
    expect(read.displayMode).toBe('bidask')
    expect(read.theme).toBe('daylight')
    expect(read.ticksPerRow).toBe(1)
    expect(read.minTradeSize).toBe(0)
    expect(read.imbalanceRatio).toBe(3)
  })

  it('is a registered profile chart type', () => {
    expect(isProfileKind('order-flow')).toBe(true)
  })

  it('offers only colors the footprint library has', () => {
    const field = profileFields('order-flow').find((f) => f.key === 'profiles.of.theme') as
      | { options: { value: string }[] }
      | undefined
    expect(field?.options.length).toBeGreaterThan(0)
    for (const option of field?.options ?? []) {
      expect(option.value === 'default' || option.value in FOOTPRINT_THEMES).toBe(true)
    }
  })
})

describe('order flow profile', () => {
  it('draws nothing, and fails nowhere, while the feed has not loaded', () => {
    const profile = createChartProfile('order-flow', settings(), context)
    profile.attached?.({ requestUpdate: vi.fn() })
    expect(() => profile.setBars([])).not.toThrow()
    expect(profile.warning()).toBeNull()
    expect(profile.contextMenuAt(10, 10)).toBeNull()
  })

  it('reads its bars from the feed and asks the chart to redraw', () => {
    const feed = newFeed()
    feed.ingest({ ltp: 100, ltq: 5, timeSec: 10 })
    const requestUpdate = vi.fn()
    const profile = createChartProfile('order-flow', settings(), {
      ...context,
      orderFlow: () => feed,
    })
    profile.attached?.({ requestUpdate })
    profile.setBars([])
    expect(requestUpdate).toHaveBeenCalled()
  })

  it('applies the row size in the settings to what has already traded', () => {
    const feed = newFeed()
    feed.ingest({ ltp: 100, ltq: 5, timeSec: 10 })
    feed.ingest({ ltp: 103, ltq: 5, timeSec: 11 })
    expect(feed.bars()[0].cells.length).toBe(2)
    createChartProfile(
      'order-flow',
      { ...settings(), ticksPerRow: 10 },
      {
        ...context,
        orderFlow: () => feed,
      }
    )
    expect(feed.bars()[0].cells.length).toBe(1)
  })

  it('tells a trader when the symbol sends no traded quantity', () => {
    const feed = newFeed()
    for (let i = 0; i < 80; i++) feed.ingest({ ltp: 100 + i, timeSec: i })
    const profile = createChartProfile('order-flow', settings(), {
      ...context,
      orderFlow: () => feed,
    })
    expect(profile.warning()).toContain('No traded quantity')
  })

  it('rejects settings of another profile kind', () => {
    expect(() =>
      createChartProfile('order-flow', readProfileSettings('tpo', {}), context)
    ).toThrow()
  })

  it('builds footprint bars from historical OHLCV bars and lets live feed bars override them', () => {
    const feed = newFeed()
    feed.ingest({ ltp: 102, ltq: 10, timeSec: 60 })
    feed.ingest({ ltp: 103, ltq: 15, timeSec: 65 })
    const profile = createChartProfile('order-flow', { ...settings(), ticksPerRow: 1 }, {
      ...context,
      orderFlow: () => feed,
    })
    profile.attached?.({ requestUpdate: vi.fn() })
    profile.setBars([
      { time: 0, open: 100, high: 104, low: 99, close: 103, volume: 100 },
      { time: 60, open: 102, high: 103, low: 102, close: 103, volume: 500 },
      { time: 120, open: 103, high: 103, low: 103, close: 103, volume: 0 },
    ])
    const fp = (profile as unknown as { footprint: { _bars: { time: number; delta: number; cells: unknown[] }[] } }).footprint
    expect(fp._bars.map((b) => b.time)).toEqual([0, 60])
    expect(fp._bars[0].cells.length).toBeGreaterThan(0)
    expect(fp._bars[0].delta).toBeGreaterThan(0)
    // Bar at time 60 comes from the live feed (25 total qty), not the 500 historical volume.
    expect(fp._bars[1].cells).toEqual(feed.bars()[0].cells)
  })

  it('conserves exact candle volume across reconstructed historical rows', () => {
    const profile = createChartProfile(
      'order-flow',
      { ...settings(), rowSizeMode: 'manual', ticksPerRow: 5 },
      { ...context, tickSize: 0.05 }
    )
    profile.attached?.({ requestUpdate: vi.fn() })
    profile.setBars([
      { time: 0, open: 648.2, high: 652.4, low: 647.5, close: 651.8, volume: 12_347 },
      { time: 60, open: 651.8, high: 653.0, low: 649.1, close: 649.5, volume: 8_913 },
    ])
    const fp = (
      profile as unknown as {
        footprint: { _bars: { delta: number; cells: { bidVol: number; askVol: number }[] }[] }
      }
    ).footprint
    const total0 = fp._bars[0].cells.reduce((s, c) => s + c.bidVol + c.askVol, 0)
    const total1 = fp._bars[1].cells.reduce((s, c) => s + c.bidVol + c.askVol, 0)
    expect(total0).toBe(12_347)
    expect(total1).toBe(8_913)
    expect(fp._bars[0].delta).toBeGreaterThan(0)
    expect(fp._bars[1].delta).toBeLessThan(0)
  })

  it('picks clean exchange-aligned auto row steps across timeframes and instruments', () => {
    // HBLENGINE 1m (~₹4 window span, 0.05 tick) -> ₹0.20 (4 ticks) or ₹0.25 (5 ticks)
    const hbl1m = Array.from({ length: 8 }, (_, i) => ({
      time: i * 60,
      open: 650 + i * 0.3,
      high: 651.2 + i * 0.3,
      low: 649.8 + i * 0.3,
      close: 650.5 + i * 0.3,
      volume: 5000,
    }))
    const ticks1m = autoOrderFlowRowTicks(hbl1m, 0.05)
    expect([4, 5]).toContain(ticks1m)

    // HBLENGINE 1h (~₹40 window span, 0.05 tick) -> ₹2.00 (40 ticks)
    const hbl1h = Array.from({ length: 8 }, (_, i) => ({
      time: i * 3600,
      open: 640 + i * 4,
      high: 648 + i * 4,
      low: 636 + i * 4,
      close: 644 + i * 4,
      volume: 120_000,
    }))
    expect(autoOrderFlowRowTicks(hbl1h, 0.05)).toBe(40)

    // NIFTY 1m (~₹40 window span, 0.10 tick) -> ₹2.00 (20 ticks)
    const nifty1m = Array.from({ length: 8 }, (_, i) => ({
      time: i * 60,
      open: 23800 + i * 4,
      high: 23810 + i * 4,
      low: 23796 + i * 4,
      close: 23806 + i * 4,
      volume: 30_000,
    }))
    expect(autoOrderFlowRowTicks(nifty1m, 0.1)).toBe(20)
  })

  it('wires GoCharting pro signals and preserves candle OHLC on mid-bar live feeds', () => {
    const feed = newFeed()
    feed.ingest({ ltp: 102, ltq: 15, timeSec: 65 })
    const onRowTicks = vi.fn()
    const profile = createChartProfile(
      'order-flow',
      {
        ...settings(),
        rowSizeMode: 'manual',
        ticksPerRow: 1,
        absorptionMultiple: 2,
        bigOrderMultiple: 3,
        multipleNodeBars: 1, // clamped safely to >= 2
        flipBars: 2,
        intensityScope: 'visible',
        compactNumbers: false,
        markDivergence: true,
      },
      { ...context, orderFlow: () => feed, onOrderFlowRowTicks: onRowTicks }
    )
    profile.attached?.({ requestUpdate: vi.fn() })
    profile.setBars([{ time: 60, open: 99, high: 105, low: 98, close: 102, volume: 1200 }])
    const fp = (
      profile as unknown as {
        footprint: {
          options: () => {
            absorptionMultiple: number
            bigOrderMultiple: number
            multipleNodeBars: number
            flipBars: number
            intensityScope: string
            compactNumbers: boolean
            showDeltaLabels: boolean
          }
          _bars: { open?: number; high?: number; low?: number; close?: number }[]
        }
      }
    ).footprint
    expect(onRowTicks).toHaveBeenCalledWith(1)
    expect(fp.options()).toMatchObject({
      absorptionMultiple: 2,
      bigOrderMultiple: 3,
      multipleNodeBars: 2,
      flipBars: 2,
      intensityScope: 'visible',
      compactNumbers: false,
      showDeltaLabels: true,
    })
    // Full candle OHLC envelope preserved even though live feed only captured 102.
    expect(fp._bars[0]).toMatchObject({ open: 99, high: 105, low: 98, close: 102 })
  })

  it('ignores corrupted zero-low / zero-close bars in auto row sizing and footprint reconstruction', () => {
    const cleanBars = [
      { time: 0, open: 840, high: 860, low: 818, close: 823, volume: 66_455 },
      { time: 14400, open: 823, high: 847, low: 822, close: 830, volume: 32_900 },
      { time: 28800, open: 822, high: 825, low: 815, close: 820, volume: 17_894 },
    ]
    const corruptBar = { time: 43200, open: 815, high: 826, low: 0, close: 0, volume: 54_290 }

    // Adding a corrupt zero-low bar must NOT blow up auto row ticks from ~₹2 to ₹40.
    const cleanTicks = autoOrderFlowRowTicks(cleanBars, 0.05)
    const withCorruptTicks = autoOrderFlowRowTicks([...cleanBars, corruptBar], 0.05)
    expect(withCorruptTicks).toBe(cleanTicks)

    const profile = createChartProfile('order-flow', settings(), { ...context, tickSize: 0.05 })
    profile.attached?.({ requestUpdate: vi.fn() })
    profile.setBars([...cleanBars, corruptBar])
    const fp = (profile as unknown as { footprint: { _bars: { time: number }[] } }).footprint
    // The corrupted 0.00 bar is skipped rather than filling footprints down to 0.00.
    expect(fp._bars.map((b) => b.time)).toEqual([0, 14400, 28800])
  })

  it('delegates visible autoscale to the primary OHLC series so old history never flattens footprints', () => {
    const profile = createChartProfile('order-flow', settings(), { ...context, tickSize: 0.05 })
    profile.attached?.({ requestUpdate: vi.fn() })
    profile.setBars([
      { time: 0, open: 880, high: 885, low: 875, close: 878, volume: 10_000 },
      { time: 60, open: 776.5, high: 777.0, low: 776.0, close: 776.5, volume: 500 },
    ])
    expect(profile.autoscaleInfo?.()).toBeNull()
  })

  it('backfills pre-open volume from 1m intrabars and merges live prints when opened mid-bar', () => {
    const t0 = 1_795_664_700 // 09:15 IST 4h bar open
    const feed = new OrderFlowFeed({
      intervalSec: 14_400,
      anchorSec: t0,
      tickSize: 0.05,
      rowTicks: 5, // 0.25 rows
      minTradeSize: 0,
      initialSide: 'bid',
    })
    // User opens the 4h chart 2 hours into the bar; only 733 live shares arrived at 87.05.
    feed.ingest({ ltp: 87.05, ltq: 733, timeSec: t0 + 7200 })

    // 1m intrabars captured real volume across 89.40 -> 87.05 before 11:15.
    const intrabars = [
      { time: t0, open: 88.05, high: 89.4, low: 88.0, close: 89.0, volume: 1_000_000 },
      { time: t0 + 60, open: 89.0, high: 89.0, low: 87.5, close: 87.6, volume: 900_000 },
      { time: t0 + 120, open: 87.6, high: 87.6, low: 87.05, close: 87.05, volume: 620_000 },
    ]
    const profile = createChartProfile(
      'order-flow',
      { ...settings(), rowSizeMode: 'manual', ticksPerRow: 5 },
      {
        tickSize: 0.05,
        exchange: 'NSE',
        timezone: 'Asia/Kolkata',
        intervalSeconds: 14_400,
        orderFlow: () => feed,
        intrabarBars: () => intrabars,
      }
    )
    profile.attached?.({ requestUpdate: vi.fn() })
    profile.setBars([
      { time: t0, open: 88.05, high: 89.4, low: 87.05, close: 87.05, volume: 2_520_733 },
    ])

    const fp = (
      profile as unknown as {
        footprint: { _bars: { delta: number; cells: { price: number; bidVol: number; askVol: number }[] }[] }
      }
    ).footprint
    const bar = fp._bars[0]
    const totalVol = bar.cells.reduce((sum, c) => sum + c.bidVol + c.askVol, 0)
    // Exact total exchange volume preserved (never dropped to just 733 shares!).
    expect(totalVol).toBe(2_520_733)
    // Spans the entire 87.00..89.50 price range instead of a single lone row at 87.05.
    expect(bar.cells.length).toBeGreaterThanOrEqual(8)
    // Live flat lower-circuit print at 87.05 was classified as sell (initialSide: 'bid').
    expect(feed.bars()[0].delta).toBe(-733)
  })

  it('scopes active footprint bars to the recent session window on deep 1m histories', () => {
    const start = Date.UTC(2026, 10, 20, 3, 45) / 1000
    // 1,600 bars (~4+ trading days of 1m history)
    const manyBars = Array.from({ length: 1600 }, (_, i) => ({
      time: start + i * 60,
      open: 100 + (i % 5) * 0.1,
      high: 100.6 + (i % 5) * 0.1,
      low: 99.8 + (i % 5) * 0.1,
      close: 100.2 + (i % 5) * 0.1,
      volume: 1000,
    }))
    const profile = createChartProfile(
      'order-flow',
      { ...settings(), rowSizeMode: 'manual', ticksPerRow: 2 },
      { ...context, tickSize: 0.05 }
    )
    profile.attached?.({ requestUpdate: vi.fn() })
    profile.setBars(manyBars)
    const fp = (profile as unknown as { footprint: { _bars: { time: number }[] } }).footprint
    // Bounded to the recent active session window (not all 1,600 bars) while
    // always including the latest forming bar.
    expect(fp._bars.length).toBeLessThan(800)
    expect(fp._bars.length).toBeGreaterThanOrEqual(400)
    expect(fp._bars[fp._bars.length - 1].time).toBe(manyBars[manyBars.length - 1].time)
  })

  it('never paints green or red fills on zero-volume cells in profile, heatmap, or ladder styles', () => {
    const t0 = 1_795_664_700
    const feed = new OrderFlowFeed({
      intervalSec: 60,
      anchorSec: t0,
      tickSize: 0.05,
      rowTicks: 1,
      minTradeSize: 0,
      initialSide: 'ask',
    })
    // Top row: 0 | 333, middle row (gap via showEmptyRows): 0 | 0, bottom row: 66 | 0
    feed.ingest({ ltp: 221.0, ltq: 333, timeSec: t0 + 1, side: 'ask' })
    feed.ingest({ ltp: 220.9, ltq: 66, timeSec: t0 + 2, side: 'bid' })

    const bar = { time: t0, open: 220.9, high: 221.0, low: 220.9, close: 221.0, volume: 399 }

    const renderFills = (cellStyle: 'profile' | 'heatmap' | 'ladder') => {
      const profile = createChartProfile(
        'order-flow',
        { ...settings(), cellStyle, rowSizeMode: 'manual', ticksPerRow: 1, showEmptyRows: true, showPoc: false, showValueArea: false, showBarCards: false },
        { tickSize: 0.05, exchange: 'NSE', timezone: 'Asia/Kolkata', intervalSeconds: 60, orderFlow: () => feed }
      )
      profile.attached?.({ requestUpdate: vi.fn() })
      profile.setBars([bar])

      const fills: { x: number; y: number; w: number; h: number; style: string }[] = []
      let currentStyle = ''
      let pendingRect: { x: number; y: number; w: number; h: number } | null = null
      const ctx = new Proxy(
        {
          set fillStyle(v: string) {
            currentStyle = v
          },
          get fillStyle() {
            return currentStyle
          },
          measureText: (text: string) => ({ width: text.length * 6 }),
          roundRect: (x: number, y: number, w: number, h: number) => {
            pendingRect = { x, y, w, h }
          },
          fill: () => {
            if (pendingRect) {
              fills.push({ ...pendingRect, style: currentStyle })
              pendingRect = null
            }
          },
        },
        { get: (target, key) => (key in target ? Reflect.get(target, key) : () => {}) }
      ) as unknown as CanvasRenderingContext2D

      // Map prices cleanly: 221.00 -> y=100, 220.95 -> y=140, 220.90 -> y=180
      const priceToY = (p: number) => 100 + ((221.0 - p) / 0.05) * 40
      const rc = {
        dpr: 1,
        plotWidth: 400,
        plotHeight: 300,
        theme: {
          background: '#131722',
          upColor: '#26a69a',
          downColor: '#ef5350',
          axisText: '#9ca3af',
        },
        dataLayer: { timeToIndex: (t: number) => (t === t0 ? 1 : undefined) },
        timeScale: {
          barSpacing: 160,
          indexToX: (idx: number) => idx * 160,
          visibleRange: () => ({ from: 0, to: 3 }),
        },
        priceScale: { priceToY },
      }
      profile.draw(ctx, rc as never)
      return fills
    }

    const isVisibleFill = (style: string) => !style.endsWith(',0)') && style !== 'transparent'

    for (const style of ['profile', 'heatmap', 'ladder'] as const) {
      const fills = renderFills(style).filter((f) => isVisibleFill(f.style))
      // Match rows by vertical midpoint to avoid floating-point boundary jitter:
      // 221.00 -> midY ~ 100, 220.95 (0|0) -> midY ~ 140, 220.90 (66|0) -> midY ~ 180.
      const midY = (f: { y: number; h: number }) => f.y + f.h / 2

      // Row 220.95 is 0 | 0 -> ZERO visible cell fills anywhere on that row!
      const middleRowFills = fills.filter((f) => midY(f) >= 120 && midY(f) <= 160)
      expect(middleRowFills, `${style}: empty 0|0 row must have no color fill`).toEqual([])

      // Row 221.00 is 0 | 333 -> ONLY the ask (right) side has a visible fill!
      const topRowFills = fills.filter((f) => midY(f) >= 80 && midY(f) < 120)
      expect(topRowFills.length, `${style}: 0|333 must fill only ask side`).toBe(1)

      // Row 220.90 is 66 | 0 -> ONLY the bid (left) side has a visible fill!
      const bottomRowFills = fills.filter((f) => midY(f) > 160 && midY(f) <= 200)
      expect(bottomRowFills.length, `${style}: 66|0 must fill only bid side`).toBe(1)
      expect(bottomRowFills[0].x).toBeLessThan(topRowFills[0].x)
    }
  })
})

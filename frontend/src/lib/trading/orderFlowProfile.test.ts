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
})

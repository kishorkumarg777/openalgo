import { describe, expect, it } from 'vitest'
import {
  MAX_FOOTPRINT_BARS,
  NO_QUANTITY_AFTER,
  OrderFlowFeed,
  type OrderFlowFeedOptions,
} from './orderFlowFeed'

const options: OrderFlowFeedOptions = {
  intervalSec: 60,
  anchorSec: 0,
  tickSize: 0.05,
  rowTicks: 1,
  minTradeSize: 0,
}

const feed = (patch: Partial<OrderFlowFeedOptions> = {}) =>
  new OrderFlowFeed({ ...options, ...patch })

const total = (bar: { cells: { bidVol: number; askVol: number }[] }) =>
  bar.cells.reduce((sum, cell) => sum + cell.bidVol + cell.askVol, 0)

describe('OrderFlowFeed quantity', () => {
  it('takes the change in cumulative volume as the traded quantity', () => {
    const f = feed()
    // The first volume reading has nothing to subtract from.
    expect(f.ingest({ ltp: 100, volume: 1000, timeSec: 10 })).toBe(false)
    expect(f.ingest({ ltp: 100.05, volume: 1030, timeSec: 11 })).toBe(true)
    expect(f.ingest({ ltp: 100, volume: 1050, timeSec: 12 })).toBe(true)
    const [bar] = f.bars()
    expect(total(bar)).toBe(50)
    expect(f.tradeCount()).toBe(2)
  })

  it('reads a buy when the price rises and a sell when it falls', () => {
    const f = feed()
    f.ingest({ ltp: 100, volume: 1000, timeSec: 10 })
    f.ingest({ ltp: 100.05, volume: 1030, timeSec: 11 })
    expect(f.bars()[0].delta).toBe(30)
    f.ingest({ ltp: 100, volume: 1040, timeSec: 12 })
    expect(f.bars()[0].delta).toBe(20)
  })

  it('classifies aggressive side from bid/ask quotes when present', () => {
    const f = feed()
    f.ingest({ ltp: 650.0, volume: 1000, bid: 649.95, ask: 650.05, timeSec: 10 })
    // Trade at bid despite unchanged price is seller-initiated.
    f.ingest({ ltp: 649.95, volume: 1050, bid: 649.95, ask: 650.05, timeSec: 11 })
    expect(f.bars()[0].delta).toBe(-50)
    // Trade at ask is buyer-initiated.
    f.ingest({ ltp: 650.05, volume: 1120, bid: 649.95, ask: 650.05, timeSec: 12 })
    expect(f.bars()[0].delta).toBe(20)
  })

  it('falls back to the last traded quantity when the feed has no volume', () => {
    const f = feed()
    expect(f.ingest({ ltp: 100, ltq: 25, timeSec: 10 })).toBe(true)
    expect(total(f.bars()[0])).toBe(25)
  })

  it('ignores an update that repeats the volume, which is a price refresh', () => {
    const f = feed()
    f.ingest({ ltp: 100, volume: 1000, timeSec: 10 })
    expect(f.ingest({ ltp: 100.05, volume: 1000, ltq: 40, timeSec: 11 })).toBe(false)
    expect(f.tradeCount()).toBe(0)
  })

  it('starts the count again when volume falls, which is a new trading day', () => {
    const f = feed()
    f.ingest({ ltp: 100, volume: 5000, timeSec: 10 })
    expect(f.ingest({ ltp: 100, volume: 200, ltq: 15, timeSec: 11 })).toBe(true)
    expect(total(f.bars()[0])).toBe(15)
  })

  it('ignores prices and times that are not numbers', () => {
    const f = feed()
    expect(f.ingest({ ltp: 0, ltq: 5, timeSec: 10 })).toBe(false)
    expect(f.ingest({ ltp: Number.NaN, ltq: 5, timeSec: 10 })).toBe(false)
    expect(f.ingest({ ltp: 100, ltq: 5, timeSec: Number.NaN })).toBe(false)
    expect(f.bars()).toHaveLength(0)
  })
})

describe('OrderFlowFeed bars', () => {
  it('opens one bar per interval, lined up with the anchor', () => {
    const f = feed({ anchorSec: 30 })
    f.ingest({ ltp: 100, ltq: 1, timeSec: 31 })
    f.ingest({ ltp: 100.05, ltq: 1, timeSec: 89 })
    f.ingest({ ltp: 100.1, ltq: 1, timeSec: 91 })
    const times = f.bars().map((bar) => bar.time)
    expect(times).toEqual([30, 90])
  })

  it('accepts an update stamped before the newest one without failing', () => {
    const f = feed()
    f.ingest({ ltp: 100, ltq: 1, timeSec: 50 })
    expect(() => f.ingest({ ltp: 100.05, ltq: 1, timeSec: 40 })).not.toThrow()
    expect(f.bars()).toHaveLength(1)
    expect(total(f.bars()[0])).toBe(2)
  })

  it('hands back the same array until something changes', () => {
    const f = feed()
    f.ingest({ ltp: 100, ltq: 1, timeSec: 10 })
    const first = f.bars()
    expect(f.bars() === first).toBe(true)
    f.ingest({ ltp: 100.05, ltq: 1, timeSec: 11 })
    expect(f.bars() === first).toBe(false)
  })

  it('keeps no more than the newest bars', () => {
    const f = feed()
    for (let i = 0; i < MAX_FOOTPRINT_BARS + 25; i++) {
      f.ingest({ ltp: 100, ltq: 1, timeSec: i * 60 })
    }
    expect(f.bars()).toHaveLength(MAX_FOOTPRINT_BARS)
    expect(f.bars()[f.bars().length - 1].time).toBe((MAX_FOOTPRINT_BARS + 24) * 60)
  })
})

describe('OrderFlowFeed rows', () => {
  it('regroups what has traded when the row size changes', () => {
    const f = feed()
    f.ingest({ ltp: 100, ltq: 10, timeSec: 10 })
    f.ingest({ ltp: 100.05, ltq: 20, timeSec: 11 })
    f.ingest({ ltp: 100.4, ltq: 30, timeSec: 12 })
    const before = f.bars()[0]
    expect(before.cells.length).toBeGreaterThan(2)
    f.configure(20, 0)
    const after = f.bars()[0]
    expect(after.cells.length).toBeLessThan(before.cells.length)
    expect(total(after)).toBe(60)
  })

  it('does not rebuild when nothing changed', () => {
    const f = feed()
    f.ingest({ ltp: 100, ltq: 10, timeSec: 10 })
    const first = f.bars()
    f.configure(options.rowTicks, options.minTradeSize)
    expect(f.bars() === first).toBe(true)
  })

  it('leaves small prints out of the rows but keeps the bar', () => {
    const f = feed({ minTradeSize: 50 })
    f.ingest({ ltp: 100, ltq: 10, timeSec: 10 })
    f.ingest({ ltp: 100.05, ltq: 80, timeSec: 11 })
    expect(total(f.bars()[0])).toBe(80)
  })
})

describe('OrderFlowFeed missing quantity', () => {
  it('says so after enough updates with none usable', () => {
    const f = feed()
    for (let i = 0; i < NO_QUANTITY_AFTER - 1; i++) f.ingest({ ltp: 100 + i * 0.05, timeSec: i })
    expect(f.lacksQuantity()).toBe(false)
    f.ingest({ ltp: 110, timeSec: 999 })
    expect(f.lacksQuantity()).toBe(true)
  })

  it('stays quiet once a trade has been recorded', () => {
    const f = feed()
    f.ingest({ ltp: 100, ltq: 5, timeSec: 1 })
    for (let i = 0; i < NO_QUANTITY_AFTER * 2; i++) f.ingest({ ltp: 100, timeSec: 2 + i })
    expect(f.lacksQuantity()).toBe(false)
  })
})

describe('OrderFlowFeed hydration', () => {
  it('hydrates recorded backend prints without duplicating live ticks', () => {
    const f = feed()
    // Simulate a live tick arriving before the backend hydration response returns.
    f.ingest({ ltp: 650.2, ltq: 25, bid: 650.15, ask: 650.2, timeSec: 130 })

    // Backend returns older prints plus an overlapping print at t=130.
    const added = f.hydrate([
      [61, 649.8, 40, 0],
      [75, 650.0, 60, 1],
      [121, 650.1, 30, 1],
      [130, 650.2, 25, 1],
    ])
    expect(added).toBe(3)
    expect(f.hydratedTradeCount()).toBe(3)
    expect(f.tradeCount()).toBe(4)
    expect(f.recordedBarCount()).toBe(2)

    const bars = f.bars()
    expect(bars.map((b) => b.time)).toEqual([60, 120])
    expect(total(bars[0])).toBe(100)
    expect(bars[0].delta).toBe(20)
    expect(total(bars[1])).toBe(55)
    expect(bars[1].delta).toBe(55)

    // Re-hydrating idempotently keeps exact counts without duplicating prints.
    f.hydrate([
      [61, 649.8, 40, 0],
      [75, 650.0, 60, 1],
      [121, 650.1, 30, 1],
    ])
    expect(f.tradeCount()).toBe(4)
    expect(total(f.bars()[0])).toBe(100)
    expect(total(f.bars()[1])).toBe(55)
  })
})

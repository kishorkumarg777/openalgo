/**
 * Live order flow for the Order Flow chart type.
 *
 * Turns the terminal's quote ticks into footprint bars: each print is given a
 * traded quantity, a side (buy or sell), and a row, and the library's
 * aggregator builds one footprint bar per chart interval.
 *
 * What this can and cannot know, so nobody mistakes it for exchange tape:
 *
 * - Quantity comes from the change in cumulative day volume between two quote
 *   updates, or from the last traded quantity when the feed has no volume. A
 *   broker that coalesces several trades into one update shows them as one
 *   print at the latest price.
 * - Side comes from the quote rule (at/above ask = buy, at/below bid = sell)
 *   whenever top-of-book bid/ask are present on the update, falling back to the
 *   tick rule (above last print = buy, below = sell, equal keeps prior side)
 *   when the quote feed carries no bid or ask.
 * - Nothing is stored. Bars exist for this session of the chart only, and start
 *   when the chart type is selected.
 *
 * This module imports the profile tier, so it is loaded on demand with
 * `import()`, never statically from the terminal.
 */
import {
  createTradeClassifier,
  FootprintAggregator,
  type FootprintBar,
  type FootprintTick,
} from 'openalgo-charts/profile'

/** Bars kept. Older bars, and the prints they were built from, are dropped. */
export const MAX_FOOTPRINT_BARS = 600
/** Prints kept for rebuilding the rows when the row size changes. */
export const MAX_RETAINED_TICKS = 250_000
const TICK_TRIM_SLACK = 10_000
/** Quote updates seen with no usable quantity before the feed says so. */
export const NO_QUANTITY_AFTER = 60

/** Compact backend-recorded print tuple: [timeSec, price, qty, sideCode (1=ask, 0=bid)]. */
export type RecordedOrderFlowTick = readonly [
  timeSec: number,
  price: number,
  qty: number,
  sideCode: number,
]

export interface OrderFlowTick {
  ltp: number
  /** Last traded quantity from the feed, when it has one. */
  ltq?: number
  /** Cumulative day volume from the feed, when it has one. */
  volume?: number
  /** Top-of-book bid at trade time, when available. */
  bid?: number
  /** Top-of-book ask at trade time, when available. */
  ask?: number
  /** Seconds since the Unix epoch. */
  timeSec: number
}

export interface OrderFlowFeedOptions {
  /** Chart interval, in seconds. One footprint bar per interval. */
  intervalSec: number
  /** A known bar start, so buckets line up with the chart's own bars. */
  anchorSec: number
  /** The instrument's tick size. */
  tickSize: number
  /** Ticks per row: rows are tickSize * rowTicks tall. */
  rowTicks: number
  /** Count only single prints of at least this quantity. 0 counts all. */
  minTradeSize: number
}

export class OrderFlowFeed {
  private opts: OrderFlowFeedOptions
  private aggregator: FootprintAggregator
  private classify = createTradeClassifier('quote')
  private prevVolume: number | null = null
  private ticks: FootprintTick[] = []
  private list: FootprintBar[] = []
  private snapshot: readonly FootprintBar[] = []
  private dirty = false
  private lastTime = Number.NEGATIVE_INFINITY
  private liveStartTime: number | null = null
  private seen = 0
  private recorded = 0
  private hydrated = 0
  private lastQty = 0

  constructor(opts: OrderFlowFeedOptions) {
    this.opts = { ...opts }
    this.aggregator = this.makeAggregator()
  }

  private makeAggregator(): FootprintAggregator {
    const { intervalSec, anchorSec, tickSize, rowTicks, minTradeSize } = this.opts
    return new FootprintAggregator(
      { mode: 'interval', seconds: intervalSec, anchorSec },
      tickSize,
      rowTicks,
      { minTradeSize }
    )
  }

  /**
   * Feed one quote update. Returns true when it recorded a trade, false when it
   * carried none (a price refresh, the first volume reading, a bad value).
   */
  ingest(tick: OrderFlowTick): boolean {
    this.lastQty = 0
    if (!(tick.ltp > 0) || !Number.isFinite(tick.ltp) || !Number.isFinite(tick.timeSec)) {
      return false
    }
    this.seen += 1
    const qty = this.quantity(tick)
    if (!(qty > 0)) return false
    // The aggregator refuses a tick older than the last one it took. A late
    // update is placed at the newest time rather than dropped or thrown.
    const time = Math.max(tick.timeSec, this.lastTime)
    if (this.liveStartTime === null) this.liveStartTime = time
    const bid = typeof tick.bid === 'number' && tick.bid > 0 ? tick.bid : undefined
    const ask = typeof tick.ask === 'number' && tick.ask > 0 ? tick.ask : undefined
    const side = this.classify({ price: tick.ltp, qty, bid, ask })
    const print: FootprintTick = { time, price: tick.ltp, qty, side }
    this.lastTime = time
    this.lastQty = qty
    this.ticks.push(print)
    this.apply(print)
    this.recorded += 1
    if (this.ticks.length > MAX_RETAINED_TICKS + TICK_TRIM_SLACK) {
      this.ticks.splice(0, TICK_TRIM_SLACK)
    }
    return true
  }

  /**
   * Prepend backend-recorded classified ticks collected earlier in the session
   * (or prior sessions) without disturbing live ticks that already arrived or
   * the live cumulative-volume baseline.
   */
  hydrate(records: readonly RecordedOrderFlowTick[]): number {
    if (!records.length) return 0
    const cutoff = this.liveStartTime
    const incoming: FootprintTick[] = []
    let prevTs = Number.NEGATIVE_INFINITY
    for (const row of records) {
      const [rawTime, price, qty, sideCode] = row
      if (
        !Number.isFinite(rawTime) ||
        !(price > 0) ||
        !Number.isFinite(price) ||
        !(qty > 0) ||
        !Number.isFinite(qty)
      ) {
        continue
      }
      if (cutoff !== null && rawTime >= cutoff) continue
      const time = Math.max(rawTime, prevTs)
      prevTs = time
      incoming.push({
        time,
        price,
        qty: Math.round(qty),
        side: sideCode === 1 ? 'ask' : 'bid',
      })
    }
    if (!incoming.length) return 0

    // Keep only already-ingested live ticks (>= cutoff) so repeated hydrations
    // replace prior hydrated prints idempotently instead of duplicating them.
    const liveOnly =
      cutoff !== null ? this.ticks.filter((t) => t.time >= cutoff) : []
    const combined = [...incoming, ...liveOnly]
    if (combined.length > MAX_RETAINED_TICKS) {
      combined.splice(0, combined.length - MAX_RETAINED_TICKS)
    }
    this.ticks = combined
    this.hydrated = incoming.length
    this.recorded = combined.length
    this.aggregator = this.makeAggregator()
    this.list = []
    for (const print of this.ticks) this.apply(print)
    this.lastTime = this.ticks.length
      ? this.ticks[this.ticks.length - 1].time
      : Number.NEGATIVE_INFINITY
    this.dirty = true
    return incoming.length
  }

  /**
   * The traded quantity this update adds. Cumulative volume wins when the feed
   * has it, because it counts every trade since the last update; the last
   * traded quantity is the fallback for a feed without volume.
   */
  private quantity(tick: OrderFlowTick): number {
    const ltq = typeof tick.ltq === 'number' && tick.ltq > 0 ? tick.ltq : 0
    const volume = typeof tick.volume === 'number' && tick.volume > 0 ? tick.volume : null
    if (volume === null) return ltq
    const previous = this.prevVolume
    this.prevVolume = volume
    // The first reading has nothing to subtract from.
    if (previous === null) return 0
    // Volume going down is a new trading day: this update starts the count.
    if (volume < previous) return ltq
    return volume - previous
  }

  private apply(print: FootprintTick): void {
    const update = this.aggregator.onTick(print)
    const last = this.list[this.list.length - 1]
    if (last && last.time === update.bar.time) this.list[this.list.length - 1] = update.bar
    else this.list.push(update.bar)
    this.dirty = true
    if (this.list.length > MAX_FOOTPRINT_BARS) {
      this.list.splice(0, this.list.length - MAX_FOOTPRINT_BARS)
      const from = this.list[0].time
      const keep = this.ticks.findIndex((t) => t.time >= from)
      if (keep > 0) this.ticks.splice(0, keep)
    }
  }

  /** The footprint bars so far, oldest first. A new array whenever they changed. */
  bars(): readonly FootprintBar[] {
    if (this.dirty) {
      this.snapshot = [...this.list]
      this.dirty = false
    }
    return this.snapshot
  }

  /**
   * Change the row size or the smallest counted print. Bars are rebuilt from
   * the prints still held, so a wider row regroups what has already traded.
   */
  configure(rowTicks: number, minTradeSize: number): void {
    if (rowTicks === this.opts.rowTicks && minTradeSize === this.opts.minTradeSize) return
    this.opts = { ...this.opts, rowTicks, minTradeSize }
    this.aggregator = this.makeAggregator()
    this.list = []
    for (const print of this.ticks) this.apply(print)
    this.dirty = true
  }

  /** True once enough updates have arrived without one usable quantity. */
  lacksQuantity(): boolean {
    return this.seen >= NO_QUANTITY_AFTER && this.recorded === 0
  }

  /** Trades recorded so far (hydrated + live). */
  tradeCount(): number {
    return this.recorded
  }

  /** Trades loaded from the backend tick recorder. */
  hydratedTradeCount(): number {
    return this.hydrated
  }

  /** Number of footprint bars backed by real recorded/live prints. */
  recordedBarCount(): number {
    let count = 0
    for (const bar of this.list) {
      if (bar.cells.length > 0) count += 1
    }
    return count
  }

  /** Active tick multiplier for this feed's aggregator. */
  currentRowTicks(): number {
    return this.opts.rowTicks
  }

  /** Quantity recorded by the most recent live `ingest()` call, or 0 if none. */
  lastTradeQty(): number {
    return this.lastQty
  }

  /** Timestamp of the newest footprint bar, or null before any print arrives. */
  lastBarTime(): number | null {
    return this.list.length ? this.list[this.list.length - 1].time : null
  }
}

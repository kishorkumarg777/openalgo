/** Lazy profile tier adapter. All price distributions come from the chart engine. */
import {
  type Bar,
  type IPrimitive,
  type PrimitiveHost,
  type PrimitiveRenderContext,
  utcSecondsToZonedParts,
  zonedDayIndex,
  zonedWallClockToUtcSeconds,
} from 'openalgo-charts'
import {
  compactVol,
  computeMarketProfile,
  computeVolumeProfileSessions,
  FOOTPRINT_THEMES,
  Footprint,
  type FootprintBar,
  type FootprintCell,
  type FootprintOptions,
  inWindow,
  MarketProfile,
  type MarketProfilePrimitiveOptions,
  type MarketProfileResult,
  priceBuckets,
  type SessionWindow,
  tpoLetter,
  type VolumeProfileSessionResult,
} from 'openalgo-charts/profile'
import type { OrderFlowFeed } from './orderFlowFeed'
import type { ProfileMenuAction } from './profileLayer'
import type {
  OrderFlowSettings,
  ProfileKind,
  ProfileSettings,
  SessionVolumeProfileSettings,
  TpoProfileSettings,
} from './profileSettings'

/** The settings of the two profiles that are built from the chart's own bars. */
type SessionProfileSettings = TpoProfileSettings | SessionVolumeProfileSettings

export interface ChartProfileContext {
  tickSize: number
  exchange: string
  timezone: string
  intervalSeconds: number
  /** Resolve the primary series scale after a left/right/overlay axis move. */
  priceScale?: () => PrimitiveRenderContext['priceScale']
  /**
   * Order Flow only: the live feed the terminal owns. A getter, because the feed
   * loads on demand and outlives the profile, so a chart rebuild keeps the bars.
   */
  orderFlow?: () => OrderFlowFeed | null
  /** Order Flow only: notifies the terminal of the active resolved row tick count. */
  onOrderFlowRowTicks?: (rowTicks: number) => void
}

export interface ChartProfile extends IPrimitive {
  setBars(bars: readonly Bar[]): void
  setSettings(settings: ProfileSettings, context?: ChartProfileContext): void
  warning(): string | null
  contextMenuAt(x: number, y: number): ProfileMenuAction | null
}

export const MAX_PROFILE_ROWS = 1024
const MAX_TPO_WORK = 20_000_000
const MAX_DEVELOPING_POINTS = 64
const MAX_DEVELOPING_WORK = 2_000_000
const INDIAN_EXCHANGES = new Set([
  'NSE',
  'BSE',
  'NFO',
  'BFO',
  'CDS',
  'BCD',
  'MCX',
  'NCDEX',
  'NCO',
  'NSE_INDEX',
  'BSE_INDEX',
])

interface DevelopingPoint {
  time: number
  poc: number
  vah: number
  val: number
}

export interface ProfileSession {
  key: string
  bars: readonly Bar[]
  startTime: number
  endTime: number
  rowSize: number
  volume: VolumeProfileSessionResult | null
  market: MarketProfileResult | null
  initialBalanceAvailable: boolean
  developing: DevelopingPoint[]
}

function sessionZone(context: ChartProfileContext): string {
  return INDIAN_EXCHANGES.has(context.exchange.toUpperCase()) ? 'Asia/Kolkata' : context.timezone
}

function minute(value: string): number {
  const [hour, min] = value.split(':').map(Number)
  return hour * 60 + min
}

function customWindow(settings: SessionProfileSettings, zone: string): SessionWindow | undefined {
  return settings.sessionMode === 'custom'
    ? { startMinute: minute(settings.sessionStart), endMinute: minute(settings.sessionEnd), zone }
    : undefined
}

function sessionDay(time: number, zone: string, window?: SessionWindow): number {
  let day =
    zone === 'Asia/Kolkata' ? Math.floor((time + 19_800) / 86_400) : zonedDayIndex(time, zone)
  if (window && window.endMinute < window.startMinute) {
    const parts = utcSecondsToZonedParts(time, zone)
    if (parts.hour * 60 + parts.minute < window.endMinute) day--
  }
  return day
}

function groupBars(
  bars: readonly Bar[],
  settings: SessionProfileSettings,
  context: ChartProfileContext
): Map<string, Bar[]> {
  const zone = sessionZone(context)
  const window = customWindow(settings, zone)
  const groups = new Map<string, Bar[]>()
  for (const bar of bars) {
    if (
      ![bar.time, bar.open, bar.high, bar.low, bar.close].every(Number.isFinite) ||
      bar.high < bar.low
    )
      continue
    if (window && !inWindow(bar.time, window)) continue
    const day = sessionDay(bar.time, zone, window)
    let key = String(day)
    if (settings.kind === 'tpo' && settings.periodUnit === 'week')
      key = String(Math.floor((day + 3) / 7))
    if (settings.kind === 'tpo' && settings.periodUnit === 'month') {
      const openingDay = new Date(day * 86_400_000)
      key = `${openingDay.getUTCFullYear()}-${openingDay.getUTCMonth() + 1}`
    }
    const group = groups.get(key) ?? []
    group.push({ ...bar, volume: Number.isFinite(bar.volume) && bar.volume! > 0 ? bar.volume : 0 })
    groups.set(key, group)
  }
  if (settings.kind !== 'tpo' || settings.periodCount === 1) return groups
  const merged = new Map<string, Bar[]>()
  const entries = [...groups.entries()]
  for (let i = 0; i < entries.length; i += settings.periodCount) {
    merged.set(
      entries[i][0],
      entries.slice(i, i + settings.periodCount).flatMap(([, group]) => group)
    )
  }
  return merged
}

function sameBars(a: readonly Bar[], b: readonly Bar[]): boolean {
  return (
    a.length === b.length &&
    a.every((bar, i) => {
      const next = b[i]
      return (
        bar.time === next.time &&
        bar.open === next.open &&
        bar.high === next.high &&
        bar.low === next.low &&
        bar.close === next.close &&
        bar.volume === next.volume
      )
    })
  )
}

function unchangedPrefix(a: readonly Bar[], b: readonly Bar[]): number {
  let index = 0
  while (index < Math.min(a.length, b.length)) {
    const left = a[index]
    const right = b[index]
    if (
      left.time !== right.time ||
      left.open !== right.open ||
      left.high !== right.high ||
      left.low !== right.low ||
      left.close !== right.close ||
      left.volume !== right.volume
    )
      break
    index++
  }
  return index
}

function rowSizeFor(
  bars: readonly Bar[],
  settings: SessionProfileSettings,
  context: ChartProfileContext
): { size: number; limited: boolean } {
  const tick =
    Number.isFinite(context.tickSize) && context.tickSize >= 1e-8 ? context.tickSize : 0.05
  let low = Infinity
  let high = -Infinity
  for (const bar of bars) {
    low = Math.min(low, bar.low)
    high = Math.max(high, bar.high)
  }
  const range = high - low
  const automatic =
    settings.kind === 'tpo'
      ? settings.rowSizeMode === 'auto'
      : settings.rowsLayout === 'number-of-rows'
  const rows = settings.kind === 'tpo' ? 24 : settings.rowCount
  let ticks = automatic
    ? Math.max(1, Math.ceil(range / Math.max(1, rows - 1) / tick))
    : settings.ticksPerRow
  // A tiny tick at a large absolute price must still advance the engine's floating-point loop.
  const representable = Number.EPSILON * Math.max(Math.abs(low), Math.abs(high)) * 4
  const required = Math.max(
    1,
    Math.ceil(Math.max(range / (MAX_PROFILE_ROWS - 2), representable) / tick)
  )
  const limited = required > ticks
  ticks = Math.max(ticks, required)
  // Engine bucketing rounds both ends to nearest; alignment can add one row.
  if (automatic) {
    while (Math.round(high / (ticks * tick)) - Math.round(low / (ticks * tick)) + 1 > rows) ticks++
  }
  return { size: ticks * tick, limited }
}

function volumeFor(
  bars: readonly Bar[],
  rowSize: number,
  valueAreaPercent: number
): VolumeProfileSessionResult | null {
  if (!bars.some((bar) => (bar.volume ?? 0) > 0)) return null
  return (
    computeVolumeProfileSessions(bars, {
      session: 'composite',
      tickSize: rowSize,
      valueAreaPercent: valueAreaPercent / 100,
    }).sessions[0] ?? null
  )
}

function windowBoundary(day: number, minute: number, zone: string): number {
  const date = new Date(day * 86_400_000)
  return zonedWallClockToUtcSeconds(
    date.getUTCFullYear(),
    date.getUTCMonth() + 1,
    date.getUTCDate(),
    Math.floor(minute / 60),
    minute % 60,
    0,
    zone
  )
}

interface SourceBlock {
  index: number
  startTime: number
  endTime: number
}

interface AlignedMarketBars {
  bars: Bar[]
  blocks: Map<number, SourceBlock>
  initialBalance: { high: number; low: number } | null
}

function exchangeOpeningMinute(context: ChartProfileContext): number {
  const exchange = context.exchange.toUpperCase()
  if (['NSE', 'BSE', 'NFO', 'BFO', 'NSE_INDEX', 'BSE_INDEX'].includes(exchange)) return 9 * 60 + 15
  return INDIAN_EXCHANGES.has(exchange) ? 9 * 60 : 0
}

/**
 * Give native analytics one stable slot per exchange-clock block. These times are
 * compute-only coordinates: every session/period timestamp is restored below.
 * Native counts must not depend on which minute a partial history began on.
 */
function alignMarketBars(
  group: readonly Bar[],
  settings: TpoProfileSettings,
  context: ChartProfileContext
): AlignedMarketBars {
  const zone = sessionZone(context)
  const window = customWindow(settings, zone)
  const timedWindow = window && window.startMinute !== window.endMinute ? window : undefined
  const blockSeconds = settings.blockMinutes * 60
  const openingMinute = timedWindow?.startMinute ?? exchangeOpeningMinute(context)
  interface DayBlocks {
    day: number
    open: number
    close: number | null
    min: number
    max: number
    base: number
    origin: number
  }
  const days = new Map<number, DayBlocks>()
  const sources = group.map((bar) => {
    const day = sessionDay(bar.time, zone, window)
    let info = days.get(day)
    if (!info) {
      info = {
        day,
        open: windowBoundary(day, openingMinute, zone),
        close: timedWindow
          ? windowBoundary(
              day + (timedWindow.endMinute < timedWindow.startMinute ? 1 : 0),
              timedWindow.endMinute,
              zone
            )
          : null,
        min: Infinity,
        max: -Infinity,
        base: 0,
        origin: 0,
      }
      days.set(day, info)
    }
    const block = Math.floor((bar.time - info.open) / blockSeconds)
    info.min = Math.min(info.min, block)
    info.max = Math.max(info.max, block)
    return { bar, info, block }
  })
  let base = 0
  for (const day of days.values()) {
    // All sessions retain pre-opening bars as additional columns. Known custom
    // windows retain their missing opening/trailing blocks, but omit closures.
    day.origin = Math.min(0, day.min)
    day.base = base
    base +=
      day.close === null
        ? day.max - day.origin + 1
        : Math.ceil((day.close - day.open) / blockSeconds)
  }
  const first = sources[0].info
  const ibEnd = first.open + settings.initialBalanceBlocks * blockSeconds
  let initialBalance: AlignedMarketBars['initialBalance'] = null
  const blocks = new Map<number, SourceBlock>()
  const bars = sources.map(({ bar, info, block }) => {
    const index = info.base + block - info.origin
    const time = index * blockSeconds
    const existing = blocks.get(time)
    if (existing) existing.endTime = bar.time
    else blocks.set(time, { index, startTime: bar.time, endTime: bar.time })
    if (info === first && bar.time >= first.open && bar.time < ibEnd) {
      initialBalance = initialBalance
        ? {
            high: Math.max(initialBalance.high, bar.high),
            low: Math.min(initialBalance.low, bar.low),
          }
        : { high: bar.high, low: bar.low }
    }
    return { ...bar, time }
  })
  return { bars, blocks, initialBalance }
}

/** Restore source times and letters after native analytics consume aligned slots. */
function restoreMarketTimes(
  result: MarketProfileResult,
  aligned: AlignedMarketBars,
  group: readonly Bar[]
): MarketProfileResult {
  return {
    ...result,
    sessions: result.sessions.map((session) => {
      const indexes = new Map<number, number>()
      for (const period of session.periodDetail) {
        indexes.set(period.index, aligned.blocks.get(period.startTime)!.index)
      }
      const mapped = (index: number) => indexes.get(index)!
      const initialBalance = aligned.initialBalance ?? { high: Number.NaN, low: Number.NaN }
      return {
        ...session,
        startTime: group[0].time,
        endTime: group[group.length - 1].time,
        initialBalance,
        rangeExtension: {
          up: Math.max(0, session.high - initialBalance.high),
          down: Math.max(0, initialBalance.low - session.low),
        },
        periods: session.periodDetail.length ? mapped(session.periodDetail.at(-1)!.index) + 1 : 0,
        levels: session.levels.map((level) => {
          const periods = level.periods.map(mapped)
          return { ...level, periods, letters: periods.map(tpoLetter).join('') }
        }),
        periodDetail: session.periodDetail.map((period) => ({
          ...period,
          startTime: aligned.blocks.get(period.startTime)!.startTime,
          endTime: aligned.blocks.get(period.endTime)!.endTime,
          index: mapped(period.index),
          letter: tpoLetter(mapped(period.index)),
        })),
        developing: session.developing.map((point) => ({
          ...point,
          time: aligned.blocks.get(point.time)!.endTime,
          periodIndex: mapped(point.periodIndex),
        })),
      }
    }),
  }
}

/** Snapshot cache is bounded by the supplied history/replay prefix, never by wall time. */
export class ProfileSessionStore {
  sessions: readonly ProfileSession[] = []
  warning: string | null = null
  private cache = new Map<string, ProfileSession>()
  private settings: SessionProfileSettings
  private context: ChartProfileContext

  constructor(settings: SessionProfileSettings, context: ChartProfileContext) {
    this.settings = settings
    this.context = context
  }

  setSettings(settings: SessionProfileSettings, context = this.context): void {
    if (
      JSON.stringify(settings) !== JSON.stringify(this.settings) ||
      JSON.stringify(context) !== JSON.stringify(this.context)
    )
      this.clear()
    this.settings = settings
    this.context = context
  }

  clear(): void {
    this.cache.clear()
    this.sessions = []
    this.warning = null
  }

  setBars(bars: readonly Bar[]): void {
    const next = new Map<string, ProfileSession>()
    this.warning = null
    if (
      this.settings.kind === 'tpo' &&
      this.context.intervalSeconds > this.settings.blockMinutes * 60
    ) {
      this.clear()
      this.warning =
        'The source interval must be no larger than the TPO block size. Select a finer chart interval.'
      return
    }
    for (const [key, group] of groupBars(bars, this.settings, this.context)) {
      const prior = this.cache.get(key)
      const row = rowSizeFor(group, this.settings, this.context)
      if (row.limited)
        this.warning = `Profile rows were widened to stay within ${MAX_PROFILE_ROWS} price rows. Increase ticks per row for this range.`
      if (prior && sameBars(prior.bars, group)) {
        next.set(key, prior)
        continue
      }
      const settings = this.settings
      let market: MarketProfileResult | null = null
      let initialBalanceAvailable = false
      if (settings.kind === 'tpo') {
        const aligned = alignMarketBars(group, settings, this.context)
        const periods = aligned.blocks.size
        let low = Infinity
        let high = -Infinity
        for (const bar of group) {
          low = Math.min(low, bar.low)
          high = Math.max(high, bar.high)
        }
        const rows = Math.ceil((high - low) / row.size) + 2
        if (rows * periods * periods > MAX_TPO_WORK) {
          this.warning =
            'This TPO period contains too much data to calculate interactively. Reduce period count, increase block size, or increase ticks per row.'
          continue
        }
        const tickSize =
          Number.isFinite(this.context.tickSize) && this.context.tickSize >= 1e-8
            ? this.context.tickSize
            : 0.05
        market = computeMarketProfile(aligned.bars, {
          session: 'composite',
          tickSize,
          rowTicks: Math.round(row.size / tickSize),
          blockMinutes: settings.blockMinutes,
          valueAreaPercent: settings.valueAreaPercent / 100,
          initialBalancePeriods: settings.initialBalanceBlocks,
          timezone: sessionZone(this.context),
        })
        market = restoreMarketTimes(market, aligned, group)
        initialBalanceAvailable = aligned.initialBalance !== null
      }
      const volume =
        settings.kind === 'session-volume-profile' || settings.showVolumeProfile
          ? volumeFor(
              group,
              row.size,
              settings.kind === 'tpo' ? settings.volumeValueAreaPercent : settings.valueAreaPercent
            )
          : null
      const developing: DevelopingPoint[] = []
      if (
        settings.kind === 'session-volume-profile' &&
        volume &&
        (settings.showDevelopingPoc || settings.showDevelopingVa)
      ) {
        // A bounded set of exact cumulative snapshots; no interpolated price analytics.
        const limit = Math.min(
          MAX_DEVELOPING_POINTS,
          Math.max(2, Math.floor(MAX_DEVELOPING_WORK / (group.length * volume.levels.length)))
        )
        const stride = Math.max(1, Math.ceil((group.length - 1) / (limit - 1)))
        const reusable =
          prior && prior.rowSize === row.size ? unchangedPrefix(prior.bars, group) : 0
        const snapshots = new Map(prior?.developing.map((point) => [point.time, point]))
        for (let i = 0; i < group.length; i++) {
          if (i % stride !== 0 && i !== group.length - 1) continue
          const cached = i < reusable ? snapshots.get(group[i].time) : undefined
          if (cached) {
            developing.push(cached)
            continue
          }
          const snapshot =
            i === group.length - 1
              ? volume
              : volumeFor(group.slice(0, i + 1), row.size, settings.valueAreaPercent)
          if (snapshot)
            developing.push({
              time: group[i].time,
              poc: snapshot.poc,
              vah: snapshot.vah,
              val: snapshot.val,
            })
        }
      }
      next.set(key, {
        key,
        bars: group,
        startTime: group[0].time,
        endTime: group[group.length - 1].time,
        rowSize: row.size,
        market,
        initialBalanceAvailable,
        volume,
        developing,
      })
    }
    this.cache = next
    this.sessions = [...next.values()]
    if (!this.warning && bars.length > 0) {
      if (this.sessions.length === 0 && this.settings.sessionMode === 'custom') {
        this.warning =
          'No supplied bars fall within the custom session hours. Adjust the session start and end.'
      } else if (
        this.settings.kind === 'session-volume-profile' &&
        this.sessions.length > 0 &&
        this.sessions.every((session) => session.volume === null)
      ) {
        this.warning =
          'The supplied bars contain no volume. A Session Volume Profile cannot be calculated for this data.'
      }
    }
  }
}

interface SessionBounds {
  left: number
  right: number
  width: number
}

function boundsFor(session: ProfileSession, rc: PrimitiveRenderContext): SessionBounds | null {
  const first = rc.dataLayer.timeToIndex(session.startTime)
  const last = rc.dataLayer.timeToIndex(session.endTime)
  if (first === undefined || last === undefined) return null
  const left = rc.timeScale.indexToX(first)
  const right = rc.timeScale.indexToX(last + 1)
  if (!Number.isFinite(left) || !Number.isFinite(right) || right <= left) return null
  return { left, right, width: right - left }
}

function horizontalLine(
  ctx: CanvasRenderingContext2D,
  rc: PrimitiveRenderContext,
  bounds: SessionBounds,
  price: number,
  color: string,
  extend = false,
  width = 1
): void {
  const y = rc.priceScale.priceToY(price) * rc.dpr
  if (!Number.isFinite(y) || y < 0 || y > rc.plotHeight * rc.dpr) return
  ctx.globalAlpha = 1
  ctx.strokeStyle = color
  ctx.lineWidth = width * rc.dpr
  ctx.beginPath()
  ctx.moveTo(Math.max(0, bounds.left) * rc.dpr, y)
  ctx.lineTo((extend ? rc.plotWidth : Math.min(rc.plotWidth, bounds.right)) * rc.dpr, y)
  ctx.stroke()
}

function drawLevels(
  ctx: CanvasRenderingContext2D,
  rc: PrimitiveRenderContext,
  bounds: SessionBounds,
  values: { poc: number; vah: number; val: number },
  settings: SessionProfileSettings
): void {
  if (settings.showVah)
    horizontalLine(ctx, rc, bounds, values.vah, settings.vahColor, settings.extendVah)
  if (settings.showVal)
    horizontalLine(ctx, rc, bounds, values.val, settings.valColor, settings.extendVal)
  if (settings.showPoc)
    horizontalLine(ctx, rc, bounds, values.poc, settings.pocColor, settings.extendPoc, 1.5)
}

interface HistogramStyle {
  widthPercent: number
  placement: 'left' | 'right'
  volumeMode: 'up-down' | 'total' | 'delta'
  upColor: string
  downColor: string
  vaUpColor: string
  vaDownColor: string
  showValues: boolean
  valuesColor: string
}

function drawHistogram(
  ctx: CanvasRenderingContext2D,
  rc: PrimitiveRenderContext,
  session: ProfileSession,
  bounds: SessionBounds,
  settings: HistogramStyle
): void {
  const volume = session.volume
  if (!volume || volume.totalVolume <= 0) return
  const max = Math.max(
    ...volume.levels.map((level) =>
      settings.volumeMode === 'delta' ? Math.abs(level.delta) : level.volume
    )
  )
  if (!(max > 0)) return
  const width = (bounds.width * Math.max(0, Math.min(100, settings.widthPercent))) / 100
  for (const level of volume.levels) {
    const y0 = rc.priceScale.priceToY(level.price + session.rowSize / 2)
    const y1 = rc.priceScale.priceToY(level.price - session.rowSize / 2)
    const top = Math.min(y0, y1)
    const height = Math.max(
      0.5 / rc.dpr,
      Math.abs(y1 - y0) - Math.min(0.6, Math.abs(y1 - y0) * 0.08)
    )
    if (top + height < 0 || top > rc.plotHeight) continue
    const inVa = level.price <= volume.vah && level.price >= volume.val
    const up = inVa ? settings.vaUpColor : settings.upColor
    const down = inVa ? settings.vaDownColor : settings.downColor
    const value = settings.volumeMode === 'delta' ? Math.abs(level.delta) : level.volume
    const rowWidth = (width * value) / max
    const x = settings.placement === 'left' ? bounds.left : bounds.right - rowWidth
    const fill = (offset: number, w: number, color: string) => {
      if (!(w > 0)) return
      ctx.globalAlpha = 1
      ctx.fillStyle = color
      ctx.fillRect((x + offset) * rc.dpr, top * rc.dpr, w * rc.dpr, height * rc.dpr)
    }
    if (settings.volumeMode === 'up-down') {
      const buyWidth = level.volume > 0 ? (rowWidth * level.buyVolume) / level.volume : 0
      fill(0, buyWidth, up)
      fill(buyWidth, rowWidth - buyWidth, down)
    } else fill(0, rowWidth, settings.volumeMode === 'delta' && level.delta < 0 ? down : up)
    if (settings.showValues && height * rc.dpr >= 8 && rowWidth >= 20) {
      const label = compactVol(settings.volumeMode === 'delta' ? level.delta : level.volume)
      const font = Math.min(10, height * 0.85)
      ctx.font = `${font * rc.dpr}px ui-monospace, monospace`
      if (ctx.measureText(label).width + 4 * rc.dpr > rowWidth * rc.dpr) continue
      ctx.fillStyle = settings.valuesColor
      ctx.textAlign = settings.placement === 'left' ? 'left' : 'right'
      ctx.textBaseline = 'middle'
      ctx.fillText(
        label,
        (settings.placement === 'left' ? x + 2 : x + rowWidth - 2) * rc.dpr,
        (top + height / 2) * rc.dpr
      )
    }
  }
}

function drawDeveloping(
  ctx: CanvasRenderingContext2D,
  rc: PrimitiveRenderContext,
  points: readonly DevelopingPoint[],
  key: 'poc' | 'vah' | 'val',
  color: string
): void {
  ctx.globalAlpha = 1
  ctx.strokeStyle = color
  ctx.lineWidth = rc.dpr
  ctx.beginPath()
  let previousY: number | undefined
  for (const point of points) {
    const index = rc.dataLayer.timeToIndex(point.time)
    if (index === undefined) continue
    const x = rc.timeScale.indexToX(index) * rc.dpr
    const y = rc.priceScale.priceToY(point[key]) * rc.dpr
    if (previousY === undefined) ctx.moveTo(x, y)
    else {
      ctx.lineTo(x, previousY)
      ctx.lineTo(x, y)
    }
    previousY = y
  }
  ctx.stroke()
}

function nativeOptions(settings: TpoProfileSettings): Partial<MarketProfilePrimitiveOptions> {
  return {
    blockDisplay: settings.display === 'blocks' ? 'blocks' : 'compact',
    colorMode: 'period',
    periodColors: [
      settings.gradientColor1,
      settings.gradientColor2,
      settings.gradientColor3,
      settings.gradientColor4,
    ],
    outsideVaOpacity: settings.outsideVaOpacity / 100,
    split: settings.split,
    profileSpacing: 0,
    showPoc: false,
    showPocLabel: false,
    showValueArea: false,
    showValueAreaLabels: false,
    fillValueArea: false,
    showTails: false,
    showNakedLevels: false,
    showSessionLabel: false,
    showPoorHighLow: settings.showPoorHighLow,
    poorColor: settings.poorHighLowColor,
    showSinglePrints: settings.showSinglePrints,
    singlePrintColor: settings.singlePrintColor,
    showInitialBalance: settings.showInitialBalance,
    ibColor: settings.initialBalanceColor,
    showSessionOpen: settings.showOpen,
    sessionOpenColor: settings.openColor,
    showLastPrice: settings.showClose,
    lastPriceColor: settings.closeColor,
    showVolumeProfile: false,
  }
}

function periodGradient(settings: TpoProfileSettings, market: MarketProfileResult): string[] {
  const stops = [
    settings.gradientColor1,
    settings.gradientColor2,
    settings.gradientColor3,
    settings.gradientColor4,
  ]
  const channels = stops.map((color) =>
    [1, 3, 5].map((offset) => Number.parseInt(color.slice(offset, offset + 2), 16))
  )
  const session = market.sessions[0]
  const colors: string[] = []
  for (const period of session.periodDetail) {
    const position = (period.index * 3) / Math.max(1, session.periods - 1)
    const left = Math.min(2, Math.floor(position))
    const fraction = position - left
    const rgb = channels[left].map((channel, c) =>
      Math.round(channel + (channels[left + 1][c] - channel) * fraction)
        .toString(16)
        .padStart(2, '0')
    )
    // Keep gaps sparse: missing trading days need no generated colors.
    colors[period.index] = `#${rgb.join('')}`
  }
  return colors
}

interface NativeSession {
  main: MarketProfile
  bright: MarketProfile
  blocks: MarketProfile
  viewEnd: number
}

function splitSettingsIdentity(
  settings: SessionProfileSettings,
  context: ChartProfileContext
): string {
  return JSON.stringify({
    context,
    kind: settings.kind,
    sessionMode: settings.sessionMode,
    sessionStart: settings.sessionStart,
    sessionEnd: settings.sessionEnd,
    ...(settings.kind === 'tpo'
      ? {
          periodUnit: settings.periodUnit,
          periodCount: settings.periodCount,
          blockMinutes: settings.blockMinutes,
          split: settings.split,
        }
      : {}),
  })
}

function applySessionSplit(native: NativeSession, split: boolean): void {
  native.main.setSessionSplit(0, split)
  native.bright.setSessionSplit(0, split)
  native.blocks.setSessionSplit(0, split)
}

class SessionChartProfile implements ChartProfile {
  private host: PrimitiveHost | null = null
  private bars: readonly Bar[] = []
  private store: ProfileSessionStore
  private native = new Map<ProfileSession, NativeSession>()
  private splitOverrides = new Map<string, boolean>()
  private sessionTokens = new Map<string, symbol>()
  private painted = new Map<ProfileSession, NativeSession>()
  private plotSize: { width: number; height: number } | null = null
  private settings: SessionProfileSettings
  private context: ChartProfileContext

  constructor(settings: SessionProfileSettings, context: ChartProfileContext) {
    this.settings = settings
    this.context = context
    this.store = new ProfileSessionStore(settings, context)
  }

  zOrder(): 'top' {
    return 'top'
  }
  // The transparent OHLC series owns visible-price autoscale; old sessions never flatten it.
  autoscaleInfo(): null {
    return null
  }
  warning(): string | null {
    return this.store.warning
  }
  contextMenuAt(x: number, y: number): ProfileMenuAction | null {
    if (
      !this.host ||
      this.settings.kind !== 'tpo' ||
      !this.plotSize ||
      !Number.isFinite(x) ||
      !Number.isFinite(y) ||
      x < 0 ||
      y < 0 ||
      x > this.plotSize.width ||
      y > this.plotSize.height
    )
      return null
    for (const [session, native] of this.painted) {
      if (!native.main.hoverAt(x, y)) continue
      const key = session.key
      const token = this.sessionTokens.get(key)
      if (!token) continue
      const split = !native.main.isSessionSplit(0)
      const zone = sessionZone(this.context)
      const window = customWindow(this.settings, zone)
      const first = sessionDay(session.startTime, zone, window)
      const last = sessionDay(session.endTime, zone, window)
      const formatter = new Intl.DateTimeFormat('en-IN', {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
        timeZone: 'UTC',
      })
      const date = (day: number) => formatter.format(new Date(day * 86_400_000))
      return {
        label: split ? 'Split this session' : 'Unsplit this session',
        sessionLabel: first === last ? date(first) : `${date(first)} – ${date(last)}`,
        run: () => {
          // A live replacement keeps its token. Removal/reappearance, regrouping
          // and detach invalidate menus without discarding saved replay choices.
          if (!this.host || this.sessionTokens.get(key) !== token) return
          const current = [...this.native].find(([entry]) => entry.key === key)?.[1]
          if (!current) return
          this.splitOverrides.set(key, split)
          applySessionSplit(current, split)
          this.painted.clear()
          this.host.requestUpdate()
        },
      }
    }
    return null
  }
  attached(host: PrimitiveHost): void {
    this.host = host
  }
  detached(): void {
    this.host = null
    this.bars = []
    this.store.clear()
    this.native.clear()
    this.splitOverrides.clear()
    this.sessionTokens.clear()
    this.painted.clear()
    this.plotSize = null
  }

  setSettings(settings: SessionProfileSettings, context = this.context): void {
    if (
      splitSettingsIdentity(settings, context) !==
      splitSettingsIdentity(this.settings, this.context)
    ) {
      this.splitOverrides.clear()
      this.sessionTokens.clear()
    }
    this.settings = settings
    this.context = context
    this.store.setSettings(settings, context)
    this.setBars(this.bars)
  }

  setBars(bars: readonly Bar[]): void {
    this.painted.clear()
    this.bars = bars
    this.store.setBars(bars)
    this.sessionTokens = new Map(
      this.store.sessions.map((session) => [
        session.key,
        this.sessionTokens.get(session.key) ?? Symbol(session.key),
      ])
    )
    const nextNative = new Map<ProfileSession, NativeSession>()
    if (this.settings.kind === 'tpo') {
      const settings = this.settings
      for (const session of this.store.sessions) {
        if (!session.market) continue
        const cached = this.native.get(session)
        if (cached) {
          nextNative.set(session, cached)
          continue
        }
        // Extend only the renderer's last slot. Analytics retain their actual final bar time.
        const viewEnd = session.endTime + Math.max(1, this.context.intervalSeconds)
        const result = {
          ...session.market,
          sessions: session.market.sessions.map((s) => ({ ...s, endTime: viewEnd })),
        }
        const base = {
          ...nativeOptions(settings),
          periodColors: periodGradient(settings, result),
          showInitialBalance: settings.showInitialBalance && session.initialBalanceAvailable,
        }
        // Preserve the opening column geometry in every pass, while painting its marker once.
        const quiet = {
          ...base,
          sessionOpenColor: 'transparent',
          showLastPrice: false,
          showPoorHighLow: false,
          showSinglePrints: false,
          showInitialBalance: false,
        }
        const native: NativeSession = {
          main: new MarketProfile(result, {
            ...base,
            opacity:
              settings.display === 'blocks' ? 0.92 : (0.92 * settings.outsideVaOpacity) / 100,
          }),
          bright: new MarketProfile(result, { ...quiet, blockDisplay: 'compact', opacity: 0.92 }),
          blocks: new MarketProfile(result, { ...quiet, blockDisplay: 'blocks', opacity: 0.14 }),
          viewEnd,
        }
        const split = this.splitOverrides.get(session.key)
        if (split !== undefined) applySessionSplit(native, split)
        nextNative.set(session, native)
      }
    }
    this.native = nextNative
    this.host?.requestUpdate()
  }

  draw(ctx: CanvasRenderingContext2D, paneContext: PrimitiveRenderContext): void {
    const priceScale = this.context.priceScale?.() ?? paneContext.priceScale
    const rc = priceScale === paneContext.priceScale ? paneContext : { ...paneContext, priceScale }
    this.painted.clear()
    this.plotSize = { width: rc.plotWidth, height: rc.plotHeight }
    ctx.save()
    ctx.beginPath()
    ctx.rect(0, 0, rc.plotWidth * rc.dpr, rc.plotHeight * rc.dpr)
    ctx.clip()
    const settings = this.settings
    for (const session of this.store.sessions) {
      const bounds = boundsFor(session, rc)
      if (!bounds || bounds.left > rc.plotWidth) continue
      const onScreen = bounds.right >= 0
      if (onScreen) {
        ctx.save()
        ctx.beginPath()
        ctx.rect(bounds.left * rc.dpr, 0, bounds.width * rc.dpr, rc.plotHeight * rc.dpr)
        ctx.clip()
        if (settings.kind === 'session-volume-profile')
          this.drawVolumeSession(ctx, rc, session, bounds, settings)
        else this.drawMarketSession(ctx, rc, session, bounds, settings)
        ctx.restore()
      }
      // Prior extended levels remain visible when their source session is offscreen.
      const levels = settings.kind === 'tpo' ? session.market?.sessions[0] : session.volume
      if (levels && (onScreen || settings.extendPoc || settings.extendVah || settings.extendVal)) {
        drawLevels(
          ctx,
          rc,
          bounds,
          levels,
          onScreen
            ? settings
            : {
                ...settings,
                showPoc: settings.showPoc && settings.extendPoc,
                showVah: settings.showVah && settings.extendVah,
                showVal: settings.showVal && settings.extendVal,
              }
        )
      }
    }
    ctx.restore()
  }

  private drawVolumeSession(
    ctx: CanvasRenderingContext2D,
    rc: PrimitiveRenderContext,
    session: ProfileSession,
    bounds: SessionBounds,
    settings: SessionVolumeProfileSettings
  ): void {
    if (!session.volume) return
    if (settings.showHistogramBox) {
      const high = session.volume.levels[0].price + session.rowSize / 2
      const low =
        session.volume.levels[session.volume.levels.length - 1].price - session.rowSize / 2
      const a = rc.priceScale.priceToY(high)
      const b = rc.priceScale.priceToY(low)
      ctx.globalAlpha = 0.1
      ctx.fillStyle = settings.histogramBoxColor
      ctx.fillRect(
        bounds.left * rc.dpr,
        Math.min(a, b) * rc.dpr,
        bounds.width * rc.dpr,
        Math.abs(b - a) * rc.dpr
      )
    }
    drawHistogram(ctx, rc, session, bounds, settings)
    if (settings.showDevelopingPoc)
      drawDeveloping(ctx, rc, session.developing, 'poc', settings.pocColor)
    if (settings.showDevelopingVa) {
      drawDeveloping(ctx, rc, session.developing, 'vah', settings.vahColor)
      drawDeveloping(ctx, rc, session.developing, 'val', settings.valColor)
    }
  }

  private drawMarketSession(
    ctx: CanvasRenderingContext2D,
    rc: PrimitiveRenderContext,
    session: ProfileSession,
    bounds: SessionBounds,
    settings: TpoProfileSettings
  ): void {
    const native = this.native.get(session)
    const market = session.market?.sessions[0]
    if (!native || !market) return
    const lastIndex = rc.dataLayer.timeToIndex(session.endTime)!
    const firstIndex = rc.dataLayer.timeToIndex(session.startTime)!
    const reservedWidth =
      settings.showVolumeProfile && session.volume
        ? (bounds.width * settings.volumeWidthPercent) / 100
        : 0
    const viewContext = {
      ...rc,
      dataLayer: Object.assign(Object.create(rc.dataLayer), {
        timeToIndex: (time: number) =>
          time === native.viewEnd ? lastIndex + 1 : rc.dataLayer.timeToIndex(time),
      }),
      timeScale: Object.assign(Object.create(rc.timeScale), {
        indexToX: (index: number) => {
          if (index === firstIndex && settings.volumePlacement === 'left')
            return bounds.left + reservedWidth
          if (index === lastIndex + 1 && settings.volumePlacement === 'right')
            return bounds.right - reservedWidth
          return rc.timeScale.indexToX(index)
        },
      }),
    }
    if (settings.display === 'both') native.blocks.draw(ctx, viewContext)
    native.main.draw(ctx, viewContext)
    this.painted.set(session, native)
    if (settings.display !== 'blocks') {
      // Native compact glyphs retain opacity outside VA. Two clipped passes apply the host's dimming control.
      const a = rc.priceScale.priceToY(market.vah + session.rowSize / 2)
      const b = rc.priceScale.priceToY(market.val - session.rowSize / 2)
      ctx.save()
      ctx.beginPath()
      ctx.rect(
        bounds.left * rc.dpr,
        Math.min(a, b) * rc.dpr,
        bounds.width * rc.dpr,
        Math.abs(b - a) * rc.dpr
      )
      ctx.clip()
      native.bright.draw(ctx, viewContext)
      ctx.restore()
    }
    if (settings.showMidpoint)
      horizontalLine(ctx, rc, bounds, (market.high + market.low) / 2, settings.midpointColor)
    if (settings.showVolumeProfile && session.volume) {
      drawHistogram(ctx, rc, session, bounds, {
        widthPercent: settings.volumeWidthPercent,
        placement: settings.volumePlacement,
        volumeMode: 'total',
        upColor: settings.volumeColor,
        downColor: settings.volumeColor,
        vaUpColor: settings.volumeVaColor,
        vaDownColor: settings.volumeVaColor,
        showValues: settings.showVolumeValues,
        valuesColor: settings.volumeValuesColor,
      })
      if (settings.showVolumePoc)
        horizontalLine(ctx, rc, bounds, session.volume.poc, settings.volumePocColor)
      if (settings.showVolumeVah)
        horizontalLine(ctx, rc, bounds, session.volume.vah, settings.volumeVahColor)
      if (settings.showVolumeVal)
        horizontalLine(ctx, rc, bounds, session.volume.val, settings.volumeValColor)
    }
  }
}

const ORDER_FLOW_AUTO_WINDOW = 8
const ORDER_FLOW_TARGET_WINDOW_ROWS = 22
const ORDER_FLOW_TARGET_BAR_ROWS = 10
const NICE_PRICE_STEPS = [1, 2, 2.5, 5] as const

/**
 * Pick a clean, exchange-aligned tick multiplier for Order Flow rows based on
 * the recent visible window's price span and typical candle range (GoCharting-style
 * auto precision so every row stays tall enough for crisp Bid x Ask labels).
 */
export function autoOrderFlowRowTicks(
  bars: readonly Bar[],
  tickSize: number,
  fallbackTicks = 10,
  previousTicks?: number
): number {
  const safeFallback = Math.max(1, Math.round(fallbackTicks))
  if (!bars.length || !(tickSize > 0) || !Number.isFinite(tickSize)) {
    return safeFallback
  }
  const recent = bars.slice(-ORDER_FLOW_AUTO_WINDOW)
  let low = Number.POSITIVE_INFINITY
  let high = Number.NEGATIVE_INFINITY
  for (const bar of recent) {
    if (Number.isFinite(bar.low)) low = Math.min(low, bar.low)
    if (Number.isFinite(bar.high)) high = Math.max(high, bar.high)
  }
  const windowSpan = Number.isFinite(low) && Number.isFinite(high) ? Math.max(0, high - low) : 0

  const spans: number[] = []
  for (const bar of bars.slice(-24)) {
    if (Number.isFinite(bar.high) && Number.isFinite(bar.low) && bar.high >= bar.low) {
      const span = bar.high - bar.low
      if (span > 0) spans.push(span)
    }
  }
  spans.sort((a, b) => a - b)
  const medianSpan = spans.length ? spans[Math.floor(spans.length / 2)] : 0

  const desiredStep = Math.max(
    windowSpan / ORDER_FLOW_TARGET_WINDOW_ROWS,
    medianSpan / ORDER_FLOW_TARGET_BAR_ROWS
  )
  if (!(desiredStep > 0)) return 1

  // Keep the active auto step steady across small live-tick fluctuations.
  if (previousTicks && previousTicks >= 1) {
    const currentStep = previousTicks * tickSize
    if (desiredStep >= currentStep * 0.62 && desiredStep <= currentStep * 1.25) {
      return previousTicks
    }
  }

  const rawTicks = Math.max(1, desiredStep / tickSize)
  if (rawTicks <= 1.25) return 1

  const expMin = Math.floor(Math.log10(tickSize)) - 1
  const expMax = Math.ceil(Math.log10(Math.max(desiredStep, tickSize))) + 2
  const candidates = new Set<number>([1])
  for (let exp = expMin; exp <= expMax; exp++) {
    const scale = 10 ** exp
    for (const base of NICE_PRICE_STEPS) {
      const step = base * scale
      const ticks = step / tickSize
      const rounded = Math.round(ticks)
      if (rounded >= 1 && rounded <= 10_000 && Math.abs(ticks - rounded) < 1e-5) {
        candidates.add(rounded)
      }
    }
  }
  if (candidates.size < 4) {
    for (const mult of [1, 2, 4, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10_000]) {
      candidates.add(mult)
    }
  }

  const sorted = [...candidates].sort((a, b) => a - b)
  let best = sorted[sorted.length - 1]
  let bestDist = Number.POSITIVE_INFINITY
  const logTarget = Math.log(rawTicks)
  for (const cand of sorted) {
    const dist = Math.abs(Math.log(cand) - logTarget)
    if (dist < bestDist) {
      bestDist = dist
      best = cand
    }
  }
  return Math.max(1, Math.min(10_000, best))
}

/** The library's footprint options for these settings. Row height comes from each bar. */
function footprintOptions(
  settings: OrderFlowSettings,
  context: ChartProfileContext
): Partial<FootprintOptions> {
  const themeColors: Partial<FootprintOptions> =
    settings.theme === 'default' ? {} : FOOTPRINT_THEMES[settings.theme]
  return {
    ...themeColors,
    displayMode: settings.displayMode,
    cellStyle: settings.cellStyle,
    intensityScope: settings.intensityScope,
    compactNumbers: settings.compactNumbers,
    textColorMode: settings.textColorMode,
    font: 10,
    minTextHeight: 8,
    textFade: 2,
    timezone: context.timezone,
    cvdReset: 'session',
    volumeDivisor: settings.volumeDivisor,
    showPoc: settings.showPoc,
    pocStyle: 'outline',
    pocColor: themeColors.pocColor ?? '#e2e8f0',
    showValueArea: settings.showValueArea,
    // The footprint takes a fraction of volume; the setting is a percentage.
    valueAreaPercent: settings.valueAreaPercent / 100,
    showDeltaLabels: settings.showDeltaLabels || settings.markDivergence,
    markDivergence: settings.markDivergence,
    statsPosition: 'bar',
    statsRows: settings.showBarCards ? ['volume', 'delta', 'deltaPct'] : [],
    tableRows: settings.showSummaryTable ? ['volume', 'delta', 'deltaPct', 'cvd'] : [],
    showVisibleProfile: settings.showVisibleProfile,
    showEmptyRows: settings.showEmptyRows,
    showUnfinishedAuctions: settings.showUnfinishedAuctions,
    lastPriceColor: themeColors.lastPriceColor ?? '#38bdf8',
    imbalanceRatio: settings.imbalanceRatio,
    imbalanceThreshold: settings.imbalanceThreshold,
    stackedImbalances: settings.stackedImbalances,
    multipleNodeBars: settings.multipleNodeBars > 0 ? Math.max(2, settings.multipleNodeBars) : 0,
    bigOrderMultiple: settings.bigOrderMultiple,
    absorptionMultiple: settings.absorptionMultiple,
    flipBars: settings.flipBars,
    showZones: settings.showZones,
  }
}

/**
 * Approximate a FootprintBar from a historical OHLCV bar when live tick data
 * is unavailable. Volume concentrates inside the candle body vs wicks using
 * exact integer volume conservation, and splits into bid/ask from candle
 * direction and price position within [low, high].
 */
function buildHistoricalFootprintBar(bar: Bar, rowSize: number): FootprintBar | null {
  if (
    !(rowSize > 0) ||
    !Number.isFinite(rowSize) ||
    ![bar.time, bar.open, bar.high, bar.low, bar.close].every(Number.isFinite) ||
    bar.high < bar.low ||
    !(typeof bar.volume === 'number' && Number.isFinite(bar.volume) && bar.volume > 0)
  ) {
    return null
  }
  const span = bar.high - bar.low
  const step =
    span / rowSize > MAX_PROFILE_ROWS
      ? rowSize * Math.ceil(span / (rowSize * MAX_PROFILE_ROWS))
      : rowSize
  const buckets = priceBuckets(bar.low, bar.high, step)
  if (!buckets.length) return null

  const bodyLow = Math.min(bar.open, bar.close)
  const bodyHigh = Math.max(bar.open, bar.close)
  const midBody = (bodyLow + bodyHigh) * 0.5
  const bodyHalf = Math.max(step, (bodyHigh - bodyLow) * 0.5)
  const halfSpan = Math.max(step, span * 0.5)
  const upperWickSpan = Math.max(step, bar.high - bodyHigh)
  const lowerWickSpan = Math.max(step, bodyLow - bar.low)

  const weights = buckets.map((price) => {
    const inBody = price >= bodyLow - step * 0.5 && price <= bodyHigh + step * 0.5
    const structure = inBody
      ? 1.0 + 0.65 * Math.exp(-1.5 * ((price - midBody) / bodyHalf) ** 2)
      : price > bodyHigh
        ? 0.25 + 0.55 * Math.exp(-2.2 * ((price - bodyHigh) / upperWickSpan) ** 2)
        : 0.25 + 0.55 * Math.exp(-2.2 * ((bodyLow - price) / lowerWickSpan) ** 2)
    const envelope = 0.65 + 0.35 * Math.exp(-1.2 * ((price - midBody) / halfSpan) ** 2)
    return structure * envelope
  })
  const totalWeight = weights.reduce((sum, w) => sum + w, 0)

  // Conserve total candle volume exactly via largest-remainder rounding.
  const targetVol = Math.max(1, Math.round(bar.volume))
  const exact = weights.map((w) => (targetVol * w) / totalWeight)
  const rowTotals = exact.map((v) => Math.floor(v))
  let remainder = targetVol - rowTotals.reduce((sum, v) => sum + v, 0)
  if (remainder > 0) {
    const order = exact
      .map((v, idx) => ({ idx, frac: v - rowTotals[idx] }))
      .sort((a, b) => b.frac - a.frac)
    for (let k = 0; k < remainder && k < order.length; k++) {
      rowTotals[order[k].idx] += 1
    }
  }

  const baseAsk =
    span > 0
      ? Math.min(
          0.82,
          Math.max(
            0.18,
            0.5 + 0.22 * ((bar.close - bar.open) / span) + 0.16 * ((bar.close - bar.low) / span - 0.5)
          )
        )
      : bar.close > bar.open
        ? 0.68
        : bar.close < bar.open
          ? 0.32
          : 0.5

  const cells: FootprintCell[] = []
  let delta = 0
  for (let i = 0; i < buckets.length; i++) {
    const total = rowTotals[i]
    if (total <= 0) continue
    const price = buckets[i]
    const rowPos = buckets.length > 1 ? i / (buckets.length - 1) : 0.5
    let rowAsk = baseAsk + (rowPos - 0.5) * 0.16
    if (price > bodyHigh + step * 0.25) rowAsk -= 0.08
    else if (price < bodyLow - step * 0.25) rowAsk += 0.08
    const rowAskRatio = Math.min(0.9, Math.max(0.1, rowAsk))
    const askVol = Math.round(total * rowAskRatio)
    const bidVol = Math.max(0, total - askVol)
    cells.push({ price, bidVol, askVol })
    delta += askVol - bidVol
  }
  if (!cells.length) {
    const askVol = Math.round(targetVol * baseAsk)
    const bidVol = Math.max(0, targetVol - askVol)
    const fallbackPrice = buckets[Math.floor(buckets.length / 2)]
    cells.push({ price: fallbackPrice, bidVol, askVol })
    delta = askVol - bidVol
  }

  return {
    time: bar.time,
    cells,
    delta,
    rowSize: step,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
  }
}

/**
 * Order Flow: the library's footprint, fed by the terminal's live feed for
 * real-time bars and approximated from OHLCV history for older bars.
 */
class OrderFlowChartProfile implements ChartProfile {
  private footprint: Footprint
  private host: PrimitiveHost | null = null
  private settings: OrderFlowSettings
  private context: ChartProfileContext
  private sourceBars: readonly Bar[] = []
  private autoRowTicks: number | undefined
  private historicalCache = new Map<number, { key: string; fp: FootprintBar | null }>()

  constructor(settings: OrderFlowSettings, context: ChartProfileContext) {
    this.settings = settings
    this.context = context
    this.footprint = new Footprint(footprintOptions(settings, context))
    this.sync()
  }

  private feed(): OrderFlowFeed | null {
    return this.context.orderFlow?.() ?? null
  }

  private effectiveRowTicks(tickSize: number): number {
    if (this.settings.rowSizeMode === 'manual') {
      this.autoRowTicks = undefined
      return Math.max(1, this.settings.ticksPerRow)
    }
    const ticks = autoOrderFlowRowTicks(
      this.sourceBars,
      tickSize,
      this.settings.ticksPerRow,
      this.autoRowTicks
    )
    this.autoRowTicks = ticks
    return ticks
  }

  private historicalBar(bar: Bar, rowSize: number): FootprintBar | null {
    const key = `${bar.open}|${bar.high}|${bar.low}|${bar.close}|${bar.volume ?? 0}|${rowSize}`
    const cached = this.historicalCache.get(bar.time)
    if (cached && cached.key === key) return cached.fp
    const fp = buildHistoricalFootprintBar(bar, rowSize)
    this.historicalCache.set(bar.time, { key, fp })
    return fp
  }

  private sync(): void {
    const tickSize =
      Number.isFinite(this.context.tickSize) && this.context.tickSize >= 1e-8
        ? this.context.tickSize
        : 0.05
    const rowTicks = this.effectiveRowTicks(tickSize)
    this.context.onOrderFlowRowTicks?.(rowTicks)
    const feed = this.feed()
    feed?.configure(rowTicks, this.settings.minTradeSize)
    const liveBars = feed?.bars() ?? []
    const liveByTime = new Map<number, FootprintBar>()
    for (const bar of liveBars) {
      if (bar.cells.length > 0) liveByTime.set(bar.time, bar)
    }

    const rowSize = tickSize * rowTicks
    const merged: FootprintBar[] = []
    for (const bar of this.sourceBars) {
      const live = liveByTime.get(bar.time)
      if (live) {
        // Preserve the candle's full OHLC wick envelope when a live feed starts
        // mid-bar, while keeping the live bar's real recorded cells intact.
        merged.push({
          ...live,
          open: bar.open,
          high: Math.max(bar.high, live.high ?? bar.high),
          low: Math.min(bar.low, live.low ?? bar.low),
          close: live.close ?? bar.close,
        })
        liveByTime.delete(bar.time)
      } else {
        const fp = this.historicalBar(bar, rowSize)
        if (fp) merged.push(fp)
      }
    }
    for (const live of liveByTime.values()) {
      merged.push(live)
    }

    this.footprint.setBars(merged)
    this.host?.requestUpdate()
  }

  zOrder() {
    return this.footprint.zOrder()
  }
  attached(host: PrimitiveHost): void {
    this.host = host
    this.footprint.attached(host)
  }
  detached(): void {
    this.host = null
    this.footprint.detached()
  }
  draw(ctx: CanvasRenderingContext2D, rc: PrimitiveRenderContext): void {
    this.footprint.draw(ctx, rc)
  }
  autoscaleInfo() {
    return this.footprint.autoscaleInfo()
  }
  hitTest(x: number, y: number) {
    return this.footprint.hitTest(x, y)
  }
  setBars(bars: readonly Bar[]): void {
    this.sourceBars = bars
    this.sync()
  }
  setSettings(settings: ProfileSettings, context = this.context): void {
    if (settings.kind !== 'order-flow') return
    if (
      settings.rowSizeMode !== this.settings.rowSizeMode ||
      settings.ticksPerRow !== this.settings.ticksPerRow ||
      context.tickSize !== this.context.tickSize
    ) {
      this.autoRowTicks = undefined
      this.historicalCache.clear()
    }
    this.settings = settings
    this.context = context
    this.footprint.setOptions(footprintOptions(settings, context))
    this.sync()
  }
  warning(): string | null {
    return this.feed()?.lacksQuantity()
      ? 'No traded quantity is coming through for this symbol, so Order Flow has nothing to draw. Indices do not carry it; try a stock, future or option.'
      : null
  }
  contextMenuAt(): ProfileMenuAction | null {
    return null
  }
}

export function createChartProfile(
  kind: ProfileKind,
  settings: ProfileSettings,
  context: ChartProfileContext
): ChartProfile {
  if (kind !== settings.kind) throw new Error('Profile kind and settings must match')
  if (settings.kind === 'order-flow') return new OrderFlowChartProfile(settings, context)
  return new SessionChartProfile(settings, context)
}

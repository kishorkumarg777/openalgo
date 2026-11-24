/**
 * Compact GoCharting-style control strip shown above the chart canvas when the
 * pane's chart type is Order Flow. Gives one-click control over cluster style,
 * cell values, Auto/Manual row precision, POC/VA/Cards/Table/Zones, pro order
 * flow signals, and live/recorded tape hydration status.
 */
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ChromeIcon } from '@/lib/trading/glyphs'
import type { OrderFlowSettings } from '@/lib/trading/profileSettings'
import type { OrderFlowUiState } from '@/lib/trading/terminal'
import { cn } from '@/lib/utils'
import { MenuChevron, TB_BTN, TB_COUNT, TB_ON, ToolbarDivider } from './toolbarStyles'

const STYLE_OPTIONS: ReadonlyArray<{ value: OrderFlowSettings['cellStyle']; label: string }> = [
  { value: 'profile', label: 'Profile' },
  { value: 'ladder', label: 'Ladder' },
  { value: 'heatmap', label: 'Heatmap' },
]

const VALUE_OPTIONS: ReadonlyArray<{
  value: OrderFlowSettings['displayMode']
  short: string
  label: string
}> = [
  { value: 'bidask', short: 'Bid × Ask', label: 'Bid × Ask' },
  { value: 'imbalance', short: 'Imbalance', label: 'Imbalance' },
  { value: 'delta', short: 'Delta', label: 'Delta' },
  { value: 'deltaPct', short: 'Delta %', label: 'Delta %' },
  { value: 'volume', short: 'Volume', label: 'Volume' },
  { value: 'volumeDelta', short: 'Vol × Δ', label: 'Volume × Delta' },
  { value: 'deltaShadedVolume', short: 'Δ-Vol', label: 'Volume shaded by delta' },
]

const ROW_PRESETS = [1, 2, 4, 5, 10, 20, 40, 50] as const

function fmtStep(step: number): string {
  if (!Number.isFinite(step) || step <= 0) return '0.05'
  if (step >= 1 && Math.abs(step - Math.round(step)) < 1e-6) return String(Math.round(step))
  return Number(step.toFixed(4)).toString()
}

function fmtCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

export interface OrderFlowBarProps {
  state: OrderFlowUiState
  onPatch: (patch: Record<string, string | number | boolean>) => void
  onSync: () => void
  onOpenSettings: () => void
}

export function OrderFlowBar({ state, onPatch, onSync, onOpenSettings }: OrderFlowBarProps) {
  const { settings, tickSize, effectiveRowSize, recordedBars, tradeCount, syncing } = state

  const styleLabel =
    STYLE_OPTIONS.find((o) => o.value === settings.cellStyle)?.label ?? 'Profile'
  const valueLabel =
    VALUE_OPTIONS.find((o) => o.value === settings.displayMode)?.short ?? 'Bid × Ask'
  const rowLabel =
    settings.rowSizeMode === 'auto'
      ? `Auto (${fmtStep(effectiveRowSize)})`
      : `${settings.ticksPerRow}t (${fmtStep(tickSize * settings.ticksPerRow)})`

  const activeSignals =
    (settings.absorptionMultiple > 0 ? 1 : 0) +
    (settings.bigOrderMultiple > 0 ? 1 : 0) +
    (settings.multipleNodeBars > 0 ? 1 : 0) +
    (settings.flipBars > 0 ? 1 : 0) +
    (settings.showUnfinishedAuctions ? 1 : 0) +
    (settings.markDivergence ? 1 : 0)

  const toggleBtn = (active: boolean) =>
    cn(TB_BTN, 'h-6 px-1.5 text-[11px]', active && TB_ON)

  return (
    <div
      role="toolbar"
      aria-label="Order Flow controls"
      className="oa-terminal-band flex h-8 shrink-0 items-center gap-0.5 overflow-x-auto border-b px-2 text-[11px]"
    >
      {/* Cluster style */}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={`Order Flow style: ${styleLabel}`}
            className={cn(TB_BTN, 'h-6 gap-1 px-1.5 text-[11px]')}
          >
            <span className="text-muted-foreground">Style</span>
            <span className="font-semibold text-foreground">{styleLabel}</span>
            <MenuChevron />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-36">
          {STYLE_OPTIONS.map((opt) => (
            <DropdownMenuItem
              key={opt.value}
              onSelect={() => onPatch({ 'profiles.of.cellStyle': opt.value })}
              className={cn('text-xs', opt.value === settings.cellStyle && 'text-primary font-medium')}
            >
              {opt.label}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Cell values */}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={`Order Flow values: ${valueLabel}`}
            className={cn(TB_BTN, 'h-6 gap-1 px-1.5 text-[11px]')}
          >
            <span className="text-muted-foreground">Values</span>
            <span className="font-semibold text-foreground">{valueLabel}</span>
            <MenuChevron />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-48">
          {VALUE_OPTIONS.map((opt) => (
            <DropdownMenuItem
              key={opt.value}
              onSelect={() => onPatch({ 'profiles.of.displayMode': opt.value })}
              className={cn(
                'text-xs',
                opt.value === settings.displayMode && 'text-primary font-medium'
              )}
            >
              {opt.label}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Row precision (Auto / Manual tick multiplier) */}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={`Order Flow row precision: ${rowLabel}`}
            className={cn(TB_BTN, 'h-6 gap-1 px-1.5 text-[11px] tabular-nums')}
          >
            <span className="text-muted-foreground">Row</span>
            <span className="font-semibold text-foreground">{rowLabel}</span>
            <MenuChevron />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-48">
          <DropdownMenuItem
            onSelect={() => onPatch({ 'profiles.of.rowSizeMode': 'auto' })}
            className={cn(
              'justify-between text-xs',
              settings.rowSizeMode === 'auto' && 'text-primary font-medium'
            )}
          >
            <span>Auto precision</span>
            <span className="text-muted-foreground tabular-nums">{fmtStep(effectiveRowSize)}</span>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuLabel className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Manual tick multiplier
          </DropdownMenuLabel>
          {ROW_PRESETS.map((ticks) => {
            const active =
              settings.rowSizeMode === 'manual' && settings.ticksPerRow === ticks
            return (
              <DropdownMenuItem
                key={ticks}
                onSelect={() =>
                  onPatch({
                    'profiles.of.rowSizeMode': 'manual',
                    'profiles.of.ticksPerRow': ticks,
                  })
                }
                className={cn('justify-between text-xs tabular-nums', active && 'text-primary font-medium')}
              >
                <span>{ticks} {ticks === 1 ? 'tick' : 'ticks'}</span>
                <span className="text-muted-foreground">{fmtStep(tickSize * ticks)}</span>
              </DropdownMenuItem>
            )
          })}
        </DropdownMenuContent>
      </DropdownMenu>

      <ToolbarDivider className="h-3.5" />

      {/* Quick toggles */}
      <button
        type="button"
        aria-pressed={settings.showPoc}
        onClick={() => onPatch({ 'profiles.of.showPoc': !settings.showPoc })}
        className={toggleBtn(settings.showPoc)}
      >
        POC
      </button>
      <button
        type="button"
        aria-pressed={settings.showValueArea}
        onClick={() => onPatch({ 'profiles.of.showValueArea': !settings.showValueArea })}
        className={toggleBtn(settings.showValueArea)}
      >
        VA
      </button>
      <button
        type="button"
        aria-pressed={settings.showBarCards}
        onClick={() => onPatch({ 'profiles.of.showBarCards': !settings.showBarCards })}
        className={toggleBtn(settings.showBarCards)}
      >
        Cards
      </button>
      <button
        type="button"
        aria-pressed={settings.showSummaryTable}
        onClick={() => onPatch({ 'profiles.of.showSummaryTable': !settings.showSummaryTable })}
        className={toggleBtn(settings.showSummaryTable)}
      >
        Table
      </button>
      <button
        type="button"
        aria-pressed={settings.showZones}
        onClick={() => onPatch({ 'profiles.of.showZones': !settings.showZones })}
        className={toggleBtn(settings.showZones)}
      >
        Zones
      </button>

      <ToolbarDivider className="h-3.5" />

      {/* Pro GoCharting signals & display options */}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label="Order Flow signals"
            className={cn(TB_BTN, 'h-6 gap-1 px-1.5 text-[11px]', activeSignals > 0 && TB_ON)}
          >
            <span>Signals</span>
            {activeSignals > 0 && <span className={TB_COUNT}>{activeSignals}</span>}
            <MenuChevron />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56">
          <DropdownMenuLabel className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Order flow signals
          </DropdownMenuLabel>
          <DropdownMenuCheckboxItem
            checked={settings.absorptionMultiple > 0}
            onCheckedChange={(checked) =>
              onPatch({ 'profiles.of.absorptionMultiple': checked ? 2 : 0 })
            }
            className="text-xs"
          >
            Absorption bands (2× avg)
          </DropdownMenuCheckboxItem>
          <DropdownMenuCheckboxItem
            checked={settings.bigOrderMultiple > 0}
            onCheckedChange={(checked) =>
              onPatch({ 'profiles.of.bigOrderMultiple': checked ? 3 : 0 })
            }
            className="text-xs"
          >
            Big limit orders (3× avg)
          </DropdownMenuCheckboxItem>
          <DropdownMenuCheckboxItem
            checked={settings.multipleNodeBars > 0}
            onCheckedChange={(checked) =>
              onPatch({ 'profiles.of.multipleNodeBars': checked ? 2 : 0 })
            }
            className="text-xs"
          >
            Stacked POC nodes (2+ bars)
          </DropdownMenuCheckboxItem>
          <DropdownMenuCheckboxItem
            checked={settings.flipBars > 0}
            onCheckedChange={(checked) => onPatch({ 'profiles.of.flipBars': checked ? 2 : 0 })}
            className="text-xs"
          >
            Aggressive delta flips
          </DropdownMenuCheckboxItem>
          <DropdownMenuCheckboxItem
            checked={settings.showUnfinishedAuctions}
            onCheckedChange={(checked) =>
              onPatch({ 'profiles.of.showUnfinishedAuctions': Boolean(checked) })
            }
            className="text-xs"
          >
            Unfinished auctions
          </DropdownMenuCheckboxItem>
          <DropdownMenuCheckboxItem
            checked={settings.markDivergence}
            onCheckedChange={(checked) =>
              onPatch({
                'profiles.of.markDivergence': Boolean(checked),
                'profiles.of.showDeltaLabels': Boolean(checked),
              })
            }
            className="text-xs"
          >
            Delta divergence
          </DropdownMenuCheckboxItem>
          <DropdownMenuSeparator />
          <DropdownMenuLabel className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Display precision
          </DropdownMenuLabel>
          <DropdownMenuCheckboxItem
            checked={settings.intensityScope === 'visible'}
            onCheckedChange={(checked) =>
              onPatch({ 'profiles.of.intensityScope': checked ? 'visible' : 'bar' })
            }
            className="text-xs"
          >
            Visible-range heat scaling
          </DropdownMenuCheckboxItem>
          <DropdownMenuCheckboxItem
            checked={!settings.compactNumbers}
            onCheckedChange={(checked) =>
              onPatch({ 'profiles.of.compactNumbers': !checked })
            }
            className="text-xs"
          >
            Exact whole numbers
          </DropdownMenuCheckboxItem>
          <DropdownMenuCheckboxItem
            checked={settings.showVisibleProfile}
            onCheckedChange={(checked) =>
              onPatch({ 'profiles.of.showVisibleProfile': Boolean(checked) })
            }
            className="text-xs"
          >
            Visible volume profile
          </DropdownMenuCheckboxItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={onOpenSettings} className="text-xs">
            All Order Flow settings…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Spacer + Tape fidelity badge */}
      <div className="ml-auto flex shrink-0 items-center gap-1.5 pl-2">
        <span
          title={
            recordedBars > 0
              ? `${recordedBars} bar(s) built from ${tradeCount} real quote-classified prints; earlier bars use OHLCV estimates.`
              : 'Showing OHLCV estimates until live or recorded ticks arrive. Live broker quotes are recorded automatically.'
          }
          className={cn(
            'inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-medium tabular-nums',
            recordedBars > 0
              ? 'bg-emerald-500/15 text-emerald-400'
              : 'bg-amber-500/15 text-amber-400'
          )}
        >
          <span
            aria-hidden="true"
            className={cn(
              'h-1.5 w-1.5 rounded-full',
              recordedBars > 0 ? 'bg-emerald-400' : 'bg-amber-400'
            )}
          />
          {recordedBars > 0
            ? `Tape: ${recordedBars} bar${recordedBars === 1 ? '' : 's'} · ${fmtCount(tradeCount)}`
            : 'OHLCV Est.'}
        </span>

        <button
          type="button"
          onClick={onSync}
          disabled={syncing}
          title="Reload recorded order flow ticks from local database"
          aria-label="Sync recorded order flow tape"
          className={cn(TB_BTN, 'h-6 px-1.5 text-[11px]')}
        >
          <ChromeIcon id="refresh" size={12} className={cn(syncing && 'animate-spin')} />
          <span className="hidden xl:inline">{syncing ? 'Syncing' : 'Sync'}</span>
        </button>

        <button
          type="button"
          onClick={onOpenSettings}
          title="Order Flow settings"
          aria-label="Order Flow settings"
          className={cn(TB_BTN, 'h-6 w-6 px-0')}
        >
          <ChromeIcon id="settings" size={12} />
        </button>
      </div>
    </div>
  )
}

import { Copy, Download, RotateCcw, Upload } from 'lucide-react'
import { type ChangeEvent, useMemo, useRef, useState } from 'react'
import type { ScreenerMeta, SettingSpec, StrategyMeta } from '@/api/screener'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { showToast } from '@/utils/toast'

interface SettingsSheetProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  meta: ScreenerMeta
  strategy: StrategyMeta
  effectiveScan: Record<string, unknown>
  effectiveStrategy: Record<string, unknown>
  watchlistFileName: string
  onUpdateScan: (patch: Record<string, unknown>) => void
  onUploadWatchlist: (fileName: string, content: string) => void
  onUpdateStrategy: (key: string, value: unknown) => void
  onResetScan: () => void
  onResetStrategy: () => void
  onResetAll: () => void
  onImportJson: (raw: string) => { ok: boolean; error?: string }
  exportPayload: Record<string, unknown>
}

function SettingField({
  spec,
  value,
  onChange,
}: {
  spec: SettingSpec
  value: unknown
  onChange: (val: unknown) => void
}) {
  if (spec.kind === 'bool') {
    const checked = Boolean(value)
    return (
      <div className="flex items-start justify-between gap-4 rounded-lg border border-border/70 bg-muted/20 px-3 py-2.5">
        <div className="space-y-0.5">
          <Label className="text-sm font-medium leading-snug">{spec.label}</Label>
          {spec.tooltip && (
            <p className="text-xs leading-relaxed text-muted-foreground">{spec.tooltip}</p>
          )}
        </div>
        <Switch checked={checked} onCheckedChange={onChange} className="mt-0.5 shrink-0" />
      </div>
    )
  }

  if (spec.kind === 'choice') {
    const strVal = String(value ?? spec.choices[0]?.value ?? '')
    return (
      <div className="space-y-1.5">
        <Label className="text-xs font-medium text-foreground">{spec.label}</Label>
        <Select value={strVal} onValueChange={onChange}>
          <SelectTrigger className="h-9 text-sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {spec.choices.map((c) => (
              <SelectItem key={c.value} value={c.value}>
                {c.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {spec.tooltip && (
          <p className="text-[11px] leading-relaxed text-muted-foreground">{spec.tooltip}</p>
        )}
      </div>
    )
  }

  if (spec.kind === 'int' || spec.kind === 'float') {
    const numStr = value === undefined || value === null ? '' : String(value)
    return (
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <Label className="text-xs font-medium text-foreground">{spec.label}</Label>
          {spec.min !== null && spec.max !== null && (
            <span className="text-[10px] tabular-nums text-muted-foreground">
              {spec.min} – {spec.max}
            </span>
          )}
        </div>
        <Input
          type="number"
          step={spec.kind === 'int' ? 1 : 'any'}
          min={spec.min ?? undefined}
          max={spec.max ?? undefined}
          value={numStr}
          onChange={(e) => {
            const raw = e.target.value
            if (raw === '') {
              onChange(0)
              return
            }
            const parsed = spec.kind === 'int' ? parseInt(raw, 10) : parseFloat(raw)
            onChange(Number.isNaN(parsed) ? raw : parsed)
          }}
          className="h-9 text-sm tabular-nums"
        />
        {spec.tooltip && (
          <p className="text-[11px] leading-relaxed text-muted-foreground">{spec.tooltip}</p>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <Label className="text-xs font-medium text-foreground">{spec.label}</Label>
      <Input
        type="text"
        value={String(value ?? '')}
        onChange={(e) => onChange(e.target.value)}
        placeholder={spec.key === 'as_of_date' ? 'YYYY-MM-DD (blank = latest)' : ''}
        className="h-9 text-sm"
      />
      {spec.tooltip && (
        <p className="text-[11px] leading-relaxed text-muted-foreground">{spec.tooltip}</p>
      )}
    </div>
  )
}

export function SettingsSheet({
  open,
  onOpenChange,
  meta,
  strategy,
  effectiveScan,
  effectiveStrategy,
  watchlistFileName,
  onUpdateScan,
  onUploadWatchlist,
  onUpdateStrategy,
  onResetScan,
  onResetStrategy,
  onResetAll,
  onImportJson,
  exportPayload,
}: SettingsSheetProps) {
  const [jsonDraft, setJsonDraft] = useState('')
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const jsonFileRef = useRef<HTMLInputElement | null>(null)

  const scanByGroup = useMemo(() => {
    const scanGeneral: SettingSpec[] = []
    const filters: SettingSpec[] = []
    const mtfSpecs: SettingSpec[] = []
    const skip = new Set(['strategy', 'timeframe', 'custom_symbols', 'watchlist_symbols'])
    for (const sp of meta.scan_specs) {
      if (skip.has(sp.key)) continue
      if (sp.group.startsWith('Filters')) filters.push(sp)
      else if (sp.group.startsWith('Multi-timeframe')) mtfSpecs.push(sp)
      else scanGeneral.push(sp)
    }
    return { scanGeneral, filters, mtfSpecs }
  }, [meta.scan_specs])

  const strategyGroups = useMemo(() => {
    const map = new Map<string, SettingSpec[]>()
    for (const sp of strategy.settings_specs) {
      const g = sp.group || 'Rules'
      const list = map.get(g) ?? []
      list.push(sp)
      map.set(g, list)
    }
    return Array.from(map.entries())
  }, [strategy.settings_specs])

  const handleWatchlistFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : ''
      onUploadWatchlist(file.name, text)
      showToast.success(`Loaded watchlist "${file.name}"`)
    }
    reader.readAsText(file)
    e.target.value = ''
  }

  const handleExportJson = () => {
    const pretty = JSON.stringify(exportPayload, null, 2)
    const blob = new Blob([pretty], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'openalgo_smc_screener_config.json'
    a.click()
    URL.revokeObjectURL(url)
    showToast.success('Exported config JSON')
  }

  const handleCopyJson = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(exportPayload, null, 2))
      showToast.success('Config JSON copied to clipboard')
    } catch {
      showToast.error('Could not copy to clipboard')
    }
  }

  const handleImportText = () => {
    if (!jsonDraft.trim()) {
      showToast.warning('Paste a config JSON first')
      return
    }
    const res = onImportJson(jsonDraft)
    if (res.ok) {
      setJsonDraft('')
      showToast.success('Imported screener configuration')
    } else {
      showToast.error(res.error ?? 'Failed to import JSON')
    }
  }

  const handleJsonFileUpload = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : ''
      const res = onImportJson(text)
      if (res.ok) {
        showToast.success(`Imported config from "${file.name}"`)
      } else {
        showToast.error(res.error ?? 'Failed to import JSON file')
      }
    }
    reader.readAsText(file)
    e.target.value = ''
  }

  const ltfOn = Boolean(effectiveScan.ltf_on)
  const htfOn = Boolean(effectiveScan.htf_on)

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <div className="flex items-center justify-between pr-6">
            <div>
              <SheetTitle className="text-base">Screener Configuration</SheetTitle>
              <SheetDescription className="text-xs">
                Exact LuxAlgo SMC, filter, and multi-timeframe rules — persisted in your browser.
              </SheetDescription>
            </div>
            <Button variant="ghost" size="sm" onClick={onResetAll} className="h-8 gap-1.5 text-xs">
              <RotateCcw className="h-3.5 w-3.5" />
              Reset All
            </Button>
          </div>
        </SheetHeader>

        <Tabs defaultValue="rules" className="flex min-h-0 flex-1 flex-col">
          <div className="border-b border-border px-5 py-2.5">
            <TabsList className="grid w-full grid-cols-5">
              <TabsTrigger value="rules" className="text-xs">
                Rules
              </TabsTrigger>
              <TabsTrigger value="scan" className="text-xs">
                Scan
              </TabsTrigger>
              <TabsTrigger value="filters" className="text-xs">
                Filters
              </TabsTrigger>
              <TabsTrigger value="mtf" className="text-xs">
                MTF
              </TabsTrigger>
              <TabsTrigger value="json" className="text-xs">
                JSON
              </TabsTrigger>
            </TabsList>
          </div>

          <ScrollArea className="flex-1 px-5 py-4">
            {/* Strategy-specific rules */}
            <TabsContent value="rules" className="mt-0 space-y-5 pb-6">
              <div className="flex items-center justify-between rounded-lg border border-border bg-muted/30 px-3.5 py-2.5">
                <div className="min-w-0 pr-2">
                  <div className="text-sm font-semibold">{strategy.name}</div>
                  <p className="mt-0.5 text-xs text-muted-foreground">{strategy.description}</p>
                </div>
                <Button variant="outline" size="sm" onClick={onResetStrategy} className="h-7 shrink-0 text-xs">
                  Reset
                </Button>
              </div>

              {strategyGroups.map(([group, specs]) => (
                <div key={group} className="space-y-3">
                  <div className="flex items-center gap-2">
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      {group}
                    </h4>
                    <Separator className="flex-1" />
                  </div>
                  <div className="space-y-3">
                    {specs.map((sp) => (
                      <SettingField
                        key={sp.key}
                        spec={sp}
                        value={effectiveStrategy[sp.key]}
                        onChange={(val) => onUpdateStrategy(sp.key, val)}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </TabsContent>

            {/* Scan & Universe */}
            <TabsContent value="scan" className="mt-0 space-y-4 pb-6">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Universe & History Window
                </span>
                <Button variant="outline" size="sm" onClick={onResetScan} className="h-7 text-xs">
                  Reset Scan Settings
                </Button>
              </div>

              {scanByGroup.scanGeneral.map((sp) => (
                <SettingField
                  key={sp.key}
                  spec={sp}
                  value={effectiveScan[sp.key]}
                  onChange={(val) => onUpdateScan({ [sp.key]: val })}
                />
              ))}

              {effectiveScan.universe_mode === 'custom' && (
                <div className="space-y-1.5">
                  <Label className="text-xs font-medium">Custom Symbols</Label>
                  <Textarea
                    rows={4}
                    placeholder="RELIANCE, TCS, INFY, HDFCBANK, SBIN..."
                    value={String(effectiveScan.custom_symbols ?? '')}
                    onChange={(e) => onUpdateScan({ custom_symbols: e.target.value })}
                    className="font-mono text-xs"
                  />
                  <p className="text-[11px] text-muted-foreground">
                    NSE tickers separated by commas, spaces, or new lines (.NS suffix stripped automatically).
                  </p>
                </div>
              )}

              {effectiveScan.universe_mode === 'watchlist' && (
                <div className="space-y-2 rounded-lg border border-border bg-muted/20 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div>
                      <Label className="text-xs font-medium">Watchlist Upload (.txt / .csv)</Label>
                      <p className="text-[11px] text-muted-foreground">
                        {watchlistFileName
                          ? `Loaded: ${watchlistFileName}`
                          : 'First CSV column or one ticker per line (# comments ignored).'}
                      </p>
                    </div>
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".txt,.csv,text/plain,text/csv"
                      onChange={handleWatchlistFile}
                      className="hidden"
                    />
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => fileInputRef.current?.click()}
                      className="h-8 gap-1.5 text-xs"
                    >
                      <Upload className="h-3.5 w-3.5" />
                      Upload File
                    </Button>
                  </div>
                  <Textarea
                    rows={4}
                    placeholder="Or paste watchlist tickers here..."
                    value={String(effectiveScan.watchlist_symbols ?? '')}
                    onChange={(e) => onUpdateScan({ watchlist_symbols: e.target.value })}
                    className="font-mono text-xs"
                  />
                </div>
              )}
            </TabsContent>

            {/* Common Filters */}
            <TabsContent value="filters" className="mt-0 space-y-4 pb-6">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-muted-foreground">Quick presets:</span>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs"
                  onClick={() =>
                    onUpdateScan({
                      min_price: 50,
                      rsi_period: 14,
                      rsi_min: 0,
                      rsi_max: 65,
                      avg_period: 20,
                      min_avg_volume: 50000,
                      min_avg_value_cr: 0,
                    })
                  }
                >
                  Stock Screener Default
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs"
                  onClick={() =>
                    onUpdateScan({
                      min_price: 100,
                      rsi_min: 25,
                      rsi_max: 65,
                      min_avg_volume: 200000,
                      min_avg_value_cr: 10,
                    })
                  }
                >
                  High Liquidity (10 Cr+)
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs"
                  onClick={() =>
                    onUpdateScan({
                      min_price: 0,
                      rsi_min: 0,
                      rsi_max: 100,
                      min_avg_volume: 0,
                      min_avg_value_cr: 0,
                    })
                  }
                >
                  All Off
                </Button>
              </div>

              <div className="space-y-3">
                {scanByGroup.filters.map((sp) => (
                  <SettingField
                    key={sp.key}
                    spec={sp}
                    value={effectiveScan[sp.key]}
                    onChange={(val) => onUpdateScan({ [sp.key]: val })}
                  />
                ))}
              </div>
            </TabsContent>

            {/* Multi-timeframe checks */}
            <TabsContent value="mtf" className="mt-0 space-y-5 pb-6">
              <div className="rounded-lg border border-border bg-muted/20 p-3 text-xs text-muted-foreground">
                Multi-timeframe checks download LTF / HTF candles for <strong>matched rows only</strong> after
                the main scan completes, keeping scans fast while verifying institutional confluence.
              </div>

              {/* LTF section */}
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Lower-Timeframe Confirmation (LTF)
                  </h4>
                  {ltfOn && (
                    <Badge variant="secondary" className="bg-emerald-500/15 text-[10px] text-emerald-500">
                      Active
                    </Badge>
                  )}
                </div>
                {scanByGroup.mtfSpecs
                  .filter((s) => s.key.startsWith('ltf_'))
                  .map((sp) => (
                    <div key={sp.key} className={!ltfOn && sp.key !== 'ltf_on' ? 'opacity-50' : ''}>
                      <SettingField
                        spec={sp}
                        value={effectiveScan[sp.key]}
                        onChange={(val) => onUpdateScan({ [sp.key]: val })}
                      />
                    </div>
                  ))}
              </div>

              <Separator />

              {/* HTF section */}
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Other-Timeframe OB Confluence (HTF)
                  </h4>
                  {htfOn && (
                    <Badge variant="secondary" className="bg-sky-500/15 text-[10px] text-sky-500">
                      Active
                    </Badge>
                  )}
                </div>
                {scanByGroup.mtfSpecs
                  .filter((s) => s.key.startsWith('htf_'))
                  .map((sp) => (
                    <div key={sp.key} className={!htfOn && sp.key !== 'htf_on' ? 'opacity-50' : ''}>
                      <SettingField
                        spec={sp}
                        value={effectiveScan[sp.key]}
                        onChange={(val) => onUpdateScan({ [sp.key]: val })}
                      />
                    </div>
                  ))}
              </div>
            </TabsContent>

            {/* JSON Import / Export */}
            <TabsContent value="json" className="mt-0 space-y-4 pb-6">
              <div className="space-y-2 rounded-lg border border-border bg-muted/20 p-3.5">
                <div className="text-xs font-semibold">Compatible with stock_screener/config/config.json</div>
                <p className="text-xs text-muted-foreground">
                  Export your active settings or import an existing desktop <code>config.json</code> directly.
                </p>
                <div className="flex flex-wrap gap-2 pt-1">
                  <Button type="button" size="sm" variant="outline" onClick={handleExportJson} className="h-8 gap-1.5 text-xs">
                    <Download className="h-3.5 w-3.5" />
                    Download JSON
                  </Button>
                  <Button type="button" size="sm" variant="outline" onClick={handleCopyJson} className="h-8 gap-1.5 text-xs">
                    <Copy className="h-3.5 w-3.5" />
                    Copy JSON
                  </Button>
                  <input
                    ref={jsonFileRef}
                    type="file"
                    accept=".json,application/json"
                    onChange={handleJsonFileUpload}
                    className="hidden"
                  />
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => jsonFileRef.current?.click()}
                    className="h-8 gap-1.5 text-xs"
                  >
                    <Upload className="h-3.5 w-3.5" />
                    Import File
                  </Button>
                </div>
              </div>

              <div className="space-y-2">
                <Label className="text-xs font-medium">Paste config.json to import</Label>
                <Textarea
                  rows={8}
                  value={jsonDraft}
                  onChange={(e) => setJsonDraft(e.target.value)}
                  placeholder='{"scan": {"min_price": 50, "rsi_max": 65}, "strategies": {...}}'
                  className="font-mono text-xs"
                />
                <Button type="button" size="sm" onClick={handleImportText} className="h-8 text-xs">
                  Apply Pasted JSON
                </Button>
              </div>
            </TabsContent>
          </ScrollArea>
        </Tabs>
      </SheetContent>
    </Sheet>
  )
}

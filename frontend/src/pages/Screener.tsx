/**
 * SMC Stock Screener — LuxAlgo-compatible Smart Money Concepts scanner
 *
 * Ports all 8 screeners from stock_screener natively into OpenAlgo:
 *   1. SMC Breaker Block (+BB)
 *   2. SMC Order Block - price at OB
 *   3. Potential OB - before the break
 *   4. Bullish OB just below a bearish OB
 *   5. Last candle in bullish OB + SSL sweep
 *   6. OB just above another OB
 *   7. OB on chosen candle time(s) (intraday)
 *   8. Last candle in an OB inside a breaker
 *
 * Supports live Broker API (with native 5-stock INDstocks batching on INDMoney),
 * offline Historify DuckDB scans, LTF confirmation & HTF OB confluence, single-stock
 * SMC X-Ray Explain diagnostic, and 1-click deep-linking to /trading charts & Order Flow.
 */

import { useQuery } from '@tanstack/react-query'
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  CheckCircle2,
  Clock,
  Copy,
  Database,
  Download,
  ExternalLink,
  Filter,
  Layers,
  Loader2,
  Microscope,
  Play,
  Radar,
  Search,
  SlidersHorizontal,
  Square,
  Upload,
  Wifi,
} from 'lucide-react'
import { type ChangeEvent, useEffect, useMemo, useRef, useState } from 'react'
import {
  type ColumnSpec,
  extractScreenerError,
  getScreenerMeta,
  getScreenerStatus,
  type ScanRow,
  type ScanSnapshot,
  startScreenerScan,
  stopScreenerScan,
} from '@/api/screener'
import { ExplainDialog } from '@/components/screener/ExplainDialog'
import { SettingsSheet } from '@/components/screener/SettingsSheet'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { chartHref } from '@/lib/trading/chartLink'
import { useAuthStore } from '@/stores/authStore'
import { useScreenerStore } from '@/stores/screenerStore'
import { showToast } from '@/utils/toast'

type RowFilter = 'matches' | 'all' | 'rejected' | 'ltf' | 'htf'
type SortDir = 'asc' | 'desc'

const CSV_ESCAPE = (val: unknown): string => {
  const s = val === null || val === undefined ? '' : String(val)
  if (/[",\n\r]/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`
  }
  return s
}

function formatCellValue(col: ColumnSpec, raw: unknown): string {
  if (raw === null || raw === undefined || raw === '') return '—'
  if (col.kind === 'num') {
    const n = Number(raw)
    if (Number.isNaN(n)) return String(raw)
    return n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  }
  if (col.kind === 'int') {
    const n = Number(raw)
    if (Number.isNaN(n)) return String(raw)
    return Math.round(n).toLocaleString('en-IN')
  }
  if (col.kind === 'pct') {
    const n = Number(raw)
    if (Number.isNaN(n)) return String(raw)
    const sign = n > 0 ? '+' : ''
    return `${sign}${n.toFixed(2)}%`
  }
  return String(raw)
}

function renderCellContent(col: ColumnSpec, row: ScanRow) {
  const raw = row.values[col.key]
  const text = formatCellValue(col, raw)

  if (col.key === 'status') {
    return (
      <Badge
        variant="secondary"
        className={
          row.valid
            ? 'bg-emerald-500/15 font-medium text-emerald-600 dark:text-emerald-400'
            : 'bg-amber-500/15 font-medium text-amber-600 dark:text-amber-400'
        }
      >
        {text}
      </Badge>
    )
  }

  if (col.key === 'ltf_status' && text !== '—') {
    const confirmed = text === 'Confirmed'
    const waiting = text.startsWith('Waiting')
    return (
      <Badge
        variant="secondary"
        className={
          confirmed
            ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
            : waiting
              ? 'bg-sky-500/15 text-sky-600 dark:text-sky-400'
              : 'bg-muted text-muted-foreground'
        }
      >
        {text}
      </Badge>
    )
  }

  if (col.key === 'htf_ob' && text !== '—') {
    const isHit = !text.startsWith('none') && !text.startsWith('no ')
    return (
      <span
        className={
          isHit ? 'font-medium text-sky-600 dark:text-sky-400' : 'text-muted-foreground'
        }
      >
        {text}
      </span>
    )
  }

  if (col.key === 'reaction' && text !== '—') {
    return (
      <span className="font-medium text-emerald-600 dark:text-emerald-400">{text}</span>
    )
  }

  if (col.key === 'gap_pct' || col.key === 'dist_pct' || col.key === 'depth_pct') {
    const n = Number(raw)
    if (!Number.isNaN(n)) {
      const cls =
        n < 0
          ? 'text-rose-600 dark:text-rose-400'
          : n > 0
            ? 'text-emerald-600 dark:text-emerald-400'
            : 'text-muted-foreground'
      return <span className={`font-mono tabular-nums ${cls}`}>{text}</span>
    }
  }

  if (col.kind === 'num' || col.kind === 'int' || col.kind === 'pct') {
    return <span className="font-mono tabular-nums">{text}</span>
  }

  return <span>{text}</span>
}

export default function Screener() {
  const apiKey = useAuthStore((s) => s.apiKey)
  const broker = useAuthStore((s) => s.user?.broker ?? null)

  const {
    strategy,
    timeframe,
    scanOverrides,
    strategyOverrides,
    watchlistFileName,
    setStrategy,
    setTimeframe,
    updateScan,
    setWatchlist,
    updateStrategySetting,
    resetScan,
    resetStrategy,
    resetAll,
    importConfigJson,
  } = useScreenerStore()

  const {
    data: meta,
    isLoading: metaLoading,
    error: metaError,
  } = useQuery({
    queryKey: ['screener-meta'],
    queryFn: getScreenerMeta,
    staleTime: 5 * 60 * 1000,
  })

  const [snapshot, setSnapshot] = useState<ScanSnapshot | null>(null)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [explainOpen, setExplainOpen] = useState(false)
  const [explainSymbol, setExplainSymbol] = useState('')

  const [rowFilter, setRowFilter] = useState<RowFilter>('matches')
  const [searchText, setSearchText] = useState('')
  const [sortKey, setSortKey] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  const pollTimerRef = useRef<number | null>(null)
  const inlineWatchlistRef = useRef<HTMLInputElement | null>(null)

  const selectedStrategy = useMemo(() => {
    if (!meta) return null
    return meta.strategies.find((s) => s.id === strategy) ?? meta.strategies[0] ?? null
  }, [meta, strategy])

  const effectiveScan = useMemo<Record<string, unknown>>(() => {
    if (!meta) return { strategy, timeframe, ...scanOverrides }
    return {
      ...meta.scan_defaults,
      ...scanOverrides,
      strategy: selectedStrategy?.id ?? strategy,
      timeframe,
    }
  }, [meta, scanOverrides, selectedStrategy, strategy, timeframe])

  const effectiveStrategy = useMemo<Record<string, unknown>>(() => {
    if (!selectedStrategy) return {}
    return {
      ...selectedStrategy.defaults,
      ...(strategyOverrides[selectedStrategy.id] ?? {}),
    }
  }, [selectedStrategy, strategyOverrides])

  const activeOverridesCount = useMemo(() => {
    if (!meta || !selectedStrategy) return 0
    let count = 0
    for (const [k, v] of Object.entries(scanOverrides)) {
      if (meta.scan_defaults[k] !== v) count++
    }
    const stratOv = strategyOverrides[selectedStrategy.id] ?? {}
    for (const [k, v] of Object.entries(stratOv)) {
      if (selectedStrategy.defaults[k] !== v) count++
    }
    return count
  }, [meta, selectedStrategy, scanOverrides, strategyOverrides])

  const isRunning = snapshot?.state === 'running'

  // Cleanup polling interval on unmount
  useEffect(() => {
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current)
      }
    }
  }, [])

  // Poll job status while running
  useEffect(() => {
    if (!snapshot || snapshot.state !== 'running' || !apiKey) {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
      return
    }

    const jobId = snapshot.job_id
    let inFlight = false

    const poll = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const next = await getScreenerStatus(apiKey, jobId)
        setSnapshot(next)
        if (next.state !== 'running' && pollTimerRef.current !== null) {
          window.clearInterval(pollTimerRef.current)
          pollTimerRef.current = null
          if (next.state === 'completed') {
            showToast.success(
              `Scan complete: ${next.stats.matches} match(es) in ${next.stats.scanned} stocks`
            )
          } else if (next.state === 'failed' && next.fatal) {
            setRunError(next.fatal)
            showToast.error(next.fatal)
          } else if (next.state === 'cancelled') {
            showToast.info('Scan stopped by user')
          }
        }
      } catch (err) {
        const msg = extractScreenerError(err, 'Lost connection to screener job')
        setRunError(msg)
        if (pollTimerRef.current !== null) {
          window.clearInterval(pollTimerRef.current)
          pollTimerRef.current = null
        }
      } finally {
        inFlight = false
      }
    }

    pollTimerRef.current = window.setInterval(() => {
      void poll()
    }, 1000)

    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [snapshot?.job_id, snapshot?.state, apiKey])

  const handleStartScan = async () => {
    if (!selectedStrategy) return
    if (!apiKey) {
      const msg = 'No OpenAlgo API key found. Generate one on the API Key page first.'
      setRunError(msg)
      showToast.error(msg)
      return
    }

    setStarting(true)
    setRunError(null)
    try {
      const snap = await startScreenerScan({
        apikey: apiKey,
        strategy: selectedStrategy.id,
        timeframe,
        scan: effectiveScan,
        strategy_settings: effectiveStrategy,
      })
      setSnapshot(snap)
      setRowFilter(Boolean(effectiveStrategy.show_invalid) ? 'all' : 'matches')
      setSortKey(null)
    } catch (err) {
      const msg = extractScreenerError(err, 'Could not start SMC scan')
      setRunError(msg)
      showToast.error(msg)
    } finally {
      setStarting(false)
    }
  }

  const handleStopScan = async () => {
    if (!snapshot || !apiKey) return
    setStopping(true)
    try {
      const next = await stopScreenerScan(apiKey, snapshot.job_id)
      setSnapshot(next)
    } catch (err) {
      showToast.error(extractScreenerError(err, 'Failed to stop scan'))
    } finally {
      setStopping(false)
    }
  }

  const handleInlineWatchlistUpload = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : ''
      setWatchlist(file.name, text)
      showToast.success(`Loaded watchlist "${file.name}"`)
    }
    reader.readAsText(file)
    e.target.value = ''
  }

  const columns = snapshot?.columns ?? []

  const filteredRows = useMemo(() => {
    const rawRows = snapshot?.rows ?? []
    const q = searchText.trim().toLowerCase()

    const narrowed = rawRows.filter((r) => {
      if (rowFilter === 'matches' && !r.valid) return false
      if (rowFilter === 'rejected' && r.valid) return false
      if (rowFilter === 'ltf' && r.values.ltf_status !== 'Confirmed') return false
      if (rowFilter === 'htf') {
        const h = String(r.values.htf_ob ?? '')
        if (!h || h.startsWith('none') || h.startsWith('no ')) return false
      }
      if (!q) return true
      return Object.values(r.values).some((v) =>
        v !== null && v !== undefined && String(v).toLowerCase().includes(q)
      )
    })

    if (!sortKey) return narrowed
    const col = columns.find((c) => c.key === sortKey)
    const numeric = col && (col.kind === 'num' || col.kind === 'int' || col.kind === 'pct')
    const factor = sortDir === 'asc' ? 1 : -1

    return [...narrowed].sort((a, b) => {
      const av = a.values[sortKey]
      const bv = b.values[sortKey]
      const aEmpty = av === null || av === undefined || av === ''
      const bEmpty = bv === null || bv === undefined || bv === ''
      if (aEmpty && bEmpty) return 0
      if (aEmpty) return 1
      if (bEmpty) return -1
      if (numeric) {
        return (Number(av) - Number(bv)) * factor
      }
      return String(av).localeCompare(String(bv)) * factor
    })
  }, [snapshot?.rows, rowFilter, searchText, sortKey, sortDir, columns])

  const handleSort = (key: string) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  const handleCopyTickers = async () => {
    const syms = Array.from(
      new Set(
        filteredRows
          .map((r) => String(r.values.symbol ?? '').trim())
          .filter(Boolean)
      )
    )
    if (syms.length === 0) {
      showToast.warning('No symbols to copy')
      return
    }
    try {
      await navigator.clipboard.writeText(syms.join(', '))
      showToast.success(`Copied ${syms.length} ticker(s) to clipboard`)
    } catch {
      showToast.error('Could not copy tickers')
    }
  }

  const handleExportCsv = () => {
    if (!snapshot || filteredRows.length === 0) {
      showToast.warning('No rows to export')
      return
    }
    const headers = ['Valid', ...columns.map((c) => c.label)]
    const lines = [
      headers.map(CSV_ESCAPE).join(','),
      ...filteredRows.map((r) =>
        [r.valid ? 'MATCH' : 'REJECTED', ...columns.map((c) => r.values[c.key])].map(CSV_ESCAPE).join(',')
      ),
    ]
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `openalgo_${snapshot.strategy}_${snapshot.timeframe}_${new Date()
      .toISOString()
      .slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
    showToast.success(`Exported ${filteredRows.length} row(s) to CSV`)
  }

  const exportConfigPayload = useMemo(() => {
    if (!meta) return {}
    const allStrats: Record<string, Record<string, unknown>> = {}
    for (const s of meta.strategies) {
      allStrats[s.id] = {
        ...s.defaults,
        ...(strategyOverrides[s.id] ?? {}),
      }
    }
    return {
      scan: effectiveScan,
      strategies: allStrats,
    }
  }, [meta, effectiveScan, strategyOverrides])

  const progressPct = useMemo(() => {
    if (!snapshot) return 0
    if (snapshot.state === 'completed') return 100
    const { done, total } = snapshot.progress
    if (!total || total <= 0) return snapshot.state === 'running' ? 8 : 0
    return Math.min(99, Math.round((done / total) * 100))
  }, [snapshot])

  const activeConfigPills = useMemo(() => {
    const pills: string[] = []
    if (effectiveScan.as_of_date) pills.push(`As of ${String(effectiveScan.as_of_date)}`)
    pills.push(`Lookback ${String(effectiveScan.lookback_candles ?? 250)}`)
    pills.push(`Forming: ${String(effectiveScan.forming_candle ?? 'final')}`)
    if (Number(effectiveScan.min_price ?? 0) > 0) pills.push(`Price >= ₹${String(effectiveScan.min_price)}`)
    const rMin = Number(effectiveScan.rsi_min ?? 0)
    const rMax = Number(effectiveScan.rsi_max ?? 100)
    if (rMin > 0 || rMax < 100) pills.push(`RSI ${rMin}–${rMax}`)
    if (Number(effectiveScan.min_avg_volume ?? 0) > 0) {
      pills.push(`Avg Vol >= ${Number(effectiveScan.min_avg_volume).toLocaleString('en-IN')}`)
    }
    if (Number(effectiveScan.min_avg_value_cr ?? 0) > 0) {
      pills.push(`Value >= ${String(effectiveScan.min_avg_value_cr)} Cr`)
    }
    if (effectiveScan.ltf_on) {
      pills.push(
        `LTF ${String(effectiveScan.ltf_timeframe ?? 'auto')}${effectiveScan.ltf_keep_only ? ' (strict)' : ''}`
      )
    }
    if (effectiveScan.htf_on) {
      pills.push(
        `HTF ${String(effectiveScan.htf_timeframe ?? 'auto')}${effectiveScan.htf_keep_only ? ' (strict)' : ''}`
      )
    }
    if (effectiveStrategy.show_invalid) pills.push('Showing rejected')
    return pills
  }, [effectiveScan, effectiveStrategy])

  if (metaLoading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center gap-2.5 text-sm text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin text-primary" />
        <span>Loading SMC Stock Screener engine...</span>
      </div>
    )
  }

  if (metaError || !meta || !selectedStrategy) {
    return (
      <div className="mx-auto max-w-2xl p-6">
        <Card className="border-rose-500/30">
          <CardContent className="flex items-start gap-3 p-6 text-sm">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-rose-500" />
            <div>
              <div className="font-semibold text-foreground">Could not load SMC Screener metadata</div>
              <p className="mt-1 text-xs text-muted-foreground">
                {extractScreenerError(metaError, 'Check that OpenAlgo backend is running and /api/v1/screener/meta is reachable.')}
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    )
  }

  const allowedTfs = new Set(selectedStrategy.timeframes)
  const isIndMoneyFastPath =
    effectiveScan.source === 'api' && (broker ?? '').toLowerCase() === 'indmoney'

  return (
    <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 p-4 sm:p-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Radar className="h-5 w-5" />
            </div>
            <h1 className="text-xl font-bold tracking-tight sm:text-2xl">SMC Stock Screener</h1>
            <Badge variant="secondary" className="text-[11px] font-medium">
              8 LuxAlgo Screeners
            </Badge>
            {isIndMoneyFastPath && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Badge
                    variant="outline"
                    className="gap-1 border-emerald-500/40 bg-emerald-500/10 text-[11px] text-emerald-600 dark:text-emerald-400"
                  >
                    <Wifi className="h-3 w-3" />
                    INDMoney 5x Batch Mode
                  </Badge>
                </TooltipTrigger>
                <TooltipContent className="max-w-xs text-xs">
                  Using INDstocks native 5-stock batch historical API + daily NSE EQ series master
                  for 100% parity with stock_screener at 5x throughput.
                </TooltipContent>
              </Tooltip>
            )}
          </div>
          <p className="mt-1 text-xs text-muted-foreground sm:text-sm">
            Smart Money Concepts scanner for Order Blocks, Bullish Breakers (+BB), Potential OBs,
            SSL Liquidity Sweeps, and Multi-Timeframe confluence.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              setExplainSymbol('')
              setExplainOpen(true)
            }}
            className="h-9 gap-1.5"
          >
            <Microscope className="h-4 w-4" />
            Explain Symbol
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setSettingsOpen(true)}
            className="h-9 gap-1.5"
          >
            <SlidersHorizontal className="h-4 w-4" />
            Settings
            {activeOverridesCount > 0 && (
              <Badge variant="secondary" className="ml-0.5 h-5 px-1.5 text-[10px]">
                {activeOverridesCount}
              </Badge>
            )}
          </Button>
          {isRunning ? (
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={handleStopScan}
              disabled={stopping}
              className="h-9 gap-1.5 px-4"
            >
              {stopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
              Stop Scan
            </Button>
          ) : (
            <Button
              type="button"
              size="sm"
              onClick={handleStartScan}
              disabled={starting}
              className="h-9 gap-1.5 px-4"
            >
              {starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              Run Screener
            </Button>
          )}
        </div>
      </div>

      {/* Command Bar */}
      <Card>
        <CardContent className="space-y-3.5 p-4">
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
            {/* Screener Picker */}
            <div className="space-y-1.5 lg:col-span-4">
              <label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                Screener Strategy
              </label>
              <Select
                value={selectedStrategy.id}
                onValueChange={(id) => {
                  const found = meta.strategies.find((s) => s.id === id)
                  setStrategy(id, found?.timeframes)
                }}
                disabled={isRunning}
              >
                <SelectTrigger className="h-9 text-sm font-medium">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {meta.strategies.map((s) => (
                    <SelectItem key={s.id} value={s.id}>
                      {s.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Timeframe Pills */}
            <div className="space-y-1.5 lg:col-span-4">
              <label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                Timeframe
              </label>
              <div className="flex flex-wrap gap-1">
                {meta.timeframes.map((tf) => {
                  const allowed = allowedTfs.has(tf.label)
                  const active = timeframe === tf.label
                  return (
                    <Button
                      key={tf.label}
                      type="button"
                      size="sm"
                      variant={active ? 'default' : 'outline'}
                      disabled={!allowed || isRunning}
                      onClick={() => setTimeframe(tf.label)}
                      title={
                        allowed
                          ? `Scan on ${tf.label} candles`
                          : `${selectedStrategy.name} supports intraday timeframes only`
                      }
                      className="h-9 min-w-[44px] px-2.5 font-mono text-xs"
                    >
                      {tf.label}
                    </Button>
                  )
                })}
              </div>
            </div>

            {/* Universe & Source */}
            <div className="grid grid-cols-2 gap-2 lg:col-span-4">
              <div className="space-y-1.5">
                <label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Universe
                </label>
                <Select
                  value={String(effectiveScan.universe_mode ?? 'all_nse_eq')}
                  onValueChange={(v) => updateScan({ universe_mode: v })}
                  disabled={isRunning}
                >
                  <SelectTrigger className="h-9 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {meta.universe_modes.map((u) => (
                      <SelectItem key={u.value} value={u.value}>
                        {u.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <label className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Data Source
                </label>
                <Select
                  value={String(effectiveScan.source ?? 'api')}
                  onValueChange={(v) => updateScan({ source: v })}
                  disabled={isRunning}
                >
                  <SelectTrigger className="h-9 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {meta.data_sources.map((src) => (
                      <SelectItem key={src.value} value={src.value}>
                        <span className="flex items-center gap-1.5">
                          {src.value === 'db' ? (
                            <Database className="h-3.5 w-3.5 text-sky-500" />
                          ) : (
                            <Wifi className="h-3.5 w-3.5 text-emerald-500" />
                          )}
                          {src.label}
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
          </div>

          {/* Contextual inline inputs: Custom symbols, Watchlist upload, or ob_at_open candle_time */}
          {(effectiveScan.universe_mode === 'custom' ||
            effectiveScan.universe_mode === 'watchlist' ||
            selectedStrategy.ui_keys.includes('candle_time')) && (
            <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border/70 bg-muted/25 px-3 py-2.5">
              {selectedStrategy.ui_keys.includes('candle_time') && (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="flex items-center gap-1 text-xs font-medium text-foreground">
                    <Clock className="h-3.5 w-3.5 text-primary" />
                    OB Candle Time(s) IST:
                  </span>
                  <Input
                    value={String(effectiveStrategy.candle_time ?? '09:15')}
                    onChange={(e) =>
                      updateStrategySetting(selectedStrategy.id, 'candle_time', e.target.value)
                    }
                    disabled={isRunning}
                    placeholder="09:15, 13:15, 15:15"
                    className="h-8 w-52 font-mono text-xs"
                  />
                </div>
              )}

              {effectiveScan.universe_mode === 'custom' && (
                <div className="flex min-w-[260px] flex-1 items-center gap-2">
                  <span className="shrink-0 text-xs font-medium text-foreground">Tickers:</span>
                  <Input
                    value={String(effectiveScan.custom_symbols ?? '')}
                    onChange={(e) => updateScan({ custom_symbols: e.target.value })}
                    disabled={isRunning}
                    placeholder="RELIANCE, TCS, INFY, HDFCBANK, HBLENGINE..."
                    className="h-8 font-mono text-xs"
                  />
                </div>
              )}

              {effectiveScan.universe_mode === 'watchlist' && (
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    ref={inlineWatchlistRef}
                    type="file"
                    accept=".txt,.csv,text/plain,text/csv"
                    onChange={handleInlineWatchlistUpload}
                    className="hidden"
                  />
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={isRunning}
                    onClick={() => inlineWatchlistRef.current?.click()}
                    className="h-8 gap-1.5 text-xs"
                  >
                    <Upload className="h-3.5 w-3.5" />
                    {watchlistFileName ? `Change (${watchlistFileName})` : 'Upload .txt / .csv Watchlist'}
                  </Button>
                  {Boolean(effectiveScan.watchlist_symbols) && (
                    <span className="text-xs text-muted-foreground">
                      {
                        String(effectiveScan.watchlist_symbols)
                          .split(/[\s,;]+/)
                          .filter(Boolean).length
                      }{' '}
                      lines loaded
                    </span>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Strategy Description & Active Config Pills */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/60 pt-2.5">
            <p className="text-xs text-muted-foreground">{selectedStrategy.description}</p>
            <div className="flex flex-wrap items-center gap-1.5">
              {activeConfigPills.map((pill) => (
                <Badge
                  key={pill}
                  variant="secondary"
                  className="bg-muted/70 px-2 py-0.5 text-[11px] font-normal text-muted-foreground"
                >
                  {pill}
                </Badge>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Error banner */}
      {runError && (
        <div className="flex items-start gap-2.5 rounded-xl border border-rose-500/35 bg-rose-500/10 px-4 py-3 text-xs text-rose-600 dark:text-rose-400">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="space-y-0.5">
            <div className="font-semibold">Screener Alert</div>
            <div>{runError}</div>
          </div>
        </div>
      )}

      {/* Live Progress + KPI Strip */}
      {snapshot && (
        <div className="space-y-3">
          <Card>
            <CardContent className="space-y-2.5 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <div className="flex items-center gap-2">
                  {snapshot.state === 'running' ? (
                    <Loader2 className="h-4 w-4 animate-spin text-primary" />
                  ) : snapshot.state === 'completed' ? (
                    <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                  ) : (
                    <AlertCircle className="h-4 w-4 text-amber-500" />
                  )}
                  <span className="font-medium text-foreground">
                    {snapshot.state === 'running' ? snapshot.progress.message : snapshot.summary}
                  </span>
                  <Badge variant="outline" className="text-[10px] uppercase">
                    {snapshot.progress.phase}
                  </Badge>
                </div>
                <span className="font-mono text-muted-foreground tabular-nums">
                  {snapshot.progress.done}/{snapshot.progress.total || '?'} · {snapshot.stats.elapsed_sec}s
                </span>
              </div>
              <Progress value={progressPct} className="h-2" />
            </CardContent>
          </Card>

          <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4 lg:grid-cols-7">
            <Card>
              <CardContent className="p-3">
                <div className="text-[11px] text-muted-foreground">Matches</div>
                <div className="mt-0.5 font-mono text-xl font-bold tabular-nums text-emerald-600 dark:text-emerald-400">
                  {snapshot.stats.matches}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-3">
                <div className="text-[11px] text-muted-foreground">Scanned</div>
                <div className="mt-0.5 font-mono text-xl font-bold tabular-nums">
                  {snapshot.stats.scanned}
                  <span className="text-xs font-normal text-muted-foreground">
                    /{snapshot.stats.universe}
                  </span>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-3">
                <div className="text-[11px] text-muted-foreground">Filtered Out</div>
                <div className="mt-0.5 font-mono text-xl font-bold tabular-nums text-amber-600 dark:text-amber-400">
                  {snapshot.stats.filtered}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-3">
                <div className="text-[11px] text-muted-foreground">LTF Confirmed</div>
                <div className="mt-0.5 font-mono text-xl font-bold tabular-nums text-sky-600 dark:text-sky-400">
                  {snapshot.stats.ltf_checked > 0
                    ? `${snapshot.stats.ltf_confirmed}/${snapshot.stats.ltf_checked}`
                    : '—'}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-3">
                <div className="text-[11px] text-muted-foreground">HTF Confluence</div>
                <div className="mt-0.5 font-mono text-xl font-bold tabular-nums text-violet-600 dark:text-violet-400">
                  {snapshot.stats.htf_checked > 0
                    ? `${snapshot.stats.htf_hits}/${snapshot.stats.htf_checked}`
                    : '—'}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-3">
                <div className="text-[11px] text-muted-foreground">Short / Errors</div>
                <div className="mt-0.5 font-mono text-xl font-bold tabular-nums text-muted-foreground">
                  {snapshot.stats.short} / {snapshot.stats.errors}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-3">
                <div className="text-[11px] text-muted-foreground">API Calls</div>
                <div className="mt-0.5 font-mono text-xl font-bold tabular-nums">
                  {snapshot.stats.calls}
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {/* Results Table Card */}
      <Card className="overflow-hidden">
        {/* Table Toolbar */}
        <div className="flex flex-wrap items-center justify-between gap-2.5 border-b border-border px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative w-56">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                placeholder="Filter symbol or status..."
                className="h-8 pl-8 text-xs"
              />
            </div>

            <div className="flex flex-wrap items-center gap-1">
              <Button
                type="button"
                size="sm"
                variant={rowFilter === 'matches' ? 'default' : 'outline'}
                onClick={() => setRowFilter('matches')}
                className="h-8 text-xs"
              >
                Matches ({snapshot?.stats.matches ?? 0})
              </Button>
              <Button
                type="button"
                size="sm"
                variant={rowFilter === 'all' ? 'default' : 'outline'}
                onClick={() => setRowFilter('all')}
                className="h-8 text-xs"
              >
                All ({snapshot?.rows.length ?? 0})
              </Button>
              {(snapshot?.stats.rejected ?? 0) > 0 && (
                <Button
                  type="button"
                  size="sm"
                  variant={rowFilter === 'rejected' ? 'default' : 'outline'}
                  onClick={() => setRowFilter('rejected')}
                  className="h-8 text-xs"
                >
                  Rejected ({snapshot?.stats.rejected ?? 0})
                </Button>
              )}
              {(snapshot?.stats.ltf_checked ?? 0) > 0 && (
                <Button
                  type="button"
                  size="sm"
                  variant={rowFilter === 'ltf' ? 'default' : 'outline'}
                  onClick={() => setRowFilter('ltf')}
                  className="h-8 text-xs"
                >
                  LTF Confirmed ({snapshot?.stats.ltf_confirmed ?? 0})
                </Button>
              )}
              {(snapshot?.stats.htf_checked ?? 0) > 0 && (
                <Button
                  type="button"
                  size="sm"
                  variant={rowFilter === 'htf' ? 'default' : 'outline'}
                  onClick={() => setRowFilter('htf')}
                  className="h-8 text-xs"
                >
                  HTF Hit ({snapshot?.stats.htf_hits ?? 0})
                </Button>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleCopyTickers}
              disabled={filteredRows.length === 0}
              className="h-8 gap-1.5 text-xs"
            >
              <Copy className="h-3.5 w-3.5" />
              Copy Tickers
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleExportCsv}
              disabled={filteredRows.length === 0}
              className="h-8 gap-1.5 text-xs"
            >
              <Download className="h-3.5 w-3.5" />
              Export CSV
            </Button>
          </div>
        </div>

        {/* Table Body */}
        {!snapshot ? (
          <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
              <Layers className="h-6 w-6" />
            </div>
            <div className="max-w-md space-y-1">
              <h3 className="text-sm font-semibold text-foreground">Ready to scan NSE stocks</h3>
              <p className="text-xs leading-relaxed text-muted-foreground">
                Pick any of the 8 SMC screeners and timeframe above, tune filters or multi-timeframe
                confirmation in <strong>Settings</strong>, and click <strong>Run Screener</strong>.
                Use <strong>NIFTY 50</strong> for a 5-second test scan or <strong>All NSE EQ</strong>{' '}
                for the full market.
              </p>
            </div>
          </div>
        ) : filteredRows.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center text-xs text-muted-foreground">
            <Filter className="h-6 w-6 opacity-40" />
            <p>
              {isRunning
                ? 'Scanning stocks — matches will stream in automatically as batches finish...'
                : 'No rows match the current filter.'}
            </p>
          </div>
        ) : (
          <div className="max-h-[62vh] overflow-auto">
            <table className="w-full border-collapse text-left text-xs">
              <thead className="sticky top-0 z-20 border-b border-border bg-muted/90 backdrop-blur">
                <tr>
                  <th className="sticky left-0 z-30 border-r border-border/60 bg-muted/95 px-3 py-2.5 font-semibold text-foreground">
                    Actions
                  </th>
                  {columns.map((col) => {
                    const isSorted = sortKey === col.key
                    const rightAlign = col.kind === 'num' || col.kind === 'int' || col.kind === 'pct'
                    return (
                      <th
                        key={col.key}
                        onClick={() => handleSort(col.key)}
                        title={col.tooltip || col.label}
                        className={`cursor-pointer select-none whitespace-nowrap px-3 py-2.5 font-semibold text-muted-foreground transition-colors hover:text-foreground ${
                          rightAlign ? 'text-right' : 'text-left'
                        }`}
                      >
                        <span className="inline-flex items-center gap-1">
                          {col.label}
                          {isSorted ? (
                            sortDir === 'asc' ? (
                              <ArrowUp className="h-3 w-3 text-primary" />
                            ) : (
                              <ArrowDown className="h-3 w-3 text-primary" />
                            )
                          ) : (
                            <ArrowUpDown className="h-3 w-3 opacity-35" />
                          )}
                        </span>
                      </th>
                    )
                  })}
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {filteredRows.map((row, idx) => {
                  const sym = String(row.values.symbol ?? '')
                  return (
                    <tr
                      key={`${sym}-${idx}`}
                      className={`transition-colors hover:bg-muted/35 ${
                        row.valid ? '' : 'opacity-65'
                      }`}
                    >
                      <td className="sticky left-0 z-10 whitespace-nowrap border-r border-border/60 bg-card px-2.5 py-2">
                        <div className="flex items-center gap-1">
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                className="h-7 w-7"
                                onClick={() => {
                                  setExplainSymbol(sym)
                                  setExplainOpen(true)
                                }}
                                aria-label={`Explain ${sym}`}
                              >
                                <Microscope className="h-3.5 w-3.5 text-primary" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent className="text-xs">
                              Explain {sym} (SMC X-Ray)
                            </TooltipContent>
                          </Tooltip>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                asChild
                                variant="ghost"
                                size="icon"
                                className="h-7 w-7"
                                aria-label={`Open ${sym} chart`}
                              >
                                <a
                                  href={chartHref(sym, 'NSE')}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                >
                                  <ExternalLink className="h-3.5 w-3.5 text-emerald-500" />
                                </a>
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent className="text-xs">
                              Open {sym} in Chart &amp; Order Flow
                            </TooltipContent>
                          </Tooltip>
                        </div>
                      </td>
                      {columns.map((col) => {
                        const rightAlign =
                          col.kind === 'num' || col.kind === 'int' || col.kind === 'pct'
                        return (
                          <td
                            key={col.key}
                            className={`whitespace-nowrap px-3 py-2 ${
                              rightAlign ? 'text-right' : 'text-left'
                            } ${col.key === 'symbol' ? 'font-mono font-bold text-foreground' : ''}`}
                          >
                            {col.key === 'symbol' ? (
                              <a
                                href={chartHref(sym, 'NSE')}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="hover:text-primary hover:underline"
                              >
                                {sym}
                              </a>
                            ) : (
                              renderCellContent(col, row)
                            )}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Missing / Error footnote when present */}
        {snapshot && (snapshot.missing_symbols.length > 0 || Object.keys(snapshot.error_samples).length > 0) && (
          <div className="space-y-1 border-t border-border bg-muted/20 px-4 py-2.5 text-[11px] text-muted-foreground">
            {snapshot.missing_symbols.length > 0 && (
              <div>
                <strong>Unknown symbols ({snapshot.stats.missing}):</strong>{' '}
                {snapshot.missing_symbols.join(', ')}
              </div>
            )}
            {Object.keys(snapshot.error_samples).length > 0 && (
              <div>
                <strong>Sample errors ({snapshot.stats.errors}):</strong>{' '}
                {Object.entries(snapshot.error_samples)
                  .slice(0, 5)
                  .map(([s, e]) => `${s} (${e})`)
                  .join(' | ')}
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Settings Slide-over Sheet */}
      <SettingsSheet
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        meta={meta}
        strategy={selectedStrategy}
        effectiveScan={effectiveScan}
        effectiveStrategy={effectiveStrategy}
        watchlistFileName={watchlistFileName}
        onUpdateScan={updateScan}
        onUploadWatchlist={setWatchlist}
        onUpdateStrategy={(key, val) => updateStrategySetting(selectedStrategy.id, key, val)}
        onResetScan={resetScan}
        onResetStrategy={() => resetStrategy(selectedStrategy.id)}
        onResetAll={resetAll}
        onImportJson={importConfigJson}
        exportPayload={exportConfigPayload}
      />

      {/* SMC Explain X-Ray Modal */}
      <ExplainDialog
        open={explainOpen}
        onOpenChange={setExplainOpen}
        initialSymbol={explainSymbol}
        apiKey={apiKey}
        strategy={selectedStrategy.id}
        timeframe={timeframe}
        effectiveScan={effectiveScan}
        effectiveStrategy={effectiveStrategy}
      />
    </div>
  )
}

import React from 'react'

/**
 * Simple horizontal progress bar.
 * value: current value
 * max: maximum value (default 1)
 * thresholds: [greenBelow, orangeBelow] — above orangeBelow is red
 */
export default function HealthBar({ value, max = 1, thresholds = [0.4, 0.7] }) {
  const pct = Math.min(100, Math.round((value / max) * 100))
  const ratio = value / max
  const color =
    ratio < thresholds[0]
      ? 'bg-green-500'
      : ratio < thresholds[1]
      ? 'bg-yellow-500'
      : 'bg-red-500'

  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 bg-gray-800 rounded-full h-1.5 min-w-12">
        <div className={`${color} h-1.5 rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-400 w-10 text-right tabular-nums">
        {typeof value === 'number' ? value.toFixed(3) : value}
      </span>
    </div>
  )
}

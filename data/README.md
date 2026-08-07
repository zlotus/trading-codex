# Local Market Data

Market payloads stay outside Git. The planned local layout is:

```text
data/
  raw/           Immutable provider responses with source and receive times
  normalized/    Point-in-time bars, instruments, calendars, and actions
  features/      Versioned feature sets derived from normalized data
```

Execution prices always use unadjusted market data. Adjusted series are derived
for research and signals and must retain a link to their adjustment inputs.

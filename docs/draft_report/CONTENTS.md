# Drive JAMA-style run report — draft overview

Open [`drive_report_DRAFT.html`](./drive_report_DRAFT.html) in a browser (light / white theme).

## Look

- White/light background, card layout, blue metric tiles for currents
- Summary: Tests / Passed / Failed / Pass rate / Overall
- Per-case: metrics (when present) + SIGNAL / EXPECTED / OBSERVED / SOURCES
- Notes / log evidence only when relevant (not on Capture)

## Case content

| Section | What’s shown |
|---------|----------------|
| Capture | Started / data available — **no** Notes |
| Commission | Rating + tab positions; discovery success + gateway evidence |
| SBX | EXPECTED = concrete msg IDs; OBSERVED = what arrived (seen/missing) + Modbus lines |
| Throttle / Recover | SBX request A · panel total (gateway feeder) · LB2 load A + Modbus evidence |

## IDs

`DRIVE-CAP-*`, `DRIVE-COM-*`, `DRIVE-SBX-*`, `DRIVE-THR-*`, `DRIVE-REC-*`

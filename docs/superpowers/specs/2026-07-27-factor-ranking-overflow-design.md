# Factor Ranking Overflow Design

## Goal

Prevent long scheme names and remarks from escaping their ranking-table cells or
covering accuracy metrics at customer-facing viewport widths.

## Design

- Give all eight ranking columns explicit widths under the existing fixed table
  layout and raise the table minimum width to 1340px so those widths remain
  usable.
- Render scheme names and remarks in dedicated elements.
- Clamp both scheme name and remark to two lines.
- Permit long underscore-delimited names to break within the scheme cell.
- Preserve the complete value in a native `title` tooltip.
- Keep the existing horizontal-scroll wrapper for narrow screens.

No backend payload, calculation, sorting, candidate count, or database behavior
changes.

## Acceptance

- `MACRO_DIFFUSION_FUNDSEASON_TRENDKERNEL` never overlaps the accuracy column.
- A long Chinese remark stays inside the remark cell and does not create an
  unbounded row.
- Full text remains available on hover.
- Existing selection, sorting, accessibility names, and responsive horizontal
  scrolling remain functional.

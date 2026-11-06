---
version: alpha
name: LinkedIn
description: "Light interface measured from in.linkedin.com: system-ui type, blue accents, tonal surfaces and 24px corners."
colors:
  background: "#FFFFFF"
  surface: "#FFFFFF"
  on-surface: "#191919"
  on-background: "#191919"
  on-surface-variant: "#404040"
  on-surface-muted: "#666666"
  surface-container-low: "#F4F2EE"
  primary: "#0A66C2"
  on-primary: "#FFFFFF"
  secondary: "#B24020"
typography:
  headline-display:
    fontFamily: system-ui
    fontSize: 48px
    fontWeight: 400
    lineHeight: 1.13
  headline-lg:
    fontFamily: system-ui
    fontSize: 30px
    fontWeight: 400
    lineHeight: 1.13
  body-md:
    fontFamily: system-ui
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.25
  body-lg:
    fontFamily: system-ui
    fontSize: 18px
    fontWeight: 400
    lineHeight: 1.5
  body-sm:
    fontFamily: system-ui
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.25
  label-md:
    fontFamily: system-ui
    fontSize: 16px
    fontWeight: 600
    lineHeight: 1.25
  headline-display-mobile:
    fontFamily: system-ui
    fontSize: 30px
    fontWeight: 400
    lineHeight: 1.13
rounded:
  md: 24px
spacing:
  base: 4px
  xs: 4px
  sm: 8px
  md: 12px
  lg: 20px
  gutter: 32px
components:
  button-ghost:
    textColor: "{colors.on-surface}"
    typography: "{typography.body-md}"
    rounded: 0px
    padding: 0px
  link:
    textColor: "{colors.on-surface-muted}"
---

# LinkedIn Design System

## Overview

LinkedIn describes itself as: "1 billion members | Manage your professional identity. Build and engage with your professional network. Access knowledge, insights and opportunities." The interface is light, built on **White (#FFFFFF)** with **Charcoal (#191919)** text, and **Blue (#0A66C2)** as the main accent.

Type is set in **system-ui**. Headlines run large; body text is 14px. Depth comes from hairline borders and inset highlights rather than drop shadows. Corners are mostly 24px.

Measured from [in.linkedin.com](https://in.linkedin.com/) on November 2, 2026 at 1440×900 and 390×844 from 282 rendered elements. Values are computed styles from the live page, not the original source files.

## Colors

The palette has 8 roles.

- **White (#FFFFFF):** Page background. Token `background`.
- **Charcoal (#191919):** Primary text and headings. Token `on-surface`.
- **Graphite (#404040):** Secondary text, seen 33 times. Token `on-surface-variant`.
- **Slate (#666666):** Muted text and metadata, seen 6 times. Token `on-surface-muted`.
- **Warm Mist (#F4F2EE):** Raised areas one step from the page, seen 1 time. Token `surface-container-low`.
- **Blue (#0A66C2):** Brand color in the logo, seen 9 times. Token `primary`, CSS `--b6ba4i`.
- **White (#FFFFFF):** Text and icons on primary. Token `on-primary`.
- **Red (#B24020):** Accent in text, fills and borders, seen 2 times. Token `secondary`.

## Typography

**system-ui** carries the whole interface. Fallback stack: `system-ui, -apple-system, BlinkMacSystemFont, Segoe UI`.

- **headline-display:** system-ui 48px at weight 400, line height 1.13 (30px on mobile). Page title (h1).
- **headline-lg:** system-ui 30px at weight 400, line height 1.13. Section headings (h2).
- **body-md:** system-ui 14px at weight 400, line height 1.25. Body text.
- **body-lg:** system-ui 18px at weight 400, line height 1.5. Lead paragraphs.
- **body-sm:** system-ui 12px at weight 400, line height 1.25. Secondary and small text.
- **label-md:** system-ui 16px at weight 600, line height 1.25. Buttons, navigation and labels.

## Layout

Spacing follows a 4px grid: 100% of measured gaps and paddings are multiples of 4px.

- **Scale:** xs 4px, sm 8px, md 12px, lg 20px.
- **Gaps:** 32px is the most common gap in flex and grid layouts.

## Elevation & Depth

The design avoids drop shadows. Layers are separated by tonal surface steps (#F4F2EE) and hairline rings:

- `rgba(0, 0, 0, 0.75) 0px 0px 0px 1px inset`
- `rgb(10, 102, 194) 0px 0px 0px 1px inset`

## Shapes

Most elements use 24px corners. Measured radii, most used first:

- **md (24px):** 148 elements.

## Components

Measured from the rendered page. Token values reference the tokens above.

- **button-ghost ("Show more jobs"):** transparent fill, #000000E6 text, 14px/400, padding 0px.
- **link:** transparent fill, #00000099 text, padding 0px.

## Do's and Don'ts

- Do use #FFFFFF for the page and lift surfaces in small steps: #F4F2EE.
- Do keep text in 3 levels: #191919, #404040, #666666.
- Do use #0A66C2 as the brand accent: header, logo and key highlights.
- Do keep spacing on the 4px grid.
- Do use 24px corners on most elements.
- Don't add drop shadows for depth; use hairline rings and tonal surfaces.
- Don't introduce new font families beyond system-ui.
- Don't use #0A66C2 for large backgrounds or body text.
- Don't make interface transitions slower than 0.216s.

## Motion

Interface transitions are quick: 0.216s.

- **Easing:** `cubic-bezier(0.34, 0, 0.21, 1)`.

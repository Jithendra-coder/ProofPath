# Extractable Components

## Button
- Source: `frontend/app/components/ui.tsx`
- Category: basic
- Description: Small primary action for status and recovery screens.
- Extractable props: `children`, optional `href`, optional `onClick`.
- Hardcoded: class `ui-button` and token-based styling.

## StatusPanel
- Source: `frontend/app/components/ui.tsx`
- Category: layout
- Description: Compact loading, not-found, and error surface.
- Extractable props: `title`, `message`, optional `action`, `href`, `loading`, `onAction`.
- Hardcoded: Proofpath wordmark, panel layout, status mark.

No shared app navigation or page content components exist yet. Skip extraction of the old page-specific HTML until those screens are ported to React.

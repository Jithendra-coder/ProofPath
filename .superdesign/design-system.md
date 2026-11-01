# Proofpath Workspace Design System

## Product and task
- Candidate workspace preview in Next.js App Router with React 19 and custom CSS.
- This is a read-only visual preview. Sample content must stay labeled as sample content.
- The design should make portfolio projects, applications, and the next useful action easy to scan.

## Visual constraints selected by the user
- Use a pure white page and surface background.
- Use charcoal text: primary `#242928`, body `#303332`, muted `#626966`.
- Use burnt orange `#a62a16` as the main accent, dark orange `#842211` for hover/link contrast, soft orange `#fbece6`, and peach `#eea17e` only for small brand marks.
- Avoid green-tinted or dark green panel backgrounds.
- Keep text inputs plain with their existing border as the only focus treatment; no blue glow or added focus shadow.

## Typography and spacing
- Headings: Manrope, with Inter/system fallbacks.
- Body and controls: Inter, with system fallbacks.
- Spacing scale: 4, 8, 12, 16, 24, 32, 48, 64px.
- Radii: 4, 8, 12, 16px. Use 8–12px for workspace controls and panels.
- Main content width: up to 1240px. Desktop workspace has a compact top bar, a persistent left navigation rail, and a vertically scrollable content pane. On mobile, use normal page scrolling and a compact horizontal section nav.

## Interaction and content
- Keep one obvious primary action per screen and show which workspace section is active.
- Use clear labels, meaningful status text, and comfortable touch targets.
- Keep the preview read-only and clearly labeled; do not imply sample profile, project, employer, or application data belongs to the current user.
- Prefer a few useful surfaces and clear section hierarchy over repeated cards or decorative dashboard statistics.

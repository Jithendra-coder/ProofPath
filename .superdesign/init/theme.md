# Theme Sources

There is no Tailwind build config or third-party component theme. The current interface combines per-page HTML/CSS, a shared Next stylesheet and `frontend/auth.css`. The homepage includes a runtime Tailwind CDN config in its document head; it fails to consistently style the Next-served screen and is scheduled to be replaced by local React/CSS.

## `frontend/app/globals.css`

```css
:root {
  color-scheme: light;
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-6: 24px;
  --space-8: 32px;
  --space-12: 48px;
  --space-16: 64px;
  --ink: #172321;
  --muted: #5b6866;
  --line: #dfe6e4;
  --paper: #fff;
  --canvas: #f5f8f7;
  --brand: #a62a16;
  --focus: #0f3d3e;
  --radius-1: 4px;
  --radius-2: 8px;
  --radius-3: 12px;
  --radius-4: 16px;
  font-family: Inter, system-ui, sans-serif;
  color: var(--ink);
  background: var(--canvas);
  font-synthesis: none;
  text-rendering: optimizeLegibility;
}

* { box-sizing: border-box; }
html, body { min-height: 100%; margin: 0; }
body { background: var(--canvas); font-size: 16px; line-height: 1.5; }
a { color: var(--focus); }
button, input, select, textarea { font: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }

.legacy-screen {
  min-height: 100vh;
  color: var(--ink);
  background: var(--canvas);
  font: 16px/1.5 Inter, system-ui, sans-serif !important;
}
.legacy-screen :where(img, video, canvas, svg) { max-width: 100%; }
.legacy-screen :where(pre, code) { overflow-wrap: anywhere; }
.legacy-screen :where(button, input, select, textarea) { max-width: 100%; }
.legacy-screen :where(p) { line-height: 1.55; }
.legacy-screen :where(h1, h2, h3, h4) { text-wrap: pretty; }
.legacy-screen h1 { font-size: clamp(26px, 3vw, 32px) !important; line-height: 1.2 !important; }
.legacy-screen h2 { font-size: clamp(20px, 2.4vw, 24px) !important; line-height: 1.3 !important; }
.legacy-screen h3 { font-size: 18px !important; line-height: 1.35 !important; }
.legacy-screen header:not(.fixed) {
  position: relative !important;
  z-index: 1 !important;
  width: 100% !important;
  padding: 12px max(16px, calc((100vw - 1160px) / 2)) !important;
}
.legacy-screen header.fixed {
  position: sticky !important;
  inset: 0 0 auto !important;
  width: 100% !important;
  height: 56px !important;
  z-index: 20 !important;
}
.legacy-screen main:not(.w-full):not(.layout) {
  width: min(100% - 32px, 1160px) !important;
  margin: 24px auto !important;
}
.legacy-screen main.layout {
  width: min(100% - 32px, 1100px) !important;
  min-height: min(100vh, 820px) !important;
  margin: 0 auto !important;
  padding: 24px !important;
  grid-template-columns: minmax(0, 1.1fr) minmax(260px, 0.9fr) !important;
  gap: 24px !important;
  align-items: center;
}
.legacy-screen .form-panel { min-height: 0 !important; padding: 20px !important; }
.legacy-screen .content { width: min(100%, 420px) !important; padding: 24px 0 !important; }
.legacy-screen .content h1 { font-size: 30px !important; }
.legacy-screen .intro { margin-bottom: 24px !important; }
.legacy-screen .aside { min-height: 340px; padding: 32px !important; border-radius: 16px; }
.legacy-screen .aside h2 { font-size: clamp(28px, 3.5vw, 40px) !important; }
.legacy-screen main.w-full { width: 100% !important; margin: 0 !important; }
.legacy-screen main.w-full > div { min-height: 0 !important; }
.legacy-screen main.w-full > div > section { padding-block: 48px !important; }
.legacy-screen [class~="text-headline-hero"] { font-size: clamp(34px, 4.2vw, 48px) !important; line-height: 1.12 !important; }
.legacy-screen [class~="text-headline-page"] { font-size: clamp(28px, 3vw, 36px) !important; line-height: 1.2 !important; }
.legacy-screen [class~="text-headline-section"] { font-size: clamp(23px, 2.5vw, 28px) !important; line-height: 1.25 !important; }
.legacy-screen [class~="py-24"], .legacy-screen [class~="py-20"] { padding-block: 48px !important; }
.legacy-screen [class~="py-16"] { padding-block: 32px !important; }
.legacy-screen [class~="px-margin"] { padding-inline: clamp(16px, 3vw, 32px) !important; }
.legacy-screen [class~="p-6"], .legacy-screen [class~="p-space-xl"] { padding: 20px !important; }
.legacy-screen [class~="p-8"] { padding: 24px !important; }
.legacy-screen [class~="p-16"] { padding: 32px !important; }
.legacy-screen [class~="mt-16"], .legacy-screen [class~="mt-12"] { margin-top: 32px !important; }
.legacy-screen [class~="mt-10"], .legacy-screen [class~="mt-8"] { margin-top: 24px !important; }
.legacy-screen [class~="min-h-screen"] { min-height: 0 !important; }
.legacy-screen [class~="rounded-3xl"] { border-radius: 16px !important; }
.legacy-screen [class~="shadow-xl"], .legacy-screen [class~="shadow-lg"] { box-shadow: 0 8px 24px rgb(24 47 43 / 8%) !important; }
.legacy-screen :where(.card, .profile, .panel, .assessment, .application, .snapshot) {
  border-color: var(--line) !important;
  border-radius: var(--radius-3) !important;
  box-shadow: 0 1px 3px rgb(24 47 43 / 5%);
}
.legacy-screen :where(.card, .profile, .panel, .assessment, .application, .snapshot) { padding: 18px !important; }
.legacy-screen label { margin: 16px 0 6px !important; font-size: 14px !important; }
.legacy-screen :where(input:not([type="checkbox"]), select, textarea) {
  min-height: 44px !important;
  padding: 10px 12px !important;
  border-color: #c8d3d0 !important;
  border-radius: var(--radius-2) !important;
  font-size: 15px !important;
}
.legacy-screen textarea { min-height: 96px !important; }
.legacy-screen button {
  min-height: 40px !important;
  padding: 8px 12px !important;
  border-radius: var(--radius-2) !important;
  font-size: 14px !important;
}
.legacy-screen .layout button { min-height: 44px !important; }
.legacy-screen :where(nav) { gap: 12px !important; }
.legacy-screen table { width: 100%; }
.legacy-screen :where(.table-wrap, [class~="overflow-x-auto"]) { max-width: 100%; overflow-x: auto; }
.legacy-screen :where(footer) { margin-top: 24px !important; }

.status-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: var(--canvas); }
.status-panel { width: min(100%, 500px); padding: 28px; border: 1px solid var(--line); border-radius: 16px; background: var(--paper); box-shadow: 0 12px 32px rgb(24 47 43 / 7%); }
.ui-brand { color: var(--ink); font: 700 20px/1.2 Manrope, sans-serif; letter-spacing: -.04em; text-decoration: none; }
.ui-brand span { color: var(--brand); }
.status-content { margin-top: 28px; }
.status-content h1 { margin: 0 0 8px; font: 700 26px/1.2 Manrope, sans-serif; letter-spacing: -.035em; }
.status-content p { margin: 0; color: var(--muted); }
.ui-button { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; margin-top: 20px; padding: 10px 16px; border: 0; border-radius: 8px; color: white; background: var(--brand); font-weight: 600; text-decoration: none; cursor: pointer; }
.ui-button:hover { background: #8f2515; }
.ui-button:focus-visible { outline: 3px solid #0f3d3e; outline-offset: 3px; }
.status-mark { display: block; width: 32px; height: 4px; margin-bottom: 20px; border-radius: 999px; background: var(--brand); animation: status-pulse 1.1s ease-in-out infinite alternate; }
@keyframes status-pulse { to { opacity: .35; transform: scaleX(.68); transform-origin: left; } }

@media (max-width: 760px) {
  .legacy-screen main.layout { grid-template-columns: minmax(0, 1fr) !important; padding: 16px !important; }
  .legacy-screen .aside { display: none !important; }
  .legacy-screen .form-panel { padding: 8px !important; }
  .legacy-screen :where(header:not(.fixed), nav) { align-items: flex-start; flex-wrap: wrap; }
  .legacy-screen [class~="py-24"], .legacy-screen [class~="py-20"] { padding-block: 32px !important; }
  .legacy-screen [class~="p-16"] { padding: 24px !important; }
}
@media (max-width: 480px) {
  .status-page { padding: 16px; }
  .status-panel { padding: 22px; }
  .legacy-screen main:not(.w-full):not(.layout) { width: min(100% - 24px, 1160px) !important; margin-block: 16px !important; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
}
```

## `frontend/auth.css`

```css
:root { color-scheme: light; font-family: Inter, sans-serif; color: #111827; background: #fff; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; }
.layout { min-height: 100vh; display: grid; grid-template-columns: 58% 42%; }
.form-panel { padding: 32px clamp(24px, 7vw, 96px); display: flex; flex-direction: column; }
.brand { color: inherit; text-decoration: none; font: 700 22px Manrope, sans-serif; letter-spacing: -0.04em; }
.brand span { color: #a62a16; }
.content { width: min(100%, 420px); margin: auto; padding: 56px 0; }
h1 { margin: 0 0 8px; font: 700 40px/1.2 Manrope, sans-serif; letter-spacing: -0.04em; }
.intro { color: #5a6573; margin: 0 0 32px; line-height: 1.6; }
label { display: block; margin: 20px 0 7px; font-size: 14px; font-weight: 600; }
input:not([type="checkbox"]), select, textarea { width: 100%; min-height: 48px; padding: 12px 14px; border: 1px solid #c6cdd3; border-radius: 8px; font: inherit; background: white; }
textarea { resize: vertical; line-height: 1.5; }
input:focus, select:focus, textarea:focus { outline: 2px solid #0f3d3e; outline-offset: 2px; }
.hint { color: #6b7280; font-size: 13px; margin: 7px 0 0; }
button { margin-top: 24px; width: 100%; min-height: 48px; border: 0; border-radius: 8px; background: #a62a16; color: #fff; font: 600 15px Inter, sans-serif; cursor: pointer; }
button:hover { background: #a93424; }
button.secondary { margin-top: 10px; background: #eaf6f2; color: #174b49; }
button.secondary:hover { background: #d7ebe5; }
button:disabled { opacity: .7; cursor: not-allowed; }
.message { min-height: 24px; margin: 16px 0 0; font-size: 14px; line-height: 1.5; }
.message[data-kind="error"] { color: #b42318; }
.message[data-kind="success"] { color: #166534; }
.trust { margin-top: 28px; color: #3b6566; font-size: 13px; }
.text-link { color: #a62a16; font-weight: 600; }
.terms-row { display: flex; align-items: flex-start; gap: 9px; margin-top: 20px; color: #5a6573; font-size: 13px; line-height: 1.5; }
.terms-row input { margin-top: 2px; accent-color: #a62a16; }
.aside { padding: 56px; background: #ffe5da; display: flex; align-items: flex-end; }
.aside-copy { max-width: 470px; padding-bottom: 5vh; }
.eyebrow { color: #6b7280; font-size: 12px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
h2 { margin: 18px 0 12px; font: 700 clamp(34px, 4vw, 56px)/1.1 Manrope, sans-serif; letter-spacing: -.04em; }
.aside p { margin: 0; color: #394150; font-size: 18px; line-height: 1.6; }
@media (max-width: 800px) { .layout { grid-template-columns: 1fr; } .form-panel { min-height: 100vh; } .aside { display: none; } }
@media (max-width: 480px) { .form-panel { padding: 24px; } h1 { font-size: 32px; } }
```

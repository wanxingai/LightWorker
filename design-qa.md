# LightWorker Active-Run and Composer QA

- Source visual truth: `/var/folders/d2/_9y5p7g94g7fq81k1qz8kxz80000gn/T/codex-clipboard-744535a6-65da-4227-96da-14d837acebd4.png`
- Active implementation screenshot: `/tmp/lightworker-active-layout-v2.png`
- Completed implementation screenshot: `/tmp/lightworker-completed-layout-v2.png`
- Narrow implementation screenshot: `/tmp/lightworker-narrow-layout-v2.png`
- Combined active-state comparison: `/tmp/lightworker-active-comparison-v2.png`
- Source pixels: `1516 × 838`
- Desktop implementation viewport: `1280 × 587`, device scale factor `1`
- Narrow implementation viewport: `720 × 900`, device scale factor `1`
- Comparison normalization: source and desktop implementation were each scaled to `1000px` width and stacked vertically.

## Implemented structure

The active assistant message now follows the reference hierarchy: elapsed status and divider, a short runtime explanation, real tool activity rows in execution order, a centered live progress pill, and a large rounded composer. On success, the process area collapses to `已处理 {elapsed}` while the final Markdown answer remains visible below it.

The composer uses real LightWorker controls only. New tasks retain workspace-source and runtime-settings controls. Existing conversations show the inherited workspace, active runs expose a functional Stop control, the configured model is visible on desktop, and Send remains available for follow-up or steering.

## Required fidelity surfaces

- Fonts and typography: retained the product's Inter/system Chinese stack. Runtime prose uses a more readable `15px` scale and generous line height; metadata remains visually subordinate.
- Spacing and layout: active content follows the source order without nested cards. Tool rows use a flat vertical rhythm. The composer is `24px` rounded, `132px` minimum height, with text above a bottom toolbar.
- Colors and tokens: intentionally retained LightWorker's light theme. Neutral surface, divider, muted metadata, success, and stop colors reuse existing tokens instead of introducing a second theme.
- Shape and surfaces: the process stream is borderless except for the status divider; the composer receives the reference's large rounded surface and restrained elevation.
- Copy and content: all rows derive from actual run data. Tool names are translated into readable activity labels while preserving the technical tool name for inspection.
- Icons and assets: the reference contains no product imagery that must be copied. No fake decorative images, handcrafted SVGs, or placeholder assets were introduced; existing text controls remain consistent with the established LightWorker UI.
- States and behavior: verified active progress, functional Stop, completion collapse, manual expand/collapse, final Markdown visibility, continuing-task context, and model display.
- Accessibility: controls remain semantic buttons, the progress line is `aria-live`, focus styles are inherited, and mobile tap targets for Send and Stop are at least `42px` high.
- Responsiveness: at `720 × 900`, the model label is suppressed, technical tool names are hidden, the composer remains usable, and there is no horizontal overflow.

## Comparison history

### Iteration 1

- Finding: runtime inspector panels appeared before tool activity, weakening the reference's `explanation → activity` reading order.
- Severity: P2, layout/content hierarchy.
- Fix: moved the activity stream immediately below the assistant intro and kept optional runtime inspector sections after it.

- Finding: the centered progress pill used a translucent background, allowing long underlying text to reduce legibility.
- Severity: P2, color/contrast.
- Fix: changed it to an opaque neutral surface with a subtle shadow.

### Iteration 2

- Finding: the generic `Agentic Loop` stage row duplicated the live progress pill and made the activity stream feel like a nested card.
- Severity: P2, spacing/shape.
- Fix: hide the generic stage summary when actual tools or notices exist and stream the tool rows directly.

- Finding: the initial narrow layout kept too much technical metadata in the bottom toolbar and activity rows.
- Severity: P2, responsiveness.
- Fix: hide the model label and technical tool code at narrow widths while retaining readable action labels and controls.

### Final verification

- Active desktop: real run displayed elapsed time, runtime intro, live network activity, progress pill, model, Stop, and Send.
- Completed desktop: process was collapsed by default, retained elapsed time, and kept the final Markdown answer expanded; expanding exposed the stored activity stream and collapsing hid it again.
- Narrow viewport: no horizontal overflow or unusable controls.
- Browser console: no warnings or errors.
- Remaining P0/P1/P2 findings: none.

## Follow-up polish

- None required for the requested interaction. The intentional light-theme difference preserves the existing product identity while matching the reference layout and behavior.

final result: passed

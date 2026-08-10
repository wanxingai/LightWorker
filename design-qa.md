# LightWorker Message Actions, Sidebar, and Citation QA

- Message-action reference: `/var/folders/d2/_9y5p7g94g7fq81k1qz8kxz80000gn/T/codex-clipboard-e17c3a6c-ae40-4a30-baf8-55b51155d914.png`
- Citation reference: `/var/folders/d2/_9y5p7g94g7fq81k1qz8kxz80000gn/T/codex-clipboard-439f15c0-01e2-47ed-995c-b982472ba033.png`
- Hover implementation: `/tmp/lightworker-message-actions-hover.png`
- Citation implementation: `/tmp/lightworker-citation-popover.png`
- Collapsed-sidebar implementation: `/tmp/lightworker-sidebar-collapsed.png`
- Message-action comparison: `/tmp/lightworker-message-actions-comparison.png`
- Citation comparison: `/tmp/lightworker-citation-comparison.png`
- Desktop verification viewport: `1280 × 800`, device scale factor `1`
- Narrow verification viewport: `720 × 900`, device scale factor `1`

## Implemented structure

User and assistant message blocks now expose a bottom-left action row on hover or keyboard focus. The row provides working Copy, Like, Complaint, and creation-time controls. Like and Complaint are mutually exclusive, toggleable, and retained locally per run and role. Touch and narrow layouts keep these actions visible because hover is not dependable there.

The desktop conversation sidebar can be collapsed from its own header and restored from the chat header. The choice survives reloads. At narrow widths the existing off-canvas navigation remains authoritative so the desktop collapsed state does not remove mobile navigation.

Final answers now receive numbered source badges. Standard Markdown source links are annotated in place; older answers without inline links receive a compact source tail after the last answer paragraph. Selecting a badge opens a document preview with the source host, source/observation date, document title, captured excerpt, and a real external link. Citation data is derived from web, browser, HTTP, and RAG tool evidence rather than invented client-side metadata.

## Required fidelity surfaces

- Fonts and typography: retained LightWorker's Inter/system Chinese stack. Action metadata is intentionally subordinate; citation titles and excerpts follow the reference's strong-title/readable-snippet hierarchy.
- Spacing and layout: message actions sit under the message content without shifting it on hover. The source popover anchors next to the selected badge, remains inside the viewport, and uses a compact reading width.
- Colors and tokens: retained the product's light theme while mapping the reference's neutral controls and elevated citation card to existing surface, border, shadow, muted, success, and danger tokens.
- Shape and surfaces: action rows stay flat and borderless. The citation preview uses the reference's rounded elevated card rather than adding a permanent container around every citation.
- Copy and content: labels are concise Chinese product copy. Creation times use the actual turn timestamps. Preview text and URLs come from captured evidence.
- Icons and assets: no placeholder imagery, CSS art, handcrafted SVGs, or fake source logos were introduced. Text controls remain consistent with the existing LightWorker visual language while preserving every requested function.
- States and interactions: verified hidden, hover/focus-visible, liked, complained, cancelled-feedback, citation-open, citation-close, sidebar-collapsed, sidebar-expanded, and reload-persisted states.
- Accessibility: all actions are semantic buttons; feedback exposes `aria-pressed`; citation badges expose expanded state and descriptive labels; the popover is a labelled dialog; Escape and outside click close it; existing focus rings remain available.
- Responsiveness: at `720 × 900`, page width equals viewport width, the sidebar stays off-canvas, both message-action groups are visible, all eight citation badges wrap without clipping, and the composer remains `700 × 104px`.

## Comparison history

### Iteration 1

- Finding: always-visible desktop actions made completed answers feel noisy compared with the hover-led reference.
- Severity: P2, behavior/visual density.
- Fix: reserve stable action-row space but reveal controls only on message hover or `focus-within`; retain visibility on touch/narrow devices.

- Finding: duplicating the existing answer-level Copy button would expose two Copy controls for the same assistant output.
- Severity: P2, content/interaction clarity.
- Fix: removed the old top-right answer toolbar and consolidated Copy with Like, Complaint, and time in the message action row.

### Iteration 2

- Finding: older stored answers did not contain inline Markdown links even when tool evidence included sources.
- Severity: P1, feature completeness.
- Fix: added structured citation extraction to run and conversation payloads and appended unmatched source badges to the final answer paragraph. Agent prompts now request claim-adjacent Markdown links for future output.

- Finding: direct browser extraction sometimes exposed only the domain as the source title.
- Severity: P2, citation content hierarchy.
- Fix: parse both HTML `<title>` and captured `Title:` metadata, while preferring a captured description for the preview excerpt.

### Iteration 3

- Finding: collapsing the desktop sidebar needed an obvious recovery control and reload persistence.
- Severity: P1, navigation behavior.
- Fix: added paired Collapse/Conversation controls, synchronized expanded state, stored the preference locally, and verified both collapsed and expanded states after reload.

### Final verification

- Desktop hover: action row changes from `opacity: 0; visibility: hidden` to visible under the assistant message; both user and assistant groups are mounted.
- Feedback: Like activates; Complaint clears Like and activates itself; selecting Complaint again clears the state.
- Citation: eight real evidence sources render; the first preview resolves to `https://www.chinamoney.com.cn/chinese/bkccpr/`, shows the parsed Chinese document title, a captured excerpt, and observation time.
- Sidebar: collapsed and expanded preferences each survived a full reload.
- Narrow viewport: `body.scrollWidth === innerWidth === 720`; no horizontal overflow; two message-action groups and eight citation badges remain present.
- Source/implementation comparisons: `/tmp/lightworker-message-actions-comparison.png` and `/tmp/lightworker-citation-comparison.png` confirm the requested control placement and source-card hierarchy while preserving LightWorker's established light theme.
- Remaining P0/P1/P2 findings: none.

## Follow-up polish

- None required for the requested interaction. Source-site icons can be added later only when trustworthy icon metadata is available; no fake logos are used now.

final result: passed

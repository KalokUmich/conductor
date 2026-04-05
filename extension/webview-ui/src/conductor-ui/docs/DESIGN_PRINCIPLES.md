# Conductor UI — Design Principles

## Identity: "Warm Intelligence"

We are not cold enterprise software. We are not playful consumer chat. We are the feeling of sitting next to a brilliant colleague in a well-designed studio.

## Three Pillars

### 1. Material Quality (视觉质感)

Every surface has physical presence.

- **Glass materials** with `backdrop-filter: blur()` — elements feel like they exist in space
- **0.5px borders** on Retina — Apple's dark mode signature
- **Three-layer shadows** — inline edge + diffuse ambient + ground contact
- **Elevation** — higher z-index elements are lighter in dark mode
- **Warm palette** — terracotta brand, violet interactive, neutral-cool text

### 2. Kinetic Harmony (动态和谐)

All motion communicates causality.

- **Spring physics** for all transitions (snappy, gentle, bouncy)
- **Enter > Exit**: New content needs 350-500ms to register; removal is 200ms
- **No scale on messages** — `translateY` only (scale = "popping", translate = "sliding into place")
- **Interruptible**: Every animation can be cancelled mid-flight
- **Reduced motion**: Full `prefers-reduced-motion` fallback

### 3. Flow State Protection (心流保护)

The UI never interrupts unnecessarily.

- **Severity hierarchy**: status bar (ambient) → inline (contextual) → toast (notable) → modal (blocking)
- **Keyboard-first**: Every action reachable via keyboard; `Cmd+K` command palette
- **Concurrent rendering**: `useDeferredValue` keeps input responsive during AI streaming
- **Progressive disclosure**: Details collapsed by default, expandable on demand

## Three-Channel Aesthetics

| Channel | Reader | Aesthetic Goal |
|---------|--------|---------------|
| Human → AI | AI model | Intuitive, forgiving, structured commands |
| AI → Human | Human eyes | Zero cognitive burden, scannable, warm |
| AI ↔ AI | AI model | Maximum signal per token, unambiguous |

## Color Philosophy

- **ONE interactive accent**: Violet (`--c-tint: #8b5cf6`)
- **Brand (warm amber)**: Identity only — logo, AI bubble tint, primary CTA
- **Status colors**: Apple dark-mode desaturated (#30d158 green, #ff453a red)
- **Text**: Neutral-cool (#f5f5f7 primary → #545456 muted) — never fights warm accents

## Motion Rules

1. Motion must communicate causality (B emerges from A's position)
2. Duration proportional to distance (4px shift = fast, 200px shift = slow)
3. Spring curves, never `linear` or `ease`
4. Group animations stagger by 50ms per child (max 5)
5. Exits are always faster than enters

## Typography Rules

1. Apple HIG type scale: 10px (caption2) → 18px (title)
2. Letter-spacing widens at small sizes (optical correction)
3. Leading varies by context: heading (1.2), body (1.47), long-form (1.65)
4. Weight creates hierarchy within same size (400 body, 500 labels, 600 headers)
5. Bold in prose uses weight change only, NOT color change

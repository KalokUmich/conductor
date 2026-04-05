# Conductor UI — Reusable Component Library

> "Warm Intelligence" Design System

A standalone library of React components, CSS tokens, and hooks extracted from the Conductor VS Code extension. All components follow the three pillars: **Material Quality**, **Kinetic Harmony**, **Flow State Protection**.

## Structure

```
conductor-ui/
├── tokens/           # CSS custom properties (colors, typography, motion, materials)
├── primitives/       # Atomic building blocks (no business logic)
├── surfaces/         # Container components with material depth
├── content/          # Content rendering components
├── patterns/         # Composite interaction patterns
├── hooks/            # Reusable React hooks
└── docs/             # Design principles, component catalog, guides
```

## Usage

Import components from their category:

```tsx
import { CommandPalette } from './conductor-ui/patterns';
import { useContainerWidth } from './conductor-ui/hooks';
```

## Design Tokens

All visual properties are defined as CSS custom properties in `tokens/`. Import `tokens/index.css` to get the full design system.

| Token Category | File | Examples |
|---------------|------|---------|
| Colors | `colors.css` | `--c-tint`, `--c-label`, `--c-success` |
| Typography | `typography.css` | `--text-body`, `--tracking-caption`, `--leading-long` |
| Motion | `motion.css` | `--spring-gentle`, `--dur-medium`, keyframes |
| Materials | `materials.css` | `--material-chrome`, `--blur-thick`, `--elevation-2` |

## Principles

1. **No business logic** in library components — they are pure UI
2. **Barrel exports** from each category (`index.ts`)
3. **Typed props** with JSDoc documentation on every component
4. **Accessibility built in** — ARIA roles, focus management, reduced motion
5. **Spring physics** — all motion uses spring curves, never linear

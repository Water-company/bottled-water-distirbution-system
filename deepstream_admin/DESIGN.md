# Design System Specification: The Fluid Professional

## 1. Overview & Creative North Star

### Creative North Star: "The Hydrated Workspace"
This design system moves beyond the rigid, boxy constraints of traditional B2B logistics software. Our North Star is **The Hydrated Workspace**: an interface that feels as clear, essential, and frictionless as water itself. We reject the "spreadsheet-trapped-in-a-box" aesthetic in favor of an editorial, high-end experience that balances authoritative reliability with mobile-first agility.

To break the "template" look, we employ **Intentional Asymmetry** and **Tonal Depth**. Instead of standard grids, we use generous white space (negative space) as a structural element. Elements don't just sit on a page; they float in a coordinated ecosystem of light and clarity. This system is designed to be highly legible for drivers in high-glare environments while maintaining a premium "Executive Dashboard" feel for office administrators.

---

## 2. Colors: Tonal Fluidity

Our palette is rooted in the reliability of deep blues, but its execution relies on subtle shifts in luminance rather than harsh lines.

### The "No-Line" Rule
**Borders are prohibited for sectioning.** To define boundaries, designers must use background color shifts (e.g., a `surface-container-low` card nested within a `surface` background). This creates a sophisticated, seamless transition that feels "carved" rather than "drawn."

### Surface Hierarchy & Nesting
We treat the UI as a series of physical layers. Use the `surface-container` tiers to create depth:
*   **Base Layer:** `surface` (#FAF8FF)
*   **Secondary Content:** `surface-container-low` (#F3F3FB)
*   **Primary Interaction Cards:** `surface-container-lowest` (#FFFFFF)
*   **Elevated Overlays:** `surface-container-high` (#E8E7F0)

### The "Glass & Gradient" Rule
To elevate the experience, use **Glassmorphism** for floating mobile navigation or persistent headers. Apply a `surface` color at 80% opacity with a `20px` backdrop blur. 
*   **Signature Textures:** For primary CTAs and Hero sections, use a subtle linear gradient from `primary` (#003178) to `primary-container` (#0D47A1) at a 135-degree angle. This adds "visual soul" and depth that flat color cannot replicate.

---

## 3. Typography: The Editorial Scale

We utilize **Inter** for its neutral, high-legibility "ink traps," making it perfect for data-heavy inventory tables.

*   **Display (lg/md/sm):** Reserved for high-level data summaries (e.g., "4,200 Units"). Use `display-md` (2.75rem) to make critical metrics feel like headlines.
*   **Headline (lg/md/sm):** Use `headline-sm` (1.5rem) for section titles. The contrast between large headlines and small labels creates an authoritative, editorial rhythm.
*   **Body (lg/md/sm):** `body-md` (0.875rem) is the workhorse for table data. Use `on_surface_variant` (#434652) for secondary body text to reduce visual noise.
*   **Labels (md/sm):** All-caps with +0.05em letter spacing for "Status" or "Category" tags to provide a distinct "Utility" feel.

---

## 4. Elevation & Depth: Tonal Layering

We avoid the "floating card" cliché of 2010s design. Depth is achieved through light physics, not heavy drop shadows.

*   **The Layering Principle:** Place a `surface-container-lowest` (Pure White) card on a `surface-container-low` background. The subtle 2% shift in value is enough for the human eye to perceive elevation without the need for a stroke.
*   **Ambient Shadows:** For high-priority floating elements (like a driver's "Confirm Delivery" button), use a multi-layered shadow: `0px 12px 32px rgba(0, 49, 120, 0.06)`. Note the use of a blue-tinted shadow (`primary`) instead of black—this mimics natural ambient light reflecting off water.
*   **The "Ghost Border" Fallback:** If a border is required for accessibility in input fields, use `outline_variant` at **20% opacity**. Never use a 100% opaque border.

---

## 5. Components: Precision Logistics

### Buttons
*   **Primary:** High-contrast `primary` to `primary-container` gradient. `8px` (DEFAULT) rounded corners. Text must be `on_primary` (#FFFFFF).
*   **Secondary:** No background, `outline` at 20% opacity, `primary` text.
*   **Driver Action (Mobile):** Minimum height `3.5rem` (16 spacing unit) for easy thumb interaction.

### Input Fields & Search
*   **Styling:** Forgo the box. Use a `surface-container-highest` background with a bottom-only `primary` stroke (2px) that animates from the center on focus. 
*   **Validation:** Error states use `error` (#BA1A1A) text but a `error_container` (#FFDAD6) background fill for the entire input area to ensure the error is unmissable.

### Inventory Cards & Tables
*   **No Dividers:** Instead of 1px lines between rows, use a `1.1rem` (5 spacing unit) vertical gap. 
*   **Dynamic Status:** Use `tertiary_container` (#005914) with `on_tertiary_fixed` (#002204) text for "Delivered" statuses. This high-contrast pairing ensures "Success" is the most visible element on a driver's screen.

### Mobile "Quick-Flow" Chips
*   **Context:** Selection chips for "Water Type" or "Bottle Size."
*   **Style:** Use `secondary_container` (#D3E2ED) with a `0.75rem` (md) radius. When selected, transition to `primary` with a scale-up effect (1.05x).

---

## 6. Do’s and Don’ts

### Do:
*   **Do** use asymmetrical layouts for dashboards. A large metric on the left balanced by a condensed list on the right creates a premium, custom feel.
*   **Do** use the `1.75rem` (8 spacing unit) as your standard "Breathing Room" between major sections.
*   **Do** use `backdrop-blur` on mobile navigation bars to allow the "Fluid" colors of the inventory to peek through.

### Don't:
*   **Don't** use pure black (#000000) for text. Always use `on_surface` (#1A1B21) to maintain the soft, premium feel.
*   **Don't** use 1px solid dividers. If you must separate content, use a `0.1rem` (0.5 spacing unit) gap of the base `surface` color.
*   **Don't** cram data. If a table has more than 6 columns, use a horizontal scroll with a "Glass" fade effect on the right edge to signal more content.
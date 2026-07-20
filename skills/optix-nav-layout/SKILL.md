---
name: optix-nav-layout
description: Build a multi-screen NavigationPanel layout in Optix via the live bridge — tabs that switch content, done the ONE-mechanism way that actually renders. Use for "add navigation", "multi-screen HMI", "tabs to switch screens", "nav bar with pages".
user_invocable: true
---

# Multi-screen navigation (the pattern that renders)

Use **one** `NavigationPanel` — it is self-contained (tab bar **and** content area
in a single widget). Its `NavigationPanelItem`s each point at a `Screen`, and it
shows the selected one. **Do NOT also add a `PanelLoader`** — a NavigationPanel plus
a separate content loader is the #1 way to end up with a working tab bar over an
**empty white void** (the content never loads because nothing drives the loader).

Every value below is verified against the live bridge.

## Recipe (bridge tools; Studio open + bridge armed)

1. **A loadable screen must be an Object TYPE, not an instance.** Create them under
   `UI/Screens`:
   `optix_bridge_create_widget(project, screen="UI/Screens", name="Overview", widget_type="Screen")`
   `optix_bridge_create_widget(project, screen="UI/Screens", name="Details",  widget_type="Screen")`
   The real invariant: a NavigationPanel/PanelLoader **loads a screen by TYPE and
   instantiates it** — so the target must be an `ObjectType`, not a plain instance.
   `create_widget(type="Screen")` makes a proper `ScreenType`. A
   bridge **widget instance** used as a screen is the trap — it renders in the Studio
   designer but the **runtime can't load it**.
   > Note: `Screen` is a subtype of `Panel`, so a **`PanelType`** is also a valid loadable
   > "screen" (people use them when they don't need Screen-specific features). Both are
   > ObjectTypes — that's the load-bearing part. The bridge's `create_widget(type="Panel")`
   > currently makes a Panel **instance** (an inline layout container), not a PanelType, so
   > for a loadable page use `type="Screen"`.
   Put content on each: `optix_bridge_add_label(project, "UI/Screens/Overview", "Title", "Overview", left=20, top=20)` (repeat for Details).

2. **Create the NavigationPanel on the window:**
   `optix_bridge_create_widget(project, screen="UI/MainWindow", name="MainNav", widget_type="NavigationPanel")`

3. **Add one NavigationPanelItem per screen, pointing `Panel` at the screen.**
   `Panel` is a **NodeId** (NodePointer) — pass the screen's node PATH; the bridge
   resolves it:
   - create item: `optix_bridge_create_widget(project, "UI/MainWindow/MainNav/Panels", "OverviewItem", "NavigationPanelItem")`
   - title: `optix_bridge_set_property(project, "UI/MainWindow/MainNav/Panels/OverviewItem", "Title", "Overview")`
   - target: `optix_bridge_set_property(project, "UI/MainWindow/MainNav/Panels/OverviewItem", "Panel", "UI/Screens/Overview")`
   (repeat for DetailsItem → Details.)

4. **Select the initial tab** — set `CurrentTabIndex` (0-based **Int32**), NOT
   `CurrentPanel`:
   `optix_bridge_set_property(project, "UI/MainWindow/MainNav", "CurrentTabIndex", "0")`
   ⚠️ `CurrentPanel` is **read-only** on a NavigationPanel — setting it fails
   `permission denied`. It's the *computed* reflection of
   the selection; `CurrentTabIndex` is the settable control. (There are also
   `ChangePanelByTabIndexMethod` / `ChangePanelByTabNameMethod` for wiring a tab
   change to an event.)
   > **Render-verify still open:** a NavigationPanel also exposes `AttachedPanelLoader`;
   > it's not yet confirmed whether items + `CurrentTabIndex` render on their own or
   > need an attached loader wired. Deploy + CDP-screenshot to confirm before trusting
   > the visual result.

5. **Verify:**
   `optix_restart_emulator(project)` →
   `optix_cdp_screenshot(project, save_path="<your session dir>/nav-verify.jpg")`.
   **Always pass `save_path`** and read the file — an inline base64 return makes some
   hosts try to *render* it ("visualize"), which stalls on a headless/sandboxed box.

## Gotchas (each cost a real debugging cycle)

- **One content mechanism only.** NavigationPanel is tabs+content in one. A second
  PanelLoader alongside it renders the empty void the operator kept hitting.
- **Screens are `Screen` types**, created under `UI/Screens` — not bridge widgets
  repurposed as screens.
- **The item's `Panel` is a NodeId prop** — set it to a node **path** (e.g.
  `UI/Screens/Overview`); the bridge resolves path→NodeId. Setting it to a
  raw string on an older bridge fails "Conversion to NodeId not supported".
- **Do NOT set `CurrentPanel`** — it's read-only (`permission denied`).
  Use `CurrentTabIndex` (Int32, 0-based) to pick the active tab.
- **`describe_type` before you set.** `optix_describe_type("NavigationPanel")` returns
  the authoritative property legend (inheritance-complete). The
  bridge rejects an undeclared property with `unknown_property` rather than author
  something the type can't hold — so consult it instead of guessing (`CurrentPanel` vs
  `CurrentTabIndex` is exactly this trap).
- **If the runtime serves nothing,** ensure a web presentation engine exists first:
  `optix_bridge_ensure_web_engine(project)` (or right-click the bridge NetLogic →
  SetupProject) — a fresh project has none, so the canvas won't serve.

## Verify checklist
Deploy → screenshot shows the tab bar **and** the first screen's content (not a white
void) → CDP-click a tab (coords read off the screenshot) → screenshot shows the other
screen's content. Both mean the item `Panel` NodeId wiring + `CurrentTabIndex` landed.
(If content is a white void even with items wired, try setting `AttachedPanelLoader` —
render-behavior of items-alone vs attached-loader is not yet live-confirmed.)

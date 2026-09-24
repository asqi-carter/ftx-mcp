# FT Optix enum wire-name lookup table

FT Optix exposes two name sets for the same enum property. The **Module.xml**
(OPC UA information-model file at
`Modules/FTOptix.UI/21.2.0.89/Module.xml` and the `FTOptix.CoreBase`
equivalent) defines DataType browse names and their enumeration field names.
The **bridge** (.NET reflection on the loaded FTOptix assemblies) accepts the
friendly member names from the `FTOptix.UI.*` enum types. For 15 of the 22
enum properties probed, these two name sets differ — usually a dropped prefix
or suffix, but member **counts** also differ in some cases — so the mapping
cannot be derived from a rule. This table is the lookup.

> **Visibility note.** This file is invisible to MCP agents: `optix_get_skill`
> serves only `SKILL.md` bodies, so a doc under `docs/` never reaches the
> model's context. The three-line operational rule (use numeric ordinals; to
> harvest the real string set issue a deliberately bad `set_property` and read
> the `bad_value` reply) lives **inline** in each skill that sets enum
> properties. This document is the reference for human operators and reviewers.

---

## Full 22-enum wire-name table

Sourced from `probe_enum_wire_names.py` run against Optix 1.7.4.32 /
FTOptix.UI 21.2.0.89.  "Module.xml name" is the DataType's browse name and
(where it differs) its field-name pattern. "Bridge-accepted name" is the
member name the bridge accepts via reflection. Numeric ordinals are always
accepted and both sources agree on them.

| Module.xml name | Bridge-accepted name | Member-count note |
|---|---|---|
| `HorizontalAlignment` | `Left`, `Right`, `Center`, `Stretch` | 4 members; names **identical** in Module.xml — no prefix/suffix difference |
| `VerticalAlignment` | `Top`, `Bottom`, `Center`, `Stretch` | 4 members; names **identical** — ordinal order: Top=0, Bottom=1, Center=2, Stretch=3 (NOT the WPF standard; Center is 2, not 1) |
| `TextHorizontalAlignment` | `Left`, `Center`, `Right` | 3 members; names **identical** in Module.xml |
| `TextVerticalAlignment` | `Top`, `Center`, `Bottom` | 3 members; names **identical** in Module.xml |
| `ScrollBarVisibility` | `Auto`, `Hidden`, `Disabled`, `Visible` | 4 members; names **identical** in Module.xml |
| `StrokeStyle` | `Solid`, `Dot`, `Dash`, `DashDot`, `DashDotDot` | 5 members; names **identical** in Module.xml |
| `DateTimePickerMode` | `DateTime`, `Date`, `Time` | 3 members; names **identical** in Module.xml |
| `FillMode` | `Fit`, `Fill`, `None`, `Stretch` | 4 members; **DIFFERS** — Module.xml field names carry the type name as prefix: `FillModeFit`, `FillModeFill`, `FillModeNone`, `FillModeStretch` |
| `Orientation` | `Horizontal`, `Vertical` | 2 members; **DIFFERS** — Module.xml: `OrientationHorizontal`, `OrientationVertical` |
| `SortOrder` | `Ascending`, `Descending` | 2 members; **DIFFERS** — Module.xml: `AscendingSortOrder`, `DescendingSortOrder` |
| `FontWeightEnum` | `Thin`, `ExtraLight`, `Light`, `Normal`, `Medium`, `SemiBold`, `Bold`, `ExtraBold`, `Black` | 9 members; **DIFFERS** — DataType browse name in Module.xml is `FontWeightEnum`; bridge resolves to .NET type `FontWeight` (strip `Enum` suffix) |
| `FontStyleEnum` | `Normal`, `Italic`, `Oblique` | 3 members; **DIFFERS** — DataType `FontStyleEnum` → .NET `FontStyle` (strip `Enum` suffix) |
| `WrapMode` | `NoWrap`, `Char`, `Word` | 3 bridge members vs 4 Module.xml fields (**count differs**); Module.xml includes `NoWrapWrapMode`, `CharWrapMode`, `WordWrapMode`, plus a legacy `DefaultWrapMode` that the bridge omits |
| `InputMode` | `Numeric`, `Standard`, `Password` | 3 members; **DIFFERS** — Module.xml: `NumericInputMode`, `StandardInputMode`, `PasswordInputMode` |
| `MaskMode` | `No`, `Simple`, `RegEx` | 3 members; **DIFFERS** — Module.xml: `NoMaskMode`, `SimpleMaskMode`, `RegExMaskMode` |
| `ProgressBarStyle` | `Continuous`, `Segmented` | 2 members; **DIFFERS** — Module.xml: `ContinuousProgressBarStyle`, `SegmentedProgressBarStyle` |
| `TabBarPosition` | `Top`, `Bottom`, `Left`, `Right` | 4 members; **DIFFERS** — Module.xml: `TopTabBarPosition`, `BottomTabBarPosition`, `LeftTabBarPosition`, `RightTabBarPosition` |
| `AnchorHorizontal` | `Left`, `Center`, `Right`, `Stretch` | 4 members; **DIFFERS** — Module.xml: `LeftAnchorHorizontal`, `CenterAnchorHorizontal`, `RightAnchorHorizontal`, `StretchAnchorHorizontal` |
| `AnchorVertical` | `Top`, `Center`, `Bottom`, `Stretch` | 4 members; **DIFFERS** — Module.xml: `TopAnchorVertical`, `CenterAnchorVertical`, `BottomAnchorVertical`, `StretchAnchorVertical` |
| `MotionType` | `None`, `Fade`, `Slide` | 3 members; **DIFFERS** — Module.xml: `NoMotionType`, `FadeMotionType`, `SlideMotionType` |
| `ContentMode` | `Fixed`, `Adaptive` | 2 members; **DIFFERS** — Module.xml: `FixedContentMode`, `AdaptiveContentMode` |
| `ValueChangeBehaviour` | `OnRelease`, `OnStep` | 2 members; **DIFFERS** — SpinBox only; the Module.xml DataType browse name does not map to any loaded .NET enum type by name, so `ResolveEnumType` returns null and the member list comes from `DeclaredEnumType` (the owner's CLR proxy) instead. This was register **I31** (2026-08-28): before the fix an invalid member triggered a native assert that killed Studio. See harvest procedure below. |

**Summary:** 7 of 22 enums have identical Module.xml and bridge member names;
**15 of 22 differ** — usually by a dropped prefix or suffix, but in two cases
(`WrapMode`, `ValueChangeBehaviour`) the member count or type-resolution path
also differs. There is no derivable rule; use the table or the harvest
procedure.

---

## Illustrative examples

The three examples cited inline in every skill that sets enum properties:

| Module.xml field name | Bridge-accepted member | Enum type |
|---|---|---|
| `FillModeFit` | `Fit` | `FillMode` |
| `OrientationHorizontal` | `Horizontal` | `Orientation` |
| `AscendingSortOrder` | `Ascending` | `SortOrder` |

In every case the numeric ordinal works too: `FillModeFit` = 0, `OrientationHorizontal` = 0,
`AscendingSortOrder` = 0. Both sources agree on ordinal values.

---

## Harvest procedure

When an enum property is not in this table, or you need to confirm the live
member set, issue a deliberately invalid `set_property`:

```
optix_bridge_edit(project, ops=[{
    "op": "set_property",
    "path": "<node with the property>",
    "name": "<PropertyName>",
    "value": "__probe__"
}])
```

The bridge answers `bad_value` with the **valid member list** and writes
nothing to the model. Studio stays up. This is the mechanism behind register
**I31** (fixed in 1.0.8): before the fix, the one enum
(`SpinBox.ValueChangeBehaviour`) whose DataType browse name did not match any
loaded .NET enum type by name fell through to a bare-string assign, triggering
an uncatchable native assert that killed the Studio process. After the fix,
all 22 enum properties return `bad_value` + the valid member list on an
invalid value.

`ci/agent-smoke.txt` step 6 asserts this behaviour:

> Try a deliberately bad enum: … create a SpinBox UI/Screens/SmokeScreen/Spin
> and set ValueChangeBehaviour to `__probe__`. The bridge must answer
> `bad_value` with the member list and Studio must stay up (optix_bridge_status
> again). This is the I31 check.

---

## Provenance

- **`probe_enum_wire_names.py`** — one-shot script that set `__probe__` on
  each of the 22 enum-typed properties, collected the `bad_value` member
  lists, and compared them to the Module.xml field names. Not committed to
  this repo (ad-hoc probe tool); results are captured in this table.
- **`Modules/FTOptix.UI/21.2.0.89/Module.xml`** — OPC UA NodeSet2 file
  shipped with FT Optix Studio 1.7.4.32 / FTOptix.UI 21.2.0.89. Defines
  DataType browse names and their enumeration field names. The `FTOptix.CoreBase`
  module supplies additional DataTypes (`SortOrder` and others).

All data was measured live against the CI project `CI_619952f` (FTOptix.UI
21.2.0.89 / Studio 1.7.4.32). Ordinal values were verified by cross-referencing
`probe_enum_wire_names.py` output with the .NET assembly reflection results.

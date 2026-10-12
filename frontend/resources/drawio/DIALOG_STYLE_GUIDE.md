# Dialog Style Guide

All new dialogs using `CustomDialog` should follow these patterns for consistent
appearance. CSS classes are defined in `/src/main/webapp/styles/grapheditor.css`.

## Auto-Height

Pass `null` for the height parameter in `showDialog()`. The `Dialog` constructor
measures `elt.scrollHeight` after the element is in the DOM and sizes the dialog
automatically. Never compute dialog heights manually with `height += N` increments.

```javascript
var dlg = new CustomDialog(this, div, okFn, null, mxResources.get('export'), helpLink);
this.showDialog(dlg.container, 360, null, true, true);
```

## Spacing (handled by CustomDialog and Dialog)

- **Dialog padding**: 24px on all sides (`.geDialog` CSS)
- **Buttons margin-top**: 34px (default in `CustomDialog`)
- **Container padding-bottom**: 10px (`CustomDialog` adds this to prevent margin collapse)
- **Dialog chrome**: `Dialog` constructor adds 48px to both width and height for padding

These produce equal visual spacing above (34px) and below (34px = 10px + 24px) the
button row. Do not override these values in individual dialogs.

## Sections (`geDialogSection`)

Group related controls in rounded section containers:

```javascript
var section = document.createElement('div');
section.className = 'geDialogSection';
// ... add rows to section ...
div.appendChild(section);
```

## Form Rows (`geDialogFormRow`)

Use for label + input pairs (text fields, selects):

```javascript
var row = document.createElement('div');
row.className = 'geDialogFormRow';

var lbl = document.createElement('span');
lbl.className = 'geDialogFormLabel';
mxUtils.write(lbl, mxResources.get('zoom') + ':');
row.appendChild(lbl);

var input = document.createElement('input');
input.setAttribute('type', 'text');
row.appendChild(input);

section.appendChild(row);
```

## Checkbox Rows (`geDialogCheckRow`)

Use `addCheckbox` with `useCheckRow=true` (9th parameter) for consistent checkbox
layout with flex alignment:

```javascript
var cb = this.addCheckbox(section, mxResources.get('shadow'),
    false, null, null, null, null, null, true);
```

For indented sub-options (e.g., "Layers" under "Lightbox"):

```javascript
cb.checkRow.style.paddingLeft = '24px';
```

## Inline Fields (`geDialogInlineFields` / `geDialogInlineField`)

Use for side-by-side fields (e.g., Width + Height):

```javascript
var row = document.createElement('div');
row.className = 'geDialogInlineFields';

var field = document.createElement('div');
field.className = 'geDialogInlineField';
// ... add label + input to field ...
row.appendChild(field);

section.appendChild(row);
```

## Dialog Title

Center-aligned `<h3>` with standard margins:

```javascript
var hd = document.createElement('h3');
mxUtils.write(hd, title);
hd.style.cssText = 'width:100%;text-align:center;margin-top:0px;margin-bottom:10px';
div.appendChild(hd);
```

## Edit Button (Lightbox Dialogs)

Use `addEditButton` which creates a `geDialogCheckRow` with an indented select:

```javascript
var editSection = this.addEditButton(optSection, lightbox);
var edit = editSection.getEditInput();
```

## Alignment of Dialog Items

Radio/checkbox rows (`geDialogCheckRow`) use flexbox with `align-items: center`.
When a radio button is followed by an input or select, both elements sit after
the radio's right margin (8px). The CSS rule `.geDialogCheckRow select` adds
`margin-left: 8px` by default — this is intended for selects that follow a
checkbox + label. When a select sits directly after a radio button (same position
as a text input in another row), reset the margin so both align:

```javascript
pageSelect.style.marginLeft = '0';
```

For `geDialogFormRow`, the `geDialogFormLabel` span provides a fixed-width left
column (`min-width: 100px`). All inputs and selects use `flex: 1` to fill the
remaining space. If labels in a specific dialog are longer (e.g. German
translations), increase the dialog width rather than overriding `min-width` on
individual labels.

## Non-Resizable Dialogs

Most dialogs are non-resizable. Do **not** pass a `minSize` parameter to
`showDialog()` — this avoids adding a resize handler. Use `null` for the height
parameter so the dialog auto-sizes to its content:

```javascript
this.showDialog(dlg.container, 360, null, true, true);
```

Only pass `minSize` (a `mxRectangle`) when the dialog content is scrollable or
has a variable-height area that benefits from user resizing.

## Button Spacing (Manual Buttons)

When using `CustomDialog`, button spacing is handled automatically (34px
margin-top, 10px padding-bottom on the content wrapper). When building buttons
manually (e.g., dialogs with custom button arrangements like Reset + Open),
replicate the same values:

```javascript
// Content wrapper needs padding-bottom to prevent margin collapse
div.style.paddingBottom = '10px';

// Button row
var btns = document.createElement('div');
btns.style.marginTop = '34px';
btns.style.textAlign = 'right';
```

This produces equal visual spacing above (34px) and below (34px = 10px + 24px
dialog padding) the button row, matching `CustomDialog` exactly. Never use
other margin values (16px, 20px, 30px) for button rows.

## Dark Mode

All CSS uses `light-dark()` for color values. Never hardcode colors in JS — use
the CSS classes which handle both modes automatically.

## Testing Dialogs

Use `?dev=1&ui=classic` to keep the menubar visible regardless of window size.
Test with both single-page and multi-page diagrams, as extra controls appear
when multiple pages exist.

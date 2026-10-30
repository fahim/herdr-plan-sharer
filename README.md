# Herdr Plan Sharer

Copy or export the latest completed plan from the focused Codex or Claude pane.

## Actions

- `fahim.plan-sharer.copy` copies raw plan Markdown to the macOS clipboard.
- `fahim.plan-sharer.export` writes a private, timestamped Markdown file under
  `~/Documents/Herdr Plans`, reveals it in Finder, and copies its absolute path.

If a visible Herdr terminal selection exists, either action uses that exact
selection. Otherwise the plugin finds the latest tagged plan in the focused
Codex session or the plan file referenced by the focused Claude pane. It never
chooses a globally recent plan from another agent.

## Install

Requires macOS, Herdr 0.9.1 or newer, Python 3, `lsof`, `pbcopy`, and `open`.

```sh
herdr plugin install fahim/herdr-plan-sharer
```

For development, link a local checkout instead:

```sh
herdr plugin link /path/to/herdr-plan-sharer
```

Add these entries to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+y"
type = "plugin_action"
command = "fahim.plan-sharer.copy"
description = "copy latest agent plan"

[[keys.command]]
key = "prefix+shift+y"
type = "plugin_action"
command = "fahim.plan-sharer.export"
description = "export latest agent plan"
```

Then run `herdr server reload-config`.

You can also invoke either action directly:

```sh
herdr plugin action invoke fahim.plan-sharer.copy
herdr plugin action invoke fahim.plan-sharer.export
```

## Test

```sh
python3 -m unittest discover -s tests -v
```

Network link sharing is intentionally outside v1; exported Markdown files can
be attached using any local sharing tool.

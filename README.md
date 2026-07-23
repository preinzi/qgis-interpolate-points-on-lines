# Interpolate Points on Lines

A QGIS Processing script that builds Z/M-enabled line and point geometries from a line layer plus a set of measurement points along those lines.

[screenshot](screenshot.png)

## What it does

Given a line layer and a set of measurement points that lie along those lines, this tool:

- Snaps each measurement point onto its line and inserts it as a vertex at the correct position.
- Builds a `LineStringZM` version of each line, where **Z** comes from the measurement values (interpolated linearly between points for the line's own vertices) and **M** is the station (distance along the line).
- Optionally generates points at a regular interval along each line, reusing an existing measurement point instead of creating a redundant one nearby.
- Optionally segments each line into individual features between consecutive points, carrying that segment's station/measure/point type as attributes.

Lines with fewer than 2 matching measurement points are skipped (with a warning explaining why).

## Installation

This is a single-file **Processing script**, not a full plugin:

1. Download [`interpolate_points_on_lines.py`](./interpolate_points_on_lines.py).
2. In QGIS: **Processing → Toolbox → Scripts (gear icon) → Add Script to Toolbox…**, and select the file.
3. It will appear under **Vector Analysis → Interpolate Points on Lines**.

Tested on QGIS 3.34.4-Prizren. No extra Python packages required beyond what ships with QGIS.

## Usage

| Parameter | Description |
|---|---|
| **Line layer** | Line features with unique IDs |
| **Measurement points layer** | Point features carrying a line ID and a numeric measurement value |
| **Line ID field** | Field on the line layer identifying each line |
| **Point ID field** | Field on the points layer matching each point to its line |
| **Z-value field (numeric)** | Field on the points layer holding the measurement value |
| **Tolerance (map units)** | Warn if a measurement point sits farther from its line than this |
| **Point interval (map units)** | Spacing between generated points along the output line |

### Outputs

| Output | Default | Description |
|---|---|---|
| **Output line segments** | on | One line feature per segment between two consecutive points, carrying `station` / `measure` / `point_type` attributes |
| **Output simple lines** | off | `LineStringZM` layer with the original line shape plus the measurement points |
| **Output points** | off | Points every *interval* map units along each line (existing measurement points are reused where close enough), with Z/M in the geometry plus `station` / `measure` / `point_type` attributes |

## License

GPL-2.0-or-later — see [LICENSE](./LICENSE). This follows QGIS's own licensing, since the script builds on the PyQGIS API.

## Credits

Written by Stephan Preinstorfer (LiberGIS) with help from Claude (Anthropic).

## Contributing

Issues and pull requests welcome.

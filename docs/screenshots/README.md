# Screenshots

This directory should contain the following screenshots for the main README:

## Required Screenshots

1. **comparison.png** — Dual-pane library comparison view showing:
   - Left and right library selections
   - Matched/unmatched file indicators
   - Match confidence scores
   - File metadata side-by-side

2. **duplicates.png** — Duplicate detection results showing:
   - Grouped duplicate files
   - Content-identical vs different-quality versions
   - Quality scores and recommendations
   - Selection controls

3. **player.png** — Built-in audio player showing:
   - Playback controls
   - Track metadata display
   - Waveform or progress bar
   - Queue management

## Capture Guidelines

- **Resolution:** 1920x1080 or higher
- **Format:** PNG (for quality) or WebP (for smaller size)
- **Window:** Full browser window or main content area
- **Theme:** Default Windows 10 Metro style
- **Data:** Use demo library (run `python run-test.py`) to avoid exposing real file paths

## How to Capture

```bash
# Start test mode
python run-test.py

# Open browser to http://127.0.0.1:8787
# Navigate to each view and take screenshots
# Save as PNG files in this directory
```

Alternatively, use the CDP screenshot tool:

```bash
node tools/cdp_vp.js <chrome-port> http://127.0.0.1:8787/ 20000 <script.js> 1500 docs/screenshots/
```

## Placeholder

Until screenshots are added, README will show broken image links. This is expected during initial setup.

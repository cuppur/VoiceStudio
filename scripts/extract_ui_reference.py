"""Extract visual reference data without executing prototype JavaScript."""
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = Path.home() / "Downloads" / "VoiceStudio_Full_UI_Prototype_v4.html"
html = source.read_text(encoding="utf-8")
icons = root / "src/local_voice_studio/ui/resources/prototype"
icons.mkdir(parents=True, exist_ok=True)
for name, svg in re.findall(r'<button class="nav-item[^"]*" data-page="([^"]+)">(<svg.*?</svg>)', html, re.S):
    (icons / f"{name}.svg").write_text(svg.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" '), encoding="utf-8")
brand = re.search(r'<div class="brand-mark">(<svg.*?</svg>)', html, re.S)
if brand:
    (icons / "brand.svg").write_text(brand[1].replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" '), encoding="utf-8")
reference = root / "docs/ui-baseline/html-v4"
reference.mkdir(parents=True, exist_ok=True)
(reference / "reference.html").write_text(html, encoding="utf-8")
inventory = {
    "source": str(source), "pages": re.findall(r'data-page-view="([^"]+)"', html),
    "colors": dict(re.findall(r'--([\w]+):([^;\n]+)', html[:html.index('</style>')])),
    "identified_controls": re.findall(r'<(?:button|input|select|textarea)[^>]*\bid="([^"]+)"', html),
    "baseline_viewport": [1440, 900], "status": "reference extracted; browser screenshots pending",
}
(reference / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Extracted {len(list(icons.glob('*.svg')))} SVGs; {len(inventory['identified_controls'])} identified controls")

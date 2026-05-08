import sys
with open(r'c:\Users\HP\Desktop\Files\GRID\calibration_lib\ec\processors\L2_processor.py', 'r', encoding='utf-8') as f:
    text = f.read()

import re

text = re.sub(
    r'ax\.step\(mids, hist, where=\"mid\", color=\"#2b2d42\", linewidth=1\.5, label=\"spectrum\"\)',
    r'ax.step(mids, hist, where="mid", lw=1.0, color="0.5", label="all spectrum")',
    text
)

text = re.sub(
    r'ax\.plot\(x, y, color=\"#0f9d58\", linewidth=2\.0, label=\"fit\"\)',
    r'ax.plot(x, y, color="C3", lw=2.0, label="fit_result")',
    text
)

text = re.sub(
    r'ax\.set_title\(f\"Energy Spectrum Fit\\nE=\{mu:\.2f\}, Res=\{fit_result\[\'res\'\]:\.1f\}%\"\)',
    r'ax.set_title(\n            f"Energy Spectrum Fit: center={mu:.2f}, sigma={sigma:.2f}, Res={fit_result[\"res\"]:.1f}%",\n            fontsize=10,\n        )',
    text
)

text = re.sub(
    r'ax\.legend\(\)\n\s+ax\.grid\(True, linestyle=\"--\", alpha=0\.3\)',
    r'ax.legend(fontsize=8)',
    text
)

text = re.sub(
    r'ax\.axvline\(mu, color=\"red\", linestyle=\"--\", linewidth=1\.0, alpha=0\.8\)',
    r'ax.axvline(mu, color="r", ls="--", lw=1.0)',
    text
)

with open(r'c:\Users\HP\Desktop\Files\GRID\calibration_lib\ec\processors\L2_processor.py', 'w', encoding='utf-8') as f:
    f.write(text)

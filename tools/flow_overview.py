"""标定全流程可视化总览生成器（只读扫描 + 自包含 HTML）。

扫描 ``products/<payload>/`` 下 TB / EC 两线、共 7 层（TB L0→L2、EC L0→L3）的
**实际产物**，生成一份自包含 HTML 总览页：数据内嵌、原生 HTML/CSS/JS、中文界面、
无需联网、无前端依赖，双击即可查看。

设计约束（与仓库 AGENTS.md 一致）：

- 只使用 **Python 标准库**；
- **只读**扫描 ``products/``，不修改任何标定算法、原始数据或产物；
- 所有统计数字都来自真实目录扫描，不写死；某层无产物时页面显示「缺少产物」；
- 文件存在只表示磁盘上有**历史产物**，不代表本次运行成功、也不代表通过 schema 校验。

用法（仓库根目录）::

    python calibration_lib/tools/flow_overview.py
    python calibration_lib/tools/flow_overview.py --products products --out course_outputs/calibration_flow.html
    python calibration_lib/tools/flow_overview.py --check   # 生成后校验所有内嵌链接是否指向真实文件

本脚本是纯标准库实现，**可以直接用任意 python 运行、无需安装任何依赖**（推荐上面的写法）。
若改用 ``python -m calibration_lib.tools.flow_overview``，会先导入包 ``calibration_lib``
（进而拉起 pandas / lmfit 等重依赖），需要已 ``uv sync`` 好的环境。

产物：``course_outputs/calibration_flow.html``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence
from urllib.parse import quote

# 本文件位于 <repo>/calibration_lib/tools/flow_overview.py
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PRODUCTS = "products"
DEFAULT_OUT = "course_outputs/calibration_flow.html"

# 载荷显示名。默认扫描这两个载荷，可用 --payloads 覆盖。
PAYLOAD_LABELS = {
    "14B": "14B（GRID-02）",
    "15B": "15B（GRID-03）",
}


# --------------------------------------------------------------------------- #
# 层级元数据：用途 / 输入 / 输出 / 依赖 / 产物匹配模式
# 说明文本对齐 cali_format.wiki/home 下各层规范（01_TB / 02_EC / 00_Naming-and-Layout）。
# --------------------------------------------------------------------------- #
LAYER_META: List[Dict[str, Any]] = [
    {
        "key": "TB_L0",
        "pipeline": "TB",
        "tag": "L0",
        "name": "TB L0 · 原始包解析",
        "short": "解析原始包",
        "purpose": (
            "把温度-偏压实验的原始二进制包（.event.dat + .hk）解析为事件级 parquet，"
            "按文件落盘；4 通道温度 / 偏压及其不确定度写入 parquet schema metadata（key = hk_data）。"
        ),
        "inputs": [
            "原始包：*.event.dat + *.hk（14、15B标定数据/<payload>/Temp_Vbias/）",
        ],
        "outputs": [
            "<stem>.l0.parquet（列：amp / channel / crc_check；metadata：hk_data）",
            "L0_manifest.json（批处理日志）",
        ],
        "deps": [],
        "artifacts": [
            {"label": "L0 parquet", "kind": "data", "patterns": ["tb/L0/parquet/*.l0.parquet"]},
            {"label": "Manifest", "kind": "manifest", "patterns": ["tb/L0/json/*.json"]},
        ],
    },
    {
        "key": "TB_L1",
        "pipeline": "TB",
        "tag": "L1",
        "name": "TB L1 · 单峰 + 单背景拟合",
        "short": "单峰拟合",
        "purpose": (
            "对每个文件、每个通道的 amp 直方图做「单高斯峰 + 单背景（4 选 1：gaus / lin / exp / quad）」"
            "拟合，自动按 R²、AIC 挑选最佳模式，导出峰中心 / σ / 振幅及其误差，供 L2 加权使用。"
        ),
        "inputs": [
            "TB L0 parquet（<stem>.l0.parquet）",
            "手选窗资源：resources/legacy_fit_windows_config_<payload>.json（只读）",
        ],
        "outputs": [
            "<stem>_fit_results.l1.json（list[record]，4 通道各一条）",
            "<stem>_fit.l1.jpg（4 通道拟合校验图）",
            "L1_manifest.json / fit_windows_used.json",
        ],
        "deps": ["TB_L0"],
        "artifacts": [
            {"label": "拟合 JSON", "kind": "data", "patterns": ["tb/L1/json/*_fit_results.l1.json"]},
            {"label": "拟合图", "kind": "figure", "patterns": ["tb/L1/figures/*.l1.jpg"]},
            {
                "label": "Manifest",
                "kind": "manifest",
                "patterns": ["tb/L1/json/L1_manifest.json", "tb/L1/json/fit_windows_used.json"],
            },
        ],
    },
    {
        "key": "TB_L2",
        "pipeline": "TB",
        "tag": "L2",
        "name": "TB L2 · 温偏曲面拟合",
        "short": "温偏曲面拟合",
        "purpose": (
            "汇总全部 L1 峰中心，按通道做 5 参数物理曲面拟合 "
            "G(T,V) = G0·(V − k·T − V0)²·(−T² + b·T + c)，以 1/center_err 加权，"
            "得到每通道一套增益-温偏参数。"
        ),
        "inputs": [
            "TB L1 拟合 JSON（峰中心 + 每通道温度 / 偏压）",
        ],
        "outputs": [
            "<timestamp>_<payload>_TB.l2.json（list[4]，每通道 11 字段）",
            "<timestamp>_<payload>_ch<n>_TB_fit.l2.png（每通道曲面拟合图）",
            "L2_manifest.json",
        ],
        "deps": ["TB_L1"],
        "artifacts": [
            {"label": "TB 参数 JSON", "kind": "data", "patterns": ["tb/L2/json/*_TB.l2.json"]},
            {"label": "拟合图", "kind": "figure", "patterns": ["tb/L2/figures/*.l2.png"]},
            {"label": "Manifest", "kind": "manifest", "patterns": ["tb/L2/json/L2_manifest.json"]},
        ],
    },
    {
        "key": "EC_L0",
        "pipeline": "EC",
        "tag": "L0",
        "name": "EC L0 · 原始包解析",
        "short": "解析原始包",
        "purpose": (
            "把能量标定（X 光机 / 放射源 / 本底）的原始包解析为 parquet。"
            "与 TB L0 不同：温度 / 偏压**逐脉冲写入数据列**（按 UTC 由 HK 线性内插），供 L1 直接做温偏矫正。"
        ),
        "inputs": [
            "X 光机原始包（每个能量一份 × 通道）",
            "放射源原始包（Na22 / Cs137 / Co60）",
            "本底原始包（全局唯一一份，所有源共用）",
        ],
        "outputs": [
            "xray：<energy>keV_ch<ch>.l0.parquet（列：amp / channel / crc_check / utc / temp / bias）",
            "source：src_<source>_<min>m.l0.parquet（含该源全部能峰）",
            "background：src_bkg_<min>m.l0.parquet",
            "L0_manifest.json",
        ],
        "deps": [],
        "artifacts": [
            {"label": "L0 parquet", "kind": "data", "patterns": ["ec/L0/parquet/*.l0.parquet"]},
            {"label": "Manifest", "kind": "manifest", "patterns": ["ec/L0/json/*.json"]},
        ],
    },
    {
        "key": "EC_L1",
        "pipeline": "EC",
        "tag": "L1",
        "name": "EC L1 · 温偏矫正",
        "short": "温偏矫正",
        "purpose": (
            "用最新 TB L2 的 5 参数曲面把每个脉冲的 amp 校准到参考条件（T=25 ℃、V=28.5 V）："
            "amp_corr = amp · G(T_ref,V_ref) / G(T_pulse,V_pulse)；并在 schema metadata（key = tb_correction）"
            "记录所用 TB 版本与四通道参数，做完整溯源。"
        ),
        "inputs": [
            "EC L0 parquet（信号 + 本底）",
            "最新 TB L2 参数：<timestamp>_<payload>_TB.l2.json",
        ],
        "outputs": [
            "<L0 stem>_corr.l1.parquet（EC L0 全部列 + amp_corr；metadata：tb_correction）",
            "L1_manifest.json",
        ],
        "deps": ["EC_L0", "TB_L2"],
        "artifacts": [
            {"label": "矫正 parquet", "kind": "data", "patterns": ["ec/L1/parquet/*_corr.l1.parquet"]},
            {"label": "Manifest", "kind": "manifest", "patterns": ["ec/L1/json/*.json"]},
        ],
    },
    {
        "key": "EC_L2",
        "pipeline": "EC",
        "tag": "L2",
        "name": "EC L2 · 峰位拟合",
        "short": "峰位拟合",
        "purpose": (
            "对矫正后的谱做背景扣除（按计数率归一：N_src/T_src − N_bkg/T_bkg）+ 单高斯峰 + 单背景拟合，"
            "得到每个能量、每个通道的峰中心；源谱按 peak_energies_keV 做多能峰拟合。"
        ),
        "inputs": [
            "EC L1 parquet（信号 *_corr.l1.parquet）",
            "EC L1 本底（src_bkg_<min>m_corr.l1.parquet）",
            "手选窗（可选）：resources/ec_xray_windows_<payload>.json / ec_source_windows_<payload>.json",
        ],
        "outputs": [
            "<stem>_fit_results.l2.json（list[record]：xray 4 条 / 源 4×N峰 条）",
            "<stem>_fit.l2.png（每文件峰拟合校验图）",
            "L2_manifest.json",
        ],
        "deps": ["EC_L1"],
        "artifacts": [
            {"label": "拟合 JSON", "kind": "data", "patterns": ["ec/L2/json/*_fit_results.l2.json"]},
            {"label": "拟合图", "kind": "figure", "patterns": ["ec/L2/figures/*.l2.png"]},
            {"label": "Manifest", "kind": "manifest", "patterns": ["ec/L2/json/L2_manifest.json"]},
        ],
    },
    {
        "key": "EC_L3",
        "pipeline": "EC",
        "tag": "L3",
        "name": "EC L3 · 能量标定",
        "short": "能量标定",
        "purpose": (
            "汇总 EC L2 全部能量 / 通道的峰中心，**按通道独立**做 ADC↔keV 二次标定与分辨率曲线拟合 "
            "R(E)=√(a·E + b·E² + c)/E，得到每通道一组最终能量标定参数。"
        ),
        "inputs": [
            "EC L2 拟合 JSON（峰中心 + 对应物理能量 + 通道）",
        ],
        "outputs": [
            "<timestamp>_<payload>_ec_coef_ch<n>.l3.json（每通道一份）",
            "<timestamp>_<payload>_EC.l3.json（4 通道汇总）",
            "<timestamp>_<payload>_ecfit_ch<n>.l3.png（ADC–E 标定图）",
            "<timestamp>_<payload>_resolution_fit_ch<n>.l3.png（分辨率曲线）",
            "L3_manifest.json",
        ],
        "deps": ["EC_L2"],
        "artifacts": [
            {"label": "标定 JSON", "kind": "data", "patterns": ["ec/L3/json/*.l3.json"]},
            {"label": "标定图", "kind": "figure", "patterns": ["ec/L3/figures/*.l3.png"]},
            {"label": "Manifest", "kind": "manifest", "patterns": ["ec/L3/json/L3_manifest.json"]},
        ],
    },
]

LAYER_KEYS = [m["key"] for m in LAYER_META]

# 两条管线的卡片顺序（同时用于流程图布局）。
PIPELINES = [
    {"pipeline": "TB", "title": "TB 管线 · 温度-偏压增益标定", "keys": ["TB_L0", "TB_L1", "TB_L2"]},
    {"pipeline": "EC", "title": "EC 管线 · 能量标定", "keys": ["EC_L0", "EC_L1", "EC_L2", "EC_L3"]},
]

DEPENDENCY_NOTE = (
    "跨线依赖：TB L2 输出的 5 参数曲面（G0, k, V0, b, c）是 EC L1 温偏矫正的输入；"
    "EC L1 会在 parquet metadata 里记录所用 TB 时间戳，保证可溯源。"
)


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def _natural_key(text: str) -> List[Any]:
    """自然排序 key，让 40keV / 45keV、m10C / m20C 之类的名字按数值顺序排列。"""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def _rel_link(target: Path, start_dir: Path) -> str:
    """生成从 HTML 所在目录指向产物的相对链接（正斜杠 + URL 转义）。"""
    rel = os.path.relpath(target, start_dir)
    return quote(rel.replace(os.sep, "/"), safe="/:")


def scan_payload(payload_dir: Path, out_dir: Path) -> Dict[str, Any]:
    """扫描单个载荷目录，返回各层产物清单。"""
    layers: Dict[str, Any] = {}
    payload_total = 0

    for meta in LAYER_META:
        groups: List[Dict[str, Any]] = []
        layer_total = 0

        for spec in meta["artifacts"]:
            files: List[Path] = []
            seen: set[Path] = set()
            for pattern in spec["patterns"]:
                for path in payload_dir.glob(pattern):
                    if path.is_file() and path not in seen:
                        seen.add(path)
                        files.append(path)
            files.sort(key=lambda p: _natural_key(p.name))

            if files:
                groups.append(
                    {
                        "label": spec["label"],
                        "kind": spec["kind"],
                        "files": [
                            {"name": p.name, "rel": _rel_link(p, out_dir)} for p in files
                        ],
                    }
                )
                layer_total += len(files)

        payload_total += layer_total
        layers[meta["key"]] = {
            "artifacts": groups,
            "total": layer_total,
            "missing": layer_total == 0,
        }

    return {"layers": layers, "totalFiles": payload_total}


def build_data(
    products_root: Path,
    products_display: str,
    out_dir: Path,
    payloads: Sequence[str],
) -> Dict[str, Any]:
    """组装嵌入 HTML 的数据结构（全部来自真实扫描）。"""
    payload_data: Dict[str, Any] = {}
    for payload in payloads:
        payload_dir = products_root / payload
        scan = scan_payload(payload_dir, out_dir)
        payload_data[payload] = {
            "label": PAYLOAD_LABELS.get(payload, payload),
            "dir": f"{products_display}/{payload}",
            "exists": payload_dir.is_dir(),
            **scan,
        }

    layer_meta = [
        {
            "key": m["key"],
            "pipeline": m["pipeline"],
            "tag": m["tag"],
            "name": m["name"],
            "short": m["short"],
            "purpose": m["purpose"],
            "inputs": m["inputs"],
            "outputs": m["outputs"],
            "deps": m["deps"],
        }
        for m in LAYER_META
    ]

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "productsDir": products_display,
        "layerMeta": layer_meta,
        "pipelines": PIPELINES,
        "dependencyNote": DEPENDENCY_NOTE,
        "payloads": payload_data,
    }


# --------------------------------------------------------------------------- #
# HTML 渲染（原生 HTML/CSS/JS，数据内嵌）
# --------------------------------------------------------------------------- #
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GRID 标定全流程总览</title>
<style>
  :root{
    --bg:#eef2f7; --panel:#ffffff; --border:#dbe3ee; --text:#1e293b; --muted:#64748b;
    --tb:#2563eb; --tb-soft:#eef4ff; --ec:#7c3aed; --ec-soft:#f5f0ff;
    --ok:#0f766e; --miss:#94a3b8; --shadow:0 1px 3px rgba(15,23,42,.08);
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--bg); color:var(--text); font-size:14px; line-height:1.55;
    font-family:"Segoe UI","Microsoft YaHei","PingFang SC",system-ui,-apple-system,sans-serif;
  }
  header{
    display:flex; flex-wrap:wrap; gap:16px; align-items:center; justify-content:space-between;
    padding:18px 24px; background:linear-gradient(135deg,#1e3a8a,#4f46e5); color:#fff;
  }
  header h1{margin:0; font-size:20px; letter-spacing:.5px}
  header p{margin:4px 0 0; font-size:12.5px; opacity:.85}
  .head-right{text-align:right}
  .payload-switch{display:inline-flex; gap:8px}
  .pl-btn{
    cursor:pointer; border:1px solid rgba(255,255,255,.5); background:rgba(255,255,255,.12);
    color:#fff; padding:7px 16px; border-radius:999px; font-size:13.5px; transition:.15s;
  }
  .pl-btn:hover{background:rgba(255,255,255,.25)}
  .pl-btn.active{background:#fff; color:#1e3a8a; font-weight:600; border-color:#fff}
  #genInfo{margin-top:8px; font-size:12px; opacity:.85}
  .banner{
    margin:14px 24px 0; padding:10px 14px; border-radius:8px; font-size:12.5px;
    background:#fffbeb; border:1px solid #fde68a; color:#92400e;
  }
  main{
    display:grid; grid-template-columns:minmax(360px,1fr) minmax(440px,1.45fr);
    gap:18px; padding:18px 24px 28px; max-width:1560px; margin:0 auto; align-items:start;
  }
  @media (max-width:960px){ main{grid-template-columns:1fr} .flow-panel{position:static !important} }
  .panel{background:var(--panel); border:1px solid var(--border); border-radius:12px; box-shadow:var(--shadow); padding:16px 18px}
  .flow-panel{position:sticky; top:18px}
  .panel > h2{margin:0 0 14px; font-size:15px; color:var(--muted); font-weight:600; letter-spacing:.5px}
  .lane{margin-bottom:14px}
  .lane-title{font-size:13px; font-weight:700; margin-bottom:10px; padding-left:8px; border-left:4px solid var(--tb)}
  .lane.ec .lane-title{border-left-color:var(--ec)}
  .cards{display:flex; flex-wrap:wrap; align-items:center; gap:6px}
  .arrow{color:#94a3b8; font-size:18px; font-weight:700; user-select:none}
  .card{
    cursor:pointer; display:flex; flex-direction:column; gap:2px; align-items:flex-start;
    min-width:104px; padding:9px 12px; border-radius:10px; text-align:left;
    border:1.5px solid var(--border); background:#fff; font-family:inherit; color:inherit;
    transition:.15s; box-shadow:var(--shadow);
  }
  .card:hover{transform:translateY(-1px); border-color:#94a3b8}
  .lane.tb .card{border-top:3px solid var(--tb)}
  .lane.ec .card{border-top:3px solid var(--ec)}
  .lane.tb .card.selected{background:var(--tb-soft); border-color:var(--tb)}
  .lane.ec .card.selected{background:var(--ec-soft); border-color:var(--ec)}
  .card.missing{opacity:.72}
  .card-tag{font-size:11px; font-weight:700; letter-spacing:.5px; color:var(--muted)}
  .card-short{font-size:13px; font-weight:600}
  .card-badge{font-size:11.5px; color:var(--ok)}
  .card.missing .card-badge{color:var(--miss)}
  .dep-note{
    margin-top:6px; padding:9px 12px; border-radius:8px; font-size:12px; color:#5b21b6;
    background:var(--ec-soft); border:1px dashed #c4b5fd;
  }
  .detail-head{display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:6px}
  .detail-head h3{margin:0; font-size:17px}
  .pipe{font-size:11.5px; font-weight:700; padding:2px 9px; border-radius:999px}
  .pipe.tb{background:var(--tb-soft); color:var(--tb); border:1px solid #bfdbfe}
  .pipe.ec{background:var(--ec-soft); color:var(--ec); border:1px solid #ddd6fe}
  .purpose{margin:6px 0 14px; color:#334155}
  .io-grid{display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:14px}
  @media (max-width:640px){ .io-grid{grid-template-columns:1fr} }
  .io-box{background:#f8fafc; border:1px solid var(--border); border-radius:8px; padding:10px 12px}
  .io-box h4{margin:0 0 6px; font-size:12.5px; color:var(--muted)}
  .io-box ul{margin:0; padding-left:18px}
  .io-box li{font-size:12.5px; margin:3px 0}
  .chips{display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px}
  .chip{font-size:12px; background:#f1f5f9; border:1px solid var(--border); border-radius:999px; padding:3px 11px}
  .chip b{color:var(--ok)}
  .chip.total{background:#ecfdf5; border-color:#a7f3d0}
  .missing-box{
    padding:14px 16px; border-radius:8px; background:#f8fafc; border:1px dashed var(--miss);
    color:#64748b; font-size:13.5px; text-align:center;
  }
  .file-group{margin-bottom:14px}
  .file-group h4{margin:0 0 6px; font-size:13px}
  .file-list{
    list-style:none; margin:0; padding:6px; max-height:280px; overflow:auto;
    border:1px solid var(--border); border-radius:8px; background:#fcfdff;
  }
  .file-item{display:flex; align-items:center; gap:8px; padding:2px 4px; border-radius:6px}
  .file-item:hover{background:#eef4ff}
  .file-link{
    color:#1d4ed8; text-decoration:none; font-family:Consolas,"Courier New",monospace;
    font-size:12.5px; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .file-link:hover{text-decoration:underline}
  .preview-btn{
    cursor:pointer; font-size:11.5px; padding:1px 8px; border-radius:6px;
    border:1px solid var(--border); background:#fff; color:var(--ec); font-family:inherit;
  }
  .preview-btn:hover{background:var(--ec-soft)}
  .preview-box{margin-bottom:14px; border:1px solid var(--border); border-radius:10px; padding:12px; background:#fcfdff}
  .preview-box h4{margin:0 0 8px; font-size:13px; color:var(--muted)}
  .preview-box figure{margin:0; text-align:center}
  .preview-box img{max-width:100%; max-height:460px; border:1px solid var(--border); border-radius:6px; background:#fff}
  .preview-box figcaption{margin-top:6px; font-size:12px; color:var(--muted); font-family:Consolas,monospace; word-break:break-all}
  .muted{color:var(--muted)}
  footer{padding:16px 24px 34px; max-width:1560px; margin:0 auto; color:var(--muted); font-size:12px}
  footer code{background:#e2e8f0; padding:1px 6px; border-radius:4px}
</style>
</head>
<body>
<header>
  <div class="head-left">
    <h1>天格（GRID）载荷标定全流程总览</h1>
    <p>TB（温度-偏压增益）与 EC（能量标定）两线七层 · 产物浏览</p>
  </div>
  <div class="head-right">
    <div class="payload-switch" id="payloadSwitch"></div>
    <div id="genInfo"></div>
  </div>
</header>

<div class="banner">
  ⚠️ 页面内容来自对 <b>products/</b> 目录的实时扫描：<b>存在文件仅表示磁盘上有历史产物，
  不代表本次运行成功，也不代表内容通过 schema 校验</b>。请以各层 <code>L{n}_manifest.json</code>
  与 <code>cali_format/src/check/*.py</code> 的校验结果为准。
</div>

<main>
  <section class="panel flow-panel">
    <h2>流程总览（点击任一处理层查看详情）</h2>
    <div id="flow"></div>
  </section>
  <section class="panel detail-panel">
    <h2>层级详情</h2>
    <div id="detail"></div>
  </section>
</main>

<footer>
  <div>生成时间：<span id="footTime"></span> · 扫描范围：<span id="footScope"></span></div>
  <div style="margin-top:4px">本页为只读浏览视图，不修改任何标定产物；下钻改窗、重拟合等交互功能后续扩展。</div>
</footer>

<script>
const DATA = __DATA__;

const LAYER_BY_KEY = {};
DATA.layerMeta.forEach(function (m) { LAYER_BY_KEY[m.key] = m; });

const state = { payload: Object.keys(DATA.payloads)[0], layer: null };

function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function payloadData() { return DATA.payloads[state.payload]; }
function defaultLayer() {
  const pd = payloadData();
  for (const key of Object.keys(pd.layers)) {
    if (!pd.layers[key].missing) return key;
  }
  return DATA.layerMeta[0].key;
}

function renderHeader() {
  const box = document.getElementById("payloadSwitch");
  box.innerHTML = Object.keys(DATA.payloads).map(function (k) {
    const v = DATA.payloads[k];
    const active = k === state.payload ? " active" : "";
    return '<button class="pl-btn' + active + '" data-payload="' + esc(k) + '">' + esc(v.label) + "</button>";
  }).join("");
  document.getElementById("genInfo").textContent =
    "扫描目录：" + DATA.productsDir + " · 生成于 " + DATA.generatedAt;
  document.getElementById("footTime").textContent = DATA.generatedAt;
  document.getElementById("footScope").textContent =
    Object.keys(DATA.payloads).map(function (k) { return DATA.payloads[k].dir; }).join("、");
}

function cardHtml(key) {
  const meta = LAYER_BY_KEY[key];
  const data = payloadData().layers[key];
  const cls = ["card"];
  if (key === state.layer) cls.push("selected");
  if (data.missing) cls.push("missing");
  const badge = data.missing ? "缺少产物" : data.total + " 份";
  return '<button class="' + cls.join(" ") + '" data-key="' + esc(key) + '">' +
    '<span class="card-tag">' + esc(meta.tag) + "</span>" +
    '<span class="card-short">' + esc(meta.short) + "</span>" +
    '<span class="card-badge">' + esc(badge) + "</span>" +
    "</button>";
}

function renderFlow() {
  let html = "";
  DATA.pipelines.forEach(function (lane) {
    const cls = lane.pipeline.toLowerCase();
    html += '<div class="lane ' + cls + '">';
    html += '<div class="lane-title">' + esc(lane.title) + "</div>";
    html += '<div class="cards">';
    lane.keys.forEach(function (key, i) {
      if (i > 0) html += '<span class="arrow">&rarr;</span>';
      html += cardHtml(key);
    });
    html += "</div></div>";
  });
  html += '<div class="dep-note">' + esc(DATA.dependencyNote) + "</div>";
  document.getElementById("flow").innerHTML = html;
}

function renderDetail() {
  const host = document.getElementById("detail");
  const meta = LAYER_BY_KEY[state.layer];
  if (!meta) {
    host.innerHTML = '<p class="muted">请在左侧流程图选择任一处理层。</p>';
    return;
  }
  const pd = payloadData();
  const data = pd.layers[state.layer];
  const pipe = meta.pipeline.toLowerCase();
  let html = "";

  html += '<div class="detail-head"><h3>' + esc(meta.name) + "</h3>" +
    '<span class="pipe ' + pipe + '">' + esc(meta.pipeline) + "</span>" +
    '<span class="muted" style="font-size:12px">' + esc(pd.dir) + "</span></div>";
  html += '<p class="purpose">' + esc(meta.purpose) + "</p>";

  html += '<div class="io-grid">' +
    '<div class="io-box"><h4>输入</h4><ul>' +
    meta.inputs.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") +
    "</ul></div>" +
    '<div class="io-box"><h4>输出</h4><ul>' +
    meta.outputs.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") +
    "</ul></div></div>";

  if (data.missing) {
    html += '<div class="missing-box">缺少产物：在 <b>' + esc(pd.dir) + "</b> 下未找到本层任何产物文件。</div>";
  } else {
    html += '<div class="preview-box" id="previewBox" hidden><h4>图像预览</h4>' +
      '<figure><img id="previewImg" alt=""><figcaption id="previewCap"></figcaption></figure></div>';

    const chips = data.artifacts.map(function (g) {
      return '<span class="chip">' + esc(g.label) + " <b>" + g.files.length + "</b></span>";
    }).join("");
    html += '<div class="chips">' + chips +
      '<span class="chip total">合计 <b>' + data.total + "</b></span></div>";

    data.artifacts.forEach(function (g) {
      html += '<div class="file-group"><h4>' + esc(g.label) +
        ' <span class="muted">(' + g.files.length + ")</span></h4>";
      html += '<ul class="file-list">';
      g.files.forEach(function (f) {
        html += '<li class="file-item">' +
          '<a class="file-link" href="' + esc(f.rel) + '" target="_blank" rel="noopener" title="打开 ' +
          esc(f.name) + '">' + esc(f.name) + "</a>";
        if (g.kind === "figure") {
          html += '<button class="preview-btn" data-preview="' + esc(f.rel) + '" data-name="' +
            esc(f.name) + '">预览</button>';
        }
        html += "</li>";
      });
      html += "</ul></div>";
    });
  }
  host.innerHTML = html;
}

function showPreview(rel, name) {
  const box = document.getElementById("previewBox");
  if (!box) return;
  document.getElementById("previewImg").src = rel;
  document.getElementById("previewCap").textContent = name;
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

function render() {
  renderFlow();
  renderDetail();
}

document.addEventListener("click", function (e) {
  const pl = e.target.closest(".pl-btn");
  if (pl && pl.dataset.payload) {
    state.payload = pl.dataset.payload;
    if (!state.layer || !payloadData().layers[state.layer]) state.layer = defaultLayer();
    renderHeader();
    render();
    return;
  }
  const card = e.target.closest(".card");
  if (card && card.dataset.key) {
    state.layer = card.dataset.key;
    render();
    return;
  }
  const pv = e.target.closest(".preview-btn");
  if (pv) {
    e.preventDefault();
    showPreview(pv.dataset.preview, pv.dataset.name);
  }
});

state.layer = defaultLayer();
renderHeader();
render();
</script>
</body>
</html>
"""


def render_html(data: Dict[str, Any]) -> str:
    """把扫描数据注入 HTML 模板，返回完整页面字符串。"""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__DATA__", payload)


# --------------------------------------------------------------------------- #
# 链接自检
# --------------------------------------------------------------------------- #
def check_links(data: Dict[str, Any], out_dir: Path) -> int:
    """校验每条内嵌链接都能解析到真实文件，返回失败数量。"""
    checked = 0
    broken = 0
    for payload, pdata in data["payloads"].items():
        for layer_key, ldata in pdata["layers"].items():
            for group in ldata["artifacts"]:
                for f in group["files"]:
                    checked += 1
                    if not (out_dir / f["rel"]).resolve().is_file():
                        broken += 1
                        print(f"  ✗ 链接失效 [{payload}/{layer_key}] {f['name']} -> {f['rel']}")
    print(f"链接自检：{checked} 条，{checked - broken} 条有效 / {broken} 条失效")
    return broken


def print_summary(data: Dict[str, Any], out_path: Path) -> None:
    """控制台打印扫描摘要。"""
    print("=" * 66)
    print("标定全流程总览 · 扫描摘要")
    print("=" * 66)
    for payload, pdata in data["payloads"].items():
        state = "" if pdata["exists"] else "  ⚠ 目录不存在"
        print(f"\n[{payload}] {pdata['dir']}  共 {pdata['totalFiles']} 个产物文件{state}")
        for meta in LAYER_META:
            ldata = pdata["layers"][meta["key"]]
            if ldata["missing"]:
                detail = "缺少产物"
            else:
                detail = " / ".join(
                    f"{g['label']} {len(g['files'])}" for g in ldata["artifacts"]
                )
            print(f"   {meta['key']:6s} {ldata['total']:>4d} 份   {detail}")
    print(f"\n已生成：{out_path}")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="扫描 products/<payload>/ 下 TB / EC 各层产物，生成自包含 HTML 总览页。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--products", default=DEFAULT_PRODUCTS, help="产物根目录（相对仓库根或绝对路径）")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出 HTML 路径（相对仓库根或绝对路径）")
    parser.add_argument(
        "--payloads",
        default="14B,15B",
        help="要扫描的载荷，逗号分隔",
    )
    parser.add_argument("--check", action="store_true", help="生成后校验所有内嵌链接是否指向真实文件")
    return parser.parse_args(argv)


def _resolve(path_str: str) -> Path:
    """相对路径按仓库根解析；绝对路径原样使用。"""
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _force_utf8_stdout() -> None:
    """让中文摘要能在 GBK/CP1252 等非 UTF-8 控制台上正常打印（Windows 常见）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdout()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    products_root = _resolve(args.products)
    out_path = _resolve(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir = out_path.parent
    payloads = [p.strip() for p in args.payloads.split(",") if p.strip()]

    if not products_root.is_dir():
        print(f"⚠ 产物根目录不存在：{products_root}（将生成缺少产物的页面）")

    data = build_data(products_root, args.products, out_dir, payloads)
    out_path.write_text(render_html(data), encoding="utf-8")

    print_summary(data, out_path)

    if args.check:
        print()
        broken = check_links(data, out_dir)
        return 1 if broken else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

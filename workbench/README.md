# GRID Calibration Workbench

这是现有 lib 的本地复核入口，直接调用七个处理器。计算模型、峰拟合和曲面拟合仍由 `tb/processors`、`ec/processors` 提供。服务只绑定 `127.0.0.1`，无需云服务、Node 构建或外部图表 CDN。

## 启动与操作

从工作区（仓库的上一级）运行：

```powershell
calibration_lib/.venv/Scripts/python.exe -X utf8 -m calibration_lib.workbench.server --workspace . --port 8765
```

浏览器访问 `http://127.0.0.1:8765`。已安装 wheel 的用户也可使用 `grid-workbench --workspace <数据工作区> --port 8765`；工作区需包含原始数据及既有分层 products。服务同时使用工作区锁，重复启动不能中断正在运行的任务。

1. 新建 14B 或 15B 试算；初始曲面和能峰数据来自明确导入的历史 JSON，状态为“未运行”。
2. 点击“运行代表闭环”，依次真正执行 TB L0–L2、EC L0–L3。也可通过左侧导航单独执行任一层。选择“全部数据”会扩大该层范围，可能需要较长时间。
3. 在 TB 页面切换通道，点击曲面测量点或残差点，查看对应 stem、实测 HK 温度/偏压、真实计数谱、拟合曲线和参数。名义温度不代替 HK 实测温度。
4. 修改 ADC 窗口，点击“保存窗口并拟合”。原 L1 处理器重跑该工况的四通道，当前通道使用修订窗；资源目录中的手选窗口文件保持只读。
5. 屏蔽/恢复只改变参与曲面拟合的点，不删除能谱。点击“重新拟合 TB”，通过主图下方“拟合残差 / 参数比较”页签查看新旧结果。选中工况在曲面和残差中同步高亮；文件及日志在底部“产物与运行记录”中展开。多工况修订必须全部完成 L1 后才能拟合 TB。
6. TB 改变后 EC 显示依赖待更新。运行 EC L1 固定新的 TB 文件和 SHA-256，再更新 L2/L3。全量 EC L2 需先完成全量 EC L1。
7. 试算选择框可重开已有运行；修订、结果和记录从磁盘恢复。页面刷新可恢复对正在执行任务的跟踪。服务意外停止后，未完成任务标为中断失败，可重新提交。

界面按流程导航、主分析图和工况详情组织。窗口控件显示三位小数，悬停显示完整边界；没有编辑时保存和拟合继续使用原始完整数值。辅助说明、输入输出依赖和日志按需展开，处理状态与规范校验仍分别显示。

## 范围与状态

默认 TB 代表样例为一个实际 raw 工况（14B `0C_270_040`，15B `m10C-265-129`）；EC 代表样例为一个四通道完整能量（14B 70 keV，15B 45 keV）。选择以当前文件和历史质量记录为依据，数据集变化时可重新选取。

TB L2 使用当前完整 L1 JSON 快照，EC L3 使用当前完整 L2 JSON 快照。代表范围之外的点继承历史，页面与逐文件依赖表会明确标注。EC L0/L1 展示实际 parquet 直方图；显示范围外的事件另列数量，不从数据中删除。L2/L3 可查看原处理器生成的图与 JSON。

四种信息独立：历史文件存在、计算成功/失败、规范校验结果、修订及依赖待更新。成功依据本次真实处理器返回、输出是否生成与异常记录，不能用历史文件或 CLI 退出码替代。失败尝试的部分输出保留在独立 job 目录，不覆盖当前成功结果。

## 持久化与溯源

```text
runs/<run_id>/
  state.json                       # 配置、版本、任务状态、逐能峰依赖
  imports.json                     # 历史 JSON 来源、大小和 SHA-256
  baseline/<payload>/...           # 不变的初始 JSON 快照
  products/<payload>/...           # 已成功计算的当前产物与继承 JSON
  revisions/0001.json              # 每次保存的配置、理由与时间
  jobs/<job_id>/
    config.json / job.json         # 不可变尝试配置与运行记录
    processor.log / error.txt      # 实际 stdout/stderr 或异常原因
    result.json / validation.json  # 处理器结果与独立检查结果
    products/<payload>/...        # 此次尝试的输入和输出，失败也保留
    workbench-fit-windows.json     # 仅本次 L1 使用的窗口
```

EC L1 的 parquet 继续按规范保存 `tb_correction`；额外 SHA-256 和人工修订存在旁置记录，不向严格 TB 11 字段 JSON 添加字段。每个 EC L2 文件分别记录 TB 来源，EC L3 记录输入文件哈希和混合来源，避免将所有历史能峰归属于本次 TB。

## 已知科学限制

- 代表 raw 解析中的 `crc_check` 为 false。现行 `cali_format` 检查器拒绝这些 L0，界面明确保留失败。原因尚需进一步调查，不能把计算成功当作数据质量通过。
- 现行 EC L3 实际拟合全局 **E=f(ADC)**，low/high 共用系数，非单调时回退线性；wiki 写的是分段 **ADC=f(E)**。本版保持既有物理算法，字段校验通过不代表拟合方向符合 wiki。系数用于外部下游前必须解决这一约定差异。
- 部分分辨率段的可用点不足，返回 null 系数与误差，并提供提示。历史产物也有缺通道或旧格式，不能默认历史全部合格。
- 单服务串行执行科学处理，防止 Matplotlib/旧解析器的全局状态竞争。尚无任务取消、独立 worker 进程、多人访问与正式算法精度评估。

## 验证

```powershell
# 工作区根目录：自动回归
calibration_lib/.venv/Scripts/python.exe -X utf8 -m pytest calibration_lib/tests -q

# 服务已启动时：创建真实代表试算，逐层执行并保存 acceptance.json
calibration_lib/.venv/Scripts/python.exe -X utf8 calibration_lib/tools/workbench_smoke.py --workspace . --payload 15B
```

自动验收同时比较 raw、products、cali_format、wiki 的文件大小和修改时间，检查运行没有回写受保护输入。规范校验直接只读调用工作区 `cali_format/src/check/<layer>.py`，并记录检查器 SHA-256；未找到检查器时显示不可用，不宣称通过。

真实验收结果保存在对应试算的 `acceptance.json`。测试覆盖持久化、实际峰拟合、错误隔离、HTTP、路径约束、依赖更新、CRC 保留和稀疏 L3 输出。

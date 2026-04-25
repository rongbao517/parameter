# UAM 参数敏感性分析：完整实施计划

---

## 一、参数配置方案（最终确认版）

### 1.1 参数分组

**A 组 — Cost（成本类）**

| 参数 | 含义 | 基准值 | 测试值（含基准） | 非基准点数 |
|------|------|--------|------------------|-----------|
| `price_ground` | 地面运营成本 $/km | 5.0 | [3.0, **5.0**, 7.0] | 2 |
| `price_air` | 空中运营成本 $/km | 7.0 | [5.0, **7.0**, 9.0, 11.0] | 3 |
| `VOT` | 乘客时间价值 $/h | 20.0 | [10.0, **20.0**, 30.0, 40.0] | 3 |

**B 组 — Speed（速度类）**

| 参数 | 含义 | 基准值 | 测试值（含基准） | 非基准点数 |
|------|------|--------|------------------|-----------|
| `speed_ground` | 地面行驶速度 km/h | 18.0 | [12.0, **18.0**, 25.0] | 2 |
| `speed_air` | eVTOL 巡航速度 km/h | 150.0 | [100.0, **150.0**, 200.0] | 2 |

**C 组 — Grid Size（空间尺度类）**

| 参数 | 含义 | 基准值 | 测试值（含基准） | 非基准点数 |
|------|------|--------|------------------|-----------|
| `GRID_CELL_SIZE_KM` | 网格单元边长 km | 1.5 | [0.75, **1.5**, 2.25] | 2 |

**固定校准参数（不进入 SENSITIVITY_VALUES）**

| 参数 | 固定值 | 合理性依据 |
|------|--------|-----------|
| `MIN_AIR_DISTANCE` | 6.0 km | 150 km/h 飞 6km 约 2.4 min vs 地面 20 min，时间优势存在 |
| `MAX_AIR_DISTANCE` | 60.0 km | SF 湾区最大跨度约 30km，60km 充分覆盖 |

**不纳入本次分析（从 `SENSITIVITY_VALUES` 移除）**

- `SEAT_CAPACITY`：容量研究属于运营侧分析，与参数敏感性研究目标不同
- `DEFAULT_DEPARTURE_CAPACITY` / `DEFAULT_ARRIVAL_CAPACITY`：同上
- `MIN_AIR_DISTANCE` / `MAX_AIR_DISTANCE`：固定校准参数

### 1.2 场景总数

| 来源 | 数量 |
|------|------|
| 基准场景 | 1 |
| Cost A 组非基准 | 2 + 3 + 3 = 8 |
| Speed B 组非基准 | 2 + 2 = 4 |
| Grid C 组非基准 | 2 |
| **总计** | **15 场景** |

每场景 50 batch（500 时间片 / batch_size=10），共 **750 次 Gurobi 求解**。

---

## 二、需要对 parameter.py 做的修改

### 2.1 `SENSITIVITY_VALUES` 修改

```
当前需要修改的项目：
1. price_air：添加 11.0 → [5.0, 7.0, 9.0, 11.0]
2. GRID_CELL_SIZE_KM：新增到 SENSITIVITY_VALUES → [0.75, 1.5, 2.25]
3. 移除：MIN_AIR_DISTANCE, MAX_AIR_DISTANCE, SEAT_CAPACITY,
         DEFAULT_DEPARTURE_CAPACITY, DEFAULT_ARRIVAL_CAPACITY
4. VOT 保留：[10.0, 20.0, 30.0, 40.0]（当前已存在）
5. price_ground 保留：[3.0, 5.0, 7.0]（当前已存在）
6. speed_ground 保留：[12.0, 18.0, 25.0]（当前已存在）
7. speed_air 保留：[100.0, 150.0, 200.0]（当前已存在）
```

### 2.2 `SCENARIO_SUMMARY_COLUMNS` 需要确认包含的字段

当前已有字段全部保留，无需新增输出列。

---

## 三、输出数据结构规范

### 3.1 主分析文件：`scenario_summary.csv`

**位置**：`sensitivity_results_<timestamp>/scenario_summary.csv`
**行数**：15 行（每行一个场景）
**列定义**：

| 列名 | 类型 | 说明 |
|------|------|------|
| `Scenario` | str | 场景名称，如 "baseline", "price_air_9p0" |
| `ChangedParameter` | str | 变化的参数名，baseline 为 "none" |
| `ChangedValue` | any | 变化的参数值，baseline 为 "baseline" |
| `CSVFile` | str | 使用的停机坪文件名 |
| `TotalObjective` | float | 系统总广义成本（美元），所有 batch 目标函数之和 |
| `TotalDemand` | float | 总出行需求（人次） |
| `TotalServed` | float | UAM 服务人次（≈ TotalDemand，因惩罚机制） |
| `TotalUnserved` | float | 未服务人次（预计接近 0） |
| `ServiceRate` | float | 服务率 = TotalServed / TotalDemand |
| `AvgGroundStartKM` | float | 加权平均出发接驳距离（km），权重为 AssignedFlow |
| `AvgAirKM` | float | 加权平均空中段距离（km） |
| `AvgGroundEndKM` | float | 加权平均到达接驳距离（km） |
| `AvgTotalTravelKM` | float | = AvgGroundStartKM + AvgAirKM + AvgGroundEndKM |
| `UsedVertiports` | int | 实际启用停机坪数量（出发+到达去重） |
| `UsedRoutes` | int | 实际启用航线数量（停机坪对数） |
| `TotalFlights` | float | 总飞行架次 |
| `AverageLoadFactor` | float | 平均座位利用率 = 总客流 / (总架次 × 座位数) |
| `OptimalBatches` | int | 达到最优解的 batch 数量 |
| `TimeLimitBatches` | int | 触发时间限制的 batch 数量 |
| `NoSolutionBatches` | int | 无可行解的 batch 数量 |

### 3.2 辅助分析文件（每场景各一份）

**`batch_summary_<csv_stem>_<scenario_safe>.csv`**（用于验证和 NumValidArcs 聚合）

| 列名 | 类型 | 说明 |
|------|------|------|
| `Scenario` | str | |
| `ChangedParameter` | str | |
| `ChangedValue` | any | |
| `CSVFile` | str | |
| `BatchIndex` | int | Batch 编号（1-50） |
| `ModelStatus` | str | OPTIMAL / TIME_LIMIT / 其他 |
| `HasSolution` | bool | |
| `BatchObjective` | float | |
| `MIPGap` | float | |
| `RuntimeSeconds` | float | |
| `NumOrders` | int | 该 batch 中的订单数 |
| `NumValidArcs` | int | 该场景的可行空中航线数（场景内所有 batch 相同） |
| `NumXVars` | int | 决策变量 x 的数量 |
| `NumZVars` | int | 决策变量 z 的数量 |
| `BatchDemand` | int | |
| `BatchServed` | float | |
| `BatchUnserved` | float | |
| `BatchServiceRate` | float | |

**用途**：从此文件提取每场景的 `NumValidArcs`（取 batch 中的代表值），供验证空中距离校准合理性使用。

### 3.3 可视化脚本的输入数据结构（处理后）

可视化脚本从 `scenario_summary.csv` 派生以下分析用 DataFrame：

```python
# 主表：每行一个场景
# df_summary 列 = scenario_summary.csv 所有列 + 以下派生列：
#   "RelObjective"     = (TotalObjective - baseline_obj) / baseline_obj * 100  # % 变化
#   "Group"            = "Cost" / "Speed" / "Grid" / "Baseline"
#   "AvgGroundTotalKM" = AvgGroundStartKM + AvgGroundEndKM                     # 总接驳距离

# 基准值查询
baseline_row = df_summary[df_summary["ChangedParameter"] == "none"].iloc[0]

# group_mapping
group_mapping = {
    "price_ground":       "Cost",
    "price_air":          "Cost",
    "VOT":                "Cost",
    "speed_ground":       "Speed",
    "speed_air":          "Speed",
    "GRID_CELL_SIZE_KM":  "Grid",
    "none":               "Baseline",
}
```

---

## 四、可视化分析计划

### 4.1 可视化脚本结构

- **新建文件**：`sensitivity_viz.py`
- **输入**：`sensitivity_results_<timestamp>/scenario_summary.csv`（运行时通过命令行参数或 glob 最新目录获取）
- **输出目录**：`sensitivity_results_<timestamp>/figures/`
- **依赖库**：`matplotlib`, `pandas`, `numpy`, `seaborn`（可选）

### 4.2 图表清单（共 8 张图）

---

#### 图 1：龙卷风图（全参数敏感性总览）

**文件名**：`fig01_tornado_total_objective.png`
**类型**：水平条形图（tornado chart）
**数据来源**：`scenario_summary.csv`，每参数取极端值中 `|RelObjective|` 最大的一个点

```
X 轴：TotalObjective 相对基准的百分比变化（%），中心线 = 0
Y 轴：参数名（按绝对影响量从大到小排序）
每条 bar：左侧为参数最低值对应变化，右侧为参数最高值对应变化
颜色编码：A 组红色，B 组蓝色，C 组绿色
参考线：x = 0（基准）
标注：每 bar 末端标注具体 % 数值
```

**分析目的**：一眼看出哪个参数对系统总成本影响最大。

---

#### 图 2：成本组（A 组）敏感性折线图

**文件名**：`fig02_cost_group_objective.png`
**类型**：1×3 子图（subplot），每子图一条折线

```
子图 2a：price_ground sensitivity
  X 轴：price_ground 值 [3.0, 5.0, 7.0]
  Y 轴：TotalObjective（美元）
  数据点：3 个（含基准，标注 ★）

子图 2b：price_air sensitivity
  X 轴：price_air 值 [5.0, 7.0, 9.0, 11.0]
  Y 轴：TotalObjective（美元）
  数据点：4 个

子图 2c：VOT sensitivity
  X 轴：VOT 值 [10.0, 20.0, 30.0, 40.0]
  Y 轴：TotalObjective（美元）
  数据点：4 个

公共样式：基准点用实心圆标注，非基准用空心圆；基准值处画竖虚线
```

---

#### 图 3：速度组（B 组）敏感性折线图

**文件名**：`fig03_speed_group_objective.png`
**类型**：1×2 子图

```
子图 3a：speed_ground sensitivity
  X 轴：speed_ground [12.0, 18.0, 25.0]
  Y 轴：TotalObjective（美元）

子图 3b：speed_air sensitivity
  X 轴：speed_air [100.0, 150.0, 200.0]
  Y 轴：TotalObjective（美元）
```

---

#### 图 4：网格尺寸组（C 组）—— 双 Y 轴分析

**文件名**：`fig04_grid_size_analysis.png`
**类型**：单图，双 Y 轴

```
X 轴：GRID_CELL_SIZE_KM [0.75, 1.5, 2.25]
左 Y 轴（蓝色）：TotalObjective（美元）
右 Y 轴（橙色）：AvgGroundTotalKM（= AvgGroundStartKM + AvgGroundEndKM，km）

两条折线：系统总成本 vs 平均接驳总距离
目的：验证 grid size 通过地面距离等比放大影响系统成本，两条线应同向变化
```

---

#### 图 5：出行距离构成堆叠条形图

**文件名**：`fig05_distance_breakdown_stacked.png`
**类型**：分组堆叠条形图

```
X 轴：所有 15 个场景（x 轴标签 = ChangedParameter + ChangedValue，旋转 45°）
Y 轴：平均出行距离（km）
堆叠层：
  - 底层（蓝）：AvgGroundStartKM
  - 中层（橙）：AvgAirKM
  - 顶层（绿）：AvgGroundEndKM
基准场景用黑色边框高亮
图例标注三段含义
右侧辅助 Y 轴：可选标注 AvgTotalTravelKM 折线
```

**分析目的**：观察参数变化时，路线结构（接驳 vs 空中段比例）是否改变。

---

#### 图 6：停机坪与航线利用率

**文件名**：`fig06_vertiport_route_utilization.png`
**类型**：3×1 子图（每组一个），每子图双折线

```
每个子图：
  X 轴：当组参数值
  左 Y 轴：UsedVertiports（实线）
  右 Y 轴：UsedRoutes（虚线）

子图 6a：A 组（price_ground, price_air, VOT 各一条折线组）
子图 6b：B 组（speed_ground, speed_air）
子图 6c：C 组（GRID_CELL_SIZE_KM）
```

**分析目的**：参数变化是否导致网络拓扑变化（更多/更少停机坪启用）。

---

#### 图 7：载客率（Load Factor）敏感性

**文件名**：`fig07_load_factor_sensitivity.png`
**类型**：3×1 子图（每组一个），折线图

```
X 轴：参数值（各组分别）
Y 轴：AverageLoadFactor（0~1）
参考线：y = 0.25（25% 为低效基准，可调）

子图 7a：A 组各参数（price_ground, price_air, VOT）
子图 7b：B 组各参数（speed_ground, speed_air）
子图 7c：C 组（GRID_CELL_SIZE_KM）
```

**分析目的**：成本/速度参数变化对 UAM 座位利用效率的影响。

---

#### 图 8：空中距离校准验证图（辅助图）

**文件名**：`fig08_air_distance_calibration.png`
**类型**：直方图 + 累积分布

```
数据来源：从任一 assignments 文件提取 AirKM 列（或从 routes 文件）
图形：
  左子图：AirKM 的频率直方图（bins=20）
  右子图：AirKM 的累积分布（CDF）
标注：
  - 竖线标注 MIN_AIR_DISTANCE = 6.0 km（红色虚线）
  - 竖线标注 MAX_AIR_DISTANCE = 60.0 km（红色虚线）
  - 标注落在 [6, 60] 范围内的比例
```

**分析目的**：验证 min/max 设定合理，大多数实际使用航线落在此区间内。

---

### 4.3 可视化脚本逻辑流程

```
sensitivity_viz.py
│
├── 0. 读取参数
│   └── 从命令行或 glob 获取 sensitivity_results_* 目录路径
│
├── 1. 加载数据
│   ├── df = pd.read_csv(scenario_summary.csv)
│   └── df_batch = 合并所有 batch_summary_*.csv（用于 NumValidArcs）
│
├── 2. 数据预处理
│   ├── 提取 baseline_obj = df[df.ChangedParameter=="none"].TotalObjective.values[0]
│   ├── df["RelObjective"] = (df.TotalObjective - baseline_obj) / baseline_obj * 100
│   ├── df["AvgGroundTotalKM"] = df.AvgGroundStartKM + df.AvgGroundEndKM
│   └── df["Group"] = df.ChangedParameter.map(group_mapping)
│
├── 3. 生成各图（fig01~fig08）
│   └── 每张图：plt.figure → plot → 保存至 figures/<figname>.png，dpi=150
│
└── 4. 打印汇总表
    └── 打印关键指标对比表（ChangedParameter, ChangedValue, TotalObjective,
        RelObjective%, AvgAirKM, UsedVertiports, AverageLoadFactor）
        到终端 / 保存为 sensitivity_table.csv
```

---

## 五、执行顺序

```
步骤 1：修改 parameter.py
        → 更新 SENSITIVITY_VALUES（添加 GRID_CELL_SIZE_KM，调整 price_air，移除非必要参数）

步骤 2：运行 parameter.py
        → 生成 sensitivity_results_<timestamp>/ 目录及所有 CSV

步骤 3：新建并运行 sensitivity_viz.py
        → 传入 sensitivity_results_<timestamp>/ 路径
        → 输出 figures/fig01~fig08.png + sensitivity_table.csv

步骤 4：审查输出
        → 检查 NoSolutionBatches 是否为 0（如非零，说明某场景下容量约束过紧）
        → 审查 fig08 验证空中距离区间合理性
```

---

## 六、最脆弱的假设

**本 plan 假设**：所有 15 场景下 `NoSolutionBatches == 0`（模型总有可行解）。若 `GRID_CELL_SIZE_KM = 2.25` 时地面距离过大，导致某些接驳成本异常高但又必须服务（惩罚机制），Gurobi 可能触发 `TIME_LIMIT` 而非 `OPTIMAL`，这不影响 plan 结构，但需在结果分析中标注哪些场景为非最优解，并说明对 TotalObjective 的影响方向（TIME_LIMIT 解偏高，因此是保守估计）。

---

## 七、模型行为说明

`parameter.py` 使用 `UNSERVED_PENALTY = 10000.0`，实际效果是所有需求都被强制分配到 UAM 路线——模型永远不会选择"放弃"一个乘客，而是一定找到一条 UAM 路线服务它。

因此本次敏感性分析体现的是：
- **不同参数下 UAM 路线选择**（选哪对停机坪）如何变化
- **系统总广义成本**如何随参数变化
- **网络拓扑**（启用停机坪/航线数量）和**座位利用率**如何变化

不体现：乘客在 UAM 和纯地面之间的模态选择（此问题需要 `uam_single_factor_generalized_cost.py` 的框架）。

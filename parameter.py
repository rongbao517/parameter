import numpy as np
import pandas as pd
from gurobipy import Model, GRB, quicksum
import math
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import json
import traceback


# ============================================================
# 1. 基础文件路径
# ============================================================

FLOW_FILE = "6kmodflow_corrected.npz"
CSV_FILES = [
    "candidate_vertiports_SF_k9.csv"
]

OUTPUT_ROOT = Path(f"sensitivity_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


# ============================================================
# 2. 基础运行设置
# ============================================================

MAX_TIME_INTERVALS = 500
BATCH_SIZE = 10
GRID_WIDTH = 52

# 精确模型：None 表示每个订单都可以选择所有可行航线
# 为了先跑通，也可以改成 30 或 50，表示每个订单只保留成本最低的前 N 条航线
MAX_ARCS_PER_ORDER = None

# Gurobi 求解控制
TIME_LIMIT = 600
MIP_GAP = 0.01
OUTPUT_FLAG = 1


# ============================================================
# 3. 基准参数
# ============================================================

BASE_PARAMS = {
    # 成本参数
    "price_ground": 5.0,       # 美元/km
    "price_air": 7.0,          # 美元/km
    "VOT": 20.0,               # 美元/hour

    # 速度参数
    "speed_ground": 18.0,      # km/hour
    "speed_air": 150.0,        # km/hour

    # 地面网格尺寸
    "GRID_CELL_SIZE_KM": 1.5,  # 你的原始数据是 1.5km * 1.5km

    # eVTOL 运营参数
    "SEAT_CAPACITY": 4,        # 每架飞机座位数

    # 空中距离限制
    "MIN_AIR_DISTANCE": 6.0,   # km，小于该距离不使用 UAM
    "MAX_AIR_DISTANCE": 60.0,  # km，超过该距离认为航程不合适

    # 停机坪容量，单位：架次 / 时间片
    "DEFAULT_DEPARTURE_CAPACITY": 20,
    "DEFAULT_ARRIVAL_CAPACITY": 20,

    # 未服务惩罚，单位：美元 / 人
    # 应该明显大于正常服务一个乘客的成本，否则模型会主动放弃高成本订单
    "UNSERVED_PENALTY": 10000.0,

    # 很小的架次数惩罚，只用于避免 z 变量无意义地变大
    # 这个值很小，通常只影响同成本解之间的选择
    "EPS_FLIGHT_PENALTY": 1e-3,
}


# ============================================================
# 4. 敏感性分析设置
#    这里采用 one-at-a-time，一次只改变一个参数
# ============================================================

SENSITIVITY_VALUES = {
    "VOT": [10.0, 20.0, 30.0, 40.0],
    "price_ground": [3.0, 5.0, 7.0],
    "price_air": [5.0, 7.0, 9.0],
    "speed_ground": [12.0, 18.0, 25.0],
    "speed_air": [100.0, 150.0, 200.0],
    "MIN_AIR_DISTANCE": [3.0, 6.0, 9.0],
    "MAX_AIR_DISTANCE": [40.0, 60.0, 80.0],
    "SEAT_CAPACITY": [2, 4, 6],
    "DEFAULT_DEPARTURE_CAPACITY": [10, 20, 40],
    "DEFAULT_ARRIVAL_CAPACITY": [10, 20, 40],
}


def build_scenarios(base_params, sensitivity_values):
    scenarios = []

    baseline = base_params.copy()
    baseline["scenario_name"] = "baseline"
    baseline["changed_parameter"] = "none"
    baseline["changed_value"] = "baseline"
    scenarios.append(baseline)

    for param_name, values in sensitivity_values.items():
        base_value = base_params[param_name]
        for value in values:
            if value == base_value:
                continue

            scenario = base_params.copy()
            scenario[param_name] = value
            scenario["scenario_name"] = f"{param_name}_{str(value).replace('.', 'p')}"
            scenario["changed_parameter"] = param_name
            scenario["changed_value"] = value
            scenarios.append(scenario)

    return scenarios


SCENARIOS = build_scenarios(BASE_PARAMS, SENSITIVITY_VALUES)


# ============================================================
# 5. 工具函数
# ============================================================

def manhattan_distance(id1, id2, grid_width):
    """计算两个网格 ID 之间的曼哈顿距离，单位：网格步数"""
    row1, col1 = divmod(int(id1), grid_width)
    row2, col2 = divmod(int(id2), grid_width)
    return abs(row1 - row2) + abs(col1 - col2)


def haversine(lat1, lon1, lat2, lon2):
    """计算经纬度之间的球面距离，单位：km"""
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def gurobi_status_name(status):
    mapping = {
        GRB.LOADED: "LOADED",
        GRB.OPTIMAL: "OPTIMAL",
        GRB.INFEASIBLE: "INFEASIBLE",
        GRB.INF_OR_UNBD: "INF_OR_UNBD",
        GRB.UNBOUNDED: "UNBOUNDED",
        GRB.CUTOFF: "CUTOFF",
        GRB.ITERATION_LIMIT: "ITERATION_LIMIT",
        GRB.NODE_LIMIT: "NODE_LIMIT",
        GRB.TIME_LIMIT: "TIME_LIMIT",
        GRB.SOLUTION_LIMIT: "SOLUTION_LIMIT",
        GRB.INTERRUPTED: "INTERRUPTED",
        GRB.NUMERIC: "NUMERIC",
        GRB.SUBOPTIMAL: "SUBOPTIMAL",
    }
    return mapping.get(status, f"UNKNOWN_{status}")


def safe_name(name):
    return str(name).replace(".", "p").replace("/", "_").replace("\\", "_").replace(" ", "_")


def get_capacity_dict(vertiport_data, capacity_column, default_capacity):
    """
    如果 CSV 里有容量列，就使用 CSV 里的容量；
    如果没有，就使用统一默认容量。
    """
    capacity = {}

    if capacity_column in vertiport_data.columns:
        for _, row in vertiport_data.iterrows():
            capacity[int(row["Grid_ID"])] = int(row[capacity_column])
    else:
        for grid_id in vertiport_data["Grid_ID"].tolist():
            capacity[int(grid_id)] = int(default_capacity)

    return capacity


def weighted_average(df, value_col, weight_col):
    if df.empty:
        return 0.0

    weight_sum = df[weight_col].sum()
    if weight_sum <= 0:
        return 0.0

    return float((df[value_col] * df[weight_col]).sum() / weight_sum)


def get_model_mip_gap(model):
    try:
        return float(model.MIPGap)
    except Exception:
        return None


# ============================================================
# 6. 读取 OD 流量数据
# ============================================================

print(f"读取流量文件: {FLOW_FILE}")
data = np.load(FLOW_FILE)
print("文件中可用的键:", data.files)

if "odflow" in data.files:
    flow_data = data["odflow"]
elif "arr_0" in data.files:
    flow_data = data["arr_0"]
else:
    raise KeyError("在 npz 文件中没有找到 'odflow' 或 'arr_0'。")

print("流量数据形状:", flow_data.shape)

num_time_intervals = min(MAX_TIME_INTERVALS, flow_data.shape[0])
time_intervals = [f"T{t}" for t in range(num_time_intervals)]


orders = {
    f"T{t}": [
        (int(i), int(j), int(round(flow_data[t, i, j])))
        for i in range(flow_data.shape[1])
        for j in range(flow_data.shape[2])
        if i != j and flow_data[t, i, j] > 0
    ]
    for t in range(num_time_intervals)
}

batches = [
    time_intervals[i:i + BATCH_SIZE]
    for i in range(0, len(time_intervals), BATCH_SIZE)
]

print(f"总时间片数量: {len(time_intervals)}")
print(f"总 batch 数量: {len(batches)}")
print(f"敏感性场景数量: {len(SCENARIOS)}")


# 保存本次运行配置
with open(OUTPUT_ROOT / "run_config.json", "w", encoding="utf-8") as f:
    json.dump(
        {
            "FLOW_FILE": FLOW_FILE,
            "CSV_FILES": CSV_FILES,
            "MAX_TIME_INTERVALS": MAX_TIME_INTERVALS,
            "BATCH_SIZE": BATCH_SIZE,
            "GRID_WIDTH": GRID_WIDTH,
            "MAX_ARCS_PER_ORDER": MAX_ARCS_PER_ORDER,
            "TIME_LIMIT": TIME_LIMIT,
            "MIP_GAP": MIP_GAP,
            "BASE_PARAMS": BASE_PARAMS,
            "SENSITIVITY_VALUES": SENSITIVITY_VALUES,
            "SCENARIOS": SCENARIOS,
        },
        f,
        ensure_ascii=False,
        indent=2
    )


# ============================================================
# 7. 结果列定义
# ============================================================

ASSIGNMENT_COLUMNS = [
    "Scenario",
    "ChangedParameter",
    "ChangedValue",
    "CSVFile",
    "BatchIndex",
    "Time",
    "Order",
    "Origin",
    "Destination",
    "Demand",
    "Start_Vertiport",
    "End_Vertiport",
    "AssignedFlow",
    "GroundStartKM",
    "AirKM",
    "GroundEndKM",
    "UnitPassengerCost",
    "ModelStatus",
    "BatchObjective",
]

ORDER_COLUMNS = [
    "Scenario",
    "ChangedParameter",
    "ChangedValue",
    "CSVFile",
    "BatchIndex",
    "Time",
    "Order",
    "Origin",
    "Destination",
    "Demand",
    "ServedFlow",
    "UnservedFlow",
    "ServiceRate",
    "ModelStatus",
]

ROUTE_COLUMNS = [
    "Scenario",
    "ChangedParameter",
    "ChangedValue",
    "CSVFile",
    "BatchIndex",
    "Time",
    "Start_Vertiport",
    "End_Vertiport",
    "PassengerFlow",
    "Flights",
    "AirKM",
    "LoadFactor",
    "ModelStatus",
]

BATCH_SUMMARY_COLUMNS = [
    "Scenario",
    "ChangedParameter",
    "ChangedValue",
    "CSVFile",
    "BatchIndex",
    "ModelStatus",
    "HasSolution",
    "BatchObjective",
    "MIPGap",
    "RuntimeSeconds",
    "NumOrders",
    "NumValidArcs",
    "NumXVars",
    "NumZVars",
    "BatchDemand",
    "BatchServed",
    "BatchUnserved",
    "BatchServiceRate",
]

SCENARIO_SUMMARY_COLUMNS = [
    "Scenario",
    "ChangedParameter",
    "ChangedValue",
    "CSVFile",
    "TotalObjective",
    "TotalDemand",
    "TotalServed",
    "TotalUnserved",
    "ServiceRate",
    "AvgGroundStartKM",
    "AvgAirKM",
    "AvgGroundEndKM",
    "AvgTotalTravelKM",
    "UsedVertiports",
    "UsedRoutes",
    "TotalFlights",
    "AverageLoadFactor",
    "OptimalBatches",
    "TimeLimitBatches",
    "NoSolutionBatches",
]


all_scenario_summaries = []


# ============================================================
# 8. 主循环：CSV 文件 × 敏感性场景 × batch
# ============================================================

for csv_file in CSV_FILES:
    print(f"\n==============================")
    print(f"读取停机坪文件: {csv_file}")
    print(f"==============================")

    vertiport_data = pd.read_csv(csv_file)

    required_columns = {"Grid_ID", "Latitude", "Longitude"}
    missing_columns = required_columns - set(vertiport_data.columns)
    if missing_columns:
        raise ValueError(f"{csv_file} 缺少必要列: {missing_columns}")

    vertiport_data = vertiport_data.drop_duplicates(subset=["Grid_ID"]).copy()
    vertiport_data["Grid_ID"] = vertiport_data["Grid_ID"].astype(int)

    vertiports = vertiport_data["Grid_ID"].tolist()
    print(f"载入停机坪数量: {len(vertiports)}")

    # 预先计算空中距离，单位 km
    coords = {
        int(row["Grid_ID"]): (float(row["Latitude"]), float(row["Longitude"]))
        for _, row in vertiport_data.iterrows()
    }

    distance_air = {}
    for p in vertiports:
        lat_p, lon_p = coords[p]
        for q in vertiports:
            if p == q:
                continue
            lat_q, lon_q = coords[q]
            distance_air[(p, q)] = haversine(lat_p, lon_p, lat_q, lon_q)

    print("空中距离计算完成。")

    for scenario_idx, params in enumerate(SCENARIOS, start=1):
        scenario_name = params["scenario_name"]
        changed_parameter = params["changed_parameter"]
        changed_value = params["changed_value"]

        print(f"\n--------------------------------------------------")
        print(f"场景 {scenario_idx}/{len(SCENARIOS)}: {scenario_name}")
        print(f"变化参数: {changed_parameter} = {changed_value}")
        print(f"--------------------------------------------------")

        assignment_rows = []
        order_rows = []
        route_rows = []
        batch_summary_rows = []

        # 当前场景的成本参数
        price_ground = float(params["price_ground"])
        price_air = float(params["price_air"])
        VOT = float(params["VOT"])
        speed_ground = float(params["speed_ground"])
        speed_air = float(params["speed_air"])
        grid_cell_size_km = float(params["GRID_CELL_SIZE_KM"])
        seat_capacity = int(params["SEAT_CAPACITY"])
        min_air_distance = float(params["MIN_AIR_DISTANCE"])
        max_air_distance = float(params["MAX_AIR_DISTANCE"])
        unserved_penalty = float(params["UNSERVED_PENALTY"])
        eps_flight_penalty = float(params["EPS_FLIGHT_PENALTY"])

        departure_capacity = get_capacity_dict(
            vertiport_data,
            "Departure_Capacity",
            int(params["DEFAULT_DEPARTURE_CAPACITY"])
        )

        arrival_capacity = get_capacity_dict(
            vertiport_data,
            "Arrival_Capacity",
            int(params["DEFAULT_ARRIVAL_CAPACITY"])
        )

        # 当前场景可行空中航线
        valid_arcs = [
            (p, q)
            for (p, q), dist in distance_air.items()
            if min_air_distance <= dist <= max_air_distance
        ]

        valid_arcs_by_start = defaultdict(list)
        valid_arcs_by_end = defaultdict(list)
        for p, q in valid_arcs:
            valid_arcs_by_start[p].append((p, q))
            valid_arcs_by_end[q].append((p, q))

        # 当前场景空中成本
        cost_air = {
            (p, q): distance_air[(p, q)] * price_air
            + VOT * (distance_air[(p, q)] / speed_air)
            for (p, q) in valid_arcs
        }

        for batch_idx, batch in enumerate(batches, start=1):
            print(f"\n  -> 场景 {scenario_name}, batch {batch_idx}/{len(batches)}")

            batch_orders = {t: orders[t] for t in batch}
            num_orders_in_batch = sum(len(batch_orders[t]) for t in batch)
            batch_demand = sum(
                demand
                for t in batch
                for _, _, demand in batch_orders[t]
            )

            print(f"     订单数量: {num_orders_in_batch}")
            print(f"     总需求: {batch_demand}")
            print(f"     可行空中航线数量: {len(valid_arcs)}")

            if num_orders_in_batch == 0:
                batch_summary_rows.append({
                    "Scenario": scenario_name,
                    "ChangedParameter": changed_parameter,
                    "ChangedValue": changed_value,
                    "CSVFile": csv_file,
                    "BatchIndex": batch_idx,
                    "ModelStatus": "NO_ORDERS",
                    "HasSolution": True,
                    "BatchObjective": 0.0,
                    "MIPGap": 0.0,
                    "RuntimeSeconds": 0.0,
                    "NumOrders": 0,
                    "NumValidArcs": len(valid_arcs),
                    "NumXVars": 0,
                    "NumZVars": 0,
                    "BatchDemand": 0,
                    "BatchServed": 0.0,
                    "BatchUnserved": 0.0,
                    "BatchServiceRate": 1.0,
                })
                continue

            # ------------------------------------------------------------
            # 8.1 计算当前 batch 的地面距离，单位 km
            # ------------------------------------------------------------

            distance_ground_start = {
                (i, p): manhattan_distance(i, p, GRID_WIDTH) * grid_cell_size_km
                for t in batch
                for (i, j, demand) in batch_orders[t]
                for p in vertiports
            }

            distance_ground_end = {
                (j, q): manhattan_distance(j, q, GRID_WIDTH) * grid_cell_size_km
                for t in batch
                for (i, j, demand) in batch_orders[t]
                for q in vertiports
            }

            cost_ground_start = {
                (i, p): dist * price_ground + VOT * (dist / speed_ground)
                for (i, p), dist in distance_ground_start.items()
            }

            cost_ground_end = {
                (j, q): dist * price_ground + VOT * (dist / speed_ground)
                for (j, q), dist in distance_ground_end.items()
            }

            # ------------------------------------------------------------
            # 8.2 为每个订单生成候选航线
            # ------------------------------------------------------------

            candidate_arcs_by_order = {}
            orders_by_arc = defaultdict(list)
            x_keys = []

            for t in batch:
                for o, (i, j, demand) in enumerate(batch_orders[t]):
                    if MAX_ARCS_PER_ORDER is None:
                        candidate_arcs = valid_arcs
                    else:
                        candidate_arcs = sorted(
                            valid_arcs,
                            key=lambda arc: (
                                cost_ground_start[(i, arc[0])]
                                + cost_air[arc]
                                + cost_ground_end[(j, arc[1])]
                            )
                        )[:MAX_ARCS_PER_ORDER]

                    candidate_arcs_by_order[(t, o)] = candidate_arcs

                    for p, q in candidate_arcs:
                        key = (t, o, p, q)
                        x_keys.append(key)
                        orders_by_arc[(t, p, q)].append(o)

            z_keys = [
                (t, p, q)
                for t in batch
                for (p, q) in valid_arcs
            ]

            print(f"     x 变量数量: {len(x_keys)}")
            print(f"     z 变量数量: {len(z_keys)}")

            # ------------------------------------------------------------
            # 8.3 建立 Gurobi 模型
            # ------------------------------------------------------------

            model = Model(f"UAM_{scenario_name}_batch_{batch_idx}")
            model.setParam("OutputFlag", OUTPUT_FLAG)
            model.setParam("TimeLimit", TIME_LIMIT)
            model.setParam("MIPGap", MIP_GAP)

            x = model.addVars(
                x_keys,
                vtype=GRB.INTEGER,
                lb=0,
                name="x"
            )

            z = model.addVars(
                z_keys,
                vtype=GRB.INTEGER,
                lb=0,
                name="z"
            )

            unmet_keys = [
                (t, o)
                for t in batch
                for o in range(len(batch_orders[t]))
            ]

            unmet = model.addVars(
                unmet_keys,
                vtype=GRB.INTEGER,
                lb=0,
                name="unmet"
            )

            # ------------------------------------------------------------
            # 8.4 目标函数
            # ------------------------------------------------------------

            service_cost = quicksum(
                x[t, o, p, q] * (
                    cost_ground_start[(batch_orders[t][o][0], p)]
                    + cost_air[(p, q)]
                    + cost_ground_end[(batch_orders[t][o][1], q)]
                )
                for (t, o, p, q) in x_keys
            )

            unserved_cost = quicksum(
                unmet[t, o] * unserved_penalty
                for (t, o) in unmet_keys
            )

            flight_tiebreaker = quicksum(
                eps_flight_penalty * z[t, p, q]
                for (t, p, q) in z_keys
            )

            model.setObjective(
                service_cost + unserved_cost + flight_tiebreaker,
                GRB.MINIMIZE
            )

            # ------------------------------------------------------------
            # 8.5 约束 1：需求平衡
            # 服务量 + 未服务量 = 订单需求
            # ------------------------------------------------------------

            model.addConstrs(
                quicksum(
                    x[t, o, p, q]
                    for (p, q) in candidate_arcs_by_order[(t, o)]
                )
                + unmet[t, o]
                == batch_orders[t][o][2]
                for (t, o) in unmet_keys
            )

            # ------------------------------------------------------------
            # 8.6 约束 2：座位容量
            # 某条航线的乘客量 <= 座位数 * 飞行架次数
            # ------------------------------------------------------------

            model.addConstrs(
                quicksum(
                    x[t, o, p, q]
                    for o in orders_by_arc.get((t, p, q), [])
                )
                <= seat_capacity * z[t, p, q]
                for (t, p, q) in z_keys
            )

            # ------------------------------------------------------------
            # 8.7 约束 3：停机坪起飞容量
            # ------------------------------------------------------------

            model.addConstrs(
                quicksum(
                    z[t, p, q]
                    for (p2, q) in valid_arcs_by_start[p]
                    if p2 == p
                )
                <= departure_capacity[p]
                for t in batch
                for p in vertiports
            )

            # ------------------------------------------------------------
            # 8.8 约束 4：停机坪降落容量
            # ------------------------------------------------------------

            model.addConstrs(
                quicksum(
                    z[t, p, q]
                    for (p, q2) in valid_arcs_by_end[q]
                    if q2 == q
                )
                <= arrival_capacity[q]
                for t in batch
                for q in vertiports
            )

            # 不加入总机队时间约束
            # 不加入航线容量约束
            # 不加入飞机流动平衡约束

            # ------------------------------------------------------------
            # 8.9 求解
            # ------------------------------------------------------------

            try:
                model.optimize()
            except Exception:
                print("     Gurobi 求解出错:")
                traceback.print_exc()
                continue

            status_name = gurobi_status_name(model.status)
            has_solution = model.SolCount > 0

            if has_solution:
                batch_objective = float(model.ObjVal)
                mip_gap = get_model_mip_gap(model)
            else:
                batch_objective = None
                mip_gap = None

            print(f"     模型状态: {status_name}")
            print(f"     是否有可行解: {has_solution}")

            # ------------------------------------------------------------
            # 8.10 提取结果
            # ------------------------------------------------------------

            batch_served = 0.0
            batch_unserved = 0.0
            order_served = {
                (t, o): 0.0
                for (t, o) in unmet_keys
            }

            route_passenger_flow = defaultdict(float)

            if has_solution:
                for (t, o, p, q) in x_keys:
                    assigned = float(x[t, o, p, q].X)

                    if assigned > 1e-6:
                        i, j, demand = batch_orders[t][o]

                        ground_start_km = distance_ground_start[(i, p)]
                        ground_end_km = distance_ground_end[(j, q)]
                        air_km = distance_air[(p, q)]

                        unit_cost = (
                            cost_ground_start[(i, p)]
                            + cost_air[(p, q)]
                            + cost_ground_end[(j, q)]
                        )

                        assignment_rows.append({
                            "Scenario": scenario_name,
                            "ChangedParameter": changed_parameter,
                            "ChangedValue": changed_value,
                            "CSVFile": csv_file,
                            "BatchIndex": batch_idx,
                            "Time": t,
                            "Order": o,
                            "Origin": i,
                            "Destination": j,
                            "Demand": demand,
                            "Start_Vertiport": p,
                            "End_Vertiport": q,
                            "AssignedFlow": assigned,
                            "GroundStartKM": ground_start_km,
                            "AirKM": air_km,
                            "GroundEndKM": ground_end_km,
                            "UnitPassengerCost": unit_cost,
                            "ModelStatus": status_name,
                            "BatchObjective": batch_objective,
                        })

                        order_served[(t, o)] += assigned
                        route_passenger_flow[(t, p, q)] += assigned
                        batch_served += assigned

                for (t, o) in unmet_keys:
                    i, j, demand = batch_orders[t][o]
                    unserved_value = float(unmet[t, o].X)
                    served_value = order_served[(t, o)]

                    batch_unserved += unserved_value

                    order_rows.append({
                        "Scenario": scenario_name,
                        "ChangedParameter": changed_parameter,
                        "ChangedValue": changed_value,
                        "CSVFile": csv_file,
                        "BatchIndex": batch_idx,
                        "Time": t,
                        "Order": o,
                        "Origin": i,
                        "Destination": j,
                        "Demand": demand,
                        "ServedFlow": served_value,
                        "UnservedFlow": unserved_value,
                        "ServiceRate": served_value / demand if demand > 0 else 1.0,
                        "ModelStatus": status_name,
                    })

                for (t, p, q) in z_keys:
                    flights = float(z[t, p, q].X)
                    passenger_flow = route_passenger_flow[(t, p, q)]

                    if flights > 1e-6 or passenger_flow > 1e-6:
                        route_rows.append({
                            "Scenario": scenario_name,
                            "ChangedParameter": changed_parameter,
                            "ChangedValue": changed_value,
                            "CSVFile": csv_file,
                            "BatchIndex": batch_idx,
                            "Time": t,
                            "Start_Vertiport": p,
                            "End_Vertiport": q,
                            "PassengerFlow": passenger_flow,
                            "Flights": flights,
                            "AirKM": distance_air[(p, q)],
                            "LoadFactor": (
                                passenger_flow / (flights * seat_capacity)
                                if flights > 1e-6 else 0.0
                            ),
                            "ModelStatus": status_name,
                        })

            else:
                # 理论上由于 unmet 变量存在，模型通常总是可行。
                # 若没有解，则保守记录为未服务。
                for (t, o) in unmet_keys:
                    i, j, demand = batch_orders[t][o]
                    order_rows.append({
                        "Scenario": scenario_name,
                        "ChangedParameter": changed_parameter,
                        "ChangedValue": changed_value,
                        "CSVFile": csv_file,
                        "BatchIndex": batch_idx,
                        "Time": t,
                        "Order": o,
                        "Origin": i,
                        "Destination": j,
                        "Demand": demand,
                        "ServedFlow": 0.0,
                        "UnservedFlow": demand,
                        "ServiceRate": 0.0,
                        "ModelStatus": status_name,
                    })

                batch_served = 0.0
                batch_unserved = float(batch_demand)

            batch_summary_rows.append({
                "Scenario": scenario_name,
                "ChangedParameter": changed_parameter,
                "ChangedValue": changed_value,
                "CSVFile": csv_file,
                "BatchIndex": batch_idx,
                "ModelStatus": status_name,
                "HasSolution": has_solution,
                "BatchObjective": batch_objective,
                "MIPGap": mip_gap,
                "RuntimeSeconds": float(model.Runtime),
                "NumOrders": num_orders_in_batch,
                "NumValidArcs": len(valid_arcs),
                "NumXVars": len(x_keys),
                "NumZVars": len(z_keys),
                "BatchDemand": batch_demand,
                "BatchServed": batch_served,
                "BatchUnserved": batch_unserved,
                "BatchServiceRate": batch_served / batch_demand if batch_demand > 0 else 1.0,
            })

        # ============================================================
        # 9. 保存当前场景结果
        # ============================================================

        csv_stem = Path(csv_file).stem
        scenario_safe = safe_name(scenario_name)

        assignments_df = pd.DataFrame(assignment_rows, columns=ASSIGNMENT_COLUMNS)
        orders_df = pd.DataFrame(order_rows, columns=ORDER_COLUMNS)
        routes_df = pd.DataFrame(route_rows, columns=ROUTE_COLUMNS)
        batch_summary_df = pd.DataFrame(batch_summary_rows, columns=BATCH_SUMMARY_COLUMNS)

        assignments_file = OUTPUT_ROOT / f"assignments_{csv_stem}_{scenario_safe}.csv"
        orders_file = OUTPUT_ROOT / f"orders_{csv_stem}_{scenario_safe}.csv"
        routes_file = OUTPUT_ROOT / f"routes_{csv_stem}_{scenario_safe}.csv"
        batch_summary_file = OUTPUT_ROOT / f"batch_summary_{csv_stem}_{scenario_safe}.csv"

        assignments_df.to_csv(assignments_file, index=False)
        orders_df.to_csv(orders_file, index=False)
        routes_df.to_csv(routes_file, index=False)
        batch_summary_df.to_csv(batch_summary_file, index=False)

        # ============================================================
        # 10. 当前场景总汇总
        # ============================================================

        total_demand = float(orders_df["Demand"].sum()) if not orders_df.empty else 0.0
        total_served = float(orders_df["ServedFlow"].sum()) if not orders_df.empty else 0.0
        total_unserved = float(orders_df["UnservedFlow"].sum()) if not orders_df.empty else 0.0

        total_objective = (
            float(batch_summary_df["BatchObjective"].dropna().sum())
            if not batch_summary_df.empty else 0.0
        )

        avg_ground_start_km = weighted_average(assignments_df, "GroundStartKM", "AssignedFlow")
        avg_air_km = weighted_average(assignments_df, "AirKM", "AssignedFlow")
        avg_ground_end_km = weighted_average(assignments_df, "GroundEndKM", "AssignedFlow")

        avg_total_travel_km = (
            avg_ground_start_km + avg_air_km + avg_ground_end_km
        )

        if assignments_df.empty:
            used_vertiports = set()
            used_routes = set()
        else:
            used_vertiports = set(assignments_df["Start_Vertiport"]).union(
                set(assignments_df["End_Vertiport"])
            )
            used_routes = set(
                zip(assignments_df["Start_Vertiport"], assignments_df["End_Vertiport"])
            )

        total_flights = float(routes_df["Flights"].sum()) if not routes_df.empty else 0.0

        if total_flights > 0:
            average_load_factor = float(
                routes_df["PassengerFlow"].sum() / (routes_df["Flights"].sum() * seat_capacity)
            )
        else:
            average_load_factor = 0.0

        optimal_batches = int((batch_summary_df["ModelStatus"] == "OPTIMAL").sum())
        time_limit_batches = int((batch_summary_df["ModelStatus"] == "TIME_LIMIT").sum())
        no_solution_batches = int((batch_summary_df["HasSolution"] == False).sum())

        scenario_summary = {
            "Scenario": scenario_name,
            "ChangedParameter": changed_parameter,
            "ChangedValue": changed_value,
            "CSVFile": csv_file,
            "TotalObjective": total_objective,
            "TotalDemand": total_demand,
            "TotalServed": total_served,
            "TotalUnserved": total_unserved,
            "ServiceRate": total_served / total_demand if total_demand > 0 else 1.0,
            "AvgGroundStartKM": avg_ground_start_km,
            "AvgAirKM": avg_air_km,
            "AvgGroundEndKM": avg_ground_end_km,
            "AvgTotalTravelKM": avg_total_travel_km,
            "UsedVertiports": len(used_vertiports),
            "UsedRoutes": len(used_routes),
            "TotalFlights": total_flights,
            "AverageLoadFactor": average_load_factor,
            "OptimalBatches": optimal_batches,
            "TimeLimitBatches": time_limit_batches,
            "NoSolutionBatches": no_solution_batches,
        }

        all_scenario_summaries.append(scenario_summary)

        print(f"\n  当前场景结果已保存:")
        print(f"    {assignments_file}")
        print(f"    {orders_file}")
        print(f"    {routes_file}")
        print(f"    {batch_summary_file}")


# ============================================================
# 11. 保存所有场景汇总
# ============================================================

scenario_summary_df = pd.DataFrame(
    all_scenario_summaries,
    columns=SCENARIO_SUMMARY_COLUMNS
)

scenario_summary_file = OUTPUT_ROOT / "scenario_summary.csv"
scenario_summary_df.to_csv(scenario_summary_file, index=False)

print("\n============================================================")
print("所有敏感性分析运行完成。")
print(f"结果目录: {OUTPUT_ROOT}")
print(f"总汇总文件: {scenario_summary_file}")
print("============================================================")
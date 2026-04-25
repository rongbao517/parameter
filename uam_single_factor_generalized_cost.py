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

# 只做单因素分析
SINGLE_FACTOR_NAME = "MIN_AIR_DISTANCE"
SINGLE_FACTOR_VALUES = [3.0, 6.0, 9.0]

OUTPUT_ROOT = Path(
    f"single_factor_results_{SINGLE_FACTOR_NAME}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
)
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


# ============================================================
# 2. 基础运行设置
# ============================================================

MAX_TIME_INTERVALS = 500
BATCH_SIZE = 10
GRID_WIDTH = 52

# None 表示每个订单都可以选择所有可行航线。
# 如果模型太大，可以改成 30 或 50。
MAX_ARCS_PER_ORDER = None

TIME_LIMIT = 600
MIP_GAP = 0.01
OUTPUT_FLAG = 1


# ============================================================
# 3. 基准参数
# ============================================================

BASE_PARAMS = {
    # 运营成本，美元/km
    "price_ground": 5.0,
    "price_air": 7.0,

    # 乘客时间价值，美元/hour
    "VOT": 20.0,

    # 速度，km/hour
    "speed_ground": 18.0,
    "speed_air": 150.0,

    # 网格尺寸，km
    "GRID_CELL_SIZE_KM": 1.5,

    # UAM 固定时间，分钟：等待、登机、起降缓冲等
    "UAM_FIXED_TIME_MINUTES": 10.0,

    # UAM 固定费用，美元/人
    "UAM_FIXED_COST": 0.0,

    # 飞机座位数
    "SEAT_CAPACITY": 4,

    # 空中距离限制
    "MIN_AIR_DISTANCE": 6.0,
    "MAX_AIR_DISTANCE": 60.0,

    # 停机坪容量，单位：架次 / 时间片
    "DEFAULT_DEPARTURE_CAPACITY": 20,
    "DEFAULT_ARRIVAL_CAPACITY": 20,

    # 很小的飞行架次数惩罚，只用于 tie-break
    "EPS_FLIGHT_PENALTY": 1e-3,
}


# ============================================================
# 4. 单因素场景
# ============================================================

def build_single_factor_scenarios(base_params, factor_name, factor_values):
    if factor_name not in base_params:
        raise KeyError(f"{factor_name} 不在 BASE_PARAMS 中。")

    scenarios = []

    baseline = base_params.copy()
    baseline["scenario_name"] = "baseline"
    baseline["changed_parameter"] = "none"
    baseline["changed_value"] = "baseline"
    scenarios.append(baseline)

    base_value = base_params[factor_name]
    for value in factor_values:
        if value == base_value:
            continue

        scenario = base_params.copy()
        scenario[factor_name] = value
        scenario["scenario_name"] = f"{factor_name}_{str(value).replace('.', 'p')}"
        scenario["changed_parameter"] = factor_name
        scenario["changed_value"] = value
        scenarios.append(scenario)

    return scenarios


SCENARIOS = build_single_factor_scenarios(
    BASE_PARAMS,
    SINGLE_FACTOR_NAME,
    SINGLE_FACTOR_VALUES,
)


# ============================================================
# 5. 工具函数
# ============================================================

def manhattan_distance(id1, id2, grid_width):
    row1, col1 = divmod(int(id1), grid_width)
    row2, col2 = divmod(int(id2), grid_width)
    return abs(row1 - row2) + abs(col1 - col2)


def haversine(lat1, lon1, lat2, lon2):
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


def direct_ground_generalized_cost(distance_km, price_ground, speed_ground, VOT):
    time_hours = distance_km / speed_ground
    return price_ground * distance_km + VOT * time_hours


def uam_generalized_cost(
    ground_start_km,
    air_km,
    ground_end_km,
    price_ground,
    price_air,
    speed_ground,
    speed_air,
    VOT,
    fixed_time_minutes,
    fixed_cost,
):
    ground_km = ground_start_km + ground_end_km
    travel_time_hours = (
        ground_start_km / speed_ground
        + air_km / speed_air
        + ground_end_km / speed_ground
        + fixed_time_minutes / 60.0
    )
    operating_cost = price_ground * ground_km + price_air * air_km + fixed_cost
    time_cost = VOT * travel_time_hours
    return operating_cost + time_cost


def uam_travel_time_hours(
    ground_start_km,
    air_km,
    ground_end_km,
    speed_ground,
    speed_air,
    fixed_time_minutes,
):
    return (
        ground_start_km / speed_ground
        + air_km / speed_air
        + ground_end_km / speed_ground
        + fixed_time_minutes / 60.0
    )


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
print(f"单因素场景数量: {len(SCENARIOS)}")


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
            "SINGLE_FACTOR_NAME": SINGLE_FACTOR_NAME,
            "SINGLE_FACTOR_VALUES": SINGLE_FACTOR_VALUES,
            "SCENARIOS": SCENARIOS,
            "objective_description": (
                "Minimize system generalized cost. Each passenger can either use UAM "
                "or remain on direct ground travel. UAM is selected only when its generalized "
                "cost is competitive after considering operating cost, time value, fixed UAM time, "
                "seat capacity, and vertiport capacities."
            ),
        },
        f,
        ensure_ascii=False,
        indent=2,
    )


# ============================================================
# 7. 结果列
# ============================================================

ASSIGNMENT_COLUMNS = [
    "Scenario", "ChangedParameter", "ChangedValue", "CSVFile", "BatchIndex",
    "Time", "Order", "Origin", "Destination", "Demand",
    "Start_Vertiport", "End_Vertiport", "AssignedFlow",
    "DirectGroundKM", "GroundStartKM", "AirKM", "GroundEndKM",
    "DirectGroundTimeHours", "UAMTimeHours", "TimeSavingHours",
    "DirectGroundCost", "UAMGeneralizedCost", "UnitCostSaving",
    "ModelStatus", "BatchObjective",
]

ORDER_COLUMNS = [
    "Scenario", "ChangedParameter", "ChangedValue", "CSVFile", "BatchIndex",
    "Time", "Order", "Origin", "Destination", "Demand",
    "UAMFlow", "GroundOnlyFlow", "UAMShare",
    "DirectGroundKM", "DirectGroundTimeHours", "DirectGroundCost",
    "GroundOnlyCost", "ModelStatus",
]

ROUTE_COLUMNS = [
    "Scenario", "ChangedParameter", "ChangedValue", "CSVFile", "BatchIndex",
    "Time", "Start_Vertiport", "End_Vertiport",
    "PassengerFlow", "Flights", "AirKM", "LoadFactor", "ModelStatus",
]

BATCH_SUMMARY_COLUMNS = [
    "Scenario", "ChangedParameter", "ChangedValue", "CSVFile", "BatchIndex",
    "ModelStatus", "HasSolution", "BatchObjective",
    "BatchDirectGroundBaselineCost", "BatchSystemGeneralizedCost",
    "BatchSavingsVsDirectGround", "BatchPassengerTimeSavingHours",
    "MIPGap", "RuntimeSeconds", "NumOrders", "NumValidArcs",
    "NumXVars", "NumZVars", "BatchDemand",
    "BatchUAMServed", "BatchGroundOnly", "BatchUAMShare",
]

SCENARIO_SUMMARY_COLUMNS = [
    "Scenario", "ChangedParameter", "ChangedValue", "CSVFile",
    "TotalObjective", "TotalDirectGroundBaselineCost",
    "TotalSystemGeneralizedCost", "TotalSavingsVsDirectGround",
    "TotalPassengerTimeSavingHours", "TotalDemand", "TotalUAMServed",
    "TotalGroundOnly", "UAMShare", "AvgGroundStartKM", "AvgAirKM",
    "AvgGroundEndKM", "AvgUAMTotalTravelKM",
    "AvgDirectGroundKMForUAMUsers", "AvgTimeSavingHoursForUAMUsers",
    "AvgUnitCostSavingForUAMUsers", "UsedVertiports", "UsedRoutes",
    "TotalFlights", "AverageLoadFactor", "OptimalBatches",
    "TimeLimitBatches", "NoSolutionBatches",
]


all_scenario_summaries = []


# ============================================================
# 8. 主循环
# ============================================================

for csv_file in CSV_FILES:
    print("\n==============================")
    print(f"读取停机坪文件: {csv_file}")
    print("==============================")

    vertiport_data = pd.read_csv(csv_file)

    required_columns = {"Grid_ID", "Latitude", "Longitude"}
    missing_columns = required_columns - set(vertiport_data.columns)
    if missing_columns:
        raise ValueError(f"{csv_file} 缺少必要列: {missing_columns}")

    vertiport_data = vertiport_data.drop_duplicates(subset=["Grid_ID"]).copy()
    vertiport_data["Grid_ID"] = vertiport_data["Grid_ID"].astype(int)

    vertiports = vertiport_data["Grid_ID"].tolist()
    print(f"载入停机坪数量: {len(vertiports)}")

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

        print("\n--------------------------------------------------")
        print(f"场景 {scenario_idx}/{len(SCENARIOS)}: {scenario_name}")
        print(f"变化参数: {changed_parameter} = {changed_value}")
        print("--------------------------------------------------")

        assignment_rows = []
        order_rows = []
        route_rows = []
        batch_summary_rows = []

        price_ground = float(params["price_ground"])
        price_air = float(params["price_air"])
        VOT = float(params["VOT"])
        speed_ground = float(params["speed_ground"])
        speed_air = float(params["speed_air"])
        grid_cell_size_km = float(params["GRID_CELL_SIZE_KM"])
        fixed_time_minutes = float(params["UAM_FIXED_TIME_MINUTES"])
        fixed_cost = float(params["UAM_FIXED_COST"])
        seat_capacity = int(params["SEAT_CAPACITY"])
        min_air_distance = float(params["MIN_AIR_DISTANCE"])
        max_air_distance = float(params["MAX_AIR_DISTANCE"])
        eps_flight_penalty = float(params["EPS_FLIGHT_PENALTY"])

        departure_capacity = get_capacity_dict(
            vertiport_data,
            "Departure_Capacity",
            int(params["DEFAULT_DEPARTURE_CAPACITY"]),
        )

        arrival_capacity = get_capacity_dict(
            vertiport_data,
            "Arrival_Capacity",
            int(params["DEFAULT_ARRIVAL_CAPACITY"]),
        )

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
                    "BatchDirectGroundBaselineCost": 0.0,
                    "BatchSystemGeneralizedCost": 0.0,
                    "BatchSavingsVsDirectGround": 0.0,
                    "BatchPassengerTimeSavingHours": 0.0,
                    "MIPGap": 0.0,
                    "RuntimeSeconds": 0.0,
                    "NumOrders": 0,
                    "NumValidArcs": len(valid_arcs),
                    "NumXVars": 0,
                    "NumZVars": 0,
                    "BatchDemand": 0,
                    "BatchUAMServed": 0.0,
                    "BatchGroundOnly": 0.0,
                    "BatchUAMShare": 0.0,
                })
                continue

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

            distance_direct_ground = {
                (i, j): manhattan_distance(i, j, GRID_WIDTH) * grid_cell_size_km
                for t in batch
                for (i, j, demand) in batch_orders[t]
            }

            cost_ground_start = {
                (i, p): dist * price_ground + VOT * (dist / speed_ground)
                for (i, p), dist in distance_ground_start.items()
            }

            cost_ground_end = {
                (j, q): dist * price_ground + VOT * (dist / speed_ground)
                for (j, q), dist in distance_ground_end.items()
            }

            cost_direct_ground = {
                (i, j): direct_ground_generalized_cost(
                    dist,
                    price_ground,
                    speed_ground,
                    VOT,
                )
                for (i, j), dist in distance_direct_ground.items()
            }

            time_direct_ground = {
                (i, j): dist / speed_ground
                for (i, j), dist in distance_direct_ground.items()
            }

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
                                + fixed_cost
                                + VOT * (fixed_time_minutes / 60.0)
                            ),
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

            ground_only_keys = [
                (t, o)
                for t in batch
                for o in range(len(batch_orders[t]))
            ]

            print(f"     x 变量数量: {len(x_keys)}")
            print(f"     z 变量数量: {len(z_keys)}")
            print(f"     ground_only 变量数量: {len(ground_only_keys)}")

            model = Model(f"UAM_{scenario_name}_batch_{batch_idx}")
            model.setParam("OutputFlag", OUTPUT_FLAG)
            model.setParam("TimeLimit", TIME_LIMIT)
            model.setParam("MIPGap", MIP_GAP)

            x = model.addVars(
                x_keys,
                vtype=GRB.INTEGER,
                lb=0,
                name="x_uam",
            )

            z = model.addVars(
                z_keys,
                vtype=GRB.INTEGER,
                lb=0,
                name="z_flights",
            )

            ground_only = model.addVars(
                ground_only_keys,
                vtype=GRB.INTEGER,
                lb=0,
                name="ground_only",
            )

            uam_service_cost = quicksum(
                x[t, o, p, q] * (
                    cost_ground_start[(batch_orders[t][o][0], p)]
                    + cost_air[(p, q)]
                    + cost_ground_end[(batch_orders[t][o][1], q)]
                    + fixed_cost
                    + VOT * (fixed_time_minutes / 60.0)
                )
                for (t, o, p, q) in x_keys
            )

            direct_ground_service_cost = quicksum(
                ground_only[t, o]
                * cost_direct_ground[(batch_orders[t][o][0], batch_orders[t][o][1])]
                for (t, o) in ground_only_keys
            )

            flight_tiebreaker = quicksum(
                eps_flight_penalty * z[t, p, q]
                for (t, p, q) in z_keys
            )

            model.setObjective(
                uam_service_cost + direct_ground_service_cost + flight_tiebreaker,
                GRB.MINIMIZE,
            )

            model.addConstrs(
                (
                    quicksum(
                        x[t, o, p, q]
                        for (p, q) in candidate_arcs_by_order[(t, o)]
                    )
                    + ground_only[t, o]
                    == batch_orders[t][o][2]
                    for (t, o) in ground_only_keys
                ),
                name="demand_balance",
            )

            model.addConstrs(
                (
                    quicksum(
                        x[t, o, p, q]
                        for o in orders_by_arc.get((t, p, q), [])
                    )
                    <= seat_capacity * z[t, p, q]
                    for (t, p, q) in z_keys
                ),
                name="seat_capacity",
            )

            model.addConstrs(
                (
                    quicksum(
                        z[t, p, q]
                        for _, q in valid_arcs_by_start.get(p, [])
                    )
                    <= departure_capacity[p]
                    for t in batch
                    for p in vertiports
                ),
                name="departure_capacity",
            )

            model.addConstrs(
                (
                    quicksum(
                        z[t, p, q]
                        for p, _ in valid_arcs_by_end.get(q, [])
                    )
                    <= arrival_capacity[q]
                    for t in batch
                    for q in vertiports
                ),
                name="arrival_capacity",
            )

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

            order_uam_served = {
                (t, o): 0.0
                for (t, o) in ground_only_keys
            }
            route_passenger_flow = defaultdict(float)

            batch_uam_served = 0.0
            batch_ground_only = 0.0
            batch_uam_cost = 0.0
            batch_ground_only_cost = 0.0
            batch_direct_ground_baseline_cost = 0.0
            batch_passenger_time_saving_hours = 0.0

            for (t, o) in ground_only_keys:
                i, j, demand = batch_orders[t][o]
                direct_cost = cost_direct_ground[(i, j)]
                batch_direct_ground_baseline_cost += demand * direct_cost

            if has_solution:
                for (t, o, p, q) in x_keys:
                    assigned = float(x[t, o, p, q].X)

                    if assigned > 1e-6:
                        i, j, demand = batch_orders[t][o]

                        ground_start_km = distance_ground_start[(i, p)]
                        ground_end_km = distance_ground_end[(j, q)]
                        air_km = distance_air[(p, q)]
                        direct_ground_km = distance_direct_ground[(i, j)]

                        direct_time_hours = time_direct_ground[(i, j)]
                        uam_time_hours_value = uam_travel_time_hours(
                            ground_start_km,
                            air_km,
                            ground_end_km,
                            speed_ground,
                            speed_air,
                            fixed_time_minutes,
                        )

                        direct_cost = cost_direct_ground[(i, j)]
                        uam_cost = uam_generalized_cost(
                            ground_start_km,
                            air_km,
                            ground_end_km,
                            price_ground,
                            price_air,
                            speed_ground,
                            speed_air,
                            VOT,
                            fixed_time_minutes,
                            fixed_cost,
                        )

                        time_saving_hours = direct_time_hours - uam_time_hours_value
                        unit_cost_saving = direct_cost - uam_cost

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
                            "DirectGroundKM": direct_ground_km,
                            "GroundStartKM": ground_start_km,
                            "AirKM": air_km,
                            "GroundEndKM": ground_end_km,
                            "DirectGroundTimeHours": direct_time_hours,
                            "UAMTimeHours": uam_time_hours_value,
                            "TimeSavingHours": time_saving_hours,
                            "DirectGroundCost": direct_cost,
                            "UAMGeneralizedCost": uam_cost,
                            "UnitCostSaving": unit_cost_saving,
                            "ModelStatus": status_name,
                            "BatchObjective": batch_objective,
                        })

                        order_uam_served[(t, o)] += assigned
                        route_passenger_flow[(t, p, q)] += assigned
                        batch_uam_served += assigned
                        batch_uam_cost += assigned * uam_cost
                        batch_passenger_time_saving_hours += assigned * time_saving_hours

                for (t, o) in ground_only_keys:
                    i, j, demand = batch_orders[t][o]
                    ground_only_value = float(ground_only[t, o].X)
                    uam_value = order_uam_served[(t, o)]

                    direct_ground_km = distance_direct_ground[(i, j)]
                    direct_time_hours = time_direct_ground[(i, j)]
                    direct_cost = cost_direct_ground[(i, j)]
                    ground_only_cost = ground_only_value * direct_cost

                    batch_ground_only += ground_only_value
                    batch_ground_only_cost += ground_only_cost

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
                        "UAMFlow": uam_value,
                        "GroundOnlyFlow": ground_only_value,
                        "UAMShare": uam_value / demand if demand > 0 else 0.0,
                        "DirectGroundKM": direct_ground_km,
                        "DirectGroundTimeHours": direct_time_hours,
                        "DirectGroundCost": direct_cost,
                        "GroundOnlyCost": ground_only_cost,
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
                for (t, o) in ground_only_keys:
                    i, j, demand = batch_orders[t][o]
                    direct_ground_km = distance_direct_ground[(i, j)]
                    direct_time_hours = time_direct_ground[(i, j)]
                    direct_cost = cost_direct_ground[(i, j)]
                    ground_only_cost = demand * direct_cost

                    batch_ground_only += demand
                    batch_ground_only_cost += ground_only_cost

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
                        "UAMFlow": 0.0,
                        "GroundOnlyFlow": demand,
                        "UAMShare": 0.0,
                        "DirectGroundKM": direct_ground_km,
                        "DirectGroundTimeHours": direct_time_hours,
                        "DirectGroundCost": direct_cost,
                        "GroundOnlyCost": ground_only_cost,
                        "ModelStatus": status_name,
                    })

            batch_system_generalized_cost = batch_uam_cost + batch_ground_only_cost
            batch_savings_vs_direct_ground = (
                batch_direct_ground_baseline_cost - batch_system_generalized_cost
            )

            batch_summary_rows.append({
                "Scenario": scenario_name,
                "ChangedParameter": changed_parameter,
                "ChangedValue": changed_value,
                "CSVFile": csv_file,
                "BatchIndex": batch_idx,
                "ModelStatus": status_name,
                "HasSolution": has_solution,
                "BatchObjective": batch_objective,
                "BatchDirectGroundBaselineCost": batch_direct_ground_baseline_cost,
                "BatchSystemGeneralizedCost": batch_system_generalized_cost,
                "BatchSavingsVsDirectGround": batch_savings_vs_direct_ground,
                "BatchPassengerTimeSavingHours": batch_passenger_time_saving_hours,
                "MIPGap": mip_gap,
                "RuntimeSeconds": float(model.Runtime),
                "NumOrders": num_orders_in_batch,
                "NumValidArcs": len(valid_arcs),
                "NumXVars": len(x_keys),
                "NumZVars": len(z_keys),
                "BatchDemand": batch_demand,
                "BatchUAMServed": batch_uam_served,
                "BatchGroundOnly": batch_ground_only,
                "BatchUAMShare": batch_uam_served / batch_demand if batch_demand > 0 else 0.0,
            })

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

        total_demand = float(orders_df["Demand"].sum()) if not orders_df.empty else 0.0
        total_uam_served = float(orders_df["UAMFlow"].sum()) if not orders_df.empty else 0.0
        total_ground_only = float(orders_df["GroundOnlyFlow"].sum()) if not orders_df.empty else 0.0

        total_objective = (
            float(batch_summary_df["BatchObjective"].dropna().sum())
            if not batch_summary_df.empty else 0.0
        )

        total_direct_ground_baseline_cost = (
            float(batch_summary_df["BatchDirectGroundBaselineCost"].sum())
            if not batch_summary_df.empty else 0.0
        )

        total_system_generalized_cost = (
            float(batch_summary_df["BatchSystemGeneralizedCost"].sum())
            if not batch_summary_df.empty else 0.0
        )

        total_savings_vs_direct_ground = (
            total_direct_ground_baseline_cost - total_system_generalized_cost
        )

        total_passenger_time_saving_hours = (
            float(batch_summary_df["BatchPassengerTimeSavingHours"].sum())
            if not batch_summary_df.empty else 0.0
        )

        avg_ground_start_km = weighted_average(assignments_df, "GroundStartKM", "AssignedFlow")
        avg_air_km = weighted_average(assignments_df, "AirKM", "AssignedFlow")
        avg_ground_end_km = weighted_average(assignments_df, "GroundEndKM", "AssignedFlow")
        avg_uam_total_travel_km = avg_ground_start_km + avg_air_km + avg_ground_end_km
        avg_direct_ground_km_for_uam_users = weighted_average(assignments_df, "DirectGroundKM", "AssignedFlow")
        avg_time_saving_hours_for_uam_users = weighted_average(assignments_df, "TimeSavingHours", "AssignedFlow")
        avg_unit_cost_saving_for_uam_users = weighted_average(assignments_df, "UnitCostSaving", "AssignedFlow")

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
            "TotalDirectGroundBaselineCost": total_direct_ground_baseline_cost,
            "TotalSystemGeneralizedCost": total_system_generalized_cost,
            "TotalSavingsVsDirectGround": total_savings_vs_direct_ground,
            "TotalPassengerTimeSavingHours": total_passenger_time_saving_hours,
            "TotalDemand": total_demand,
            "TotalUAMServed": total_uam_served,
            "TotalGroundOnly": total_ground_only,
            "UAMShare": total_uam_served / total_demand if total_demand > 0 else 0.0,
            "AvgGroundStartKM": avg_ground_start_km,
            "AvgAirKM": avg_air_km,
            "AvgGroundEndKM": avg_ground_end_km,
            "AvgUAMTotalTravelKM": avg_uam_total_travel_km,
            "AvgDirectGroundKMForUAMUsers": avg_direct_ground_km_for_uam_users,
            "AvgTimeSavingHoursForUAMUsers": avg_time_saving_hours_for_uam_users,
            "AvgUnitCostSavingForUAMUsers": avg_unit_cost_saving_for_uam_users,
            "UsedVertiports": len(used_vertiports),
            "UsedRoutes": len(used_routes),
            "TotalFlights": total_flights,
            "AverageLoadFactor": average_load_factor,
            "OptimalBatches": optimal_batches,
            "TimeLimitBatches": time_limit_batches,
            "NoSolutionBatches": no_solution_batches,
        }

        all_scenario_summaries.append(scenario_summary)

        print("\n  当前场景结果已保存:")
        print(f"    {assignments_file}")
        print(f"    {orders_file}")
        print(f"    {routes_file}")
        print(f"    {batch_summary_file}")


# ============================================================
# 9. 保存所有场景汇总
# ============================================================

scenario_summary_df = pd.DataFrame(
    all_scenario_summaries,
    columns=SCENARIO_SUMMARY_COLUMNS,
)

scenario_summary_file = OUTPUT_ROOT / "scenario_summary.csv"
scenario_summary_df.to_csv(scenario_summary_file, index=False)

print("\n============================================================")
print("单因素敏感性分析运行完成。")
print(f"分析因素: {SINGLE_FACTOR_NAME}")
print(f"结果目录: {OUTPUT_ROOT}")
print(f"总汇总文件: {scenario_summary_file}")
print("============================================================")

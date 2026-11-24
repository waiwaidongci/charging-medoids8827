from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations, product
from math import fsum, inf


STEP_MINUTES = 5
OFFSETS = tuple(range(0, 40, STEP_MINUTES))
START_FORMAT = '%Y-%m-%d %H:%M'
MAX_POWER_KW = 600
DEFAULT_CLUSTERS = 3
MAX_CLUSTERS = 8
DEFAULT_BAND = 1
KWH_PER_SLOT = STEP_MINUTES / 60
MAX_COMBINATIONS = 200_000


@dataclass
class Session:
    bus_id: str
    depot: str
    started_at: str
    readings: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))

    def sampling_issues(self) -> list[str]:
        categories = (
            ('缺少', [offset for offset in OFFSETS if offset not in self.readings]),
            ('重复', [offset for offset in sorted(self.readings) if len(self.readings[offset]) > 1]),
            ('超出采样网格', sorted(self.readings.keys() - set(OFFSETS))),
        )
        return [f'{kind} {"、".join(map(str, offsets))} 分钟点' for kind, offsets in categories if offsets]

    def curve(self) -> tuple[float, ...]:
        return tuple(self.readings[offset][0] for offset in OFFSETS)


def sample_rows() -> list[dict]:
    profiles = (
        (105, 126, 138, 142, 144, 142, 138, 133),
        (122, 121, 114, 101, 85, 68, 52, 40),
        (46, 48, 49, 48, 47, 46, 45, 44),
    )
    first_start = datetime(2026, 11, 24, 0, 5)
    rows = []
    for profile_index, profile in enumerate(profiles):
        for variant, adjustment in enumerate((-6, -1, 2, 5)):
            index = profile_index * 4 + variant
            started_at = first_start + timedelta(minutes=10 * index)
            for offset, power in zip(OFFSETS, profile):
                rows.append({
                    'session_id': f'C1124-{index + 1:02d}',
                    'bus_id': f'滨B{301 + index}',
                    'depot': ('东桥场', '南环场')[index % 2],
                    'started_at': started_at.strftime(START_FORMAT),
                    'minute_offset': offset,
                    'power_kw': float(power + adjustment),
                })
    return rows


def _load_sessions(rows: list[dict]) -> dict[str, Session]:
    sessions = {}
    for line, row in enumerate(rows, start=2):
        session_id = row.get('session_id')
        bus_id = row.get('bus_id')
        depot = row.get('depot')
        started_at = row.get('started_at')
        if any(not isinstance(value, str) or not value.strip() for value in (session_id, bus_id, depot)):
            raise ValueError(f'第 {line} 行缺少会话、车辆或场站标识，请核对调度台账后重新导出。')
        try:
            start = datetime.strptime(started_at, START_FORMAT)
            if start.isoformat(sep=' ', timespec='minutes') != started_at:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError(f'第 {line} 行会话 {session_id} 的起充时间无效，请填写 YYYY-MM-DD HH:MM。') from None

        offset = row.get('minute_offset')
        if type(offset) is not int:
            raise ValueError(f'第 {line} 行会话 {session_id} 的相对分钟无效，请填写整数分钟。')
        power = row.get('power_kw')
        if type(power) not in (int, float) or not 0 <= power <= MAX_POWER_KW:
            raise ValueError(f'第 {line} 行会话 {session_id} 的功率无效，请核对 {offset} 分钟点并填写 0–{MAX_POWER_KW} kW 的实测值。')

        session = sessions.get(session_id)
        if session is None:
            session = sessions[session_id] = Session(bus_id, depot, started_at)
        elif (bus_id, depot, started_at) != (session.bus_id, session.depot, session.started_at):
            raise ValueError(f'第 {line} 行会话 {session_id} 的车辆、场站或起充时间与台账冲突，请核对会话编号后重新导出。')
        session.readings[offset].append(float(power))
    return sessions


def _dtw(left: tuple[float, ...], right: tuple[float, ...], band: int) -> float:
    """Minimum cumulative absolute power difference on the band-limited grid."""
    previous = [0.0] + [inf] * len(right)
    for i, power in enumerate(left, start=1):
        current = [inf] * (len(right) + 1)
        for j in range(max(1, i - band), min(len(right), i + band) + 1):
            current[j] = abs(power - right[j - 1]) + min(previous[j - 1], previous[j], current[j - 1])
        previous = current
    return previous[-1]


def _k_medoids(distances: list[list[float]], count: int) -> list[int]:
    """PAM BUILD and best improving SWAP, using session order to break ties."""
    size = len(distances)
    medoids = []
    remaining = list(range(size))
    nearest = [inf] * size
    for _ in range(count):
        medoid = min(remaining, key=lambda candidate: fsum(
            min(distance, row[candidate]) for row, distance in zip(distances, nearest)
        ))
        medoids.append(medoid)
        remaining.remove(medoid)
        nearest = [min(distance, row[medoid]) for row, distance in zip(distances, nearest)]

    while remaining:
        medoids.sort()
        closest = []
        for row in distances:
            ordered = sorted(medoids, key=row.__getitem__)
            first = ordered[0]
            second = row[ordered[1]] if count > 1 else inf
            closest.append((first, row[first], second))
        best_cost = fsum(distance for _, distance, _ in closest)
        best_swap = None
        for outgoing in medoids:
            retained = [second if first == outgoing else distance for first, distance, second in closest]
            for incoming in remaining:
                cost = fsum(min(distance, row[incoming]) for row, distance in zip(distances, retained))
                if cost < best_cost:
                    best_cost = cost
                    best_swap = outgoing, incoming
        if best_swap is None:
            break
        outgoing, incoming = best_swap
        medoids.remove(outgoing)
        medoids.append(incoming)
        remaining.remove(incoming)
        remaining.append(outgoing)
        remaining.sort()
    return sorted(medoids)


def compute(rows: list[dict], options: dict) -> dict:
    count = options.get('cluster_count', DEFAULT_CLUSTERS)
    band = options.get('warp_band', DEFAULT_BAND)
    if type(count) is not int or not 1 <= count <= MAX_CLUSTERS:
        raise ValueError(f'请将分群数设为 1–{MAX_CLUSTERS} 的整数。')
    if type(band) is not int or not 0 <= band < len(OFFSETS):
        raise ValueError(f'请将对齐带宽设为 0–{len(OFFSETS) - 1} 的整数采样格。')
    if not rows:
        raise ValueError('请上传充电采样 CSV，至少提供一条完整会话，并使完整会话数达到分群数。')

    sessions = _load_sessions(rows)
    checks = {key: sessions[key].sampling_issues() for key in sorted(sessions)}
    complete_ids = [key for key, issues in checks.items() if not issues]
    size = len(complete_ids)
    if size < count:
        examples = [f'{key}（{"；".join(issues)}）' for key, issues in checks.items() if issues][:3]
        detail = '。待修正示例：' + '，'.join(examples) if examples else ''
        raise ValueError(f'完整会话仅 {size} 条，无法分为 {count} 群{detail}。请补齐采样、修正重复或网格错误；也可将分群数调至完整会话数以内。')

    curves = [sessions[key].curve() for key in complete_ids]
    distances = [[0.0] * size for _ in curves]
    for i, j in combinations(range(size), 2):
        distances[i][j] = distances[j][i] = _dtw(curves[i], curves[j], band)
    medoids = _k_medoids(distances, count)

    assignments = {}
    cluster_distances = {medoid: [] for medoid in medoids}
    cluster_labels = {medoid: f'第 {number} 群' for number, medoid in enumerate(medoids, start=1)}
    for index, (key, row) in enumerate(zip(complete_ids, distances)):
        ordered = sorted(medoids, key=lambda medoid: (row[medoid], medoid != index, medoid))
        medoid = ordered[0]
        runner_up = ordered[1] if count > 1 else None
        assignments[key] = medoid, row[medoid], runner_up, None if runner_up is None else row[runner_up]
        cluster_distances[medoid].append(row[medoid])

    means = {medoid: fsum(values) / len(values) for medoid, values in cluster_distances.items()}
    maxima = {medoid: max(values) for medoid, values in cluster_distances.items()}
    output_rows = []
    for key, issues in checks.items():
        session = sessions[key]
        record = {
            '会话编号': key,
            '车辆编号': session.bus_id,
            '场站': session.depot,
            '起充时间': session.started_at,
            '采样状态': '待修正' if issues else '完整',
            '有效采样点': sum(len(session.readings.get(offset, ())) == 1 for offset in OFFSETS),
            '采样问题': '；'.join(issues),
            '功率轨迹（kW）': '',
            '所属分群': '',
            '中心会话': '',
            '是否中心': '',
            '到中心DTW': None,
            '次近中心': '',
            '到次近中心DTW': None,
            '分配距离差': None,
            '组内会话数': None,
            '组内平均DTW': None,
            '组内最大DTW': None,
            '归属依据': '修正采样后重新分群。',
        }
        if not issues:
            medoid, distance, runner_up, runner_up_distance = assignments[key]
            is_medoid = key == complete_ids[medoid]
            margin = None if runner_up_distance is None else runner_up_distance - distance
            if is_medoid:
                reason = '本会话为实测中心，归入自身分群。'
            elif runner_up is None:
                reason = '本次仅设置一个分群。'
            elif margin == 0:
                reason = '存在并列最近中心，按中心会话编号升序归组。'
            else:
                reason = '到本群中心的 DTW 距离小于其余中心。'
            record.update({
                '功率轨迹（kW）': ' / '.join(str(power) for power in session.curve()),
                '所属分群': cluster_labels[medoid],
                '中心会话': complete_ids[medoid],
                '是否中心': '是' if is_medoid else '否',
                '到中心DTW': distance,
                '次近中心': '' if runner_up is None else complete_ids[runner_up],
                '到次近中心DTW': runner_up_distance,
                '分配距离差': margin,
                '组内会话数': len(cluster_distances[medoid]),
                '组内平均DTW': means[medoid],
                '组内最大DTW': maxima[medoid],
                '归属依据': reason,
            })
        output_rows.append(record)

    excluded = len(sessions) - size
    headline = f'{size} 条完整充电会话分为 {count} 群'
    if excluded:
        headline += f'，{excluded} 条会话待修正，报告暂缓发布'
    return {
        'headline': headline,
        'metrics': {
            '采样记录数': len(rows),
            '会话总数': len(sessions),
            '完整会话数': size,
            '待修正会话数': excluded,
            '完整会话比例（%）': round(100 * size / len(sessions), 2),
            '分群数': count,
            '对齐带宽（分钟）': band * STEP_MINUTES,
            '组内DTW总距离': fsum(distance for _, distance, _, _ in assignments.values()),
        },
        'rows': output_rows,
        'notes': [
            '每条会话须有起充后 0、5、10、15、20、25、30、35 分钟各一个实测点。待修正会话保留在清单，重导完整批次后方可发布。',
            f'DTW 从首点走到末点，允许横、竖、斜步，仅在相差 {band * STEP_MINUTES} 分钟以内对齐。距离累加匹配点的绝对功率差（kW），保留功率规模。',
            'PAM 按会话编号确定初始选择与同分交换的顺序，每次执行使总距离降幅最大的单中心交换，直到没有改进。',
            '组内平均和最大 DTW 按成员到中心的距离计算，平均值计入中心自身的零距离。功率轨迹按相对分钟升序展示。',
            '分配距离差等于次近中心距离减去所属中心距离。差值为 0 表示并列，中心保留自身归属，其余会话选编号最小的最近中心。',
        ],
        'chart': {
            'label': '各实测中心的组内平均 DTW',
            'unit': 'kW（累计差）',
            'points': [
                {'label': f'{cluster_labels[medoid]} · {complete_ids[medoid]}', 'value': means[medoid]}
                for medoid in medoids
            ],
        },
        'can_publish': excluded == 0,
        'centers': [
            {'label': cluster_labels[medoid], 'session_id': complete_ids[medoid], 'curve': list(curves[medoid])}
            for medoid in medoids
        ],
    }


def _assign(curve: tuple[float, ...], centers: list[dict], band: int):
    """Nearest and runner-up among reused reviewed centers; ties broken by center order."""
    distances = [(_dtw(curve, tuple(center['curve']), band), index) for index, center in enumerate(centers)]
    ordered = sorted(distances)
    first_distance, first = ordered[0]
    if len(ordered) > 1:
        runner_distance, runner = ordered[1]
    else:
        runner_distance, runner = None, None
    return first, first_distance, runner, runner_distance


def _spread(low: float, high: float):
    return low if low == high else f'{round(low, 6)} ~ {round(high, 6)}'


def reconcile(rows: list[dict], options: dict, centers: list[dict], meters: dict, base_id: int) -> dict:
    """Resolve retransmission conflicts against same-batch same-depot meter records.

    Each conflicted sampling slot takes exactly one original value; a combination is
    feasible when the depot energy converted at five minutes per slot falls within the
    meter tolerance. Only sessions assigned to the same reviewed center under every
    feasible combination are included in the shift report.
    """
    band = options.get('warp_band', DEFAULT_BAND)
    if not rows:
        raise ValueError('请上传充电采样 CSV，至少提供一条会话。')
    if not centers:
        raise ValueError('缺少已复核的实测中心，无法对账。')
    sessions = _load_sessions(rows)
    order = sorted(sessions)
    meta = {}
    for key in order:
        readings = sessions[key].readings
        missing = [offset for offset in OFFSETS if offset not in readings]
        off_grid = sorted(readings.keys() - set(OFFSETS))
        conflicts = [offset for offset in OFFSETS if len(set(readings.get(offset, ()))) > 1]
        issues = []
        if missing:
            issues.append(f'缺少 {"、".join(map(str, missing))} 分钟点')
        if conflicts:
            issues.append(f'重发冲突 {"、".join(map(str, conflicts))} 分钟点')
        if off_grid:
            issues.append(f'超出采样网格 {"、".join(map(str, off_grid))} 分钟点')
        meta[key] = {
            'issues': issues,
            'broken': bool(missing or off_grid),
            'conflicted': bool(conflicts) and not (missing or off_grid),
        }

    groups = defaultdict(list)
    for key in order:
        session = sessions[key]
        groups[(session.started_at[:10], session.depot)].append(key)

    outcomes = {}
    meter_refs = set()
    for (batch, depot), keys in sorted(groups.items()):
        conflicted = [key for key in keys if meta[key]['conflicted']]
        if not conflicted:
            continue
        broken = [key for key in keys if meta[key]['broken']]
        if broken:
            reason = f'同批同场站存在待修正会话（{"、".join(broken[:3])}），全场电量无法折算对账。'
            outcomes.update({key: {'status': '无法对账', 'reason': reason} for key in conflicted})
            continue
        meter = meters.get((batch, depot))
        if meter is None:
            reason = f'缺少 {batch} {depot} 的表计记录，请先在起充采样台账登记。'
            outcomes.update({key: {'status': '无法对账', 'reason': reason} for key in conflicted})
            continue
        meter_refs.add(meter['id'])
        slots = []
        fixed_power = 0.0
        for key in keys:
            readings = sessions[key].readings
            for offset in OFFSETS:
                values = sorted(set(readings[offset]))
                if len(values) > 1:
                    slots.append((key, offset, values))
                else:
                    fixed_power += values[0]
        space = 1
        for _, _, values in slots:
            space *= len(values)
        if space > MAX_COMBINATIONS:
            reason = f'重发组合共 {space} 种，超出单次核算上限 {MAX_COMBINATIONS}，请先核对源记录减少冲突。'
            outcomes.update({key: {'status': '无法对账', 'reason': reason} for key in conflicted})
            continue
        target = meter['energy_kwh']
        limit = meter['tolerance_kwh'] + 1e-9
        feasible = 0
        lowest = inf
        highest = -inf
        choices = {key: set() for key in conflicted}
        for combo in product(*(values for _, _, values in slots)):
            energy = (fixed_power + fsum(combo)) * KWH_PER_SLOT
            lowest = min(lowest, energy)
            highest = max(highest, energy)
            if abs(energy - target) <= limit:
                feasible += 1
                picked = defaultdict(list)
                for (key, offset, _), value in zip(slots, combo):
                    picked[key].append((offset, value))
                for key in conflicted:
                    choices[key].add(tuple(picked[key]))
        if not feasible:
            reason = (f'按五分钟折算的全场电量在 {round(lowest, 3)}–{round(highest, 3)} kWh 之间，'
                      f'表计记录 #{meter["id"]} 为 {target}±{meter["tolerance_kwh"]} kWh，任何原值组合均超出误差。')
            outcomes.update({key: {'status': '无可行组合', 'reason': reason, 'combos': 0, 'curves': 0}
                             for key in conflicted})
            continue
        for key in conflicted:
            assignments = {}
            for choice in choices[key]:
                picked = dict(choice)
                curve = tuple(picked.get(offset, sessions[key].readings[offset][0]) for offset in OFFSETS)
                assignments[curve] = _assign(curve, centers, band)
            nearest = {assignment[0] for assignment in assignments.values()}
            if len(nearest) == 1:
                distances = [assignment[1] for assignment in assignments.values()]
                runners = {assignment[2] for assignment in assignments.values() if assignment[2] is not None}
                runner_distances = [assignment[3] for assignment in assignments.values() if assignment[3] is not None]
                margins = [assignment[3] - assignment[1] for assignment in assignments.values()
                           if assignment[3] is not None]
                outcomes[key] = {
                    'status': '归属一致', 'center': nearest.pop(),
                    'distance': (min(distances), max(distances)),
                    'runners': runners,
                    'runner_distance': (min(runner_distances), max(runner_distances)) if runner_distances else None,
                    'margin': min(margins) if margins else None,
                    'curves': len(assignments), 'combos': feasible, 'meter_id': meter['id'],
                }
            else:
                labels = '、'.join(sorted({centers[assignment[0]]['label'] for assignment in assignments.values()}))
                outcomes[key] = {
                    'status': '归属不稳',
                    'reason': f'{len(assignments)} 条可行曲线的归属在 {labels} 之间变化，暂不收录。',
                    'curves': len(assignments), 'combos': feasible,
                }

    seats = {index: 0 for index in range(len(centers))}
    included = 0
    output_rows = []
    for key in order:
        session = sessions[key]
        info = meta[key]
        outcome = outcomes.get(key)
        if outcome is None and not info['broken'] and not info['conflicted']:
            first, distance, runner, runner_distance = _assign(session.curve(), centers, band)
            outcome = {'status': '完整无需对账', 'center': first, 'distance': (distance, distance),
                       'runners': {runner} if runner is not None else set(),
                       'runner_distance': (runner_distance, runner_distance) if runner_distance is not None else None,
                       'margin': None if runner_distance is None else runner_distance - distance}
        outcome = outcome or {}
        record = {
            '会话编号': key,
            '车辆编号': session.bus_id,
            '场站': session.depot,
            '起充时间': session.started_at,
            '采样状态': '待修正' if info['broken'] else ('重发冲突' if info['conflicted'] else '完整'),
            '采样问题': '；'.join(info['issues']),
            '对账结论': '待修正' if info['broken'] else outcome['status'],
            '班报收录': '剔除',
            '所属分群': '',
            '中心会话': '',
            '到中心DTW': None,
            '次近中心': '',
            '到次近中心DTW': None,
            '分配距离差': None,
            '可行组合数': outcome.get('combos'),
            '可行曲线数': outcome.get('curves'),
            '归属依据': '',
        }
        if info['broken']:
            record['归属依据'] = '采样缺点或偏离网格，修正源数据后重新对账。'
        elif outcome['status'] in ('完整无需对账', '归属一致'):
            center = centers[outcome['center']]
            runners = outcome['runners']
            record.update({
                '班报收录': '收录',
                '所属分群': center['label'],
                '中心会话': center['session_id'],
                '到中心DTW': _spread(*outcome['distance']),
                '次近中心': (centers[next(iter(runners))]['session_id'] if len(runners) == 1
                            else ('随组合变化' if runners else '')),
                '到次近中心DTW': _spread(*outcome['runner_distance']) if outcome['runner_distance'] else None,
                '分配距离差': outcome['margin'],
                '归属依据': (f'重发冲突按表计记录 #{outcome["meter_id"]} 对账，{outcome["curves"]} 条可行曲线均归入本群。'
                            if outcome['status'] == '归属一致' else '会话完整，直接对照已复核中心归群。'),
            })
            seats[outcome['center']] += 1
            included += 1
        else:
            record['归属依据'] = outcome['reason']
        output_rows.append(record)

    broken_count = sum(1 for key in order if meta[key]['broken'])
    conflicted_count = sum(1 for key in order if meta[key]['conflicted'])
    stable = sum(1 for key in order if outcomes.get(key, {}).get('status') == '归属一致')
    headline = f'沿用 #{base_id} 已复核中心对账：{included} 条会话归属一致，收录进班报'
    excluded = len(order) - included
    if excluded:
        headline += f'，{excluded} 条剔除并在页面标明原因'
    if broken_count:
        headline += f'；含 {broken_count} 条待修正会话，报告暂缓发布'
    return {
        'headline': headline,
        'metrics': {
            '采样记录数': len(rows),
            '会话总数': len(order),
            '完整会话数': len(order) - broken_count - conflicted_count,
            '重发冲突会话数': conflicted_count,
            '待修正会话数': broken_count,
            '归属一致会话数': stable,
            '归属不稳会话数': sum(1 for key in order if outcomes.get(key, {}).get('status') == '归属不稳'),
            '无可行组合会话数': sum(1 for key in order if outcomes.get(key, {}).get('status') == '无可行组合'),
            '无法对账会话数': sum(1 for key in order if outcomes.get(key, {}).get('status') == '无法对账'),
            '班报收录会话数': included,
            '中心来源任务': f'#{base_id}',
            '对齐带宽（分钟）': band * STEP_MINUTES,
            '对账表计记录': '、'.join(f'#{ref}' for ref in sorted(meter_refs)) or '无',
        },
        'rows': output_rows,
        'notes': [
            '重发冲突的采样格只取一个原值；同格数值一致的重发记录视为一个原值。',
            f'每个采样格按 {STEP_MINUTES} 分钟折算电量（功率 × {STEP_MINUTES}/60 小时），同批同场站的全场电量须落在表计记录 ± 误差内，不满足的组合不可行。',
            f'沿用 #{base_id} 已复核的实测中心与 {band * STEP_MINUTES} 分钟对齐带宽，不再重新分群；并列最近时按中心顺序归组。',
            '只有在所有可行组合下归属同一中心的会话才收录进班报；无可行组合、归属不稳或无法对账的会话在本页标明原因，不进入班报。',
            '表计更正后，基于旧表计的待发布版本自动失效且不能恢复发布资格；已送达班报原样保留。',
        ],
        'chart': {
            'label': '各已复核中心的收录会话数',
            'unit': '条',
            'points': [
                {'label': f"{center['label']} · {center['session_id']}", 'value': seats[index]}
                for index, center in enumerate(centers)
            ],
        },
        'can_publish': not broken_count and included > 0,
        'meter_refs': sorted(meter_refs),
        'centers': centers,
        'centers_source': base_id,
    }

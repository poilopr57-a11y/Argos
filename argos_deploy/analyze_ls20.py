"""
analyze_ls20.py — BFS-анализ всех 7 уровней ls20.

Запускать через .venv_arc:
  F:\\debug\\argoss\\.venv_arc\\Scripts\\python.exe analyze_ls20.py

Извлекает позиции игрока/цели/стен/модификаторов из ls20.py и строит
оптимальные пути для каждого уровня. Поддерживает уровни с несколькими целями
(каждая со своими требованиями).

Результат используется в arc_play.py:_smart_runner_script_ls20() как
hardcoded sequences для всех 7 уровней.
"""
import importlib.util
from collections import deque
from pathlib import Path

LS20_PATH = Path(__file__).parent / "environment_files" / "ls20" / "9607627b" / "ls20.py"

spec = importlib.util.spec_from_file_location("ls20", str(LS20_PATH))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

levels = m.levels
COLORS = [m.epqvqkpffo, m.jninpsotet, m.bejggpjowv, m.tqogkgimes]  # [12, 9, 14, 8]
ROTS   = [0, 90, 180, 270]
STEP   = 5  # размер спрайта игрока


def bfs_multi(start_x, start_y, start_ri, start_ci, start_si,
              walls, goals_reqs, rot_ch, col_ch, shape_ch, max_depth=80):
    """
    BFS по состоянию (x, y, rot_i, col_i, shape).

    walls: set of (x,y) — позиции стен (ihdgageizm).
    goals_reqs: dict {(x,y): (goal_ri, goal_ci, goal_si)} — каждая цель со своими
                требованиями к ротации/цвету/форме.
    rot_ch / col_ch / shape_ch: set of (x,y) — модификаторы.

    Returns: list of action_ids [1..4] (1=up, 2=down, 3=left, 4=right) или None.
    """
    MOVES = {1: (0, -STEP), 2: (0, STEP), 3: (-STEP, 0), 4: (STEP, 0)}
    start = (start_x, start_y, start_ri, start_ci, start_si)
    queue = deque([(start, [])])
    visited = {start}

    while queue:
        (x, y, ri, ci, si), path = queue.popleft()
        if len(path) >= max_depth:
            continue

        for act, (dx, dy) in MOVES.items():
            nx, ny = x + dx, y + dy
            nri, nci, nsi = ri, ci, si

            if (nx, ny) in walls:
                continue

            # Модификаторы применяются при входе на клетку
            if (nx, ny) in rot_ch:
                nri = (ri + 1) % 4
            if (nx, ny) in col_ch:
                nci = (ci + 1) % len(COLORS)
            if (nx, ny) in shape_ch:
                nsi = (nsi + 1) % 6

            # Цель?
            if (nx, ny) in goals_reqs:
                gri, gci, gsi = goals_reqs[(nx, ny)]
                if nsi == gsi and nci == gci and nri == gri:
                    return path + [act]
                else:
                    continue  # цель блокирует — атрибуты не совпадают

            ns = (nx, ny, nri, nci, nsi)
            if ns not in visited:
                visited.add(ns)
                queue.append((ns, path + [act]))

    return None


def extract_level(lvl):
    """Извлекает данные уровня: позиции, цели, модификаторы, требования."""
    sprites_list = lvl._sprites if hasattr(lvl, '_sprites') else []

    walls = set()
    player_pos = None
    goals_raw = []
    rot_ch = set()
    col_ch = set()
    shape_ch = set()
    bouncy = set()

    for spr in sprites_list:
        tags = spr.tags or []
        x, y = spr.x, spr.y
        if 'ihdgageizm' in tags:
            walls.add((x, y))
        elif 'rjlbuycveu' in tags:
            goals_raw.append((x, y))
        elif 'sfqyzhzkij' in tags:
            player_pos = (x, y)
        elif 'rhsxkxzdjz' in tags:
            rot_ch.add((x, y))
        elif 'soyhouuebz' in tags:
            col_ch.add((x, y))
        elif 'ttfwljgohq' in tags:
            shape_ch.add((x, y))
        elif 'npxgalaybz' in tags:
            bouncy.add((x, y))

    start_rot   = lvl.get_data("StartRotation") or 0
    start_col   = lvl.get_data("StartColor")    or 9
    start_shape = lvl.get_data("StartShape")    or 5
    goal_rot    = lvl.get_data("GoalRotation")  or 0
    goal_col    = lvl.get_data("GoalColor")     or 9
    goal_shape  = lvl.get_data("kvynsvxbpi")    or 5
    step_dec    = lvl.get_data("StepsDecrement")
    step_count  = lvl.get_data("StepCounter") or 42

    # Списки для уровней с несколькими целями
    if isinstance(goal_rot,   int): goal_rot   = [goal_rot]   * len(goals_raw)
    if isinstance(goal_col,   int): goal_col   = [goal_col]   * len(goals_raw)
    if isinstance(goal_shape, int): goal_shape = [goal_shape] * len(goals_raw)

    return {
        'walls': walls,
        'player_pos': player_pos,
        'goals_raw': goals_raw,
        'rot_ch': rot_ch,
        'col_ch': col_ch,
        'shape_ch': shape_ch,
        'bouncy': bouncy,
        'start_rot': start_rot,
        'start_col': start_col,
        'start_shape': start_shape,
        'goal_rot_list': goal_rot,
        'goal_col_list': goal_col,
        'goal_shape_list': goal_shape,
        'step_decrement': step_dec if step_dec is not None else 2,  # default 2
        'step_counter': step_count,
    }


def analyze_all_levels():
    """Анализирует все 7 уровней и возвращает BFS-пути."""
    all_sequences = []
    print(f"Total levels: {len(levels)}\n")

    for li, lvl in enumerate(levels):
        info = extract_level(lvl)

        # Индексы для BFS
        start_ri = ROTS.index(info['start_rot'])   if info['start_rot']   in ROTS   else 0
        start_ci = COLORS.index(info['start_col']) if info['start_col']   in COLORS else 0
        start_si = info['start_shape']

        # goals_reqs: (x,y) -> (ri, ci, si)
        goals_reqs = {}
        for gi, gpos in enumerate(info['goals_raw']):
            gr = info['goal_rot_list'][gi]   if gi < len(info['goal_rot_list'])   else info['goal_rot_list'][0]
            gc = info['goal_col_list'][gi]   if gi < len(info['goal_col_list'])   else info['goal_col_list'][0]
            gs = info['goal_shape_list'][gi] if gi < len(info['goal_shape_list']) else info['goal_shape_list'][0]
            gri = ROTS.index(gr)   if gr in ROTS   else 0
            gci = COLORS.index(gc) if gc in COLORS else 0
            goals_reqs[gpos] = (gri, gci, gs)

        # Жизнеспособность: max actions per life = step_counter // step_decrement + 1
        per_life = (info['step_counter'] // info['step_decrement']) + 1

        print(f"=== Level {li+1} ===")
        print(f"  player={info['player_pos']}, goals={info['goals_raw']}")
        print(f"  rot_ch={info['rot_ch']}, col_ch={info['col_ch']}, shape_ch={info['shape_ch']}")
        print(f"  bouncy={info['bouncy']} (refill step counter)")
        print(f"  start: rot={info['start_rot']}(ri={start_ri}), col={info['start_col']}(ci={start_ci}), shape={start_si}")
        print(f"  StepCounter={info['step_counter']}, StepsDecrement={info['step_decrement']}")
        print(f"  Max actions per life: {per_life}, total (3 lives): {per_life * 3}")

        if info['player_pos'] is None or not goals_reqs:
            print("  SKIP: no player/goal\n")
            all_sequences.append([])
            continue

        seq = bfs_multi(
            info['player_pos'][0], info['player_pos'][1],
            start_ri, start_ci, start_si,
            info['walls'], goals_reqs,
            info['rot_ch'], info['col_ch'], info['shape_ch'],
            max_depth=100,
        )

        if seq:
            ok = "OK" if len(seq) <= per_life else f"TOO LONG (>{per_life}/life)"
            print(f"  BFS: {len(seq)} actions [{ok}]")
            print(f"  path: {seq}")
            all_sequences.append(seq)
        else:
            print("  BFS: NO PATH FOUND")
            all_sequences.append([])
        print()

    return all_sequences


if __name__ == "__main__":
    sequences = analyze_all_levels()

    print("\n=== HARDCODED SEQUENCES (для arc_play.py) ===")
    print("L = {")
    for i, seq in enumerate(sequences):
        print(f"    {i+1}: {seq},  # {len(seq)} actions")
    print("}")

    total = sum(len(s) for s in sequences)
    print(f"\nTotal: {total} actions for all 7 levels")

"""
BFS с правильной 5x5 коллизией игрока + bouncy pad refill step counter.

Ключевое исправление: игрок 5x5 захватывает спрайты в боксе [px, px+5) × [py, py+5).
Off-grid bouncy pads (вроде (15, 16)) триггерятся когда player на (14, 15)!
"""
import importlib.util
from collections import deque
from pathlib import Path

LS20_PATH = Path(__file__).parent / "environment_files" / "ls20" / "9607627b" / "ls20.py"
spec = importlib.util.spec_from_file_location("ls20", str(LS20_PATH))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

STEP = 5
COLORS = [m.epqvqkpffo, m.jninpsotet, m.bejggpjowv, m.tqogkgimes]
ROTS   = [0, 90, 180, 270]


def player_box_contains(px, py, sx, sy):
    """True если top-left спрайта (sx, sy) попадает в bbox игрока [px, px+5) × [py, py+5)."""
    return px <= sx < px + STEP and py <= sy < py + STEP


def collisions_at(px, py, sprites_set):
    """Возвращает все спрайты из sprites_set которые попадают в bbox игрока."""
    return {pos for pos in sprites_set if player_box_contains(px, py, pos[0], pos[1])}


def bfs_with_bouncy(player_pos, walls, goals_reqs, rot_ch, col_ch, shape_ch, bouncy,
                    start_ri, start_ci, start_si,
                    step_counter, step_decrement, lives=3, max_depth=80):
    """
    BFS с учётом step counter, bouncy refill, и многоразовых жизней.

    state: (x, y, ri, ci, si, steps_left, lives, frozenset_used_bouncy_keys)
    """
    MOVES = {1: (0, -STEP), 2: (0, STEP), 3: (-STEP, 0), 4: (STEP, 0)}

    start = (player_pos[0], player_pos[1], start_ri, start_ci, start_si,
             step_counter, lives, frozenset())
    queue = deque([(start, [])])
    visited = {start}

    while queue:
        state, path = queue.popleft()
        x, y, ri, ci, si, steps, lvs, used = state

        if len(path) >= max_depth:
            continue

        for act, (dx, dy) in MOVES.items():
            nx, ny = x + dx, y + dy

            # Проверка стен по 5x5 коллизии (но стены 5x5 grid-aligned, проще проверять exact)
            wall_hits = collisions_at(nx, ny, walls)
            if wall_hits:
                continue

            nri, nci, nsi = ri, ci, si
            nsteps = steps - step_decrement
            nlives = lvs
            nused = used

            # Модификаторы по 5x5 коллизии
            if collisions_at(nx, ny, rot_ch):
                nri = (ri + 1) % 4
            if collisions_at(nx, ny, col_ch):
                nci = (ci + 1) % len(COLORS)
            if collisions_at(nx, ny, shape_ch):
                nsi = (si + 1) % 6

            # Bouncy pad: рефилит counter и удаляет спрайт
            bouncy_hits = collisions_at(nx, ny, bouncy)
            unused_bouncy = bouncy_hits - used
            bounced = False
            if unused_bouncy:
                nsteps = step_counter  # refill
                nused = used | unused_bouncy
                bounced = True

            # Цель?
            goal_hits = collisions_at(nx, ny, set(goals_reqs.keys()))
            if goal_hits:
                gpos = next(iter(goal_hits))
                gri, gci, gsi = goals_reqs[gpos]
                if nsi == gsi and nci == gci and nri == gri:
                    return path + [act]
                else:
                    continue  # цель блокирует

            # Смерть?
            if nsteps < 0 and not bounced:
                if nlives <= 1:
                    continue  # game over
                nlives -= 1
                nx, ny = player_pos
                nri, nci, nsi = start_ri, start_ci, start_si
                nsteps = step_counter
                nused = frozenset()  # bouncy восстанавливаются

            new_state = (nx, ny, nri, nci, nsi, nsteps, nlives, nused)
            if new_state not in visited:
                visited.add(new_state)
                queue.append((new_state, path + [act]))

    return None


def extract_level(lvl):
    sprites_list = lvl._sprites if hasattr(lvl, '_sprites') else []
    walls, goals, rot_ch, col_ch, shape_ch, bouncy = set(), [], set(), set(), set(), set()
    player_pos = None
    for spr in sprites_list:
        tags = spr.tags or []
        pos = (spr.x, spr.y)
        if 'ihdgageizm' in tags:
            walls.add(pos)
        elif 'rjlbuycveu' in tags:
            goals.append(pos)
        elif 'sfqyzhzkij' in tags:
            player_pos = pos
        elif 'rhsxkxzdjz' in tags:
            rot_ch.add(pos)
        elif 'soyhouuebz' in tags:
            col_ch.add(pos)
        elif 'ttfwljgohq' in tags:
            shape_ch.add(pos)
        elif 'npxgalaybz' in tags:
            bouncy.add(pos)

    sr   = lvl.get_data("StartRotation") or 0
    sc   = lvl.get_data("StartColor")    or 9
    sshp = lvl.get_data("StartShape")    or 5
    gr   = lvl.get_data("GoalRotation")  or 0
    gc   = lvl.get_data("GoalColor")     or 9
    gshp = lvl.get_data("kvynsvxbpi")    or 5
    sd   = lvl.get_data("StepsDecrement")
    sct  = lvl.get_data("StepCounter")   or 42

    if isinstance(gr, int):   gr   = [gr]   * len(goals)
    if isinstance(gc, int):   gc   = [gc]   * len(goals)
    if isinstance(gshp, int): gshp = [gshp] * len(goals)

    start_ri = ROTS.index(sr)   if sr in ROTS   else 0
    start_ci = COLORS.index(sc) if sc in COLORS else 0

    goals_reqs = {}
    for gi, gp in enumerate(goals):
        gri = ROTS.index(gr[gi])   if gi < len(gr)   and gr[gi]   in ROTS   else 0
        gci = COLORS.index(gc[gi]) if gi < len(gc)   and gc[gi]   in COLORS else 0
        gsi = gshp[gi]             if gi < len(gshp) else gshp[0]
        goals_reqs[gp] = (gri, gci, gsi)

    return {
        'player_pos': player_pos,
        'walls': walls,
        'goals_reqs': goals_reqs,
        'rot_ch': rot_ch, 'col_ch': col_ch, 'shape_ch': shape_ch,
        'bouncy': bouncy,
        'start_ri': start_ri, 'start_ci': start_ci, 'start_si': sshp,
        'step_counter': sct,
        'step_decrement': sd if sd is not None else 2,
    }


if __name__ == "__main__":
    import time
    levels = m.levels
    sequences = []

    for li, lvl in enumerate(levels):
        info = extract_level(lvl)
        per_life = (info['step_counter'] // info['step_decrement']) + 1

        # Проверяем какие bouncy достижимы (по 5x5 collision)
        reachable_bouncy = set()
        # Имитируем: ходим по grid'у от стартовой позиции и проверяем collision
        px, py = info['player_pos']
        # все возможные grid позиции
        grid_xs = list(range(px % STEP if px % STEP else 0, 64, STEP))
        if px not in grid_xs:
            grid_xs = sorted(set(grid_xs + [px - 5*k for k in range(20) if 0 <= px - 5*k < 64] +
                                          [px + 5*k for k in range(20) if 0 <= px + 5*k < 64]))
        grid_ys = sorted(set([py - 5*k for k in range(20) if 0 <= py - 5*k < 64] +
                             [py + 5*k for k in range(20) if 0 <= py + 5*k < 64]))
        for gx in grid_xs:
            for gy in grid_ys:
                if collisions_at(gx, gy, info['bouncy']):
                    reachable_bouncy |= collisions_at(gx, gy, info['bouncy'])

        print(f"=== Level {li+1} ===")
        print(f"  player={info['player_pos']}, goals={list(info['goals_reqs'].keys())}")
        print(f"  StepCounter={info['step_counter']}, StepsDecrement={info['step_decrement']} -> {per_life}/life")
        print(f"  bouncy: {info['bouncy']}, reachable: {reachable_bouncy}")

        if not info['player_pos'] or not info['goals_reqs']:
            print("  SKIP\n"); sequences.append([]); continue

        t0 = time.time()
        seq = bfs_with_bouncy(
            info['player_pos'], info['walls'], info['goals_reqs'],
            info['rot_ch'], info['col_ch'], info['shape_ch'], info['bouncy'],
            info['start_ri'], info['start_ci'], info['start_si'],
            info['step_counter'], info['step_decrement'],
            lives=3, max_depth=100,
        )
        dt = time.time() - t0

        if seq:
            print(f"  BFS: {len(seq)} actions ({dt:.1f}s)")
            print(f"  path: {seq}")
            sequences.append(seq)
        else:
            print(f"  BFS: NO PATH ({dt:.1f}s)")
            sequences.append([])
        print()

    print("\n=== HARDCODED L = ===")
    for i, s in enumerate(sequences):
        print(f"    {i+1}: {s},  # {len(s)} actions")
    total = sum(len(s) for s in sequences)
    print(f"\nTotal: {total} actions")

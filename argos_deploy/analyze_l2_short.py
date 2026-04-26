"""Поиск решения Level 2 ls20 при ограничении 22 действий/жизнь."""
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

lvl = m.levels[1]  # Level 2
walls = set()
goals_pos = set()
rot_ch = set()
col_ch = set()
shape_ch = set()
bouncy = set()  # npxgalaybz - refills step counter
player_pos = None

for spr in lvl._sprites:
    tags = spr.tags or []
    pos = (spr.x, spr.y)
    if 'ihdgageizm' in tags:
        walls.add(pos)
    elif 'rjlbuycveu' in tags:
        goals_pos.add(pos)
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

# Goal requirement
goal_pos = list(goals_pos)[0]
goal_ri = ROTS.index(270)  # GoalRotation=270
goal_ci = COLORS.index(9)
goal_si = 5  # kvynsvxbpi=5

print(f"Player start: {player_pos}")
print(f"Goal pos: {goal_pos}, requires (ri={goal_ri}, ci={goal_ci}, si={goal_si})")
print(f"Rotation changers: {rot_ch}")
print(f"Bouncy (refill counter): {bouncy}")

# BFS с учетом step counter и lives
# State: (x, y, ri, ci, si, steps_left, lives)
# Death resets x,y,ri,ci,si but keeps level state (used_bouncies)
# Actually after death, removed sprites (bouncies) are restored to ofoahudlo...
# checking code:
# "for iybkldaxol in self.ofoahudlo: self.current_level.add_sprite(iybkldaxol)"
# So bouncies ARE restored on death!

MOVES = {1: (0, -STEP), 2: (0, STEP), 3: (-STEP, 0), 4: (STEP, 0)}
START_STEPS = 42
DECREMENT = 2  # Level 2 default
LIVES = 3
START_RI, START_CI, START_SI = 0, 1, 5

# Multi-life BFS with step counter tracking
# State: (x, y, ri, ci, si, steps_left, lives, frozenset_used_bouncies)
# Used bouncies don't reset within a life but DO reset on death

start_state = (player_pos[0], player_pos[1], START_RI, START_CI, START_SI,
               START_STEPS, LIVES, frozenset())
queue = deque([(start_state, [])])
visited = {start_state}

print("\nRunning multi-life BFS with step counter tracking...")
print(f"  decrement={DECREMENT}, lives={LIVES}, start_steps={START_STEPS}")

iterations = 0
max_path = 0
while queue:
    iterations += 1
    if iterations > 500000:
        print(f"  iterations limit at {len(queue)} states queued, max_path={max_path}")
        break
    state, path = queue.popleft()
    x, y, ri, ci, si, steps, lives, used = state
    if len(path) > max_path:
        max_path = len(path)

    if len(path) > 80:
        continue

    for act, (dx, dy) in MOVES.items():
        nx, ny = x + dx, y + dy
        if (nx, ny) in walls:
            continue

        nri, nci, nsi = ri, ci, si
        nused = used
        nsteps = steps - DECREMENT
        nlives = lives

        # Apply modifiers
        if (nx, ny) in rot_ch:
            nri = (ri + 1) % 4
        if (nx, ny) in col_ch:
            nci = (ci + 1) % len(COLORS)
        if (nx, ny) in shape_ch:
            nsi = (nsi + 1) % 6

        bounce = False
        if (nx, ny) in bouncy and (nx, ny) not in used:
            nsteps = START_STEPS
            nused = used | {(nx, ny)}
            bounce = True

        # Check goal
        if (nx, ny) == goal_pos:
            if nsi == goal_si and nci == goal_ci and nri == goal_ri:
                print(f"\nFOUND PATH! {len(path) + 1} actions: {path + [act]}")
                exit()
            else:
                continue  # blocked by mismatched goal

        # Check death (counter <= -1)
        if nsteps < 0 and not bounce:
            # Player dies
            if nlives <= 1:
                continue  # game over
            nlives -= 1
            nx, ny = player_pos
            nri, nci, nsi = START_RI, START_CI, START_SI
            nsteps = START_STEPS
            nused = frozenset()  # bouncies restored

        new_state = (nx, ny, nri, nci, nsi, nsteps, nlives, nused)
        if new_state not in visited:
            visited.add(new_state)
            queue.append((new_state, path + [act]))

print(f"\nNo path found after {iterations} iterations. Max depth: {max_path}")
print(f"Visited states: {len(visited)}")

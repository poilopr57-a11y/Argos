"""Симуляция BFS-пути Level 3 против реальной механики игры."""
import importlib.util
from pathlib import Path

LS20_PATH = Path(__file__).parent / "environment_files" / "ls20" / "9607627b" / "ls20.py"
spec = importlib.util.spec_from_file_location("ls20", str(LS20_PATH))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

STEP = 5
COLORS = [m.epqvqkpffo, m.jninpsotet, m.bejggpjowv, m.tqogkgimes]
ROTS = [0, 90, 180, 270]

lvl = m.levels[2]  # Level 3 (index 2)

walls = set()
goals = set()
rot_ch = set()
col_ch = set()
shape_ch = set()
bouncy = set()
player_pos = None

for spr in lvl._sprites:
    tags = spr.tags or []
    pos = (spr.x, spr.y)
    if 'ihdgageizm' in tags: walls.add(pos)
    elif 'rjlbuycveu' in tags: goals.add(pos)
    elif 'sfqyzhzkij' in tags: player_pos = pos
    elif 'rhsxkxzdjz' in tags: rot_ch.add(pos)
    elif 'soyhouuebz' in tags: col_ch.add(pos)
    elif 'ttfwljgohq' in tags: shape_ch.add(pos)
    elif 'npxgalaybz' in tags: bouncy.add(pos)

print(f"Player start: {player_pos}")
print(f"Goals: {goals}, requires:")
print(f"  rot={lvl.get_data('GoalRotation')}, col={lvl.get_data('GoalColor')}, shape={lvl.get_data('kvynsvxbpi')}")
print(f"Start: rot={lvl.get_data('StartRotation')}, col={lvl.get_data('StartColor')}, shape={lvl.get_data('StartShape')}")
print(f"Modifiers: rot_ch={rot_ch}, col_ch={col_ch}, shape_ch={shape_ch}")
print(f"Bouncy: {bouncy}")
print(f"Walls: {len(walls)}")
print(f"StepCounter={lvl.get_data('StepCounter')}, StepsDecrement={lvl.get_data('StepsDecrement')}")

# BFS path from arc_play.py
path = [1,1,1,1,1,1,1,1,4,4,4,4,2,2,2,2,2,3,3,4,4,2,2,2,1,1,1,1,1,1,4,2,2,4,4,4,4,1,1,1,3,1,2,2,4,2,2,2,2,2,2,2]

def player_box_contains(px, py, sx, sy):
    return px <= sx < px + STEP and py <= sy < py + STEP

def collisions(px, py, sprites_set):
    return {pos for pos in sprites_set if player_box_contains(px, py, pos[0], pos[1])}

MOVES = {1: (0, -STEP), 2: (0, STEP), 3: (-STEP, 0), 4: (STEP, 0)}

x, y = player_pos
ri = ROTS.index(lvl.get_data("StartRotation") or 0)
ci = COLORS.index(lvl.get_data("StartColor") or 12)
si = lvl.get_data("StartShape") or 5
counter = lvl.get_data("StepCounter") or 42
sd = lvl.get_data("StepsDecrement") or 2  # Level 3: not set → default 2
lives = 3
used_bouncy = set()

goal_rot = lvl.get_data("GoalRotation") or 0
goal_col = lvl.get_data("GoalColor") or 9
goal_shape = lvl.get_data("kvynsvxbpi") or 5
gri = ROTS.index(goal_rot)
gci = COLORS.index(goal_col)
gsi = goal_shape

print(f"\nGoal req: ri={gri}, ci={gci}, si={gsi}\n")
print(f"Decrement={sd}, max actions/life = {counter//sd + 1}")

print(f"Start: ({x},{y}) ri={ri} ci={ci} si={si} counter={counter} lives={lives}")
for i, act in enumerate(path):
    dx, dy = MOVES[act]
    nx, ny = x + dx, y + dy

    wall_hit = collisions(nx, ny, walls)
    if wall_hit:
        print(f"  {i+1:2d}. A{act} ({x},{y})->({nx},{ny}) WALL{wall_hit} BLOCKED")
        # decrement counter even on blocked move? Check game code...
        # Looking at txnfzvzetn: collision checks happen BEFORE counter decrement.
        # If bwdzgjttjp=True, position not updated but action complete()d.
        # mfyzdfvxsm() decrement happens in step() unconditionally after position update.
        counter -= sd
        if counter < 0:
            lives -= 1
            if lives == 0:
                print(f"     *** GAME OVER (life {lives}, action {i+1}) ***")
                break
            x, y = player_pos
            ri = ROTS.index(lvl.get_data("StartRotation") or 0)
            ci = COLORS.index(lvl.get_data("StartColor") or 12)
            si = lvl.get_data("StartShape") or 5
            counter = lvl.get_data("StepCounter") or 42
            used_bouncy = set()
            print(f"     >>> RESET: lives={lives}, pos={player_pos}")
        continue

    nri, nci, nsi = ri, ci, si
    if collisions(nx, ny, rot_ch):
        nri = (ri + 1) % 4
    if collisions(nx, ny, col_ch):
        nci = (ci + 1) % len(COLORS)
    if collisions(nx, ny, shape_ch):
        nsi = (si + 1) % 6

    bounce_hit = collisions(nx, ny, bouncy) - used_bouncy
    bounced = False
    if bounce_hit:
        used_bouncy |= bounce_hit
        counter = lvl.get_data("StepCounter") or 42  # refill
        bounced = True

    goal_hit = collisions(nx, ny, goals)
    if goal_hit:
        if nsi == gsi and nci == gci and nri == gri:
            print(f"  {i+1:2d}. A{act} ({x},{y})->({nx},{ny}) GOAL ENTERED! ri={nri} ci={nci} si={nsi}")
            print(f"     *** LEVEL COMPLETED in {i+1} actions! ***")
            break
        else:
            print(f"  {i+1:2d}. A{act} ({x},{y})->({nx},{ny}) GOAL BLOCKED (need ri={gri}/ci={gci}/si={gsi}, have ri={nri}/ci={nci}/si={nsi})")
            counter -= sd
            continue

    x, y, ri, ci, si = nx, ny, nri, nci, nsi
    if not bounced:
        counter -= sd

    note = ""
    if collisions(nx, ny, rot_ch): note += " ROT"
    if collisions(nx, ny, col_ch): note += " COL"
    if collisions(nx, ny, shape_ch): note += " SHA"
    if bounce_hit: note += f" BOUNCE+ refill"
    print(f"  {i+1:2d}. A{act} ({x},{y}) ri={ri} ci={ci} si={si} cnt={counter}{note}")

    if counter < 0:
        lives -= 1
        if lives == 0:
            print(f"     *** GAME OVER (action {i+1}) ***")
            break
        x, y = player_pos
        ri = ROTS.index(lvl.get_data("StartRotation") or 0)
        ci = COLORS.index(lvl.get_data("StartColor") or 12)
        si = lvl.get_data("StartShape") or 5
        counter = lvl.get_data("StepCounter") or 42
        used_bouncy = set()
        print(f"     >>> RESET: lives={lives}, pos={player_pos}")

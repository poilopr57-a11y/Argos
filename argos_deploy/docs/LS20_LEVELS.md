# ls20 — анализ уровней и BFS-стратегия

## Механика игры (из `environment_files/ls20/9607627b/ls20.py`)

### Игровое поле
- Сетка 64×64 пикселя
- Все позиции в пикселях (top-left corner)
- Игрок (sfqyzhzkij): 5×5 пикселей, движение шагом 5

### Действия
| API | Direction | Effect          |
|-----|-----------|-----------------|
| ACTION1 | up    | y -= 5          |
| ACTION2 | down  | y += 5          |
| ACTION3 | left  | x -= 5          |
| ACTION4 | right | x += 5          |

### Спрайт-теги
| Tag           | Роль                                               |
|---------------|----------------------------------------------------|
| `sfqyzhzkij`  | Игрок (player) — управляется ACTION1-4             |
| `rjlbuycveu`  | Цель (goal) — требует совпадение shape/color/rot   |
| `ihdgageizm`  | Стена — блокирует движение                         |
| `rhsxkxzdjz`  | Rotation changer — `cklxociuu = (cklxociuu+1) % 4` |
| `soyhouuebz`  | Color changer — `hiaauhahz = (hiaauhahz+1) % 4`    |
| `ttfwljgohq`  | Shape changer — `fwckfzsyc = (fwckfzsyc+1) % 6`    |
| `npxgalaybz`  | Bouncy pad — рефилит StepCounter, удаляется        |

### Цвета (`tnkekoeuk`)
```
[epqvqkpffo=12, jninpsotet=9, bejggpjowv=14, tqogkgimes=8]
индексы:  0          1            2             3
```

### Условие победы (`bejndxqqzf`)
Игрок входит на цель, если:
- `fwckfzsyc == kvynsvxbpi` (shape совпадает)
- `hiaauhahz == GoalColor index` (color совпадает)
- `cklxociuu == GoalRotation index` (rotation совпадает)

### Step Counter (жизненный лимит)
- `StepCounter` (default 42) — счётчик шагов на одну жизнь
- `StepsDecrement` (default 2 если не задан) — уменьшение за действие
- Max actions per life = `StepCounter // StepsDecrement + 1`
- 3 жизни (`aqygnziho = 3`)
- При смерти: position/rotation/color/shape сбрасываются, bouncy pads восстанавливаются

## BFS-результаты

| Level | Player    | Goal      | StepsDec | Per life | BFS path | Status |
|-------|-----------|-----------|----------|----------|----------|--------|
| 1     | (34, 45)  | (34, 10)  | **1**    | 43       | 13       | ✅ OK  |
| 2     | (29, 40)  | (14, 40)  | 2        | 22       | 41       | ❌ TOO LONG |
| 3     | (9, 45)   | (54, 50)  | 2        | 22       | 44       | ❌ TOO LONG |
| 4     | (54, 5)   | (9, 5)    | **1**    | 43       | 41       | ✅ OK |
| 5     | (49, 40)  | (54, 5)   | 2        | 22       | 42       | ❌ TOO LONG |
| 6     | (24, 50)  | multi     | **1**    | 43       | 35       | ✅ OK |
| 7     | (19, 15)  | (29, 50)  | 2        | 22       | 55       | ❌ TOO LONG |

**Total**: 271 BFS actions for 7 levels.

### Уровни 1, 4, 6 — `StepsDecrement=1` → 43 действия/жизнь, BFS-путь помещается ✅
### Уровни 2, 3, 5, 7 — `StepsDecrement=2` (default) → 22 действия/жизнь, BFS-путь не помещается ❌

При смерти игрока в уровнях 2/3/5/7 rotation сбрасывается → нельзя «продолжить» прогресс.
Bouncy pads на off-grid позициях (15,16) и (40,51) — недостижимы (player на 5-pixel grid).

## Текущий результат

```
Score: 3.5714% (1/7 уровень)
level_actions: [13, 66, 0, 0, 0, 0, 0]
level_baseline_actions: [22, 123, 73, 84, 96, 192, 186]
state: GAME_OVER (на Level 2)
```

Game Over срабатывает после 3 смертей игрока на Level 2 → дальнейшие уровни недоступны.

## Возможные пути решения для Level 2-7

1. **Frame-aware navigation**: читать фрейм после каждого env.step(), динамически
   рассчитывать путь к цели/модификатору. Может найти решения, которые не видит чистый BFS.

2. **Многоходовая death-стратегия**: если на каждой жизни специально добраться до
   определённой точки (с целью настроить rotation/color), может удастся накопить
   нужное состояние через серию смертей. Но `qetwzqzzik()` сбрасывает состояние —
   нужно проверить что именно сбрасывается.

3. **Использование `akoadfsur` блокировки**: вход на цель с неверными атрибутами
   запускает блокировку движения на N тиков. Step counter в это время не
   декрементируется — теоретически даёт «бесплатные» действия.

4. **Анализ через arcade game source**: возможно есть скрытые механики,
   неочевидные из BFS-модели (например, спрайты с tag-ами не входящими в
   стандартный набор collisions).

## Файлы

- `argos_deploy/arc_play.py` — runtime с BFS-маршрутами
- `argos_deploy/analyze_ls20.py` — генератор BFS-путей
- `argos_deploy/analyze_l2_short.py` — multi-life BFS для Level 2 (доказывает невозможность)
- `argos_deploy/environment_files/ls20/9607627b/ls20.py` — исходник игры

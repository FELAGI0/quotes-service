"""Generate a catalog snapshot file for load testing.

Usage: python scripts/gen_snapshot.py [count] [path]
"""

from __future__ import annotations

import json
import random
import sys

AUTHORS = ["Марк Аврелий", "Сенека", "Эпиктет", "Конфуций", "Лао-цзы", "Сократ"]
TAGS = ["стоицизм", "философия", "античность", "этика", "мудрость", "судьба"]
LANGS = ["ru", "en"]
WORDS = (
    "мудрость время честь путь разум сердце жизнь смерть свобода долг истина "
    "сомнение терпение мужество умеренность справедливость дружба любовь судьба "
    "природа знание память воля покой тревога надежда страх гнев радость печаль "
    "разлука встреча слово молчание дело праздность труд отдых закон обычай "
    "государство народ человек бог мир война стремление препятствие сила слабость"
).split()


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 340_000
    path = sys.argv[2] if len(sys.argv) > 2 else "snapshot.json"
    rng = random.Random(42)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"quotes":[\n')
        for i in range(count):
            record = {
                "id": f"q-{i:07d}",
                "author": rng.choice(AUTHORS),
                "text": " ".join(rng.choice(WORDS) for _ in range(17)),
                "tags": [rng.choice(TAGS)],
                "year": rng.randint(-500, 2020),
                "lang": rng.choice(LANGS),
            }
            f.write(json.dumps(record, ensure_ascii=False))
            f.write(",\n")
        f.write("]}")
    size = __import__("os").path.getsize(path)
    print(f"records={count} bytes={size} (~{size / 1024 / 1024:.1f} MiB) -> {path}")


if __name__ == "__main__":
    main()
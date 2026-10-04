"""Eval cases: loaded from evals/datasets/<name>.yaml (format documented in chinook.yaml)."""
from dataclasses import dataclass

import yaml

from evals.config import DATASETS_DIR


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    tags: tuple[str, ...]
    gold: tuple[str, ...]  # any one matching counts as correct
    cannot_answer: bool = False
    core: bool = False  # in the small set run first, covering every question type


def load_cases(dataset: str) -> list[Case]:
    raw = yaml.safe_load((DATASETS_DIR / f"{dataset}.yaml").read_text(encoding="utf-8"))
    cases = []
    for item in raw["cases"]:
        gold = item.get("gold", ())
        cases.append(
            Case(
                id=item["id"],
                question=item["question"],
                tags=tuple(item["tags"]),
                gold=(gold,) if isinstance(gold, str) else tuple(gold),
                cannot_answer=item.get("cannot_answer", False),
                core=item.get("core", False),
            )
        )
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate case ids in {dataset}.yaml")
    for case in cases:
        if not case.gold and not case.cannot_answer:
            raise ValueError(f"Case {case.id} needs gold SQL or cannot_answer: true")
    return cases

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""publish.py — публикация калибровочных сессий юридического агента в HuggingFace.

Назначение
----------
Отдельный контур публикации для `legal-agent-calibration/` (не путать с
`assignments/00-first-contribution/dataset-schema/publish.py`). Публикует
публичные (редактированные) журналы сессий:

    calibrations/<session_id>/session.jsonl

Поведение
---------
* `huggingface_hub` импортируется **лениво** — без него доступен только dry-run;
* по умолчанию `--repo-id lawful-good-project/legal-agent-calibrations`;
* `--dry-run` ничего не загружает, только показывает план;
* `create_pr=True` по умолчанию: создаётся PR, а не прямая запись в main;
* токен читается **только** из переменной окружения `HF_TOKEN`;
* токен **никогда** не печатается и не логируется;
* публикуется **только** отредактированный экспорт (session.py::export_public),
  секреты/сырые полные тексты/ПД не выгружаются.

Примеры:
  python publish.py --session-dir calibrations/ip-law-2026-001 --dry-run
  HF_TOKEN=... python publish.py --session-dir calibrations/ip-law-2026-001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import session as sess  # noqa: E402  (локальный модуль калибровки)

DEFAULT_REPO_ID = "lawful-good-project/legal-agent-calibrations"
DEFAULT_FILENAME = "session.jsonl"


def _repo_path(session_id: str, filename: str = DEFAULT_FILENAME) -> str:
    return f"calibrations/{session_id}/{filename}"


def validate_journal(path: Path) -> tuple[bool, str, Optional[str]]:
    """Проверяет журнал сессии. Возвращает (ok, msg, session_id)."""
    if not path.is_file():
        return False, f"журнал не найден: {path}", None
    try:
        events = sess.read_jsonl(path)
    except json.JSONDecodeError as e:
        return False, f"некорректный JSONL: {e}", None
    if not events:
        return False, "пустой журнал", None
    errors = sess.validate_session(events)
    if errors:
        return False, "сессия не проходит валидацию: " + "; ".join(errors[:5]), None
    sid = str(events[0].get("session_id") or "")
    return True, "ok", sid


def build_public(session_dir: Path, out_path: Path) -> tuple[bool, str]:
    """Пишет публичный (редактированный) session.jsonl в out_path."""
    journal = session_dir / DEFAULT_FILENAME
    ok, msg, _sid = validate_journal(journal)
    if not ok:
        return False, msg
    events = sess.read_jsonl(journal)
    n = sess.write_public_jsonl(events, out_path)
    return True, f"публичный экспорт: {out_path} (событий: {n})"


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Публикация калибровочных сессий юридического агента в HuggingFace. "
            "Публикуется только публичный (редактированный) экспорт; токен — из HF_TOKEN."
        )
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--session-dir",
        help="каталог сессии с session.jsonl (рабочим журналом)",
    )
    src.add_argument(
        "--journal",
        help="путь к отдельному журналу session.jsonl",
    )
    ap.add_argument("--repo-id", default=DEFAULT_REPO_ID, help="целевой репозиторий HF")
    ap.add_argument(
        "--out-dir",
        default="",
        help="каталог для публичного экспорта (по умолчанию — временный рядом с журналом)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="только показать план, без загрузки"
    )
    ap.add_argument(
        "--no-pr",
        action="store_true",
        help="не создавать PR (прямая запись). По умолчанию create_pr=True",
    )
    ap.add_argument("--title", default="Legal agent calibration sessions")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.session_dir:
        session_dir = Path(args.session_dir)
        journal = session_dir / DEFAULT_FILENAME
    else:
        journal = Path(args.journal)
        session_dir = journal.parent

    ok, msg, sid = validate_journal(journal)
    if not ok:
        print(f"error: {msg}", file=sys.stderr)
        return 1
    assert sid is not None

    out_dir = Path(args.out_dir) if args.out_dir else session_dir / "_public"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_public = out_dir / DEFAULT_FILENAME
    ok2, msg2 = build_public(
        session_dir if args.session_dir else journal.parent, out_public
    )
    if not ok2:
        print(f"error: {msg2}", file=sys.stderr)
        return 1

    repo_path = _repo_path(sid)
    print(f"Сессия: {sid}")
    print(f"Репозиторий: {args.repo_id}")
    print(f"Путь в репозитории: {repo_path}")
    print(msg2)

    create_pr = not args.no_pr
    if args.dry_run:
        print("dry-run: загрузка пропущена.")
        return 0

    token = os.environ.get("HF_TOKEN", "")
    if not token:
        print("Нет HF_TOKEN — загрузка пропущена.", file=sys.stderr)
        return 0

    try:
        from huggingface_hub import HfApi  # ленивый импорт
    except ImportError:
        print(
            "huggingface_hub не установлен (pip install huggingface_hub). "
            "Доступен только --dry-run.",
            file=sys.stderr,
        )
        return 1

    api = HfApi()
    try:
        api.create_repo(repo_id=args.repo_id, repo_type="dataset", exist_ok=True)
        api.upload_file(
            path_or_fileobj=str(out_public),
            path_in_repo=repo_path,
            repo_id=args.repo_id,
            repo_type="dataset",
            create_pr=create_pr,
            commit_message=f"Add calibration session {sid}",
        )
    except Exception as e:  # noqa: BLE001
        # Не выводим токен; только тип ошибки.
        print(f"error: загрузка не удалась: {type(e).__name__}", file=sys.stderr)
        return 1

    where = f"https://huggingface.co/datasets/{args.repo_id}"
    print(f"Загружено: {repo_path} -> {where}" + (" (PR)" if create_pr else " (main)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

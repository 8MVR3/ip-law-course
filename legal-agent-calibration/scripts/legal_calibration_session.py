#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""legal_calibration_session.py — CLI калибровочной сессии юридического эксперта.

Подкоманды
----------
  init     --session-id ID [--journal FILE]        создать журнал сессии
  append   --journal FILE --role R ...             добавить событие
  validate --journal FILE [--json]                 проверить сессию
  summary  --journal FILE [--json]                 сводка по сессии
  export   --journal FILE --out FILE [--dry-run]   публичный (редактированный) экспорт

Последовательность ролей:
  task -> agent_answer -> expert_review ->
    (correction -> agent_answer -> expert_review)* -> final(expert_approved=true)

`init` пишет компаньон-файл `<journal>.sessionid`, чтобы последующие `append`
не требовали повторять `--session-id`.

Коды возврата: 0 — успех; 1 — ошибки валидации/данных; 2 — ошибка аргументов/файла.

Юридическая безопасность: модуль не даёт юридических консультаций и не публикует
данные автоматически; экспорт — только по явной команде и с редактированием.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_DIR = HERE.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import session as sess  # noqa: E402  (локальный модуль калибровки)

PREDICATES = sorted(sess.PREDICATES)
DOC_TYPES = sorted(sess.DOC_TYPES)
JURISDICTIONS = sorted(sess.JURISDICTIONS)


def _today() -> str:
    return date.today().isoformat()


def _print(args, *msg) -> None:
    print(*msg)


def _load_journal(path: Path):
    if not path.is_file():
        print(f"error: журнал не найден: {path}", file=sys.stderr)
        return None
    try:
        events = sess.read_jsonl(path)
    except json.JSONDecodeError as e:
        print(f"error: некорректный JSONL в {path}: {e}", file=sys.stderr)
        return None
    if not events:
        print(f"error: пустой журнал: {path}", file=sys.stderr)
        return None
    return events


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _sid_file(journal: Path) -> Path:
    return journal.with_suffix(journal.suffix + ".sessionid")


def cmd_init(args) -> int:
    journal = Path(args.journal)
    if journal.is_file() and not args.force:
        print(
            f"error: журнал уже существует: {journal} (используйте --force для перезаписи)",
            file=sys.stderr,
        )
        return 2
    if not sess.SESSION_ID_RE.match(args.session_id):
        print(
            "error: --session-id: латиница/цифры/. _ - (3..64 символа), "
            "например 'ip-law-2026-001'",
            file=sys.stderr,
        )
        return 2
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("", encoding="utf-8")
    # Компаньон-файл хранит session_id, чтобы `append` не требовал его повторно.
    _sid_file(journal).write_text(args.session_id + "\n", encoding="utf-8")
    print(f"Сессия создана: {journal}")
    print(f"  session_id: {args.session_id}")
    print("  следующий шаг: append --role task (задание эксперта)")
    return 0


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


def _build_source(args) -> dict:
    return {
        "authority": args.authority,
        "jurisdiction": args.jurisdiction,
        "doc_type": args.doc_type,
        "title": args.title,
        "identifier": args.identifier,
        "effective_date": args.effective_date,
        "coordinate": args.coordinate,
        "url": args.url,
    }


def _build_payload(args) -> dict:
    return {
        "norm_id": args.norm_id,
        "predicate": args.predicate,
        "subject": args.subject,
        "object": args.obj,
        "effective_from": args.effective_from,
        "effective_to": args.effective_to,
        "confidence": args.confidence,
        "change_ru": args.change,
        "note_ru": args.note,
    }


def cmd_append(args) -> int:
    journal = Path(args.journal)
    # Пустой (только что созданный) либо отсутствующий журнал — допустим при init.
    events: list = []
    if journal.is_file():
        try:
            events = sess.read_jsonl(journal)
        except json.JSONDecodeError as e:
            print(f"error: некорректный JSONL в {journal}: {e}", file=sys.stderr)
            return 2
    if not events:
        # session_id: из аргумента, иначе из компаньон-файла после init.
        sid = args.session_id
        if not sid and _sid_file(journal).is_file():
            sid = _sid_file(journal).read_text(encoding="utf-8").strip() or None
        if not sid:
            print(
                "error: журнал отсутствует; укажите --session-id, чтобы создать его",
                file=sys.stderr,
            )
            return 2
        writer = sess.SessionWriter(journal, sid)
    else:
        sid = args.session_id or str(events[0].get("session_id"))
        writer = sess.SessionWriter(journal, sid)

    source = _build_source(args)
    payload = _build_payload(args)
    try:
        ev = writer.append(
            args.role,
            source,
            payload,
            created_at=args.created_at or _today(),
            expert_approved=True if args.role == "final" else None,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(
        f"Событие добавлено: seq={ev['seq']} role={ev['role']} event_id={ev['event_id']}"
    )
    summary = writer.summary()
    print(
        f"  всего событий: {summary['events']}; следующая роль: {summary['next_expected']}"
    )
    if summary["finished"]:
        if summary["expert_approved"]:
            print(
                "  сессия завершена и УТВЕРЖДЕНА экспертом (final, expert_approved=true)."
            )
        else:
            print("  внимание: сессия завершена без утверждения эксперта.")
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def cmd_validate(args) -> int:
    events = _load_journal(Path(args.journal))
    if events is None:
        return 2
    errors = sess.validate_session(events)
    if args.json:
        print(
            json.dumps(
                {
                    "journal": str(args.journal),
                    "events": len(events),
                    "errors": errors,
                    "ok": not errors,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if errors:
            print(f"ОШИБКИ: {len(errors)} (событий: {len(events)})")
            for e in errors:
                print(f"  - {e}")
        else:
            print(
                f"OK: последовательность и события корректны (событий: {len(events)})"
            )
    return 0 if not errors else 1


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def cmd_summary(args) -> int:
    events = _load_journal(Path(args.journal))
    if events is None:
        return 2
    sid = str(events[0].get("session_id"))
    writer = sess.SessionWriter(Path(args.journal), sid)
    s = writer.summary()
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    print(f"Сессия: {s['session_id']}")
    print(f"  событий: {s['events']}")
    print(f"  роли: {' -> '.join(s['roles']) or '—'}")
    print(f"  разборов эксперта: {s['expert_reviews']}; правок: {s['corrections']}")
    print(f"  завершена: {'да' if s['finished'] else 'нет'}")
    print(f"  утверждена экспертом: {'да' if s['expert_approved'] else 'нет'}")
    print(f"  следующая роль: {s['next_expected']}")
    return 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def cmd_export(args) -> int:
    events = _load_journal(Path(args.journal))
    if events is None:
        return 2
    errors = sess.validate_session(events)
    if errors:
        print("error: сессия не проходит валидацию — экспорт отменён:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    out = Path(args.out)
    if args.dry_run:
        public = sess.export_public(events)
        print(f"dry-run: будет выгружено событий: {len(public)} -> {out}")
        for ev in public[:3]:
            print("  " + json.dumps(ev, ensure_ascii=False)[:200])
        return 0
    n = sess.write_public_jsonl(events, out)
    print(f"Публичный экспорт записан: {out} (событий: {n})")
    print(
        "  секреты/сырые тексты/ПД редактированы; это отдельный файл, не рабочий журнал."
    )
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="legal_calibration_session.py",
        description=(
            "Калибровочная сессия юридического эксперта: эксперт задаёт норму и "
            "официальные ориентиры, агент извлекает правовые факты, эксперт "
            "указывает явные ошибки, агент правит до утверждения."
        ),
        epilog=(
            "Последовательность: task -> agent_answer -> expert_review -> "
            "(correction -> agent_answer -> expert_review)* -> final. "
            "Модуль НЕ является юридической консультацией и не публикует данные сам."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_i = sub.add_parser("init", help="создать журнал сессии")
    p_i.add_argument("--session-id", required=True, help="ID сессии (латиница)")
    p_i.add_argument("--journal", required=True, help="путь к JSONL-журналу")
    p_i.add_argument(
        "--force", action="store_true", help="перезаписать существующий журнал"
    )
    p_i.set_defaults(func=cmd_init)

    p_a = sub.add_parser(
        "append",
        help="добавить событие (task/agent_answer/expert_review/correction/final)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_a.add_argument("--journal", required=True)
    p_a.add_argument("--session-id", help="нужен только если журнал ещё не создан")
    p_a.add_argument("--role", required=True, choices=list(sess.ROLES))
    p_a.add_argument(
        "--created-at", help="дата события YYYY-MM-DD (по умолчанию — сегодня)"
    )
    # source
    p_a.add_argument("--authority", required=True, help="орган/нормотворец")
    p_a.add_argument(
        "--jurisdiction",
        required=True,
        choices=JURISDICTIONS,
        help="юрисдикция (INT — международный договор)",
    )
    p_a.add_argument("--doc-type", required=True, choices=DOC_TYPES)
    p_a.add_argument(
        "--title", required=True, help="название документа (без полного текста)"
    )
    p_a.add_argument("--identifier", required=True, help="номер и дата акта")
    p_a.add_argument(
        "--effective-date",
        default=None,
        help="дата вступления в силу YYYY[-MM[-DD]] или 'null'",
    )
    p_a.add_argument(
        "--coordinate", required=True, help="точная координата (статья/пункт/страница)"
    )
    p_a.add_argument("--url", default=None, help="ссылка на официальную публикацию")
    # payload
    p_a.add_argument("--norm-id", required=True, help="например гк-рф/ст.1252")
    p_a.add_argument("--predicate", required=True, choices=PREDICATES)
    p_a.add_argument("--subject", required=True, help="что изменяется (норма)")
    p_a.add_argument(
        "--object", dest="obj", required=True, help="чем изменено (поправка/акт)"
    )
    p_a.add_argument("--effective-from", default=None, help="YYYY[-MM[-DD]] или 'null'")
    p_a.add_argument("--effective-to", default=None, help="YYYY[-MM[-DD]] или 'null'")
    p_a.add_argument("--confidence", type=float, default=0.6, help="уверенность 0..1")
    p_a.add_argument("--change", default=None, help="краткое описание изменения")
    p_a.add_argument("--note", default=None, help="примечание")
    p_a.set_defaults(func=cmd_append)

    p_v = sub.add_parser("validate", help="проверить сессию")
    p_v.add_argument("--journal", required=True)
    p_v.add_argument("--json", action="store_true")
    p_v.set_defaults(func=cmd_validate)

    p_s = sub.add_parser("summary", help="сводка по сессии")
    p_s.add_argument("--journal", required=True)
    p_s.add_argument("--json", action="store_true")
    p_s.set_defaults(func=cmd_summary)

    p_e = sub.add_parser("export", help="публичный (редактированный) экспорт")
    p_e.add_argument("--journal", required=True)
    p_e.add_argument("--out", required=True)
    p_e.add_argument("--dry-run", action="store_true")
    p_e.set_defaults(func=cmd_export)

    return ap


def _normalize_null(ap: argparse.Namespace) -> None:
    """Приводит строку 'null' к None для дат/ссылок."""
    for field in ("effective_date", "effective_from", "effective_to", "url"):
        val = getattr(ap, field, None)
        if isinstance(val, str) and val.strip().lower() == "null":
            setattr(ap, field, None)


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    _normalize_null(args)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Прервано пользователем.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

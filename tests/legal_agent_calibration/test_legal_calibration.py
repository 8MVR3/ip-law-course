#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тесты модуля калибровки юридического агента (`legal-agent-calibration/`).

Без сети. Проверяют:
  * последовательность ролей task -> answer -> review -> (correction...)* -> final;
  * обязательные юридические поля source/payload;
  * редакцию секретов/ПД/полных текстов в публичном экспорте;
  * отклонение некорректных и неутверждённых сессий;
  * publisher: dry-run и репозиторий по умолчанию (без загрузки).

Запуск:
    python3 tests/legal_agent_calibration/test_legal_calibration.py
    pytest tests/legal_agent_calibration/
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "legal-agent-calibration"
sys.path.insert(0, str(MODULE))

import session as sess  # noqa: E402

CLI = MODULE / "scripts" / "legal_calibration_session.py"
PUBLISH = MODULE / "publish.py"

_passed = 0
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failures.append(name)
        print(f"  FAIL {name}  {detail}")


def _source(**over) -> dict:
    base = {
        "authority": "Государственная Дума ФС РФ",
        "jurisdiction": "RU",
        "doc_type": "amending_act",
        "title": "147-ФЗ",
        "identifier": "ФЗ от 01.07.2017 № 147-ФЗ",
        "effective_date": "2017-10-01",
        "coordinate": "ст. 1",
        "url": None,
    }
    base.update(over)
    return base


def _payload(**over) -> dict:
    base = {
        "norm_id": "гк-рф/ст.1252",
        "predicate": "изменена",
        "subject": "ГК РФ ст. 1252",
        "object": "147-ФЗ",
        "effective_from": "2017-10-01",
        "effective_to": None,
        "confidence": 0.8,
        "change_ru": "изменение нормы",
        "note_ru": None,
    }
    base.update(over)
    return base


def _writer(tmp: Path, sid: str = "ip-law-test-001") -> sess.SessionWriter:
    return sess.SessionWriter(tmp / f"{sid}.jsonl", sid)


def _write_valid(w: sess.SessionWriter) -> None:
    w.append("task", _source(), _payload(predicate="уточнена"), created_at="2026-10-05")
    w.append(
        "agent_answer",
        _source(),
        _payload(predicate="уточнена"),
        created_at="2026-10-05",
    )
    w.append(
        "expert_review",
        _source(),
        _payload(predicate="уточнена"),
        created_at="2026-10-05",
    )
    w.append(
        "correction", _source(), _payload(predicate="изменена"), created_at="2026-10-05"
    )
    w.append(
        "agent_answer",
        _source(),
        _payload(predicate="изменена", confidence=0.9),
        created_at="2026-10-05",
    )
    w.append(
        "expert_review",
        _source(),
        _payload(predicate="изменена", confidence=0.9),
        created_at="2026-10-05",
    )
    w.append(
        "final",
        _source(),
        _payload(predicate="изменена", confidence=0.95),
        created_at="2026-10-05",
        expert_approved=True,
    )


def test_sequence() -> None:
    print("[последовательность]")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        w = _writer(tmp)
        _write_valid(w)
        check(
            "полная последовательность без ошибок",
            sess.validate_session(w.events()) == [],
            str(sess.validate_session(w.events())),
        )
        s = w.summary()
        check("сессия завершена", s["finished"] is True)
        check("сессия утверждена экспертом", s["expert_approved"] is True)
        check("есть минимум одна правка", s["corrections"] >= 1)
        check("есть минимум два разбора эксперта", s["expert_reviews"] >= 2)

        # первый агент до задания — запрещён
        w2 = _writer(tmp, "ip-law-test-002")
        try:
            w2.append("agent_answer", _source(), _payload(), created_at="2026-10-05")
            check("agent_answer первым отклонён", False, "не отклонён")
        except ValueError:
            check("agent_answer первым отклонён", True)

        # final без утверждения — запрещён
        w3 = _writer(tmp, "ip-law-test-003")
        w3.append("task", _source(), _payload(), created_at="2026-10-05")
        w3.append("agent_answer", _source(), _payload(), created_at="2026-10-05")
        w3.append("expert_review", _source(), _payload(), created_at="2026-10-05")
        try:
            w3.append(
                "final",
                _source(),
                _payload(),
                created_at="2026-10-05",
                expert_approved=False,
            )
            check("final без утверждения отклонён", False, "не отклонён")
        except ValueError:
            check("final без утверждения отклонён", True)

        # final сразу после review допустим
        w4 = _writer(tmp, "ip-law-test-004")
        w4.append("task", _source(), _payload(), created_at="2026-10-05")
        w4.append("agent_answer", _source(), _payload(), created_at="2026-10-05")
        w4.append("expert_review", _source(), _payload(), created_at="2026-10-05")
        w4.append(
            "final",
            _source(),
            _payload(),
            created_at="2026-10-05",
            expert_approved=True,
        )
        check(
            "final сразу после review допустим",
            sess.validate_session(w4.events()) == [],
        )


def test_legal_requirements() -> None:
    print("[юридические требования к source/payload]")
    good = {
        "schema": 1,
        "session_id": "ip-law-test-010",
        "seq": 1,
        "role": "task",
        "created_at": "2026-10-05",
        "source": _source(),
        "payload": _payload(),
    }
    good["event_id"] = sess.compute_event_id(good)
    check(
        "корректное событие проходит",
        sess.validate_event(good) == [],
        str(sess.validate_event(good)),
    )

    for field in (
        "authority",
        "jurisdiction",
        "doc_type",
        "title",
        "identifier",
        "effective_date",
        "coordinate",
    ):
        bad = json.loads(json.dumps(good))
        del bad["source"][field]
        errs = sess.validate_event(bad)
        check(f"source.{field} обязателен", any(field in e for e in errs), str(errs))

    bad = json.loads(json.dumps(good))
    bad["source"]["jurisdiction"] = "MARS"
    check(
        "недопустимая юрисдикция отклонена",
        any("jurisdiction" in e for e in sess.validate_event(bad)),
    )

    for field in (
        "norm_id",
        "predicate",
        "subject",
        "object",
        "effective_from",
        "effective_to",
        "confidence",
    ):
        bad = json.loads(json.dumps(good))
        del bad["payload"][field]
        errs = sess.validate_event(bad)
        check(f"payload.{field} обязателен", any(field in e for e in errs), str(errs))

    bad = json.loads(json.dumps(good))
    bad["payload"]["predicate"] = "не_предикат"
    check(
        "недопустимый predicate отклонён",
        any("predicate" in e for e in sess.validate_event(bad)),
    )

    bad = json.loads(json.dumps(good))
    bad["payload"]["confidence"] = 1.5
    check(
        "confidence вне [0,1] отклонён",
        any("confidence" in e for e in sess.validate_event(bad)),
    )

    bad = json.loads(json.dumps(good))
    bad["source"]["effective_date"] = "вчера"
    check(
        "некорректная effective_date отклонена",
        any("effective_date" in e for e in sess.validate_event(bad)),
    )

    # null допустим для effective_date/effective_to
    ok = json.loads(json.dumps(good))
    ok["source"]["effective_date"] = None
    ok["payload"]["effective_to"] = None
    ok["event_id"] = sess.compute_event_id(ok)
    check(
        "null даты допустимы",
        sess.validate_event(ok) == [],
        str(sess.validate_event(ok)),
    )


def test_redaction() -> None:
    print("[редакция публичного экспорта]")
    ev = {
        "schema": 1,
        "session_id": "ip-law-test-020",
        "seq": 1,
        "role": "task",
        "created_at": "2026-10-05",
        "source": _source(),
        "payload": _payload(
            note_ru="контакт user@example.com, тел +7 999 123-45-67, token sk-abcdefghijklmnop1234",
            raw_text="полный сырой текст " * 300,
        ),
    }
    ev["event_id"] = sess.compute_event_id(ev)
    red = sess.redact_record(ev)
    check(
        "raw_text замаскирован",
        red["payload"]["raw_text"] == sess.REDACTED,
        str(red["payload"]["raw_text"])[:60],
    )
    check(
        "секрет-ключ замаскирован",
        "sk-abcdefghijklmnop1234" not in json.dumps(red, ensure_ascii=False),
    )
    check(
        "e-mail замаскирован",
        "user@example.com" not in json.dumps(red, ensure_ascii=False),
    )
    check(
        "телефон замаскирован", "123-45-67" not in json.dumps(red, ensure_ascii=False)
    )
    check(
        "длинный текст усечён",
        len(str(red["payload"].get("raw_text", ""))) < sess.LONG_TEXT_LIMIT + 100,
    )
    check("редакция не меняет исходное событие", "raw_text" in ev["payload"])

    # запрещённое поле на верхнем уровне
    ev2 = json.loads(json.dumps(ev))
    ev2["api_key"] = "secret-value"
    red2 = sess.redact_record(ev2)
    check("api_key замаскирован", red2["api_key"] == sess.REDACTED)


def test_malformed_and_unapproved() -> None:
    print("[отклонение некорректных сессий]")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # незавершённая сессия (нет final)
        w = _writer(tmp, "ip-law-test-030")
        w.append("task", _source(), _payload(), created_at="2026-10-05")
        w.append("agent_answer", _source(), _payload(), created_at="2026-10-05")
        errs = sess.validate_session(w.events())
        check("сессия без final отклонена", any("final" in e for e in errs), str(errs))

        # дубликат seq
        ev = json.loads(json.dumps(w.events()[0]))
        ev2 = json.loads(json.dumps(w.events()[1]))
        ev2["seq"] = 1
        errs = sess.validate_sequence([ev, ev2])
        check("неверная нумерация отклонена", any("seq" in e for e in errs), str(errs))

        # несоответствие event_id
        bad = json.loads(json.dumps(w.events()[0]))
        bad["event_id"] = "tk-000000000000"
        errs = sess.validate_event(bad)
        check(
            "недетерминированный event_id отклонён",
            any("mismatch" in e for e in errs),
            str(errs),
        )

        # expert_approved не на final
        bad2 = json.loads(json.dumps(w.events()[1]))
        bad2["expert_approved"] = True
        bad2["event_id"] = sess.compute_event_id(bad2)
        errs = sess.validate_event(bad2)
        check(
            "expert_approved вне final отклонён",
            any("expert_approved" in e for e in errs),
            str(errs),
        )

        # разный session_id
        mixed = json.loads(json.dumps(w.events()[0]))
        other = json.loads(json.dumps(w.events()[0]))
        other["session_id"] = "other-session"
        other["seq"] = 2
        other["event_id"] = sess.compute_event_id(other)
        errs = sess.validate_sequence([mixed, other])
        check(
            "разные session_id отклонены",
            any("session_id" in e for e in errs),
            str(errs),
        )


def test_publisher() -> None:
    print("[publisher: dry-run и репозиторий по умолчанию]")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        sdir = tmp / "ip-law-test-040"
        sdir.mkdir(parents=True)
        w = sess.SessionWriter(sdir / "session.jsonl", "ip-law-test-040")
        _write_valid(w)

        r = subprocess.run(
            [sys.executable, str(PUBLISH), "--session-dir", str(sdir), "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        check(
            "publisher dry-run завершается успешно",
            r.returncode == 0,
            r.stdout + r.stderr,
        )
        check(
            "publisher сообщает репозиторий по умолчанию",
            "lawful-good-project/legal-agent-calibrations" in r.stdout,
            r.stdout,
        )
        check(
            "publisher указывает путь calibrations/<session>/session.jsonl",
            "calibrations/ip-law-test-040/session.jsonl" in r.stdout,
            r.stdout,
        )
        check(
            "publisher dry-run не грузит",
            "dry-run" in r.stdout and "загрузка пропущена" in r.stdout,
            r.stdout,
        )
        public = sdir / "_public" / "session.jsonl"
        check("публичный экспорт создан", public.is_file())
        if public.is_file():
            lines = [
                l for l in public.read_text(encoding="utf-8").splitlines() if l.strip()
            ]
            check(
                "экспорт содержит все события",
                len(lines) == len(w.events()),
                f"{len(lines)} != {len(w.events())}",
            )

        # незавершённая сессия не публикуется
        bad_dir = tmp / "ip-law-bad-041"
        bad_dir.mkdir(parents=True)
        (bad_dir / "session.jsonl").write_text(
            json.dumps(
                {"schema": 1, "session_id": "ip-law-bad-041"}, ensure_ascii=False
            )
            + "\n",
            encoding="utf-8",
        )
        r2 = subprocess.run(
            [sys.executable, str(PUBLISH), "--session-dir", str(bad_dir), "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=ROOT,
        )
        check(
            "publisher отклоняет невалидную сессию",
            r2.returncode == 1,
            r2.stdout + r2.stderr,
        )

        # без токена dry-run не пытается грузить
        check("publisher не требует токен в dry-run", "HF_TOKEN" not in r.stdout)


def test_schema_json() -> None:
    print("[схема]")
    schema = MODULE / "schema" / "session.schema.json"
    check("session.schema.json на месте", schema.is_file())
    if schema.is_file():
        data = json.loads(schema.read_text(encoding="utf-8"))
        roles = data.get("$defs", {}).get("payload", {})
        enum = None
        props = data.get("properties", {})
        if "role" in props:
            enum = props["role"].get("enum")
        check("схема объявляет все роли", enum == list(sess.ROLES), str(enum))
        src_req = set(data["$defs"]["source"]["required"])
        check(
            "source требует юридические поля",
            {
                "authority",
                "jurisdiction",
                "doc_type",
                "title",
                "identifier",
                "effective_date",
                "coordinate",
            }
            <= src_req,
            str(src_req),
        )
        pay_req = set(data["$defs"]["payload"]["required"])
        check(
            "payload требует правовые поля",
            {
                "norm_id",
                "predicate",
                "subject",
                "object",
                "effective_from",
                "effective_to",
                "confidence",
            }
            <= pay_req,
            str(pay_req),
        )


def main() -> int:
    test_schema_json()
    test_sequence()
    test_legal_requirements()
    test_redaction()
    test_malformed_and_unapproved()
    test_publisher()
    print(f"\n{_passed} passed, {len(_failures)} failed")
    if _failures:
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


def test_legal_calibration() -> None:
    """Pytest entry point."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

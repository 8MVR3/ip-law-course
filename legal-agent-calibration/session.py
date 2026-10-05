#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""session.py — append-only журнал калибровочной сессии юридического эксперта.

Назначение
----------
Этот модуль — **отдельный** контур калибровки юридического агента (по аналогии
с физическими калибровочными сессиями, но с правовой спецификой). Эксперт даёт
задание и официальные ориентиры; агент извлекает правовые факты/изменения норм;
эксперт указывает **явные** ошибки; агент правит, пока эксперт не утвердит.

Сессия ведётся как неизменяемый (append-only) JSONL-журнал событий:

    task -> agent_answer -> expert_review -> (correction -> agent_answer -> expert_review)* -> final

где `final` обязан нести `expert_approved=true`.

Гарантии
--------
* Zero external dependencies (stdlib only).
* Детерминированные `event_id`: `<role_short>-<sha256[:12]>` от канонического
  содержимого события (тела без служебного event_id).
* Валидация последовательности ролей и запрет автоматического «самоодобрения».
* Редакция: публичный экспорт вычищает секреты, полные сырые тексты и ПД.
* Публичный экспорт отделён от рабочего журнала.

Запуск через CLI: `scripts/legal_calibration_session.py`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCHEMA = 1
ROLES = ("task", "agent_answer", "expert_review", "correction", "final")
ROLE_SHORT = {
    "task": "tk",
    "agent_answer": "an",
    "expert_review": "rv",
    "correction": "cr",
    "final": "fn",
}
PREDICATES = {
    "изменена",
    "дополнена",
    "введена",
    "изложена_в_новой_редакции",
    "признана_утратившей_силу",
    "исключена",
    "уточнена",
}
DOC_TYPES = {
    "statute",
    "code",
    "amending_act",
    "by-law",
    "case_law",
    "international_treaty",
    "official_guidance",
    "other",
}
JURISDICTIONS = {"RU", "INT", "EU", "US", "UK", "CIS", "OTHER"}
SESSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
NORM_ID_RE = re.compile(r"^[a-z0-9а-я-]+/.+$")
DATE_RE = re.compile(r"^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$")
FULL_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# --- Поля, запрещённые к публикации ------------------------------------------
# Секреты/ключи, сырые полные тексты, персональные данные.
FORBIDDEN_KEYS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "auth",
    "private_key",
    "raw_text",
    "full_text",
    "source_text",
    "case_file",
    "personal_data",
    "pii",
    "passport",
    "snils",
    "inn",
    "email",
    "phone",
    "address",
    "full_name",
    "birth_date",
}
# Значения-маски, если запрещённое поле всё же встречено (публичный экспорт).
REDACTED = "[REDACTED]"

# Регэкспы «сырых» значений — грубая, но предсказуемая защита публичного экспорта.
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}"
    r"|Bearer\s+[A-Za-z0-9._-]{16,}|AKIA[0-9A-Z]{16})"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b")
LONG_TEXT_LIMIT = 1200  # длиннее — считаем «сырым полным текстом»


# ---------------------------------------------------------------------------
# Каноникализация и идентификаторы
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> str:
    """Канонический JSON для детерминированного хэша (сортированные ключи)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_event_id(body: Dict[str, Any]) -> str:
    """Детерминированный event_id: <role_short>-<sha256(canonical)[:12]>.

    Служебное поле `event_id` в тело не входит, поэтому ID воспроизводим.
    """
    b = {k: v for k, v in body.items() if k != "event_id"}
    short = ROLE_SHORT.get(str(b.get("role") or ""), "ev")
    digest = hashlib.sha256(_canonical(b).encode("utf-8")).hexdigest()[:12]
    return f"{short}-{digest}"


# ---------------------------------------------------------------------------
# Валидация одного события
# ---------------------------------------------------------------------------


def _is_date_or_null(v: Any) -> bool:
    return v is None or (isinstance(v, str) and bool(DATE_RE.match(v)))


def _check_source(src: Any, errors: List[str]) -> None:
    if not isinstance(src, dict):
        errors.append("source: must be object")
        return
    required = [
        "authority",
        "jurisdiction",
        "doc_type",
        "title",
        "identifier",
        "effective_date",
        "coordinate",
    ]
    for r in required:
        if r not in src:
            errors.append(f"source: missing required '{r}'")
    for key in ("authority", "title", "identifier", "coordinate"):
        v = src.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"source.{key}: must be non-empty string")
    if src.get("jurisdiction") not in JURISDICTIONS:
        errors.append(f"source.jurisdiction invalid: {src.get('jurisdiction')!r}")
    if src.get("doc_type") not in DOC_TYPES:
        errors.append(f"source.doc_type invalid: {src.get('doc_type')!r}")
    if "effective_date" not in src:
        errors.append("source: missing required 'effective_date'")
    elif not _is_date_or_null(src.get("effective_date")):
        errors.append("source.effective_date must be YYYY[-MM[-DD]] or null")


def _check_payload(p: Any, errors: List[str]) -> None:
    if not isinstance(p, dict):
        errors.append("payload: must be object")
        return
    required = [
        "norm_id",
        "predicate",
        "subject",
        "object",
        "effective_from",
        "effective_to",
        "confidence",
    ]
    for r in required:
        if r not in p:
            errors.append(f"payload: missing required '{r}'")
    nid = p.get("norm_id")
    if not isinstance(nid, str) or not NORM_ID_RE.match(nid):
        errors.append(f"payload.norm_id invalid: {nid!r}")
    if p.get("predicate") not in PREDICATES:
        errors.append(f"payload.predicate invalid: {p.get('predicate')!r}")
    subj = p.get("subject")
    if not isinstance(subj, str) or not subj.strip():
        errors.append("payload.subject: must be non-empty string")
    obj = p.get("object")
    if not isinstance(obj, str):
        errors.append("payload.object: must be string")
    for key in ("effective_from", "effective_to"):
        if key not in p:
            errors.append(f"payload: missing required '{key}'")
        elif not _is_date_or_null(p.get(key)):
            errors.append(f"payload.{key} must be YYYY[-MM[-DD]] or null")
    c = p.get("confidence")
    if not isinstance(c, (int, float)) or isinstance(c, bool) or not (0 <= c <= 1):
        errors.append("payload.confidence must be a number in [0,1]")


def validate_event(ev: Dict[str, Any]) -> List[str]:
    """Проверяет структуру одного события (не последовательность)."""
    errors: List[str] = []
    if not isinstance(ev, dict):
        return ["event: must be object"]
    required = [
        "schema",
        "session_id",
        "event_id",
        "seq",
        "role",
        "created_at",
        "source",
        "payload",
    ]
    for r in required:
        if r not in ev:
            errors.append(f"missing required '{r}'")
    if ev.get("schema") != SCHEMA:
        errors.append("schema must be 1")
    sid = ev.get("session_id")
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        errors.append(f"session_id invalid: {sid!r}")
    role = ev.get("role")
    if role not in ROLES:
        errors.append(f"role invalid: {role!r}")
    seq = ev.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        errors.append("seq must be integer >= 1")
    created = ev.get("created_at")
    if not isinstance(created, str) or not FULL_DATE_RE.match(created):
        errors.append("created_at must be YYYY-MM-DD")
    eid = ev.get("event_id")
    if not isinstance(eid, str) or not re.match(
        r"^[a-z0-9][a-z0-9._-]{0,15}-[a-f0-9]{12}$", eid
    ):
        errors.append(f"event_id invalid: {eid!r}")
    elif "schema" in ev and "seq" in ev:
        expected = compute_event_id(ev)
        if eid != expected:
            errors.append(
                f"event_id mismatch: {eid!r} != {expected!r} (event_id must be deterministic)"
            )
    if role == "final":
        if ev.get("expert_approved") is not True:
            errors.append("role=final requires expert_approved=true")
    elif "expert_approved" in ev:
        errors.append(
            f"expert_approved is only allowed on role=final (got role={role!r})"
        )
    _check_source(ev.get("source"), errors)
    _check_payload(ev.get("payload"), errors)
    return errors


# ---------------------------------------------------------------------------
# Валидация последовательности
# ---------------------------------------------------------------------------


def validate_sequence(events: List[Dict[str, Any]]) -> List[str]:
    """Проверяет роли, нумерацию и завершение сессии.

    Допустимая последовательность:
      task -> agent_answer -> expert_review ->
        (correction -> agent_answer -> expert_review)* -> final(expert_approved=true)
    """
    errors: List[str] = []
    if not events:
        return ["empty session: at least task..final required"]
    for i, ev in enumerate(events, 1):
        if ev.get("seq") != i:
            errors.append(f"event #{i}: seq must be {i}, got {ev.get('seq')!r}")
        if ev.get("schema") != SCHEMA:
            errors.append(f"event #{i}: schema must be 1")
    sids = {ev.get("session_id") for ev in events}
    if len(sids) != 1:
        errors.append(
            f"all events must share one session_id, got {sorted(map(str, sids))}"
        )

    roles = [ev.get("role") for ev in events]
    if roles[0] != "task":
        errors.append(f"first event must be role=task, got {roles[0]!r}")
    if roles[-1] != "final":
        errors.append(f"last event must be role=final, got {roles[-1]!r}")
    finals = [i for i, r in enumerate(roles) if r == "final"]
    if finals and finals != [len(roles) - 1]:
        errors.append("role=final is only allowed as the last event")

    # Конечный автомат последовательности.
    # expected: множество допустимых следующих ролей, либо None (сессия завершена).
    expected: Optional[Set[str]] = {"task"}
    for i, r in enumerate(roles):
        if expected is not None and r not in expected:
            errors.append(
                f"event #{i + 1}: expected role in {sorted(expected)}, got {r!r}"
            )
        if r == "task":
            expected = {"agent_answer"}
        elif r == "agent_answer":
            expected = {"expert_review"}
        elif r == "expert_review":
            # после разбора эксперта: либо правка, либо финал (утверждение)
            expected = {"correction", "final"}
        elif r == "correction":
            expected = {"agent_answer"}
        elif r == "final":
            expected = None
    # final допустим только последним событием
    for i, r in enumerate(roles[:-1]):
        if r == "final":
            errors.append(f"event #{i + 1}: final found before end of session")

    # expert_approved: должно быть ровно на final и нигде больше.
    for i, ev in enumerate(events):
        approved = ev.get("expert_approved")
        if roles[i] == "final" and approved is not True:
            errors.append(
                "final event must carry expert_approved=true "
                "(эксперт утверждает результат; автоодобрение агента запрещено)"
            )
        if roles[i] != "final" and "expert_approved" in ev:
            errors.append(f"event #{i + 1}: expert_approved only allowed on final")
    return errors


def validate_session(events: List[Dict[str, Any]]) -> List[str]:
    """Валидация последовательности + каждого события."""
    errors = validate_sequence(events)
    for i, ev in enumerate(events, 1):
        for e in validate_event(ev):
            errors.append(f"event #{i}: {e}")
    return errors


# ---------------------------------------------------------------------------
# Редакция и публичный экспорт
# ---------------------------------------------------------------------------


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        out = SECRET_VALUE_RE.sub(REDACTED, value)
        out = EMAIL_RE.sub(REDACTED, out)
        out = PHONE_RE.sub(REDACTED, out)
        if len(out) > LONG_TEXT_LIMIT:
            out = out[:LONG_TEXT_LIMIT].rstrip() + "…[" + REDACTED + " длинный текст]"
        return out
    return value


def redact_record(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Возвращает публичную копию события без секретов/сырых текстов/ПД.

    * поля из FORBIDDEN_KEYS удаляются (значение -> REDACTED);
    * вложенные строки проходят через маскировку секретов/почты/телефона;
    * слишком длинные строки усекаются (защита от полных сырых текстов).
    """

    def walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: Dict[str, Any] = {}
            for k, v in obj.items():
                if str(k).lower() in FORBIDDEN_KEYS:
                    out[k] = REDACTED
                    if isinstance(v, (dict, list)):
                        out[k] = REDACTED
                    continue
                out[k] = walk(v)
            return out
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        return _redact_value(obj)

    return walk(copy.deepcopy(ev))


# ---------------------------------------------------------------------------
# Append-only запись и чтение
# ---------------------------------------------------------------------------


def read_jsonl(path: str | Path) -> List[Dict[str, Any]]:
    """Читает JSONL; пустые строки пропускаются."""
    p = Path(path)
    out: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


class SessionWriter:
    """Append-only писатель сессии. Не перезаписывает существующие события.

    Проверяет `session_id` и ожидаемую роль до записи. Хранит журнал в одном
    файле JSONL; каждое событие — одна строка.
    """

    def __init__(self, path: str | Path, session_id: str):
        if not SESSION_ID_RE.match(session_id):
            raise ValueError(f"invalid session_id: {session_id!r}")
        self.path = Path(path)
        self.session_id = session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: List[Dict[str, Any]] = (
            read_jsonl(self.path) if self.path.is_file() else []
        )
        for ev in self._events:
            if ev.get("session_id") != session_id:
                raise ValueError(
                    "existing journal belongs to a different session_id: "
                    f"{ev.get('session_id')!r}"
                )

    def __len__(self) -> int:
        return len(self._events)

    def events(self) -> List[Dict[str, Any]]:
        return list(self._events)

    def expected_role(self) -> Optional[str]:
        """Ожидаемая следующая роль, либо None если сессия завершена."""
        roles = [e.get("role") for e in self._events]
        if not roles:
            return "task"
        last = roles[-1]
        if last == "task":
            return "agent_answer"
        if last == "agent_answer":
            return "expert_review"
        if last == "correction":
            return "agent_answer"
        # expert_review -> correction|final; final -> сессия завершена
        return None

    def can_append(self, role: str) -> Tuple[bool, str]:
        """Допустим ли append роли с точки зрения последовательности."""
        if role not in ROLES:
            return False, f"unknown role: {role!r}"
        roles: List[str] = [str(e.get("role")) for e in self._events]
        if not roles:
            return (role == "task", "first event must be task")
        last = roles[-1]
        if last == "final":
            return False, "session already finished (last event is final)"
        allowed = {
            "task": "agent_answer",
            "agent_answer": "expert_review",
            "correction": "agent_answer",
        }
        if last == "expert_review":
            if role not in ("correction", "final"):
                return False, "after expert_review expected correction or final"
            return True, ""
        exp = allowed.get(last)
        if exp is None:
            return False, f"cannot append after {last!r}"
        if role != exp:
            return False, f"after {last!r} expected {exp!r}, got {role!r}"
        return True, ""

    def append(
        self,
        role: str,
        source: Dict[str, Any],
        payload: Dict[str, Any],
        *,
        created_at: str,
        expert_approved: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Добавляет событие. Возвращает записанное событие (с event_id).

        `expert_approved` допускается только для role=final и должен быть True.
        """
        ok, why = self.can_append(role)
        if not ok:
            raise ValueError(f"sequence violation: {why}")
        if role == "final":
            if expert_approved is not True:
                raise ValueError(
                    "role=final requires expert_approved=True "
                    "(эксперт утверждает; агент не может одобрить сам себя)"
                )
        elif expert_approved is not None:
            raise ValueError("expert_approved is only allowed on role=final")

        body: Dict[str, Any] = {
            "schema": SCHEMA,
            "session_id": self.session_id,
            "seq": len(self._events) + 1,
            "role": role,
            "created_at": created_at,
            "source": source,
            "payload": payload,
        }
        if role == "final":
            body["expert_approved"] = True
        body["event_id"] = compute_event_id(body)

        errors = validate_event(body)
        if errors:
            raise ValueError("invalid event: " + "; ".join(errors))

        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(body, ensure_ascii=False) + "\n")
        self._events.append(body)
        return body

    def summary(self) -> Dict[str, Any]:
        """Краткая сводка сессии."""
        roles = [e.get("role") for e in self._events]
        n_reviews = sum(1 for r in roles if r == "expert_review")
        n_corrections = sum(1 for r in roles if r == "correction")
        finished = bool(self._events) and roles[-1] == "final"
        approved = finished and self._events[-1].get("expert_approved") is True
        return {
            "session_id": self.session_id,
            "events": len(self._events),
            "roles": roles,
            "expert_reviews": n_reviews,
            "corrections": n_corrections,
            "finished": finished,
            "expert_approved": approved,
            "next_expected": self.expected_role()
            or ("—" if finished else "correction|final"),
        }


def export_public(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Публичный экспорт: редактируем каждое событие, сохраняя порядок.

    Публичный экспорт не заменяет рабочий журнал; это отдельный файл/поток.
    """
    return [redact_record(ev) for ev in events]


def write_public_jsonl(events: List[Dict[str, Any]], path: str | Path) -> int:
    """Пишет публичный (редактированный) JSONL. Возвращает число строк."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    public = export_public(events)
    rows = "\n".join(json.dumps(ev, ensure_ascii=False) for ev in public)
    p.write_text(rows + ("\n" if rows else ""), encoding="utf-8")
    return len(public)

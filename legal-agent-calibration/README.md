# Калибровка юридического агента (первое задание эксперта)

Это **отдельное первое задание юридического эксперта**: настроить (откалибровать)
агента извлечения правовых фактов, **до** основного задания по вкладу в датасет
(`assignments/00-first-contribution/`). Модуль аналогичен физическим
калибровочным сессиям, но с правовой спецификой: официальные источники,
юрисдикция и даты действия норм, а не физические эксперименты.

Курс-специфичная цель: получить у эксперта **явные** ошибки агента на правовом
материале и довести ответ агента до **утверждённого экспертом** состояния.

## Как это устроено

1. **Эксперт** даёт задание: норму/статью, официальный ориентир и координату
   источника. Это событие `task`.
2. **Агент** извлекает правовой факт/изменение нормы (`agent_answer`):
   `norm_id`, `predicate`, `subject`, `object`, даты действия, уверенность.
3. **Эксперт** разбирает ответ и указывает **явные** ошибки (`expert_review`):
   неверный предикат, юрисдикция, дата вступления в силу, координата и т.п.
4. **Цикл правок**: `correction` → `agent_answer` → `expert_review`.
   Повторяется, пока эксперт не примет результат.
5. **Финал** (`final`) несёт `expert_approved=true`. Только эксперт может
   утвердить; агент **не** утверждает сам себя.

Сессия — это неизменяемый (append-only) JSONL-журнал, одно событие на строку.
Схема — `schema/session.schema.json`.

```
task -> agent_answer -> expert_review -> (correction -> agent_answer -> expert_review)* -> final
```

## Запуск (CLI)

```bash
python3 legal-agent-calibration/scripts/legal_calibration_session.py init \
  --session-id ip-law-2026-001 --journal sessions/ip-law-2026-001/session.jsonl

python3 .../legal_calibration_session.py append --journal ... \
  --session-id ip-law-2026-001 --role task \
  --authority "Государственная Дума ФС РФ" --jurisdiction RU \
  --doc-type amending_act --title "147-ФЗ" --identifier "ФЗ от 01.07.2017 № 147-ФЗ" \
  --effective-date 2017-10-01 --coordinate "ст. 1" \
  --norm-id "гк-рф/ст.1252" --predicate изменена \
  --subject "ГК РФ ст. 1252" --object "147-ФЗ" --confidence 0.6

python3 .../legal_calibration_session.py validate --journal ...
python3 .../legal_calibration_session.py summary  --journal ...
python3 .../legal_calibration_session.py export   --journal ... --out public/session.jsonl
```

Полное пошаговое задание и критерии приёмки — `docs/EXPERT-CALIBRATION.md`.
Правила агента-калибровщика — `docs/OPENCODE-LEGAL-CALIBRATOR.md`.

## Публикация

`publish.py` публикует **только публичный (редактированный) экспорт** сессии в
HuggingFace-датасет `calibrations/<session_id>/session.jsonl`:

```bash
python3 legal-agent-calibration/publish.py --session-dir sessions/ip-law-2026-001 --dry-run
HF_TOKEN=... python3 legal-agent-calibration/publish.py --session-dir sessions/ip-law-2026-001
```

- репозиторий по умолчанию: `lawful-good-project/legal-agent-calibrations`;
- `huggingface_hub` импортируется лениво — без него доступен только `--dry-run`;
- токен читается **только** из `HF_TOKEN` и никогда не выводится;
- `create_pr=True` по умолчанию (прямая запись — только `--no-pr`);
- секреты, полные сырые тексты и персональные данные вычищаются редакцией.

## Юридическая безопасность (обязательно)

- **Это не юридическая консультация.** Учебный и исследовательский контур.
- Предпочитайте **официальные источники** (pravo.gov.ru, vsrf.ru, WIPO Lex);
  пересказ свободных статей — только как ориентир.
- Проверяйте **юрисдикцию, версию и даты действия** нормы.
- **Не** загружайте персональные данные и частные судебные дела.
- **Не** приводите полные сырые тексты охраняемых произведений; норма
  излагается кратко своими словами.

## Далее

После утверждения хотя бы одной сессии участник переходит к основному заданию
первого вклада: `assignments/00-first-contribution/` (роль «эксперт», 3 записи
об изменениях в статьи НПА). Калибровка — вход, а не замена.

## Состав папки

```
README.md                       — это описание
schema/session.schema.json      — схема события сессии (JSONL)
session.py                      — append-only журнал, валидация, редакция, экспорт
scripts/legal_calibration_session.py — CLI (init/append/validate/summary/export)
publish.py                      — публикация публичного экспорта в HuggingFace
docs/EXPERT-CALIBRATION.md      — точное задание эксперта и критерии приёмки
docs/OPENCODE-LEGAL-CALIBRATOR.md — промпт/правила агента-калибровщика
```

Тесты — `tests/legal_agent_calibration/` (без сети).

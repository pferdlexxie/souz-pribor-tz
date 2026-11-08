#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

NO_DATA = "данных нет"
TITLE_MAX = 70
DESC_MIN, DESC_MAX = 800, 1200
DESC_TARGET = 1000  
QUERIES_MIN, QUERIES_MAX = 3, 5
MAX_ATTEMPTS = 3  
REWRITE_ATTEMPTS = 2 

YANDEX_DEFAULT_MODEL = "yandexgpt/latest"
YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

HTTP_ERRORS = {
    401: "неверный YANDEX_API_KEY",
    403: "нет доступа к каталогу или модели",
    404: "модель не найдена",
    429: "превышен лимит запросов",
}


def load_env(path: str = ".env") -> None:
    """Загрузчик env"""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class CardError(Exception):
    """Ошибка, понятная пользователю."""

# --------------------------------------------------------------------------- промт

SYSTEM_PROMPT = f"""Ты — помощник контент-менеджера B2B-интернет-магазина контрольно-измерительных приборов и промышленной автоматики. По входным данным ты готовишь черновик карточки товара на русском языке.

ПРАВИЛА ФАКТОВ (главное требование: ничего не выдумывать)
1. Единственный источник сведений это входные данные. Технические значения (числа, единицы, диапазоны, интерфейсы, протоколы, напряжения, классы точности, размеры, степень защиты, материалы, сертификаты) переноси в той же записи, что во входных данных. Не округляй, не пересчитывай, не переводи единицы, не меняй названия характеристик.
2. Не используй собственные знания о приборах этого типа, производителя или похожих моделях. Не достраивай характеристики «по аналогии».
3. Если сведений о чём-то нет, просто не упоминай это в тексте.
4. Не придумывай отрасли и области применения, сертификаты, гарантии, цены, наличие, сроки поставки, страну производства.
5. Никаких оценок и маркетинга, которых нет в данных: «надёжный», «точный», «универсальный», «удобный», «оптимальный», «высококачественный» и подобных. Говори о конкретных функциях и параметрах.

ФОРМАТ ОТВЕТА
Строго один JSON-объект по заданной схеме, без пояснений. Таблицу характеристик формирует программа из входных данных, поэтому в ответ её не включай.
- seo_title — не длиннее {TITLE_MAX} символов с пробелами: тип прибора, модель и производитель или главный параметр из данных. Если сомневаешься в длине, делай короче.
- seo_description — связный текст от {DESC_MIN} до {DESC_MAX} символов с пробелами. Объём считай словами: около 130–150 слов, 7–9 предложений, это примерно {DESC_TARGET} символов. Порядок: что это за прибор и для чего он предназначен, затем функции и принцип работы, затем основные параметры, интерфейсы, исполнение и условия эксплуатации, если они есть в данных. Выбирай главное, не перечисляй всё подряд и не пересказывай входной текст дословно. Лучше опустить второстепенное, чем превысить {DESC_MAX} символов. Если фактов мало, излагай имеющиеся подробнее и точнее, без оценок, воды и догадок.
- search_queries — от {QUERIES_MIN} до {QUERIES_MAX} поисковых запросов по 2–6 слов, составленных из терминов входных данных: тип прибора, модель, производитель, ключевой параметр или интерфейс. Допустимо добавить слово «купить».
- commercial.suitable_for — кому подходит прибор, 1–2 предложения, только по данным.
- commercial.tasks — какие задачи решает прибор, 1–3 предложения, только по данным. Если данных недостаточно, в обоих полях напиши «{NO_DATA}».
"""

REWRITE_PROMPT = """Ты редактор текстов для B2B-интернет-магазина. Не добавляй фактов, которых нет во входных данных товара. Числа, единицы и названия переноси без изменений. Не обрывай слова и предложения. Верни только готовый текст описания, без кавычек, заголовков и пояснений."""

SCHEMA = {
    "type": "object",
    "properties": {
        "seo_title": {"type": "string"},
        "seo_description": {"type": "string"},
        "search_queries": {"type": "array", "items": {"type": "string"}},
        "commercial": {
            "type": "object",
            "properties": {"suitable_for": {"type": "string"}, "tasks": {"type": "string"}},
            "required": ["suitable_for", "tasks"],
        },
    },
    "required": ["seo_title", "seo_description", "search_queries", "commercial"],
}

# ------------------------------------------------------------------ проверка инпута


def parse_input(text: str) -> list:
    if not text.strip():
        raise CardError("Входной JSON пуст.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CardError(f"Некорректный JSON: {e.msg} (строка {e.lineno}, столбец {e.colno}).")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise CardError("Ожидался JSON-объект товара или непустой массив таких объектов.")
    return data


def clean_product(raw, idx: int) -> dict:
    """Приводит товар к стандартному виду"""
    if not isinstance(raw, dict):
        raise CardError(f"Товар №{idx}: ожидался объект, получено {type(raw).__name__}.")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CardError(f"Товар №{idx}: поле «name» отсутствует или пусто, без названия карточку делать нельзя.")

    man = raw.get("manufacturer")
    desc = raw.get("description")
    specs = raw.get("specs")
    return {
        "name": name.strip(),
        "manufacturer": man.strip() if isinstance(man, str) and man.strip() else NO_DATA,
        "specs": {
            str(k): str(v).strip()
            for k, v in (specs.items() if isinstance(specs, dict) else [])
            if v is not None and str(v).strip()
        },
        "description": desc.strip() if isinstance(desc, str) and desc.strip() else NO_DATA,
    }


# ------------------------------------------------------------------------------ ЛЛМ


def get_config(args) -> dict:
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if not key or not folder:
        raise CardError("Не заданы YANDEX_API_KEY и YANDEX_FOLDER_ID (см. .env.example).")
    model = args.model or os.environ.get("YANDEX_MODEL", "").strip() or YANDEX_DEFAULT_MODEL
    if "/" not in model:
        model += "/latest"
    return {
        "key": key,
        "folder": folder,
        "uri": model if model.startswith("gpt://") else f"gpt://{folder}/{model}",
        "base": os.environ.get("YANDEX_BASE_URL", YANDEX_BASE_URL).rstrip("/"),
        "timeout": args.timeout,
    }


def _post(cfg: dict, body: dict) -> dict:
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['key']}",
            "OpenAI-Project": cfg["folder"],
        },
    )
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
        return json.loads(r.read().decode("utf-8"))


def llm_chat(cfg: dict, messages: list, structured: bool = True) -> str:
    body = {"model": cfg["uri"], "messages": messages, "temperature": 0, "max_tokens": 2048}
    if structured:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "product_card", "schema": SCHEMA}}
    try:
        try:
            payload = _post(cfg, body)
        except urllib.error.HTTPError as e:
            if e.code != 400 or not structured:
                raise
            body.pop("response_format")  
            payload = _post(cfg, body)
        return payload["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise CardError(f"Yandex API: {HTTP_ERRORS.get(e.code) or f'HTTP {e.code}'}.")
    except urllib.error.URLError as e:
        raise CardError(f"Yandex API недоступен: {e.reason}.")
    except TimeoutError:
        raise CardError("Yandex API не ответил вовремя, увеличьте --timeout.")
    except (ValueError, KeyError, IndexError, TypeError):
        raise CardError("Yandex API вернул некорректный ответ.")


def parse_card(text: str) -> dict:
    """Разбор ответа модели и приведение типов."""
    text = text.strip()
    if text.startswith("```"):  
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("ответ не объект")
    c = data.get("commercial") or {}
    card = {
        "seo_title": str(data.get("seo_title", "")).strip(),
        "seo_description": str(data.get("seo_description", "")).strip(),
        "search_queries": [str(q).strip() for q in (data.get("search_queries") or []) if str(q).strip()],
        "commercial": {
            "suitable_for": str(c.get("suitable_for", "")).strip() or NO_DATA,
            "tasks": str(c.get("tasks", "")).strip() or NO_DATA,
        },
    }
    if not card["seo_title"] or not card["seo_description"]:
        raise ValueError("пустой seo_title или seo_description")
    return card


# ----------------------------------------------------------------------- checks


def desc_gap(text: str) -> int:
    """Превышение символов в описании."""
    n = len(text)
    return max(0, DESC_MIN - n, n - DESC_MAX)


def title_query_problems(card: dict) -> list:
    p = []
    t, q = len(card["seo_title"]), len(card["search_queries"])
    if t > TITLE_MAX:
        p.append(f"seo_title содержит {t} символов, нужно не более {TITLE_MAX}")
    if not QUERIES_MIN <= q <= QUERIES_MAX:
        p.append(f"search_queries содержит {q} запросов, нужно от {QUERIES_MIN} до {QUERIES_MAX}")
    return p


def length_problems(card: dict) -> list:
    p = title_query_problems(card)
    d = len(card["seo_description"])
    if d < DESC_MIN:
        p.append(f"seo_description содержит {d} символов, нужно не менее {DESC_MIN}")
    elif d > DESC_MAX:
        p.append(f"seo_description содержит {d} символов, нужно не более {DESC_MAX}")
    return p


def missing_warnings(product: dict) -> list:
    """Предупреждения о пустых входных полях."""
    p = []
    if product["manufacturer"] == NO_DATA:
        p.append("данных нет: manufacturer (производитель)")
    if not product["specs"]:
        p.append("данных нет: specs (характеристики)")
    if product["description"] == NO_DATA:
        p.append("данных нет: description (описание)")
    return p


# ----------------------------------------------------------------------- pipeline


def rewrite_description(cfg: dict, product: dict, text: str) -> str:
    """Отдельный запрос для подсчета."""
    n = len(text)
    target_words = max(int(DESC_TARGET / (n / max(len(text.split()), 1))), 1)
    if n > DESC_MAX:
        task = (
            f"Сократи текст до {target_words} слов, чтобы он занимал около {DESC_TARGET} символов с пробелами "
            f"(сейчас {n} символов, предел {DESC_MAX}). Оставь назначение, ключевые функции и параметры, "
            "удали второстепенные перечисления и повторы."
        )
    else:
        task = (
            f"Расширь текст до {target_words} слов, чтобы он занимал около {DESC_TARGET} символов с пробелами "
            f"(сейчас {n} символов, минимум {DESC_MIN}). Добавляй подробности только из входных данных."
        )
    messages = [
        {"role": "system", "content": REWRITE_PROMPT},
        {
            "role": "user",
            "content": "Входные данные о товаре (JSON):\n"
            + json.dumps(product, ensure_ascii=False, indent=2)
            + f"\n\nТекст описания:\n{text}\n\n{task}",
        },
    ]
    out = llm_chat(cfg, messages, structured=False).strip().strip('"«»').strip()
    return text if not out or out.startswith("{") else out


def generate_card(cfg: dict, product: dict) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Входные данные о товаре (JSON):\n"
            + json.dumps(product, ensure_ascii=False, indent=2)
            + "\n\nСформируй черновик карточки.",
        },
    ]
    best, best_problems = None, None
    for _ in range(MAX_ATTEMPTS):
        raw = llm_chat(cfg, messages)
        try:
            card = parse_card(raw)
        except (ValueError, json.JSONDecodeError) as e:
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Ответ непригоден ({e}). Верни ровно один JSON по схеме."},
            ]
            continue
        problems = title_query_problems(card)
        if best is None or len(problems) < len(best_problems):
            best, best_problems = card, problems
        if not problems:
            break
        messages += [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "Исправь и верни JSON целиком, факты не меняй, слова не обрывай: " + "; ".join(problems) + ".",
            },
        ]
    if best is None:
        raise CardError(f"Товар «{product['name']}»: модель не вернула корректный JSON за {MAX_ATTEMPTS} попытки.")

    for _ in range(REWRITE_ATTEMPTS):  
        if not desc_gap(best["seo_description"]):
            break
        try:
            new = rewrite_description(cfg, product, best["seo_description"])
        except CardError:
            break
        if desc_gap(new) < desc_gap(best["seo_description"]):
            best["seo_description"] = new

    return {
        "seo_title": best["seo_title"],
        "seo_description": best["seo_description"],
        "key_specs": [{"name": k, "value": v} for k, v in product["specs"].items()],  
        "search_queries": best["search_queries"],
        "commercial": best["commercial"],
        "warnings": missing_warnings(product) + length_problems(best),
    }


# ---------------------------------------------------------------------------- main


def main(argv=None) -> int:
    load_env()
    for stream in (sys.stdout, sys.stderr):  # кириллица в консоли Windows
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="путь к JSON с товаром или массивом товаров")
    ap.add_argument("-o", "--out", default="results", help="папка для результатов (по умолчанию ./results)")
    ap.add_argument("--model", help="модель Yandex, например yandexgpt/latest (по умолчанию из YANDEX_MODEL)")
    ap.add_argument("--timeout", type=int, default=120, help="таймаут ответа модели, секунд")
    a = ap.parse_args(argv)

    try:
        text = Path(a.input).read_text(encoding="utf-8")
    except OSError as e:
        print(f"Ошибка: не удалось прочитать файл: {e}", file=sys.stderr)
        return 2
    out = Path(a.out)
    try:
        items = parse_input(text)
        cfg = get_config(a)
        out.mkdir(parents=True, exist_ok=True)
    except (CardError, OSError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1

    failed = 0
    for i, raw in enumerate(items, 1):  
        try:
            product = clean_product(raw, i)
            card = generate_card(cfg, product)
        except CardError as e:
            print(f"[ошибка] {e}", file=sys.stderr)
            failed += 1
            continue
        path = out / f"product_{i:02d}.json"
        path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
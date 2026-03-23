"""
tests/test_routing.py
"""

_SCRIPT_KEYWORDS = [
    "дай диалог", "дай діалог", "дай скрипт", "скрипт з лікарем",
    "конкретный диалог", "конкретний діалог",
    "пример диалога", "приклад діалогу",
    "покажи діалог", "покажи диалог",
]

_FOLLOWUP_COACH_KEYWORDS = [
    "інші аргументи", "другие аргументы",
    "що ще сказати", "что ещё сказать",
    "розпиши детально", "распиши подробно",
    "як відповісти", "как ответить",
    "що сказати", "что сказать",
]

_OPERATIONAL_EARLY_KEYWORDS = [
    "відрядження", "командировк",
    "семінар", "семинар",
    "возврат товар", "повернення товар",
]

_EMET_PRODUCTS = [
    "ellanse", "elanse", "елансе", "элансе", "эллансе",
    "neuramis", "нейрамис", "нейраміс",
    "vitaran", "вітаран", "витаран",
    "petaran", "петаран",
    "exoxe", "экзокс",
    "esse", "эссе", "ессе",
    "iuse", "айюз", "июз",
    "magnox", "магнокс",
]

_OBJECTION_KEYWORDS = [
    "дорого", "дорогой", "дорога",
    "є дешевше", "есть дешевле",
    "не вірю", "не верю", "подумаю",
]

_PRODUCT_CANONICAL = {
    "ellanse": "Ellanse", "elanse": "Ellanse",
    "елансе": "Ellanse", "элансе": "Ellanse", "эллансе": "Ellanse",
    "neuramis": "Neuramis", "нейрамис": "Neuramis", "нейраміс": "Neuramis",
    "vitaran": "Vitaran", "вітаран": "Vitaran", "витаран": "Vitaran",
    "petaran": "Petaran", "петаран": "Petaran",
    "exoxe": "EXOXE", "экзокс": "EXOXE",
    "esse": "ESSE", "эссе": "ESSE", "ессе": "ESSE",
    "iuse": "IUSE", "айюз": "IUSE", "июз": "IUSE",
    "magnox": "Magnox", "магнокс": "Magnox",
}


def _is_script(text): return any(kw in text.lower() for kw in _SCRIPT_KEYWORDS)
def _is_followup(text): return any(kw in text.lower() for kw in _FOLLOWUP_COACH_KEYWORDS)
def _is_operational(text): return any(kw in text.lower() for kw in _OPERATIONAL_EARLY_KEYWORDS)
def _detect_product(text):
    t = text.lower()
    return next((p for p in _EMET_PRODUCTS if p in t), None)
def _has_objection(text): return any(kw in text.lower() for kw in _OBJECTION_KEYWORDS)


def test_script_dai_dialog(): assert _is_script("дай диалог по эллансе")
def test_script_konkretny(): assert _is_script("дай конкретный диалог с примерами")
def test_script_ukr(): assert _is_script("покажи діалог менеджера з лікарем")
def test_script_negative(): assert not _is_script("Еллансе дорого, що відповісти?")

def test_followup_args_ru(): assert _is_followup("врача не интересует, есть другие аргументы?")
def test_followup_how_to_answer(): assert _is_followup("як відповісти на це заперечення?")
def test_followup_negative(): assert not _is_followup("Еллансе дорого")

def test_operational_vidryadzhennya(): assert _is_operational("як оформити відрядження?")
def test_operational_seminar(): assert _is_operational("семінар наступного тижня")
def test_operational_negative(): assert not _is_operational("Еллансе дорого")

def test_product_ellanse_ru():
    p = _detect_product("эллансе дорого")
    assert p == "эллансе"
    assert _PRODUCT_CANONICAL[p] == "Ellanse"

def test_product_vitaran():
    p = _detect_product("розкажи про вітаран")
    assert _PRODUCT_CANONICAL[p] == "Vitaran"

def test_product_not_found(): assert _detect_product("клієнт каже що дорого") is None

def test_objection_dorogo(): assert _has_objection("еллансе дорого")
def test_objection_negative(): assert not _has_objection("розкажи про механізм дії")

def test_sos_trigger():
    text = "эллансе дорого"
    assert _detect_product(text) is not None
    assert _has_objection(text) is True
    assert _is_script(text) is False
    assert _is_followup(text) is False

def test_no_sos_when_script():
    text = "дай конкретный диалог по эллансе дорого"
    assert _is_script(text) is True

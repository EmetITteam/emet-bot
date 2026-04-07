#!/usr/bin/env python3
"""
Quality test: routing simulation (17 cases) + real LLM answer quality (6 cases).
Uses langchain Chroma for RAG + Gemini for LLM (mirrors bot failover mode).
"""
import sys, os, time
sys.path.insert(0, '/app')
os.chdir('/app')
from dotenv import load_dotenv
load_dotenv('/app/.env')

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
import google.generativeai as genai

OAI_KEY = os.environ.get('OPENAI_API_KEY', '')
GEM_KEY = os.environ.get('GEMINI_API_KEY', '')

emb = OpenAIEmbeddings(model='text-embedding-3-small', openai_api_key=OAI_KEY)
db_coach = Chroma(persist_directory='/app/data/db_index_coach_openai', embedding_function=emb)
genai.configure(api_key=GEM_KEY)
llm = genai.GenerativeModel('gemini-2.0-flash')
print('DB loaded, Gemini ready')

SYSTEM = (
    "Ti - AI-trener z prodazhu EMET. Pratsyuy TILKY z nadanym KONTEKSTOM.\n"
    "KONKURENTY: ZABORONENO pozytyvni otsinky ('chudovi rezultaty','efektyvnyy','populyarnyy','otsinyenyy likaryamy').\n"
    "Pry zapyti pro konkurenta: neytralnyy faktazh -> perekhid do dyferentsiatsiyi nashoho preparatu.\n"
    "Yakshcho lyshe chastykovi dani - kazhy 'U nashomu analizi ye taki dani: ...' NE kazhy 'nemaye informatsiyi'.\n"
    "ZAPERECHENNYA: SOS-format. PERSHYY argument - finansova vygoda likarya abo unikalnyy mekhanizm, NE tryvalist.\n"
    "Vidpovidades movoy zapytu (UA/RU). Telegram Markdown *zhyrnyy* cherez odnu zirochku."
)

def ask(search_q, user_text, k=20):
    docs = db_coach.similarity_search(search_q, k=k)
    ctx = '\n\n'.join('[R'+str(i+1)+'] '+d.page_content for i, d in enumerate(docs))
    prompt = SYSTEM + '\n\nKONTEKST:\n' + ctx + '\n\nQUESTION:\n' + user_text
    resp = llm.generate_content(prompt)
    return resp.text.strip()

def prev(t, n=320):
    t = t.replace('\n', ' ')
    return t[:n] + ('...' if len(t) > n else '')

# ============================================================
# PART 1 - ROUTING SIMULATION
# ============================================================
def route(text, hist=None):
    t = text.lower().strip()
    hist = hist or []

    FOLLOWUP = [
        '\u0440\u043e\u0437\u043a\u0430\u0436\u0438 \u043f\u0440\u043e',
        '\u0440\u0430\u0441\u0441\u043a\u0430\u0436\u0438 \u043f\u0440\u043e',
        '\u0449\u043e \u0442\u0430\u043a\u0435', '\u0447\u0442\u043e \u0442\u0430\u043a\u043e\u0435',
        '\u0447\u0438\u043c \u0432\u0456\u0434\u0440\u0456\u0437\u043d\u044f\u0454\u0442\u044c',
        '\u0447\u0435\u043c \u043e\u0442\u043b\u0438\u0447\u0430\u0435\u0442\u0441\u044f',
        '\u0456\u043d\u0448\u0456 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u0438',
        '\u0434\u0440\u0443\u0433\u0438\u0435 \u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442\u044b',
        '\u0440\u043e\u0437\u043f\u0438\u0448\u0438 \u0434\u0435\u0442\u0430\u043b\u044c\u043d\u043e',
        '\u0434\u0435\u0442\u0430\u043b\u044c\u043d\u0456\u0448\u0435', '\u043f\u043e\u0434\u0440\u043e\u0431\u043d\u0435\u0435',
        '\u044f\u043a \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0441\u0442\u0438',
        '\u043a\u0430\u043a \u043e\u0442\u0432\u0435\u0442\u0438\u0442\u044c',
        '\u0449\u043e \u0441\u043a\u0430\u0437\u0430\u0442\u0438', '\u0447\u0442\u043e \u0441\u043a\u0430\u0437\u0430\u0442\u044c',
    ]
    is_cf = any(kw in t for kw in FOLLOWUP)

    SCRIPT_KW = [
        '\u0441\u043a\u0440\u0438\u043f\u0442', 'script',
        '\u0434\u0456\u0430\u043b\u043e\u0433', '\u0434\u0438\u0430\u043b\u043e\u0433',
        '\u0444\u0440\u0430\u0437\u0430', '\u0430\u0440\u0433\u0443\u043c\u0435\u043d\u0442',
        '\u044f\u043a \u043f\u0440\u043e\u0434\u0430\u0442\u0438', '\u043a\u0430\u043a \u043f\u0440\u043e\u0434\u0430\u0442\u044c',
    ]
    is_script = any(kw in t for kw in SCRIPT_KW)

    AFF = [
        '\u0445\u043e\u0447\u0443', '\u0442\u0430\u043a', '\u0434\u0430', '\u043e\u043a',
        '\u0434\u043e\u0431\u0440\u0435', '\u0445\u043e\u0440\u043e\u0448\u043e', '\u0430\u0433\u0430',
        '\u043d\u0443\u0436\u043d\u043e', '\u0434\u0430\u0432\u0430\u0439', '\u0431\u0456\u043b\u044c\u0448\u0435',
        '\u0449\u0435', '\u0435\u0449\u0451', '\u0454', '\u0435\u0441\u0442\u044c', '\u043d\u0435\u0442',
        '\u043d\u0456', '\u0437\u0440\u043e\u0437\u0443\u043c\u0456\u043b\u043e', '\u043f\u043e\u043d\u044f\u0442\u043d\u043e',
        '\u0434\u044f\u043a\u0443\u044e', '\u0441\u043f\u0430\u0441\u0438\u0431\u043e', '\u043e\u043a\u0435\u0439',
    ]
    tc = t.rstrip('!?.,')
    is_sa = (
        len(t.split()) <= 4 and bool(hist)
        and any(tc == k or tc.startswith(k+' ') or tc.endswith(' '+k) for k in AFF)
    )

    RH = [
        '\u043f\u043e\u0447\u0435\u043c\u0443 \u0442\u044b', '\u0437\u0430\u0447\u0435\u043c \u0442\u044b',
        '\u043a\u0430\u043a \u0442\u0430\u043a', '\u0447\u043e\u043c\u0443 \u0442\u0438',
        '\u043f\u043e\u0447\u0435\u043c\u0443 \u043d\u0435', '\u0430 \u0447\u043e\u043c\u0443', '\u0430 \u043a\u0430\u043a',
    ]
    is_rh = bool(hist) and len(t.split()) <= 10 and any(t.startswith(k) for k in RH)

    if is_sa or is_rh:
        is_cf = True

    EMET = [
        'ellanse', 'elanse', 'neuramis',
        '\u043d\u0435\u0439\u0440\u0430\u043c\u0438\u0441',
        'vitaran', '\u0432\u0456\u0442\u0430\u0440\u0430\u043d', '\u0432\u0438\u0442\u0430\u0440\u0430\u043d',
        'petaran', '\u043f\u0435\u0442\u0430\u0440\u0430\u043d',
        'exoxe', 'esse', '\u0435\u0441\u0441\u0435', 'iuse', 'magnox', 'neuronox', 'pdrn', 'pcl',
    ]
    has_emet = any(p in t for p in EMET)
    OPS = [
        '\u0441\u0435\u043c\u0456\u043d\u0430\u0440', '\u0441\u0435\u043c\u0438\u043d\u0430\u0440',
        '\u0432\u0456\u0434\u0440\u044f\u0434\u0436\u0435\u043d\u043d\u044f', '\u043a\u043e\u043c\u0430\u043d\u0434\u0438\u0440\u043e\u0432\u043a',
    ]
    is_op = any(k in t for k in OPS)

    if has_emet:
        mode = 'coach[emet]'
    elif is_op:
        mode = 'operational'
    elif (is_cf or is_script) and hist:
        mode = 'coach[followup]'
    else:
        mode = 'detect_intent?'

    COMP = [
        'radiesse', 'sculptra', 'juvederm', 'teosyal', 'restylane', 'rejuran',
        '\u0440\u0435\u0434\u0436\u0443\u0440\u0430\u043d', 'aesthefill', 'plinest',
        '\u043f\u043b\u0456\u043d\u0435\u0441\u0442', 'nucleofill',
    ]
    comp = next((c for c in COMP if c in t), None)
    C2C = {
        'plinest': 'Vitaran', '\u043f\u043b\u0456\u043d\u0435\u0441\u0442': 'Vitaran',
        'rejuran': 'Vitaran', '\u0440\u0435\u0434\u0436\u0443\u0440\u0430\u043d': 'Vitaran',
        'nucleofill': 'Vitaran', 'sculptra': 'Petaran', 'aesthefill': 'Petaran',
        'radiesse': 'Ellanse', 'juvederm': 'Neuramis', 'teosyal': 'Neuramis', 'restylane': 'Neuramis',
    }
    cmap = False
    if mode == 'detect_intent?' and comp and comp in C2C:
        mode = 'coach[comp->' + C2C[comp] + ']'
        cmap = True
    return mode, is_cf, comp, cmap


C2C = {
    'plinest': 'Vitaran', '\u043f\u043b\u0456\u043d\u0435\u0441\u0442': 'Vitaran',
    'rejuran': 'Vitaran', '\u0440\u0435\u0434\u0436\u0443\u0440\u0430\u043d': 'Vitaran',
    'nucleofill': 'Vitaran', 'sculptra': 'Petaran', 'aesthefill': 'Petaran',
    'radiesse': 'Ellanse', 'juvederm': 'Neuramis', 'teosyal': 'Neuramis', 'restylane': 'Neuramis',
}

HV = [
    {'role': 'user', 'content': 'Rozkazhy pro Vitaran'},
    {'role': 'assistant', 'content': 'Vitaran - PDRN preparat...'},
]
HE = [
    {'role': 'user', 'content': 'Rozkazhy pro Ellanse'},
    {'role': 'assistant', 'content': 'Ellanse - PCL-filer...'},
    {'role': 'user', 'content': '\u041b\u0456\u043a\u0430\u0440 \u043a\u0430\u0436\u0435 Radiesse'},
    {'role': 'assistant', 'content': 'SOS Radiesse...'},
]

CASES = [
    # Препарати EMET
    ('\u0429\u043e \u0442\u0430\u043a\u0435 Neuramis \u0456 \u0447\u0438\u043c \u0432\u0456\u0434 Juvederm?', [],
     '\u041f\u0440\u0435\u043f\u0430\u0440\u0430\u0442 EMET + \u043a\u043e\u043d\u043a\u0443\u0440\u0435\u043d\u0442'),
    ('Ellanse S vs M \u2014 \u044f\u043a\u0430 \u0440\u0456\u0437\u043d\u0438\u0446\u044f?', [],
     '\u041f\u0440\u043e\u0434\u0443\u043a\u0442 S vs M'),
    ('\u0420\u043e\u0437\u043a\u0430\u0436\u0438 \u043f\u0440\u043e EXOXE \u0434\u0435\u0442\u0430\u043b\u044c\u043d\u043e', [],
     '\u041f\u0440\u043e\u0434\u0443\u043a\u0442 EXOXE'),
    ('Petaran \u2014 \u0434\u043b\u044f \u044f\u043a\u0438\u0445 \u043f\u0430\u0446\u0456\u0454\u043d\u0442\u0456\u0432?', [],
     '\u041f\u0440\u043e\u0434\u0443\u043a\u0442 \u0430\u0443\u0434\u0438\u0442\u043e\u0440\u0456\u044f'),
    ('\u0421\u043a\u0440\u0438\u043f\u0442 \u0434\u043b\u044f \u043f\u0440\u043e\u0434\u0430\u0436\u0443 Neuronox \u043b\u0456\u043a\u0430\u0440\u044e', [],
     '\u0421\u043a\u0440\u0438\u043f\u0442-\u0437\u0430\u043f\u0438\u0442'),
    # Конкуренти (без EMET-продукту — тестуємо Fix2)
    ('\u0420\u043e\u0437\u043a\u0430\u0436\u0438 \u043f\u0440\u043e \u043b\u0456\u043d\u0456\u0439\u043a\u0443 Sculptra', [],
     '\u041a\u043e\u043d\u043a. Sculptra \u2192 comp map Petaran'),
    ('\u0429\u043e \u0442\u0430\u043a\u0435 Aesthefill \u0456 \u044f\u043a \u0432\u043e\u043d\u043e \u043f\u0440\u0430\u0446\u044e\u0454?', [],
     '\u041a\u043e\u043d\u043a. Aesthefill \u2192 comp map Petaran'),
    ('Nucleofill \u2014 \u0449\u043e \u0446\u0435 \u0442\u0430\u043a\u0435?', [],
     '\u041a\u043e\u043d\u043a. Nucleofill \u2192 comp map Vitaran'),
    ('Juvederm \u2014 \u043f\u043e\u0440\u0456\u0432\u043d\u044f\u043d\u043d\u044f \u0437 \u043d\u0430\u0448\u0438\u043c\u0438', [],
     '\u041a\u043e\u043d\u043a. Juvederm \u2192 comp map Neuramis'),
    # Заперечення
    ('\u041b\u0456\u043a\u0430\u0440 \u043a\u0430\u0436\u0435 \u0449\u043e Juvederm \u0434\u0435\u0448\u0435\u0432\u0448\u0438\u0439', [],
     '\u0417\u0430\u043f. Neuramis \u0432\u0441 Juvederm'),
    ('\u041a\u043b\u0456\u0454\u043d\u0442 \u043a\u0430\u0436\u0435 \u0449\u043e Ellanse \u0434\u043e\u0440\u043e\u0433\u043e', [],
     '\u0417\u0430\u043f. Ellanse \u0434\u043e\u0440\u043e\u0433\u043e'),
    ('\u041d\u0435 \u0432\u043f\u0435\u0432\u043d\u0435\u043d\u0438\u0439 \u0443 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0456 Vitaran', [],
     '\u0417\u0430\u043f. Vitaran \u0441\u0443\u043c\u043d\u0456\u0432'),
    # Follow-up (Fix1 — активна сесія)
    ('\u0414\u0430, \u0440\u0430\u0441\u0441\u043a\u0430\u0436\u0438 \u043f\u043e\u0434\u0440\u043e\u0431\u043d\u0435\u0435', HV,
     'Follow-up \u043f\u0456\u0434\u0442\u0432\u0435\u0440\u0434\u0436\u0435\u043d\u043d\u044f'),
    ('\u0415\u0441\u0442\u044c \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u044b \u043e\u0442\u0432\u0435\u0442\u043e\u0432?', HE,
     'Follow-up \u0432\u0430\u0440\u0456\u0430\u043d\u0442\u0438'),
    ('\u0410 \u0447\u0442\u043e \u0435\u0441\u043b\u0438 \u0441\u043a\u0430\u0436\u0435\u0442 \u0447\u0442\u043e Plinest \u0434\u0435\u0448\u0435\u0432\u043b\u0435?', HV,
     'Follow-up \u043a\u043e\u043d\u043a\u0443\u0440\u0435\u043d\u0442'),
    ('\u0418\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u044f \u043f\u043e \u0446\u0435\u043d\u0430\u043c \u0435\u0441\u0442\u044c?', HV,
     'Follow-up \u0446\u0456\u043d\u0438'),
    ('\u041e\u043a, \u0434\u0430\u0432\u0430\u0439 \u0441\u043a\u0440\u0438\u043f\u0442', HV,
     'Follow-up \u0441\u043a\u0440\u0438\u043f\u0442'),
]

print()
print('=' * 62)
print('PART 1 - ROUTING (17 cases)')
print('=' * 62)
ok_r = 0
for text, hist, label in CASES:
    mode, cf, comp, cmap = route(text, hist)
    ok = 'OK' if 'coach' in mode else 'FAIL'
    if ok == 'OK':
        ok_r += 1
    ci = ' comp=' + str(comp) + ('->' + C2C.get(comp, '?') if cmap else '') if comp else ''
    print('[' + ok + '] ' + label)
    print('     ' + mode + ci)

print()
print('ROUTING: ' + str(ok_r) + '/' + str(len(CASES)) + ' correct')

# ============================================================
# PART 2 - LLM ANSWER QUALITY (6 cases)
# ============================================================
print()
print('=' * 62)
print('PART 2 - LLM QUALITY (6 cases, Gemini 2.0-flash)')
print('=' * 62)

QCASES = [
    {
        'id': 'Q1', 'label': 'Ellanse "dorogo" - objection',
        'sq': 'Ellanse zaperech dorogo argumenty PCL skrypt',
        'ut': 'Ellanse \u0434\u043e\u0440\u043e\u0433\u043e, \u043b\u0456\u043a\u0430\u0440 \u043d\u0435 \u0445\u043e\u0447\u0435 \u043a\u0443\u043f\u0443\u0432\u0430\u0442\u0438',
        'ck': ['pcl', 'sos'],
        'bad': ['\u0434\u043e 2 \u0440\u043e\u043a', '\u0434\u043e\u0432\u0433\u043e\u0442\u0440\u0438\u0432\u0430\u043b\u0438\u0439 \u0435\u0444\u0435\u043a\u0442'],
        'ok_hint': 'SOS + PCL \u043f\u0435\u0440\u0448\u0438\u043c, \u043d\u0435 \u0442\u0440\u0438\u0432\u0430\u043b\u0456\u0441\u0442\u044c',
    },
    {
        'id': 'Q2', 'label': 'Competitor lineup Plinest (Fix2)',
        'sq': 'plinest Vitaran PDRN porivnyannya konkurentnyy analiz',
        'ut': (
            '[SYS: zapyt pro konkurenta plinest. '
            '1) Neytralnyy faktazh BEZ pozytvnykh otsinok (chudovi, efektyvnyy, populyarnyy). '
            '2) Perekhid: A os chym nash Vitaran vidriznyayetsya + 2-3 perevahy.]\n\n'
            'PYTANNYA: \u0420\u043e\u0437\u043a\u0430\u0436\u0438 \u043f\u0440\u043e \u043b\u0456\u043d\u0456\u0439\u043a\u0443 Plinest'
        ),
        'ck': ['vitaran'],
        'bad': [
            '\u0447\u0443\u0434\u043e\u0432\u0456 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0438',
            '\u0435\u0444\u0435\u043a\u0442\u0438\u0432\u043d\u0438\u0439 \u043f\u0440\u0435\u043f\u0430\u0440\u0430\u0442',
            '\u043f\u043e\u043f\u0443\u043b\u044f\u0440\u043d\u0438\u0439',
        ],
        'ok_hint': '\u041d\u0435\u0439\u0442\u0440\u0430\u043b\u044c\u043d\u0438\u0439 \u0444\u0430\u043a\u0442\u0430\u0436 + \u0434\u0438\u0444\u0435\u0440\u0435\u043d\u0446\u0456\u0430\u0446\u0456\u044f Vitaran',
    },
    {
        'id': 'Q3', 'label': 'Competitor Rejuran lineup (Fix2)',
        'sq': '\u0440\u0435\u0434\u0436\u0443\u0440\u0430\u043d rejuran Vitaran PDRN porivnyannya',
        'ut': (
            '[SYS: zapyt pro konkurenta rejuran. '
            '1) Neytralnyy faktazh BEZ pozytvnykh otsinok. '
            '2) Perekhid do dyferentsiatsiyi Vitaran.]\n\n'
            'PYTANNYA: \u0420\u0430\u0441\u0441\u043a\u0430\u0436\u0438 \u043f\u0440\u043e \u043b\u0438\u043d\u0435\u0439\u043a\u0443 \u0420\u0435\u0434\u0436\u0443\u0440\u0430\u043d'
        ),
        'ck': ['vitaran'],
        'bad': [
            '\u0447\u0443\u0434\u043e\u0432\u0456 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0438',
            '\u0435\u0444\u0435\u043a\u0442\u0438\u0432\u043d\u0438\u0439',
            '\u043f\u043e\u043f\u0443\u043b\u044f\u0440\u043d\u0438\u0439',
            '\u0447\u0443\u0434\u043e\u0432\u0438\u0439',
        ],
        'ok_hint': '\u041d\u0435\u0439\u0442\u0440\u0430\u043b\u044c\u043d\u0438\u0439 + Vitaran \u0434\u0438\u0444\u0435\u0440\u0435\u043d\u0446\u0456\u0430\u0446\u0456\u044f',
    },
    {
        'id': 'Q4', 'label': 'Vitaran objection - doubt in result',
        'sq': 'Vitaran zaperech rezultat argumenty PDRN klinichni dani',
        'ut': '\u041d\u0435 \u0432\u043f\u0435\u0432\u043d\u0435\u043d\u0438\u0439 \u0443 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0456 Vitaran, \u043b\u0456\u043a\u0430\u0440 \u0441\u0443\u043c\u043d\u0456\u0432\u0430\u0454\u0442\u044c\u0441\u044f',
        'ck': ['vitaran'],
        'bad': ['\u0434\u043e 2 \u0440\u043e\u043a', '\u0434\u043e\u0432\u0433\u043e\u0442\u0440\u0438\u0432\u0430\u043b\u0438\u0439'],
        'ok_hint': 'SOS + PDRN \u043c\u0435\u0445\u0430\u043d\u0456\u0437\u043c + \u043f\u0438\u0442\u0430\u043d\u043d\u044f',
    },
    {
        'id': 'Q5', 'label': 'Follow-up "Da rasskazhy" (Fix1 test)',
        'sq': 'Vitaran PDRN argumenty perevahy detalno skrypt',
        'ut': (
            '[SYS: produkt - Vitaran. Korystuvach vidpoviv: Da rasskazhy podrobnee - '
            'tse pidtverdzhennya. Vykony te shcho proponuvav v ostanniy vidpovidi po Vitaran.]\n\n'
            'PYTANNYA: \u0414\u0430, \u0440\u0430\u0441\u0441\u043a\u0430\u0436\u0438 \u043f\u043e\u0434\u0440\u043e\u0431\u043d\u0435\u0435'
        ),
        'ck': ['vitaran'],
        'bad': ['\u0440\u0435\u0433\u043b\u0430\u043c\u0435\u043d\u0442', '\u0432\u0456\u0434\u043f\u0443\u0441\u0442\u043a'],
        'ok_hint': '\u041f\u0440\u043e\u0434\u043e\u0432\u0436\u0443\u0454 \u0442\u0435\u043c\u0443 Vitaran (\u043d\u0435 KB)',
    },
    {
        'id': 'Q6', 'label': 'Ellanse vs Juvelook - PCL first arg',
        'sq': 'Ellanse Juvelook PCL PLLA porivnyannya argumenty zaperech',
        'ut': (
            '[SYS: produkt - Ellanse (PCL-mikrosfery). Porivnyannya z Juvelook (PLLA). '
            'PERSHYY argument: mekhanizm PCL - nehaynyx obem + neokol I tipu. '
            'Ellanse = obem+lifting odnochasno, Juvelook = lyshe biostymulyatsiya. '
            'Ne kazhy pro tryvalist pershym.]\n\n'
            'PYTANNYA: \u041b\u0456\u043a\u0430\u0440 \u043a\u0430\u0436\u0435 \u0449\u043e Juvelook \u0434\u0435\u0448\u0435\u0432\u0448\u0438\u0439 \u0437\u0430 Ellanse'
        ),
        'ck': ['pcl', 'juvelook'],
        'bad': [
            '\u0434\u043e 2 \u0440\u043e\u043a', '\u0434\u043e\u0432\u0433\u043e\u0442\u0440\u0438\u0432\u0430\u043b\u0438\u0439 \u0435\u0444\u0435\u043a\u0442',
            '\u043c\u0435\u043d\u0448\u0435 \u043f\u043e\u0432\u0442\u043e\u0440\u043d\u0438\u0445',
        ],
        'ok_hint': 'PCL \u043f\u0435\u0440\u0448\u0438\u043c + Juvelook \u0432 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u0456',
    },
]

all_q = []
for case in QCASES:
    print()
    print('[' + case['id'] + '] ' + case['label'])
    try:
        ans = ask(case['sq'], case['ut'], k=20)
        al = ans.lower()
        ck_ok = [c for c in case['ck'] if c in al]
        ck_ms = [c for c in case['ck'] if c not in al]
        bd = [b for b in case['bad'] if b in al]
        q = 'FAIL' if bd else ('PASS' if len(ck_ok) >= max(1, len(case['ck']) // 2) else 'WARN')
        all_q.append({'id': case['id'], 'q': q, 'bad': bd, 'miss': ck_ms})
        print('  [' + q + '] bad=' + str(bd) + ' miss=' + str(ck_ms))
        print('  Expected: ' + case['ok_hint'])
        print('  >> ' + prev(ans))
    except Exception as e:
        print('  [ERROR] ' + str(e)[:120])
        all_q.append({'id': case['id'], 'q': 'ERROR', 'bad': [], 'miss': []})
    time.sleep(1)

print()
print('=' * 62)
print('FINAL SUMMARY')
print('=' * 62)
print('ROUTING ' + str(ok_r) + '/' + str(len(CASES)))
for r in all_q:
    ic = 'PASS' if r['q'] == 'PASS' else ('WARN' if r['q'] == 'WARN' else 'FAIL')
    ex = ' | BAD: ' + str(r['bad']) if r['bad'] else ''
    ex += ' | miss: ' + str(r['miss']) if r['miss'] else ''
    print('[' + ic + '] ' + r['id'] + ' ' + ex)
p = sum(1 for r in all_q if r['q'] == 'PASS')
w = sum(1 for r in all_q if r['q'] == 'WARN')
f = sum(1 for r in all_q if r['q'] in ('FAIL', 'ERROR'))
print()
print('LLM: ' + str(p) + ' PASS / ' + str(w) + ' WARN / ' + str(f) + ' FAIL  (of ' + str(len(all_q)) + ')')

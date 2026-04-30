"""Quick test for combo for indication query after combo diversification fix."""
import asyncio, os, sys
sys.path.insert(0, '/app')
from openai import AsyncOpenAI, OpenAI
import classifier as clf
from main import get_context, MODEL_OPENAI_COACH
from prompts_v2 import PROMPT_COACH_BASE


async def test():
    cli = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    sync_cli = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    q = 'комбо для контуру обличчя'
    cr = await clf.classify(cli, q, chat_history=[])
    print('CLASSIFIER:', cr.get('intent'), '| product=', cr.get('primary_product'))

    ctx, srcs = get_context(
        q, mode='combo', provider='openai',
        intent=cr.get('intent'),
        comparison_target=[],
        has_competitor=False,
        product_canonical=None,
    )
    print(f'\nCONTEXT chars: {len(ctx)}')
    print(f'SOURCES: {len(srcs)}')
    for ref_id, meta in list(srcs.items())[:8]:
        print(f'  {ref_id}: {meta.get("name", "")[:70]}')

    resp = sync_cli.chat.completions.create(
        model=MODEL_OPENAI_COACH,
        messages=[
            {'role': 'system', 'content': PROMPT_COACH_BASE},
            {'role': 'user', 'content': f'КОНТЕКСТ:\n{ctx}\n\nВОПРОС:\n{q}'},
        ],
        temperature=0.3, max_tokens=1500,
    )
    ans = resp.choices[0].message.content
    print(f'\n=== ANSWER ({len(ans)} chars) ===')
    print(ans)


if __name__ == '__main__':
    asyncio.run(test())

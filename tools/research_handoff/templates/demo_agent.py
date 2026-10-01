"""Local smoke task, no network/model/GPU. Demonstrates one context rotation."""
import time
from agent_protocol import context, context_usage, heartbeat, compact, turn_complete

ctx = context()
generation = ctx['generation']
conversation = ctx.get('conversation_id') or f'demo-{generation}'
if generation:
    assert ctx['handoff']['summary'] == 'Demo handoff: next generation may finish.'
context_usage(ctx['used_tokens'] + 10, conversation_id=conversation)
for _ in range(5):
    heartbeat()
    time.sleep(.05)
if generation == 0:
    compact('Demo handoff: next generation may finish.')
turn_complete(credit=True)

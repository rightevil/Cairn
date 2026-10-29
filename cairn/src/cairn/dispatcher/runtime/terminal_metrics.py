"""Transport measurements only: never record input or model message content."""
import json
import time


def record(directory, event, milliseconds):
    with (directory/'terminal-metrics.jsonl').open('a', encoding='utf-8') as target:
        target.write(json.dumps({'event':event,'ms':round(max(0,milliseconds),3),'time':time.time()})+'\n')

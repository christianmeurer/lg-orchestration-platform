import httpx
import json

# Test batch_execute
resp = httpx.post('http://lula-runner:8088/v1/tools/batch_execute', 
    json={'calls': [
        {'tool': 'list_files', 'input': {'path': '.'}},
        {'tool': 'search_files', 'input': {'path': '.', 'regex': 'def', 'file_pattern': '*.py'}}
    ]}, 
    timeout=60
)
print('Status:', resp.status_code)
data = resp.json()
print('Results count:', len(data.get('results', [])))
for i, r in enumerate(data.get('results', [])):
    print(f'Result {i}: tool={r.get("tool")}, ok={r.get("ok")}, exit_code={r.get("exit_code")}')
    if not r.get("ok"):
        print(f'  stderr: {r.get("stderr", "")[:200]}')

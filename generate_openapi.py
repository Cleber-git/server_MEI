import json
from pathlib import Path
from main import app

schema = app.openapi()
assert schema["openapi"].startswith("3.1")
Path("openapi.json").write_text(
    json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

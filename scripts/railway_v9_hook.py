import base64, gzip, os
from pathlib import Path

root = Path("/app") if (Path("/app") / "main.py").exists() else Path(".")
main = root / "main.py"

def append_hook(import_name: str, call: str = "apply()") -> None:
    text = main.read_text(encoding="utf-8")
    if f"{import_name}.apply" in text:
        print("HOOK_EXISTS", import_name, flush=True)
        return
    block = (
        "\n\ntry:\n"
        f"    import {import_name}\n"
        f"    {import_name}.{call}\n"
        "except Exception as _hook_err:\n"
        f"    print('{import_name} apply failed', _hook_err)\n"
    )
    main.write_text(text + block, encoding="utf-8")
    print("HOOKED", import_name, flush=True)

for env_key, mod_name in (("AUTOPILOT_V9_B64", "cockpit_v9"), ("AUTOPILOT_V10_B64", "cockpit_v10_isolate")):
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        continue
    try:
        data = base64.b64decode(raw)
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
        (root / f"{mod_name}.py").write_text(data.decode("utf-8"), encoding="utf-8")
        append_hook(mod_name, "apply()")
    except Exception as exc:
        print(env_key, "decode/hook failed", exc, flush=True)

print("V9_HOOK_DONE", flush=True)

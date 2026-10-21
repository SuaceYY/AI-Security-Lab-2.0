"""Reuse an existing project Python with FastAPI without installing dependencies."""
import json
import sys
from pathlib import Path

from tools.dual_ai.web_batch import HumanRequired
from tools.issue_claimer.claimer import run_command, sanitized_opencode_env


def backend_python(root: Path) -> dict:
    candidates = []
    for folder in (root / ".venv", root / "backend" / ".venv", root / "venv", root / "backend" / "venv"):
        candidates.extend([folder / "Scripts" / "python.exe", folder / "bin" / "python"])
    candidates.append(Path(sys.executable))
    seen = set()
    probe = "import json,sys,fastapi; print(json.dumps({'python':sys.executable,'fastapi':fastapi.__version__}))"
    for path in candidates:
        if not path.is_file() or str(path) in seen:
            continue
        seen.add(str(path))
        result = run_command([str(path), "-c", probe], cwd=root, timeout=15, env=sanitized_opencode_env())
        if result.returncode != 0 or result.timed_out:
            continue
        try:
            data = json.loads(result.stdout)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("python") and data.get("fastapi"):
            return {"executable": str(path), "fastapi_version": data["fastapi"],
                    "selection": "existing_project_environment_or_current_python"}
    raise HumanRequired("现有项目虚拟环境和当前 Python 均无法导入 fastapi；请提供后端 Python 路径。未安装或更改依赖。")

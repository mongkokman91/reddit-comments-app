from pathlib import Path
import textwrap

code_text = textwrap.dedent(app_code).lstrip()
compile(code_text, "app.py", "exec")

artifact_path = Path("/mnt/data/Reddit_Research_Extractor_App.md")
artifact_path.write_text(
    "# Reddit Research Extractor — Complete `app.py`\n\n"
    "Copy the entire code block below and replace the contents of GitHub `app.py`.\n\n"
    "```python\n"
    + code_text
    + "\n```\n",
    encoding="utf-8",
)

print(f"Created: {artifact_path}")
print("Syntax check: passed")

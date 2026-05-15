"""
Fail CI if any migration file contains placeholder stubs.
"""
import sys
import re
from pathlib import Path

STUB_PATTERNS = [
    r"#\s*TODO",
    r"#\s*STUB",
    r"#\s*FIXME",
    r"^\s*pass\s*$",  # bare `pass` in upgrade()/downgrade()
    r"raise\s+NotImplementedError",
]

def main():
    migrations_dir = Path("alembic/versions")
    if not migrations_dir.exists():
        print("✅ No migrations directory found.")
        return

    failed = []
    for f in migrations_dir.glob("*.py"):
        content = f.read_text()
        for pattern in STUB_PATTERNS:
            if re.search(pattern, content, re.MULTILINE):
                failed.append((f.name, pattern))

    if failed:
        print("❌ Stub-like patterns found in migrations:")
        for fname, pattern in failed:
            print(f"  {fname}: matches `{pattern}`")
        sys.exit(1)
    print("✅ No stub migrations detected.")

if __name__ == "__main__":
    main()

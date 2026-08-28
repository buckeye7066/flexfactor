#!/data/data/com.termux/files/usr/bin/bash
# Install one allowlisted cloud SDK for the authenticated on-phone dashboard.
set -euo pipefail

provider="${1:-}"
case "$provider" in
  openai) package="openai==2.44.0" ;;
  anthropic) package="anthropic==0.116.0" ;;
  *) echo "provider must be openai or anthropic" >&2; exit 2 ;;
esac

# Termux manages pip itself. Rust/binutils are needed when pydantic-core has no
# matching Android wheel and must compile locally.
pkg install -y rust binutils python-pip
python -m pip install --disable-pip-version-check "$package"
python - "$provider" <<'PY'
import importlib
import sys

module = importlib.import_module(sys.argv[1])
print(sys.argv[1], getattr(module, "__version__", "installed"))
PY

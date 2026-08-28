#!/data/data/com.termux/files/usr/bin/bash
# ---------------------------------------------------------------------------
# setup.sh — make FlexFactor runnable ON AN ANDROID PHONE, with no laptop.
#
# WHY TERMUX AND NOT AN EMBEDDED PYTHON
# -------------------------------------
# FlexFactor's unit of work is: take a real checkout, branch it, install the
# project's own dependencies, run its build and test gates, have a model write
# fixes, gate each file, commit, push. Embedding a Python interpreter in an APK
# (Chaquopy) supplies the interpreter and none of that: no `git`, no `npm`, no
# project toolchain. The audit would start and then fail at the first gate — or
# worse, "pass" a gate it never actually ran.
#
# Termux is a real userland, so flexfactor.py runs unchanged. The Windows-only
# code paths in it are already guarded (`_winify` is a no-op off Windows, the
# .lnk/.ps1 handling is behind isfile/suffix checks), so nothing is stubbed out
# here either.
#
# WHAT IS HONESTLY DIFFERENT ON A PHONE
# -------------------------------------
#  - Playwright/e2e never runs. It already never runs headless (by design).
#  - Only ecosystems whose toolchain exists in Termux can be gated. Node and
#    Python are the realistic pair. .NET, Java/Gradle and Ruby are not.
#  - The FCC free proxy is a laptop service. On the phone it is simply absent,
#    and FlexFactor already fails soft to the next provider — see PROVIDERS
#    below, because "fails soft" here means "spends money", which you should
#    choose on purpose.
#
# PREREQUISITE: Termux, from F-Droid (the Play Store build cannot install
# packages).
#
# USAGE (inside Termux):
#   bash setup.sh
#   GH_TOKEN=ghp_xxx WITH_SDK=1 bash setup.sh
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_URL="${FLEXFACTOR_REPO_URL:-https://github.com/buckeye7066/flexfactor.git}"
ROOT="${PHONE_CONSOLE_ROOT:-$HOME/phone-console}"
APP_DIR="$ROOT/flexfactor"

say() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

[ -d /data/data/com.termux/files/usr ] || \
  die "this script is for Termux on Android. On the desktop use flexfactor_launch.ps1."

# --- 1. packages ----------------------------------------------------------
if [ "${FLEXFACTOR_SKIP_PACKAGES:-0}" != "1" ]; then
  say "installing packages"
  pkg update -y
  # python: the engine. git: mandatory, every audit branches and commits.
  # nodejs-lts + esbuild-capable npm: needed to GATE JavaScript projects.
  # gh: optional, used only for pr create/merge (guarded by `which gh`).
  pkg install -y python git gh nodejs-lts openssh which termux-api
fi
command -v python >/dev/null || die "python did not install"
command -v git    >/dev/null || die "git did not install (audits cannot run without it)"
say "python $(python -V 2>&1 | awk '{print $2}'), git $(git --version | awk '{print $3}')"

# --- 2. GitHub auth -------------------------------------------------------
if ! gh auth status >/dev/null 2>&1; then
  say "GitHub login required (repo is private)"
  if [ -n "${GH_TOKEN:-}" ]; then
    printf '%s' "$GH_TOKEN" | gh auth login --with-token
  elif [ "${FLEXFACTOR_NONINTERACTIVE:-0}" = "1" ]; then
    die "GitHub is not signed in. Open Termux and run: gh auth login --web --git-protocol https"
  else
    echo "Paste a GitHub token with 'repo' scope, then press Enter:"
    read -r _tok
    [ -n "$_tok" ] || die "no token given; cannot clone a private repo"
    printf '%s' "$_tok" | gh auth login --with-token
  fi
  gh auth status >/dev/null 2>&1 || die "gh auth did not take"
fi
gh auth setup-git

# --- 3. source ------------------------------------------------------------
mkdir -p "$ROOT"
if [ -d "$APP_DIR/.git" ]; then
  say "updating existing checkout at $APP_DIR"
  git -C "$APP_DIR" fetch --prune origin
  git -C "$APP_DIR" checkout main
  git -C "$APP_DIR" pull --ff-only origin main
else
  say "cloning $REPO_URL"
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

# --- 4. providers ---------------------------------------------------------
# FlexFactor has NO hard pip dependencies; the dashboard, the web dashboard and
# the whole engine are stdlib. Only the cloud providers need SDKs, and those
# SDKs pull pydantic-core, which is Rust and has no aarch64/Android wheel — so
# pip BUILDS it here. That is a long compile and it is opt-in for that reason.
if [ "${WITH_SDK:-0}" = "1" ]; then
  say "installing cloud provider SDKs (pydantic-core compiles from source; slow)"
  # NOT `pip install --upgrade pip`: Termux refuses it outright --
  #   ERROR: Installing pip is forbidden, this will break the python-pip package
  # -- and under `set -e` that aborts the whole setup before a single SDK is
  # fetched. pip is a pkg-managed component here, so it is installed the same
  # way everything else is. Measured on an S25 Ultra.
  pkg install -y rust binutils python-pip
  # Pinned to the pair the desktop is tested against (requirements.txt).
  pip install "anthropic==0.116.0" "openai==2.44.0" || die "SDK build failed.
This is the known pydantic-core/Rust build. Options, in order of honesty:
  1. Re-run with more free storage and RAM (the build is heavy).
  2. Use --provider ollama with an Ollama server on this phone (no pip at all).
  3. Accept that this phone reports on runs rather than starting cloud ones."
  python - <<'PY'
import anthropic, openai
print("anthropic", anthropic.__version__, "openai", openai.__version__)
PY
else
  cat <<'EOF'

  PROVIDERS — read this, it decides whether audits can run at all here.

    The desktop routes every call through the FCC free proxy on 127.0.0.1:8082.
    On this phone that address is THIS phone, and FCC is not installed, so the
    free route is simply not available. FlexFactor detects that and moves on
    (shutil.which("fcc-server") -> None), it does not hang.

    That leaves two real choices:
      a) Cloud SDKs: re-run this script with WITH_SDK=1. Costs money per run.
      b) Ollama in Termux: no pip, no cloud, no cost — and slow, with small
         models. `--provider ollama` and OLLAMA_BASE_URL on loopback.

    Until one of those is in place this phone can WATCH runs (the web
    dashboard below) but cannot START one. That is stated plainly rather than
    letting the first audit fail three minutes in.

EOF
fi

# --- 5. program roots -----------------------------------------------------
# flexfactor.py resolves a bare `--program NAME` by scanning _PROJECT_ROOTS,
# whose built-in default is Windows-absolute. Point it at the phone's checkouts
# so `--program flexfactor` works here the way it does on the desk.
PROFILE="$HOME/.flexfactor-phone.env"
cat > "$PROFILE" <<EOF
# sourced by flexfactor-engine
export FLEXFACTOR_PROJECT_ROOTS="$ROOT:$HOME"
EOF
grep -q 'flexfactor-phone.env' "$HOME/.bashrc" 2>/dev/null || \
  echo '[ -f "$HOME/.flexfactor-phone.env" ] && . "$HOME/.flexfactor-phone.env"' >> "$HOME/.bashrc"

# --- 6. supervisor on PATH ------------------------------------------------
# chmod is not belt-and-braces, it is the fix for a defect this hit on a real
# phone: these files were committed 100644, so the symlink resolved to a
# non-executable target and `flexfactor-engine start` died with "Permission
# denied" -- which reads like an Android sandbox problem, not a mode bit. The
# blobs are 100755 now; this keeps it working if anyone's umask, filesystem or
# zip-based copy loses the bit again.
mkdir -p "$HOME/.local/bin"
chmod +x "$APP_DIR/scripts/phone/engine.sh" "$APP_DIR/scripts/phone/setup.sh" \
  "$APP_DIR/scripts/phone/install-provider.sh"
ln -sf "$APP_DIR/scripts/phone/engine.sh" "$HOME/.local/bin/flexfactor-engine"
# Test the FILE, not the live $PATH. Testing $PATH is subtly wrong: whoever
# runs setup often already has ~/.local/bin exported, so the check passes, the
# line is never written, and an interactive shell later cannot find the engine.
for rc in "$HOME/.bashrc" "$HOME/.profile"; do
  touch "$rc"
  grep -q 'local/bin' "$rc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
done

# --- 7. start at boot -----------------------------------------------------
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/10-flexfactor.sh" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
exec "$HOME/.local/bin/flexfactor-engine" start
BOOT
chmod +x "$HOME/.termux/boot/10-flexfactor.sh"

# Termux intentionally requires this property in addition to Android's
# RUN_COMMAND permission before the FlexFactor icon may start or repair the
# engine. setup.sh runs as the Termux user, so this remains an explicit Termux
# owner action rather than an APK bypass of Termux-private storage.
TERMUX_PROPERTIES="$HOME/.termux/termux.properties"
touch "$TERMUX_PROPERTIES"
if grep -q '^[[:space:]]*allow-external-apps[[:space:]]*=' "$TERMUX_PROPERTIES"; then
  sed -i 's/^[[:space:]]*allow-external-apps[[:space:]]*=.*/allow-external-apps=true/' "$TERMUX_PROPERTIES"
else
  printf '\nallow-external-apps=true\n' >> "$TERMUX_PROPERTIES"
fi
command -v termux-reload-settings >/dev/null && termux-reload-settings || true

say "setup complete"
cat <<'EOF'

  Next:
    flexfactor-engine start            # dashboard on 127.0.0.1:8765
    flexfactor-engine run <program>    # a REAL audit, on this phone
    flexfactor-engine status

  "start" also hands the freshly-minted dashboard token to the FlexFactor app
  on this phone. Approve the on-phone connection when the app asks; there is
  nothing secret to type.

  Install Termux:Boot from F-Droid so the dashboard survives a reboot.
EOF
